"""Tests for backfiller.py (SPEC §7).

Runs against a real ``http.server`` on 127.0.0.1:0 rather than a mocked transport, because
three of the defects this module has to avoid live in the HTTP layer itself: the 302 trap,
the inclusive-bound boundary row, and the never-send-``page`` rule. A mock that returns
dicts would not exercise any of them.

No test contains real chat text or real coordinates (SPEC §11.4).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import backfiller
import config
import statefile
import tsutil
import writer

CHAT = "hello-corpus-sentinel"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        try:
            self.records.append(self.format(record))
        except Exception:
            self.records.append(record.getMessage())

    def text(self):
        return "\n".join(self.records)


class _Handler(BaseHTTPRequestHandler):
    """Emulates GET /chats/window. Behaviour comes from server.script."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        self.server.requests.append(qs)
        step = self.server.script(qs, len(self.server.requests) - 1)
        code, ctype, body = step
        self.send_response(code)
        if ctype:
            self.send_header("Content-Type", ctype)
        if code in (301, 302):
            self.send_header("Location", "https://example.invalid/scalar")
        payload = body if isinstance(body, bytes) else (body or "").encode("utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)


def _rows(items):
    return json.dumps({"chats": [
        {"time": tsutil.fmt_ts(ts), "chat": txt, "playerName": name, "uuid": uid}
        for ts, txt, name, uid in items
    ]})


class BackfillerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="chatcol-bf-")
        self.log = logging.getLogger("test.backfiller." + self.id())
        self.log.setLevel(logging.DEBUG)
        self.log.propagate = False
        self.cap = _Capture()
        self.log.handlers = [self.cap]
        self.base = tsutil.parse_ts("2026-07-25T12:00:00Z")
        self.addCleanup(self._teardown)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.srv.requests = []
        self.srv.script = lambda qs, n: (204, None, b"")
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def _teardown(self):
        try:
            self.srv.shutdown()
            self.srv.server_close()
        except Exception:
            pass
        for w in getattr(self, "_writers", []):
            try:
                w.close()
            except Exception:
                pass
        leak = self.cap.text()
        self.assertNotIn(CHAT, leak, "chat text leaked into a log record (SPEC §11.4)")
        self.log.handlers = []
        shutil.rmtree(self.dir, ignore_errors=True)

    # ---------------------------------------------------------------- helpers

    def _cfg(self, **over):
        cfg = json.loads(json.dumps(config.DEFAULTS))
        cfg["data_dir"] = self.dir
        cfg["state_path"] = os.path.join(self.dir, "state.json")
        cfg["status_path"] = os.path.join(self.dir, "status.json")
        cfg["lock_path"] = os.path.join(self.dir, "c.lock")
        cfg["log_path"] = os.path.join(self.dir, "c.log")
        cfg["backfill"]["host"] = "127.0.0.1:%d" % self.port
        cfg["backfill"]["min_interval_s"] = 0.0
        cfg["backfill"]["request_timeout_s"] = 5
        cfg["backfill"]["audit_enabled"] = False
        cfg["backfill"]["page_size"] = 2
        cfg.update(over)
        return cfg

    def _writer(self, cfg):
        st = statefile.new_state(self.base)
        st["hwm_us"] = self.base
        w = writer.Writer(cfg, st, self.log)
        self._writers = getattr(self, "_writers", [])
        self._writers.append(w)
        return w

    def _bf(self, cfg, w):
        stop = threading.Event()
        status = _FakeStatus()
        bf = backfiller.Backfiller(cfg, w, status, self.log, stop)
        # http.client needs host:port split; HTTPSConnection would try TLS, so patch the
        # fetch to plain HTTP against the loopback fixture.
        bf._fetch = _plain_fetch(bf)
        return bf, stop

    def _corpus_rows(self):
        out = []
        for fn in sorted(os.listdir(self.dir)):
            if not fn.startswith("chat-") or not fn.endswith(".jsonl"):
                continue
            with open(os.path.join(self.dir, fn), "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        out.append(json.loads(line))
        return out

    # ---------------------------------------------------------------- tests

    def test_never_sends_page_parameter(self):
        """SPEC §7.3: offset paging is unsafe at a tie-group boundary; time-slice instead."""
        cfg = self._cfg()
        w = self._writer(cfg)
        self.srv.script = lambda qs, n: (
            (200, "application/json",
             _rows([(self.base + 1_000_000, CHAT, "Alice", "a" * 8 + "-1111-2222-3333-444444444444")]))
            if n == 0 else (204, None, b""))
        bf, stop = self._bf(cfg, w)
        gid = w.enqueue_gap(self.base, self.base + 60_000_000)
        bf._run_gap(dict(w.claim_gap()))
        self.assertTrue(self.srv.requests)
        for qs in self.srv.requests:
            self.assertNotIn("page", qs, "the page parameter must never be sent")
            self.assertEqual(qs["pageSize"], ["2"])
            self.assertEqual(qs["sort"], ["ASC"])

    def test_bounds_carry_no_timezone_suffix(self):
        """SPEC §3.3: the endpoint binds LocalDateTime and silently discards an offset."""
        cfg = self._cfg()
        w = self._writer(cfg)
        bf, stop = self._bf(cfg, w)
        w.enqueue_gap(self.base, self.base + 60_000_000)
        bf._run_gap(dict(w.claim_gap()))
        qs = self.srv.requests[0]
        for key in ("startDate", "endDate"):
            val = qs[key][0]
            self.assertNotIn("Z", val)
            self.assertNotIn("+", val)
            self.assertRegex(val, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}$")

    def test_redirect_is_permanent_and_not_retried(self):
        """SPEC §7.5: an unparseable date returns 302, not 400. Retrying spins forever."""
        cfg = self._cfg()
        w = self._writer(cfg)
        self.srv.script = lambda qs, n: (302, "text/html", b"<html/>")
        bf, stop = self._bf(cfg, w)
        gid = w.enqueue_gap(self.base, self.base + 60_000_000)
        bf._run_gap(dict(w.claim_gap()))
        self.assertEqual(len(self.srv.requests), 1, "a 302 must not be retried")
        snap = w.snapshot()
        self.assertEqual(snap["backfill"]["gaps_error"], 1)

    def test_400_is_permanent_and_not_retried(self):
        cfg = self._cfg()
        w = self._writer(cfg)
        self.srv.script = lambda qs, n: (400, "application/json", b"{}")
        bf, stop = self._bf(cfg, w)
        w.enqueue_gap(self.base, self.base + 60_000_000)
        bf._run_gap(dict(w.claim_gap()))
        self.assertEqual(len(self.srv.requests), 1)
        self.assertEqual(w.snapshot()["backfill"]["gaps_error"], 1)

    def test_non_json_200_is_rejected(self):
        """A 200 text/html is the explorer page arriving via a followed redirect."""
        cfg = self._cfg()
        w = self._writer(cfg)
        self.srv.script = lambda qs, n: (200, "text/html", b"<html>ok</html>")
        bf, stop = self._bf(cfg, w)
        w.enqueue_gap(self.base, self.base + 60_000_000)
        bf._run_gap(dict(w.claim_gap()))
        self.assertEqual(w.snapshot()["backfill"]["gaps_error"], 1)

    def test_inclusive_boundary_row_written_exactly_once(self):
        """SPEC §7.3: both bounds are inclusive, so page N's last row is page N+1's first.

        The cursor must NOT be nudged forward to avoid it (that provably drops rows); the
        duplicate has to be absorbed by dedupe instead.
        """
        cfg = self._cfg()
        w = self._writer(cfg)
        uid = "aaaaaaaa-1111-2222-3333-444444444444"
        t0, t1, t2 = self.base + 1_000_000, self.base + 2_000_000, self.base + 3_000_000

        def script(qs, n):
            if n == 0:
                return (200, "application/json",
                        _rows([(t0, CHAT + "-0", "Alice", uid), (t1, CHAT + "-1", "Bob", uid)]))
            if n == 1:
                # inclusive lower bound re-delivers t1
                return (200, "application/json",
                        _rows([(t1, CHAT + "-1", "Bob", uid), (t2, CHAT + "-2", "Cara", uid)]))
            return (204, None, b"")

        self.srv.script = script
        bf, stop = self._bf(cfg, w)
        w.enqueue_gap(self.base, self.base + 60_000_000)
        bf._run_gap(dict(w.claim_gap()))

        rows = self._corpus_rows()
        ids = [r["row_id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)), "boundary row written more than once")
        self.assertEqual(len(rows), 3)

    def test_rewalk_of_old_gap_writes_no_duplicates(self):
        """Regression: the audit re-walks a 7-day span but the live dedupe window is 6 h.

        Without the per-gap set seeded from disk (SPEC §4.3c via row_ids_in_range), the
        second walk appends every row again -- ~124k duplicates a week -- and inflates
        audit_rows_added, destroying the only metric that can reveal a lossy feed.
        """
        cfg = self._cfg()
        cfg["dedupe"]["window_s"] = 60          # far narrower than the gap below
        w = self._writer(cfg)
        uid = "bbbbbbbb-1111-2222-3333-444444444444"
        # Rows spread over 3 hours: way outside a 60-second live window.
        items = [(self.base + i * 3_600_000_000, CHAT + "-%d" % i, "P%d" % i, uid)
                 for i in range(3)]

        def script(qs, n):
            # Every walk: one full page then 204.
            return ((200, "application/json", _rows(items[:2])) if n % 2 == 0
                    else (204, None, b""))

        self.srv.script = script
        bf, stop = self._bf(cfg, w)
        span_end = self.base + 4 * 3_600_000_000

        w.enqueue_gap(self.base, span_end)
        bf._run_gap(dict(w.claim_gap()))
        first = len(self._corpus_rows())
        self.assertEqual(first, 2)

        # Second walk over the identical range, as pass 2 / the weekly audit would do.
        w.enqueue_gap(self.base, span_end, pass_no=2, clamp=False)
        gap = w.claim_gap()
        self.assertIsNotNone(gap, "second pass gap should be claimable")
        bf._run_gap(dict(gap))

        rows = self._corpus_rows()
        ids = [r["row_id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)),
                         "re-walking an old gap duplicated rows: the per-gap dedupe set "
                         "was not seeded from disk")
        self.assertEqual(len(rows), first)

    def test_short_page_terminates_the_gap(self):
        cfg = self._cfg()
        w = self._writer(cfg)
        uid = "cccccccc-1111-2222-3333-444444444444"
        self.srv.script = lambda qs, n: (
            200, "application/json",
            _rows([(self.base + 1_000_000, CHAT, "Solo", uid)]))   # 1 < page_size 2
        bf, stop = self._bf(cfg, w)
        w.enqueue_gap(self.base, self.base + 60_000_000)
        bf._run_gap(dict(w.claim_gap()))
        self.assertEqual(len(self.srv.requests), 1, "a short page ends the walk")
        self.assertEqual(w.snapshot()["backfill"]["gaps_done"], 1)

    def test_cursor_persisted_after_every_page(self):
        """SPEC §7.1: a 429 storm or a restart must resume mid-gap, not re-walk."""
        cfg = self._cfg()
        w = self._writer(cfg)
        uid = "dddddddd-1111-2222-3333-444444444444"
        t0, t1 = self.base + 1_000_000, self.base + 2_000_000
        self.srv.script = lambda qs, n: (
            (200, "application/json", _rows([(t0, CHAT, "A", uid), (t1, CHAT, "B", uid)]))
            if n == 0 else (204, None, b""))
        bf, stop = self._bf(cfg, w)
        gid = w.enqueue_gap(self.base, self.base + 60_000_000)
        bf._run_gap(dict(w.claim_gap()))
        saved = json.load(open(cfg["state_path"], encoding="utf-8"))
        gaps = {g["gap_id"]: g for g in saved["gaps"]}
        self.assertIn(gid, gaps)
        self.assertGreaterEqual(int(gaps[gid]["cursor_us"]), t1)


class _FakeStatus(object):
    def __init__(self):
        self.sections = {}

    def update(self, section, **fields):
        self.sections.setdefault(section, {}).update(fields)


def _plain_fetch(bf):
    """Swap HTTPS for plain HTTP against the loopback fixture, keeping every other
    behaviour (query construction, redirect refusal, content-type check) under test."""
    import http.client
    import urllib.parse as _up

    def fetch(start_us, end_us):
        query = _up.urlencode([
            ("startDate", tsutil.fmt_bound(start_us)),
            ("endDate", tsutil.fmt_bound(end_us)),
            ("sort", "ASC"),
            ("pageSize", str(bf._page_size)),
        ])
        conn = http.client.HTTPConnection(bf._host, timeout=bf._timeout_s)
        try:
            conn.request("GET", "%s?%s" % (bf._path, query),
                         headers={"Accept": "application/json",
                                  "User-Agent": bf._ua,
                                  "Connection": "close"})
            resp = conn.getresponse()
            body = resp.read()
            bf._requests += 1
            return resp.status, resp.getheader("Content-Type"), body
        finally:
            try:
                conn.close()
            except Exception:
                pass

    return fetch


if __name__ == "__main__":
    unittest.main()
