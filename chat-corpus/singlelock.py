"""Single-instance lock (SPEC §10, "Single-instance lock").

Two collectors on one host would double the ``/feed/*`` connection count against
the edge's cap of 6 per IP, so this lock is a correctness requirement rather than
hygiene: leaked connections manufacture their own 429s (SPEC §1, §12.12).

``fcntl.flock(LOCK_EX | LOCK_NB)`` on POSIX, ``msvcrt.locking(LK_NBLCK)`` on
Windows -- both stdlib. The lock file also carries our PID as text so the
operator can see who holds it, and so a losing process can report the holder.

Two implementation details that matter:

* The lock file is opened **without** ``O_TRUNC`` and is only truncated *after*
  the lock is held. Truncating first would blank the incumbent's PID and destroy
  the one diagnostic the operator has.
* On Windows ``msvcrt.locking`` takes a *mandatory* byte-range lock, so the byte
  we lock is at offset :data:`_WIN_LOCK_OFFSET`, far past the PID text. Locking
  byte 0 would make the holder's PID unreadable to everyone else -- exactly when
  it is needed.

Lifetime: :func:`acquire` returns a handle that owns the open file descriptor.
**The caller must keep that handle referenced for the whole process lifetime.**
Dropping the last reference does not release the lock (the descriptor is a raw
``int`` with no finaliser, so the lock survives to process exit) but it does
leak the descriptor and removes any way to release deliberately. Store it, e.g.
on the collector object, and call :meth:`LockHandle.release` at shutdown.
"""

from __future__ import annotations

import os
from typing import Optional

_IS_WINDOWS = os.name == "nt"

if _IS_WINDOWS:  # pragma: no cover - platform-specific
    import msvcrt
else:  # pragma: no cover - platform-specific
    import fcntl

#: Windows locks a single byte here. Well past any plausible PID text, so the
#: mandatory lock never covers the bytes another process wants to read.
_WIN_LOCK_OFFSET = 4096

#: Bytes read when identifying the incumbent. A PID plus newline is tiny; the
#: cap keeps a hand-edited or garbage lock file from being read into memory.
_PID_READ_BYTES = 64


class AlreadyRunning(RuntimeError):
    """Another process holds the lock. `pid` is the holder, or ``None`` if illegible.

    SPEC §10: the caller prints the holding PID and exits 3.
    """

    pid: Optional[int]

    def __init__(
        self, message: str = "collector already running", pid: Optional[int] = None
    ) -> None:
        RuntimeError.__init__(self, message)
        self.pid = pid


class LockHandle:
    """Owns the locked descriptor. Keep it alive; see the module docstring."""

    __slots__ = ("_fd", "_path", "_pid", "_released")

    def __init__(self, fd: int, path: str, pid: int) -> None:
        self._fd = fd
        self._path = path
        self._pid = pid
        self._released = False

    @property
    def path(self) -> str:
        """Path of the lock file."""
        return self._path

    @property
    def pid(self) -> int:
        """PID written into the lock file (ours)."""
        return self._pid

    def fileno(self) -> int:
        """The locked descriptor. Do not close it directly; use release()."""
        return self._fd

    def release(self) -> None:
        """Unlock and close. Idempotent, and never raises.

        The lock file is intentionally left on disk with our PID in it: unlinking
        races with a concurrent opener, and a stale PID is useful to the operator.
        """
        if self._released:
            return
        self._released = True
        try:
            _unlock(self._fd)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass

    def __enter__(self) -> "LockHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False

    def __repr__(self) -> str:
        return "<LockHandle path=%r pid=%d released=%r>" % (
            self._path,
            self._pid,
            self._released,
        )


def acquire(path: str) -> LockHandle:
    """Take the single-instance lock at `path` (SPEC §10).

    Creates the file if absent, never truncates it before the lock is held, then
    rewrites it with our PID. Returns a handle the caller must retain for the
    process lifetime.

    Raises :class:`AlreadyRunning` (with ``.pid`` set to the incumbent's PID, or
    ``None`` when the file holds nothing legible) if the lock is taken. Other
    ``OSError``s -- an unwritable directory, a permissions problem -- propagate,
    because they are operator errors and not contention.
    """
    _ensure_parent_dir(path)

    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_BINARY"):  # Windows: no newline translation, ever.
        flags |= os.O_BINARY
    fd = os.open(path, flags, 0o644)

    try:
        _lock(fd)
    except OSError as exc:
        holder = _read_pid(fd)
        errno_value = exc.errno
        try:
            os.close(fd)
        except OSError:
            pass
        # No `from exc`: keep the message metadata-only and short for the CLI.
        raise AlreadyRunning(
            "lock %s is held by another process (pid=%s, errno=%s)"
            % (os.path.basename(path), holder if holder is not None else "unknown",
               errno_value),
            pid=holder,
        ) from None
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise

    pid = os.getpid()
    try:
        _write_pid(fd, pid)
    except OSError:
        # The lock is what matters; the PID text is a courtesy to the operator.
        pass
    return LockHandle(fd, path, pid)


# --------------------------------------------------------------------------- #
# platform primitives
# --------------------------------------------------------------------------- #


def _lock(fd: int) -> None:
    """Non-blocking exclusive lock. Raises OSError when already held (SPEC §10)."""
    if _IS_WINDOWS:  # pragma: no cover - platform-specific
        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
    else:  # pragma: no cover - platform-specific
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if _IS_WINDOWS:  # pragma: no cover - platform-specific
        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
    else:  # pragma: no cover - platform-specific
        fcntl.flock(fd, fcntl.LOCK_UN)


def _write_pid(fd: int, pid: int) -> None:
    """Replace the file contents with our PID (SPEC §10). Lock must be held."""
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, ("%d\n" % pid).encode("ascii"))
    try:
        os.fsync(fd)
    except OSError:
        pass
    os.lseek(fd, 0, os.SEEK_SET)


def _read_pid(fd: int) -> Optional[int]:
    """Best-effort read of the incumbent's PID. None when illegible (SPEC §10)."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, _PID_READ_BYTES)
    except OSError:
        return None
    try:
        fields = raw.decode("ascii", "replace").split()
    except (UnicodeError, AttributeError):
        return None
    if not fields:
        return None
    try:
        pid = int(fields[0], 10)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
