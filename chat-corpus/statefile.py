"""Durable collector state: `state.json` and its atomic write (SPEC §10).

`state.json` is the only thing that tells a restarted collector where it was. It is
therefore written with the crash-safe dance of SPEC §10 -- serialise, write a temp
file, fsync it, rotate the previous generation to ``.bak``, then ``os.replace`` --
so that at every instant at least one of ``state.json`` / ``state.json.bak`` is a
complete, parseable JSON document.

Two rules this module exists to enforce:

* **Never invent state.** :func:`load_state` returns ``None`` when neither
  generation is usable. The caller must treat that as a first run, which per
  SPEC §7.9 means ``hwm_us = now`` and *no backfill at all*. Silently
  synthesising a state here would let a corrupt file trigger a full-history
  crawl or, worse, look like a healthy resume.
* **Never upgrade silently.** An unrecognised ``version`` is a hard error
  (:class:`StateError`), not a best-effort migration.

Privacy (SPEC §11.4): nothing in this module ever puts a record body into a log
line or an exception message. Failure reasons are built from metadata only --
file role, exception *type*, decoder line/column, key names, counters.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

STATE_VERSION = 1

#: The nine counters of SPEC §10, in spec order. The writer may add further
#: counters at runtime (e.g. ``dedupe_evictions``, SPEC §4.3a); :func:`load_state`
#: preserves any extras it finds and only ever *adds* missing spec counters.
COUNTER_KEYS = (
    "rows_sse",
    "rows_backfill",
    "rows_log",
    "dupes_dropped",
    "parse_errors",
    "tie_skips",
    "clamped_gaps",
    "rows_added_pass2",
    "audit_rows_added",
)

#: Top-level keys that cannot be reconstructed and must be present and sane.
_REQUIRED_INT_KEYS = ("hwm_us", "seq")


class StateError(ValueError):
    """A state file exists and parses but is not a state file we may use.

    Raised for an unknown ``version`` and for a state whose load-bearing fields
    (``hwm_us``, ``seq``) are missing or not integers. Distinct from "corrupt or
    absent", which :func:`load_state` reports by returning ``None``.
    """


def new_state(now_us: int) -> Dict[str, Any]:
    """Build a first-run state, exactly the shape of SPEC §10.

    SPEC §10. ``hwm_us`` is seeded to *now*, which is what makes a first run
    non-backfilling per SPEC §7.9 -- there is no history to heal towards.
    """
    return {
        "version": STATE_VERSION,
        "hwm_us": int(now_us),
        "seq": 0,
        # The seq high-water RESERVATION. `seq` is the last value issued; this is the
        # last value the writer has made durable ahead of issuing it, so a hard kill
        # can never re-issue a seq that is already stamped on a row on disk
        # (SPEC §2 key 11: monotonic, persisted, never reset). A clean close lowers it
        # back to `seq`, so an orderly restart continues without a jump.
        "seq_reserved": 0,
        "connections": 0,
        "active_file": None,
        "pending_gzip": [],
        "gaps": [],
        "log_tail": {"dev": None, "ino": None, "offset": 0, "path": None},
        "audit_last_run_us": 0,
        "counters": dict((k, 0) for k in COUNTER_KEYS),
    }


def save_state(path: str, obj: Dict[str, Any]) -> None:
    """Atomically persist `obj` to `path` (SPEC §10, "Atomic write").

    Sequence: serialise -> write ``path + '.tmp'`` -> ``flush`` + ``fsync`` ->
    rotate any existing `path` to ``path + '.bak'`` -> ``os.replace`` the temp
    over `path` -> best-effort directory ``fsync``.

    ``os.replace`` is atomic on POSIX and on Windows (``MoveFileEx`` +
    ``REPLACE_EXISTING``). The directory ``fsync`` is POSIX-only and raises on
    Windows, so it is swallowed. Never leaves a ``.tmp`` behind: serialisation
    happens before the temp file is created, and any failure after that unlinks it.

    The caller must have fsynced the active data file *before* calling this
    (SPEC §10, "Ordering guarantee"): state must never reference bytes that are
    not yet durable.
    """
    try:
        payload = json.dumps(obj, indent=1, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError) as exc:
        # Deliberately do not chain or quote the original message: json's error
        # text embeds a repr of the offending value, which could be a record
        # body (SPEC §11.4).
        raise StateError(
            "state object is not JSON-serialisable (%s)" % (type(exc).__name__,)
        ) from None

    _ensure_parent_dir(path)
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            try:
                os.replace(path, path + ".bak")
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path)


def load_state(path: str, log: logging.Logger) -> Optional[Dict[str, Any]]:
    """Load `path`, else ``path + '.bak'``, else ``None`` (SPEC §10).

    Logs at ERROR when it falls back to the backup and when neither generation
    is usable. ``None`` is meaningful and must not be papered over: the caller
    treats it as a first run, i.e. ``hwm_us = now`` and **no backfill**
    (SPEC §7.9). An unknown ``version`` raises :class:`StateError` rather than
    being silently upgraded or skipped.

    Within a known version, structural keys that a state file can be missing
    without ambiguity (``connections``, ``pending_gzip``, ``gaps``, ``log_tail``,
    ``audit_last_run_us`` and individual counters) are filled from
    :func:`new_state` and reported at WARNING with key names only. ``hwm_us``
    and ``seq`` are never fabricated -- a state without them is a
    :class:`StateError`, because guessing either one silently loses data.

    **A :class:`StateError` from the primary generation is not fatal on its own.**
    The whole point of keeping two generations is to survive an unusable primary, so
    an unknown ``version`` or an unusable ``hwm_us``/``seq`` in ``state.json`` is
    logged at ERROR and ``state.json.bak`` is tried exactly as a decode failure would
    be. The error is re-raised only when the backup is unusable *for the same class of
    reason*, so a wrong-version state is still never silently accepted or downgraded
    into a first run (which would mean ``hwm_us = now`` and a permanent hole).
    """
    primary, reason, primary_error = _try_read(path)
    if primary is not None:
        _normalise(primary, log, "state.json")
        return primary

    backup_path = path + ".bak"
    backup, breason, backup_error = _try_read(backup_path)
    if backup is not None:
        log.error(
            "state file unusable (%s); falling back to backup generation: %s",
            reason,
            os.path.basename(backup_path),
        )
        _normalise(backup, log, "state.json.bak")
        return backup

    if primary_error is not None:
        # Neither generation is usable and the primary was not merely corrupt: it was
        # a state we are not allowed to interpret. Treating that as a first run would
        # silently reset hwm_us to now; the operator has to look at it.
        log.error(
            "state file refused (%s) and backup unusable (%s); refusing to guess",
            reason,
            breason,
        )
        raise primary_error
    if backup_error is not None:
        log.error(
            "state file missing/corrupt (%s) and backup refused (%s); refusing to guess",
            reason,
            breason,
        )
        raise backup_error

    log.error(
        "no usable state file (%s: %s / %s: %s); treating as first run: "
        "hwm_us=now and no backfill",
        os.path.basename(path),
        reason,
        os.path.basename(backup_path),
        breason,
    )
    return None


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #


def _try_read(
    candidate: str,
) -> Tuple[Optional[Dict[str, Any]], str, Optional[StateError]]:
    """Read one generation. Returns ``(state, reason, refusal)``.

    SPEC §10. `state` is None on any failure and `reason` is a short metadata-only
    token safe to log. `refusal` is the :class:`StateError` for a document that
    parsed but that we are not allowed to use (unknown ``version``, unusable
    ``hwm_us``/``seq``) -- returned rather than raised so :func:`load_state` can try
    the other generation first, and re-raised by it if that one is unusable too.
    """
    try:
        with open(candidate, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None, "missing", None
    except OSError as exc:
        return None, "os_error:%s" % (type(exc).__name__,), None

    if not raw.strip():
        return None, "empty", None

    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        # Only the position, never the document text.
        return None, "decode_error:line=%d,col=%d" % (exc.lineno, exc.colno), None
    except ValueError as exc:
        return None, "decode_error:%s" % (type(exc).__name__,), None

    if not isinstance(obj, dict):
        return None, "not_an_object", None

    try:
        _check_version(obj, candidate)
        _check_required(obj, candidate)
    except StateError as exc:
        return None, "refused", exc
    return obj, "ok", None


def _check_version(obj: Dict[str, Any], candidate: str) -> None:
    """Reject any state whose version is not ours (SPEC §10)."""
    version = obj.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise StateError(
            "state file %s has no usable 'version' (got %s); refusing to guess"
            % (os.path.basename(candidate), type(version).__name__)
        )
    if version != STATE_VERSION:
        raise StateError(
            "state file %s has version %d, expected %d; refusing to upgrade "
            "silently" % (os.path.basename(candidate), version, STATE_VERSION)
        )


def _check_required(obj: Dict[str, Any], candidate: str) -> None:
    """hwm_us and seq must exist as ints; they cannot be safely defaulted."""
    for key in _REQUIRED_INT_KEYS:
        value = obj.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise StateError(
                "state file %s has no usable '%s' (got %s)"
                % (os.path.basename(candidate), key, type(value).__name__)
            )


def _normalise(obj: Dict[str, Any], log: logging.Logger, role: str) -> None:
    """Fill absent structural keys/counters in place, reporting key names only."""
    template = new_state(obj["hwm_us"])
    added = []
    for key, default in template.items():
        if key in _REQUIRED_INT_KEYS or key == "version":
            continue
        if key not in obj:
            obj[key] = default
            added.append(key)

    counters = obj.get("counters")
    if not isinstance(counters, dict):
        obj["counters"] = dict((k, 0) for k in COUNTER_KEYS)
        if "counters" not in added:
            added.append("counters")
    else:
        for key in COUNTER_KEYS:
            if key not in counters:
                counters[key] = 0
                added.append("counters." + key)

    if added:
        log.warning(
            "%s was missing %d key(s), filled with defaults: %s",
            role,
            len(added),
            ",".join(added),
        )


def _ensure_parent_dir(path: str) -> None:
    """Create the state file's directory if it does not exist yet."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)


def _fsync_dir(path: str) -> None:
    """fsync the containing directory; a no-op that raises on Windows (SPEC §10)."""
    try:
        fd = os.open(os.path.dirname(os.path.abspath(path)) or ".", os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
