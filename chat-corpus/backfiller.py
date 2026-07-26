"""Gap backfill against ``GET /chats/window`` (SPEC §7).

Every SSE disconnect is a hole in the corpus: the feed carries no ``id:`` field and no
``Last-Event-ID`` support, so replay is impossible and the only way to recover a gap is to
re-query the REST endpoint. Since the server closes the stream roughly every 15 minutes
(SPEC §6.4), this thread is not an error path — it runs ~96 times a day in normal operation.

Four things here are counter-intuitive and are the reason this module reads the way it does:

1. **Paging is time-slicing, never offset.** The ``page`` parameter is never sent. ``ORDER BY
   time`` has no tiebreaker and the table has no primary key, so offset paging is formally
   unsafe at a page boundary inside a tie group (SPEC §7.3).
2. **Both window bounds are inclusive**, so the last row of page N is re-delivered as the
   first row of page N+1. That is intended; the dedupe key discards it for free. Adding an
   epsilon to the cursor to "fix" it provably DROPS rows — a real row at ``.872508`` vanishes
   when ``startDate`` is ``.873``.
3. **Query bounds carry no timezone suffix.** The endpoint binds a Java ``LocalDateTime`` and
   silently *discards* any offset, so sending ``Z`` queries the wrong instant with no error.
   :func:`tsutil.fmt_bound` is the only correct renderer for a bound.
4. **An unparseable date returns 302, not 400** — a redirect to the API explorer page. With
   redirect-following enabled that looks like a successful empty poll and stalls the backfill
   forever, silently. Hence: no redirects, and any non-JSON content type is rejected.

Privacy: no chat text, component value, or record body may reach a log line or an exception
message from this module (SPEC §11.4). Errors are described with counts, HTTP status, gap ids
and timestamps only.
"""

from __future__ import annotations

import http.client
import json
import logging
import random
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import record
import status as status_mod
import tsutil

#: Backoff ladder for HTTP 429, in seconds, before jitter (SPEC §7.6). Retried indefinitely:
#: the gap is durable and there is no deadline, so giving up would lose data permanently.
_BACKOFF_429 = (5, 10, 20, 40, 80, 160, 300)

#: Sleep between polls of the gap queue when there is nothing runnable.
_IDLE_POLL_S = 2.0

#: Statuses a gap can be finished with. "pending" returns it to the queue (graceful stop).
_ST_DONE = "done"
_ST_ERROR = "error"
_ST_PENDING = "pending"

_US_PER_DAY = 86_400_000_000


def _exc_summary(exc: BaseException) -> str:
    """Type name plus a *bounded* message, for logs (SPEC §11.4).

    Never interpolates a record body. Response bodies are never passed in here, so a
    ``JSONDecodeError`` — whose ``str()`` includes the offending document on some Python
    versions — can never leak chat text through this path.
    """
    return "%s(%.200s)" % (type(exc).__name__, exc)


class Backfiller(threading.Thread):
    """Drains the durable gap queue, one gap at a time, paced against a shared rate limit.

    ``/chats/window`` sits in a 5-permits/second bucket that is **global across every caller
    of the API**, not per-IP, with a 10 s acquire timeout and then a 429 carrying an empty
    body and no ``Retry-After``. Measured behaviour: ~18% of requests 429'd even while pacing
    at one request every 3-4 seconds. Throughput therefore depends on strangers' traffic and
    the only correct posture is to pace politely and retry forever (SPEC §7.6).
    """

    def __init__(self, cfg: dict, writer: Any, status: Any,
                 log: logging.Logger, stop: threading.Event) -> None:
        super().__init__(name="backfiller", daemon=True)
        self._cfg = cfg
        self._writer = writer
        self._status = status
        self._log = log
        self._stop = stop

        g = lambda *k, **kw: status_mod.cfg_get(cfg, "backfill", *k, **kw)  # noqa: E731
        self._host = str(g("host", default="api.2b2t.vc"))
        self._path = str(g("path", default="/chats/window"))
        self._page_size = int(status_mod.as_number(g("page_size", default=100), 100))
        self._min_interval_s = float(status_mod.as_number(g("min_interval_s", default=1.0), 1.0))
        self._timeout_s = float(status_mod.as_number(g("request_timeout_s", default=30), 30))
        self._backoff_max_s = float(status_mod.as_number(g("backoff_429_max_s", default=300), 300))
        self._audit_enabled = bool(g("audit_enabled", default=True))
        self._audit_interval_us = int(status_mod.as_number(
            g("audit_interval_days", default=7), 7)) * _US_PER_DAY
        self._audit_span_us = int(status_mod.as_number(
            g("audit_span_days", default=7), 7)) * _US_PER_DAY
        self._audit_lag_us = int(status_mod.as_number(
            g("audit_lag_days", default=1), 1)) * _US_PER_DAY

        self._ua = str(status_mod.cfg_get(cfg, "user_agent",
                                          default="chat-corpus-collector/1.0"))

        # Monotonic token bucket. time.monotonic() is immune to wall-clock steps (NTP, DST),
        # which a months-long unattended run will certainly see.
        self._next_request_at = 0.0
        self._last_429_at: Optional[float] = None
        self._requests = 0
        self._failed = False

    # ------------------------------------------------------------------ rate limiting

    def _pace(self) -> bool:
        """Block until the next request is allowed. False if we were asked to stop."""
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= self._next_request_at:
                self._next_request_at = now + self._min_interval_s
                return True
            if self._stop.wait(min(self._next_request_at - now, 0.5)):
                return False
        return False

    def _sleep_backoff(self, attempt: int) -> bool:
        """Jittered 429 backoff. False if we were asked to stop while waiting."""
        base = _BACKOFF_429[min(attempt, len(_BACKOFF_429) - 1)]
        delay = min(float(base), self._backoff_max_s)
        delay += random.uniform(0, 0.3 * delay)
        self._log.info("backfill 429 backoff attempt=%d delay_s=%.1f", attempt + 1, delay)
        return not self._stop.wait(delay)

    # ------------------------------------------------------------------ HTTP

    def _fetch(self, start_us: int, end_us: int) -> Tuple[int, Optional[str], Optional[bytes]]:
        """One ``/chats/window`` request. Returns ``(status, content_type, body)``.

        A fresh connection per request: at one request per second the TLS handshake is
        irrelevant next to the correctness win of never inheriting a half-dead socket.
        ``http.client`` does not follow redirects, which is exactly what we want — see the
        302 trap in the module docstring. Status -1 means the request never completed.
        """
        query = urllib.parse.urlencode([
            ("startDate", tsutil.fmt_bound(start_us)),   # NO zone suffix — SPEC §3.3
            ("endDate", tsutil.fmt_bound(end_us)),
            ("sort", "ASC"),                             # ASC requires startDate
            ("pageSize", str(self._page_size)),          # explicit: the default is 25
        ])
        url = "%s?%s" % (self._path, query)
        conn = None
        try:
            conn = http.client.HTTPSConnection(self._host, timeout=self._timeout_s)
            conn.request("GET", url, headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": self._ua,          # Cloudflare 403s urllib's default UA
                "Connection": "close",
            })
            resp = conn.getresponse()
            body = resp.read()
            self._requests += 1
            return resp.status, resp.getheader("Content-Type"), body
        except Exception as exc:
            self._log.warning("backfill request failed err=%s", _exc_summary(exc))
            return -1, None, None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------ gap processing

    def _ingest_page(self, rows: List[dict], gap_id: str,
                     known: set) -> Tuple[int, int, int]:
        """Write one page. Returns ``(written, seen, max_ts_us)``.

        A row that fails to normalise is counted and skipped, never fatal: one malformed
        record must not abandon a gap. ``max_ts_us`` is computed across the page rather than
        taken from the last element, so a server-side ordering surprise cannot walk the
        cursor backwards into an infinite loop.

        ``known`` is the per-gap dedupe set of SPEC §4.3c, seeded from what is already on
        disk for this gap's range. It is consulted **before** :meth:`Writer.put_record`
        because the writer's live set is pruned by *event* time against
        ``dedupe.window_s`` (6 h by default): a gap older or wider than that window has no
        keys left in the live set, so the writer would happily accept rows already in the
        corpus. That is not hypothetical — the weekly audit re-walks a 7-day span of which
        only the last 6 h is covered, so without this it would append roughly 124k
        duplicate rows every week, forever, and inflate ``audit_rows_added`` with them,
        destroying the one metric that is supposed to reveal whether the SSE feed is
        silently lossy (SPEC §7.8, §12.7).
        """
        written = 0
        seen = 0
        max_ts = -1
        now_us = tsutil.now_us()
        for row in rows:
            try:
                rec = record.from_window(
                    row, seq=self._writer.next_seq(), batch=gap_id, ingest_us=now_us)
            except Exception as exc:
                self._writer.bump("parse_errors")
                # Metadata only: the offending row is never logged (SPEC §11.4).
                self._log.warning("backfill row rejected gap_id=%s err=%s",
                                  gap_id, _exc_summary(exc))
                continue
            seen += 1
            ts_us = int(rec["ts_us"])
            if ts_us > max_ts:
                max_ts = ts_us
            rid = rec["row_id"]
            if rid in known:
                self._writer.bump("dupes_dropped")
                continue
            if self._writer.put_record(rec):
                written += 1
                known.add(rid)
        return written, seen, max_ts

    def _run_gap(self, gap: dict) -> None:
        """Walk one gap to completion by time-slicing (SPEC §7.3, §7.4, §7.5)."""
        gap_id = str(gap.get("gap_id"))
        start_us = int(gap.get("start_us") or 0)
        end_us = int(gap.get("end_us") or 0)
        cursor = int(gap.get("cursor_us") or start_us)
        attempt_429 = 0
        pages = 0
        total_written = 0

        # SPEC §4.3c: seed this gap's dedupe set from the corpus itself. Seeded from the
        # gap's FULL range rather than from the cursor, so a gap resumed mid-walk after a
        # restart still recognises the rows its earlier pages already wrote.
        try:
            known, truncated = self._writer.row_ids_in_range(start_us, end_us)
        except Exception as exc:
            known, truncated = set(), True
            self._log.error("could not seed gap dedupe set gap_id=%s err=%s",
                            gap_id, _exc_summary(exc))
        if truncated:
            # Not fatal, but the operator should know duplicates are possible for this gap:
            # the byte budget ran out before the corpus did, so the set is incomplete.
            self._log.warning("gap dedupe set truncated by byte budget gap_id=%s "
                              "keys=%d: duplicate rows are possible for this range",
                              gap_id, len(known))
        else:
            self._log.info("gap dedupe set seeded gap_id=%s keys=%d", gap_id, len(known))

        while not self._stop.is_set():
            if cursor > end_us:
                self._writer.finish_gap(gap_id, _ST_DONE)
                return
            if not self._pace():
                break

            sent_cursor = cursor
            code, ctype, body = self._fetch(sent_cursor, end_us)

            if code == 429:
                self._last_429_at = time.monotonic()
                self._publish()
                if not self._sleep_backoff(attempt_429):
                    break
                attempt_429 += 1
                continue

            if code in (301, 302, 303, 307, 308):
                # SPEC §7.5: an unparseable date redirects to the API explorer. This is a
                # permanent bug in our request, never a transient condition. Alarm and stop
                # — retrying would spin forever against a request that cannot succeed.
                self._log.error("backfill got redirect: permanent request bug gap_id=%s "
                                "http=%d start=%s end=%s", gap_id, code,
                                tsutil.fmt_bound(sent_cursor), tsutil.fmt_bound(end_us))
                self._writer.finish_gap(gap_id, _ST_ERROR)
                self._alert("backfill_redirect",
                            "gap %s got HTTP %d (unparseable date bound)" % (gap_id, code))
                return

            if code == 400:
                self._log.error("backfill got 400: permanent request bug gap_id=%s "
                                "start=%s end=%s", gap_id,
                                tsutil.fmt_bound(sent_cursor), tsutil.fmt_bound(end_us))
                self._writer.finish_gap(gap_id, _ST_ERROR)
                self._alert("backfill_bad_request", "gap %s got HTTP 400" % gap_id)
                return

            if code == 204:
                # End of window. There is never a 200 with an empty array (SPEC §7.4).
                self._writer.finish_gap(gap_id, _ST_DONE)
                self._log.info("gap complete (204) gap_id=%s pages=%d rows=%d",
                               gap_id, pages, total_written)
                return

            if code != 200:
                # Includes -1 (request never completed) and 5xx. Transient: leave the gap
                # runnable and let the queue re-claim it rather than burning it.
                self._log.warning("backfill unexpected status gap_id=%s http=%d", gap_id, code)
                if not self._sleep_backoff(attempt_429):
                    break
                attempt_429 += 1
                continue

            if not (ctype or "").lower().startswith("application/json"):
                # SPEC §7.5: a non-JSON 200 is the API explorer page arriving via a followed
                # redirect, or an edge error page. Never try to parse it.
                self._log.error("backfill got non-JSON 200 gap_id=%s content_type=%.60s",
                                gap_id, ctype)
                self._writer.finish_gap(gap_id, _ST_ERROR)
                self._alert("backfill_non_json", "gap %s got a non-JSON 200" % gap_id)
                return

            try:
                doc = json.loads((body or b"").decode("utf-8", "replace"))
                rows = doc["chats"]
                if not isinstance(rows, list):
                    raise TypeError("chats is not a list")
            except Exception as exc:
                # Deliberately does not log `body` — it is chat content (SPEC §11.4).
                self._log.error("backfill malformed body gap_id=%s bytes=%d err=%s",
                                gap_id, len(body or b""), _exc_summary(exc))
                self._writer.finish_gap(gap_id, _ST_ERROR)
                self._alert("backfill_malformed", "gap %s returned an unparseable body" % gap_id)
                return

            attempt_429 = 0
            pages += 1
            written, seen, max_ts = self._ingest_page(rows, gap_id, known)
            total_written += written

            if seen == 0:
                # Every row in a non-empty page failed to normalise, or the page was empty
                # despite a 200. Either way the cursor cannot advance from the data.
                if len(rows) < self._page_size:
                    self._writer.finish_gap(gap_id, _ST_DONE)
                    return
                cursor = sent_cursor + 1
                self._writer.advance_gap(gap_id, cursor, 0)
                continue

            if len(rows) >= self._page_size and max_ts == sent_cursor and written == 0:
                # SPEC §7.4 tie-group guard: a full page whose last timestamp equals the
                # cursor we sent, every row already known. pageSize is already at max, so
                # the only way forward is to step past the tie by one microsecond and accept
                # that rows sharing that exact instant beyond the page may be missed.
                #
                # The page-FULL check is load-bearing, not defensive. Without it this fires
                # on the ordinary last fetch of a healthy walk: the cursor sits *on* the
                # last row written, the inclusive lower bound re-delivers exactly that row,
                # so max_ts == sent_cursor and written == 0 with a one-row page. That is
                # end-of-window, not a tie group — nothing is beyond the page to lose. A
                # short page cannot hide a tie group by definition, and since tie_skips is
                # a cumulative counter wired to a health warning that is never reset, one
                # spurious bump pins the collector at warn permanently and destroys the
                # signal SPEC §7.4 wants it to carry ("treat any occurrence as a signal
                # that the lookback clamp misfired").
                self._writer.bump("tie_skips")
                self._log.warning("tie group larger than a page gap_id=%s ts_us=%d "
                                  "page_size=%d: advancing cursor by 1us, rows at this "
                                  "instant beyond the page may be lost",
                                  gap_id, sent_cursor, self._page_size)
                cursor = sent_cursor + 1
            else:
                cursor = max(max_ts, sent_cursor)

            self._writer.advance_gap(gap_id, cursor, written)
            self._publish()

            if len(rows) < self._page_size:
                self._writer.finish_gap(gap_id, _ST_DONE)
                self._log.info("gap complete (short page) gap_id=%s pages=%d rows=%d",
                               gap_id, pages, total_written)
                return

        # Stopped mid-gap: hand it back so the next run resumes from the persisted cursor
        # rather than leaving it 'running' for the startup requeue to rescue.
        if self._stop.is_set():
            self._writer.finish_gap(gap_id, _ST_PENDING)
            self._log.info("gap suspended for shutdown gap_id=%s cursor_us=%d rows=%d",
                           gap_id, cursor, total_written)

    # ------------------------------------------------------------------ audit

    def _maybe_enqueue_audit(self) -> None:
        """Weekly completeness audit (SPEC §7.8).

        The SSE feed and the Postgres table are fed by *separate* ingest paths — a Redis
        reliable topic published by an external bot fleet versus the API's own writer — and
        there is no code-level guarantee that the two sets are equal in either direction.
        Nothing in normal operation would reveal a systematic shortfall, because no
        disconnect occurs and so no gap is created. Re-walking a settled week via REST and
        reporting ``audit_rows_added`` is the only mechanism that can detect it.
        """
        if not self._audit_enabled:
            return
        state_last = 0
        try:
            state_last = int(self._writer.audit_last_run_us())
        except Exception:
            state_last = 0
        now_us = tsutil.now_us()
        if state_last and now_us - state_last < self._audit_interval_us:
            return

        end_us = now_us - self._audit_lag_us
        start_us = end_us - self._audit_span_us
        # clamp=False: this window is synthesised from `now` and bounded by config on both
        # ends, so it has no runaway path — whereas the §7.9 clamp would fire on it every
        # week and pin health to critical forever. See Writer.enqueue_gap.
        gid = self._writer.enqueue_gap(start_us, end_us, tag="audit", clamp=False)
        try:
            self._writer.set_audit_last_run(now_us)
        except Exception as exc:
            self._log.warning("could not persist audit timestamp err=%s", _exc_summary(exc))
        if gid:
            self._log.info("weekly completeness audit queued gap_id=%s span_days=%d",
                           gid, self._audit_span_us // _US_PER_DAY)

    # ------------------------------------------------------------------ status

    def _alert(self, kind: str, reason: str) -> None:
        try:
            status_mod.fire_alert(self._cfg, self._log, "critical", "%s: %s" % (kind, reason))
        except Exception as exc:
            self._log.warning("alert failed err=%s", _exc_summary(exc))

    def _publish(self) -> None:
        try:
            snap = self._writer.snapshot()
            counts = snap.get("backfill", {}) if isinstance(snap, dict) else {}
            fields: Dict[str, Any] = {
                "requests": self._requests,
                "last_429_s_ago": (None if self._last_429_at is None
                                   else round(time.monotonic() - self._last_429_at, 1)),
            }
            if isinstance(counts, dict):
                fields.update(counts)
            self._status.update("backfill", **fields)
        except Exception as exc:
            self._log.debug("backfill status update failed err=%s", _exc_summary(exc))

    # ------------------------------------------------------------------ main loop

    def run(self) -> None:
        """Claim and drain gaps until stopped. Never raises (SPEC §11.2)."""
        self._log.info("backfiller started host=%s min_interval_s=%.2f page_size=%d",
                       self._host, self._min_interval_s, self._page_size)
        try:
            while not self._stop.is_set():
                try:
                    self._maybe_enqueue_audit()
                except Exception as exc:
                    self._log.warning("audit scheduling failed err=%s", _exc_summary(exc))

                gap = None
                try:
                    gap = self._writer.claim_gap()
                except Exception as exc:
                    self._log.error("claim_gap failed err=%s", _exc_summary(exc))

                if gap is None:
                    self._publish()
                    if self._stop.wait(_IDLE_POLL_S):
                        break
                    continue

                try:
                    self._run_gap(gap)
                except Exception as exc:
                    gid = str(gap.get("gap_id"))
                    self._log.exception("gap walk crashed gap_id=%s err=%s",
                                        gid, _exc_summary(exc))
                    try:
                        self._writer.finish_gap(gid, _ST_ERROR)
                    except Exception:
                        pass
        except Exception as exc:
            # A dead backfill thread must be visible, not silent (SPEC §11.3).
            self._failed = True
            self._log.critical("backfiller died err=%s", _exc_summary(exc))
        finally:
            self._publish()
            self._log.info("backfiller stopped requests=%d", self._requests)

    @property
    def failed(self) -> bool:
        return self._failed
