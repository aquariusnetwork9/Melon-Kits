"""Tests for writer.py — the crash-safety core.

Covers SPEC §4.3 (three dedupe tiers), §5.1-§5.5 (partitioning, rotation, gzip verify,
startup repair, durability), §7.1/§7.7/§7.9 (gap queue, pass 2, lookback clamp), §8.5
(proxy-log namespace) and §10 (state ordering guarantee).

Every test gets its own temp directory and cleans it up. Every test also asserts that no
chat text and no component text ever reached a log record (SPEC §11.4) — the sentinels
below are deliberately unmistakable so a leak cannot pass unnoticed. No network, no real
2b2t coordinates, no real chat text.
"""
from __future__ import annotations

import glob
import gzip
import json
import logging
import os
import shutil
import tempfile
import unittest

import record
import statefile
import tsutil
import writer

# Obviously-synthetic content. If either string ever shows up in a log line or in
# snapshot(), that is a privacy defect (SPEC §11.4) and the tests must fail.
CHAT = "hello-sentinel-zzz"
COMPONENT = '{"text":"","extra":[{"text":"gg-component-sentinel-zzz"}]}'
UUID = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
NAME = "SyntheticPlayer"

_S = 1_000_000
_DAY = 86_400 * _S


class _Clock(object):
    """Callable frozen clock, substituted for tsutil.now_us."""

    def __init__(self, us):
        self.us = int(us)

    def __call__(self):
        return self.us

    def advance(self, us):
        self.us += int(us)


class _Capture(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self)
        self.lines = []

    def emit(self, rec):
        try:
            self.lines.append(rec.getMessage())
        except Exception:              # pragma: no cover - a formatting bug is still a leak
            self.lines.append("<unformattable log record>")

    def text(self):
        return "\n".join(self.lines)


class WriterCase(unittest.TestCase):
    # ------------------------------------------------------------------ fixture

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="chatcol-writer-")
        self.state_path = os.path.join(self.dir, "state.json")
        self.log = logging.getLogger("test.writer." + self.id())
        self.log.setLevel(logging.DEBUG)
        self.log.propagate = False
        self.cap = _Capture()
        self.log.handlers = [self.cap]

        # 2026-07-25T12:00:00Z, mid-day so a +12 h advance crosses UTC midnight.
        self.base = tsutil.parse_ts("2026-07-25T12:00:00Z")
        self.clock = _Clock(self.base)
        self._real_now = tsutil.now_us
        tsutil.now_us = self.clock

        self._seq = 0
        self._writers = []

        self.addCleanup(self._teardown)

    def _teardown(self):
        tsutil.now_us = self._real_now
        for w in self._writers:
            try:
                w.close()
            except Exception:
                pass
        leak = self.cap.text()
        self.assertNotIn(CHAT, leak, "chat text leaked into a log record (SPEC §11.4)")
        self.assertNotIn("gg-component-sentinel-zzz", leak,
                         "component text leaked into a log record (SPEC §11.4)")
        self.log.handlers = []
        shutil.rmtree(self.dir, ignore_errors=True)

    # -------------------------------------------------------------- helpers

    def cfg(self, **over):
        conf = {
            "data_dir": self.dir,
            "state_path": self.state_path,
            "storage": {"store_component": True, "fsync_interval_ms": 2000,
                        "gzip_delay_s": 10 ** 9, "gzip_level": 6, "max_file_bytes": 0},
            "dedupe": {"window_s": 21600, "max_keys": 400000,
                       "rehydrate_max_bytes": 268435456},
            "backfill": {"max_lookback_days": 7, "second_pass_delay_s": 5400},
            "ops": {"alert_command": None},
        }
        for key, val in over.items():
            if isinstance(val, dict) and isinstance(conf.get(key), dict):
                conf[key].update(val)
            else:
                conf[key] = val
        return conf

    def new_writer(self, state=None, **over):
        if state is None:
            state = statefile.new_state(self.clock())
        w = writer.Writer(self.cfg(**over), state, self.log)
        self._writers.append(w)
        return w

    def next_seq(self):
        self._seq += 1
        return self._seq

    def sse(self, ts_us, chat=CHAT, name=NAME, uuid=UUID, ingest_us=None,
            component=COMPONENT):
        return record.from_sse(
            {"time": tsutil.fmt_ts(ts_us), "playerName": name, "playerUuid": uuid,
             "chat": chat, "component": component},
            seq=self.next_seq(), batch="c1",
            ingest_us=self.clock() if ingest_us is None else ingest_us)

    def window(self, ts_us, chat=CHAT, name=NAME, uuid=UUID, ingest_us=None):
        return record.from_window(
            {"time": tsutil.fmt_ts(ts_us), "playerName": name, "uuid": uuid, "chat": chat},
            seq=self.next_seq(), batch="g000001",
            ingest_us=self.clock() if ingest_us is None else ingest_us)

    def logrow(self, ts_us, chat=CHAT, name=NAME, ingest_us=None):
        return record.from_logline(
            ts_us, name, chat, seq=self.next_seq(), batch="latest.log@0",
            ingest_us=self.clock() if ingest_us is None else ingest_us)

    def read_state(self):
        with open(self.state_path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))

    def day(self, us=None):
        return tsutil.utc_date(self.clock() if us is None else us)

    def path_for(self, date):
        return os.path.join(self.dir, "chat-%s.jsonl" % date)

    def read_bytes(self, path):
        with open(path, "rb") as f:
            return f.read()

    def rows_in(self, path):
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rb") as f:
            return [json.loads(ln.decode("utf-8")) for ln in f if ln.strip()]

    def corpus_max_ts(self):
        """max ts_us over every sse/rest-backfill row durable on disk."""
        best = 0
        for path in sorted(glob.glob(os.path.join(self.dir, "chat-*.jsonl"))
                           + glob.glob(os.path.join(self.dir, "chat-*.jsonl.gz"))):
            for row in self.rows_in(path):
                if row.get("src") in (record.SRC_SSE, record.SRC_BACKFILL):
                    best = max(best, int(row["ts_us"]))
        return best

    # ============================================================ §10 ordering

    def test_append_fsync_state_ordering(self):
        """SPEC §10: state.json never references data that has not been fsynced."""
        initial_hwm = self.clock()
        state = statefile.new_state(initial_hwm)
        holder = {}
        calls = []
        real_save = statefile.save_state

        def spy(path, obj):
            w = holder.get("w")
            if w is not None:
                # Everything the in-memory state depends on is already durable.
                self.assertGreaterEqual(w._durable_bytes, w._state_dep_offset)
                self.assertEqual(w._durable_bytes, w._written_bytes)
            # And the persisted hwm never runs ahead of the bytes on disk.
            self.assertLessEqual(int(obj["hwm_us"]),
                                 max(initial_hwm, self.corpus_max_ts()))
            calls.append(int(obj["hwm_us"]))
            real_save(path, obj)

        statefile.save_state = spy
        self.addCleanup(setattr, statefile, "save_state", real_save)

        # fsync_interval_ms is set absurdly high so nothing is fsynced implicitly:
        # only the mandatory pre-persist fsync can make bytes durable.
        w = self.new_writer(state, storage={"fsync_interval_ms": 3_600_000})
        holder["w"] = w

        ts = self.base + 5 * _S
        self.assertTrue(w.put_record(self.sse(ts)))

        # Bytes are written but not yet fsynced...
        self.assertGreater(w._written_bytes, 0)
        self.assertLess(w._durable_bytes, w._written_bytes)
        # ...so the state on disk must NOT reference them yet.
        self.assertEqual(int(self.read_state()["hwm_us"]), initial_hwm)
        self.assertEqual(w.hwm_us(), ts)          # in-memory only, per §10's ordering block

        self.clock.advance(11 * _S)
        w.tick()

        self.assertEqual(w._durable_bytes, w._written_bytes)
        self.assertEqual(os.path.getsize(self.path_for(self.day())), w._written_bytes)
        self.assertEqual(int(self.read_state()["hwm_us"]), ts)
        self.assertGreaterEqual(len(calls), 2)

        # close() is a final fsync + persist, and is idempotent.
        w.close()
        w.close()
        self.assertEqual(int(self.read_state()["hwm_us"]), ts)
        self.assertRaises(RuntimeError, w.put_record, self.sse(ts + _S))

    # ========================================================= §5.4 repair

    def _seed(self, good_rows, garbage):
        """Write `good_rows` encoded records plus raw `garbage` into today's file."""
        path = self.path_for(self.day())
        good = b"".join(record.encode(r) for r in good_rows)
        with open(path, "wb") as f:
            f.write(good + garbage)
        return path, good

    def test_repair_truncated_mid_record(self):
        """SPEC §5.4: a torn final line is truncated back to the last complete record."""
        rows = [self.sse(self.base + i * _S) for i in range(3)]
        torn = record.encode(rows[0])[:40]           # a hard kill mid-write
        path, good = self._seed(rows, torn)

        w = self.new_writer()
        with open(path, "rb") as f:
            self.assertEqual(f.read(), good)
        self.assertIn("repaired_bytes=%d" % len(torn), self.cap.text())
        self.assertEqual(w.snapshot()["storage"]["rows_today"], 3)

        # ...and the file is still appendable and still all-JSON afterwards.
        self.assertTrue(w.put_record(self.sse(self.base + 99 * _S)))
        self.assertEqual(len(self.rows_in(path)), 4)

    def test_repair_truncated_mid_utf8_sequence(self):
        """A kill inside a multi-byte UTF-8 sequence, with no trailing newline."""
        rows = [self.sse(self.base + i * _S) for i in range(2)]
        torn = b'{"chat":"\xe4\xb8'
        path, good = self._seed(rows, torn)
        w = self.new_writer()
        with open(path, "rb") as f:
            self.assertEqual(f.read(), good)
        self.assertEqual(w.snapshot()["storage"]["repaired_bytes"], len(torn))

    def test_repair_complete_line_with_invalid_utf8(self):
        """A full line that is not decodable UTF-8 is removed by the JSON/decode step."""
        rows = [self.sse(self.base + i * _S) for i in range(2)]
        bad = b'{"chat":"\xe4\xb8"}\n'
        path, good = self._seed(rows, bad)
        w = self.new_writer()
        with open(path, "rb") as f:
            self.assertEqual(f.read(), good)
        self.assertEqual(len(self.rows_in(path)), 2)
        self.assertEqual(w.snapshot()["storage"]["repaired_bytes"], len(bad))

    def test_repair_trailing_partial_line_and_bad_lines(self):
        """Several bad trailing lines in one go; the repair loop runs up to 8 passes."""
        rows = [self.sse(self.base + i * _S) for i in range(4)]
        garbage = b"garbage-not-json\n" + b'{"partial":'
        path, good = self._seed(rows, garbage)

        w = self.new_writer()
        with open(path, "rb") as f:
            self.assertEqual(f.read(), good)
        self.assertEqual(len(self.rows_in(path)), 4)
        self.assertEqual(w.snapshot()["storage"]["repaired_bytes"], len(garbage))
        # An empty file is left alone rather than "repaired".
        self.assertNotIn("repaired_bytes=0", self.cap.text())

    def test_repair_only_ever_removes_the_tail(self):
        """The one truncation site may only remove bytes AFTER the last valid record."""
        rows = [self.sse(self.base + i * _S) for i in range(5)]
        path, good = self._seed(rows, b"")
        before = self.read_bytes(path)
        self.new_writer()
        self.assertEqual(self.read_bytes(path), before)
        self.assertEqual(len(good), os.path.getsize(path))

    # ================================================ §5.1/§5.2 rotation

    def test_rotation_partitions_by_ingest_date_not_event_date(self):
        """SPEC §5.1: the file is chosen by the UTC date of ingest_ts. A backfilled row for
        an old event date lands in TODAY's file — reopening a closed .gz is the thing the
        design exists to avoid, so this must NOT be 'fixed'."""
        day1 = self.day()
        w = self.new_writer()
        self.assertTrue(w.put_record(self.sse(self.base)))
        self.assertEqual(w.snapshot()["storage"]["active_file"], "chat-%s.jsonl" % day1)

        self.clock.advance(13 * 3600 * _S)            # cross UTC midnight
        day2 = self.day()
        self.assertNotEqual(day1, day2)

        self.assertTrue(w.put_record(self.sse(self.clock())))
        st = self.read_state()
        self.assertEqual(st["active_file"], "chat-%s.jsonl" % day2)
        self.assertEqual(st["pending_gzip"], ["chat-%s.jsonl" % day1])
        self.assertEqual(len(self.rows_in(self.path_for(day1))), 1)
        self.assertEqual(len(self.rows_in(self.path_for(day2))), 1)

        # A backfilled row whose EVENT time is three days old, ingested now.
        old_event = self.base - 3 * _DAY
        self.assertTrue(w.put_record(self.window(old_event)))
        self.assertEqual(len(self.rows_in(self.path_for(day1))), 1)
        rows2 = self.rows_in(self.path_for(day2))
        self.assertEqual(len(rows2), 2)
        self.assertEqual(rows2[-1]["ts_us"], old_event)
        self.assertEqual(rows2[-1]["src"], record.SRC_BACKFILL)
        # ...and an old event never drags the high-water mark backwards.
        self.assertGreater(w.hwm_us(), old_event)

        # Rotation also fires on the status tick with no traffic at all (§5.2).
        self.clock.advance(_DAY)
        day3 = self.day()
        w.tick()
        st = self.read_state()
        self.assertEqual(st["active_file"], "chat-%s.jsonl" % day3)
        self.assertEqual(st["pending_gzip"],
                         ["chat-%s.jsonl" % day1, "chat-%s.jsonl" % day2])

    # ==================================================== §5.3 gzip verify

    def _make_closed_day(self, gzip_delay_s=0, rows=4):
        """Write `rows` records, then cross midnight so the day file closes and queues."""
        w = self.new_writer(storage={"gzip_delay_s": gzip_delay_s})
        day1 = self.day()
        for i in range(rows):
            self.assertTrue(w.put_record(self.sse(self.base + i * _S)))
        plain = self.path_for(day1)
        original = self.read_bytes(plain)
        self.clock.advance(13 * 3600 * _S)
        w.put_record(self.sse(self.clock()))          # triggers the roll
        self.assertIn("chat-%s.jsonl" % day1, self.read_state()["pending_gzip"])
        return w, day1, plain, original

    def test_gzip_verify_then_replace(self):
        """SPEC §5.3: compress to .gz.tmp, verify by streaming it back, then replace."""
        w, day1, plain, original = self._make_closed_day()
        w.tick()

        gz = plain + ".gz"
        self.assertTrue(os.path.exists(gz))
        self.assertFalse(os.path.exists(plain))
        self.assertFalse(os.path.exists(plain + ".gz.tmp"))
        self.assertEqual(self.read_state()["pending_gzip"], [])
        with gzip.open(gz, "rb") as f:
            self.assertEqual(f.read(), original)

    def test_gzip_verify_catches_truncated_stream(self):
        """An injected corruption must be caught, and must leave the plaintext alone."""
        w, day1, plain, original = self._make_closed_day()
        real_write = w._gzip_write
        calls = []

        def corrupting_write(src, tmp):
            lines, total = real_write(src, tmp)
            calls.append(src)
            with open(tmp, "r+b") as f:               # lose the gzip trailer
                f.truncate(max(0, os.path.getsize(tmp) - 40))
            return lines, total

        w._gzip_write = corrupting_write
        w.tick()

        self.assertEqual(self.read_bytes(plain), original)     # plaintext intact
        self.assertFalse(os.path.exists(plain + ".gz"))
        self.assertFalse(os.path.exists(plain + ".gz.tmp"))
        self.assertIn("chat-%s.jsonl" % day1, self.read_state()["pending_gzip"])
        self.assertEqual(w.snapshot()["counters"]["gzip_failures"], 1)
        self.assertIn("gzip verify", self.cap.text())

        w.tick()                                      # and no automatic retry
        self.assertEqual(len(calls), 1)

    def test_gzip_verify_catches_line_count_mismatch(self):
        """A structurally valid .gz that lost rows must fail verification too."""
        w, day1, plain, original = self._make_closed_day()
        real_write = w._gzip_write

        def lossy_write(src, tmp):
            lines, total = real_write(src, tmp)       # true source counts
            with open(src, "rb") as fin:
                first = fin.readline()
            with open(tmp, "wb") as raw:              # valid gzip, wrong content
                out = gzip.GzipFile(filename="", mode="wb", fileobj=raw)
                out.write(first)
                out.close()
            return lines, total

        w._gzip_write = lossy_write
        w.tick()

        self.assertEqual(self.read_bytes(plain), original)
        self.assertFalse(os.path.exists(plain + ".gz"))
        self.assertFalse(os.path.exists(plain + ".gz.tmp"))
        self.assertEqual(w.snapshot()["counters"]["gzip_failures"], 1)
        self.assertIn("gzip verify mismatch", self.cap.text())

    def test_startup_removes_stray_gz_tmp(self):
        """SPEC §5.3: every *.gz.tmp is deleted on startup."""
        stray = os.path.join(self.dir, "chat-2026-07-01.jsonl.gz.tmp")
        with open(stray, "wb") as f:
            f.write(b"garbage")
        self.new_writer()
        self.assertFalse(os.path.exists(stray))

    # ===================================================== §4.3a dedupe

    def test_dedupe_drops_exact_repeat_keeps_near_miss(self):
        w = self.new_writer()
        rec = self.sse(self.base)
        self.assertTrue(w.put_record(rec))
        self.assertFalse(w.put_record(dict(rec)))              # byte-identical repeat
        self.assertFalse(w.put_record(self.sse(self.base)))    # same identity, new seq

        self.assertTrue(w.put_record(self.sse(self.base, chat="gg")))       # other text
        self.assertTrue(w.put_record(self.sse(self.base + 1)))             # +1 us
        self.assertTrue(w.put_record(self.sse(self.base, name="Other",
                                              uuid=None)))                 # other player

        self.assertEqual(w.snapshot()["counters"]["dupes_dropped"], 2)
        self.assertEqual(len(self.rows_in(self.path_for(self.day()))), 4)

    def test_dedupe_prunes_by_event_time(self):
        """SPEC §4.3a: buckets are pruned against EVENT time, not arrival time."""
        w = self.new_writer(dedupe={"window_s": 3600})
        old = self.sse(self.base)
        self.assertTrue(w.put_record(old))
        self.assertFalse(w.put_record(dict(old)))

        # A row two hours later (in event time) pushes the window past `old`.
        self.assertTrue(w.put_record(self.sse(self.base + 2 * 3600 * _S)))
        self.assertEqual(w.snapshot()["dedupe"]["keys"], 1)
        # `old` is outside the window now, so it is no longer deduplicated. This is the
        # documented cost of a bounded window and is why the gap-local set of §4.3c exists.
        self.assertTrue(w.put_record(dict(old)))

    def test_dedupe_max_keys_eviction_counts(self):
        """SPEC §4.3a: the hard cap evicts the oldest bucket and counts the eviction."""
        w = self.new_writer(dedupe={"window_s": 21600, "max_keys": 2})
        r1 = self.sse(self.base)
        r2 = self.sse(self.base + 60 * _S)
        r3 = self.sse(self.base + 120 * _S)
        for rec in (r1, r2, r3):
            self.assertTrue(w.put_record(rec))

        self.assertEqual(w.snapshot()["counters"]["dedupe_evictions"], 1)
        self.assertEqual(w.snapshot()["dedupe"]["evictions"], 1)
        self.assertEqual(w.snapshot()["dedupe"]["keys"], 2)
        self.assertTrue(w.put_record(dict(r1)))        # oldest bucket was evicted
        self.assertFalse(w.put_record(dict(r3)))       # newest bucket still guards

    # ============================================ §4.3b rehydration anchor

    def test_rehydration_anchored_on_hwm_catches_boundary_row(self):
        """SPEC §4.3b: rehydration is anchored on state['hwm_us'], never on now — which is
        exactly what makes the inclusive-bound boundary row dedupe after long downtime."""
        w1 = self.new_writer()
        rows = [self.window(self.base + i * _S) for i in range(3)]
        for rec in rows:
            self.assertTrue(w1.put_record(rec))
        boundary = rows[-1]
        w1.close()

        state = self.read_state()
        self.assertEqual(int(state["hwm_us"]), boundary["ts_us"])

        self.clock.advance(3 * _DAY)                   # simulated long downtime
        w2 = self.new_writer(state)

        self.assertEqual(w2.hwm_us(), boundary["ts_us"])
        self.assertEqual(w2.snapshot()["dedupe"]["keys"], 3)
        # The re-delivered boundary row of the next window query is dropped...
        self.assertFalse(w2.put_record(dict(boundary)))
        self.assertFalse(w2.put_record(self.window(boundary["ts_us"])))
        # ...while genuinely new rows still get through.
        self.assertTrue(w2.put_record(self.sse(self.clock())))
        self.assertEqual(len(self.rows_in(self.path_for(self.day()))), 1)

    def test_rehydration_reads_gzipped_days(self):
        """The dedupe log survives compression: rehydration reads chat-*.jsonl.gz too."""
        w1 = self.new_writer(storage={"gzip_delay_s": 0})
        day1 = self.day()
        boundary = self.window(self.base)
        self.assertTrue(w1.put_record(boundary))
        w1.close()

        self.clock.advance(2 * _DAY)
        w2 = self.new_writer(self.read_state(), storage={"gzip_delay_s": 0})
        w2.tick()
        w2.close()
        self.assertFalse(os.path.exists(self.path_for(day1)))
        self.assertTrue(os.path.exists(self.path_for(day1) + ".gz"))

        w3 = self.new_writer(self.read_state(), storage={"gzip_delay_s": 0})
        self.assertGreaterEqual(w3.snapshot()["dedupe"]["keys"], 1)
        self.assertFalse(w3.put_record(dict(boundary)))

    def test_same_day_restart_rehydrates_from_the_open_active_file(self):
        """A supervisor restart mid-day: the active file is reopened for append AND read
        backwards for dedupe keys at the same time (SPEC §4.3b, §11.2)."""
        w1 = self.new_writer()
        rows = [self.sse(self.base + i * _S) for i in range(3)]
        for rec in rows:
            self.assertTrue(w1.put_record(rec))
        w1.close()
        path = self.path_for(self.day())
        before = self.read_bytes(path)

        self.clock.advance(30 * _S)
        w2 = self.new_writer(self.read_state())

        self.assertEqual(w2.snapshot()["storage"]["active_file"],
                         "chat-%s.jsonl" % self.day())
        self.assertEqual(w2.snapshot()["storage"]["rows_today"], 3)
        self.assertEqual(w2.snapshot()["dedupe"]["keys"], 3)
        self.assertEqual(w2._written_bytes, len(before))
        for rec in rows:
            self.assertFalse(w2.put_record(dict(rec)))
        self.assertEqual(self.read_bytes(path), before)          # nothing re-appended

        self.assertTrue(w2.put_record(self.sse(self.clock())))
        self.assertEqual(len(self.rows_in(path)), 4)
        self.assertTrue(self.read_bytes(path).startswith(before))

    # ==================================================== §8.5 proxy-log

    def test_proxy_log_row_does_not_move_hwm_and_has_its_own_namespace(self):
        """SPEC §8.5: log rows never advance hwm_us and never share the dedupe namespace."""
        w = self.new_writer()
        hwm0 = w.hwm_us()

        far_future = self.base + 3600 * _S
        lrow = self.logrow(far_future)
        self.assertTrue(w.put_record(lrow))
        self.assertEqual(w.hwm_us(), hwm0)                    # unmoved
        self.assertEqual(w.snapshot()["counters"]["rows_log"], 1)
        self.assertTrue(lrow["row_id"].startswith("L"))
        self.assertIsNone(lrow["player_uuid"])
        self.assertEqual(lrow["precision"], "s")

        self.assertFalse(w.put_record(dict(lrow)))            # own namespace dedupes

        # An sse row for the same second/name/text is a DIFFERENT row and must be kept.
        twin = self.sse(far_future, uuid=None, component=None)
        self.assertTrue(w.put_record(twin))
        self.assertEqual(w.hwm_us(), far_future)              # sse rows do advance it
        rows = self.rows_in(self.path_for(self.day()))
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["src"] for r in rows], [record.SRC_LOG, record.SRC_SSE])

    # =================================================== §7.1/§7.9 gaps

    def test_enqueue_gap_skips_zero_length(self):
        """SPEC §6.5: startDate == endDate returns HTTP 400, not an empty result."""
        w = self.new_writer()
        now = self.clock()
        self.assertIsNone(w.enqueue_gap(now, now))
        self.assertIsNone(w.enqueue_gap(now, now - _S))
        self.assertEqual(self.read_state()["gaps"], [])

    def test_enqueue_gap_clamps_lookback(self):
        """SPEC §7.9: a corrupt hwm must never trigger a full-history crawl."""
        w = self.new_writer()
        now = self.clock()
        gap_id = w.enqueue_gap(now - 30 * _DAY, now)
        self.assertIsNotNone(gap_id)

        # Durable BEFORE returning (SPEC §6.5) — read it straight off disk.
        gaps = self.read_state()["gaps"]
        self.assertEqual(len(gaps), 1)
        gap = gaps[0]
        self.assertEqual(gap["gap_id"], gap_id)
        self.assertTrue(gap["clamped"])
        self.assertEqual(gap["start_us"], now - 7 * _DAY)
        self.assertEqual(gap["cursor_us"], now - 7 * _DAY)
        self.assertEqual(gap["status"], "pending")
        self.assertEqual(gap["pass"], 1)
        self.assertEqual(self.read_state()["counters"]["clamped_gaps"], 1)
        self.assertIn("clamp fired", self.cap.text())

        # An entirely ancient window collapses to nothing once clamped, so it is skipped
        # rather than sent as a guaranteed 400.
        self.assertIsNone(w.enqueue_gap(now - 30 * _DAY, now - 29 * _DAY))
        self.assertEqual(self.read_state()["counters"]["clamped_gaps"], 2)
        self.assertEqual(len(self.read_state()["gaps"]), 1)

    def test_gap_lifecycle_queues_confirming_second_pass(self):
        """SPEC §7.7: pass 2 re-walks the identical range at end_us + second_pass_delay_s."""
        w = self.new_writer()
        start = self.clock() - 900 * _S
        end = self.clock()
        gap_id = w.enqueue_gap(start, end)

        claimed = w.claim_gap()
        self.assertEqual(claimed["gap_id"], gap_id)
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["attempts"], 1)
        self.assertIsNone(w.claim_gap())               # nothing else is runnable

        w.advance_gap(gap_id, start + 100 * _S, 7)
        on_disk = self.read_state()["gaps"][0]
        self.assertEqual(on_disk["cursor_us"], start + 100 * _S)
        self.assertEqual(on_disk["rows"], 7)

        w.finish_gap(gap_id, "done")
        gaps = self.read_state()["gaps"]
        self.assertEqual(len(gaps), 2)
        p2 = gaps[1]
        self.assertEqual(p2["pass"], 2)
        self.assertEqual((p2["start_us"], p2["end_us"]), (start, end))
        self.assertEqual(p2["cursor_us"], start)
        self.assertEqual(p2["not_before_us"], end + 5400 * _S)
        self.assertIsNone(w.claim_gap())               # not yet due

        self.clock.advance(5401 * _S)
        claimed2 = w.claim_gap()
        self.assertEqual(claimed2["gap_id"], p2["gap_id"])
        w.advance_gap(p2["gap_id"], end, 4)
        self.assertEqual(self.read_state()["counters"]["rows_added_pass2"], 4)

        w.finish_gap(p2["gap_id"], "done")
        self.assertEqual(len(self.read_state()["gaps"]), 2)   # no pass 3, ever

        # An errored gap is terminal: no confirming pass, and it stays visible to health.
        gid3 = w.enqueue_gap(self.clock() - 60 * _S, self.clock())
        w.claim_gap()
        w.finish_gap(gid3, "error")
        self.assertEqual(len(self.read_state()["gaps"]), 3)
        self.assertEqual(w.snapshot()["backfill"]["gaps_error"], 1)

    def test_audit_gap_rows_are_counted_separately(self):
        """SPEC §7.8: the weekly audit reports its own rows_added."""
        w = self.new_writer()
        gid = w.enqueue_gap(self.clock() - 3 * _DAY, self.clock() - _DAY, tag="audit")
        w.claim_gap()
        w.advance_gap(gid, self.clock() - 2 * _DAY, 5)
        self.assertEqual(self.read_state()["counters"]["audit_rows_added"], 5)
        self.assertEqual(self.read_state()["counters"]["rows_added_pass2"], 0)

    # ===================================================== misc / privacy

    def test_snapshot_has_facts_only(self):
        """SPEC §11.3/§11.4: counters and storage facts, never content."""
        w = self.new_writer()
        w.put_record(self.sse(self.base))
        w.bump("connections", 3)
        snap = w.snapshot()
        blob = json.dumps(snap)

        self.assertNotIn(CHAT, blob)
        self.assertNotIn("gg-component-sentinel-zzz", blob)
        self.assertNotIn(NAME, blob)
        self.assertNotIn(UUID, blob)

        self.assertEqual(snap["storage"]["active_file"], "chat-%s.jsonl" % self.day())
        self.assertEqual(snap["storage"]["rows_today"], 1)
        self.assertEqual(snap["storage"]["pending_gzip"], 0)
        self.assertGreater(snap["storage"]["active_bytes"], 0)
        self.assertGreaterEqual(snap["storage"]["disk_free_bytes"], 0)
        self.assertEqual(snap["connections"], 3)
        self.assertEqual(snap["counters"]["rows_sse"], 1)
        self.assertEqual(snap["hwm_us"], self.base)
        self.assertTrue(snap["hwm_ts"].endswith("Z"))

    def test_counters_are_persisted_by_close(self):
        """SPEC §10: counters ride along with the bounded state persist, not per row."""
        w = self.new_writer()
        w.put_record(self.sse(self.base))
        w.bump("connections", 2)
        w.bump("parse_errors")
        self.assertEqual(self.read_state()["counters"]["rows_sse"], 0)   # not yet durable
        w.close()
        st = self.read_state()
        self.assertEqual(st["counters"]["rows_sse"], 1)
        self.assertEqual(st["counters"]["parse_errors"], 1)
        self.assertEqual(st["connections"], 2)

    def test_store_component_false_drops_component_from_disk(self):
        """SPEC §9: the operator may trade fidelity for disk; the key stays present."""
        w = self.new_writer(storage={"store_component": False})
        rec = self.sse(self.base)
        self.assertTrue(w.put_record(rec))
        rows = self.rows_in(self.path_for(self.day()))
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["component"])
        self.assertEqual(rec["component"], COMPONENT)      # producer's dict untouched
        self.assertEqual(list(rows[0].keys()), list(record.KEYS))

    def test_seq_is_monotonic_and_persisted(self):
        w = self.new_writer()
        first = w.next_seq()
        second = w.next_seq()
        self.assertEqual(second, first + 1)
        w.close()
        self.assertEqual(int(self.read_state()["seq"]), second)
        w2 = self.new_writer(self.read_state())
        self.assertEqual(w2.next_seq(), second + 1)

    def test_max_file_bytes_failsafe_rolls_within_the_day(self):
        """SPEC §5.2: the optional size failsafe rolls to chat-YYYY-MM-DD.NN.jsonl."""
        w = self.new_writer(storage={"max_file_bytes": 300, "gzip_delay_s": 10 ** 9})
        for i in range(6):
            self.assertTrue(w.put_record(self.sse(self.base + i * _S)))
        names = sorted(os.path.basename(p) for p in
                       glob.glob(os.path.join(self.dir, "chat-*.jsonl")))
        self.assertGreater(len(names), 1)
        self.assertIn("chat-%s.01.jsonl" % self.day(), names)
        total = sum(len(self.rows_in(os.path.join(self.dir, n))) for n in names)
        self.assertEqual(total, 6)
        self.assertTrue(self.read_state()["active_file"].endswith(".jsonl"))


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
