"""O4 — The apply lock (K4 rule 1) and service takeover (K4 rule 6). Part of M4.10 / M4.12.

- `fcntl.flock` on contracts.paths.lock_path(). The file is STABLE: never unlinked or replaced.
- After acquiring, the holder writes an owner record {"role", "pid", "start_time"} into it. The record is
  identification only — the flock is the lock. An unreadable record means "owner unknown".
- The lock fd is close-on-exec (Python's default), so child processes never hold the lock.
- Released by the OS when the holding process dies.

Must never import GTK/Adw (A2): recovery uses this without the GUI.
"""

from __future__ import annotations

import json
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover — Linux only; imported lazily so the module still loads elsewhere
    fcntl = None  # type: ignore[assignment]

from scribesense.contracts import paths
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import (
    TAKEOVER_GRACE_S,
    TAKEOVER_GROUP_WAIT_S,
    LockOwner,
    LockRole,
)

POLL_S = 0.05


def process_start_time(pid: int) -> int | None:
    """Start time of `pid` in clock ticks since boot (/proc/<pid>/stat field 22); None if gone."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    fields = data[data.rfind(")") + 2:].split()
    return int(fields[19])


class ApplyLock:
    """One instance per process. Not re-entrant."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else paths.lock_path()
        self._fd: int | None = None

    # ------------------------------------------------------------- acquire / release

    def try_acquire(self, role: LockRole) -> bool:
        if self._fd is not None:
            raise RuntimeError("lock already held by this ApplyLock")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._fd = fd
        self._write_owner(LockOwner(role, os.getpid(), process_start_time(os.getpid()) or 0))
        return True

    def acquire(
        self,
        role: LockRole,
        timeout: float,
        on_wait: Callable[[LockOwner | None], None] | None = None,
    ) -> None:
        """Bounded wait. Timeout -> ScribeSenseError(LOCK_TIMEOUT); nothing is killed."""
        deadline = time.monotonic() + timeout
        announced = False
        while not self.try_acquire(role):
            if not announced and on_wait is not None:
                on_wait(self.read_owner())
                announced = True
            if time.monotonic() >= deadline:
                raise ScribeSenseError(ErrorCode.LOCK_TIMEOUT)
            time.sleep(POLL_S)

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            os.ftruncate(self._fd, 0)  # clear the owner record; the file itself stays
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> ApplyLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    # ------------------------------------------------------------- owner record

    def _write_owner(self, owner: LockOwner) -> None:
        assert self._fd is not None
        data = json.dumps({"role": owner.role, "pid": owner.pid, "start_time": owner.start_time}).encode()
        os.ftruncate(self._fd, 0)
        os.pwrite(self._fd, data, 0)
        os.fsync(self._fd)

    def read_owner(self) -> LockOwner | None:
        try:
            raw = json.loads(self.path.read_bytes() or b"null")
            if raw is None:
                return None
            role, pid, start = raw["role"], raw["pid"], raw["start_time"]
            if role not in ("service", "reset", "recover", "uninstall") or not isinstance(pid, int):
                return None
            return LockOwner(role, pid, int(start))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def is_locked(self) -> bool:
        """True if some process (including this one) holds the lock."""
        if self._fd is not None:
            return True
        if not self.path.exists():
            return False
        fd = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)  # closing also drops a lock we may have just taken
        return False


# ----------------------------------------------------------------- takeover (K4 rule 6)

def _group_alive(pgid: int) -> bool:
    """True while any NON-zombie process is in the group. (Zombies have exited; they only wait for
    their parent to reap them, and killpg(pgid, 0) would still 'succeed' on them.)"""
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            data = stat.read_text()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        fields = data[data.rfind(")") + 2:].split()
        if int(fields[2]) == pgid and fields[0] != "Z":
            return True
    return False


def _wait_group_gone(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _group_alive(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_S)
    return True


def take_over_service(
    owner: LockOwner,
    *,
    grace_s: float = TAKEOVER_GRACE_S,
    group_wait_s: float = TAKEOVER_GROUP_WAIT_S,
) -> bool:
    """Stop a hung ScribeSense SERVICE and its process group so recovery can take the lock.

    Safety checks, in order — any failure means "do not signal anything":
      1. owner.role must be "service" (other recoveries are never interrupted).
      2. owner.pid must still be the same process: start time must match (PID reuse guard).
      3. A pidfd pins that exact process; the start time is re-checked after opening it.
      4. Never this process or this process's group.
      5. The service must lead its own process group (it is started with setsid), else only the
         service process itself is signalled.
    Returns True if the service is gone (or already was), False if checks refused to act.
    Raises ScribeSenseError(LOCK_TIMEOUT) if the group is still alive after group_wait_s.
    """
    if owner.role != "service":
        return False
    if owner.pid in (os.getpid(), 0, 1):
        return False
    if process_start_time(owner.pid) != owner.start_time:
        return True  # that process is gone; a reused PID must never be signalled
    try:
        pidfd = os.pidfd_open(owner.pid)
    except ProcessLookupError:
        return True
    try:
        if process_start_time(owner.pid) != owner.start_time:
            return True
        try:
            pgid = os.getpgid(owner.pid)
        except ProcessLookupError:
            return True
        lead_group = pgid == owner.pid and pgid != os.getpgrp()

        signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        if lead_group:
            _signal_group(pgid, signal.SIGTERM)
            if _wait_group_gone(pgid, grace_s):
                return True
            _signal_group(pgid, signal.SIGKILL)
            if not _wait_group_gone(pgid, group_wait_s):
                raise ScribeSenseError(ErrorCode.LOCK_TIMEOUT)
            return True
        # Not a group leader: signal the service process only, through the pidfd.
        if _wait_pid_gone(owner, grace_s):
            return True
        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        if not _wait_pid_gone(owner, group_wait_s):
            raise ScribeSenseError(ErrorCode.LOCK_TIMEOUT)
        return True
    finally:
        os.close(pidfd)


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def _wait_pid_gone(owner: LockOwner, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while process_start_time(owner.pid) == owner.start_time and not _is_zombie(owner.pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_S)
    return True


def _is_zombie(pid: int) -> bool:
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return True
    return data[data.rfind(")") + 2:].split()[0] == "Z"
