"""SPEC §13 non-negotiable test 1 — the kill-9 loop.

Drives synthetic records through a real ``Writer`` in a real child process, kills that
process without warning at a random moment, restarts, and asserts the corpus is intact.
Repeated many times, because the interesting failures are the ones that need a specific
instant to be killed at.

What each cycle asserts after the restart:

1. Every line of every corpus file parses as JSON and carries the canonical keys.
2. No ``row_id`` appears twice.
3. No ``seq`` appears twice. This is the invariant the seq *reservation* exists for: the
   writer makes ``seq + 256`` durable before issuing ``seq`` precisely so that a hard kill
   resumes past the reservation and leaves a gap in the numbering rather than re-issuing
   values already stamped on durable rows. A kill-9 loop is the only thing that can test it.
4. Every row the child was told was written is still on disk.

**What this proves, and what it does not.** ``SIGKILL`` (and ``TerminateProcess`` on
Windows, which is what ``Popen.kill()`` maps to) destroys the process but not the OS page
cache, so this harness covers the realistic failure — supervisor restart, OOM kill,
``systemctl kill`` — where every ``flush()``ed byte survives. It does **not** simulate
machine power loss, where only ``fsync``ed bytes survive; ``fsync`` runs at most every
``fsync_interval_ms`` by design, so a power cut can lose the last couple of seconds and
that is an accepted, documented tradeoff rather than a bug for this test to find.

Deliberate torn-line construction is not this test's job either — ``test_writer`` covers
tail repair with five deterministic cases. Here a torn tail happens only when a kill lands
inside a ``write()``, which is rare per cycle and near-certain across a long run.

Cycle count defaults low enough to sit in the normal suite. The full SPEC §13 run is::

    MELONKIT_KILLNINE_CYCLES=200 python -m unittest tests.test_killnine -v

Note the env prefix is *not* ``CHATCOL_``: unknown ``CHATCOL_*`` variables are a hard
startup failure by design, so a knob under that prefix would kill the child it configures.

No network. No real chat text, no real coordinates — every record is synthetic.
"""
from __future__ import annotations

import glob
import gzip
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

CHAT = "hello-killnine-sentinel-zzz"
COMPONENT = '{"text":"","extra":[{"text":"gg-killnine-sentinel-zzz"}]}'
UUID = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
NAME = "SyntheticPlayer"

DEFAULT_CYCLES = 15
READY_TIMEOUT_S = 20.0

# Kill window, measured from the moment the child confirms it is inside its write loop --
# not from spawn, because interpreter startup on Windows can exceed the whole window and
# every cycle would then kill a process that had not written anything yet. A test that
# passes because nothing happened is the failure mode to design against here.
KILL_MIN_S = 0.05
KILL_MAX_S = 0.35

CANONICAL_KEYS = frozenset((
    "ts", "ts_us", "player_uuid", "player_name", "chat", "component",
    "src", "precision", "row_id", "ingest_ts", "seq", "batch",
))

# The child. Kept as source rather than a module so it cannot be imported by accident and
# so the whole harness reads top to bottom in one file.
CHILD_SOURCE = r'''
import json, logging, os, sys, time

root, data_dir, state_path, ack_path, ready_path, base_ts = sys.argv[1:7]
sys.path.insert(0, root)

import record, statefile, tsutil, writer

CHAT = "hello-killnine-sentinel-zzz"
COMPONENT = '{"text":"","extra":[{"text":"gg-killnine-sentinel-zzz"}]}'
UUID = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
NAME = "SyntheticPlayer"

BURST = 20

log = logging.getLogger("killnine.child")
log.addHandler(logging.NullHandler())
log.propagate = False

cfg = {
    "data_dir": data_dir,
    "state_path": state_path,
    "storage": {"store_component": True, "fsync_interval_ms": 2000,
                "gzip_delay_s": 10 ** 9, "gzip_level": 6, "max_file_bytes": 0},
    "dedupe": {"window_s": 21600, "max_keys": 400000,
               "rehydrate_max_bytes": 268435456},
    "backfill": {"max_lookback_days": 7, "second_pass_delay_s": 5400},
    "ops": {"alert_command": None},
}

state = statefile.load_state(state_path, log)
if state is None:
    state = statefile.new_state(tsutil.now_us())

w = writer.Writer(cfg, state, log)
ack = open(ack_path, "a", encoding="utf-8")
base = int(base_ts)

for i in range(100000):
    ts = base + i * 1000
    rec = record.from_sse(
        {"time": tsutil.fmt_ts(ts), "playerName": NAME, "playerUuid": UUID,
         "chat": CHAT, "component": COMPONENT},
        seq=w.next_seq(), batch="k1", ingest_us=tsutil.now_us())
    if w.put_record(rec):
        # ORDERING: put_record has returned, so the row's bytes are flushed to the OS and
        # will outlive this process being killed. Only now is the row acknowledged. A kill
        # between the two loses the ack but keeps the row, which the parent permits --
        # the assertion is that acknowledged rows are on disk, not that every row on disk
        # was acknowledged.
        ack.write("%s %d\n" % (rec["row_id"], int(rec["seq"])))
        ack.flush()
    if i == 0:
        with open(ready_path, "w", encoding="utf-8") as fh:
            fh.write("ready\n")
            fh.flush()
    # Throttle in bursts, not per row. Windows' sleep granularity is ~15.6 ms, so a
    # per-row sleep of any size costs a full tick and holds the child to ~20 rows per
    # cycle -- below the 256-value seq reservation, which would mean no cycle ever
    # crosses a reservation boundary and the seq-reuse assertion never bites. Bursting
    # also raises the share of wall-clock actually spent inside write()/flush(), which
    # is where a kill has to land to leave a torn tail.
    if i % BURST == BURST - 1:
        time.sleep(0.001)

w.close()
'''


class KillNineCase(unittest.TestCase):
    """One long-running loop rather than many test methods: the corpus has to accumulate
    across kills for later cycles to be testing anything interesting."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="chatcol-kill9-")
        self.data_dir = os.path.join(self.dir, "corpus")
        os.makedirs(self.data_dir)
        self.state_path = os.path.join(self.data_dir, "state.json")
        self.ack_path = os.path.join(self.dir, "acks.txt")
        self.child_path = os.path.join(self.dir, "child.py")
        self.stderr_path = os.path.join(self.dir, "child-stderr.txt")
        with open(self.child_path, "w", encoding="utf-8") as fh:
            fh.write(CHILD_SOURCE)
        self.addCleanup(shutil.rmtree, self.dir, True)

    # ------------------------------------------------------------------ helpers

    def _cycles(self):
        raw = os.environ.get("MELONKIT_KILLNINE_CYCLES")
        if not raw:
            return DEFAULT_CYCLES
        try:
            n = int(raw)
        except ValueError:
            self.fail("MELONKIT_KILLNINE_CYCLES is not an integer: %r" % (raw,))
        if n < 1:
            self.fail("MELONKIT_KILLNINE_CYCLES must be >= 1, got %d" % (n,))
        return n

    def _spawn(self, base_ts, ready_path):
        with open(self.stderr_path, "ab") as errf:
            return subprocess.Popen(
                [sys.executable, self.child_path, ROOT, self.data_dir,
                 self.state_path, self.ack_path, ready_path, str(base_ts)],
                stdout=subprocess.DEVNULL, stderr=errf)

    def _child_stderr(self):
        try:
            with open(self.stderr_path, "rb") as fh:
                return fh.read().decode("utf-8", "replace").strip()
        except OSError:
            return ""

    def _await_ready(self, proc, ready_path, cycle):
        deadline = time.time() + READY_TIMEOUT_S
        while time.time() < deadline:
            if os.path.exists(ready_path):
                return
            if proc.poll() is not None:
                self.fail("cycle %d: child exited (rc=%s) before writing a row.\n"
                          "child stderr:\n%s" % (cycle, proc.returncode,
                                                 self._child_stderr()))
            time.sleep(0.005)
        proc.kill()
        proc.wait()
        self.fail("cycle %d: child never reached its write loop within %.0fs.\n"
                  "child stderr:\n%s"
                  % (cycle, READY_TIMEOUT_S, self._child_stderr()))

    def _acked(self):
        """Every acknowledged (row_id, seq), ignoring a tail torn by the kill.

        Splitting on the newline and dropping the final element yields only lines that were
        terminated, whether or not the file ends in a newline -- so a half-written ack is
        discarded rather than parsed into a phantom row_id.
        """
        try:
            with open(self.ack_path, "rb") as fh:
                data = fh.read()
        except OSError:
            return []
        out = []
        for raw in data.split(b"\n")[:-1]:
            parts = raw.decode("utf-8", "replace").split()
            if len(parts) != 2 or len(parts[0]) != 32:
                continue
            try:
                out.append((parts[0], int(parts[1])))
            except ValueError:
                continue
        return out

    def _restart(self):
        """A real restart: constructing a Writer is what runs tail repair and rehydration."""
        import logging
        import statefile
        import tsutil
        import writer as writer_mod

        log = logging.getLogger("test.killnine." + self.id())
        log.addHandler(logging.NullHandler())
        log.propagate = False
        cfg = {
            "data_dir": self.data_dir,
            "state_path": self.state_path,
            "storage": {"store_component": True, "fsync_interval_ms": 2000,
                        "gzip_delay_s": 10 ** 9, "gzip_level": 6, "max_file_bytes": 0},
            "dedupe": {"window_s": 21600, "max_keys": 400000,
                       "rehydrate_max_bytes": 268435456},
            "backfill": {"max_lookback_days": 7, "second_pass_delay_s": 5400},
            "ops": {"alert_command": None},
        }
        state = statefile.load_state(self.state_path, log)
        if state is None:
            state = statefile.new_state(tsutil.now_us())
        w = writer_mod.Writer(cfg, state, log)
        w.close()

    def _corpus(self, cycle):
        """Parse every corpus file. Returns (row_ids, seqs, line_count)."""
        paths = sorted(glob.glob(os.path.join(self.data_dir, "chat-*.jsonl")) +
                       glob.glob(os.path.join(self.data_dir, "chat-*.jsonl.gz")))
        row_ids, seqs, lines = [], [], 0
        for path in paths:
            opener = gzip.open if path.endswith(".gz") else open
            with opener(path, "rb") as fh:
                for lineno, raw in enumerate(fh, 1):
                    if not raw.strip():
                        continue
                    lines += 1
                    try:
                        rec = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError) as exc:
                        # Deliberately reports position and length only, never the bytes:
                        # the line is chat and a failure message is not a corpus file.
                        self.fail("cycle %d: %s line %d does not parse after restart "
                                  "(%d bytes, %s) -- tail repair did not clean the kill"
                                  % (cycle, os.path.basename(path), lineno, len(raw),
                                     type(exc).__name__))
                    missing = CANONICAL_KEYS - set(rec)
                    self.assertFalse(
                        missing,
                        "cycle %d: %s line %d is missing canonical keys %s"
                        % (cycle, os.path.basename(path), lineno, sorted(missing)))
                    row_ids.append(rec["row_id"])
                    seqs.append(int(rec["seq"]))
        return row_ids, seqs, lines

    @staticmethod
    def _dupes(values):
        seen, dupes = set(), set()
        for v in values:
            if v in seen:
                dupes.add(v)
            seen.add(v)
        return sorted(dupes)

    # ------------------------------------------------------------------ the loop

    def test_kill_nine_loop_never_loses_or_duplicates_a_row(self):
        cycles = self._cycles()
        rng = random.Random(0xC0FFEE)
        # Well in the past, so no row trips the writer's future-ts trust ceiling, and each
        # cycle occupies its own range so its row_ids are distinct from every other cycle's.
        base = 1_750_000_000_000_000
        rows_before = 0
        widest_cycle = 0

        for cycle in range(1, cycles + 1):
            ready_path = os.path.join(self.dir, "ready-%d" % cycle)
            proc = self._spawn(base + cycle * 10_000_000_000, ready_path)
            try:
                self._await_ready(proc, ready_path, cycle)
                time.sleep(rng.uniform(KILL_MIN_S, KILL_MAX_S))
            finally:
                proc.kill()
                proc.wait()

            self._restart()

            row_ids, seqs, lines = self._corpus(cycle)

            dup_ids = self._dupes(row_ids)
            self.assertFalse(dup_ids,
                             "cycle %d: %d duplicate row_id(s) on disk, e.g. %s"
                             % (cycle, len(dup_ids), dup_ids[:3]))

            dup_seqs = self._dupes(seqs)
            self.assertFalse(
                dup_seqs,
                "cycle %d: seq reused across a hard kill (%d value(s), e.g. %s) -- the "
                "reservation in next_seq() is meant to make this impossible"
                % (cycle, len(dup_seqs), dup_seqs[:3]))

            on_disk = set(row_ids)
            acked = self._acked()
            lost = [rid for rid, _ in acked if rid not in on_disk]
            self.assertFalse(
                lost,
                "cycle %d: %d acknowledged row(s) are not on disk, e.g. %s"
                % (cycle, len(lost), lost[:3]))

            self.assertGreater(
                lines, rows_before,
                "cycle %d: the corpus did not grow, so this cycle killed a child that "
                "wrote nothing and asserted nothing.\nchild stderr:\n%s"
                % (cycle, self._child_stderr()))
            widest_cycle = max(widest_cycle, lines - rows_before)
            rows_before = lines

        # A guard against the whole loop passing vacuously.
        acked = self._acked()
        self.assertGreater(len(acked), cycles,
                           "expected more than one acknowledged row per cycle, got %d "
                           "across %d cycles" % (len(acked), cycles))

        # The seq-reuse assertion only has teeth if some cycle was killed *after* it had
        # consumed a whole reservation, because that is the only state from which a naive
        # implementation would re-issue durable values. If throttling ever drifts far
        # enough that no cycle gets past 256 rows, the loop still passes while testing
        # nothing -- so fail loudly here instead.
        self.assertGreater(
            widest_cycle, 256,
            "no cycle wrote more than the %d-value seq reservation (widest was %d), so "
            "the seq-reuse assertion was never exercised -- raise the kill window or the "
            "burst size" % (256, widest_cycle))

        # The corpus is the only place chat may live: the ack channel carries ids, and the
        # child's stderr must be empty of content even if it crashed.
        self.assertNotIn(CHAT, self._child_stderr(),
                         "chat text leaked into child stderr (SPEC §11.4)")


if __name__ == "__main__":
    unittest.main()
