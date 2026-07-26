"""Entry point: CLI, config load, single-instance lock, worker supervision, status loop.

Implements SPEC §11.2 (restart behaviour, in order), §11.3 (the 15 s status/tick loop and
``--health``), §11.4 (privacy and log hygiene) and §9 (configuration surface, including
``--print-config`` so an operator can see the merged result of file + ``CHATCOL_*`` env).

Startup order is SPEC §11.2 and is not an accident:

1. Acquire the single-instance lock; exit **3** if held, printing the holder's PID. Two
   collectors on one IP would double the ``/feed/*`` connection count against the edge's
   cap of 6 (SPEC §10, §12.12), so this is a correctness requirement, not hygiene.
2. Load ``state.json`` (or ``.bak``). If neither is usable this is a first run:
   ``hwm_us = now`` and **no backfill at all** (SPEC §7.9) — the clamp exists so a bad or
   absent high-water mark can never trigger a 2013-onward crawl.
3. Construct :class:`writer.Writer`, which performs the startup repair of the active file,
   the stray ``*.gz.tmp`` cleanup, the ``pending_gzip`` re-drive and the ``hwm_us``-anchored
   dedupe rehydration (SPEC §5.4, §5.3, §4.3b).
4. Enqueue the startup gap ``[hwm_us - safety_lag, now + 5 s]``, persisted before anything
   connects (SPEC §6.5).
5. Start the backfill thread, *then* the SSE stream (SPEC §11.2 step 7).
6. Run the status/tick loop at ``ops.status_interval_s``.

Two operational rules this module enforces:

* **A thread's exception can never vanish.** Each worker's ``run`` is wrapped so any
  escaping exception is recorded as a failure and logged as *metadata only*, and every
  tick checks ``is_alive()``. A dead worker flips health to critical (SPEC §11.3) instead
  of leaving a process that looks up but collects nothing; if *every* worker is dead the
  process exits non-zero so the supervisor (`Restart=always`, SPEC §11.2) restarts it.
* **Privacy is enforced at the logging layer too.** :class:`PrivacyFilter` is installed on
  the root logger and on every handler as the last line of defence for SPEC §11.4: it
  drops tracebacks (an exception *message* from any module could interpolate a record
  body), collapses a record to one line, masks coordinate-shaped text, and caps length.
  The primary defence remains never passing content to a logger in the first place.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import threading
import time
import traceback
from typing import Any, Dict, List, Optional

import config
import status
import tsutil

PROGRAM_NAME = "chat-corpus-collector"
VERSION = "1.0.0"

#: Process exit codes. 3 is fixed by SPEC §11.2/§10 (lock held by another collector).
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2
EXIT_LOCKED = 3

#: Seconds allowed for all worker threads to join on shutdown before they are abandoned.
#: The SSE socket read timeout is 20 s (SPEC §6.1), so a reader parked in ``read()`` needs
#: room to notice the stop event.
JOIN_TIMEOUT_S = 25.0

#: SPEC §6.5 — the startup/reconnect gap overshoots the present by 5 s. Overshoot is free:
#: the overlap rows are inside the dedupe window by construction.
GAP_END_OVERSHOOT_US = 5000000

#: Hard cap on a single log message (SPEC §11.4 defence in depth).
MAX_LOG_MESSAGE_CHARS = 600

#: Hard cap on one ``%``-formatted log argument. No legitimate argument in this program is
#: this long; a `component` value is 495–532 chars (SPEC §11.1), so the cap bites first.
MAX_LOG_ARG_CHARS = 200

_MASK = "<redacted>"

#: Three bare numbers in a row is the shape a leaked coordinate would take. Our own log
#: lines never contain one (counters are always ``name=value``), so masking it costs
#: nothing and closes the most likely accident.
_COORD_TRIPLE_RE = re.compile(
    r"(?<![\w.:=/-])-?\d{2,8}\s*[,/ ]\s*-?\d{1,4}\s*[,/ ]\s*-?\d{2,8}(?![\w.])"
)
#: ``x: 123``/``z=-456`` shaped text.
_XYZ_RE = re.compile(r"\b[xXyYzZ]\s*[:=]\s*-?\d+(?:\.\d+)?")


# --------------------------------------------------------------------------------------
# privacy-enforcing logging
# --------------------------------------------------------------------------------------


def _scrub_log_text(message: Any) -> str:
    """Reduce a log message to a single safe line. SPEC §11.4.

    Collapses whitespace and control characters (a multi-line log record is a strong
    signal that something unintended was interpolated), masks coordinate-shaped text, and
    truncates. A non-string message is replaced by its type name rather than ``str()``-ed,
    because ``str()`` of an unknown object can carry a record body.
    """
    if not isinstance(message, str):
        return "<%s>" % type(message).__name__
    text = "".join((ch if ch.isprintable() else " ") for ch in message)
    text = " ".join(text.split())
    text = _XYZ_RE.sub(_MASK, text)
    text = _COORD_TRIPLE_RE.sub(_MASK, text)
    if len(text) > MAX_LOG_MESSAGE_CHARS:
        dropped = len(text) - MAX_LOG_MESSAGE_CHARS
        text = text[:MAX_LOG_MESSAGE_CHARS] + " ...(truncated, dropped=%d chars)" % dropped
    return text


def _safe_scalar(value: Any) -> Any:
    """Reduce a log *argument* to something that cannot be a record body. SPEC §11.4.

    Numbers, booleans, ``None`` and short strings pass through — that is what counters,
    ``row_id``s, offsets and state names are. Everything else (a dict, a list, a record, a
    component object) becomes its type name, because ``logging`` would otherwise ``str()``
    it straight into the file: ``log.error("bad record %s", rec)`` is the single most likely
    way a body escapes, and it must not work.
    """
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        if len(value) > MAX_LOG_ARG_CHARS:
            return value[:MAX_LOG_ARG_CHARS] + "...(arg truncated)"
        return value
    return "<%s>" % type(value).__name__


def _safe_mapping_value(value: Any) -> Any:
    """Reduce a value inside a dict-shaped log argument. SPEC §11.4.

    Stricter than :func:`_safe_scalar`: strings are replaced by ``<str>`` as well. When
    ``logging`` is handed a single mapping it keeps it *as* the args, so
    ``log.info("record dump %s", rec)`` formats the whole dict — a canonical record has its
    body in a string value, and there is no way to tell that value from a benign one. This
    program never uses ``%(key)s`` mapping-style logging, so nothing is lost by refusing to
    render string values from a dict at all.
    """
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return "<%s>" % type(value).__name__


def _safe_args(args: Any) -> Any:
    """Apply :func:`_safe_scalar` across a log record's args. SPEC §11.4."""
    if args is None:
        return None
    if isinstance(args, dict):
        return dict((key, _safe_mapping_value(val)) for key, val in args.items())
    if isinstance(args, tuple):
        return tuple(_safe_scalar(val) for val in args)
    return _safe_scalar(args)


class PrivacyFilter(logging.Filter):
    """Last line of defence for SPEC §11.4 — no record body ever reaches a log sink.

    Installed on the root logger *and* on every handler, so a record emitted by any module
    (including one that used ``logging.getLogger(__name__)`` instead of the injected
    logger) is scrubbed before it is formatted:

    * a non-string ``msg`` becomes its type name **without** being ``str()``-ed, and every
      argument goes through :func:`_safe_scalar` *before* ``%``-formatting. Deferring to
      ``record.getMessage()`` would be too late: it stringifies the object first, which is
      precisely the leak.
    * the rendered line is scrubbed by :func:`_scrub_log_text` and re-attached with
      ``args = None``, so no later ``%``-formatting can reintroduce anything;
    * ``exc_info``/``exc_text``/``stack_info`` are **dropped**. A traceback's final line is
      the exception's message, and an exception message from a sibling module could
      contain a chat body — SPEC §11.4 calls that a defect, so tracebacks are not emitted
      at all. Code that needs exception detail logs :func:`_exc_meta` instead, which is
      type names and ``file:line:function`` only.

    This is a backstop, not a content classifier: a caller that deliberately passes a chat
    string as an argument will still log it. The primary rule stands — never hand content
    to a logger.

    Scrubbing is idempotent: a record already processed is passed straight through, so
    installing the same filter on two handlers cannot double-truncate.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Scrub ``record`` in place; always returns True. SPEC §11.4."""
        if getattr(record, "_privacy_scrubbed", False):
            return True
        msg = record.msg
        if not isinstance(msg, str):
            rendered = "<%s>" % type(msg).__name__
        else:
            safe_args = _safe_args(record.args)
            if safe_args:
                try:
                    rendered = msg % safe_args
                except Exception:
                    rendered = msg + " <args unformattable>"
            else:
                rendered = msg
        record.msg = _scrub_log_text(rendered)
        record.args = None
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        record._privacy_scrubbed = True  # type: ignore[attr-defined]
        return True


def _exc_meta(exc: BaseException) -> str:
    """Describe an exception using metadata only. SPEC §11.4.

    Returns the exception type, up to the last four traceback frames as
    ``basename:lineno:function``, and the type of a chained cause. Deliberately never
    includes ``str(exc)``: an exception message is the classic way a record body escapes
    into ``collector.log``.
    """
    parts = [type(exc).__name__]
    tb = getattr(exc, "__traceback__", None)
    if tb is not None:
        try:
            frames = traceback.extract_tb(tb)[-4:]
            parts.append(
                "at="
                + "|".join(
                    "%s:%s:%s"
                    % (os.path.basename(frame.filename or "?"), frame.lineno or 0, frame.name)
                    for frame in frames
                )
            )
        except Exception:
            parts.append("at=<unavailable>")
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    if cause is not None:
        parts.append("caused_by=" + type(cause).__name__)
    return " ".join(parts)


def _setup_logging(cfg: Dict[str, Any]) -> logging.Logger:
    """Install the rotating file handler plus stderr, both privacy-filtered. SPEC §9, §11.4.

    Sizes come from ``ops.log_max_bytes`` / ``ops.log_backup_count``; the level from
    ``ops.log_level``. The root logger is taken over deliberately so that records from any
    module pass through :class:`PrivacyFilter`. If the log file cannot be opened the
    collector still starts with stderr logging only — losing the file is not a reason to
    stop collecting.
    """
    level_name = str(status.cfg_get(cfg, "ops", "log_level", default="INFO")).upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.INFO

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    formatter.converter = time.gmtime  # UTC everywhere (INTERFACES: hard platform rules)

    privacy = PrivacyFilter()
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for existing in list(getattr(root, "filters", [])):
        root.removeFilter(existing)
    root.addFilter(privacy)

    stream_handler = logging.StreamHandler(stream=sys.stderr)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(privacy)
    root.addHandler(stream_handler)

    log = logging.getLogger("chatcol")
    log.setLevel(level)

    log_path = str(status.cfg_get(cfg, "log_path", default="") or "")
    if log_path:
        try:
            directory = os.path.dirname(os.path.abspath(log_path))
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=int(status.as_number(status.cfg_get(cfg, "ops", "log_max_bytes", default=10485760), default=10485760)),
                backupCount=int(status.as_number(status.cfg_get(cfg, "ops", "log_backup_count", default=5), default=5)),
                encoding="utf-8",
            )
        except OSError as exc:
            log.error(
                "log file unavailable, stderr only path=%s errno=%s exc=%s",
                log_path,
                getattr(exc, "errno", None),
                type(exc).__name__,
            )
        else:
            file_handler.setFormatter(formatter)
            file_handler.addFilter(privacy)
            root.addHandler(file_handler)
    return log


# --------------------------------------------------------------------------------------
# worker supervision
# --------------------------------------------------------------------------------------


class _Worker(object):
    """A supervised worker thread and its liveness bookkeeping. SPEC §11.3."""

    __slots__ = ("name", "section", "thread", "failure", "reported")

    def __init__(self, name: str, section: str, thread: threading.Thread) -> None:
        self.name = name
        self.section = section
        self.thread = thread
        self.failure = None  # type: Optional[str]
        self.reported = False


def _guard_worker(worker: _Worker, log: logging.Logger) -> None:
    """Wrap ``worker.thread.run`` so an escaping exception can never vanish. SPEC §11.3.

    The wrapper is bound as an *instance* attribute, which shadows the class's ``run`` for
    ``Thread._bootstrap_inner``. The exception is recorded as a type name on the worker —
    which the status loop publishes and :func:`status.evaluate_health` turns into critical
    — and logged as metadata only (SPEC §11.4).
    """
    original = worker.thread.run

    def guarded() -> None:
        try:
            original()
        except BaseException as exc:  # noqa: BLE001 - nothing may escape a worker thread
            worker.failure = type(exc).__name__
            log.critical("worker thread died name=%s %s", worker.name, _exc_meta(exc))
        else:
            log.info("worker thread returned name=%s", worker.name)

    worker.thread.run = guarded  # type: ignore[assignment]


def _shadowed_thread_methods(thread: threading.Thread) -> List[str]:
    """Names of :class:`threading.Thread` methods the subclass overwrote with a non-callable.

    A worker that stores its stop event as ``self._stop`` shadows ``Thread._stop()``, and
    then ``is_alive()`` and ``join()`` raise ``TypeError: 'Event' object is not callable``
    from inside :mod:`threading` — the supervisor's liveness check becomes the thing that
    breaks the supervisor. Detecting it by name turns a mystifying crash loop into one log
    line. ``run`` is excluded because :func:`_guard_worker` replaces it on purpose (with a
    callable).
    """
    shadowed = []  # type: List[str]
    for attr in dir(threading.Thread):
        if not attr.startswith("_") or attr == "run":
            continue
        if not callable(getattr(threading.Thread, attr, None)):
            continue
        if not callable(getattr(thread, attr, None)):
            shadowed.append(attr)
    return shadowed


def _worker_alive(worker: _Worker, log: logging.Logger) -> bool:
    """Is this worker still doing its job? SPEC §11.3.

    False both when the thread has ended and when it recorded a failure. An exception from
    ``is_alive()`` itself (see :func:`_shadowed_thread_methods`) is recorded as a failure
    and reported as *not alive*, so a broken worker class flips health to critical instead
    of taking the status loop down with it.
    """
    try:
        alive = bool(worker.thread.is_alive())
    except Exception as exc:
        if worker.failure is None:
            worker.failure = "ThreadStateUnreadable-" + type(exc).__name__
            log.critical("worker liveness unreadable name=%s %s", worker.name, _exc_meta(exc))
        return False
    return alive and worker.failure is None


def _join_workers(workers: List[_Worker], log: logging.Logger) -> None:
    """Join every worker within :data:`JOIN_TIMEOUT_S` total. SPEC §11.2.

    A worker that will not join is abandoned rather than waited on forever: the threads
    are daemons, ``writer.close()`` still runs, and the process is designed to be killed.
    Nothing in here may raise — this runs in the shutdown ``finally``, where an exception
    would skip ``writer.close()`` and the final status publication.
    """
    deadline = time.monotonic() + JOIN_TIMEOUT_S
    for worker in workers:
        remaining = deadline - time.monotonic()
        if remaining < 0.5:
            remaining = 0.5
        try:
            worker.thread.join(remaining)
        except Exception as exc:
            log.error("worker join failed name=%s %s", worker.name, _exc_meta(exc))
            continue
        try:
            still_running = worker.thread.is_alive()
        except Exception:
            still_running = False
        if still_running:
            log.warning(
                "worker thread did not join name=%s timeout_s=%.1f", worker.name, remaining
            )
        else:
            log.info("worker thread joined name=%s", worker.name)


def _install_signal_handlers(stop: threading.Event, log: logging.Logger) -> Dict[str, int]:
    """Route SIGINT/SIGTERM (and SIGBREAK where it exists) to the stop event. SPEC §11.2.

    The handler does nothing but set the event and remember the signal number: calling
    into :mod:`logging` from a signal handler can deadlock on the handler lock the
    interrupted frame already holds. The status loop logs the signal afterwards.

    Signals absent on the platform are skipped — Windows has no SIGTERM in the POSIX
    sense on every build, and installing an unknown signal raises. ``SIGBREAK`` is included
    where it exists because that is what NSSM sends on Windows (SPEC §11.2).

    Shutdown latency is bounded by ``ops.status_interval_s``: the main thread is parked in
    ``Event.wait(interval)`` and processes the pending handler when that wait returns. With
    the default 15 s that is well inside systemd's ``TimeoutStopSec``.
    """
    seen = {}  # type: Dict[str, int]

    def handler(signum: int, frame: Any) -> None:
        seen["signum"] = int(signum)
        stop.set()

    installed = []  # type: List[str]
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError, RuntimeError):
            # ValueError: not the main thread. OSError: signal not supported here.
            log.warning("signal handler not installed sig=%s", name)
        else:
            installed.append(name)
    log.info("signal handlers installed sigs=%s", ",".join(installed) or "none")
    return seen


# --------------------------------------------------------------------------------------
# startup
# --------------------------------------------------------------------------------------


def _ensure_dirs(cfg: Dict[str, Any]) -> None:
    """Create the corpus directory and the parents of every configured path. SPEC §9."""
    data_dir = str(status.cfg_get(cfg, "data_dir", default="") or "")
    if data_dir and not os.path.isdir(data_dir):
        os.makedirs(data_dir, exist_ok=True)
    for key in ("state_path", "status_path", "log_path", "lock_path"):
        path = str(status.cfg_get(cfg, key, default="") or "")
        if not path:
            continue
        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)


def _enqueue_startup_gap(
    cfg: Dict[str, Any], writer: Any, log: logging.Logger, first_run: bool
) -> None:
    """Enqueue ``[hwm_us - safety_lag, now + 5 s]``. SPEC §11.2 step 6, §6.5, §7.9.

    Skipped entirely on a first run: with no state there is no high-water mark to trust,
    and SPEC §7.9 is explicit that a first run must not backfill. Skipped when backfill is
    disabled, because nothing would ever drain the queue and the pending count would climb
    into a permanent warn (SPEC §11.3).
    """
    if first_run:
        log.info(
            "first run: no startup gap, hwm_us=%d (SPEC 7.9)", int(writer.hwm_us())
        )
        return
    if not bool(status.cfg_get(cfg, "backfill", "enabled", default=True)):
        log.warning(
            "backfill disabled: startup gap not enqueued, downtime will not be healed"
        )
        return
    safety_lag_us = int(
        float(status.as_number(status.cfg_get(cfg, "backfill", "safety_lag_s", default=120), default=120))
        * 1000000
    )
    start_us = int(writer.hwm_us()) - safety_lag_us
    end_us = tsutil.now_us() + GAP_END_OVERSHOOT_US
    gap_id = writer.enqueue_gap(start_us, end_us, pass_no=1, tag="startup")
    if gap_id is None:
        log.info(
            "startup gap skipped (zero-length or clamped away) start_us=%d end_us=%d",
            start_us,
            end_us,
        )
    else:
        log.info(
            "startup gap enqueued gap_id=%s start_us=%d end_us=%d span_s=%d",
            gap_id,
            start_us,
            end_us,
            int((end_us - start_us) // 1000000),
        )


def _start_workers(
    cfg: Dict[str, Any],
    writer: Any,
    publisher: "status.StatusPublisher",
    log: logging.Logger,
    stop: threading.Event,
) -> List[_Worker]:
    """Start the backfill thread, then the SSE reader. SPEC §11.2 step 7.

    Order matters: the gap queue must have a consumer before the stream starts producing
    new gaps. Both worker modules are imported here rather than at module scope so that
    ``--health``, ``--version`` and ``--print-config`` keep working even if a worker
    module is broken — a health check must never fail for the same reason the collector
    did.
    """
    workers = []  # type: List[_Worker]

    def register(name: str, section: str, thread: threading.Thread) -> _Worker:
        """Name the thread, check it for shadowed Thread internals, and supervise it."""
        thread.daemon = True
        thread.name = name
        shadowed = _shadowed_thread_methods(thread)
        if shadowed:
            log.critical(
                "worker class shadows threading.Thread internals name=%s attrs=%s: "
                "liveness checks will be treated as dead",
                name,
                ",".join(shadowed),
            )
        worker = _Worker(name, section, thread)
        _guard_worker(worker, log)
        workers.append(worker)
        publisher.update(section, thread_alive=True, thread_failure=None)
        return worker

    if bool(status.cfg_get(cfg, "backfill", "enabled", default=True)):
        import backfiller

        thread = backfiller.Backfiller(cfg, writer, publisher, log, stop)
        register("backfiller", "backfill", thread)
        thread.start()
        log.info(
            "backfill thread started min_interval_s=%s",
            status.cfg_get(cfg, "backfill", "min_interval_s", default=1.0),
        )
    else:
        log.warning("backfill disabled by config: gaps will not be filled")

    if bool(status.cfg_get(cfg, "sse", "enabled", default=True)):
        import ssereader

        thread = ssereader.SseReader(cfg, writer, publisher, log, stop)
        register("ssereader", "sse", thread)
        thread.start()
        log.info(
            "sse thread started host=%s path=%s",
            status.cfg_get(cfg, "sse", "host", default="?"),
            status.cfg_get(cfg, "sse", "path", default="?"),
        )
    else:
        log.warning("sse disabled by config: no live capture")

    if bool(status.cfg_get(cfg, "log_tail", "enabled", default=False)):
        # SPEC §8 / §12.6: the log-tail source is phase 2 and is not part of this build.
        # Say so loudly rather than letting the operator believe a third source is running.
        log.error(
            "log_tail.enabled is true but the log-tail source is not part of this build; "
            "no proxy-log rows will be collected"
        )
        publisher.update("log_tail", enabled=True, running=False)

    return workers


# --------------------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------------------


def _status_loop(
    cfg: Dict[str, Any],
    writer: Any,
    publisher: "status.StatusPublisher",
    workers: List[_Worker],
    stop: threading.Event,
    log: logging.Logger,
    signal_seen: Dict[str, int],
) -> int:
    """Drive rotation/gzip/fsync and publish ``status.json``. SPEC §5.2, §11.3.

    Every tick: ``writer.tick()`` (rotation check, gzip drive, periodic fsync and state
    persist), then a liveness check of every worker, then a publication. A worker that is
    not alive is recorded so health goes critical; if *all* workers are dead the loop
    returns non-zero so the supervisor restarts the process rather than leaving a
    half-dead collector publishing a healthy-looking heartbeat.
    """
    interval = float(status.as_number(status.cfg_get(cfg, "ops", "status_interval_s", default=15), default=15))
    if interval <= 0:
        interval = 15.0
    rc = EXIT_OK

    while not stop.is_set():
        started = time.monotonic()

        try:
            writer.tick()
        except Exception as exc:
            log.error("writer tick failed %s", _exc_meta(exc))

        alive_count = 0
        for worker in workers:
            alive = _worker_alive(worker, log)
            if alive:
                alive_count += 1
            elif not worker.reported:
                worker.reported = True
                log.critical(
                    "worker thread not alive name=%s failure=%s",
                    worker.name,
                    worker.failure or "exited",
                )
            publisher.update(
                worker.section, thread_alive=bool(alive), thread_failure=worker.failure
            )

        try:
            publisher.publish(writer)
        except Exception as exc:
            # publish() already swallows its own I/O failures; this is the belt to its
            # braces, because a loop that dies stops publishing and looks wedged.
            log.error("status publish raised %s", _exc_meta(exc))

        if workers and alive_count == 0 and not stop.is_set():
            log.critical(
                "every worker thread is dead: exiting for supervisor restart (SPEC 11.2)"
            )
            rc = EXIT_FAILURE
            break

        elapsed = time.monotonic() - started
        wait_s = interval - elapsed
        if wait_s < 0.5:
            wait_s = 0.5
        stop.wait(wait_s)

    signum = signal_seen.get("signum")
    if signum is not None:
        log.info("stop requested by signal signum=%d", signum)
    return rc


def _run(cfg: Dict[str, Any]) -> int:
    """Full collector run. SPEC §11.2 (steps in order), §11.3.

    Returns a process exit code: 0 on a clean stop, 1 on a fatal error or an all-workers-
    dead exit, 3 when another collector holds the lock.
    """
    try:
        _ensure_dirs(cfg)
    except OSError as exc:
        sys.stderr.write(
            "cannot create working directories: errno=%s exc=%s\n"
            % (getattr(exc, "errno", None), type(exc).__name__)
        )
        return EXIT_FAILURE

    log = _setup_logging(cfg)
    log.info(
        "%s %s starting pid=%d python=%d.%d.%d platform=%s",
        PROGRAM_NAME,
        VERSION,
        os.getpid(),
        sys.version_info[0],
        sys.version_info[1],
        sys.version_info[2],
        sys.platform,
    )

    try:
        import singlelock
    except ImportError as exc:
        log.critical("cannot import singlelock %s", _exc_meta(exc))
        return EXIT_FAILURE

    lock_path = str(status.cfg_get(cfg, "lock_path", default="collector.lock"))
    try:
        lock_handle = singlelock.acquire(lock_path)
    except singlelock.AlreadyRunning as exc:
        holder = getattr(exc, "pid", None)
        message = "another collector already holds the lock path=%s holder_pid=%s" % (
            lock_path,
            holder,
        )
        log.error(message)
        sys.stderr.write(message + "\n")
        return EXIT_LOCKED
    except OSError as exc:
        log.critical(
            "lock acquisition failed path=%s errno=%s exc=%s",
            lock_path,
            getattr(exc, "errno", None),
            type(exc).__name__,
        )
        return EXIT_FAILURE
    log.info("single-instance lock acquired path=%s", lock_path)

    stop = threading.Event()
    workers = []  # type: List[_Worker]
    writer = None  # type: Any
    publisher = None  # type: Optional[status.StatusPublisher]
    rc = EXIT_OK

    try:
        # Imported inside the guard so an ImportError in a sibling module is reported as a
        # logged fatal (metadata only) with the lock released cleanly, not as a raw
        # traceback on stderr.
        import statefile
        import writer as writer_mod

        state_path = str(status.cfg_get(cfg, "state_path", default="state.json"))
        state = statefile.load_state(state_path, log)
        first_run = state is None
        if first_run:
            state = statefile.new_state(tsutil.now_us())
            statefile.save_state(state_path, state)
            log.warning(
                "no usable state file: treating as first run, hwm_us=now, no backfill "
                "(SPEC 7.9) path=%s hwm_us=%s",
                state_path,
                state.get("hwm_us"),
            )
        else:
            log.info(
                "state loaded path=%s hwm_us=%s seq=%s gaps=%d pending_gzip=%d",
                state_path,
                state.get("hwm_us"),
                state.get("seq"),
                len(state.get("gaps") or ()),
                len(state.get("pending_gzip") or ()),
            )

        # Writer construction performs SPEC §5.4 repair, stray .gz.tmp cleanup, the
        # pending_gzip re-drive and the hwm_us-anchored dedupe rehydration (§4.3b).
        writer = writer_mod.Writer(cfg, state, log)
        log.info("writer ready hwm_us=%d", int(writer.hwm_us()))

        publisher = status.StatusPublisher(cfg, log)
        status.reset_alert_state()

        _enqueue_startup_gap(cfg, writer, log, first_run)

        signal_seen = _install_signal_handlers(stop, log)
        workers = _start_workers(cfg, writer, publisher, log, stop)
        if not workers:
            log.critical("no sources enabled: sse and backfill are both disabled, nothing to do")
            return EXIT_CONFIG

        rc = _status_loop(cfg, writer, publisher, workers, stop, log, signal_seen)
    except Exception as exc:
        log.critical("fatal error in collector %s", _exc_meta(exc))
        rc = EXIT_FAILURE
    finally:
        stop.set()
        _join_workers(workers, log)
        if publisher is not None:
            for worker in workers:
                publisher.update(
                    worker.section,
                    thread_alive=_worker_alive(worker, log),
                    thread_failure=worker.failure,
                )
            publisher.update("sse", state="stopped")
            try:
                publisher.publish(writer)
            except Exception as exc:
                log.error("final status publish failed %s", _exc_meta(exc))
        if writer is not None:
            try:
                writer.close()
                log.info("writer closed")
            except Exception as exc:
                log.critical("writer close failed %s", _exc_meta(exc))
        log.info("collector stopped rc=%d", rc)
        # lock_handle is held until the frame dies, which is the process lifetime here
        # (INTERFACES: "Returned handle must be kept alive for the process lifetime").
        del lock_handle
    return rc


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI. SPEC §9, §11.3."""
    parser = argparse.ArgumentParser(
        prog="collector.py",
        description="2b2t chat corpus collector (SPEC v1). Stdlib only, unattended.",
    )
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="path to collector.json (defaults apply when omitted; CHATCOL_* env always wins)",
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="read status.json and exit 0 ok / 1 warn / 2 critical (SPEC 11.3)",
    )
    parser.add_argument(
        "--version", action="store_true", help="print the collector version and exit"
    )
    parser.add_argument(
        "--print-config",
        dest="print_config",
        action="store_true",
        help="print the merged configuration (defaults <- file <- env) and exit",
    )
    return parser


def main(argv: Optional[list] = None) -> int:
    """CLI entry point. SPEC §9 (config), §11.2 (startup), §11.3 (``--health``).

    Exit codes: 0 success, 1 fatal error, 2 configuration error, 3 another collector holds
    the lock. With ``--health`` the exit code is the health code from
    :func:`status.run_health_check` (0/1/2).
    """
    args = _build_parser().parse_args(argv)

    if args.version:
        print("%s %s" % (PROGRAM_NAME, VERSION))
        return EXIT_OK

    try:
        cfg = config.load_config(args.config)
    except config.ConfigError as exc:
        # ConfigError messages are about key names and types, never record content.
        sys.stderr.write("config error: %s\n" % exc)
        return EXIT_CONFIG
    except OSError as exc:
        sys.stderr.write(
            "config unreadable: path=%s errno=%s exc=%s\n"
            % (args.config, getattr(exc, "errno", None), type(exc).__name__)
        )
        return EXIT_CONFIG

    if args.print_config:
        print(json.dumps(cfg, indent=2, sort_keys=True))
        return EXIT_OK

    if args.health:
        code = status.run_health_check(cfg)
        _reported, reason = status.describe_health(cfg)
        print(
            "health=%s code=%d reason=%s"
            % (status.HEALTH_NAMES.get(code, "critical"), code, reason)
        )
        return code

    return _run(cfg)


if __name__ == "__main__":
    sys.exit(main())
