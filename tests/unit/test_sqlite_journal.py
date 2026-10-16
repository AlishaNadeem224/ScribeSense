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
