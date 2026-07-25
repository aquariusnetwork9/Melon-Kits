"""Tests for statefile.py and singlelock.py -- both are SPEC §10.

No network, no real chat text, no coordinates anywhere in this file.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import singlelock  # noqa: E402
import statefile  # noqa: E402

NOW_US = 1785012433404045


class _Capture(logging.Handler):
    """Collects records so tests can assert on level without touching stderr."""

    def __init__(self) -> None:
        logging.Handler.__init__(self)
        self.records = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self, level: int) -> list:
        return [r.getMessage() for r in self.records if r.levelno == level]


def _logger(name: str) -> tuple:
    log = logging.getLogger(name)
    log.handlers = []
    log.propagate = False
    log.setLevel(logging.DEBUG)
    cap = _Capture()
    log.addHandler(cap)
    return log, cap


class StateFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="chatcol-state-")
        self.path = os.path.join(self.tmp, "state.json")
        self.log, self.cap = _logger("test.statefile." + str(id(self)))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- shape -------------------------------------------------------------

    def test_new_state_shape_matches_spec_10(self) -> None:
        st = statefile.new_state(NOW_US)
        self.assertEqual(
            sorted(st),
            sorted(
                [
                    "version",
                    "hwm_us",
                    "seq",
                    # Documented addition to the SPEC §10 shape: the durable seq
                    # RESERVATION, without which a hard kill re-issues seq values that
                    # are already stamped on durable rows (SPEC §2 key 11).
                    "seq_reserved",
                    "connections",
                    "active_file",
                    "pending_gzip",
                    "gaps",
                    "log_tail",
                    "audit_last_run_us",
                    "counters",
                ]
            ),
        )
        self.assertEqual(st["version"], statefile.STATE_VERSION)
        self.assertEqual(st["version"], 1)
        self.assertEqual(st["hwm_us"], NOW_US)
        self.assertIsInstance(st["hwm_us"], int)
        self.assertEqual(st["seq"], 0)
        self.assertEqual(st["connections"], 0)
        self.assertIsNone(st["active_file"])
        self.assertEqual(st["pending_gzip"], [])
        self.assertEqual(st["gaps"], [])
        self.assertEqual(sorted(st["log_tail"]), ["dev", "ino", "offset", "path"])
        self.assertEqual(st["log_tail"]["offset"], 0)
        self.assertEqual(st["audit_last_run_us"], 0)

    def test_new_state_has_every_counter_key_at_zero(self) -> None:
        st = statefile.new_state(NOW_US)
        expected = [
            "rows_sse",
            "rows_backfill",
            "rows_log",
            "dupes_dropped",
            "parse_errors",
            "tie_skips",
            "clamped_gaps",
            "rows_added_pass2",
            "audit_rows_added",
        ]
        self.assertEqual(sorted(st["counters"]), sorted(expected))
        self.assertEqual(set(st["counters"].values()), {0})
        self.assertEqual(list(statefile.COUNTER_KEYS), expected)

    def test_new_state_returns_independent_objects(self) -> None:
        a = statefile.new_state(NOW_US)
        b = statefile.new_state(NOW_US)
        a["gaps"].append({"gap_id": "g000001"})
        a["counters"]["rows_sse"] = 5
        self.assertEqual(b["gaps"], [])
        self.assertEqual(b["counters"]["rows_sse"], 0)

    # -- round trip --------------------------------------------------------

    def test_round_trip(self) -> None:
        st = statefile.new_state(NOW_US)
        st["seq"] = 184223
        st["connections"] = 417
        st["active_file"] = "chat-2026-07-25.jsonl"
        st["pending_gzip"] = ["chat-2026-07-24.jsonl"]
        st["gaps"] = [
            {
                "gap_id": "g000417",
                "start_us": NOW_US - 120000000,
                "end_us": NOW_US,
                "pass": 1,
                "attempts": 0,
                "not_before_us": NOW_US,
                "cursor_us": NOW_US - 120000000,
                "rows": 0,
                "status": "pending",
            }
        ]
        st["counters"]["rows_sse"] = 12488
        statefile.save_state(self.path, st)

        loaded = statefile.load_state(self.path, self.log)
        self.assertEqual(loaded, st)
        self.assertEqual(self.cap.messages(logging.ERROR), [])
        self.assertEqual(self.cap.messages(logging.WARNING), [])

    def test_save_creates_missing_parent_directory(self) -> None:
        nested = os.path.join(self.tmp, "deep", "corpus", "state.json")
        statefile.save_state(nested, statefile.new_state(NOW_US))
        self.assertTrue(os.path.exists(nested))

    def test_atomic_write_leaves_no_tmp_behind(self) -> None:
        st = statefile.new_state(NOW_US)
        for i in range(3):
            st["seq"] = i
            statefile.save_state(self.path, st)
            self.assertFalse(
                os.path.exists(self.path + ".tmp"),
                "save_state must not leave a .tmp file behind",
            )
        leftovers = [n for n in os.listdir(self.tmp) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_first_save_creates_no_bak_second_rotates_previous(self) -> None:
        first = statefile.new_state(NOW_US)
        first["seq"] = 1
        statefile.save_state(self.path, first)
        self.assertFalse(os.path.exists(self.path + ".bak"))

        second = statefile.new_state(NOW_US)
        second["seq"] = 2
        statefile.save_state(self.path, second)
        self.assertTrue(os.path.exists(self.path + ".bak"))
        with open(self.path + ".bak", "rb") as f:
            rotated = json.loads(f.read().decode("utf-8"))
        self.assertEqual(rotated["seq"], 1)
        self.assertEqual(statefile.load_state(self.path, self.log)["seq"], 2)

    def test_unserialisable_state_raises_and_leaves_no_tmp(self) -> None:
        st = statefile.new_state(NOW_US)
        st["gaps"] = [object()]
        with self.assertRaises(statefile.StateError):
            statefile.save_state(self.path, st)
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        self.assertFalse(os.path.exists(self.path))

    # -- fallback ----------------------------------------------------------

    def _truncate_primary_mid_write(self) -> None:
        """Leave the primary looking like a write that died halfway."""
        with open(self.path, "rb") as f:
            raw = f.read()
        with open(self.path, "wb") as f:
            f.write(raw[: max(1, len(raw) // 2)])

    def test_bak_fallback_when_primary_truncated(self) -> None:
        good = statefile.new_state(NOW_US)
        good["seq"] = 11
        statefile.save_state(self.path, good)
        newer = statefile.new_state(NOW_US)
        newer["seq"] = 12
        statefile.save_state(self.path, newer)  # rotates `good` to .bak

        self._truncate_primary_mid_write()

        loaded = statefile.load_state(self.path, self.log)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["seq"], 11)
        errors = self.cap.messages(logging.ERROR)
        self.assertEqual(len(errors), 1)
        self.assertIn("falling back", errors[0])

    def test_returns_none_when_both_generations_are_corrupt(self) -> None:
        st = statefile.new_state(NOW_US)
        statefile.save_state(self.path, st)
        statefile.save_state(self.path, st)
        for target in (self.path, self.path + ".bak"):
            with open(target, "wb") as f:
                f.write(b'{"version": 1, "hwm')
        self.assertIsNone(statefile.load_state(self.path, self.log))
        self.assertEqual(len(self.cap.messages(logging.ERROR)), 1)

    def test_returns_none_when_nothing_exists(self) -> None:
        self.assertIsNone(statefile.load_state(self.path, self.log))
        errors = self.cap.messages(logging.ERROR)
        self.assertEqual(len(errors), 1)
        self.assertIn("first run", errors[0])

    def test_empty_file_is_not_usable(self) -> None:
        with open(self.path, "wb") as f:
            f.write(b"")
        self.assertIsNone(statefile.load_state(self.path, self.log))

    def test_non_object_json_is_not_usable(self) -> None:
        with open(self.path, "wb") as f:
            f.write(b"[1, 2, 3]")
        self.assertIsNone(statefile.load_state(self.path, self.log))

    # -- version and required fields ---------------------------------------

    def test_version_mismatch_raises(self) -> None:
        st = statefile.new_state(NOW_US)
        st["version"] = 2
        statefile.save_state(self.path, st)
        with self.assertRaises(statefile.StateError):
            statefile.load_state(self.path, self.log)

    def test_missing_version_raises(self) -> None:
        st = statefile.new_state(NOW_US)
        del st["version"]
        statefile.save_state(self.path, st)
        with self.assertRaises(statefile.StateError):
            statefile.load_state(self.path, self.log)

    def test_missing_hwm_us_raises_rather_than_being_invented(self) -> None:
        st = statefile.new_state(NOW_US)
        del st["hwm_us"]
        statefile.save_state(self.path, st)
        with self.assertRaises(statefile.StateError):
            statefile.load_state(self.path, self.log)

    def test_missing_counter_is_filled_and_reported(self) -> None:
        st = statefile.new_state(NOW_US)
        del st["counters"]["tie_skips"]
        del st["gaps"]
        st["counters"]["dedupe_evictions"] = 3  # writer-added extra, must survive
        statefile.save_state(self.path, st)

        loaded = statefile.load_state(self.path, self.log)
        self.assertEqual(loaded["counters"]["tie_skips"], 0)
        self.assertEqual(loaded["counters"]["dedupe_evictions"], 3)
        self.assertEqual(loaded["gaps"], [])
        warnings = self.cap.messages(logging.WARNING)
        self.assertEqual(len(warnings), 1)
        self.assertIn("tie_skips", warnings[0])


class SingleLockTest(unittest.TestCase):
    """SPEC §10, single-instance lock."""

    CHILD = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import singlelock\n"
        "try:\n"
        "    h = singlelock.acquire(sys.argv[2])\n"
        "    print(json.dumps({'acquired': True, 'pid': h.pid}))\n"
        "    h.release()\n"
        "except singlelock.AlreadyRunning as exc:\n"
        "    print(json.dumps({'acquired': False, 'pid': exc.pid}))\n"
    )

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="chatcol-lock-")
        self.path = os.path.join(self.tmp, "collector.lock")
        self.handles = []

    def tearDown(self) -> None:
        for handle in self.handles:
            handle.release()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _acquire(self) -> singlelock.LockHandle:
        handle = singlelock.acquire(self.path)
        self.handles.append(handle)
        return handle

    def _child_attempt(self) -> dict:
        """Second acquisition from a *separate process*.

        Re-locking from the same process can legitimately succeed on some
        platforms (a POSIX flock on a second fd of the same file is an upgrade,
        not contention), so the only honest test is a subprocess.
        """
        proc = subprocess.run(
            [sys.executable, "-c", self.CHILD, ROOT, self.path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(
            proc.returncode,
            0,
            "child failed: %s" % proc.stderr.decode("utf-8", "replace"),
        )
        return json.loads(proc.stdout.decode("ascii").strip())

    def test_acquires_when_file_does_not_exist(self) -> None:
        self.assertFalse(os.path.exists(self.path))
        handle = self._acquire()
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(handle.pid, os.getpid())
        self.assertEqual(handle.path, self.path)
        self.assertIsInstance(handle.fileno(), int)

    def test_writes_our_pid_for_the_operator(self) -> None:
        self._acquire()
        with open(self.path, "rb") as f:
            body = f.read()
        self.assertEqual(body.strip(), str(os.getpid()).encode("ascii"))

    def test_second_attempt_raises_already_running_with_pid(self) -> None:
        self._acquire()
        result = self._child_attempt()
        self.assertFalse(result["acquired"])
        self.assertEqual(result["pid"], os.getpid())

    def test_contended_attempt_does_not_truncate_the_lock_file(self) -> None:
        self._acquire()
        self._child_attempt()
        with open(self.path, "rb") as f:
            body = f.read()
        self.assertEqual(body.strip(), str(os.getpid()).encode("ascii"))

    def test_lock_is_reacquirable_after_release(self) -> None:
        handle = self._acquire()
        handle.release()
        handle.release()  # idempotent
        result = self._child_attempt()
        self.assertTrue(result["acquired"])
        self.assertNotEqual(result["pid"], os.getpid())

    def test_stale_lock_file_with_garbage_is_acquirable(self) -> None:
        with open(self.path, "wb") as f:
            f.write(b"not-a-pid\n")
        handle = self._acquire()
        self.assertEqual(handle.pid, os.getpid())

    def test_already_running_pid_is_none_when_illegible(self) -> None:
        exc = singlelock.AlreadyRunning()
        self.assertIsNone(exc.pid)
        self.assertIsInstance(exc, RuntimeError)

    def test_context_manager_releases(self) -> None:
        with singlelock.acquire(self.path) as handle:
            self.assertEqual(handle.pid, os.getpid())
        self.assertTrue(self._child_attempt()["acquired"])


if __name__ == "__main__":
    unittest.main()
