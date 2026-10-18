"""M4.10 — the SQLite journal: card acceptance tests (lock · illegal transitions · kill mid-apply) and the lock rules.

The shared K4 behaviour (records, revert, recovery, the §9 scenarios) runs on this journal and on the in-memory double
in tests/unit/test_revert_recovery.py and tests/recovery/. Two `ApplyLock` objects on the same path stand in for two
processes (flock is per open file).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from scribesense.contracts import paths
from scribesense.contracts.adapter import AppliedItem, PlannedWrite, SnapshotItem, Target, state_sha256
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import ActiveConfig
from scribesense.contracts.states import TxState
from scribesense.safety.journal import SqliteJournal
from scribesense.safety.lock import ApplyLock
from scribesense.store import JournalTables, Store, default_path

T = Target("fake", "t", "Test", False)
FILE = "/fake/a.conf"
CFG = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.0)


def journal(role: str = "service") -> SqliteJournal:
    return SqliteJournal(Store(default_path()), ApplyLock(), role=role)  # type: ignore[arg-type]


def snap() -> SnapshotItem:
    return SnapshotItem(T, "file", FILE, True, None, state_sha256(True, b"orig"), 0o644, None)


def to_awaiting(j: SqliteJournal) -> int:
    tx = j.begin("apply", "default")
    item = j.record_snapshot(tx, snap(), b"orig")
    j.set_state(tx, TxState.SNAPSHOTTED)
    j.set_state(tx, TxState.APPLYING)
    j.record_intent(tx, item, PlannedWrite(T, FILE, b"new", state_sha256(True, b"new")))
    j.record_applied(tx, item, AppliedItem(T, FILE, state_sha256(True, b"new")))
    j.set_state(tx, TxState.VERIFYING)
    j.mark_retained(tx, item)
    j.set_state(tx, TxState.AWAITING_CONFIRMATION)
    return tx


# ---- card: lock ---------------------------------------------------------------------------------------------

def test_only_one_transaction_at_a_time_across_processes() -> None:
    service, other = journal(), journal("reset")
    tx = to_awaiting(service)  # K4 rule 1: held through the whole confirmation period
    with pytest.raises(ScribeSenseError) as exc:
        other.begin("revert", None, "all")
    assert exc.value.code is ErrorCode.APPLY_IN_PROGRESS
    with pytest.raises(ScribeSenseError):
        service.begin("apply", None)  # and never two in one process
    assert ApplyLock().read_owner().role == "service"  # owner record written

    service.keep(tx, ActiveConfig(CFG, "abc123abc123", "default", 0))  # COMPLETED -> released
    assert not service.lock_held
    other.begin("revert", None, "all")


def test_revert_failed_releases_the_lock_so_recovery_can_retry() -> None:
    j = journal()
    tx = to_awaiting(j)
    j.set_state(tx, TxState.REVERTING)
    j.set_state(tx, TxState.REVERT_FAILED)
    assert not ApplyLock().is_locked() and j.unfinished() == [tx]
    retry = journal("recover").begin("revert", None, tx)
    assert retry > tx


def test_a_lock_the_caller_already_holds_is_used_and_left_to_the_caller() -> None:
    lock = ApplyLock()
    assert lock.try_acquire("reset")  # recovery.LockPolicy takes it before begin("revert")
    j = SqliteJournal(Store(default_path()), lock, role="reset")
    tx = j.begin("revert", None, "all")
    j.set_state(tx, TxState.REVERTING)
    j.set_state(tx, TxState.REVERTED)
    assert lock.held  # the journal did not release what it did not take
    lock.release()


def test_cannot_persist_means_refused_before_any_write_and_the_lock_is_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def store_down(*_a, **_k):  # type: ignore[no-untyped-def]
        raise ScribeSenseError(ErrorCode.STORE_IO_ERROR)

    monkeypatch.setattr(JournalTables, "create_tx", store_down)
    with pytest.raises(ScribeSenseError) as exc:
        journal().begin("apply", None)
    assert exc.value.code is ErrorCode.STORE_IO_ERROR
    assert not ApplyLock().is_locked()


def test_pruning_is_refused_while_any_process_holds_the_lock() -> None:
    later = datetime.now(timezone.utc) + timedelta(days=60)
    holder = journal()
    holder.begin("apply", None)
    for j in (holder, journal("recover")):
        with pytest.raises(ScribeSenseError) as exc:
            j.prune_history(later, 0)
        assert exc.value.code is ErrorCode.APPLY_IN_PROGRESS


# ---- card: illegal transitions ------------------------------------------------------------------------------

@pytest.mark.parametrize("target", [TxState.VERIFYING, TxState.AWAITING_CONFIRMATION, TxState.COMPLETED])
def test_illegal_transitions_are_refused(target: TxState) -> None:
    j = journal()
    tx = j.begin("apply", None)
    with pytest.raises(ValueError):
        j.set_state(tx, target)
    assert j.state(tx) is TxState.CREATED and j.lock_held


# ---- card: kill mid-apply -> unfinished() returns the tx ----------------------------------------------------

CHILD = textwrap.dedent("""
    import os, signal
    from scribesense.contracts.adapter import PlannedWrite, SnapshotItem, Target, state_sha256
    from scribesense.contracts.states import TxState
    from scribesense.safety.journal import SqliteJournal
    from scribesense.safety.lock import ApplyLock
    from scribesense.store import Store, default_path
    t = Target("fake", "t", "Test", False)
    j = SqliteJournal(Store(default_path()), ApplyLock())
    tx = j.begin("apply", None)
    item = j.record_snapshot(tx, SnapshotItem(t, "file", "/fake/a.conf", True, None,
                                              state_sha256(True, b"orig"), 0o644, None), b"orig")
    j.set_state(tx, TxState.SNAPSHOTTED)
    j.set_state(tx, TxState.APPLYING)
    j.record_intent(tx, item, PlannedWrite(t, "/fake/a.conf", b"new", state_sha256(True, b"new")))
    print(tx, flush=True)
    os.kill(os.getpid(), signal.SIGKILL)  # die mid-apply, holding the lock
""")


@pytest.mark.real_subprocess
def test_kill_mid_apply_leaves_the_tx_unfinished_and_the_lock_free() -> None:
    child = subprocess.run([sys.executable, "-c", CHILD], capture_output=True, timeout=60, check=False)
    assert child.returncode == -signal.SIGKILL, child.stderr.decode()
    tx = int(child.stdout)

    j = journal("recover")
    assert j.unfinished() == [tx] and j.state(tx) is TxState.APPLYING
    (item,) = j.items(tx)
    assert item.intent is not None and item.intent.payload == b"new"  # durable before the (never-made) write
    assert j.backup(tx, item.item_id) == b"orig"
    assert not ApplyLock().is_locked()  # the OS dropped the flock
    assert j.begin("revert", None, tx) > tx


def test_store_and_lock_files_live_in_the_data_dir() -> None:
    journal().begin("apply", None)
    assert default_path().parent == paths.data_dir() and paths.lock_path().exists()
    assert os.stat(default_path()).st_mode & 0o077 == 0


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_style_changed_hook_runs_after_keep_and_original_and_never_breaks_them(kind: str) -> None:
    from scribesense.safety.memory_journal import InMemoryJournal

    seen: list[int] = []
    make = (lambda cb: InMemoryJournal(on_style_changed=cb)) if kind == "memory" else \
        (lambda cb: SqliteJournal(Store(default_path()), ApplyLock(), on_style_changed=cb))
    j = make(seen.append)
    tx = to_awaiting(j)
    assert seen == []  # nothing confirmed yet
    rev = j.keep(tx, ActiveConfig(CFG, "abc123abc123", "default", 0))
    assert seen == [rev] and j.style_revision() == rev
    assert j.set_original() == rev + 1 and seen == [rev, rev + 1]

    def failing(_rev: int) -> None:
        raise OSError("no subscriber")

    k = make(failing)  # sqlite: same database, so the revision continues; memory: a fresh journal
    tx2 = to_awaiting(k)
    rev2 = k.keep(tx2, ActiveConfig(CFG, "abc123abc123", "default", 0))  # the hook raises ...
    assert rev2 == k.style_revision() and k.active() is not None and not k.lock_held  # ... Keep stands
    if kind == "sqlite":
        probe = ApplyLock()
        assert probe.try_acquire("service")  # the apply lock was released despite the failing hook
        probe.release()
    before = k.style_revision()
    assert k.set_original() == before + 1 and k.style_revision() == before + 1  # committed anyway
    assert k.active() is None
    from scribesense.log import log_path

    assert '"step": "style_changed"' in log_path().read_text() or "style_changed" in log_path().read_text()


# ---- O3 review notes (2026-10-18) ---------------------------------------------------------------------------

def test_two_threads_in_begin_get_exactly_one_tx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Note 1: one journal shared by two threads. The second thread calls begin() while the first holds the apply
    lock but has not recorded its tx yet (a slow create_tx) — it must not get a tx of its own."""
    real = JournalTables.create_tx
    inside = threading.Event()

    def slow_create_tx(self, *a, **k):  # type: ignore[no-untyped-def]
        inside.set()
        time.sleep(0.2)
        return real(self, *a, **k)

    monkeypatch.setattr(JournalTables, "create_tx", slow_create_tx)
    j = journal()
    got: list[object] = []

    def run() -> None:
        try:
            got.append(j.begin("apply", None))
        except ScribeSenseError as exc:
            got.append(exc.code)

    first = threading.Thread(target=run)
    first.start()
    assert inside.wait(10)  # the first thread holds the lock and is inside create_tx
    second = threading.Thread(target=run)
    second.start()
    first.join(10)
    second.join(10)
    assert got.count(ErrorCode.APPLY_IN_PROGRESS) == 1, got
    (tx,) = [g for g in got if isinstance(g, int)]
    assert j.lock_held and j.unfinished() == [tx]


def test_keep_failing_at_its_last_statement_keeps_the_tx_and_the_lock_and_a_retry_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    j = journal()
    tx = to_awaiting(j)
    real = Store.execute

    def fail_last(self, sql, params=()):  # type: ignore[no-untyped-def]
        if sql.startswith("UPDATE transactions SET state"):
            raise ScribeSenseError(ErrorCode.STORE_IO_ERROR)
        return real(self, sql, params)

    monkeypatch.setattr(Store, "execute", fail_last)
    with pytest.raises(ScribeSenseError) as exc:
        j.keep(tx, ActiveConfig(CFG, "abc123abc123", "default", 0))
    assert exc.value.code is ErrorCode.STORE_IO_ERROR
    monkeypatch.setattr(Store, "execute", real)

    assert j.state(tx) is TxState.AWAITING_CONFIRMATION and j.pending_confirmation() == tx
    assert j.active() is None and j.location(FILE).managed_sha256 is None  # nothing partial committed
    assert j.lock_held and not ApplyLock().try_acquire("reset")  # still held across processes
    revision = j.keep(tx, ActiveConfig(CFG, "abc123abc123", "default", 0))
    assert j.state(tx) is TxState.COMPLETED and j.style_revision() == revision
    assert not j.lock_held and not ApplyLock().is_locked()


def test_a_journal_on_a_recovery_mode_store_runs_begin_and_revert() -> None:
    service = journal()
    tx = to_awaiting(service)
    (item,) = service.items(tx)
    service._lock.release()  # the service died: the OS dropped its flock (the row stays AWAITING)
    service._t._s.close()

    lock = ApplyLock()
    assert lock.try_acquire("recover")  # recover --login takes it through LockPolicy first
    rec = SqliteJournal(Store(default_path(), mode="recovery"), lock, role="recover")
    assert rec.unfinished() == [tx]
    r = rec.begin("revert", None, tx)
    rec.set_state(tx, TxState.REVERTING)
    rec.record_revert(tx, item.item_id, "reverted")
    rec.set_state(tx, TxState.REVERTED)
    rec.set_state(r, TxState.REVERTING)
    rec.set_state(r, TxState.REVERTED)
    assert rec.unfinished() == [] and rec.items(tx)[0].revert_outcome == "reverted"
    assert lock.held  # the caller's lock stays the caller's
    lock.release()


def test_prune_holds_the_apply_lock_while_it_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Note 2 (K9: pruning runs under the apply lock)."""
    entered, go = threading.Event(), threading.Event()
    real = JournalTables.prune_history

    def slow_prune(self, before, keep_last):  # type: ignore[no-untyped-def]
        entered.set()
        assert go.wait(10)
        return real(self, before, keep_last)

    monkeypatch.setattr(JournalTables, "prune_history", slow_prune)
    j = journal("recover")
    result: list[int] = []
    worker = threading.Thread(target=lambda: result.append(j.prune_history(datetime.now(timezone.utc), 0)))
    worker.start()
    try:
        assert entered.wait(10)
        other = ApplyLock()
        assert not other.try_acquire("service")  # another process cannot start an Apply mid-prune
        assert other.read_owner().role == "recover"  # taken with this journal's role
    finally:
        go.set()
        worker.join(10)
    assert result == [0]
    assert not ApplyLock().is_locked()  # released when the prune ended


def test_prune_uses_a_lock_the_caller_holds_and_leaves_it() -> None:
    lock = ApplyLock()
    assert lock.try_acquire("reset")
    j = SqliteJournal(Store(default_path()), lock, role="reset")
    assert j.prune_history(datetime.now(timezone.utc), 0) == 0
    assert lock.held
    lock.release()


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_apply_is_refused_while_recovery_obligations_remain_but_revert_is_allowed(kind: str) -> None:
    """Note 3 / A3: recovery completes before any new Apply (decision O4 2026-10-18, both journals)."""
    from scribesense.safety.memory_journal import InMemoryJournal

    j = InMemoryJournal() if kind == "memory" else journal()
    tx = to_awaiting(j)
    j.set_state(tx, TxState.REVERTING)
    j.set_state(tx, TxState.REVERT_FAILED)  # lock released, tx unfinished
    with pytest.raises(ScribeSenseError) as exc:
        j.begin("apply", "default")
    assert exc.value.code is ErrorCode.RECOVERY_OBLIGATIONS_REMAIN
    assert not j.lock_held and j.unfinished() == [tx]  # nothing recorded
    if kind == "sqlite":
        assert not ApplyLock().is_locked()  # the lock begin() took was released
    r = j.begin("revert", None, tx)
    assert j.lock_held and j.unfinished() == [tx, r]


def test_refused_apply_leaves_a_lock_the_caller_holds() -> None:
    first = journal()
    tx = to_awaiting(first)
    first._lock.release()  # crashed service
    lock = ApplyLock()
    assert lock.try_acquire("reset")
    j = SqliteJournal(Store(default_path()), lock, role="reset")
    with pytest.raises(ScribeSenseError) as exc:
        j.begin("apply", None)
    assert exc.value.code is ErrorCode.RECOVERY_OBLIGATIONS_REMAIN
    assert lock.held and not j.lock_held and j.unfinished() == [tx]
    lock.release()
