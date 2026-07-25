"""Writer — owns every byte on disk for the chat corpus collector.

Implements SPEC §4.3 (three-tier dedupe), §5 (files, rotation, gzip, startup repair,
durability), §7.1/§7.7/§7.9 (durable gap queue, confirming second pass, lookback clamp),
§8.5 (proxy-log rows are a separate, non-authoritative stream), §10 (state ordering
guarantee) and §11.1.

DELIBERATE DEVIATION FROM SPEC §1
---------------------------------
SPEC §1 routes every producer through a single ``queue.Queue`` into the writer. This
module instead exposes plain thread-safe methods guarded by one internal
``threading.RLock``, per INTERFACES.md. Two reasons, both load-bearing:

1. Observed volume is a few rows per *minute* (SPEC §11.1), so decoupling producers from
   the disk buys nothing measurable while costing a whole extra failure mode (a queue that
   is silently backing up, or draining after the state has already been persisted).
2. ``enqueue_gap`` must be *durable before it returns* — the SSE reader is required to
   persist a gap before it reconnects (SPEC §6.5). A fire-and-forget queue cannot express
   "this is on disk now" without an ack round-trip, which is a worse version of a lock.

An RLock (not a Lock) because the public methods legitimately call one another —
``put_record`` rotates, rotation persists state, ``finish_gap`` calls ``enqueue_gap``.

THE ORDERING GUARANTEE (SPEC §10), which everything else is subordinate to
--------------------------------------------------------------------------
    append record bytes -> (fsync if the interval has elapsed) -> update in-memory state
    ...
    before ANY save_state(): fsync the active data file first, unconditionally

So ``state.json`` is always at or behind the durable data on disk. A hard kill therefore
re-delivers rows, never loses them: the reconnect gap re-fetches the tail and the
rehydrated dedupe set discards whatever was already written.

PRIVACY (SPEC §11.4, standing operator order)
---------------------------------------------
No ``chat``, no ``component``, no record body ever reaches a log record, an exception
message, an alert argument or ``snapshot()``. Only row_id / ts / player_uuid / counters /
byte offsets / file names. Exception messages in this module are built from metadata only,
and caught exceptions are logged by ``type(exc).__name__`` where the library's own message
could conceivably carry payload bytes.
"""
from __future__ import annotations

import glob
import gzip
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

import record
import statefile
import tsutil

_US_PER_S = 1_000_000
_US_PER_DAY = 86_400 * _US_PER_S
_BUCKET_US = 60_000_000          # SPEC §4.3a: one-minute dedupe buckets
_CHUNK = 1 << 20                 # 1 MiB, the read/compress unit used throughout SPEC §5
_MAX_REPAIR_PASSES = 8           # SPEC §5.4: "Repeat up to 8 times."
_DONE_GAPS_KEPT = 100            # keep state.json bounded; errors/pending are never pruned

#: How far ahead of our own wall clock an incoming ``ts_us`` may still move ``hwm_us``
#: and the dedupe prune anchor. One bogus far-future row would otherwise be permanent
#: and unrecoverable: ``hwm_us`` is persisted, so every later gap would compute
#: ``end_us <= start_us`` and be skipped (backfill silently stops forever), every
#: dedupe bucket would sit below the prune cutoff (dedupe silently stops forever) and
#: rehydration would cover no file at all. A full day is far wider than any plausible
#: clock skew between us and the feed, and still infinitely narrower than "year 2999".
_MAX_TS_SKEW_US = _US_PER_DAY

#: SPEC §12.9: "Do not shrink the dedupe window below 2 h."
_MIN_SANE_DEDUPE_WINDOW_US = 7200 * _US_PER_S

#: How many ``seq`` values are reserved (durably) per state write. See ``next_seq``.
_SEQ_RESERVE = 256

#: Attempts to find an appendable active file before giving up (SPEC §5.4 fallout).
_MAX_OPEN_ATTEMPTS = 8

# chat-YYYY-MM-DD.jsonl or the max_file_bytes failsafe form chat-YYYY-MM-DD.NN.jsonl
_NAME_RE = re.compile(r"^chat-(\d{4}-\d{2}-\d{2})(?:\.(\d+))?\.jsonl$")

# bump() targets that SPEC §10 places at the top level of state.json rather than in
# state["counters"] — the SSE reader needs state["connections"] for its batch ordinal.
_TOP_LEVEL_COUNTERS = ("connections",)

_COUNTER_KEYS = ("rows_sse", "rows_backfill", "rows_log", "rows_other",
                 "dupes_dropped", "parse_errors", "tie_skips", "clamped_gaps",
                 "rows_added_pass2", "audit_rows_added", "dedupe_evictions",
                 "gzip_failures", "repaired_bytes",
                 # Additions: every one of these was a silent failure mode before it
                 # had a counter (SPEC §11.3 grades none of them, but an operator
                 # reading status.json can now see them at all).
                 "dedupe_pruned",            # keys dropped by event-time pruning
                 "rehydrate_parse_errors",   # unparseable lines seen while rehydrating
                 "write_errors",             # append/flush failures (e.g. ENOSPC)
                 "future_ts_rows",           # rows too far in the future to trust
                 "gaps_skipped",             # zero-length windows never queued
                 "gaps_requeued",            # 'running' gaps recovered after a kill
                 "gaps_merged")              # gaps folded into an overlapping pending one

_SRC_COUNTER = {record.SRC_SSE: "rows_sse",
                record.SRC_BACKFILL: "rows_backfill",
                record.SRC_LOG: "rows_log"}


def _sub(cfg: dict, section: str, key: str, default: Any) -> Any:
    """Read cfg[section][key] tolerantly, falling back to the SPEC §9 default."""
    sec = cfg.get(section)
    if not isinstance(sec, dict):
        return default
    val = sec.get(key, default)
    return default if val is None else val


def _date_of(name: str) -> Optional[str]:
    m = _NAME_RE.match(name)
    return m.group(1) if m else None


class Writer:
    """The only object in the process that opens a data file for writing.

    All public methods are thread-safe (SPEC §1 / INTERFACES.md).
    """

    # ------------------------------------------------------------------ setup

    def __init__(self, cfg: dict, state: dict, log: logging.Logger) -> None:
        """Startup repair, stray .gz.tmp cleanup, pending_gzip re-drive and dedupe
        rehydration anchored on state['hwm_us']. SPEC §5.3, §5.4, §4.3b, §11.2."""
        self._cfg = cfg
        self._state = state
        self._log = log
        self._lock = threading.RLock()

        self._data_dir = cfg.get("data_dir") or "."
        self._state_path = (cfg.get("state_path")
                            or os.path.join(self._data_dir, "state.json"))

        self._store_component = bool(_sub(cfg, "storage", "store_component", True))
        self._fsync_interval_us = int(_sub(cfg, "storage", "fsync_interval_ms", 2000)) * 1000
        self._gzip_delay_us = int(_sub(cfg, "storage", "gzip_delay_s", 3600)) * _US_PER_S
        self._gzip_level = int(_sub(cfg, "storage", "gzip_level", 6))
        self._max_file_bytes = int(_sub(cfg, "storage", "max_file_bytes", 0))

        self._dedupe_window_us = max(
            0, int(_sub(cfg, "dedupe", "window_s", 21600))) * _US_PER_S
        self._dedupe_max_keys = max(1, int(_sub(cfg, "dedupe", "max_keys", 400_000)))
        self._rehydrate_max_bytes = int(_sub(cfg, "dedupe", "rehydrate_max_bytes",
                                             268_435_456))

        self._max_lookback_us = int(_sub(cfg, "backfill", "max_lookback_days", 7)) * _US_PER_DAY
        self._second_pass_delay_us = int(_sub(cfg, "backfill", "second_pass_delay_s",
                                              5400)) * _US_PER_S
        self._alert_command = _sub(cfg, "ops", "alert_command", None)
        self._persist_interval_us = 10 * _US_PER_S      # SPEC §10, steady-flow persist

        # SPEC §4.3a: dict[minute_bucket] -> set(row_id). Two namespaces, because
        # proxy-log rows must never be able to collide with sse/rest-backfill rows
        # (SPEC §4.2, §8.5).
        self._buckets: Dict[int, Set[str]] = {}
        self._bucket_keys = 0
        self._log_buckets: Dict[int, Set[str]] = {}
        self._log_bucket_keys = 0

        self._fh = None                     # type: Optional[Any]
        self._active_name: Optional[str] = None
        self._active_date: Optional[str] = None
        self._written_bytes = 0             # bytes in the active file
        self._durable_bytes = 0             # bytes of the active file known fsynced
        self._state_dep_offset = 0          # offset the in-memory state depends on
        self._rows_today = 0
        self._last_fsync_us = 0
        self._last_persist_us = 0
        self._repaired_bytes = 0
        self._gzip_blocked: Set[str] = set()
        self._rehydrate_truncated = False
        self._closed = False
        self._start_us = self._now()
        self._backwards_clock_logged: Set[str] = set()
        self._alerted: Set[str] = set()
        self._alert_thread = None           # type: Optional[threading.Thread]

        # SPEC §7.9: on a first run there is no history to heal towards, so no gap may
        # reach back before this process existed. The collector marks the state it just
        # synthesised; the flag is transient and is never persisted.
        self._first_run = bool(self._state.pop("first_run", False))

        os.makedirs(self._data_dir, exist_ok=True)
        parent = os.path.dirname(self._state_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        self._ensure_state_shape()

        if 0 < self._dedupe_window_us < _MIN_SANE_DEDUPE_WINDOW_US:
            self._log.warning(
                "dedupe.window_s is below the SPEC 12.9 floor of 2h: window_s=%d; "
                "replayed rows outside the window will be written twice",
                self._dedupe_window_us // _US_PER_S)
        elif self._dedupe_window_us <= 0:
            self._log.error("dedupe.window_s is not positive: dedupe is effectively off")

        # Event-time pruning anchor. Initialised to hwm_us and NOT to now: after a week of
        # downtime the rehydrated keys must survive the first prune, which is the whole
        # mechanism behind the boundary-row guarantee (SPEC §4.3b, §6.5).
        self._max_ts_us_seen = int(self._state["hwm_us"])
        self._log_max_ts_us = 0

        self._gap_seq = self._initial_gap_seq()
        self._requeue_running_gaps()

        # SPEC §11.2 start-up order, with one deliberate reordering: the pending_gzip
        # re-drive runs LAST rather than at step 3. A stale active file left torn by a
        # hard kill has to be repaired *before* it is compressed, or the tear is frozen
        # into the .gz forever, and compression must never delay dedupe rehydration.
        self._cleanup_gz_tmp()
        self._adopt_previous_active()
        self._open_active(self._today())
        self._rehydrate()
        self._drive_gzip()
        self._save_state()

    def _ensure_state_shape(self) -> None:
        """Fill in any key SPEC §10 requires but a hand-edited or older state.json lacks."""
        st = self._state
        st.setdefault("version", statefile.STATE_VERSION)
        st.setdefault("seq", 0)
        st.setdefault("seq_reserved", 0)
        st.setdefault("connections", 0)
        st.setdefault("active_file", None)
        if not isinstance(st.get("pending_gzip"), list):
            st["pending_gzip"] = []
        if not isinstance(st.get("gzip_blocked"), list):
            # SPEC §5.3 step 3: a file whose verify failed must NOT be retried
            # automatically. That decision has to survive a restart, or a crash-looping
            # collector re-runs the whole compress+verify and re-fires the critical
            # alert on every start.
            st["gzip_blocked"] = []
        if not isinstance(st.get("gaps"), list):
            st["gaps"] = []
        if not isinstance(st.get("log_tail"), dict):
            st["log_tail"] = {}
        st.setdefault("audit_last_run_us", 0)
        if not isinstance(st.get("counters"), dict):
            st["counters"] = {}
        for key in _COUNTER_KEYS:
            st["counters"].setdefault(key, 0)
        try:
            hwm = int(st.get("hwm_us") or 0)
        except (TypeError, ValueError):
            hwm = 0
        if hwm <= 0:
            # SPEC §7.9: an absent hwm must never trigger a full-history crawl.
            hwm = self._now()
            self._log.warning("state has no usable hwm_us; anchoring on now hwm_us=%d", hwm)
        ceiling = self._now() + _MAX_TS_SKEW_US
        if hwm > ceiling:
            # A persisted hwm from the future is unrecoverable if trusted: every gap
            # would compute end_us <= start_us and be skipped, so backfill would stop
            # for good while health stayed green. Heal it here instead of requiring a
            # hand edit, and say so loudly.
            self._log.error("state hwm_us is implausibly far in the future: hwm_us=%d "
                            "ceiling_us=%d; anchoring on now", hwm, ceiling)
            self.bump("future_ts_rows")
            hwm = self._now()
        st["hwm_us"] = hwm

        # `seq` is the last value issued; `seq_reserved` is how far ahead of it the
        # writer has already made durable. Resuming from the reservation is what stops
        # a hard kill from re-issuing a seq that is already stamped on a durable row.
        try:
            issued = max(int(st.get("seq") or 0), int(st.get("seq_reserved") or 0))
        except (TypeError, ValueError):
            issued = 0
        self._seq = issued
        st["seq"] = issued
        st["seq_reserved"] = issued
        self._gzip_blocked = set(
            name for name in st["gzip_blocked"] if isinstance(name, str))
        if self._gzip_blocked:
            self._log.error("gzip is blocked for %d file(s) after a failed verify; "
                            "they stay plaintext until an operator intervenes "
                            "(SPEC 5.3)", len(self._gzip_blocked))

    def _requeue_running_gaps(self) -> None:
        """Recover gaps a hard kill left mid-walk (SPEC §7.1, §11.2).

        ``claim_gap`` only ever selects ``pending``, and nothing else in the program
        moves a gap out of ``running``. A gap that was being walked when the host
        rebooted would therefore sit in ``state["gaps"]`` forever: never claimed, never
        pruned, never surfaced by any §11.3 health threshold — a permanent hole in the
        corpus with ``health: ok``. ``cursor_us`` is left exactly as it was, so the walk
        resumes where it stopped rather than re-walking (SPEC §7.1).
        """
        requeued = 0
        for gap in self._state["gaps"]:
            if not isinstance(gap, dict) or gap.get("status") != "running":
                continue
            gap["status"] = "pending"
            try:
                gap["not_before_us"] = min(int(gap.get("not_before_us") or 0), self._now())
            except (TypeError, ValueError):
                gap["not_before_us"] = self._now()
            requeued += 1
            self._log.warning("requeued gap left running by a hard kill gap_id=%s "
                              "cursor_us=%s attempts=%s",
                              gap.get("gap_id"), gap.get("cursor_us"),
                              gap.get("attempts"))
        if requeued:
            self.bump("gaps_requeued", requeued)

    def _initial_gap_seq(self) -> int:
        seq = 0
        try:
            seq = int(self._state.get("gap_seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        for gap in self._state["gaps"]:
            gid = gap.get("gap_id") if isinstance(gap, dict) else None
            if isinstance(gid, str) and gid.startswith("g") and gid[1:].isdigit():
                seq = max(seq, int(gid[1:]))
        self._state["gap_seq"] = seq
        return seq

    # ------------------------------------------------------------------ clock

    def _now(self) -> int:
        """Wall clock in microseconds. A single seam so tests can freeze time."""
        return tsutil.now_us()

    def _today(self) -> str:
        return tsutil.utc_date(self._now())

    # --------------------------------------------------------- active file I/O

    def _names_for_date(self, date: str) -> List[str]:
        """Existing plaintext files for `date`, oldest first. Sorted by the max_file_bytes
        index (the un-indexed base file is index 0), NOT lexicographically — 'chat-D.01'
        sorts before 'chat-D' as bytes, which would make a restart reopen the full base
        file instead of resuming the newest part."""
        found = []
        for path in glob.glob(os.path.join(self._data_dir, "chat-%s*.jsonl" % date)):
            name = os.path.basename(path)
            m = _NAME_RE.match(name)
            if m and m.group(1) == date:
                found.append((int(m.group(2) or 0), name))
        return [name for _, name in sorted(found)]

    def _highest_index(self, date: str) -> int:
        """Highest ``.NN`` index in use for `date`, counting compressed days too.

        The ``.gz`` files matter: a name whose ``.gz`` already exists must never be
        chosen for a new plaintext file, or the next compression pass would
        ``os.replace`` over an archive that is already verified and fsynced.
        """
        highest = 0
        for pattern in ("chat-%s*.jsonl" % date, "chat-%s*.jsonl.gz" % date):
            for path in glob.glob(os.path.join(self._data_dir, pattern)):
                name = os.path.basename(path)
                if name.endswith(".gz"):
                    name = name[:-3]
                m = _NAME_RE.match(name)
                if m and m.group(1) == date and m.group(2):
                    highest = max(highest, int(m.group(2)))
        return highest

    def _next_indexed_name(self, date: str) -> str:
        return "chat-%s.%02d.jsonl" % (date, self._highest_index(date) + 1)

    def _archived(self, name: str) -> bool:
        return os.path.exists(os.path.join(self._data_dir, name + ".gz"))

    def _pick_active_name(self, date: str) -> str:
        """Name to append to for `date`: the newest plaintext, else an unarchived name.

        The second clause is load-bearing. If ``chat-D.jsonl.gz`` exists and no
        plaintext does, opening ``chat-D.jsonl`` would create a fresh one-row file whose
        compression pass then replaces the real, verified, multi-thousand-row archive —
        a whole day destroyed with no error, no counter and no health change. That is
        reachable from a clock step backwards across UTC midnight, so the name is
        side-stepped instead.
        """
        existing = self._names_for_date(date)
        if existing:
            return existing[-1]
        base = "chat-%s.jsonl" % date
        if not self._archived(base):
            return base
        name = self._next_indexed_name(date)
        self._log.warning("date %s is already archived; appending to a new part "
                          "instead of the archived name file=%s", date, name)
        return name

    def _open_active(self, date: str, indexed: bool = False) -> None:
        for attempt in range(_MAX_OPEN_ATTEMPTS):
            name = (self._next_indexed_name(date) if (indexed or attempt)
                    else self._pick_active_name(date))
            path = os.path.join(self._data_dir, name)
            existed = os.path.exists(path)

            if existed and os.path.getsize(path) > 0:
                ok, _repaired = self._repair_file(path)
                if not ok:
                    # The tail could not be repaired (unwritable file, EIO, or 8 passes
                    # exhausted). Appending now would concatenate the next record onto
                    # torn bytes and produce an unparseable line in the MIDDLE of the
                    # file, which no later repair pass can ever reach. Roll to a fresh
                    # part instead and leave the damaged file untouched for the operator.
                    self._log.error("refusing to append to an unrepaired file file=%s; "
                                    "rolling to a new part", name)
                    continue
                if self._max_file_bytes and os.path.getsize(path) >= self._max_file_bytes:
                    # A full file found at startup still has to be compressed, or it
                    # stays plaintext forever (SPEC §5.2 step 2).
                    if name not in self._state["pending_gzip"]:
                        self._state["pending_gzip"].append(name)
                        self._log.info("queued full file for gzip file=%s", name)
                    continue

            self._fh = open(path, "ab")  # SPEC §5.5: append mode, kernel positions at EOF
            if not existed:
                # SPEC §10: the directory entry of a brand-new day file must be durable
                # before state.json can name it. Without this a power loss just after a
                # midnight roll can leave state.json pointing at a file that does not
                # exist while hwm_us has already advanced past its rows — the one
                # direction the ordering guarantee is supposed to make impossible.
                self._fsync_dir(self._data_dir)
            size = os.path.getsize(path)
            self._written_bytes = size
            self._durable_bytes = size
            self._state_dep_offset = size
            self._rows_today = self._count_lines(path) if size else 0
            self._active_name = name
            self._active_date = date
            self._state["active_file"] = name
            self._last_fsync_us = self._now()
            return
        raise OSError("no appendable data file for date %s after %d attempts"
                      % (date, _MAX_OPEN_ATTEMPTS))

    def _adopt_previous_active(self) -> None:
        """A file named by state['active_file'] from a previous run that is not today's
        must be repaired and then queued for compression (SPEC §5.2 steps 2-3)."""
        prev = self._state.get("active_file")
        if not isinstance(prev, str) or not prev:
            return
        date = _date_of(prev)
        if date is None or date == self._today():
            return                      # today's file is adopted by _open_active
        path = os.path.join(self._data_dir, prev)
        if not os.path.exists(path):
            return
        if os.path.getsize(path) > 0:
            ok, _repaired = self._repair_file(path)
            if not ok:
                # Compressing an unrepaired file freezes the torn tail into the .gz
                # forever (SPEC §11.2 step 4 runs before step 3 for exactly this reason).
                # Leave it plaintext and try again on the next start.
                self._log.error("stale active file could not be repaired, not queued "
                                "for gzip file=%s", prev)
                return
        if prev not in self._state["pending_gzip"]:
            self._state["pending_gzip"].append(prev)
            self._log.info("queued stale active file for gzip file=%s", prev)

    @staticmethod
    def _count_lines(path: str) -> int:
        total = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                total += chunk.count(b"\n")
        return total

    # --------------------------------------------------- startup repair (§5.4)

    @staticmethod
    def _rfind_newline(f, limit: int) -> int:
        """Last b'\\n' at an offset < limit, or -1. Scans backwards in 1 MiB chunks so a
        pathologically long torn line cannot defeat the search."""
        pos = limit
        while pos > 0:
            step = min(_CHUNK, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step)
            idx = buf.rfind(b"\n")
            if idx >= 0:
                return pos + idx
        return -1

    @staticmethod
    def _line_ok(line: bytes) -> bool:
        """A complete record: decodable UTF-8, non-blank, and JSON-parseable.

        Nothing from `line` is ever surfaced — the caller logs byte counts only.
        """
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            return False                # torn mid multi-byte sequence
        if not text.strip():
            return False
        try:
            json.loads(text)
        except ValueError:
            return False
        return True

    def _repair_file(self, path: str) -> Tuple[bool, int]:
        """SPEC §5.4. Truncate a torn final line back to the last complete, JSON-parseable
        record. THIS IS THE ONLY PLACE THE COLLECTOR EVER TRUNCATES A DATA FILE, and it
        only ever removes bytes that follow the last valid record.

        Returns ``(ok, repaired_bytes)``. ``ok`` is False when the file may **not** be
        appended to: the repair could not run at all (an OSError -- a read-only mount, an
        EIO, a Windows sharing violation from a `tail`/AV/backup holding the file) or the
        8-pass budget was exhausted. Both cases leave torn bytes at the end of the file,
        and the caller must not append after them.
        """
        name = os.path.basename(path)
        repaired = 0
        resolved = False
        try:
            with open(path, "r+b") as f:
                for _ in range(_MAX_REPAIR_PASSES):
                    size = f.seek(0, os.SEEK_END)
                    if size == 0:
                        resolved = True
                        break
                    f.seek(size - 1)
                    if f.read(1) != b"\n":
                        # Step 1: no trailing newline -> drop everything after the last one.
                        nl = self._rfind_newline(f, size)
                        new_size = 0 if nl < 0 else nl + 1
                        f.truncate(new_size)
                        f.flush()
                        os.fsync(f.fileno())
                        repaired += size - new_size
                        continue
                    # Step 2: validate the final complete line.
                    prev_nl = self._rfind_newline(f, size - 1)
                    line_start = 0 if prev_nl < 0 else prev_nl + 1
                    f.seek(line_start)
                    line = f.read(size - 1 - line_start)
                    if self._line_ok(line):
                        resolved = True
                        break
                    f.truncate(line_start)
                    f.flush()
                    os.fsync(f.fileno())
                    repaired += size - line_start
        except OSError as exc:
            self._log.error("startup repair failed file=%s errno=%s",
                            name, getattr(exc, "errno", None))
            return False, 0
        if not resolved:
            self._log.error("startup repair unresolved after %d passes file=%s "
                            "repaired_bytes=%d", _MAX_REPAIR_PASSES, name, repaired)
        if repaired:
            self._repaired_bytes += repaired
            self._state["counters"]["repaired_bytes"] = (
                int(self._state["counters"].get("repaired_bytes") or 0) + repaired)
            self._log.warning("startup repair file=%s repaired_bytes=%d", name, repaired)
        return resolved, repaired

    # ----------------------------------------------------- durability (§5.5/§10)

    def _fsync_active(self, force: bool) -> None:
        """fsync the active data file at most every fsync_interval_ms, or unconditionally
        when `force` (which every state persist does — SPEC §10)."""
        if self._fh is None:
            return
        if self._durable_bytes == self._written_bytes and not force:
            return
        now = self._now()
        if not force and (now - self._last_fsync_us) < self._fsync_interval_us:
            return
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._durable_bytes = self._written_bytes
        self._last_fsync_us = now

    def _save_state(self) -> None:
        """Persist state.json. ORDERING: the data file is fsynced first, always, so
        state.json can never reference bytes that are not durable (SPEC §10)."""
        self._fsync_active(force=True)
        statefile.save_state(self._state_path, self._state)
        self._last_persist_us = self._now()

    # ------------------------------------------------------------ dedupe (§4.3)

    def _ns(self, is_log: bool) -> Dict[int, Set[str]]:
        return self._log_buckets if is_log else self._buckets

    def _seen(self, row_id_: str, ts_us: int, is_log: bool) -> bool:
        # row_id is a pure function of ts_us (SPEC §4.2), so a duplicate always lands in
        # the same one-minute bucket — an exact bucket lookup is sufficient and O(1).
        bucket = self._ns(is_log).get(ts_us // _BUCKET_US)
        return bucket is not None and row_id_ in bucket

    def _remember(self, row_id_: str, ts_us: int, is_log: bool) -> None:
        buckets = self._ns(is_log)
        key = ts_us // _BUCKET_US
        bucket = buckets.get(key)
        if bucket is None:
            bucket = set()
            buckets[key] = bucket
        if row_id_ in bucket:
            return
        bucket.add(row_id_)
        # The prune anchor may never be advanced by a row from the far future: it is what
        # the whole window floor is computed from, so one bogus ts_us would put every
        # existing bucket below the cutoff and silently end dedupe for the life of the
        # process (see _MAX_TS_SKEW_US).
        ceiling = self._now() + _MAX_TS_SKEW_US
        if is_log:
            self._log_bucket_keys += 1
            if self._log_max_ts_us < ts_us <= ceiling:
                self._log_max_ts_us = ts_us
        else:
            self._bucket_keys += 1
            if self._max_ts_us_seen < ts_us <= ceiling:
                self._max_ts_us_seen = ts_us
        self._prune_dedupe(is_log)

    def _prune_dedupe(self, is_log: bool) -> None:
        """SPEC §4.3a: prune by EVENT time against dedupe.window_s, then enforce the hard
        max_keys cap by evicting the oldest bucket and counting the eviction."""
        buckets = self._ns(is_log)
        anchor = self._log_max_ts_us if is_log else self._max_ts_us_seen
        cutoff_bucket = (anchor - self._dedupe_window_us) // _BUCKET_US
        dropped = 0
        for key in [k for k in buckets if k < cutoff_bucket]:
            dropped += len(buckets.pop(key))
        if dropped:
            if is_log:
                self._log_bucket_keys -= dropped
            else:
                self._bucket_keys -= dropped
            # Event-time pruning is by design (SPEC §4.3a) and is exactly why the
            # per-gap set of §4.3c exists, but it used to move no counter at all: a
            # backfill walk wider than the window could silently write every row twice
            # with nothing in status.json changing. Now it is at least visible.
            self.bump("dedupe_pruned", dropped)

        while len(buckets) > 1:
            keys = self._log_bucket_keys if is_log else self._bucket_keys
            if keys <= self._dedupe_max_keys:
                break
            oldest = min(buckets)
            evicted = len(buckets.pop(oldest))
            if is_log:
                self._log_bucket_keys -= evicted
            else:
                self._bucket_keys -= evicted
            self.bump("dedupe_evictions")
            self._log.warning("dedupe cap exceeded, evicted oldest bucket "
                              "bucket_min_us=%d keys_evicted=%d max_keys=%d",
                              oldest * _BUCKET_US, evicted, self._dedupe_max_keys)

    def _rehydrate(self) -> None:
        """SPEC §4.3b — the data file IS the dedupe log. Read the corpus backwards
        collecting row_ids for rows with ts_us >= hwm_us - window, anchored on hwm_us and
        NOT on now. That anchor is what makes the inclusive-bound boundary row deduplicate
        correctly after arbitrarily long downtime (SPEC §6.5, §7.3)."""
        hwm = int(self._state["hwm_us"])
        if hwm <= 0:
            return
        cutoff = hwm - self._dedupe_window_us
        # A row whose event ts is >= cutoff cannot have been ingested before it happened,
        # so it can only live in a file dated >= utc_date(cutoff). One extra day of margin
        # absorbs clock skew and the ingest-date partitioning of §5.1.
        min_date = tsutil.utc_date(max(0, cutoff - _US_PER_DAY))

        budget = self._rehydrate_max_bytes
        files = 0
        before = self._bucket_keys + self._log_bucket_keys
        for name, is_gz in self._corpus_files_newest_first(min_date):
            path = os.path.join(self._data_dir, name)
            files += 1
            try:
                for line in (self._iter_lines_forward(path) if is_gz
                             else self._iter_lines_reverse(path)):
                    budget -= len(line) + 1
                    if budget < 0:
                        self._rehydrate_truncated = True
                        break
                    self._absorb_line(line, cutoff)
            except OSError as exc:
                self._log.error("dedupe rehydration read failed file=%s err=%s",
                                name, type(exc).__name__)
            except Exception as exc:            # a corrupt .gz must not block startup
                self._log.error("dedupe rehydration aborted for file=%s err=%s",
                                name, type(exc).__name__)
            if self._rehydrate_truncated:
                break

        if self._rehydrate_truncated:
            self._log.warning("dedupe rehydration hit rehydrate_max_bytes=%d; "
                              "window is only partially covered", self._rehydrate_max_bytes)
        self._log.info("dedupe rehydrated keys=%d log_keys=%d new_keys=%d files=%d "
                       "hwm_us=%d cutoff_us=%d",
                       self._bucket_keys, self._log_bucket_keys,
                       self._bucket_keys + self._log_bucket_keys - before, files,
                       int(self._state["hwm_us"]), cutoff)

    def _corpus_files_newest_first(self, min_date: str) -> List[Tuple[str, bool]]:
        """``[(name, is_gz)]`` for every corpus file dated >= `min_date`, newest first.

        Ordering is by ``(date, part index)`` and NOT lexicographic. Bytes-wise
        ``'chat-D.jsonl'`` sorts *after* ``'chat-D.01.jsonl'`` (``'j'`` > ``'0'``), so a
        reverse byte sort hands back the oldest part of a size-rotated day first — and
        when a byte budget binds, the rows skipped are then the newest ones, i.e. exactly
        the rows nearest ``hwm_us`` that the whole mechanism exists to cover.
        """
        found: List[Tuple[str, int, bool, str]] = []
        for path in glob.glob(os.path.join(self._data_dir, "chat-*.jsonl")):
            name = os.path.basename(path)
            m = _NAME_RE.match(name)
            if m:
                found.append((m.group(1), int(m.group(2) or 0), False, name))
        for path in glob.glob(os.path.join(self._data_dir, "chat-*.jsonl.gz")):
            name = os.path.basename(path)
            m = _NAME_RE.match(name[:-3])
            if m:
                found.append((m.group(1), int(m.group(2) or 0), True, name))
        found.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        return [(name, is_gz) for date, _idx, is_gz, name in found if date >= min_date]

    def _absorb_line(self, line: bytes, cutoff: int) -> None:
        try:
            obj = json.loads(line.decode("utf-8", "replace"))
            ts_us = int(obj["ts_us"])
            rid = obj["row_id"]
            src = obj.get("src")
        except (ValueError, TypeError, KeyError):
            # A line the repair pass left behind, or a hand-edited file. Counted, never
            # shown. Deliberately NOT `parse_errors`: SPEC §11.3 grades that counter as
            # "rising", and re-reading the same bad line on every restart would raise a
            # standing warn that no live fault corresponds to.
            self.bump("rehydrate_parse_errors")
            return
        if not isinstance(rid, str) or ts_us < cutoff:
            return
        self._remember(rid, ts_us, src == record.SRC_LOG)

    @staticmethod
    def _iter_lines_reverse(path: str) -> Iterator[bytes]:
        """Yield complete lines newest-first, reading 1 MiB chunks from the end so the
        rehydrate byte budget is spent on the newest rows."""
        with open(path, "rb") as f:
            pos = f.seek(0, os.SEEK_END)
            head = b""
            while pos > 0:
                step = min(_CHUNK, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + head
                parts = buf.split(b"\n")
                head = parts.pop(0)
                for part in reversed(parts):
                    if part:
                        yield part
            if head:
                yield head

    @staticmethod
    def _iter_lines_forward(path: str) -> Iterator[bytes]:
        with gzip.open(path, "rb") as f:
            for line in f:
                line = line.rstrip(b"\n")
                if line:
                    yield line

    # --------------------------------------------------------------- ingestion

    def next_seq(self) -> int:
        """SPEC §2 field 11: monotonic per-collector counter, persisted, never reset.

        Persisted through a *reservation* rather than a state write per row. Bumping the
        in-memory counter alone would re-issue, after a hard kill, seq values that are
        already stamped on durable rows — two different rows on disk with the same seq,
        which breaks any downstream use of it as an identity. Writing state on every seq
        instead would mean an fsync per row. So the writer makes ``seq + 256`` durable
        before issuing ``seq``, resumes from the reservation on a crash (a gap in the
        numbering, never a reuse), and hands the reservation back on a clean ``close()``
        so an orderly restart continues without a jump.
        """
        with self._lock:
            self._seq += 1
            self._state["seq"] = self._seq
            if self._seq > int(self._state.get("seq_reserved") or 0):
                self._state["seq_reserved"] = self._seq + _SEQ_RESERVE
                self._save_state()
            return self._seq

    def put_record(self, rec: dict) -> bool:
        """Append one canonical record. Returns False when it was a duplicate and dropped.

        SPEC §4.3, §5.1, §5.5, §8.5, §10. proxy-log rows (src == 'proxy-log') use a
        separate dedupe namespace, never advance hwm_us and never create or close gaps —
        they are an independent, non-authoritative stream sharing the file.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("writer is closed")

            rid = rec["row_id"]
            ts_us = int(rec["ts_us"])
            src = rec.get("src")
            is_log = (src == record.SRC_LOG)

            if self._seen(rid, ts_us, is_log):
                self.bump("dupes_dropped")
                return False

            if not self._store_component and rec.get("component") is not None:
                rec = dict(rec)                  # never mutate the producer's record
                rec["component"] = None
            data = record.encode(rec)

            # SPEC §5.1 — the file is partitioned by the UTC date of *ingest_ts*, never of
            # the event ts. A backfilled row for a gap two days ago therefore lands in
            # TODAY's file. This is deliberate: it is what keeps append-only append-only
            # and guarantees a closed, gzipped, fsynced day file is never reopened
            # (invariant 3 in §1). Downstream mining sorts by ts_us anyway, and src/ts on
            # every row make the reordering explicit. DO NOT "fix" this by routing backfill
            # into per-event-date files — reopening a closed .gz is the exact failure this
            # design exists to avoid.
            self._roll_if_needed(self._ingest_date(rec))

            # ORDERING (SPEC §10): bytes first, then durability, then in-memory state.
            # One single write() including the trailing newline (SPEC §5.5).
            try:
                self._fh.write(data)
                self._fh.flush()
            except OSError as exc:
                # ENOSPC / EIO. Nothing below has run, so no counter, no dedupe key and no
                # hwm advance claims this row — the safe direction, and the reconnect gap
                # re-fetches it. But BufferedWriter *keeps* the unflushed remainder, so
                # some of these bytes may land later: byte accounting must come from the
                # file itself from here on, or _written_bytes stays permanently short of
                # reality and both the max_file_bytes failsafe and the fsync shortcut lie.
                self.bump("write_errors")
                self._resync_written_bytes()
                self._log.error("append failed file=%s errno=%s written_bytes=%d",
                                self._active_name, getattr(exc, "errno", None),
                                self._written_bytes)
                raise
            self._written_bytes += len(data)
            self._fsync_active(force=False)

            # ...and only now does any state advance. Nothing below is persisted until the
            # next _save_state(), which force-fsyncs first, so state.json can never point
            # past durable bytes.
            self._state_dep_offset = self._written_bytes
            self._rows_today += 1
            self.bump(_SRC_COUNTER.get(src, "rows_other"))
            self._remember(rid, ts_us, is_log)
            if not is_log and src in (record.SRC_SSE, record.SRC_BACKFILL):
                # hwm_us = max(ts_us) over sse + rest-backfill rows only (SPEC §6.5).
                ceiling = self._now() + _MAX_TS_SKEW_US
                if ts_us > ceiling:
                    # The row is kept — it is data — but it must not become the high-water
                    # mark: hwm_us is persisted, and a far-future value permanently stops
                    # every future gap (end_us <= start_us) while health stays green.
                    self.bump("future_ts_rows")
                    self._log.warning("row ts is beyond the trust ceiling, hwm not "
                                      "advanced row_id=%s ts_us=%d ceiling_us=%d",
                                      rid, ts_us, ceiling)
                elif ts_us > int(self._state["hwm_us"]):
                    self._state["hwm_us"] = ts_us

            if (self._now() - self._last_persist_us) >= self._persist_interval_us:
                self._save_state()
            return True

    def _resync_written_bytes(self) -> None:
        """Re-derive the byte accounting from the file after a failed append."""
        try:
            size = os.fstat(self._fh.fileno()).st_size if self._fh is not None else 0
        except (OSError, ValueError):
            return
        self._written_bytes = size
        self._durable_bytes = min(self._durable_bytes, size)
        self._state_dep_offset = min(self._state_dep_offset, size)

    def _ingest_date(self, rec: dict) -> str:
        ing = rec.get("ingest_ts")
        if isinstance(ing, str):
            try:
                return tsutil.utc_date(tsutil.parse_ts(ing))
            except tsutil.TsParseError:
                self._log.warning("record has an unparseable ingest_ts row_id=%s; "
                                  "partitioning on wall clock", rec.get("row_id"))
        return self._today()

    # ---------------------------------------------------------- rotation (§5.2)

    def _roll_if_needed(self, date: str) -> None:
        """Roll if `date` is a LATER day than the active one, or the size cap is hit.

        Rotation is strictly forwards (SPEC §1 invariant 3, §5.1). Two drivers decide the
        current date — ``tick()`` from the wall clock and ``put_record`` from the record's
        own ``ingest_ts`` — and they can legitimately disagree by a hair across midnight:
        the SSE reader stamps ``ingest_us`` *before* it blocks on this lock, so a record
        stamped at 23:59:59.9 can arrive after ``tick()`` has already rolled to D+1.
        Rotating backwards there would close D+1, re-open yesterday's already-closed
        (possibly already-compressed) file, and put both names in ``pending_gzip``.

        A late record therefore lands in the newer file. That is a sub-second partitioning
        inaccuracy against a hard invariant; ``ts_us`` on the row is unaffected, and
        downstream sorts by ``ts_us`` anyway (SPEC §5.1).
        """
        if self._active_date is None or date > self._active_date:
            self._rotate(date)
            return
        if date < self._active_date and date not in self._backwards_clock_logged:
            self._backwards_clock_logged.add(date)
            self._log.warning("record/clock date is behind the active file, not rolling "
                              "backwards date=%s active_date=%s", date, self._active_date)
        if self._max_file_bytes and self._written_bytes >= self._max_file_bytes:
            self._rotate(self._active_date, indexed=True)

    def _rotate(self, new_date: str, indexed: bool = False) -> None:
        """SPEC §5.2 roll sequence, crash-safe at every step."""
        closed = self._active_name
        if self._fh is not None:
            self._fsync_active(force=True)          # step 1
            try:
                self._fh.close()
            finally:
                # In a `finally` because BufferedWriter.close() re-raises a failed flush
                # *after* closing the fd. Leaving the dead object in place would make
                # every later put_record raise ValueError("I/O operation on closed file")
                # instead of re-opening.
                self._fh = None
        if closed and closed not in self._state["pending_gzip"]:
            self._state["pending_gzip"].append(closed)   # step 2
        self._save_state()
        self._open_active(new_date, indexed=indexed)      # step 3
        self._save_state()
        self._log.info("rotated data file closed=%s active=%s pending_gzip=%d",
                       closed, self._active_name, len(self._state["pending_gzip"]))

    # -------------------------------------------------------------- gzip (§5.3)

    def _cleanup_gz_tmp(self) -> None:
        for path in glob.glob(os.path.join(self._data_dir, "*.gz.tmp")):
            try:
                os.unlink(path)
                self._log.warning("removed stray gzip temp file=%s", os.path.basename(path))
            except OSError as exc:
                self._log.error("could not remove stray gzip temp file=%s errno=%s",
                                os.path.basename(path), getattr(exc, "errno", None))

    def _remove_pending(self, name: str) -> None:
        pending = self._state["pending_gzip"]
        while name in pending:
            pending.remove(name)

    def _drive_gzip(self) -> None:
        """Compress closed day files in the writer's idle time (SPEC §5.2 step 4)."""
        now = self._now()
        for name in list(self._state["pending_gzip"]):
            if name in self._gzip_blocked or name == self._active_name:
                continue
            src = os.path.join(self._data_dir, name)
            if not os.path.exists(src):
                # Already compressed by a previous run, or removed by the operator.
                self._remove_pending(name)
                self._save_state()
                continue
            try:
                mtime_us = int(os.stat(src).st_mtime * _US_PER_S)
            except OSError:
                continue
            if self._gzip_delay_us and (now - mtime_us) < self._gzip_delay_us:
                continue                     # grace period keeps the last day greppable
            self._compress(name, src)

    def _compress(self, name: str, src: str) -> None:
        tmp = src + ".gz.tmp"
        dst = src + ".gz"
        try:
            src_lines, src_bytes = self._gzip_write(src, tmp)
        except OSError as exc:
            self._log.error("gzip write failed file=%s errno=%s", name,
                            getattr(exc, "errno", None))
            self._unlink_quiet(tmp)
            return                            # transient (disk full); tick retries

        # SPEC §5.3 step 3 — VERIFY by streaming the candidate back out.
        try:
            gz_lines, gz_bytes = self._gzip_verify(tmp)
        except Exception as exc:              # truncated stream, bad CRC, zlib error...
            gz_lines, gz_bytes = -1, -1
            self._log.error("gzip verify could not read file=%s err=%s",
                            name, type(exc).__name__)

        if (gz_lines, gz_bytes) != (src_lines, src_bytes):
            self._unlink_quiet(tmp)
            reason = ("gzip verify mismatch file=%s src_lines=%d src_bytes=%d "
                      "gz_lines=%d gz_bytes=%d" % (name, src_lines, src_bytes,
                                                   gz_lines, gz_bytes))
            self._block_gzip(name, reason)
            return

        # SPEC §5.3's verify compares the candidate against its own source; it cannot see
        # that the DESTINATION is an existing archive about to be overwritten. That is a
        # real path: a crash between step 4 and step 5 leaves both files, and so does any
        # route that re-opens an already-archived date. os.replace would then destroy a
        # verified, fsynced day and report success.
        if os.path.exists(dst):
            try:
                dst_lines, dst_bytes = self._gzip_verify(dst)
            except Exception as exc:
                dst_lines, dst_bytes = -1, -1
                self._log.error("existing archive is unreadable file=%s err=%s",
                                name, type(exc).__name__)
            if (dst_lines, dst_bytes) == (src_lines, src_bytes):
                # Resuming SPEC §5.3 after a crash between step 4 and step 5: the archive
                # already holds exactly this content, so finish the remaining steps.
                self._unlink_quiet(tmp)
                self._finish_compress(name, src, dst, src_lines, src_bytes,
                                      "archive already current")
                return
            self._unlink_quiet(tmp)
            self._block_gzip(name, "gzip destination already exists with different "
                                   "content file=%s src_lines=%d dst_lines=%d "
                                   "src_bytes=%d dst_bytes=%d"
                             % (name, src_lines, dst_lines, src_bytes, dst_bytes))
            return

        os.replace(tmp, dst)                                  # step 4
        self._fsync_dir(self._data_dir)
        self._finish_compress(name, src, dst, src_lines, src_bytes, "gzipped")

    def _finish_compress(self, name: str, src: str, dst: str, src_lines: int,
                         src_bytes: int, what: str) -> None:
        """SPEC §5.3 steps 5-6: drop the plaintext, dequeue, persist. Idempotent."""
        try:
            os.unlink(src)                                    # step 5
        except OSError as exc:
            self._log.error("gzip could not unlink plaintext file=%s errno=%s",
                            name, getattr(exc, "errno", None))
        self._remove_pending(name)                            # step 6
        self._save_state()
        try:
            gz_size = os.path.getsize(dst)
        except OSError:
            gz_size = -1
        self._log.info("%s file=%s lines=%d plain_bytes=%d gz_bytes=%d",
                       what, name, src_lines, src_bytes, gz_size)

    def _block_gzip(self, name: str, reason: str) -> None:
        """SPEC §5.3 step 3: leave the plaintext alone and do NOT retry automatically.

        The block list is persisted. In memory only, every restart re-ran the whole
        compress+verify on a file already known to be bad and re-fired the critical
        alert — under a supervisor that is a paging loop.
        """
        self._gzip_blocked.add(name)
        blocked = self._state["gzip_blocked"]
        if name not in blocked:
            blocked.append(name)
        self.bump("gzip_failures")
        self._log.error("%s; plaintext left in place, not retried", reason)
        self._save_state()          # the failure counter and the block are durable now
        self._alert("critical", "gzip:" + name, reason)

    def _gzip_write(self, src: str, tmp: str) -> Tuple[int, int]:
        """Stream `src` into `tmp` in 1 MiB chunks; return the SOURCE (line, byte) counts."""
        lines = 0
        total = 0
        with open(src, "rb") as fin, open(tmp, "wb") as raw:
            gz = gzip.GzipFile(filename="", mode="wb",
                               compresslevel=self._gzip_level, fileobj=raw)
            try:
                while True:
                    chunk = fin.read(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    lines += chunk.count(b"\n")
                    gz.write(chunk)
            finally:
                gz.close()
            raw.flush()
            os.fsync(raw.fileno())
        return lines, total

    @staticmethod
    def _gzip_verify(tmp: str) -> Tuple[int, int]:
        """Stream-decompress the candidate and count lines and bytes (SPEC §5.3 step 3)."""
        lines = 0
        total = 0
        with gzip.open(tmp, "rb") as gz:
            while True:
                chunk = gz.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                lines += chunk.count(b"\n")
        return lines, total

    def _unlink_quiet(self, path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    def _fsync_dir(self, path: str) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return                       # POSIX-only; Windows raises here
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    # ---------------------------------------------------------- gap queue (§7.1)

    def enqueue_gap(self, start_us: int, end_us: int, *, pass_no: int = 1,
                    not_before_us: Optional[int] = None,
                    tag: Optional[str] = None,
                    clamp: bool = True) -> Optional[str]:
        """Append a durable gap and PERSIST STATE BEFORE RETURNING (SPEC §6.5 — the SSE
        reader must never reconnect with a gap held only in memory).

        Clamps start_us to backfill.max_lookback_days (SPEC §7.9); when the clamp fires it
        logs at ERROR, fires alert_command, counts clamped_gaps and marks the gap.
        Returns None when the gap is skipped: a zero-or-negative-length window is not an
        empty result, it is an HTTP 400 (startDate == endDate), so it must never be queued.

        An overlapping range that is still ``pending`` is *extended* rather than appended
        (see :meth:`_merge_into_pending`), so the returned id may be an existing gap's.

        ``clamp=False`` is for ranges that are *synthesised from the current clock* rather
        than derived from ``hwm_us`` — specifically the weekly completeness audit (SPEC
        §7.8), whose window is ``[now - (audit_span + audit_lag), now - audit_lag]`` and so
        deliberately reaches back 8 days by default. The §7.9 clamp exists to stop a
        corrupted or absent ``hwm_us`` triggering a crawl back to 2013; an audit window is
        bounded by config on both ends and has no runaway path, so clamping it would only
        ever produce a false alarm — and because ``clamped_gaps`` flips health to critical
        (SPEC §11.3), a clamped audit would pin the collector to critical *every week,
        permanently*. Do not pass ``clamp=False`` for anything derived from ``hwm_us``.
        """
        with self._lock:
            return self._enqueue_gap_locked(int(start_us), int(end_us),
                                            pass_no=int(pass_no),
                                            not_before_us=not_before_us,
                                            tag=tag, clamp=bool(clamp))

    def _enqueue_gap_locked(self, start_us: int, end_us: int, *, pass_no: int,
                            not_before_us: Optional[int], tag: Optional[str],
                            clamp: bool) -> Optional[str]:
        """Body of :meth:`enqueue_gap`. `clamp` is False only for the confirming pass 2
        of a gap that has already been accepted and walked once (SPEC §7.7): re-applying
        the §7.9 lookback clamp there would raise a false alarm about a misfire that did
        not happen, pin health critical forever on a cumulative counter, and — for the
        >7-day manual recovery of §11.2 — collapse the pass-2 range to nothing and drop
        the confirming pass entirely.
        """
        clamped = False
        if self._first_run and start_us < self._start_us:
            # SPEC §7.9: with no state there is no history to heal towards. A gap may
            # still cover the time since this process started (the stream is not up yet),
            # but never anything before it.
            self._log.info("first run: gap start pulled forward to process start "
                           "start_us=%d -> %d (SPEC 7.9)", start_us, self._start_us)
            start_us = self._start_us
        if clamp:
            floor = self._now() - self._max_lookback_us
            if start_us < floor:
                clamped = True
                self.bump("clamped_gaps")
                self._log.error("backfill lookback clamp fired: start_us=%d -> %d "
                                "end_us=%d max_lookback_days=%d",
                                start_us, floor, end_us,
                                self._max_lookback_us // _US_PER_DAY)
                self._alert("critical", "lookback_clamp",
                            "backfill lookback clamped start_us=%d floor_us=%d"
                            % (start_us, floor))
                start_us = floor

        if end_us <= start_us:
            # SPEC §6.5: startDate == endDate returns 400, not an empty result.
            self.bump("gaps_skipped")
            self._log.info("skipped zero-length gap start_us=%d end_us=%d clamped=%s",
                           start_us, end_us, clamped)
            if clamped:
                # clamped_gaps flips health to critical (SPEC §11.3), so make the
                # counter durable even though no gap was queued.
                self._save_state()
            return None

        nb = int(not_before_us) if not_before_us is not None else self._now()
        merged = self._merge_into_pending(start_us, end_us, int(pass_no), nb, tag, clamped)
        if merged is not None:
            self.bump("gaps_merged")
            self._save_state()
            self._log.info("merged gap into pending gap_id=%s pass=%s tag=%s "
                           "start_us=%d end_us=%d cursor_us=%d",
                           merged["gap_id"], merged["pass"], merged.get("tag"),
                           merged["start_us"], merged["end_us"], merged["cursor_us"])
            return merged["gap_id"]

        return self._append_gap(start_us, end_us, pass_no, nb, tag, clamped)

    def _merge_into_pending(self, start_us: int, end_us: int, pass_no: int, nb: int,
                            tag: Optional[str], clamped: bool) -> Optional[dict]:
        """Fold an overlapping range into an existing pending gap, or return None.

        Every reconnect anchors its gap at ``hwm_us - safety_lag``, and ``hwm_us`` does not
        advance while the stream is down — so a 30-minute 429 storm queues eight gaps that
        are each a strict superset of the last, and each of those queues a pass-2 clone.
        Sixteen walks of one window, all against the global 5-permit/s bucket that caused
        the 429 in the first place, plus a ``state.json`` that only ever grows. Merging
        keeps the coverage identical (the union of both ranges, with the cursor pulled back
        to any newly-covered earlier time) at one gap.

        Audit gaps never merge, in either direction: ``advance_gap`` credits their rows to
        ``audit_rows_added`` (SPEC §7.8), and folding an ordinary reconnect gap into one
        would corrupt the single number §12.7 calls the most valuable measurement here.
        """
        if tag == "audit":
            return None
        for gap in self._state["gaps"]:
            if not isinstance(gap, dict) or gap.get("status") != "pending":
                continue
            if gap.get("tag") == "audit":
                continue
            try:
                if int(gap.get("pass") or 1) != pass_no:
                    continue
                g_start = int(gap["start_us"])
                g_end = int(gap["end_us"])
                g_cursor = int(gap.get("cursor_us") or g_start)
                g_nb = int(gap.get("not_before_us") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if end_us < g_start or start_us > g_end:
                continue                                   # disjoint: keep them separate
            gap["start_us"] = min(g_start, start_us)
            gap["end_us"] = max(g_end, end_us)
            # Never let the union skip time: if the new range reaches back before the
            # walk got to, the cursor goes back with it. Dedupe makes the re-walk free.
            gap["cursor_us"] = max(gap["start_us"], min(g_cursor, start_us))
            # max(): a pass-2 gap must not be pulled forward past its settle window.
            gap["not_before_us"] = max(g_nb, nb)
            if clamped:
                gap["clamped"] = True
            return gap
        return None

    def _append_gap(self, start_us: int, end_us: int, pass_no: int, nb: int,
                    tag: Optional[str], clamped: bool) -> str:
        with self._lock:
            self._gap_seq += 1
            self._state["gap_seq"] = self._gap_seq
            gap_id = "g%06d" % self._gap_seq
            gap = {
                "gap_id": gap_id,
                "start_us": start_us,
                "end_us": end_us,
                "pass": int(pass_no),
                "attempts": 0,
                "not_before_us": int(nb),
                "cursor_us": start_us,
                "rows": 0,
                "status": "pending",
                "clamped": clamped,
                "tag": tag,
            }
            self._state["gaps"].append(gap)
            self._save_state()
            self._log.info("enqueued gap gap_id=%s pass=%d tag=%s start_us=%d end_us=%d "
                           "not_before_us=%d clamped=%s span_s=%d",
                           gap_id, gap["pass"], tag, start_us, end_us,
                           gap["not_before_us"], clamped, (end_us - start_us) // _US_PER_S)
            return gap_id

    def _find_gap(self, gap_id: str) -> Optional[dict]:
        for gap in self._state["gaps"]:
            if isinstance(gap, dict) and gap.get("gap_id") == gap_id:
                return gap
        return None

    def claim_gap(self) -> Optional[dict]:
        """Oldest runnable pending gap; marks it running and persists before returning."""
        with self._lock:
            now = self._now()
            best = None
            for gap in self._state["gaps"]:
                if not isinstance(gap, dict) or gap.get("status") != "pending":
                    continue
                nb = int(gap.get("not_before_us") or 0)
                if nb > now:
                    continue
                key = (nb, int(gap.get("start_us") or 0))
                if best is None or key < best[0]:
                    best = (key, gap)
            if best is None:
                return None
            gap = best[1]
            gap["status"] = "running"
            gap["attempts"] = int(gap.get("attempts") or 0) + 1
            self._save_state()
            self._log.info("claimed gap gap_id=%s pass=%s attempts=%d cursor_us=%d",
                           gap.get("gap_id"), gap.get("pass"), gap["attempts"],
                           int(gap.get("cursor_us") or 0))
            return dict(gap)             # a copy: mutations must come back through here

    def advance_gap(self, gap_id: str, cursor_us: int, rows_added: int) -> None:
        """Persist the cursor after EVERY successfully written page (SPEC §7.1), so a 429
        storm, a restart or a host reboot resumes mid-gap without re-walking."""
        with self._lock:
            gap = self._find_gap(gap_id)
            if gap is None:
                self._log.error("advance_gap for unknown gap_id=%s", gap_id)
                return
            gap["cursor_us"] = int(cursor_us)
            gap["rows"] = int(gap.get("rows") or 0) + int(rows_added)
            if rows_added:
                if gap.get("tag") == "audit":
                    self.bump("audit_rows_added", int(rows_added))
                elif int(gap.get("pass") or 1) >= 2:
                    self.bump("rows_added_pass2", int(rows_added))
            self._save_state()

    def finish_gap(self, gap_id: str, status: str) -> None:
        """Close a gap and, on success for a pass-1 gap, queue the confirming pass 2 over
        the identical range at end_us + backfill.second_pass_delay_s (SPEC §7.7)."""
        with self._lock:
            gap = self._find_gap(gap_id)
            if gap is None:
                self._log.error("finish_gap for unknown gap_id=%s status=%s", gap_id, status)
                return
            gap["status"] = status
            self._save_state()
            self._log.info("finished gap gap_id=%s status=%s pass=%s rows=%d",
                           gap_id, status, gap.get("pass"), int(gap.get("rows") or 0))

            if status == "done" and int(gap.get("pass") or 1) == 1:
                # Pass 2 is a re-walk of the SAME range once the DB has settled (~1 h).
                # Dedupe makes it idempotent; rows_added_pass2 turns SPEC §12.1's open
                # question into data. Never chains further: pass 2 queues no pass 3.
                # clamp=False: this exact range was already accepted and walked, so
                # re-clamping it would raise a false §7.9 alarm (and, for a gap the
                # operator injected by hand for >7 days of downtime per §11.2, would
                # silently drop the confirming pass altogether).
                self._enqueue_gap_locked(
                    int(gap["start_us"]), int(gap["end_us"]),
                    pass_no=2,
                    not_before_us=int(gap["end_us"]) + self._second_pass_delay_us,
                    tag=gap.get("tag"), clamp=False)
            self._prune_done_gaps()

    def _prune_done_gaps(self) -> None:
        """Keep state.json bounded. Only 'done' gaps are ever dropped — pending, running
        and error gaps are what health checking is built on (SPEC §11.3)."""
        gaps = self._state["gaps"]
        done_idx = [i for i, g in enumerate(gaps)
                    if isinstance(g, dict) and g.get("status") == "done"]
        excess = len(done_idx) - _DONE_GAPS_KEPT
        if excess <= 0:
            return
        drop = set(done_idx[:excess])
        self._state["gaps"] = [g for i, g in enumerate(gaps) if i not in drop]
        self._save_state()

    # ----------------------------------------------------------------- misc API

    def hwm_us(self) -> int:
        """max(ts_us) over every sse / rest-backfill row written (SPEC §6.5)."""
        with self._lock:
            return int(self._state["hwm_us"])

    def dedupe_floor_us(self) -> int:
        """Oldest event time the live dedupe set still covers (SPEC §4.3a).

        A backfill walk whose range starts at or after this instant is fully covered by
        the in-memory window, so it needs no extra machinery. A walk that reaches back
        *before* it is exactly the case SPEC §4.3c exists for: the event-time prune drops
        those keys as the walk advances, so the backfiller must seed a per-gap set of its
        own from the corpus (:meth:`row_ids_in_range`) or write every row twice.
        """
        with self._lock:
            return int(self._max_ts_us_seen) - int(self._dedupe_window_us)

    def row_ids_in_range(self, start_us: int, end_us: int,
                         max_bytes: Optional[int] = None) -> Tuple[Set[str], bool]:
        """row_ids of sse/rest-backfill rows already on disk with `start_us` <= ts <=
        `end_us`. Returns ``(ids, truncated)``; `truncated` means the byte budget ran out
        before the corpus did, so the set is incomplete (SPEC §4.3b's "the data file IS
        the dedupe log", applied to one gap instead of to the window).

        Files are read WITHOUT the writer lock held. A 7-day audit gap covers several
        days of corpus, and blocking every append for the length of that scan would age
        the SSE reader's inbound clock towards a bogus §6.3 stall. That is safe here: data
        files are append-only and are replaced atomically, so the worst case is a partial
        trailing line (skipped) or a file that vanished under us (counted, skipped).
        """
        start_us = int(start_us)
        end_us = int(end_us)
        with self._lock:
            budget = int(self._rehydrate_max_bytes if max_bytes is None else max_bytes)
            # An event can only be *ingested* at or after it happened, so a row in range
            # lives in a file dated no earlier than the event date. One day of margin
            # absorbs clock skew, exactly as rehydration does.
            min_date = tsutil.utc_date(max(0, start_us - _US_PER_DAY))
            files = self._corpus_files_newest_first(min_date)

        ids: Set[str] = set()
        truncated = False
        for name, is_gz in files:
            path = os.path.join(self._data_dir, name)
            try:
                for line in (self._iter_lines_forward(path) if is_gz
                             else self._iter_lines_reverse(path)):
                    budget -= len(line) + 1
                    if budget < 0:
                        truncated = True
                        break
                    try:
                        obj = json.loads(line.decode("utf-8", "replace"))
                        ts_us = int(obj["ts_us"])
                        rid = obj["row_id"]
                    except (ValueError, TypeError, KeyError):
                        continue
                    if (isinstance(rid, str)
                            and start_us <= ts_us <= end_us
                            and obj.get("src") in (record.SRC_SSE, record.SRC_BACKFILL)):
                        ids.add(rid)
            except OSError as exc:
                self._log.warning("gap dedupe seed could not read file=%s err=%s",
                                  name, type(exc).__name__)
            except Exception as exc:            # a corrupt .gz must not stop a backfill
                self._log.warning("gap dedupe seed aborted for file=%s err=%s",
                                  name, type(exc).__name__)
            if truncated:
                break
        if truncated:
            self._log.warning("gap dedupe seed hit its byte budget; the seed is "
                              "incomplete and duplicates are possible start_us=%d "
                              "end_us=%d", start_us, end_us)
        return ids, truncated

    def audit_last_run_us(self) -> int:
        """When the weekly completeness audit last ran, or 0 if never (SPEC §7.8).

        The writer owns state.json, so the audit's schedule lives here rather than in the
        backfiller: a schedule held only in the backfiller's memory would restart the clock
        on every process restart, and a collector restarted daily would then never run the
        audit at all.
        """
        with self._lock:
            return int(self._state.get("audit_last_run_us") or 0)

    def set_audit_last_run(self, now_us: int) -> None:
        """Record an audit run and persist immediately (SPEC §7.8).

        Persisted before the audit gap is walked, deliberately: re-queueing the same
        already-durable audit window after a crash costs a re-walk that dedupe makes free,
        whereas losing the timestamp would re-queue it on every single restart.
        """
        with self._lock:
            self._state["audit_last_run_us"] = int(now_us)
            self._save_state()

    def bump(self, counter: str, n: int = 1) -> None:
        """Increment a counter. 'connections' lives at the top level of state.json per
        SPEC §10; everything else lives under state['counters']."""
        with self._lock:
            if counter in _TOP_LEVEL_COUNTERS:
                self._state[counter] = int(self._state.get(counter) or 0) + int(n)
                return
            counters = self._state["counters"]
            counters[counter] = int(counters.get(counter) or 0) + int(n)

    def tick(self) -> None:
        """Rotation check, gzip drive, periodic fsync and periodic state persist.
        Called from the collector's status loop (SPEC §5.2, §5.3, §10)."""
        with self._lock:
            if self._closed:
                return
            today = self._today()
            # Forward only — see _roll_if_needed. A clock stepped backwards (NTP, a VM
            # resume, a corrected host) must never re-open an already-closed day.
            if self._active_date is None or today > self._active_date:
                self._rotate(today)
            elif today < self._active_date and today not in self._backwards_clock_logged:
                self._backwards_clock_logged.add(today)
                self._log.warning("wall clock is behind the active file, not rolling "
                                  "backwards today=%s active_date=%s",
                                  today, self._active_date)
            self._drive_gzip()
            self._fsync_active(force=False)
            if (self._now() - self._last_persist_us) >= self._persist_interval_us:
                self._save_state()

    def snapshot(self) -> dict:
        """Counters and storage facts for status.json (SPEC §11.3).

        NO chat content, no component, no record body — by construction: every value below
        is a count, a byte offset, a file name or a timestamp.
        """
        with self._lock:
            st = self._state
            counters = dict(st["counters"])
            pending = list(st["pending_gzip"])
            statuses = [g.get("status") for g in st["gaps"] if isinstance(g, dict)]
            try:
                free = shutil.disk_usage(self._data_dir).free
            except OSError as exc:
                free = 0
                self._log.warning("disk_usage failed dir=%s errno=%s",
                                  self._data_dir, getattr(exc, "errno", None))
            audit_us = int(st.get("audit_last_run_us") or 0)
            hwm = int(st["hwm_us"])
            return {
                "hwm_us": hwm,
                "hwm_ts": tsutil.fmt_ts(hwm) if hwm > 0 else None,
                "seq": int(st.get("seq") or 0),
                "connections": int(st.get("connections") or 0),
                "storage": {
                    "active_file": self._active_name,
                    "active_bytes": self._written_bytes,
                    "durable_bytes": self._durable_bytes,
                    "rows_today": self._rows_today,
                    "pending_gzip": len(pending),
                    "gzip_blocked": len(self._gzip_blocked),
                    "repaired_bytes": self._repaired_bytes,
                    "disk_free_bytes": free,
                },
                "backfill": {
                    "gaps_pending": statuses.count("pending"),
                    "gaps_running": statuses.count("running"),
                    "gaps_error": statuses.count("error"),
                    "gaps_done": statuses.count("done"),
                    "rows_added_pass2": int(counters.get("rows_added_pass2") or 0),
                    "audit_rows_added": int(counters.get("audit_rows_added") or 0),
                    "audit_last_run": tsutil.fmt_ts(audit_us) if audit_us > 0 else None,
                },
                "dedupe": {
                    "keys": self._bucket_keys,
                    "buckets": len(self._buckets),
                    "log_keys": self._log_bucket_keys,
                    "log_buckets": len(self._log_buckets),
                    "evictions": int(counters.get("dedupe_evictions") or 0),
                    "window_s": self._dedupe_window_us // _US_PER_S,
                    "max_keys": self._dedupe_max_keys,
                    "rehydrate_truncated": self._rehydrate_truncated,
                    "anchor_us": self._max_ts_us_seen,
                    # The coverage floor, so an operator can see how far back the live
                    # set actually reaches instead of inferring it from the window.
                    "floor_us": int(self._max_ts_us_seen) - int(self._dedupe_window_us),
                    "oldest_bucket_us": (min(self._buckets) * _BUCKET_US
                                         if self._buckets else None),
                },
                "counters": counters,
            }

    def close(self) -> None:
        """Final fsync then final state persist. Idempotent (SPEC §10)."""
        with self._lock:
            if self._closed:
                return
            try:
                # An orderly stop means every issued seq is durable, so the unused part
                # of the reservation is handed back and the next run continues from the
                # exact next number (see next_seq).
                self._state["seq_reserved"] = self._seq
                self._save_state()
            finally:
                if self._fh is not None:
                    try:
                        self._fh.close()
                    except OSError:
                        pass
                    self._fh = None
                self._closed = True
            self._log.info("writer closed active_file=%s active_bytes=%d hwm_us=%d",
                           self._active_name, self._written_bytes,
                           int(self._state["hwm_us"]))

    # ------------------------------------------------------------------- alerts

    def _alert(self, state_name: str, key: str, reason: str) -> None:
        """Fire ops.alert_command with (state_name, reason), once per `key` per process.

        Three properties, each of which was a real failure mode without it (SPEC §11.3,
        §11.4):

        * **Never repeatedly.** SPEC §11.3 says ``alert_command`` runs on a health-state
          *transition* only. This path does not go through ``status.fire_alert``'s state
          machine, so it keeps its own guard: one alert per distinct `key`. A lookback
          clamp used to fire twice for one event (once for the gap, once for its pass-2
          clone milliseconds later), and a gzip verify failure once per process start.
        * **Never under the writer lock.** Every caller here holds it, and
          ``subprocess.run`` waits up to 30 s: an ``alert_command`` wired to a curl/ntfy
          script that blocks on DNS would freeze *all* chat ingest, ``tick()`` and the
          status heartbeat for that whole time. The hook runs on its own short-lived
          daemon thread instead.
        * **Never inherits our stdio.** A hook that reads stdin would block for the full
          timeout, and a chatty hook's output would land in the collector's journal.
        """
        cmd = self._alert_command
        if not cmd:
            return
        if key in self._alerted:
            return
        self._alerted.add(key)
        argv = list(cmd) if isinstance(cmd, (list, tuple)) else [str(cmd)]
        argv += [str(state_name), str(reason)]

        def run() -> None:
            try:
                subprocess.run(argv, timeout=30, shell=False,
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            except Exception as exc:
                self._log.error("alert_command failed key=%s err=%s",
                                key, type(exc).__name__)

        thread = threading.Thread(target=run, name="writer-alert", daemon=True)
        self._alert_thread = thread          # a seam for tests to join on
        thread.start()
