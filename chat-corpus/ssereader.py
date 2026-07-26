"""Live SSE consumer for ``GET /feed/chats`` — SPEC §6.

Design facts this module is built around, all source-verified in the recon reports:

* The server closes the stream roughly **every 15 minutes** (``new SseEmitter()`` inherits
  ``spring.mvc.async.request-timeout=15m``) and it looks like a clean EOF, not an error.
  ~96+ reconnects/day is *healthy*: ``CLEAN_EOF`` is logged at DEBUG and followed by a
  0.5–2.0 s sleep (SPEC §6.4, §11.3).
* There is no ``id:`` field and no ``Last-Event-ID`` support, so replay is impossible and
  **every** disconnect is a hole that must be handed to the backfiller. The gap is
  persisted through ``Writer.enqueue_gap`` *before* the next connect (SPEC §6.5).
* The wire format is exactly ``data:{json}\\n\\n`` — no space after the colon, LF only, no
  ``event:`` line. A named event listener would therefore receive nothing, forever,
  silently, so this parser is field-driven and dispatches on the blank line only. It still
  tolerates CRLF, one optional space after ``data:``, and multi-line data fields (SPEC §6.2).
* Liveness is judged from **inbound frames** (``:heartbeat`` comments included), never from
  data events: a 50 s gap between chat messages is routine, and the heartbeat runs on a
  global 60.000 s schedule whose phase relative to our connect is arbitrary (SPEC §6.3).
* The edge caps ``/feed/*`` at 6 concurrent connections per IP, so exactly one connection is
  held and the invariant is checked, not assumed: the old socket is closed before a new one
  is created and ``_assert_no_conn`` raises if anything is still live (SPEC §1, §12.12).

Privacy (SPEC §11.4, standing operator order): no chat text, component text or record body
ever reaches a log line or ``status.json``. Exceptions are summarised by class name and
frame locations only — ``http.client.IncompleteRead``'s repr embeds up to 20 bytes of the
partial body, and a normalizer's message could quote a source value, so ``str(exc)`` is
never logged from this module.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import random
import socket
import threading
import time
import traceback
from collections import deque
from typing import Dict, List, Optional, Tuple

import record
import tsutil

__all__ = [
    "SseReader",
    "CLEAN_EOF",
    "BROKEN",
    "RATE_LIMITED",
    "STOPPED",
]

# --------------------------------------------------------------------------- #
# close modes (SPEC §6.4) — three modes, three backoff policies
# --------------------------------------------------------------------------- #

CLEAN_EOF = "CLEAN_EOF"
BROKEN = "BROKEN"
RATE_LIMITED = "RATE_LIMITED"
STOPPED = "STOPPED"  # not a close mode: an operator-requested shutdown, never backed off

#: SPEC §6.4 — the CLEAN_EOF sleep window. Deliberately tiny: a clean EOF is the normal
#: 15-minute server-side stream cap, not a fault.
CLEAN_EOF_SLEEP_MIN_S = 0.5
CLEAN_EOF_SLEEP_MAX_S = 2.0

#: SPEC §6.5 — ``end_us`` overshoot. Overshoot is free: the overlap rows are inside the
#: dedupe window by construction.
GAP_END_SLACK_US = 5000000

#: Smallest amount of newly-uncovered wall clock that justifies another gap. Guards against
#: gap spam if the server ever enters a sub-second connect/EOF spin loop; any interval
#: shorter than this is covered many times over by the next gap's ``safety_lag`` anyway.
MIN_GAP_SPACING_US = 1000000

#: Read size. Frames are ~600 B, so this is one syscall per burst.
READ_CHUNK_BYTES = 65536

#: A single SSE line may never be this long. If it is, the peer is not speaking SSE and the
#: buffer is dropped rather than grown without bound.
MAX_LINE_BYTES = 4194304

#: Chunk-size and trailer lines are a few bytes; anything longer is not HTTP framing.
MAX_CHUNK_LINE_BYTES = 1024

#: Throttle for ``StatusPublisher.update`` from the read loop.
STATUS_MIN_INTERVAL_S = 2.0

_BACKOFF_N_MAX = 16

_FALLBACK_USER_AGENT = "chat-corpus-collector/1.0"

#: U+FFFD, what errors="replace" leaves behind on malformed UTF-8 (SPEC §6.2).
_REPLACEMENT_CHAR = "�"

_SSE_DEFAULTS = {
    "enabled": True,
    "host": "api.2b2t.vc",
    "path": "/feed/chats",
    "socket_read_timeout_s": 20,
    "heartbeat_deadline_s": 90,
    "initial_grace_s": 120,
    "backoff_reset_s": 60,
    "backoff_broken_max_s": 60,
    "backoff_429_max_s": 300,
}

_SAFETY_LAG_S_DEFAULT = 120

_FIELD_PREFIXES = (b"event:", b"id:", b"retry:")


def _https_connection(host: str, timeout: float) -> http.client.HTTPSConnection:
    """Open the one TLS connection this process is allowed to hold (SPEC §6.1).

    ``http.client`` only: ``urllib.request`` would follow redirects, buffer the body and
    hide heartbeats. This is the single seam a test may replace to point the reader at a
    loopback HTTP server; there is no configuration key that can downgrade production
    traffic to plaintext.
    """
    return http.client.HTTPSConnection(host, timeout=timeout)


def _exc_summary(exc: BaseException) -> str:
    """Describe an exception with metadata only (SPEC §11.4).

    Never ``str(exc)``: ``IncompleteRead``'s repr carries up to 20 bytes of the partial
    response body, and a record normalizer's message could in principle quote a source
    value. Class name, ``errno`` and the innermost frame locations are enough to diagnose a
    socket fault and cannot contain chat.
    """
    parts = [type(exc).__name__]
    errno = getattr(exc, "errno", None)
    if isinstance(errno, int):
        parts.append("errno=%d" % (errno,))
    tb = getattr(exc, "__traceback__", None)
    if tb is not None:
        frames = traceback.extract_tb(tb)[-3:]
        where = ",".join(
            "%s:%d" % (os.path.basename(f.filename or "?"), f.lineno or 0)
            for f in frames
        )
        if where:
            parts.append("at=" + where)
    return " ".join(parts)


def _sanitize_user_agent(value: object) -> str:
    """Build the identifying UA header (SPEC §6.1).

    Cloudflare 403s urllib's default UA, and ``http.client`` sends none at all, so an empty
    configuration value must not produce a header-less request. CR/LF/TAB are stripped so a
    configuration typo cannot inject a header.
    """
    text = value if isinstance(value, str) else ""
    # split()/join() collapses CR, LF, TAB and runs of spaces in a single pass.
    text = " ".join(text.split())
    return text or _FALLBACK_USER_AGENT


def _unlatch_read_timeout(resp: object) -> bool:
    """Make a ``socket.timeout`` survivable so it can be the watchdog tick (SPEC §6.1, §6.3).

    ``socket.SocketIO`` latches ``_timeout_occurred`` on the first timeout and *every*
    subsequent read then raises ``OSError("cannot read from timed out object")``. Left alone
    that turns SPEC §6.1's "every socket.timeout on read() is a normal event" into a
    guaranteed reconnect one ``socket_read_timeout_s`` after connect — 3 connections a
    minute against a 6-connection cap, i.e. a self-inflicted 429 storm.

    Clearing the latch is sound *where this reader does it*: after a timeout in ``peek``,
    which performs a single ``recv_into`` and never advances the stream. A ``recv_into`` that
    times out has delivered no bytes at all, so nothing was consumed and nothing is lost by
    reading again. Returns True if a latch was found and cleared.
    """
    fp = getattr(resp, "fp", None)
    for candidate in (getattr(fp, "raw", None), fp):
        if candidate is None:
            continue
        if getattr(candidate, "_timeout_occurred", False):
            try:
                candidate._timeout_occurred = False
                return True
            except Exception:  # pragma: no cover - attribute is writable in CPython
                return False
    return False


class _FramingError(Exception):
    """The peer's transfer framing is unusable. Metadata-only message (SPEC §11.4)."""


class _BodyDecoder:
    """Incremental transfer-decoder for a streaming response body (SPEC §6.2 support).

    ``http.client``'s own chunked decoder cannot be used here. It keeps its framing state in
    ``chunk_left`` and re-issues the "toss the trailing CRLF" read on the call after a
    ``socket.timeout``, which eats two bytes of the next chunk-size line and desyncs the
    stream into ``IncompleteRead``. That is fatal for this reader specifically, because SPEC
    §6.1 makes the read timeout the *normal* watchdog tick on a feed that is legitimately
    silent for up to 60 s between heartbeats.

    This decoder keeps all framing state in a buffer it owns, so a timeout is a no-op: no
    bytes were consumed, nothing to resynchronise. It also means buffered bytes can never
    hide from the loop the way they do behind a readiness check like ``select``.
    """

    _SIZE = "size"
    _BODY = "body"
    _CRLF = "crlf"
    _TRAILER = "trailer"
    _DONE = "done"

    def __init__(self, chunked: bool, length: Optional[int] = None) -> None:
        self._chunked = bool(chunked)
        self._length = length if (isinstance(length, int) and not self._chunked) else None
        self._buf = bytearray()
        self._state = self._SIZE
        self._remaining = 0
        self.done = False

    def expects_more(self) -> bool:
        """True if EOF here means a truncated body (SPEC §6.4: that is BROKEN, not clean)."""
        if self._chunked:
            # Once the terminating ``0`` chunk has been read the body is complete; only
            # the optional trailer section and its blank line are outstanding. A segment
            # boundary that splits ``0\r\n`` from the final ``\r\n`` would otherwise turn
            # the routine 15-minute server-side stream cap into BROKEN, which escalates
            # the backoff exponent and logs at WARNING for a normal event.
            if self._state in (self._TRAILER, self._DONE):
                return False
            return not self.done
        if self._length is not None:
            return self._length > 0
        return False  # identity framing: EOF *is* the end of the body

    def feed(self, data: bytes) -> bytes:
        """Decode one inbound read into body bytes. Raises ``_FramingError``."""
        if not self._chunked:
            if self._length is None:
                return data
            take = data[: self._length]
            self._length -= len(take)
            if self._length <= 0:
                self.done = True
            return bytes(take)

        self._buf += data
        out = bytearray()
        while True:
            if self._state == self._SIZE:
                idx = self._buf.find(b"\n")
                if idx < 0:
                    if len(self._buf) > MAX_CHUNK_LINE_BYTES:
                        raise _FramingError("chunk size line > %d B" % (MAX_CHUNK_LINE_BYTES,))
                    break
                line = bytes(self._buf[:idx]).rstrip(b"\r")
                del self._buf[: idx + 1]
                if not line:
                    continue  # tolerate a stray blank line before a size
                token = line.split(b";", 1)[0].strip()  # drop chunk extensions
                try:
                    size = int(token, 16)
                except ValueError:
                    raise _FramingError("unparseable chunk size bytes=%d" % (len(token),))
                if size < 0:
                    raise _FramingError("negative chunk size")
                if size == 0:
                    self._state = self._TRAILER
                else:
                    self._remaining = size
                    self._state = self._BODY
            elif self._state == self._BODY:
                if not self._buf:
                    break
                take = min(self._remaining, len(self._buf))
                out += self._buf[:take]
                del self._buf[:take]
                self._remaining -= take
                if self._remaining == 0:
                    self._state = self._CRLF
            elif self._state == self._CRLF:
                idx = self._buf.find(b"\n")
                if idx < 0:
                    if len(self._buf) > 2:
                        raise _FramingError("garbage after chunk body")
                    break
                del self._buf[: idx + 1]
                self._state = self._SIZE
            elif self._state == self._TRAILER:
                idx = self._buf.find(b"\n")
                if idx < 0:
                    if len(self._buf) > MAX_CHUNK_LINE_BYTES:
                        raise _FramingError("trailer > %d B" % (MAX_CHUNK_LINE_BYTES,))
                    break
                line = bytes(self._buf[:idx]).rstrip(b"\r")
                del self._buf[: idx + 1]
                if not line:
                    self._state = self._DONE
                    self.done = True
                    break
            else:
                break
        return bytes(out)


def _num(section: dict, key: str) -> float:
    """Read one numeric ``sse`` setting, falling back to the SPEC §9 default."""
    value = section.get(key, _SSE_DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        value = _SSE_DEFAULTS[key]
    return float(value)


class SseReader(threading.Thread):
    """The process's one and only ``/feed/chats`` consumer (SPEC §6).

    Owns a connect → parse → watchdog → classify → gap → backoff loop and nothing else: it
    never sorts, never polls ``/chats/window``, and never touches disk directly. Rows and
    gaps go through the writer, which owns durability (SPEC §6.6).
    """

    def __init__(
        self,
        cfg: dict,
        writer: "Writer",
        status: "StatusPublisher",
        log: logging.Logger,
        stop: threading.Event,
    ) -> None:
        """Wire the reader up. No sockets, no disk, no threads started (SPEC §6.1)."""
        threading.Thread.__init__(self, name="sse-reader", daemon=True)
        self._cfg = cfg
        self._writer = writer
        self._status = status
        self._log = log
        # NB: NOT ``self._stop`` — threading.Thread._stop() is real machinery that join()
        # calls; shadowing it with an Event breaks join() in a way that only shows up at
        # shutdown.
        self._stop_ev = stop

        sse = cfg.get("sse") or {}
        if not isinstance(sse, dict):
            sse = {}
        backfill = cfg.get("backfill") or {}
        if not isinstance(backfill, dict):
            backfill = {}

        self._enabled = bool(sse.get("enabled", True))
        self._host = str(sse.get("host") or _SSE_DEFAULTS["host"])
        self._path = str(sse.get("path") or _SSE_DEFAULTS["path"])
        self._read_timeout_s = _num(sse, "socket_read_timeout_s")
        self._deadline_s = _num(sse, "heartbeat_deadline_s")
        self._grace_s = _num(sse, "initial_grace_s")
        self._backoff_reset_s = _num(sse, "backoff_reset_s")
        self._broken_max_s = _num(sse, "backoff_broken_max_s")
        self._rate_limited_max_s = _num(sse, "backoff_429_max_s")
        self._user_agent = _sanitize_user_agent(cfg.get("user_agent"))

        lag_s = backfill.get("safety_lag_s", _SAFETY_LAG_S_DEFAULT)
        if isinstance(lag_s, bool) or not isinstance(lag_s, (int, float)):
            lag_s = _SAFETY_LAG_S_DEFAULT
        #: SPEC §6.5/§12.8 — never tighten this on the grounds that out-of-order delivery
        #: "probably does not happen": the fan-out is one virtual thread per message.
        self._safety_lag_us = int(float(lag_s) * 1000000)

        # ---- seams (a test may replace these; production never does) ----------------
        self._conn_factory = _https_connection
        self._clean_eof_sleep_s = (CLEAN_EOF_SLEEP_MIN_S, CLEAN_EOF_SLEEP_MAX_S)
        self._min_gap_spacing_us = MIN_GAP_SPACING_US
        self._status_min_interval_s = STATUS_MIN_INTERVAL_S

        # ---- connection state -------------------------------------------------------
        self._conn = None  # type: Optional[http.client.HTTPConnection]
        self._resp = None  # type: Optional[http.client.HTTPResponse]
        self._open_conns = 0
        self._max_open_conns = 0
        self._connections = self._seed_connection_ordinal()
        self._connect_mono = None  # type: Optional[float]
        self._last_session_s = 0.0
        self._backoff_n = 0
        self._last_backoff_s = 0.0
        self._recent_connects = deque()  # type: deque
        self._state = "starting"
        self._last_close_mode = None  # type: Optional[str]
        self._last_close_detail = None  # type: Optional[str]

        # ---- frame parser state (reset per connection) -------------------------------
        self._buf = bytearray()
        self._data_lines = []  # type: List[bytes]
        self._logged_fields = set()

        # ---- gaps -------------------------------------------------------------------
        self._gap_pending = False
        self._last_gap_end_us = None  # type: Optional[int]
        self._gaps_emitted = 0

        # ---- counters (metadata only; never any record body) ------------------------
        now = time.monotonic()
        self._start_mono = now
        self._last_inbound_mono = now
        self._last_event_mono = now
        self._last_publish_mono = 0.0
        self._heartbeats = 0
        self._comments = 0
        self._events = 0
        self._rows_written = 0
        self._dupes = 0
        self._parse_errors = 0
        self._unknown_lines = 0
        self._utf8_replacements = 0
        self._stalls = 0
        self._bytes_in = 0
        self._rand = random.Random()

    # ------------------------------------------------------------------ thread entry --

    def run(self) -> None:
        """Thread entry point (SPEC §6). Never raises; a death is always visible.

        An exception escaping here would otherwise leave a live process with a dead reader
        collecting nothing. Instead the state flips to ``error``, which SPEC §11.3 scores as
        not-``ok``, and the collector's dead-thread check does the rest.
        """
        try:
            self._loop()
        except Exception as exc:
            self._state = "error"
            self._log.error("sse reader thread died err=%s", _exc_summary(exc))
        finally:
            try:
                self._close_conn()
            except Exception as exc:  # pragma: no cover - defensive
                self._log.debug("sse close on exit failed err=%s", _exc_summary(exc))
            if self._state not in ("error", "disabled"):
                self._state = "stopped"
            self._publish(force=True)

    # ------------------------------------------------------------------- main loop ----

    def _loop(self) -> None:
        """Connect / stream / classify / gap / backoff, forever (SPEC §6.4, §6.5)."""
        if not self._enabled:
            self._state = "disabled"
            self._log.info("sse disabled by config")
            self._publish(force=True)
            return

        # SPEC §6.5: process start is itself a connected->disconnected transition as far as
        # the corpus is concerned — whatever happened while we were down is a hole.
        self._gap_pending = True

        while not self._stop_ev.is_set():
            self._emit_pending_gap()
            if self._stop_ev.is_set():
                break
            mode, detail = self._one_connection()
            self._note_close(mode, detail)
            if self._stop_ev.is_set():
                break
            self._backoff(mode)

    # ------------------------------------------------------------------------ gaps ----

    def _emit_pending_gap(self) -> None:
        """Persist the reconnect hole *before* reconnecting (SPEC §6.5).

        ``start = hwm_us - safety_lag_us`` because events can arrive out of order, and
        ``end = now + 5 s`` because the connect attempt happens immediately after this
        returns. The writer clamps to ``backfill.max_lookback_days``, skips a zero-length
        range (``startDate == endDate`` is a 400, not an empty result) and persists
        ``state.json`` before returning, so the gap is durable the moment we reconnect.
        """
        if not self._gap_pending:
            return
        end_us = tsutil.now_us() + GAP_END_SLACK_US
        if (
            self._last_gap_end_us is not None
            and end_us - self._last_gap_end_us < self._min_gap_spacing_us
        ):
            self._gap_pending = False
            self._log.debug("sse gap coalesced end_us=%d", end_us)
            return
        start_us = self._writer.hwm_us() - self._safety_lag_us
        tag = "sse:" + (self._last_close_mode or "start")
        gap_id = self._writer.enqueue_gap(start_us, end_us, tag=tag)
        # Flags advance only after a durable enqueue: if the writer raises, the gap stays
        # pending and the thread dies loudly rather than dropping a hole on the floor.
        self._gap_pending = False
        self._last_gap_end_us = end_us
        if gap_id is None:
            self._log.debug(
                "sse gap skipped start_us=%d end_us=%d tag=%s", start_us, end_us, tag
            )
            return
        self._gaps_emitted += 1
        self._log.info(
            "sse gap queued gap_id=%s start_us=%d end_us=%d tag=%s",
            gap_id,
            start_us,
            end_us,
            tag,
        )

    # ------------------------------------------------------------------ connection ----

    def _assert_no_conn(self) -> None:
        """Enforce hard invariant #1: at most one open connection, ever (SPEC §1, §12.12).

        A leaked socket manufactures its own 429s against the 6-connection edge cap, so this
        is a real check and not an ``assert`` statement (``python -O`` strips those).
        """
        if self._conn is not None or self._resp is not None or self._open_conns != 0:
            raise RuntimeError(
                "sse refuses to open a second connection open_conns=%d conn=%s resp=%s"
                % (self._open_conns, self._conn is not None, self._resp is not None)
            )

    def _headers(self) -> Dict[str, str]:
        """Request headers for the feed (SPEC §6.1)."""
        return {
            "Accept": "text/event-stream",
            # Never gzip: a decompression buffer hides heartbeats and defeats the watchdog.
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
            "User-Agent": self._user_agent,
            "Connection": "close",
        }

    def _close_conn(self) -> None:
        """Close the response and the socket, unconditionally and idempotently (SPEC §1)."""
        resp, self._resp = self._resp, None
        conn, self._conn = self._conn, None
        if resp is not None:
            try:
                resp.close()
            except Exception as exc:
                self._log.debug("sse response close failed err=%s", _exc_summary(exc))
        if conn is not None:
            try:
                conn.close()
            except Exception as exc:
                self._log.debug("sse socket close failed err=%s", _exc_summary(exc))
            self._open_conns = max(0, self._open_conns - 1)

    def _one_connection(self) -> Tuple[str, str]:
        """Hold exactly one connection for its whole life. Returns ``(mode, detail)``.

        SPEC §6.1 for the request, §6.4 for the classification of every way it can end.
        Never raises for a network fault: a fault is a close mode, not an exception.
        """
        self._assert_no_conn()
        ordinal = self._connections + 1
        batch = "c%d" % (ordinal,)
        self._state = "connecting"
        self._publish(force=True)
        try:
            conn = self._conn_factory(self._host, self._read_timeout_s)
            self._conn = conn
            self._open_conns += 1
            if self._open_conns > self._max_open_conns:
                self._max_open_conns = self._open_conns
            conn.request("GET", self._path, headers=self._headers())
            resp = conn.getresponse()
            self._resp = resp
        except Exception as exc:
            self._log.warning("sse connect failed err=%s", _exc_summary(exc))
            self._close_conn()
            return BROKEN, "connect_error"

        try:
            status_code = int(getattr(resp, "status", 0))
            ctype = resp.getheader("Content-Type") or ""
            clen = resp.getheader("Content-Length") or "-"
            if status_code == 429:
                # SPEC §6.4/§12.5: never branch on the body or the content-type of a 429 —
                # the edge sends text/html, the app sends text/plain, and no Retry-After is
                # sent by either. Log them for diagnosis and back off long.
                self._log.warning(
                    "sse 429 at connect content_type=%r content_length=%s server=%r",
                    ctype,
                    clen,
                    resp.getheader("Server") or "",
                )
                return RATE_LIMITED, "status_429"
            if status_code != 200:
                # Includes 3xx: redirects are never followed anywhere in this program.
                self._log.warning(
                    "sse unexpected status=%d content_type=%r", status_code, ctype
                )
                return BROKEN, "status_%d" % (status_code,)
            if ctype.split(";")[0].strip().lower() != "text/event-stream":
                # NOT a warning-and-continue. A 200 that is not an event stream (an edge
                # challenge page, an error page, the Scalar explorer served at 200 rather
                # than behind the §7.5 302) has a body that ends immediately, which reads
                # as a clean EOF — and CLEAN_EOF is the *routine* path: 0.5-2.0 s sleep,
                # backoff exponent untouched. That is a ~2700 connect/hour spin against a
                # 6-connection edge cap, i.e. a self-inflicted 429 storm, with a durable
                # gap queued per iteration and health capped at warn. Treat it as BROKEN
                # so the exponential backoff applies.
                self._log.warning("sse unexpected content_type=%r on 200; treating as "
                                  "broken", ctype)
                return BROKEN, "content_type"
            self._connections = ordinal
            self._on_connected()
            return self._read_stream(resp, batch)
        finally:
            self._close_conn()

    def _on_connected(self) -> None:
        """Reset per-connection state and start the watchdog clock (SPEC §6.3)."""
        now = time.monotonic()
        self._connect_mono = now
        # SPEC §2 key 12 / §10: the connection ordinal in `batch` is durable state, so the
        # writer's top-level `connections` counter advances with every accepted connect.
        self._writer.bump("connections")
        # Liveness is measured from inbound frames; the connect itself is the first one.
        self._last_inbound_mono = now
        self._buf = bytearray()
        self._data_lines = []
        self._recent_connects.append(now)
        self._prune_connects(now)
        self._state = "connected"
        self._log.info(
            "sse connected conn=%d reconnects_last_hour=%d",
            self._connections,
            len(self._recent_connects),
        )
        self._publish(force=True)

    def _prune_connects(self, now: float) -> None:
        """Keep the reconnect-rate window at one hour (SPEC §11.3)."""
        cutoff = now - 3600.0
        while self._recent_connects and self._recent_connects[0] < cutoff:
            self._recent_connects.popleft()

    # ---------------------------------------------------------------- stream reader ----

    def _read_stream(self, resp: "http.client.HTTPResponse", batch: str) -> Tuple[str, str]:
        """Read frames until the stream ends. Returns ``(mode, detail)`` (SPEC §6.2–§6.4).

        Bytes are pulled with ``fp.read1`` — one raw read, or whatever is already buffered —
        and transfer-decoded by ``_BodyDecoder``, which this module owns. Two properties come
        out of that and both are load-bearing:

        * A ``socket.timeout`` consumes nothing, anywhere. ``read1`` only reaches the socket
          when the buffer is empty, and a ``recv_into`` that times out delivers no bytes. So
          SPEC §6.1's "every socket.timeout on read() is a normal event — it is the watchdog
          tick" is literally true here, which it is *not* for ``HTTPResponse.read``/``read1``
          (see ``_BodyDecoder``).
        * Buffered bytes can never hide. A readiness check such as ``select`` sees the socket,
          not the buffered reader, so two frames arriving in one TCP segment would leave the
          second one stranded until the next heartbeat woke the socket up.
        """
        fp = getattr(resp, "fp", None)
        if fp is None or not hasattr(fp, "read1"):
            self._log.warning("sse response has no readable body conn=%d", self._connections)
            return BROKEN, "no_body"
        decoder = _BodyDecoder(
            bool(getattr(resp, "chunked", False)), getattr(resp, "length", None)
        )
        while True:
            if self._stop_ev.is_set():
                return STOPPED, "stop_requested"
            try:
                raw = fp.read1(READ_CHUNK_BYTES)
            except socket.timeout:
                # SPEC §6.1: the watchdog tick. Nothing was consumed, so the stream is intact.
                _unlatch_read_timeout(resp)
                if self._watchdog_breached():
                    return BROKEN, "stall"
                self._publish()
                continue
            except Exception as exc:
                # ConnectionReset / SSLError / anything else: BROKEN (SPEC §6.4).
                self._log.warning(
                    "sse read failed conn=%d err=%s", self._connections, _exc_summary(exc)
                )
                return BROKEN, "read_error"

            if not raw:
                if decoder.expects_more():
                    # FIN part-way through a chunked body: truncated, not orderly.
                    self._log.warning(
                        "sse truncated body conn=%d bytes=%d", self._connections, self._bytes_in
                    )
                    return BROKEN, "truncated"
                return self._clean_eof()

            self._last_inbound_mono = time.monotonic()
            self._bytes_in += len(raw)
            try:
                body = decoder.feed(raw)
            except _FramingError as exc:
                self._log.warning(
                    "sse framing error conn=%d err=%s", self._connections, _exc_summary(exc)
                )
                return BROKEN, "framing"
            if body and not self._feed(body, batch):
                return BROKEN, "line_overflow"
            if decoder.done:
                # The terminating chunk: an orderly completion, i.e. the 15-minute cap.
                return self._clean_eof()
            self._publish()

    def _clean_eof(self) -> Tuple[str, str]:
        """SPEC §6.4: the normal path — log at DEBUG, never ERROR."""
        self._log.debug(
            "sse clean eof conn=%d session_s=%.1f bytes=%d events=%d",
            self._connections,
            time.monotonic() - (self._connect_mono or time.monotonic()),
            self._bytes_in,
            self._events,
        )
        return CLEAN_EOF, "eof"

    def _watchdog_breached(self) -> bool:
        """Has the inbound-frame deadline passed? (SPEC §6.3).

        Judged on inbound frames — heartbeats included — never on data events: chat is
        routinely silent for 50 s. The initial grace exists because the ``:heartbeat``
        scheduler is global at a fixed 60.000 s rate, so a fresh connection can legitimately
        wait ~60 s for its first one.
        """
        now = time.monotonic()
        connect_mono = self._connect_mono
        if connect_mono is not None and (now - connect_mono) <= self._grace_s:
            return False
        since = now - self._last_inbound_mono
        if since <= self._deadline_s:
            return False
        self._stalls += 1
        self._log.warning(
            "sse watchdog stall conn=%d s_since_last_inbound=%.1f deadline_s=%.1f",
            self._connections,
            since,
            self._deadline_s,
        )
        return True

    # ----------------------------------------------------------------- frame parser ----

    def _feed(self, chunk: bytes, batch: str) -> bool:
        """Split the byte stream into SSE lines. False means "abandon this connection".

        SPEC §6.2. The buffer is a member, not a local, so a socket timeout mid-frame cannot
        discard bytes that already arrived.
        """
        self._buf += chunk
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            line = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            self._handle_line(line.rstrip(b"\r"), batch)
        if len(self._buf) > MAX_LINE_BYTES:
            # No newline in 4 MiB: the peer is not speaking SSE. Log the size only.
            self._log.error("sse line overflow buffered=%d", len(self._buf))
            self._buf = bytearray()
            self._data_lines = []
            return False
        return True

    def _handle_line(self, line: bytes, batch: str) -> None:
        """Dispatch one SSE line (SPEC §6.2).

        Field-driven by design: there is no ``event:`` line on this feed, so a listener bound
        to a named event would receive nothing forever. A blank line is the only dispatch
        trigger and a leading ``:`` is a comment — liveness only, never JSON-parsed.
        """
        if line == b"":
            if self._data_lines:
                payload = b"\n".join(self._data_lines)
                self._data_lines = []
                self._emit_event(payload, batch)
            return
        if line.startswith(b":"):
            self._comments += 1
            if line[1:].strip().lower().startswith(b"heartbeat"):
                self._heartbeats += 1
            return
        if line.startswith(b"data:"):
            value = line[5:]
            if value.startswith(b" "):  # the SSE spec allows exactly one optional space
                value = value[1:]
            self._data_lines.append(value)
            return
        for prefix in _FIELD_PREFIXES:
            if line.startswith(prefix):
                name = prefix.decode("ascii").rstrip(":")
                if name not in self._logged_fields:
                    self._logged_fields.add(name)
                    self._log.info("sse server started sending %r fields", name)
                return
        # Unknown line: count it, log its length only. It could be anything, including
        # chat-bearing junk, so the bytes themselves never reach the log (SPEC §11.4).
        self._unknown_lines += 1
        self._log.debug("sse unknown line bytes=%d", len(line))

    def _emit_event(self, payload: bytes, batch: str) -> None:
        """Normalize one dispatched event and hand it to the writer (SPEC §6.2, §2.1).

        ``errors="replace"`` on the UTF-8 step with a replacement counter: a corpus is worth
        more than a crash. Every failure below is counted, never fatal, and logged without
        the payload.
        """
        try:
            # Strict first, so the counter below means "the server sent malformed UTF-8",
            # not "the server legitimately sent U+FFFD". Counting every U+FFFD in the
            # decoded text reports decode failures that never happened.
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            text = payload.decode("utf-8", "replace")
            replacements = text.count(_REPLACEMENT_CHAR)
            self._utf8_replacements += replacements
            self._log.warning(
                "sse utf8 replacements=%d bytes=%d", replacements, len(payload)
            )
        except Exception as exc:  # pragma: no cover - defensive
            self._parse_errors += 1
            self._writer.bump("parse_errors")
            self._log.warning("sse decode failed bytes=%d err=%s", len(payload), _exc_summary(exc))
            return

        try:
            obj = json.loads(text)
        except Exception as exc:
            self._parse_errors += 1
            self._writer.bump("parse_errors")
            self._log.warning(
                "sse json parse failed bytes=%d err=%s", len(payload), _exc_summary(exc)
            )
            return
        if not isinstance(obj, dict):
            self._parse_errors += 1
            self._writer.bump("parse_errors")
            self._log.warning(
                "sse event is not a json object type=%s bytes=%d",
                type(obj).__name__,
                len(payload),
            )
            return

        try:
            rec = record.from_sse(
                obj,
                seq=self._writer.next_seq(),
                batch=batch,
                ingest_us=tsutil.now_us(),
            )
        except Exception as exc:
            # record.RecordError's message is metadata-only by contract, but this module
            # does not rely on that: only the class name is logged.
            self._parse_errors += 1
            self._writer.bump("parse_errors")
            self._log.warning(
                "sse normalize failed bytes=%d err=%s", len(payload), _exc_summary(exc)
            )
            return

        self._events += 1
        self._last_event_mono = time.monotonic()
        # A writer failure is a durability failure: it must not be swallowed into a silently
        # lossy corpus. It propagates to run(), which flips health to not-ok.
        fresh = self._writer.put_record(rec)
        if fresh:
            self._rows_written += 1
        else:
            self._dupes += 1
        self._log.debug(
            "sse row row_id=%s ts=%s seq=%s fresh=%s",
            rec.get("row_id"),
            rec.get("ts"),
            rec.get("seq"),
            bool(fresh),
        )

    # --------------------------------------------------------------------- backoff ----

    def _note_close(self, mode: str, detail: str) -> None:
        """Record how the connection ended and arm the reconnect gap (SPEC §6.4, §6.5)."""
        connect_mono = self._connect_mono
        self._last_session_s = (
            0.0 if connect_mono is None else max(0.0, time.monotonic() - connect_mono)
        )
        # Cleared so a *failed connect* can never be credited with the previous session's
        # health and reset the backoff exponent forever.
        self._connect_mono = None

        if mode == STOPPED:
            # An operator shutdown is not a close mode: `last_close_mode` keeps reporting the
            # last real one so status.json stays diagnostic after a restart request.
            self._state = "stopped"
            self._log.info(
                "sse stopping conn=%d session_s=%.1f", self._connections, self._last_session_s
            )
            self._publish(force=True)
            return

        self._last_close_mode = mode
        self._last_close_detail = detail

        # Every connected->disconnected transition is a hole, and so is a failed connect
        # attempt: wall clock passed with nothing streaming (SPEC §6.5).
        self._gap_pending = True
        self._state = "backoff"
        if mode == CLEAN_EOF:
            self._log.debug(
                "sse close mode=%s detail=%s conn=%d session_s=%.1f",
                mode,
                detail,
                self._connections,
                self._last_session_s,
            )
        else:
            self._log.warning(
                "sse close mode=%s detail=%s conn=%d session_s=%.1f",
                mode,
                detail,
                self._connections,
                self._last_session_s,
            )
        self._publish(force=True)

    def _delay_for(self, mode: str) -> float:
        """Backoff policy per close mode (SPEC §6.4). Advances the exponent as a side effect."""
        if mode == CLEAN_EOF:
            # The normal 15-minute path. Fixed small window, and the exponent is untouched:
            # ~96 clean EOFs a day must never grow into a 60 s reconnect delay.
            low, high = self._clean_eof_sleep_s
            return self._rand.uniform(low, high)
        n = self._backoff_n
        if mode == RATE_LIMITED:
            base = min(self._rate_limited_max_s, 60.0 * (2 ** n))
        else:
            base = min(self._broken_max_s, 1.0 * (2 ** n))
        self._backoff_n = min(n + 1, _BACKOFF_N_MAX)
        return base + self._rand.uniform(0.0, 0.5 * base)

    def _backoff(self, mode: str) -> None:
        """Sleep between connections (SPEC §6.4)."""
        if mode == STOPPED:
            return
        if self._backoff_n and self._last_session_s >= self._backoff_reset_s:
            self._log.debug(
                "sse backoff exponent reset after session_s=%.1f", self._last_session_s
            )
            self._backoff_n = 0
        delay = self._delay_for(mode)
        self._last_backoff_s = delay
        self._log.debug(
            "sse backoff mode=%s delay_s=%.2f next_n=%d", mode, delay, self._backoff_n
        )
        self._sleep(delay)

    def _sleep(self, seconds: float) -> None:
        """Interruptible sleep: the stop event always wins (SPEC §6.4)."""
        if seconds > 0:
            self._stop_ev.wait(seconds)

    # ---------------------------------------------------------------------- status ----

    def _seed_connection_ordinal(self) -> int:
        """Continue ``state["connections"]`` across restarts for the ``batch`` field.

        SPEC §2 key 12 wants ``"c<connection_ordinal>"`` and SPEC §10 keeps the ordinal in
        ``state.json``, so it is read back out of ``writer.snapshot()`` here and advanced with
        ``writer.bump("connections")`` on every accepted connect. If the snapshot does not
        carry it, counting from zero is harmless: ``batch`` is provenance, ``seq`` is the
        ordering key.
        """
        try:
            snap = self._writer.snapshot()
        except Exception:
            return 0
        if not isinstance(snap, dict):
            return 0
        for holder in (snap, snap.get("sse"), snap.get("storage"), snap.get("counters")):
            if isinstance(holder, dict):
                value = holder.get("connections")
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    return value
        return 0

    def _publish(self, force: bool = False) -> None:
        """Merge SSE facts into ``status.json`` (SPEC §11.3). Counters only, never content."""
        now = time.monotonic()
        if not force and (now - self._last_publish_mono) < self._status_min_interval_s:
            return
        self._last_publish_mono = now
        fields = {
            "state": self._state,
            "connections": self._connections,
            "s_since_last_inbound": round(now - self._last_inbound_mono, 1),
            "s_since_last_event": round(now - self._last_event_mono, 1),
            "heartbeats": self._heartbeats,
            "comments": self._comments,
            "reconnects_last_hour": len(self._recent_connects),
            "last_close_mode": self._last_close_mode,
            "last_close_detail": self._last_close_detail,
            "events": self._events,
            "rows_written": self._rows_written,
            "dupes_dropped": self._dupes,
            "parse_errors": self._parse_errors,
            "unknown_lines": self._unknown_lines,
            "utf8_replacements": self._utf8_replacements,
            "stalls": self._stalls,
            "gaps_emitted": self._gaps_emitted,
            "backoff_n": self._backoff_n,
            "last_backoff_s": round(self._last_backoff_s, 2),
            "bytes_in": self._bytes_in,
            "max_open_conns": self._max_open_conns,
            "uptime_s": round(now - self._start_mono, 1),
        }
        try:
            self._status.update("sse", **fields)
        except Exception as exc:
            # A status failure must never take the ingest path down with it.
            self._log.debug("sse status update failed err=%s", _exc_summary(exc))
