"""SPEC §13 non-negotiable test 2 — the boundary row across a 3-day downtime.

Both bounds of ``/chats/window`` are **inclusive**, so a stitched walk re-requests the row
it just finished on and the server dutifully hands it back. The obvious fix — start the next
page one microsecond past the last row — provably drops data, because two rows can share a
microsecond and a real event at ``…872508`` disappears the moment a bound is rounded to
``…873``. So the collector deliberately re-fetches the boundary row every page and relies on
dedupe to discard it.

This test heals a simulated 3-day outage against a server implementing genuine inclusive
semantics, then asserts the boundary row is written **exactly once**.

The assertion that matters most is not "no duplicates" — an empty corpus has no duplicates,
and so does a server that never re-delivers anything. It is that ``dupes_dropped`` is at
least one per page boundary, which is what proves the re-delivery actually happened and was
caught rather than never having occurred.

No network beyond 127.0.0.1. No real chat text, no real coordinates (SPEC §11.4).
"""
from __future__ import annotations

import datetime
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

CHAT = "hello-boundary-sentinel"
_S = 1_000_000
_DAY = 86_400 * _S
PAGE = 3


def _bound_to_us(text):
    """Parse the zone-less bound the collector sends: '%Y-%m-%dT%H:%M:%S.%f', read as UTC."""
    dt = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%f")
    dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1_000_000)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        try:
            self.records.append(record.getMessage())
        except Exception:
            self.records.append("<unformattable>")

    def text(self):
        return "\n".join(self.records)


class _InclusiveHandler(BaseHTTPRequestHandler):
    """A faithful /chats/window: rows where startDate <= ts <= endDate, ASC, capped at
    pageSize. The inclusive lower bound is the whole point -- it is what re-delivers the
    boundary row, so it must not be 'helpfully' made exclusive here."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        self.server.requests.append(qs)
        start = _bound_to_us(qs["startDate"][0])
        end = _bound_to_us(qs["endDate"][0])
        size = int(qs["pageSize"][0])
        sel = [r for r in self.server.data if start <= r[0] <= end][:size]

        if not sel:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.server.pages.append([r[0] for r in sel])
        body = json.dumps({"chats": [
            {"time": tsutil.fmt_ts(ts), "chat": txt, "playerName": name, "uuid": uid}
            for ts, txt, name, uid in sel
        ]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _FakeStatus(object):
    def __init__(self):
        self.sections = {}

    def update(self, section, **fields):
        self.sections.setdefault(section, {}).update(fields)


class BoundaryRowTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="chatcol-boundary-")
        self.log = logging.getLogger("test.boundary." + self.id())
        self.log.setLevel(logging.DEBUG)
        self.log.propagate = False
        self.cap = _Capture()
        self.log.handlers = [self.cap]
        # The outage runs from here to +3 days.
        self.base = tsutil.parse_ts("2026-07-22T00:00:00Z")
        self._writers = []
        self.addCleanup(self._teardown)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _InclusiveHandler)
        self.srv.requests = []
        self.srv.pages = []
        self.srv.data = []
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def _teardown(self):
        try:
            self.srv.shutdown()
            self.srv.server_close()
        except Exception:
            pass
        for w in self._writers:
            try:
                w.close()
            except Exception:
                pass
        self.assertNotIn(CHAT, self.cap.text(),
                         "chat text leaked into a log record (SPEC §11.4)")
        self.log.handlers = []
        shutil.rmtree(self.dir, ignore_errors=True)

    # ---------------------------------------------------------------- helpers

    def _cfg(self):
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
        cfg["backfill"]["page_size"] = PAGE
        # 3 days of downtime must not be clipped by the lookback clamp.
        cfg["backfill"]["max_lookback_days"] = 30
        return cfg

    def _writer(self, cfg):
        st = statefile.new_state(self.base)
        st["hwm_us"] = self.base
        w = writer.Writer(cfg, st, self.log)
        self._writers.append(w)
        return w

    def _bf(self, cfg, w):
        import http.client
        import urllib.parse as _up

        bf = backfiller.Backfiller(cfg, w, _FakeStatus(), self.log,
                                   threading.Event())

        def fetch(start_us, end_us):
            query = _up.urlencode([
                ("startDate", tsutil.fmt_bound(start_us)),
                ("endDate", tsutil.fmt_bound(end_us)),
                ("sort", "ASC"),
                ("pageSize", str(bf._page_size)),
            ])
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            try:
                conn.request("GET", "/chats/window?" + query)
                resp = conn.getresponse()
                body = resp.read()
                return resp.status, resp.getheader("Content-Type"), body
            finally:
                conn.close()

        bf._fetch = fetch
        return bf

    def _rows_on_disk(self):
        out = []
        for fn in sorted(os.listdir(self.dir)):
            if fn.startswith("chat-") and fn.endswith(".jsonl"):
                with open(os.path.join(self.dir, fn), "r", encoding="utf-8") as fh:
                    for line in fh:
                        if line.strip():
                            out.append(json.loads(line))
        return out

    def _dataset(self, n, ties=()):
        """`n` rows spread irregularly over the 3-day outage.

        Irregular on purpose: an even spacing can make a page boundary land where an
        off-by-one is invisible. `ties` gives indexes that share the previous row's exact
        microsecond, which is the case an epsilon-advanced bound would silently drop.
        """
        data, ts = [], self.base
        # Spread across the whole outage, not clustered at its start -- otherwise a "3-day"
        # window is really a 20-minute one and the first-day resume case never triggers.
        step = (3 * _DAY) // (n + 2)
        for i in range(n):
            if i in ties:
                pass                      # reuse the previous ts exactly
            else:
                ts += step + ((i * 7919) % 97) * 1_000_000 + (i * 13) % 1000
            data.append((ts, "%s-%03d" % (CHAT, i),
                         "Player%02d" % (i % 5),
                         "0000000%d-1111-2222-3333-4444444444%02d" % (i % 10, i % 100)))
        self.srv.data = data
        return data

    def _heal(self, cfg, w, span_us=3 * _DAY):
        """Enqueue the outage and drain the queue, as the worker thread does.

        Draining rather than claiming once is not tidiness: finishing a gap auto-enqueues
        its pass-2 confirming re-walk, so the queue routinely holds more than one eligible
        gap and a single claim_gap() can return a different one than the gap just added.
        """
        bf = self._bf(cfg, w)
        w.enqueue_gap(self.base, self.base + span_us)
        ran = 0
        while True:
            gap = w.claim_gap()
            if gap is None:
                break
            bf._run_gap(dict(gap))
            ran += 1
            self.assertLess(ran, 50, "gap queue did not drain -- gaps are re-enqueueing")
        self.assertGreater(ran, 0, "no gap was claimed, so nothing was healed")
        return bf

    # ---------------------------------------------------------------- tests

    def test_boundary_row_written_exactly_once_across_three_day_downtime(self):
        cfg = self._cfg()
        w = self._writer(cfg)
        data = self._dataset(31)

        self._heal(cfg, w)

        rows = self._rows_on_disk()
        counters = w.snapshot()["counters"]

        # Enough pages that boundaries actually exist.
        self.assertGreater(len(self.srv.pages), 3,
                           "the walk did not paginate, so no boundary was crossed")

        # 1. The server really did re-deliver the boundary row: within a walk, every page
        #    after the first must repeat the previous page's last timestamp. If this fails
        #    the fixture is wrong and the rest of the test proves nothing.
        #
        #    Seams are counted per walk. Draining the queue runs the pass-2 confirming
        #    re-walk as well, which restarts at the window start -- that step backwards is
        #    a new walk, not a seam that failed to repeat.
        seams = repeats = 0
        for prev, cur in zip(self.srv.pages, self.srv.pages[1:]):
            if not prev or not cur or cur[0] < prev[-1]:
                continue
            seams += 1
            if cur[0] == prev[-1]:
                repeats += 1
        self.assertGreater(seams, 3, "not enough page seams to test anything")
        self.assertEqual(
            repeats, seams,
            "the fixture stopped re-delivering the boundary row (%d of %d page seams), so "
            "this test would pass without the collector deduping anything"
            % (repeats, seams))

        # 2. ...and the collector dropped exactly those re-deliveries.
        self.assertGreaterEqual(
            counters["dupes_dropped"], seams,
            "expected at least one dropped duplicate per page seam (%d seams), got %d"
            % (seams, counters["dupes_dropped"]))

        # 3. Every row exactly once, and nothing extra.
        ids = [r["row_id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)), "a row_id was written twice")
        self.assertEqual(len(rows), len(data),
                         "expected %d rows, found %d" % (len(data), len(rows)))
        self.assertEqual(sorted(r["ts_us"] for r in rows), sorted(d[0] for d in data),
                         "the set of timestamps on disk does not match the dataset")

        # 4. No row was lost to the tie-group escape hatch on a dataset with no ties.
        self.assertEqual(counters["tie_skips"], 0,
                         "tie_skips bumped on a dataset containing no tied timestamps")
        self.assertEqual(counters["rows_backfill"], len(data))

    def test_rows_sharing_a_microsecond_at_a_page_seam_all_survive(self):
        """The reason the bound is not advanced by an epsilon.

        Ties are placed to land on and around page seams, so a walk that moved the next
        startDate past the last row's timestamp would drop the tail of each tie group.
        """
        cfg = self._cfg()
        w = self._writer(cfg)
        # Pairs sharing a microsecond, positioned so seams (every PAGE=3 rows) fall inside
        # and beside them.
        data = self._dataset(25, ties=(3, 6, 7, 12, 18, 19))

        self._heal(cfg, w)

        rows = self._rows_on_disk()
        self.assertEqual(len(rows), len(data),
                         "expected %d rows, found %d -- a tied timestamp was dropped"
                         % (len(data), len(rows)))
        self.assertEqual(sorted(r["ts_us"] for r in rows), sorted(d[0] for d in data))
        ids = [r["row_id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)), "a row_id was written twice")
        # Distinct rows sharing one microsecond must survive as distinct rows: row_id keys
        # on chat and player too, not on ts alone.
        for ts in {d[0] for d in data}:
            self.assertEqual(len([r for r in rows if r["ts_us"] == ts]),
                             len([d for d in data if d[0] == ts]),
                             "wrong row count at ts_us=%d" % ts)

    def test_tie_group_larger_than_a_page_still_trips_the_guard(self):
        """The positive case for the non-advancing-cursor guard (SPEC §7.4).

        The guard only fires on a **full** page whose last timestamp equals the cursor sent
        with every row already known — a genuine tie group wider than a page, where the only
        way forward is to step past the tie and accept losing the rows beyond it. This test
        exists because narrowing that condition to full pages could otherwise disable the
        guard entirely without any test noticing.
        """
        cfg = self._cfg()
        w = self._writer(cfg)
        t0 = self.base + 1 * _S
        tie = self.base + 2 * _S           # PAGE + 2 rows share this exact microsecond
        t2 = self.base + 3 * _S
        data = [(t0, CHAT + "-a", "Alice", "00000001-1111-2222-3333-444444444401")]
        data += [(tie, "%s-tie%d" % (CHAT, i), "Bob",
                  "00000002-1111-2222-3333-4444444444%02d" % i)
                 for i in range(PAGE + 2)]
        data += [(t2, CHAT + "-z", "Carol", "00000003-1111-2222-3333-444444444403")]
        self.srv.data = data

        self._heal(cfg, w, span_us=60 * _S)

        counters = w.snapshot()["counters"]
        rows = self._rows_on_disk()

        self.assertGreaterEqual(
            counters["tie_skips"], 1,
            "a tie group wider than pageSize did not trip the guard -- the page-full "
            "condition has been narrowed too far and rows can now be walked past silently")

        # The walk got past the tie rather than spinning on it: the row after the tie landed.
        self.assertIn(t2, [r["ts_us"] for r in rows],
                      "the walk never advanced beyond the tie group")

        # And the documented loss really is a loss. SPEC §7.4 accepts that rows sharing the
        # tie instant beyond one page are missed; asserting it keeps the tradeoff honest
        # instead of letting a future change quietly turn it into silent partial data.
        at_tie = len([r for r in rows if r["ts_us"] == tie])
        self.assertLess(at_tie, PAGE + 2)
        self.assertGreaterEqual(at_tie, PAGE)

    def test_resuming_a_half_walked_gap_rewrites_nothing(self):
        """A restart mid-outage re-walks from the persisted cursor, and the cursor sits *on*
        the last row it wrote -- so the resumed walk re-fetches rows already on disk. The
        per-gap dedupe set is seeded from the corpus for exactly this reason; the writer's
        live dedupe window is pruned by event time and holds nothing for a 3-day-old range."""
        cfg = self._cfg()
        cfg["dedupe"]["window_s"] = 60          # far narrower than the 3-day outage
        w = self._writer(cfg)
        data = self._dataset(28)

        # First pass: stop it early by only advertising the first third of the outage.
        self._heal(cfg, w, span_us=_DAY)
        partial = len(self._rows_on_disk())
        self.assertGreater(partial, 0, "the first pass wrote nothing")
        self.assertLess(partial, len(data), "the first pass consumed the whole dataset")

        # Second pass over the FULL outage, overlapping everything the first pass wrote.
        self.srv.pages = []
        self._heal(cfg, w)

        rows = self._rows_on_disk()
        ids = [r["row_id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)),
                         "the resumed walk re-appended rows already on disk")
        self.assertEqual(len(rows), len(data),
                         "expected %d rows after the resumed walk, found %d"
                         % (len(data), len(rows)))


if __name__ == "__main__":
    unittest.main()
