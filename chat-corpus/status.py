"""``status.json`` publication, health evaluation, and operator alerting.

Implements SPEC §11.3 (the status document, the 0/1/2 health thresholds, and
``ops.alert_command`` fired on health-state *transitions* only) and SPEC §11.4
(privacy and log hygiene).

Three things in here are load-bearing and must not be "simplified":

1. **The document is written atomically.** ``status.json`` is rewritten every
   ``ops.status_interval_s`` seconds and is the only thing an external health check
   can see, so a torn or truncated document must be impossible. :func:`atomic_write_json`
   applies exactly the tmp → ``flush`` → ``fsync`` → :func:`os.replace` → directory-fsync
   discipline that SPEC §10 specifies for ``state.json``. It deliberately does *not*
   call ``statefile.save_state``: that function also rotates the previous file to
   ``<path>.bak``, which is correct for durable state and wrong for a disposable
   document rewritten four times a minute (it would litter the corpus directory with a
   ``status.json.bak`` that no reader ever wants and that operators mistake for data).

2. **Alerts fire on transitions only.** SPEC §11.3 is explicit: ``alert_command`` runs on
   a health-state change, never repeatedly. :meth:`StatusPublisher.publish` tracks the
   state and :func:`fire_alert` *additionally* refuses to run the command twice for the
   same state name, so a buggy caller in a 15-second loop still cannot page an operator
   240 times an hour.

3. **No content, anywhere.** No chat body, component body, or record body may reach
   ``status.json``, a log line, or an alert argv (SPEC §11.4). Every ``reason`` string
   this module produces is assembled from counter names, counter values, and state
   names — never from data. :func:`_sanitize_reason` is the last-resort scrub applied
   before a reason is handed to a subprocess.

Two documented additions to the §11.3 nested sections, both metadata-only, both required
by the "a dead worker thread must not leave the process silently half-dead" rule:
``sse.thread_alive`` / ``backfill.thread_alive`` (booleans, or ``None`` when unknown) and
``sse.thread_failure`` / ``backfill.thread_failure`` (the *type name* of the exception
that killed the thread, never its message). ``counters.parse_errors_delta`` is likewise
added so that the §11.3 warn condition "``parse_errors`` rising" is evaluable from a
single document by a one-shot ``--health`` process.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import tsutil

#: Health exit codes, SPEC §11.3.
HEALTH_OK = 0
HEALTH_WARN = 1
HEALTH_CRITICAL = 2

#: Exit code -> the state name written into ``status.json`` and passed as ``argv[1]``.
HEALTH_NAMES = {
    HEALTH_OK: "ok",
    HEALTH_WARN: "warn",
    HEALTH_CRITICAL: "critical",
}  # type: Dict[int, str]

# --- SPEC §11.3 thresholds, verbatim -------------------------------------------------
#: ok requires ``status.json`` newer than 60 s.
STATUS_FRESH_OK_S = 60.0
#: critical when ``status.json`` is stale > 120 s (process wedged or dead).
STATUS_STALE_CRITICAL_S = 120.0
#: ok requires ``s_since_last_inbound < 90``.
INBOUND_DEADLINE_OK_S = 90.0
#: ok requires ``pending_gzip <= 2``.
PENDING_GZIP_OK_MAX = 2
#: critical when ``disk_free < 2 GiB``; ok requires ``disk_free > 2 GiB``.
DISK_FREE_CRITICAL_BYTES = 2 * 1024 * 1024 * 1024
#: warn when ``disk_free < 5 GiB``.
DISK_FREE_WARN_BYTES = 5 * 1024 * 1024 * 1024
#: warn when ``reconnects_last_hour > 20`` (SPEC §11.3: ~96 reconnects/day is *normal*,
#: so only the rate matters).
RECONNECTS_WARN_PER_HOUR = 20
#: warn when ``gaps_pending > 10``.
GAPS_PENDING_WARN = 10
# -------------------------------------------------------------------------------------

#: Hard wall-clock cap on ``ops.alert_command``. Not configurable on purpose: an alert
#: hook that hangs must never be able to stall the status loop, and there is no config
#: key for it in SPEC §9.
ALERT_TIMEOUT_S = 30.0

#: Reasons are one-line operator hints, not reports. Capped so an alert argv can never
#: grow unbounded.
REASON_MAX_CHARS = 240

#: Window over which "``parse_errors`` rising" (SPEC §11.3 warn) is measured. Without a
#: window a single parse error would raise warn for exactly one 15 s tick and then clear,
#: producing two alert transitions for one event.
PARSE_ERROR_WINDOW_S = 300.0

#: Keys the writer may hand up flat rather than nested (``writer.snapshot()``'s exact
#: shape is its own business; SPEC §11.3 fixes only the *document*).
_STORAGE_KEYS = (
    "active_file",
    "active_bytes",
    "rows_today",
    "pending_gzip",
    "disk_free_bytes",
)
_BACKFILL_SNAPSHOT_KEYS = (
    "gaps_pending",
    "gaps_error",
    "gaps_running",
    "last_429_s_ago",
    "rows_added_pass2",
    "audit_rows_added",
    "audit_last_run",
)

# Module-level transition guard for fire_alert(). SPEC §11.3: "on every health-state
# transition only (never repeatedly)".
_ALERT_LOCK = threading.Lock()
_LAST_ALERT_STATE = None  # type: Optional[str]


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------


def cfg_get(cfg: Any, *keys: str, **kw: Any) -> Any:
    """Fetch a nested config value without ever raising. SPEC §9.

    ``config.load_config`` guarantees every documented key exists, but health checking
    must survive a hand-edited or partial dict too — a KeyError here would turn a
    diagnostic into an outage.
    """
    default = kw.get("default")
    cur = cfg
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return default if cur is None else cur


def as_number(value: Any, default: float = 0) -> float:
    """Coerce a status field to a number, defaulting rather than raising. SPEC §11.3."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return value
    return default


def _json_fallback(obj: Any) -> str:
    """Serialize an unexpected object as its *type name*. SPEC §11.4.

    A section value that is not JSON-serializable must not break publication, and must
    not be coerced with ``str()`` either — ``str()`` of an unknown object could carry a
    record body into ``status.json``. Only the type name is emitted.
    """
    return "<%s>" % type(obj).__name__


def _sanitize_reason(reason: str) -> str:
    """Scrub a health reason before it leaves the process. SPEC §11.4.

    Reasons are built from counters and state names by :func:`evaluate_health`, so this
    is defence in depth, not the primary control: collapse whitespace (a multi-line
    reason is a sign something unintended got interpolated), drop control characters,
    and cap the length.
    """
    if not isinstance(reason, str):
        return "<%s>" % type(reason).__name__
    cleaned = "".join((ch if (ch.isprintable() and ch != "\x1f") else " ") for ch in reason)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > REASON_MAX_CHARS:
        cleaned = cleaned[:REASON_MAX_CHARS] + "...(truncated)"
    return cleaned or "no_reason"


def atomic_write_json(path: str, obj: Any) -> None:
    """Write ``obj`` to ``path`` atomically, SPEC §10's discipline applied to §11.3.

    tmp file → ``write`` → ``flush`` → ``os.fsync`` → ``os.replace`` → directory fsync
    (POSIX only; the directory open raises on Windows and is swallowed). A reader
    therefore only ever sees a complete previous document or a complete new one.
    """
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    data = json.dumps(
        obj, indent=1, sort_keys=False, ensure_ascii=True, default=_json_fallback
    ).encode("utf-8")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(directory or ".", os.O_RDONLY)
    except OSError:
        return  # directory fsync is unavailable on Windows (SPEC §10)
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def read_status(path: str) -> Optional[Dict[str, Any]]:
    """Read ``status.json``, or ``None`` if it is missing/unreadable/undecodable.

    SPEC §11.3 — a missing or unparseable status document is the wedged-or-dead signal,
    so the caller treats ``None`` as critical rather than as "unknown".
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    try:
        doc = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


# --------------------------------------------------------------------------------------
# health evaluation
# --------------------------------------------------------------------------------------


def _status_age_s(st: Dict[str, Any]) -> Optional[float]:
    """Age of a status document in seconds from its own ``ts``. SPEC §11.3.

    ``None`` means the timestamp is absent or unparseable, which is itself critical.
    """
    ts = st.get("ts")
    if not isinstance(ts, str):
        return None
    try:
        ts_us = tsutil.parse_ts(ts)
    except tsutil.TsParseError:
        return None
    except Exception:  # a malformed value must never crash a health check
        return None
    age = (tsutil.now_us() - ts_us) / 1000000.0
    return age if age > 0.0 else 0.0


def evaluate_health(st: Dict[str, Any], cfg: Dict[str, Any]) -> Tuple[int, str]:
    """Grade a status document. SPEC §11.3 (thresholds verbatim).

    Returns ``(code, reason)`` where ``code`` is 0 ok / 1 warn / 2 critical and ``reason``
    is a short operator hint built **only** from counter names, counter values, and state
    names (SPEC §11.4 — never chat content, never a record body).

    Order of evaluation:

    * **critical (2)** — ``status.json`` stale > 120 s (or its ``ts`` unusable); or
      ``gaps_error > 0``; or ``clamped_gaps > 0``; or ``disk_free < 2 GiB``. Plus one
      documented addition: an enabled worker thread that is not alive, which is the same
      "wedged or dead" condition seen from the inside instead of from the outside.
    * **warn (1)** — ``reconnects_last_hour > 20``; or ``gaps_pending > 10``; or
      ``dedupe_evictions > 0``; or ``tie_skips > 0``; or ``parse_errors`` rising; or
      ``disk_free < 5 GiB``.
    * **ok (0)** — every ok precondition holds: document newer than 60 s, SSE
      ``connected``, ``s_since_last_inbound < 90``, ``gaps_error == 0``,
      ``pending_gzip <= 2``, ``disk_free > 2 GiB``. A document that trips none of the
      warn conditions but fails an ok precondition is reported as warn — ok is defined by
      its preconditions, so "not ok and not critical" is exactly warn.
    """
    if not isinstance(st, dict):
        return HEALTH_CRITICAL, "status_document_unreadable"

    sse = st.get("sse") if isinstance(st.get("sse"), dict) else {}
    backfill = st.get("backfill") if isinstance(st.get("backfill"), dict) else {}
    storage = st.get("storage") if isinstance(st.get("storage"), dict) else {}
    counters = st.get("counters") if isinstance(st.get("counters"), dict) else {}
    process = st.get("process") if isinstance(st.get("process"), dict) else {}

    sse_enabled = bool(cfg_get(cfg, "sse", "enabled", default=True))
    backfill_enabled = bool(cfg_get(cfg, "backfill", "enabled", default=True))

    age_s = _status_age_s(st)
    gaps_error = as_number(backfill.get("gaps_error"))
    gaps_pending = as_number(backfill.get("gaps_pending"))
    clamped_gaps = as_number(counters.get("clamped_gaps"))
    dedupe_evictions = as_number(counters.get("dedupe_evictions"))
    tie_skips = as_number(counters.get("tie_skips"))
    parse_errors_delta = as_number(counters.get("parse_errors_delta"))
    disk_free = as_number(storage.get("disk_free_bytes"), default=-1)
    pending_gzip = as_number(storage.get("pending_gzip"))
    reconnects = as_number(sse.get("reconnects_last_hour"))

    critical = []  # type: List[str]
    if age_s is None:
        critical.append("status_ts_unusable")
    elif age_s > STATUS_STALE_CRITICAL_S:
        critical.append("status_stale_s=%d" % int(age_s))
    if "running" in process and process.get("running") is not True:
        # A process that is dying still publishes one last document with a fresh ``ts``,
        # so the §11.3 "stale > 120 s == wedged or dead" detector never fires in a crash
        # loop: every restart refreshes the file five seconds later. The dying process
        # says so explicitly instead. (Absent on documents from older builds, which is
        # why the key is tested for presence rather than defaulted to True.)
        critical.append("process_not_running=%s" % _short_token(process.get("exit_reason")))
    if gaps_error > 0:
        critical.append("gaps_error=%d" % int(gaps_error))
    if clamped_gaps > 0:
        critical.append("clamped_gaps=%d" % int(clamped_gaps))
    if 0 <= disk_free < DISK_FREE_CRITICAL_BYTES:
        critical.append("disk_free_bytes=%d" % int(disk_free))
    # ``is not True`` rather than ``is False``: the field is three-state, and ``None``
    # means the worker was never registered at all — an enabled source whose thread was
    # never started collects exactly as much as one that died.
    if sse_enabled and sse.get("thread_alive") is not True:
        critical.append("sse_thread_%s" % _thread_state_name(sse))
    if backfill_enabled and backfill.get("thread_alive") is not True:
        critical.append("backfill_thread_%s" % _thread_state_name(backfill))
    if critical:
        return HEALTH_CRITICAL, _sanitize_reason(", ".join(critical))

    warn = []  # type: List[str]
    if reconnects > RECONNECTS_WARN_PER_HOUR:
        warn.append("reconnects_last_hour=%d" % int(reconnects))
    if gaps_pending > GAPS_PENDING_WARN:
        warn.append("gaps_pending=%d" % int(gaps_pending))
    if dedupe_evictions > 0:
        warn.append("dedupe_evictions=%d" % int(dedupe_evictions))
    if tie_skips > 0:
        warn.append("tie_skips=%d" % int(tie_skips))
    if parse_errors_delta > 0:
        warn.append("parse_errors_rising=%d" % int(parse_errors_delta))
    if 0 <= disk_free < DISK_FREE_WARN_BYTES:
        warn.append("disk_free_bytes=%d" % int(disk_free))
    elif disk_free < 0:
        # -1 is what the publisher writes when shutil.disk_usage raised. §11.3 defines ok
        # as "disk_free > 2 GiB"; a document that cannot say how much space is left
        # cannot satisfy that precondition, so it is not ok.
        warn.append("disk_free_unknown")
    if warn:
        return HEALTH_WARN, _sanitize_reason(", ".join(warn))

    not_ok = []  # type: List[str]
    if age_s is not None and age_s > STATUS_FRESH_OK_S:
        not_ok.append("status_age_s=%d" % int(age_s))
    if sse_enabled:
        state_name = sse.get("state")
        if state_name != "connected":
            not_ok.append(
                "sse_state=%s" % (state_name if isinstance(state_name, str) else "unknown")
            )
        since_inbound = sse.get("s_since_last_inbound")
        if isinstance(since_inbound, (int, float)) and not isinstance(since_inbound, bool):
            if since_inbound >= INBOUND_DEADLINE_OK_S:
                not_ok.append("s_since_last_inbound=%d" % int(since_inbound))
        elif state_name == "connected":
            # §11.3's ok requires ``s_since_last_inbound < 90``. A connected stream that
            # is not reporting it cannot satisfy that precondition either.
            not_ok.append("s_since_last_inbound=unknown")
    if pending_gzip > PENDING_GZIP_OK_MAX:
        not_ok.append("pending_gzip=%d" % int(pending_gzip))
    # The remaining two ok preconditions of §11.3 — gaps_error == 0 and disk_free > 2 GiB —
    # are already subsumed: both are critical conditions checked above, and any disk_free
    # at or below 2 GiB is also below the 5 GiB warn threshold.
    if not_ok:
        return HEALTH_WARN, _sanitize_reason(", ".join(not_ok))

    return HEALTH_OK, "ok"


def _short_token(value: Any) -> str:
    """A short, alphanumeric-ish token for a reason string. SPEC §11.4."""
    if not isinstance(value, str) or not value:
        return "unknown"
    return "".join(ch for ch in value if ch.isalnum() or ch in "._-")[:48] or "unknown"


def _thread_state_name(section: Dict[str, Any]) -> str:
    """``dead=<Type>`` for a thread that died, ``never_started`` for one that never ran.

    The distinction matters operationally: "died" points at the worker, "never started"
    points at construction — the import, the config, or a failure between building the
    publisher and registering the thread.
    """
    if section.get("thread_alive") is None and not section.get("thread_failure"):
        return "never_started"
    return "dead=%s" % _thread_failure_name(section)


def _thread_failure_name(section: Dict[str, Any]) -> str:
    """Exception *type name* recorded for a dead worker thread. SPEC §11.4.

    Never the exception message: a sibling module's message could interpolate a record
    body, and this string ends up in ``status.json`` and in an alert argv.
    """
    name = section.get("thread_failure")
    if isinstance(name, str) and name:
        return "".join(ch for ch in name if ch.isalnum() or ch in "._-")[:64] or "unknown"
    return "exited"


# --------------------------------------------------------------------------------------
# alerting
# --------------------------------------------------------------------------------------


def _build_argv(command: Any, state_name: str, reason: str) -> Optional[List[str]]:
    """Turn ``ops.alert_command`` into an argv. SPEC §11.3.

    ``argv[1]`` is the state name, ``argv[2]`` is the reason — nothing else is ever
    appended, and the command is never run through a shell. A list config is used
    verbatim; a string that names an existing file is treated as a single token (so
    ``C:/Program Files/notify.exe`` survives), otherwise it is split with
    :func:`shlex.split` in the platform's own mode.
    """
    if command is None:
        return None
    if isinstance(command, (list, tuple)):
        parts = [str(part) for part in command if part is not None]
    elif isinstance(command, str):
        text = command.strip()
        if not text:
            return None
        if os.path.exists(text):
            parts = [text]
        else:
            try:
                parts = shlex.split(text, posix=(os.name != "nt"))
            except ValueError:
                parts = [text]
            parts = [part.strip('"') for part in parts if part]
    else:
        return None
    if not parts:
        return None
    return parts + [state_name, reason]


def fire_alert(cfg: Dict[str, Any], log: Any, state_name: str, reason: str) -> bool:
    """Run ``ops.alert_command`` for a health-state transition. SPEC §11.3, §11.4.

    ``argv[1]`` is the state name, ``argv[2]`` is a one-line reason built from counters
    and state names only — never content. Guarantees:

    * **Transitions only.** A module-level guard drops a call whose ``state_name``
      matches the last state actually alerted on, so repeating the call in the 15 s
      status loop cannot page an operator repeatedly.
    * **Cannot hang the collector.** ``subprocess.run`` gets ``timeout=ALERT_TIMEOUT_S``,
      a closed stdin, and discarded stdout/stderr (a hook's output is arbitrary text and
      must never be relayed into ``collector.log``).
    * **Cannot kill the collector.** Every failure mode — missing command, permission
      denied, non-zero exit, timeout — is logged as metadata and swallowed.

    Returns True when the transition has been dealt with (delivered, deliberately
    suppressed as a repeat, or no command configured) and **False when the command could
    not be run**. A failed or timed-out hook must not consume the transition: the caller
    keeps it pending and tries again, otherwise the one page that mattered — ok →
    critical — is lost for good, because health stays critical and no further transition
    ever occurs.
    """
    global _LAST_ALERT_STATE

    command = cfg_get(cfg, "ops", "alert_command", default=None)
    safe_reason = _sanitize_reason(reason)
    safe_state = "".join(ch for ch in str(state_name) if ch.isalnum() or ch in "._-")[:32]
    if not safe_state:
        safe_state = "unknown"

    with _ALERT_LOCK:
        if _LAST_ALERT_STATE == safe_state:
            return True
        previous_state = _LAST_ALERT_STATE
        _LAST_ALERT_STATE = safe_state

    def _unclaim() -> None:
        """Hand the transition back so it can be retried."""
        global _LAST_ALERT_STATE
        with _ALERT_LOCK:
            if _LAST_ALERT_STATE == safe_state:
                _LAST_ALERT_STATE = previous_state

    argv = _build_argv(command, safe_state, safe_reason)
    if argv is None:
        if log is not None:
            log.info("alert suppressed: no alert_command configured state=%s", safe_state)
        return True

    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=ALERT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        if log is not None:
            log.error(
                "alert_command timed out state=%s timeout_s=%s", safe_state, ALERT_TIMEOUT_S
            )
        _unclaim()
        return False
    except OSError as exc:
        if log is not None:
            log.error(
                "alert_command failed to start state=%s errno=%s exc=%s",
                safe_state,
                getattr(exc, "errno", None),
                type(exc).__name__,
            )
        _unclaim()
        return False
    except Exception as exc:  # an alert must never be able to kill the collector
        if log is not None:
            log.error(
                "alert_command raised state=%s exc=%s", safe_state, type(exc).__name__
            )
        _unclaim()
        return False

    if log is not None:
        if completed.returncode == 0:
            log.info("alert_command ok state=%s", safe_state)
        else:
            log.error(
                "alert_command exited non-zero state=%s rc=%s",
                safe_state,
                completed.returncode,
            )
    # A non-zero exit is the hook's own business: it ran, so the transition is delivered.
    return True


def reset_alert_state() -> None:
    """Forget the last alerted state so the next :func:`fire_alert` is a transition.

    Used by tests and by a fresh :class:`StatusPublisher` inside one process. SPEC §11.3.
    """
    global _LAST_ALERT_STATE
    with _ALERT_LOCK:
        _LAST_ALERT_STATE = None


# --------------------------------------------------------------------------------------
# publisher
# --------------------------------------------------------------------------------------


class StatusPublisher:
    """Builds and atomically publishes ``status.json``. SPEC §11.3.

    Producer threads call :meth:`update` with metadata for their own section; the status
    loop calls :meth:`publish` every ``ops.status_interval_s`` seconds. All methods are
    thread-safe. Nothing this class writes or logs contains record content (SPEC §11.4).
    """

    def __init__(self, cfg: Dict[str, Any], log: Any) -> None:
        """Initialise the document skeleton. SPEC §11.3."""
        self._cfg = cfg
        self._log = log
        self._lock = threading.RLock()
        self._started_us = tsutil.now_us()
        self._path = str(cfg_get(cfg, "status_path", default="status.json"))
        self._data_dir = str(cfg_get(cfg, "data_dir", default="."))
        self._sections = {
            "sse": {
                "state": "starting",
                "connections": 0,
                "s_since_last_inbound": None,
                "s_since_last_event": None,
                "heartbeats": 0,
                "reconnects_last_hour": 0,
                "last_close_mode": None,
                "thread_alive": None,
                "thread_failure": None,
            },
            "backfill": {
                "gaps_pending": 0,
                "gaps_error": 0,
                "gaps_running": 0,
                "last_429_s_ago": None,
                "rows_added_pass2": 0,
                "audit_rows_added": 0,
                "audit_last_run": None,
                "thread_alive": None,
                "thread_failure": None,
            },
            "log_tail": {"enabled": bool(cfg_get(cfg, "log_tail", "enabled", default=False))},
            "storage": {
                "active_file": None,
                "active_bytes": 0,
                "rows_today": 0,
                "pending_gzip": 0,
                "disk_free_bytes": 0,
            },
            "counters": {},
        }  # type: Dict[str, Dict[str, Any]]
        self._health_code = None  # type: Optional[int]
        self._health_reason = ""
        # SPEC §6.3's initial grace, reused as an alert grace: a supervisor restart
        # (SPEC §11.2) must not page the operator while the SSE stream is still
        # connecting. Critical conditions during the grace are tracked but not alerted;
        # if they persist past the grace, the first post-grace evaluation alerts.
        self._alert_grace_s = float(
            as_number(cfg_get(cfg, "sse", "initial_grace_s", default=120), default=120)
        )
        #: (monotonic, parse_errors) samples over the sliding PARSE_ERROR_WINDOW_S.
        self._parse_error_samples = []  # type: List[Tuple[float, float]]
        self._publish_errors = 0
        #: An alert whose command could not be run, kept for the next publication.
        self._alert_pending = None  # type: Optional[Tuple[str, str]]

    # -- producer side ---------------------------------------------------------------

    def update(self, section: str, **fields: Any) -> None:
        """Merge ``fields`` into ``section`` of the pending document. SPEC §11.3.

        Thread-safe. Callers pass metadata only — counters, state names, connection
        ordinals, byte counts, seconds-since values (SPEC §11.4).
        """
        key = str(section)
        with self._lock:
            target = self._sections.get(key)
            if target is None:
                target = {}
                self._sections[key] = target
            target.update(fields)

    def snapshot(self) -> Dict[str, Any]:
        """Return the current document without consulting the writer. SPEC §11.3.

        Storage facts and counters are whatever was last merged from
        ``writer.snapshot()``; use :meth:`publish` for a freshly sourced document.
        """
        return self._build_doc(None)

    # -- publication -----------------------------------------------------------------

    def publish(self, writer: Any) -> None:
        """Build, grade, and atomically write ``status.json``. SPEC §11.3.

        Never raises: a full disk or a read-only directory must degrade to a log line,
        not to a dead collector. Fires ``ops.alert_command`` when the health state
        changed since the previous publication.
        """
        doc = self._build_doc(writer)
        try:
            code, reason = evaluate_health(doc, self._cfg)
        except Exception as exc:  # grading must never take the process down
            code, reason = HEALTH_CRITICAL, "health_evaluation_failed=%s" % type(exc).__name__
        doc["health"] = HEALTH_NAMES.get(code, "critical")

        try:
            atomic_write_json(self._path, doc)
        except OSError as exc:
            self._publish_errors += 1
            if self._log is not None:
                self._log.error(
                    "status publish failed path=%s errno=%s exc=%s failures=%d",
                    self._path,
                    getattr(exc, "errno", None),
                    type(exc).__name__,
                    self._publish_errors,
                )
        except Exception as exc:
            self._publish_errors += 1
            if self._log is not None:
                self._log.error(
                    "status publish failed path=%s exc=%s failures=%d",
                    self._path,
                    type(exc).__name__,
                    self._publish_errors,
                )

        self._maybe_alert(code, reason, as_number(doc.get("uptime_s"), default=0))

    def health(self) -> Tuple[int, str]:
        """Last graded health as ``(code, reason)``. SPEC §11.3."""
        with self._lock:
            code = HEALTH_OK if self._health_code is None else self._health_code
            return code, self._health_reason or "ok"

    # -- internals -------------------------------------------------------------------

    def _build_doc(self, writer: Any) -> Dict[str, Any]:
        """Assemble the §11.3 document. Key order is the spec's key order."""
        with self._lock:
            sse = dict(self._sections.get("sse", {}))
            backfill = dict(self._sections.get("backfill", {}))
            log_tail = dict(self._sections.get("log_tail", {}))
            storage = dict(self._sections.get("storage", {}))
            counters = dict(self._sections.get("counters", {}))
            started_us = self._started_us
            last_health = self._health_code

        if writer is not None:
            self._merge_writer_snapshot(writer, storage, counters, backfill)

        if as_number(storage.get("disk_free_bytes"), default=0) <= 0:
            storage["disk_free_bytes"] = self._disk_free_bytes()
        pending = storage.get("pending_gzip")
        if isinstance(pending, (list, tuple)):
            storage["pending_gzip"] = len(pending)

        counters["parse_errors_delta"] = self._parse_errors_delta(counters)

        now_us = tsutil.now_us()
        doc = {
            "ts": tsutil.fmt_ts(now_us),
            "pid": os.getpid(),
            "uptime_s": int(max(0, now_us - started_us) // 1000000),
            "sse": sse,
            "backfill": backfill,
            "log_tail": log_tail,
            "storage": storage,
            "counters": counters,
            "health": HEALTH_NAMES.get(
                HEALTH_OK if last_health is None else last_health, "ok"
            ),
        }
        return doc

    def _merge_writer_snapshot(
        self,
        writer: Any,
        storage: Dict[str, Any],
        counters: Dict[str, Any],
        backfill: Dict[str, Any],
    ) -> None:
        """Fold ``writer.snapshot()`` into the document. SPEC §11.3.

        The writer owns the counters, the active file facts and the gap queue, and its
        snapshot contains no chat content by contract. Accepted either nested
        (``{"counters": {...}, "storage": {...}}``) or flat, because the document shape
        is fixed by the spec while the snapshot shape is the writer's own affair.

        **Precedence: the writer wins.** For any key the writer reports, its value
        overrides whatever a producer thread merged via :meth:`update` — the writer holds
        ``state["gaps"]``, the dedupe counters and the file handles, so it is the single
        authority for those numbers. Keys the writer does not report (``sse.*``,
        ``backfill.last_429_s_ago``, ``backfill.audit_last_run``, the thread-liveness
        fields) are left exactly as the producer set them.
        """
        try:
            snap = writer.snapshot()
        except Exception as exc:
            if self._log is not None:
                self._log.error("writer snapshot failed exc=%s", type(exc).__name__)
            return
        if not isinstance(snap, dict):
            return

        nested_counters = snap.get("counters")
        if isinstance(nested_counters, dict):
            counters.update(nested_counters)
        nested_storage = snap.get("storage")
        if isinstance(nested_storage, dict):
            storage.update(nested_storage)
        nested_backfill = snap.get("backfill")
        if isinstance(nested_backfill, dict):
            for key in _BACKFILL_SNAPSHOT_KEYS:
                if key in nested_backfill:
                    backfill[key] = nested_backfill[key]

        for key in _STORAGE_KEYS:
            if key in snap:
                storage[key] = snap[key]
        for key in _BACKFILL_SNAPSHOT_KEYS:
            if key in snap:
                backfill[key] = snap[key]
        for key, value in snap.items():
            if key.startswith(("rows_", "dupes_", "parse_", "tie_", "clamped_", "dedupe_")):
                counters.setdefault(key, value)

    def _disk_free_bytes(self) -> int:
        """Free bytes on the corpus filesystem. SPEC §11.3 (``disk_free_bytes``)."""
        try:
            return int(shutil.disk_usage(self._data_dir or ".").free)
        except OSError:
            return -1
        except Exception:
            return -1

    def _parse_errors_delta(self, counters: Dict[str, Any]) -> int:
        """Growth in ``parse_errors`` over the measurement window. SPEC §11.3 (warn).

        The §11.3 warn condition is "``parse_errors`` rising", which a single document
        cannot express — so the growth is measured here over a **sliding**
        :data:`PARSE_ERROR_WINDOW_S` window and published as
        ``counters.parse_errors_delta`` for a one-shot ``--health`` process to read.

        Sliding, not a periodically reset baseline. Resetting the baseline to the current
        value returns 0 on the reset tick, so a *sustained* fault (parse errors arriving
        continuously) reads as ok for one tick every window: warn → ok → warn forever,
        which is a false all-clear plus a repeat page every five minutes for one
        continuous condition — precisely the "never repeatedly" behaviour §11.3 forbids.
        """
        current = as_number(counters.get("parse_errors"), default=0)
        now_mono = time.monotonic()
        with self._lock:
            samples = self._parse_error_samples
            samples.append((now_mono, current))
            cutoff = now_mono - PARSE_ERROR_WINDOW_S
            # Keep one sample at or before the cutoff so the window is a true lower bound.
            while len(samples) > 2 and samples[1][0] <= cutoff:
                samples.pop(0)
            oldest = samples[0][1]
            delta = current - oldest
        return int(delta) if delta > 0 else 0

    def _maybe_alert(self, code: int, reason: str, uptime_s: float) -> None:
        """Fire ``ops.alert_command`` on a health-state transition only. SPEC §11.3.

        During the startup grace the state is *evaluated and published* but deliberately
        not adopted as the baseline: adopting it would let a condition that has been
        critical since boot count as "no transition" and never alert, while alerting
        immediately would page the operator on every supervised restart (SPEC §11.2).
        The first evaluation after the grace therefore alerts if the state is not ok.
        """
        pending = None
        with self._lock:
            pending, self._alert_pending = self._alert_pending, None
        if pending is not None:
            # A previous transition whose hook could not be run. Retry it before grading
            # anything new, so a DNS/TLS timeout on the ok -> critical page costs one
            # status interval instead of the whole incident.
            if not fire_alert(self._cfg, self._log, pending[0], pending[1]):
                with self._lock:
                    self._alert_pending = pending

        with self._lock:
            self._health_reason = reason
            if self._health_code is None and uptime_s < self._alert_grace_s:
                # Still inside the startup grace and no baseline adopted yet. Publish the
                # state, say so in the log, and alert on nothing.
                if self._log is not None and code != HEALTH_OK:
                    self._log.info(
                        "health=%s during startup grace (no alert) reason=%s",
                        HEALTH_NAMES.get(code, "critical"),
                        reason,
                    )
                return
            previous = self._health_code
            if previous == code:
                return
            self._health_code = code

        state_name = HEALTH_NAMES.get(code, "critical")
        if self._log is not None:
            message = "health transition from=%s to=%s reason=%s"
            args = (
                HEALTH_NAMES.get(previous, "none") if previous is not None else "none",
                state_name,
                reason,
            )
            if code == HEALTH_CRITICAL:
                self._log.critical(message, *args)
            elif code == HEALTH_WARN:
                self._log.warning(message, *args)
            else:
                self._log.info(message, *args)

        if previous is None and code == HEALTH_OK:
            # First graded state is ok: adopt it silently, there is nothing to report.
            return
        if not fire_alert(self._cfg, self._log, state_name, reason):
            with self._lock:
                self._alert_pending = (state_name, reason)


# --------------------------------------------------------------------------------------
# --health
# --------------------------------------------------------------------------------------


def run_health_check(cfg: Dict[str, Any]) -> int:
    """Grade ``status.json`` from outside the process. SPEC §11.3.

    Returns the exit code ``collector.py --health`` exits with: 0 ok, 1 warn, 2 critical.
    A missing, unreadable, undecodable or stale (> 120 s) status document is **critical**
    — that is the wedged-or-dead signal, and it is the only thing an external supervisor
    can use to tell a hung collector from a healthy quiet one.
    """
    path = str(cfg_get(cfg, "status_path", default="status.json"))
    doc = read_status(path)
    if doc is None:
        return HEALTH_CRITICAL
    try:
        code, _reason = evaluate_health(doc, cfg)
    except Exception:
        return HEALTH_CRITICAL
    return code


def describe_health(cfg: Dict[str, Any]) -> Tuple[int, str]:
    """``(code, reason)`` for ``--health``'s one-line operator output. SPEC §11.3, §11.4.

    Kept next to :func:`run_health_check` so the printed reason and the exit code are
    derived from the same document read.
    """
    path = str(cfg_get(cfg, "status_path", default="status.json"))
    doc = read_status(path)
    if doc is None:
        return HEALTH_CRITICAL, "status_file_missing_or_unreadable"
    try:
        return evaluate_health(doc, cfg)
    except Exception as exc:
        return HEALTH_CRITICAL, "health_evaluation_failed=%s" % type(exc).__name__
