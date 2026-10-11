"""K4 rule 1 (apply lock) and rule 6 (service takeover)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from scribesense.contracts import paths
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import LockOwner
from scribesense.safety.lock import ApplyLock, process_start_time, take_over_service

# ------------------------------------------------------------------ in-process


def test_acquire_writes_owner_record_and_release_keeps_the_file() -> None:
    lock = ApplyLock()
    assert lock.path == paths.lock_path()
    assert lock.try_acquire("reset")
    owner = lock.read_owner()
    assert owner == LockOwner("reset", os.getpid(), process_start_time(os.getpid()))
    assert oct(lock.path.stat().st_mode & 0o777) == "0o600"
    assert oct(lock.path.parent.stat().st_mode & 0o777) == "0o700"
    lock.release()
    assert lock.path.exists(), "the lock file is stable — never unlinked"
    assert lock.read_owner() is None, "owner record cleared on release"


def test_second_holder_is_refused_until_release() -> None:
    first, second = ApplyLock(), ApplyLock()
    assert first.try_acquire("service")
    assert not second.try_acquire("reset")
    assert second.is_locked()
    first.release()
    assert second.try_acquire("reset")
    second.release()


def test_lock_fd_is_not_inherited_by_children() -> None:
    lock = ApplyLock()
    assert lock.try_acquire("reset")
    assert os.get_inheritable(lock._fd) is False  # type: ignore[arg-type]
    lock.release()


def test_bounded_wait_times_out_with_lock_timeout() -> None:
    holder, waiter = ApplyLock(), ApplyLock()
    holder.try_acquire("recover")
    start = time.monotonic()
    with pytest.raises(ScribeSenseError) as exc:
        waiter.acquire("reset", timeout=0.3)
    assert exc.value.code is ErrorCode.LOCK_TIMEOUT
    assert time.monotonic() - start < 2
    holder.release()


def test_wait_succeeds_when_holder_releases() -> None:
    holder, waiter = ApplyLock(), ApplyLock()
    holder.try_acquire("recover")
    threading.Timer(0.2, holder.release).start()
    waiter.acquire("reset", timeout=5)
    assert waiter.held
    waiter.release()


def test_garbage_owner_record_reads_as_unknown() -> None:
    lock = ApplyLock()
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_bytes(b'{"role": "hacker", "pid": 1}')
    assert lock.read_owner() is None


def test_takeover_refuses_this_process_and_non_service_roles() -> None:
    me = process_start_time(os.getpid()) or 0
    assert take_over_service(LockOwner("service", os.getpid(), me)) is False
    assert take_over_service(LockOwner("recover", 999999, 0)) is False


# ------------------------------------------------------------------ cross-process (real helper processes)

SERVICE = textwrap.dedent("""
    import signal, subprocess, sys, time
    from scribesense.safety.lock import ApplyLock
    role, ignore_term, child = sys.argv[1], sys.argv[2] == "1", sys.argv[3] == "1"
    if ignore_term:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if child:  # a grandchild in the same process group, like a running fc-cache
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    lock = ApplyLock()
    assert lock.try_acquire(role)
    print("ready", flush=True)
    time.sleep(60)
""")


def _spawn(role: str, *, ignore_term: bool = False, child: bool = False) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [sys.executable, "-c", SERVICE, role, "1" if ignore_term else "0", "1" if child else "0"],
        stdout=subprocess.PIPE, text=True, start_new_session=True,  # the service is a session leader
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
    threading.Thread(target=proc.wait, daemon=True).start()  # reap it like a real parent would
    return proc


def _group_pids(pgid: int) -> list[int]:
    pids = []
    for stat in os.listdir("/proc"):
        if stat.isdigit():
            try:
                data = open(f"/proc/{stat}/stat").read()
            except OSError:
                continue
            fields = data[data.rfind(")") + 2:].split()
            if int(fields[2]) == pgid and fields[0] != "Z":
                pids.append(int(stat))
    return pids


def _cleanup(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, 9)
    except ProcessLookupError:
        pass


@pytest.mark.real_subprocess
def test_takeover_stops_a_hung_service_and_its_whole_group() -> None:
    proc = _spawn("service", child=True)
    try:
        lock = ApplyLock()
        owner = lock.read_owner()
        assert owner is not None and owner.role == "service" and owner.pid == proc.pid
        time.sleep(0.2)
        assert len(_group_pids(proc.pid)) == 2  # service + grandchild
        assert take_over_service(owner, grace_s=1.0, group_wait_s=5.0) is True
        assert _group_pids(proc.pid) == []
        assert lock.try_acquire("reset"), "the OS released the lock when the service died"
        lock.release()
    finally:
        _cleanup(proc)


@pytest.mark.real_subprocess
def test_takeover_escalates_to_sigkill_when_sigterm_is_ignored() -> None:
    proc = _spawn("service", ignore_term=True)
    try:
        owner = ApplyLock().read_owner()
        assert owner is not None
        start = time.monotonic()
        assert take_over_service(owner, grace_s=0.5, group_wait_s=5.0) is True
        assert time.monotonic() - start < 4
        assert _group_pids(proc.pid) == []
    finally:
        _cleanup(proc)


@pytest.mark.real_subprocess
def test_reused_pid_is_never_signalled() -> None:
    proc = _spawn("service")
    try:
        owner = ApplyLock().read_owner()
        assert owner is not None
        stale = LockOwner("service", owner.pid, owner.start_time + 12345)  # same pid, different process
        assert take_over_service(stale) is True  # "that process is gone"
        time.sleep(0.2)
        assert proc.poll() is None, "the live process with that pid must not be touched"
    finally:
        _cleanup(proc)


@pytest.mark.real_subprocess
def test_another_recovery_is_never_interrupted() -> None:
    proc = _spawn("recover")
    try:
        owner = ApplyLock().read_owner()
        assert owner is not None and owner.role == "recover"
        assert take_over_service(owner) is False
        with pytest.raises(ScribeSenseError) as exc:
            ApplyLock().acquire("reset", timeout=0.3)
        assert exc.value.code is ErrorCode.LOCK_TIMEOUT
        assert proc.poll() is None, "a running recovery is never killed"
    finally:
        _cleanup(proc)
