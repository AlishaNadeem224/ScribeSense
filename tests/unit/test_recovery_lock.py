"""K4 rule 6 — how reset / login recovery take the lock (decision logic, in-process)."""

from __future__ import annotations

import threading

import pytest

from scribesense.adapters.fake import FakeAdapter, FakeWorld
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import LockOwner
from scribesense.safety import ipc
from scribesense.safety.lock import ApplyLock
from scribesense.safety.recovery import LockPolicy, acquire_for_recovery, recover_at_login, reset
from scribesense.safety.testing import make_recovery_system

FILE = "/fake/a.conf"
A = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.0)


@pytest.fixture(autouse=True)
def short_runtime_dir(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """Socket paths are limited to 108 bytes; tmp_path is too long. Use the fixture's short dir."""
    runtime = sandbox.data_dir.parent / "run"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))


class Calls:
    def __init__(self) -> None:
        self.asked = 0
        self.taken_over: list[LockOwner] = []
        self.status: list[str] = []


def policy(calls: Calls, *, answers: bool, release_on_takeover: ApplyLock | None = None,
           wait_s: float = 0.3) -> LockPolicy:
    def ask(_timeout: float) -> bool:
        calls.asked += 1
        return answers

    def take_over(owner: LockOwner) -> bool:
        calls.taken_over.append(owner)
        if release_on_takeover is not None:
            release_on_takeover.release()  # stands in for "the service died, the OS dropped its lock"
        return True

    return LockPolicy(ApplyLock(), "reset", wait_s=wait_s, reply_deadline_s=0.1,
                      ask_service=ask, take_over=take_over, on_status=calls.status.append)


def test_free_lock_is_taken_without_asking_anyone() -> None:
    calls = Calls()
    p = policy(calls, answers=True)
    acquire_for_recovery(p)
    assert p.lock.held and calls.asked == 0 and not calls.taken_over
    p.lock.release()


def test_responsive_service_is_asked_and_awaited_never_killed() -> None:
    service = ApplyLock()
    service.try_acquire("service")
    calls = Calls()
    p = policy(calls, answers=True, wait_s=5)
    threading.Timer(0.2, service.release).start()  # the service finishes its reset and releases
    acquire_for_recovery(p)
    assert calls.asked == 1 and not calls.taken_over and p.lock.held
    p.lock.release()


def test_unresponsive_service_is_taken_over() -> None:
    service = ApplyLock()
    service.try_acquire("service")
    calls = Calls()
    p = policy(calls, answers=False, release_on_takeover=service)
    acquire_for_recovery(p)
    assert calls.asked == 1 and len(calls.taken_over) == 1 and calls.taken_over[0].role == "service"
    p.lock.release()


def test_a_service_that_answers_no_is_waited_for_never_taken_over() -> None:
    from scribesense.safety.recovery import REFUSED

    service = ApplyLock()
    service.try_acquire("service")
    calls = Calls()
    p = policy(calls, answers=REFUSED, release_on_takeover=service, wait_s=5)  # type: ignore[arg-type]
    threading.Timer(0.2, service.release).start()  # e.g. it was busy confirming an apply, then finished
    acquire_for_recovery(p)
    assert calls.asked == 1 and not calls.taken_over and p.lock.held  # K4 rule 6: an answer is not "no reply"
    p.lock.release()


def test_a_service_that_answers_no_and_stays_busy_times_out_without_a_takeover() -> None:
    from scribesense.safety.recovery import REFUSED

    service = ApplyLock()
    service.try_acquire("service")
    calls = Calls()
    p = policy(calls, answers=REFUSED, release_on_takeover=service, wait_s=0.2)  # type: ignore[arg-type]
    with pytest.raises(ScribeSenseError) as exc:
        acquire_for_recovery(p)
    assert exc.value.code is ErrorCode.LOCK_TIMEOUT and not calls.taken_over  # exit 3, nothing killed
    service.release()


def test_service_that_acks_but_never_finishes_is_taken_over() -> None:
    service = ApplyLock()
    service.try_acquire("service")
    calls = Calls()
    p = policy(calls, answers=True, release_on_takeover=service, wait_s=0.2)
    acquire_for_recovery(p)
    assert calls.asked == 1 and len(calls.taken_over) == 1
    p.lock.release()


@pytest.mark.parametrize("role", ["recover", "reset", "uninstall"])
def test_other_recoveries_are_waited_for_never_interrupted(role: str) -> None:
    other = ApplyLock()
    other.try_acquire(role)  # type: ignore[arg-type]
    calls = Calls()
    with pytest.raises(ScribeSenseError) as exc:
        acquire_for_recovery(policy(calls, answers=True))
    assert exc.value.code is ErrorCode.LOCK_TIMEOUT
    assert calls.asked == 0 and not calls.taken_over
    assert any("Waiting" in s for s in calls.status)
    other.release()


def test_refused_takeover_is_lock_timeout() -> None:
    service = ApplyLock()
    service.try_acquire("service")
    p = policy(Calls(), answers=False)
    p.take_over = lambda _owner: False
    with pytest.raises(ScribeSenseError) as exc:
        acquire_for_recovery(p)
    assert exc.value.code is ErrorCode.LOCK_TIMEOUT
    service.release()


def test_reset_with_policy_releases_the_lock_and_rereads_the_journal() -> None:
    world = FakeWorld()
    world.set_file(FILE, b"orig")
    original = world.snapshot_all()
    system = make_recovery_system(world, [FakeAdapter(world, {"t": [FILE]})])
    system.apply(A, keep=True)
    p = policy(Calls(), answers=True)
    report = reset(system.journal, system.adapters, policy=p)
    assert report.ok and world.snapshot_all() == original
    assert not p.lock.held and not p.lock.is_locked()


def test_login_recovery_releases_the_lock_even_on_error() -> None:
    world = FakeWorld()
    world.set_file(FILE, b"orig")
    system = make_recovery_system(world, [FakeAdapter(world, {"t": [FILE]})])
    system.journal.begin("apply", None)  # in-journal lock held -> begin() inside recovery fails
    p = policy(Calls(), answers=True)
    with pytest.raises(ScribeSenseError):
        recover_at_login(system.journal, system.adapters, policy=p)
    assert not p.lock.is_locked()


def test_default_ask_uses_ipc_and_reports_no_service_as_not_answered() -> None:
    from scribesense.safety.recovery import _ask_service_to_reset

    assert _ask_service_to_reset(0.2) is False
    with ipc.IpcServer(lambda m: {"v": 1, "ok": m["type"] == "reset"}):
        assert _ask_service_to_reset(1.0) is True
    from scribesense.safety.recovery import REFUSED

    with ipc.IpcServer(lambda m: {"v": 1, "ok": False, "error": "APPLY_IN_PROGRESS"}):
        assert _ask_service_to_reset(1.0) == REFUSED  # it answered: alive, never "not responding"
