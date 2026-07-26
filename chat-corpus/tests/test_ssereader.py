"""Tests for ``ssereader.py`` — SPEC §6 (connection, frames, watchdog, close modes, gaps).

Everything runs against a real loopback HTTP server that speaks byte-exact SSE, so the frame
parser, the read primitive and ``http.client``'s chunked decoder are all exercised for real.
Only two things are faked: the writer/status collaborators (they own disk, which is not this
module's contract) and the connection factory, which is pointed at plain HTTP because a test
must not carry a TLS certificate.

Timing is compressed by overriding the SPEC §9 config knobs and by capturing the reader's
*computed* backoff instead of waiting it out: the assertions below check the real SPEC §6.4
delay arithmetic (0.5–2.0 s for CLEAN_EOF, 60 s+jitter for a 429) without the wall clock cost.

No test contains real chat text or coordinates (SPEC §11.4).
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import record  # noqa: E402
import ssereader  # noqa: E402
import tsutil  # noqa: E402

# --------------------------------------------------------------------------- #
# synthetic payloads — obviously fake names, no coordinates anywhere
# --------------------------------------------------------------------------- #

CANARY_CHAT = "melon-canary-chat-zzz"
CANARY_COMPONENT = '{"text":"","extra":[{"text":"melon-canary-chat-zzz"}]}'

EVENT = {
    "time": "2026-07-25T20:37:13.404045123Z",
    "chat": CANARY_CHAT,
    "playerName": "SyntheticPlayerOne",
    "playerUuid": "069A79F4-44E9-4726-A5BE-FCA90E38AAF5",
    "component": CANARY_COMPONENT,
}

EVENT2 = {
    "time": "2026-07-25T20:37:19.900000000Z",
    "chat": "gg",
    "playerName": "SyntheticPlayerTwo",
    "playerUuid": None,
    "component": None,
}


def _json(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _frame(obj: dict) -> bytes:
    """The exact wire format: ``data:{json}\\n\\n``, no space, LF only (SPEC §6.2)."""
    return b"data:" + _json(obj) + b"\n\n"


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class _FakeWriter:
    """Just enough of ``writer.Writer`` for SPEC §6: seq, records, gaps, counters."""

    def __init__(self, hwm_us: int) -> None:
        self._lock = threading.Lock()
        self._hwm_us = hwm_us
        self._seq = 0
        self.records = []
        self.gaps = []
        self.counters = {}
        self.seen = set()
        self.probe = None  # callable: extra fact captured at enqueue_gap time

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def put_record(self, rec: dict) -> bool:
        with self._lock:
            self.records.append(rec)
            rid = rec["row_id"]
            if rid in self.seen:
                return False
            self.seen.add(rid)
            if rec["ts_us"] > self._hwm_us:
                self._hwm_us = rec["ts_us"]
            return True

    def enqueue_gap(self, start_us, end_us, *, pass_no=1, not_before_us=None, tag=None):
        with self._lock:
            if end_us <= start_us:
                return None  # SPEC §6.5: a zero-length window is a 400, not an empty page
            gap_id = "g%06d" % (len(self.gaps) + 1,)
            self.gaps.append(
                {
                    "gap_id": gap_id,
                    "start_us": start_us,
                    "end_us": end_us,
                    "tag": tag,
                    "pass": pass_no,
                    "probe": self.probe() if self.probe else None,
                }
            )
            return gap_id

    def hwm_us(self) -> int:
        with self._lock:
            return self._hwm_us

    def bump(self, counter: str, n: int = 1) -> None:
        with self._lock:
            self.counters[counter] = self.counters.get(counter, 0) + n

    def snapshot(self) -> dict:
        with self._lock:
            return {"counters": dict(self.counters)}


class _FakeStatus:
    """Thread-safe section merge, like ``status.StatusPublisher.update``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.sections = {}

    def update(self, section: str, **fields) -> None:
        with self._lock:
            self.sections.setdefault(section, {}).update(fields)

    def snapshot(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self.sections.items()}


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        logging.Handler.__init__(self, level=logging.DEBUG)
        self._lock_ = threading.Lock()
        self.rows = []

    def emit(self, rec: logging.LogRecord) -> None:
        try:
            msg = rec.getMessage()
        except Exception as exc:  # pragma: no cover
            msg = "UNFORMATTABLE %r" % (exc,)
        with self._lock_:
            self.rows.append((rec.levelno, msg))

    def messages(self, min_level: int = 0):
        with self._lock_:
            return [m for lvl, m in self.rows if lvl >= min_level]

    def text(self) -> str:
        return "\n".join(self.messages())


class _ConnTracker:
    """Connection factory that proves the single-connection invariant (SPEC §1, §12.12)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.live = 0
        self.max_live = 0
        self.total = 0
        self.violations = 0

    def __call__(self, host: str, timeout: float):
        with self.lock:
            if self.live != 0:
                # A leaked socket manufactures its own 429s. Record, never raise: raising
                # here would be misread by the reader as a connect error.
                self.violations += 1
            self.live += 1
            self.total += 1
            if self.live > self.max_live:
                self.max_live = self.live
        conn = http.client.HTTPConnection(host, timeout=timeout)
        closed = []
        real_close = conn.close

        def close():
            with self.lock:
                if not closed:
                    closed.append(True)
                    self.live -= 1
            real_close()

        conn.close = close
        return conn


# --------------------------------------------------------------------------- #
# loopback SSE server
# --------------------------------------------------------------------------- #


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the test output clean
        pass

    def setup(self):
        BaseHTTPRequestHandler.setup(self)
        srv = self.server
        with srv.lock:
            srv.sockets_live += 1
            if srv.sockets_live > srv.max_sockets_live:
                srv.max_sockets_live = srv.sockets_live

    def finish(self):
        try:
            BaseHTTPRequestHandler.finish(self)
        except Exception:
            pass
        finally:
            with self.server.lock:
                self.server.sockets_live -= 1

    def do_GET(self):
        srv = self.server
        with srv.lock:
            idx = srv.conn_count
            srv.conn_count += 1
            srv.requests.append(
                {
                    "path": self.path,
                    "headers": dict((k.lower(), v) for k, v in self.headers.items()),
                }
            )
        self.close_connection = True
        plan = srv.plan
        fn = plan[idx] if idx < len(plan) else plan[-1]
        try:
            fn(self)
        except Exception:
            pass


class _StreamServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):  # abrupt closes are the point
        pass


class _Server:
    def __init__(self, plan) -> None:
        self.httpd = _StreamServer(("127.0.0.1", 0), _Handler)
        self.httpd.lock = threading.Lock()
        # one entry per connection ordinal; the last entry repeats forever
        self.httpd.plan = [plan] if callable(plan) else list(plan)
        self.httpd.conn_count = 0
        self.httpd.requests = []
        self.httpd.sockets_live = 0
        self.httpd.max_sockets_live = 0
        self.httpd.quit = threading.Event()
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02})
        self.thread.daemon = True
        self.thread.start()

    @property
    def host(self) -> str:
        addr = self.httpd.socket.getsockname()
        return "%s:%d" % (addr[0], addr[1])

    @property
    def conn_count(self) -> int:
        with self.httpd.lock:
            return self.httpd.conn_count

    @property
    def requests(self):
        with self.httpd.lock:
            return list(self.httpd.requests)

    def close(self) -> None:
        self.httpd.quit.set()
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
        self.thread.join(3.0)


# ---- plan primitives -------------------------------------------------------


def _open_stream(h, chunked: bool = True, ctype: str = "text/event-stream") -> None:
    head = ["HTTP/1.1 200 OK", "Content-Type: " + ctype, "Cache-Control: no-cache"]
    if chunked:
        head.append("Transfer-Encoding: chunked")
    head.append("Connection: close")
    h.wfile.write(("\r\n".join(head) + "\r\n\r\n").encode("ascii"))
    h.wfile.flush()


def _send(h, payload: bytes, chunked: bool = True) -> None:
    if chunked:
        h.wfile.write(("%x\r\n" % (len(payload),)).encode("ascii"))
        h.wfile.write(payload)
        h.wfile.write(b"\r\n")
    else:
        h.wfile.write(payload)
    h.wfile.flush()


def _end_clean(h, chunked: bool = True) -> None:
    """Orderly completion — what the 15-minute MVC timeout looks like (SPEC §6.4)."""
    if chunked:
        h.wfile.write(b"0\r\n\r\n")
        h.wfile.flush()


def _abrupt(h) -> None:
    """Truncated close: no terminating chunk. http.client raises IncompleteRead."""
    try:
        h.connection.close()
    except Exception:
        pass


def _hold(h, seconds: float) -> None:
    h.server.quit.wait(seconds)


def _plan_frames(payloads, chunked: bool = True, then: str = "hold"):
    def fn(h):
        _open_stream(h, chunked=chunked)
        for payload in payloads:
            _send(h, payload, chunked=chunked)
        if then == "clean":
            _end_clean(h, chunked=chunked)
        elif then == "abrupt":
            _abrupt(h)
        else:
            _hold(h, 30.0)

    return fn


def _plan_status(status_line: str, headers, body: bytes):
    def fn(h):
        head = [status_line] + list(headers) + ["Content-Length: %d" % (len(body),), "Connection: close"]
        h.wfile.write(("\r\n".join(head) + "\r\n\r\n").encode("ascii"))
        h.wfile.write(body)
        h.wfile.flush()

    return fn


def _plan_silent(seconds: float = 30.0):
    """Headers, then nothing at all — the watchdog's problem (SPEC §6.3)."""

    def fn(h):
        _open_stream(h)
        _hold(h, seconds)

    return fn


# ---- harness ---------------------------------------------------------------

_HWM_US = 1785012433404045  # a fixed, synthetic high-water mark


class _Harness:
    def __init__(self, plan, **sse_over) -> None:
        self.server = _Server(plan)
        self.writer = _FakeWriter(_HWM_US)
        self.writer.probe = lambda: self.server.conn_count
        self.status = _FakeStatus()
        self.logcap = _LogCapture()
        self.log = logging.getLogger("melon.test.ssereader.%d" % (id(self),))
        self.log.handlers = [self.logcap]
        self.log.setLevel(logging.DEBUG)
        self.log.propagate = False
        self.stop = threading.Event()
        self.tracker = _ConnTracker()

        sse = {
            "enabled": True,
            "host": self.server.host,
            "path": "/feed/chats",
            "socket_read_timeout_s": 0.2,
            "heartbeat_deadline_s": 90,
            "initial_grace_s": 120,
            "backoff_reset_s": 60,
            "backoff_broken_max_s": 60,
            "backoff_429_max_s": 300,
        }
        sse.update(sse_over)
        self.cfg = {
            "user_agent": "melon-chat-corpus-test/1.0 (+contact: test)",
            "sse": sse,
            "backfill": {"safety_lag_s": 120},
        }

        self.reader = ssereader.SseReader(
            self.cfg, self.writer, self.status, self.log, self.stop
        )
        self.reader._conn_factory = self.tracker
        self.reader._min_gap_spacing_us = 0  # fast test reconnects still get their gap
        self.reader._status_min_interval_s = 0.0
        self.sleeps = []
        real_sleep = self.reader._sleep

        def sleep_spy(seconds):
            self.sleeps.append(seconds)
            real_sleep(min(seconds, 0.02))  # keep the SPEC arithmetic, drop the wall clock

        self.reader._sleep = sleep_spy

    def start(self) -> None:
        self.reader.start()

    def finish(self) -> None:
        self.stop.set()
        self.server.httpd.quit.set()
        self.reader.join(5.0)
        self.server.close()

    # -- waiting helpers ----------------------------------------------------
    def wait(self, pred, timeout: float = 6.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            if not self.reader.is_alive() and pred():
                return True
            time.sleep(0.005)
        return pred()

    def wait_records(self, n: int, timeout: float = 6.0) -> bool:
        return self.wait(lambda: len(self.writer.records) >= n, timeout)


class SseReaderTestCase(unittest.TestCase):
    """Shared setup: every case builds one harness and always tears it down."""

    def make(self, plan, **sse_over) -> _Harness:
        h = _Harness(plan, **sse_over)
        self.addCleanup(h.finish)
        self.addCleanup(self._assert_no_leak, h)
        h.start()
        return h

    def _assert_no_leak(self, h: _Harness) -> None:
        h.stop.set()
        h.server.httpd.quit.set()
        h.reader.join(5.0)
        self.assertEqual(
            0, h.tracker.violations, "opened a connection while another was still live"
        )
        self.assertEqual(1, max(1, h.tracker.max_live), "more than one live client socket")
        self.assertLessEqual(h.tracker.max_live, 1)
        self.assertEqual(0, h.tracker.live, "a socket was left open at shutdown")
        self.assertLessEqual(h.reader._max_open_conns, 1)


# --------------------------------------------------------------------------- #
# frame parsing — SPEC §6.2
# --------------------------------------------------------------------------- #


class TestFrameParsing(SseReaderTestCase):
    def test_exact_wire_format_no_space_after_colon(self):
        """``data:{json}\\n\\n`` is the real format and must parse (SPEC §6.2)."""
        h = self.make(_plan_frames([_frame(EVENT)]))
        self.assertTrue(h.wait_records(1), "no record written")
        rec = h.writer.records[0]
        self.assertEqual(CANARY_CHAT, rec["chat"])
        self.assertEqual("069a79f4-44e9-4726-a5be-fca90e38aaf5", rec["player_uuid"])
        self.assertEqual("SyntheticPlayerOne", rec["player_name"])
        self.assertEqual(CANARY_COMPONENT, rec["component"])
        self.assertEqual(record.SRC_SSE, rec["src"])
        self.assertEqual("us", rec["precision"])
        self.assertEqual("c1", rec["batch"])
        self.assertEqual(tsutil.parse_ts(EVENT["time"]), rec["ts_us"])
        self.assertEqual(1, rec["seq"])
        self.assertEqual(0, h.writer.counters.get("parse_errors", 0))
        # SPEC §2 key 12 / §10: the ordinal behind `batch` is durable state.
        self.assertEqual(1, h.writer.counters.get("connections", 0))

    def test_connection_ordinal_is_seeded_from_the_writer(self):
        """``batch`` continues ``state["connections"]`` across restarts (SPEC §2, §10)."""
        h = self.make(_plan_frames([_frame(EVENT)]))
        self.assertTrue(h.wait_records(1))
        h.stop.set()
        h.reader.join(5.0)
        seeded = _FakeWriter(_HWM_US)
        seeded.snapshot = lambda: {"connections": 416, "counters": {}}
        reader = ssereader.SseReader(h.cfg, seeded, _FakeStatus(), h.log, threading.Event())
        self.assertEqual(416, reader._connections)

    def test_data_line_with_one_optional_space(self):
        """The SSE spec allows exactly one space after the colon (SPEC §6.2)."""
        payload = b"data: " + _json(EVENT) + b"\n\n"
        h = self.make(_plan_frames([payload]))
        self.assertTrue(h.wait_records(1))
        self.assertEqual(CANARY_CHAT, h.writer.records[0]["chat"])
        self.assertEqual(0, h.writer.counters.get("parse_errors", 0))

    def test_crlf_line_endings_tolerated(self):
        """The feed sends LF only; CRLF must still parse (SPEC §6.2)."""
        payload = b"data:" + _json(EVENT) + b"\r\n\r\n"
        h = self.make(_plan_frames([payload]))
        self.assertTrue(h.wait_records(1))
        self.assertEqual(CANARY_CHAT, h.writer.records[0]["chat"])
        self.assertEqual(0, h.writer.counters.get("parse_errors", 0))

    def test_multi_line_data_field_is_joined_with_lf(self):
        """Multi-line data fields join with '\\n' per the SSE spec (SPEC §6.2)."""
        blob = _json(EVENT2)
        cut = blob.index(b',"playerName"')
        payload = b"data:" + blob[:cut] + b"\ndata:" + blob[cut:] + b"\n\n"
        h = self.make(_plan_frames([payload]))
        self.assertTrue(h.wait_records(1))
        rec = h.writer.records[0]
        self.assertEqual("gg", rec["chat"])
        self.assertIsNone(rec["player_uuid"])
        self.assertIsNone(rec["component"])
        self.assertEqual(0, h.writer.counters.get("parse_errors", 0))

    def test_frame_split_across_tcp_chunks(self):
        """One frame delivered in three writes still parses (SPEC §6.2 buffering)."""
        blob = _frame(EVENT)
        parts = [blob[:9], blob[9:40], blob[40:]]

        def fn(handler):
            _open_stream(handler)
            for part in parts:
                _send(handler, part)
                time.sleep(0.01)
            _hold(handler, 30.0)

        h = self.make(fn)
        self.assertTrue(h.wait_records(1))
        self.assertEqual(CANARY_CHAT, h.writer.records[0]["chat"])

    def test_heartbeat_comment_is_liveness_only(self):
        """``:heartbeat`` counts for liveness and is never JSON-parsed (SPEC §6.2, §6.3)."""
        payloads = [b":heartbeat\n\n", b":heartbeat\n\n", _frame(EVENT), b":heartbeat\n\n"]
        h = self.make(_plan_frames(payloads))
        self.assertTrue(h.wait_records(1))
        self.assertTrue(h.wait(lambda: h.reader._heartbeats >= 3))
        self.assertEqual(3, h.reader._heartbeats)
        self.assertEqual(3, h.reader._comments)
        self.assertEqual(1, len(h.writer.records))
        self.assertEqual(0, h.writer.counters.get("parse_errors", 0))
        self.assertEqual(0, h.reader._unknown_lines)
        self.assertTrue(h.wait(lambda: h.status.snapshot()["sse"].get("heartbeats") == 3))

    def test_named_event_and_id_fields_ignored_once_logged(self):
        """No named-event listener exists; unknown fields are counted, not obeyed (SPEC §6.2)."""
        payload = b"event:chat\ndata:" + _json(EVENT) + b"\nid:99\n\nnonsense-line\n\n"
        h = self.make(_plan_frames([payload]))
        self.assertTrue(h.wait_records(1))
        self.assertTrue(h.wait(lambda: h.reader._unknown_lines == 1))
        self.assertEqual(CANARY_CHAT, h.writer.records[0]["chat"])
        text = h.logcap.text()
        self.assertEqual(1, text.count("server started sending 'event'"))
        self.assertEqual(1, text.count("server started sending 'id'"))
        self.assertNotIn("nonsense-line", text)

    def test_unparseable_json_counts_and_does_not_kill_the_loop(self):
        """A bad payload is a counter, not a crash (SPEC §6.2, §3.1)."""
        bad = b"data:{this is not json: " + CANARY_CHAT.encode() + b"}\n\n"
        h = self.make(_plan_frames([bad, _frame(EVENT)]))
        self.assertTrue(h.wait_records(1))
        self.assertTrue(h.wait(lambda: h.writer.counters.get("parse_errors", 0) == 1))
        self.assertEqual(1, len(h.writer.records))
        self.assertEqual(CANARY_CHAT, h.writer.records[0]["chat"])
        self.assertTrue(h.reader.is_alive())
        self.assertNotIn(CANARY_CHAT, h.logcap.text())

    def test_missing_field_payload_counts_as_parse_error(self):
        """A renamed/absent SSE field must be counted, never guessed (SPEC §2.1)."""
        broken = {"time": EVENT["time"], "chat": "gg", "playerName": "SyntheticPlayerOne"}
        h = self.make(_plan_frames([_frame(broken), _frame(EVENT)]))
        self.assertTrue(h.wait_records(1))
        self.assertTrue(h.wait(lambda: h.writer.counters.get("parse_errors", 0) == 1))
        self.assertEqual(1, len(h.writer.records))


# --------------------------------------------------------------------------- #
# request shape — SPEC §6.1
# --------------------------------------------------------------------------- #


class TestRequest(SseReaderTestCase):
    def test_headers_and_path(self):
        """Identifying UA, identity encoding, no-cache, one GET on the configured path."""
        h = self.make(_plan_frames([_frame(EVENT)]))
        self.assertTrue(h.wait_records(1))
        req = h.server.requests[0]
        self.assertEqual("/feed/chats", req["path"])
        self.assertEqual("text/event-stream", req["headers"]["accept"])
        self.assertEqual("identity", req["headers"]["accept-encoding"])
        self.assertEqual("no-cache", req["headers"]["cache-control"])
        self.assertEqual("close", req["headers"]["connection"])
        self.assertEqual(
            "melon-chat-corpus-test/1.0 (+contact: test)", req["headers"]["user-agent"]
        )

    def test_disabled_does_nothing(self):
        """``sse.enabled=false`` opens no socket and queues no gap (SPEC §9)."""
        h = self.make(_plan_frames([_frame(EVENT)]), enabled=False)
        h.reader.join(3.0)
        self.assertFalse(h.reader.is_alive())
        self.assertEqual(0, h.tracker.total)
        self.assertEqual([], h.writer.gaps)
        self.assertEqual("disabled", h.status.snapshot()["sse"]["state"])

    def test_user_agent_never_empty(self):
        """``http.client`` sends no UA of its own and Cloudflare 403s a missing one."""
        self.assertEqual("chat-corpus-collector/1.0", ssereader._sanitize_user_agent(""))
        self.assertEqual("chat-corpus-collector/1.0", ssereader._sanitize_user_agent(None))
        self.assertEqual("a b", ssereader._sanitize_user_agent(" a\r\nb "))


# --------------------------------------------------------------------------- #
# close classification and backoff — SPEC §6.4
# --------------------------------------------------------------------------- #


class TestCloseModes(SseReaderTestCase):
    def test_clean_eof_midstream_reconnects_quickly_and_quietly(self):
        """The 15-minute cap: CLEAN_EOF, DEBUG only, 0.5–2.0 s sleep (SPEC §6.4)."""
        plan = [
            _plan_frames([_frame(EVENT)], then="clean"),
            _plan_frames([_frame(EVENT2)]),
        ]
        h = self.make(plan)
        self.assertTrue(h.wait_records(2), "did not reconnect after a clean EOF")
        self.assertEqual("c1", h.writer.records[0]["batch"])
        self.assertEqual("c2", h.writer.records[1]["batch"])
        self.assertEqual(ssereader.CLEAN_EOF, h.reader._last_close_mode)
        self.assertEqual("eof", h.reader._last_close_detail)
        self.assertEqual(0, h.reader._backoff_n, "a clean EOF must not grow the exponent")
        self.assertTrue(h.sleeps, "no backoff was computed")
        self.assertGreaterEqual(h.sleeps[0], 0.5)
        self.assertLessEqual(h.sleeps[0], 2.0)
        self.assertEqual([], h.logcap.messages(logging.ERROR))
        self.assertIn("sse clean eof", h.logcap.text())
        for msg in h.logcap.messages(logging.WARNING):
            self.assertNotIn("CLEAN_EOF", msg)

    def test_stop_is_not_a_close_mode(self):
        """A shutdown must not overwrite the last real close mode in status (SPEC §11.3)."""
        plan = [
            _plan_frames([_frame(EVENT)], then="clean"),
            _plan_frames([_frame(EVENT2)]),
        ]
        h = self.make(plan)
        self.assertTrue(h.wait_records(2))
        h.stop.set()
        h.reader.join(5.0)
        sse = h.status.snapshot()["sse"]
        self.assertEqual("stopped", sse["state"])
        self.assertEqual(ssereader.CLEAN_EOF, sse["last_close_mode"])

    def test_identity_framed_stream_then_eof(self):
        """A close-delimited (non-chunked) response is still a clean EOF (SPEC §6.4)."""
        plan = [
            _plan_frames([_frame(EVENT)], chunked=False, then="clean"),
            _plan_frames([_frame(EVENT2)]),
        ]
        h = self.make(plan)
        self.assertTrue(h.wait_records(2))
        self.assertEqual(ssereader.CLEAN_EOF, h.reader._last_close_mode)

    def test_truncated_close_is_broken_with_exponential_backoff(self):
        """A torn chunked stream is BROKEN: min(60, 2**n) + jitter (SPEC §6.4)."""
        plan = [
            _plan_frames([_frame(EVENT)], then="abrupt"),
            _plan_frames([_frame(EVENT2)]),
        ]
        h = self.make(plan)
        self.assertTrue(h.wait_records(2))
        self.assertTrue(h.wait(lambda: bool(h.sleeps)))
        self.assertGreaterEqual(h.sleeps[0], 1.0)
        self.assertLessEqual(h.sleeps[0], 1.5)
        self.assertGreaterEqual(h.reader._backoff_n, 1)

    def test_stall_trips_the_watchdog_after_the_deadline(self):
        """Silence past the deadline is a stall, judged on inbound frames (SPEC §6.3)."""
        plan = [_plan_silent(30.0), _plan_frames([_frame(EVENT)])]
        h = self.make(
            plan,
            socket_read_timeout_s=0.1,
            initial_grace_s=0.05,
            heartbeat_deadline_s=0.4,
            backoff_broken_max_s=60,
        )
        self.assertTrue(h.wait(lambda: h.reader._stalls >= 1, timeout=6.0), "no stall")
        self.assertTrue(h.wait_records(1), "did not recover after the stall")
        self.assertEqual("stall", h.reader._last_close_detail)
        self.assertEqual(ssereader.BROKEN, h.reader._last_close_mode)
        self.assertTrue(any("watchdog stall" in m for m in h.logcap.messages(logging.WARNING)))

    def test_idle_period_does_not_drop_the_connection(self):
        """Regression: a read timeout is a tick, so idling must not cost a reconnect.

        SPEC §6.1 says every ``socket.timeout`` on read is normal. It only *is* normal if the
        timeout consumes nothing: ``socket.SocketIO`` latches ``_timeout_occurred`` and
        ``http.client``'s chunk decoder re-tosses a chunk terminator after an exception, and
        either one turns a quiet feed into a reconnect every ``socket_read_timeout_s``.
        """

        def fn(handler):
            _open_stream(handler)
            _send(handler, _frame(EVENT))
            handler.server.quit.wait(0.4)  # ~8 read timeouts, well under the deadline
            _send(handler, _frame(EVENT2))
            _hold(handler, 30.0)

        h = self.make(
            fn, socket_read_timeout_s=0.05, initial_grace_s=0.02, heartbeat_deadline_s=5.0
        )
        self.assertTrue(h.wait_records(2), "an idle stream was dropped")
        self.assertEqual(1, h.server.conn_count, "reconnected while merely idle")
        self.assertEqual(0, h.reader._stalls)
        self.assertEqual(1, len(h.writer.gaps), "an idle stream must not queue gaps")
        self.assertEqual("c1", h.writer.records[1]["batch"])

    def test_heartbeats_alone_keep_the_stream_alive(self):
        """Comments are liveness: no data for longer than the deadline is fine (SPEC §6.3)."""

        def fn(handler):
            _open_stream(handler)
            for _ in range(6):
                _send(handler, b":heartbeat\n\n")
                handler.server.quit.wait(0.05)
            _send(handler, _frame(EVENT))
            _hold(handler, 30.0)

        h = self.make(
            fn, socket_read_timeout_s=0.05, initial_grace_s=0.02, heartbeat_deadline_s=0.4
        )
        self.assertTrue(h.wait_records(1))
        self.assertEqual(0, h.reader._stalls, "heartbeats were not counted as liveness")
        self.assertEqual(1, h.server.conn_count, "the stream should not have reconnected")

    def test_429_at_connect_is_rate_limited_with_the_long_backoff(self):
        """429 -> min(300, 60*2**n) + jitter, and the body is never parsed (SPEC §6.4, §12.5)."""
        plan = [
            _plan_status(
                "HTTP/1.1 429 Too Many Requests",
                ["Content-Type: text/html", "Server: cloudflare"],
                b"<html>429</html>",
            ),
            _plan_frames([_frame(EVENT)]),
        ]
        h = self.make(plan)
        self.assertTrue(h.wait(lambda: bool(h.sleeps)))
        self.assertEqual(ssereader.RATE_LIMITED, h.reader._last_close_mode)
        self.assertEqual("status_429", h.reader._last_close_detail)
        self.assertGreaterEqual(h.sleeps[0], 60.0)
        self.assertLessEqual(h.sleeps[0], 90.0)
        self.assertTrue(h.wait_records(1), "did not retry after the 429")
        self.assertEqual("c1", h.writer.records[0]["batch"], "a 429 is not a connection")
        self.assertEqual(0, h.writer.counters.get("parse_errors", 0))
        self.assertTrue(any("text/html" in m for m in h.logcap.messages(logging.WARNING)))

    def test_429_backoff_exceeds_broken_backoff(self):
        """The three policies are genuinely different (SPEC §6.4)."""
        h = self.make(_plan_frames([_frame(EVENT)]))
        h.stop.set()
        h.reader.join(5.0)
        reader = h.reader

        def draw(mode, n=50):
            out = []
            for _ in range(n):
                reader._backoff_n = 0
                out.append(reader._delay_for(mode))
            return out

        cleans = draw(ssereader.CLEAN_EOF)
        self.assertEqual(0, reader._backoff_n, "CLEAN_EOF must not touch the exponent")
        self.assertGreaterEqual(min(cleans), 0.5)
        self.assertLessEqual(max(cleans), 2.0)
        brokens = draw(ssereader.BROKEN)
        self.assertGreaterEqual(min(brokens), 1.0)
        self.assertLessEqual(max(brokens), 1.5)
        limited = draw(ssereader.RATE_LIMITED)
        self.assertGreaterEqual(min(limited), 60.0)
        self.assertLessEqual(max(limited), 90.0)
        self.assertGreater(min(limited), max(brokens), "429 must back off hardest")
        # exponential growth, capped
        reader._backoff_n = 0
        seen = [reader._delay_for(ssereader.BROKEN) for _ in range(12)]
        self.assertLessEqual(max(seen), 60.0 * 1.5)
        self.assertGreater(seen[-1], seen[0])

    def test_non_200_status_is_broken(self):
        """5xx/302 at connect are BROKEN, and no redirect is ever followed (SPEC §6.4, §7.5)."""
        plan = [
            _plan_status("HTTP/1.1 502 Bad Gateway", ["Content-Type: text/html"], b"nope"),
            _plan_status(
                "HTTP/1.1 302 Found",
                ["Content-Type: text/html", "Location: https://api.2b2t.vc/scalar"],
                b"",
            ),
            _plan_frames([_frame(EVENT)]),
        ]
        h = self.make(plan)
        self.assertTrue(h.wait_records(1))
        self.assertTrue(h.wait(lambda: h.server.conn_count >= 3))
        details = [m for m in h.logcap.messages(logging.WARNING) if "unexpected status" in m]
        self.assertTrue(any("status=502" in m for m in details))
        self.assertTrue(any("status=302" in m for m in details))
        self.assertNotIn("scalar", h.logcap.text())

    def test_backoff_exponent_resets_after_a_healthy_session(self):
        """SPEC §6.4: reset n after ``backoff_reset_s`` of healthy streaming."""
        h = self.make(_plan_frames([_frame(EVENT)]))
        h.stop.set()
        h.reader.join(5.0)
        reader = h.reader
        reader._backoff_n = 4
        reader._last_session_s = 0.5
        reader._backoff(ssereader.BROKEN)
        self.assertEqual(5, reader._backoff_n, "a short session must not reset the exponent")
        reader._backoff_n = 4
        reader._last_session_s = 900.0
        reader._backoff(ssereader.BROKEN)
        self.assertEqual(1, reader._backoff_n)
        self.assertLessEqual(reader._last_backoff_s, 1.5)


# --------------------------------------------------------------------------- #
# the single-connection invariant — SPEC §1, §12.12
# --------------------------------------------------------------------------- #


class TestSingleConnection(SseReaderTestCase):
    def test_exactly_one_connection_across_several_reconnects(self):
        """Old socket closed before the new one is opened, every time (SPEC §1)."""
        plan = [
            _plan_frames([_frame(EVENT)], then="clean"),
            _plan_frames([_frame(EVENT2)], then="clean"),
            _plan_frames([_frame(EVENT)], then="abrupt"),
            _plan_frames([_frame(EVENT2)], then="clean"),
            _plan_frames([_frame(EVENT)]),
        ]
        h = self.make(plan)
        self.assertTrue(h.wait(lambda: h.server.conn_count >= 5, timeout=8.0))
        self.assertTrue(h.wait(lambda: len(h.writer.records) >= 5, timeout=8.0))
        self.assertEqual(1, h.tracker.max_live)
        self.assertEqual(0, h.tracker.violations)
        self.assertEqual(1, h.reader._max_open_conns)
        self.assertGreaterEqual(h.tracker.total, 5)

    def test_assert_no_conn_refuses_a_second_socket(self):
        """The invariant is checked, not assumed — and not via ``assert`` (SPEC §12.12)."""
        h = self.make(_plan_frames([_frame(EVENT)]))
        self.assertTrue(h.wait_records(1))
        h.stop.set()
        h.reader.join(5.0)
        h.reader._open_conns = 1
        with self.assertRaises(RuntimeError):
            h.reader._assert_no_conn()
        h.reader._open_conns = 0
        h.reader._assert_no_conn()

    def test_reader_thread_never_dies_silently(self):
        """A fatal internal error flips the state, it does not vanish (SPEC §11.3)."""
        boom = _FakeWriter(_HWM_US)

        def explode(*_a, **_k):
            raise RuntimeError("synthetic writer failure")

        boom.hwm_us = explode
        server = _Server([_plan_frames([_frame(EVENT)])])
        self.addCleanup(server.close)
        status = _FakeStatus()
        logcap = _LogCapture()
        log = logging.getLogger("melon.test.ssereader.boom")
        log.handlers = [logcap]
        log.setLevel(logging.DEBUG)
        log.propagate = False
        stop = threading.Event()
        cfg = {
            "user_agent": "melon-chat-corpus-test/1.0",
            "sse": {"enabled": True, "host": server.host, "path": "/feed/chats",
                    "socket_read_timeout_s": 0.1},
            "backfill": {"safety_lag_s": 120},
        }
        reader = ssereader.SseReader(cfg, boom, status, log, stop)
        reader._conn_factory = _ConnTracker()
        reader.start()
        reader.join(5.0)
        self.addCleanup(stop.set)
        self.assertFalse(reader.is_alive())
        self.assertEqual("error", status.snapshot()["sse"]["state"])
        self.assertTrue(any("thread died" in m for m in logcap.messages(logging.ERROR)))


# --------------------------------------------------------------------------- #
# gap emission — SPEC §6.5
# --------------------------------------------------------------------------- #


class TestGaps(SseReaderTestCase):
    def test_startup_gap_is_persisted_before_the_first_connect(self):
        """Process start is a hole, queued before any socket exists (SPEC §6.5, §11.2)."""
        h = self.make(_plan_frames([_frame(EVENT)]))
        self.assertTrue(h.wait(lambda: len(h.writer.gaps) >= 1))
        gap = h.writer.gaps[0]
        self.assertEqual(0, gap["probe"], "the gap was queued after the connect")
        self.assertEqual(_HWM_US - 120000000, gap["start_us"])
        self.assertGreater(gap["end_us"], gap["start_us"])
        self.assertEqual("sse:start", gap["tag"])
        now_us = tsutil.now_us()
        self.assertGreater(gap["end_us"], now_us - 30000000)
        self.assertLess(gap["end_us"], now_us + 30000000)

    def test_reconnect_gap_is_persisted_before_the_reconnect(self):
        """Every connected->disconnected transition queues a gap first (SPEC §6.5)."""
        plan = [
            _plan_frames([_frame(EVENT)], then="clean"),
            _plan_frames([_frame(EVENT2)]),
        ]
        h = self.make(plan)
        self.assertTrue(h.wait_records(2))
        self.assertTrue(h.wait(lambda: len(h.writer.gaps) >= 2))
        first, second = h.writer.gaps[0], h.writer.gaps[1]
        self.assertEqual(0, first["probe"])
        self.assertEqual(1, second["probe"], "gap #2 was not queued before connect #2")
        self.assertEqual("sse:start", first["tag"])
        self.assertEqual("sse:" + ssereader.CLEAN_EOF, second["tag"])
        # start is anchored on the writer's hwm, which event #1 advanced.
        self.assertEqual(h.writer.hwm_us() - 120000000, second["start_us"])
        self.assertGreater(second["end_us"], first["end_us"])

    def test_failed_connect_also_queues_a_gap(self):
        """Wall clock spent rejected is still uncovered wall clock (SPEC §6.5)."""
        plan = [
            _plan_status("HTTP/1.1 429 Too Many Requests", ["Content-Type: text/html"], b"x"),
            _plan_frames([_frame(EVENT)]),
        ]
        h = self.make(plan)
        self.assertTrue(h.wait_records(1))
        self.assertTrue(h.wait(lambda: len(h.writer.gaps) >= 2))
        self.assertEqual("sse:" + ssereader.RATE_LIMITED, h.writer.gaps[1]["tag"])

    def test_zero_length_gap_is_skipped(self):
        """``startDate == endDate`` is a 400, so the writer's None is respected (SPEC §6.5)."""
        h = self.make(_plan_frames([_frame(EVENT)]))
        self.assertTrue(h.wait(lambda: len(h.writer.gaps) >= 1))
        h.stop.set()
        h.reader.join(5.0)
        reader = h.reader
        before = len(h.writer.gaps)
        # An hwm far in the future makes start > end; the writer returns None and the reader
        # must neither count it nor crash.
        h.writer._hwm_us = tsutil.now_us() + 3600000000
        reader._gap_pending = True
        reader._last_gap_end_us = None
        counted = reader._gaps_emitted
        reader._emit_pending_gap()
        self.assertEqual(before, len(h.writer.gaps))
        self.assertEqual(counted, reader._gaps_emitted)

    def test_sub_second_gaps_are_coalesced_by_default(self):
        """A connect/EOF spin loop must not spam the gap queue (default spacing)."""
        h = self.make(_plan_frames([_frame(EVENT)]))
        self.assertTrue(h.wait(lambda: len(h.writer.gaps) >= 1))
        h.stop.set()
        h.reader.join(5.0)
        reader = h.reader
        reader._min_gap_spacing_us = ssereader.MIN_GAP_SPACING_US
        before = len(h.writer.gaps)
        reader._gap_pending = True
        reader._emit_pending_gap()
        self.assertEqual(before, len(h.writer.gaps))
        self.assertFalse(reader._gap_pending)


# --------------------------------------------------------------------------- #
# privacy — SPEC §11.4, standing operator order
# --------------------------------------------------------------------------- #


class TestPrivacy(SseReaderTestCase):
    def test_no_record_body_reaches_logs_or_status(self):
        """Chat and component text live in exactly one place: the JSONL (SPEC §11.4)."""
        payloads = [
            _frame(EVENT),
            b"data:{broken json " + CANARY_CHAT.encode() + b"\n\n",
            b"data:" + CANARY_CHAT.encode() + b"\n\n",
            b"weird:" + CANARY_CHAT.encode() + b"\n\n",
            _frame(EVENT2),
        ]
        plan = [_plan_frames(payloads, then="abrupt"), _plan_frames([_frame(EVENT)])]
        h = self.make(plan)
        self.assertTrue(h.wait_records(2))
        self.assertTrue(h.wait(lambda: h.writer.counters.get("parse_errors", 0) >= 2))
        text = h.logcap.text()
        self.assertNotIn(CANARY_CHAT, text)
        self.assertNotIn(CANARY_COMPONENT, text)
        self.assertNotIn("SyntheticPlayerOne", text)
        blob = json.dumps(h.status.snapshot(), default=str)
        self.assertNotIn(CANARY_CHAT, blob)
        self.assertNotIn(CANARY_COMPONENT, blob)
        # the row_id / ts metadata that IS allowed must be there instead
        self.assertIn(h.writer.records[0]["row_id"], text)

    def test_exception_summary_carries_no_payload_bytes(self):
        """IncompleteRead's repr embeds partial body bytes; never log it (SPEC §11.4)."""
        exc = http.client.IncompleteRead(CANARY_CHAT.encode("utf-8"))
        summary = ssereader._exc_summary(exc)
        self.assertIn("IncompleteRead", summary)
        self.assertNotIn(CANARY_CHAT, summary)
        try:
            raise OSError(104, "reset")
        except OSError as caught:
            summary = ssereader._exc_summary(caught)
        self.assertIn("OSError", summary)
        self.assertIn("errno=104", summary)
        self.assertIn("test_ssereader.py:", summary)


# --------------------------------------------------------------------------- #
# status surface — SPEC §11.3
# --------------------------------------------------------------------------- #


class TestStatus(SseReaderTestCase):
    def test_status_fields_are_metadata_only(self):
        h = self.make(_plan_frames([b":heartbeat\n\n", _frame(EVENT)]))
        self.assertTrue(h.wait_records(1))
        self.assertTrue(h.wait(lambda: h.status.snapshot()["sse"]["state"] == "connected"))
        sse = h.status.snapshot()["sse"]
        for key in (
            "state",
            "connections",
            "s_since_last_inbound",
            "s_since_last_event",
            "heartbeats",
            "reconnects_last_hour",
            "last_close_mode",
        ):
            self.assertIn(key, sse)
        self.assertEqual(1, sse["connections"])
        self.assertEqual(1, sse["reconnects_last_hour"])
        self.assertGreaterEqual(sse["s_since_last_inbound"], 0.0)
        self.assertLess(sse["s_since_last_inbound"], 90.0)
        self.assertEqual(1, sse["events"])
        self.assertEqual(1, sse["rows_written"])
        self.assertEqual(1, sse["gaps_emitted"])


# --------------------------------------------------------------------------- #
# the transfer decoder this module owns — SPEC §6.2 support
# --------------------------------------------------------------------------- #


class TestBodyDecoder(unittest.TestCase):
    """``_BodyDecoder`` exists because http.client's decoder cannot survive a read timeout."""

    def _chunk(self, payload: bytes) -> bytes:
        return ("%x\r\n" % (len(payload),)).encode("ascii") + payload + b"\r\n"

    def test_chunked_stream_byte_at_a_time(self):
        wire = self._chunk(b"data:one\n\n") + self._chunk(b":heartbeat\n\n") + b"0\r\n\r\n"
        dec = ssereader._BodyDecoder(True)
        out = bytearray()
        for i in range(len(wire)):
            out += dec.feed(wire[i : i + 1])
        self.assertEqual(b"data:one\n\n:heartbeat\n\n", bytes(out))
        self.assertTrue(dec.done)
        self.assertFalse(dec.expects_more())

    def test_chunked_stream_all_at_once(self):
        wire = self._chunk(b"data:one\n\n") + self._chunk(b"data:two\n\n") + b"0\r\n\r\n"
        dec = ssereader._BodyDecoder(True)
        self.assertEqual(b"data:one\n\ndata:two\n\n", dec.feed(wire))
        self.assertTrue(dec.done)

    def test_chunk_extensions_and_trailers(self):
        wire = b"a;name=value\r\n0123456789\r\n0\r\nX-Trailer: 1\r\n\r\n"
        dec = ssereader._BodyDecoder(True)
        self.assertEqual(b"0123456789", dec.feed(wire))
        self.assertTrue(dec.done)

    def test_truncated_chunked_body_expects_more(self):
        dec = ssereader._BodyDecoder(True)
        self.assertEqual(b"data:one\n\n", dec.feed(self._chunk(b"data:one\n\n")))
        self.assertFalse(dec.done)
        self.assertTrue(dec.expects_more(), "a missing terminator must read as truncated")

    def test_identity_passthrough_and_content_length(self):
        dec = ssereader._BodyDecoder(False, None)
        self.assertEqual(b"data:one\n\n", dec.feed(b"data:one\n\n"))
        self.assertFalse(dec.expects_more(), "identity EOF is the end of the body")
        dec = ssereader._BodyDecoder(False, 5)
        self.assertEqual(b"abcde", dec.feed(b"abcdefgh"))
        self.assertTrue(dec.done)

    def test_framing_errors(self):
        dec = ssereader._BodyDecoder(True)
        with self.assertRaises(ssereader._FramingError):
            dec.feed(b"zz\r\nbody\r\n")
        dec = ssereader._BodyDecoder(True)
        with self.assertRaises(ssereader._FramingError):
            dec.feed(b"f" * (ssereader.MAX_CHUNK_LINE_BYTES + 2))

    def test_unlatch_read_timeout(self):
        class _Raw:
            _timeout_occurred = True

        class _Fp:
            raw = _Raw()

        class _Resp:
            fp = _Fp()

        resp = _Resp()
        self.assertTrue(ssereader._unlatch_read_timeout(resp))
        self.assertFalse(resp.fp.raw._timeout_occurred)
        self.assertFalse(ssereader._unlatch_read_timeout(resp))
        self.assertFalse(ssereader._unlatch_read_timeout(object()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
