"""K4 / K5 rules on the in-memory journal (M4.10 double). The SQLite journal must pass the same tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scribesense.contracts.adapter import AppliedItem, PlannedWrite, SnapshotItem, Target, state_sha256
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import ActiveConfig, NoRecoveryObligations
from scribesense.contracts.states import TxState
from scribesense.safety.memory_journal import IllegalTransition, InMemoryJournal

T = Target("fake", "t1", "t1", False)
FILE = "/fake/a.conf"
KEY = "org.x font-name"
CFG = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.0)


def snap(location: str = FILE, existed: bool = True, data: bytes | None = b"orig") -> SnapshotItem:
    kind = "file" if location.startswith("/") else "key"
    return SnapshotItem(T, kind, location, existed, None if kind == "file" else (data or b"").decode() or None,
                        state_sha256(existed, data if existed else None), 0o644 if kind == "file" and existed else None,
                        None if kind == "file" else "s")


def planned(location: str = FILE, payload: bytes = b"new") -> PlannedWrite:
    return PlannedWrite(T, location, payload, state_sha256(True, payload))


def applied(location: str = FILE, payload: bytes = b"new") -> AppliedItem:
    return AppliedItem(T, location, state_sha256(True, payload))


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.now += timedelta(minutes=1)
        return self.now


def apply_to_awaiting(j: InMemoryJournal, *, retain: bool = True) -> tuple[int, int]:
    tx = j.begin("apply", None)
    item = j.record_snapshot(tx, snap(), b"orig")
    j.set_state(tx, TxState.SNAPSHOTTED)
    j.set_state(tx, TxState.APPLYING)
    j.record_intent(tx, item, planned())
    j.record_applied(tx, item, applied())
    j.set_state(tx, TxState.VERIFYING)
    if retain:
        j.mark_retained(tx, item)
    j.set_state(tx, TxState.AWAITING_CONFIRMATION)
    return tx, item


def active() -> ActiveConfig:
    return ActiveConfig(CFG, "abc123", "default", 0)


# ---------------------------------------------------------------- lock (rule 1, simplified)

def test_second_begin_while_locked_is_apply_in_progress() -> None:
    j = InMemoryJournal()
    j.begin("apply", None)
    with pytest.raises(ScribeSenseError) as exc:
        j.begin("apply", None)
    assert exc.value.code is ErrorCode.APPLY_IN_PROGRESS


def test_lock_is_held_through_confirmation_and_released_by_keep() -> None:
    j = InMemoryJournal()
    tx, _ = apply_to_awaiting(j)
    assert j.lock_held  # whole confirmation period
    j.keep(tx, active())
    assert not j.lock_held


def test_crash_releases_lock_but_keeps_the_unfinished_tx() -> None:
    j = InMemoryJournal()
    tx, _ = apply_to_awaiting(j)
    j.simulate_crash()
    assert not j.lock_held
    assert j.unfinished() == [tx]


# ---------------------------------------------------------------- K5 transitions

def test_illegal_transition_is_refused() -> None:
    j = InMemoryJournal()
    tx = j.begin("apply", None)
    with pytest.raises(IllegalTransition):
        j.set_state(tx, TxState.VERIFYING)


def test_completed_only_through_keep() -> None:
    j = InMemoryJournal()
    tx, _ = apply_to_awaiting(j)
    with pytest.raises(IllegalTransition):
        j.set_state(tx, TxState.COMPLETED)


def test_unfinished_includes_awaiting_and_revert_failed_but_not_failed_clean() -> None:
    j = InMemoryJournal()
    a, _ = apply_to_awaiting(j)
    j.simulate_crash()
    b = j.begin("revert", None, "all")
    j.set_state(b, TxState.REVERTING)
    j.set_state(b, TxState.REVERT_FAILED)
    c = j.begin("apply", None)
    j.set_state(c, TxState.FAILED_CLEAN)
    assert j.unfinished() == [a, b]
    assert j.pending_confirmation() == a


# ---------------------------------------------------------------- write-ahead order (rule 2)

def test_applied_before_intent_is_refused() -> None:
    j = InMemoryJournal()
    tx = j.begin("apply", None)
    item = j.record_snapshot(tx, snap(), b"orig")
    with pytest.raises(ValueError):
        j.record_applied(tx, item, applied())


def test_intent_must_match_snapshot_location() -> None:
    j = InMemoryJournal()
    tx = j.begin("apply", None)
    item = j.record_snapshot(tx, snap(), b"orig")
    with pytest.raises(ValueError):
        j.record_intent(tx, item, planned("/fake/other.conf"))


# ---------------------------------------------------------------- keep (rule 3)

def test_keep_is_one_step_and_only_retained_writes_become_managed() -> None:
    j = InMemoryJournal()
    tx, item = apply_to_awaiting(j, retain=True)
    revision = j.keep(tx, active())
    assert j.state(tx) is TxState.COMPLETED
    assert j.location(FILE).managed_sha256 == applied().new_sha256
    assert j.location(FILE).managed_font_key == "abc123"
    assert j.active().style_revision == revision == 1


def test_keep_ignores_unretained_writes() -> None:
    j = InMemoryJournal()
    tx, _ = apply_to_awaiting(j, retain=False)
    j.keep(tx, active())
    assert j.location(FILE).managed_sha256 is None


def test_keep_outside_awaiting_is_tx_not_pending() -> None:
    j = InMemoryJournal()
    tx = j.begin("apply", None)
    with pytest.raises(ScribeSenseError) as exc:
        j.keep(tx, active())
    assert exc.value.code is ErrorCode.TX_NOT_PENDING


def test_set_original_bumps_revision_and_clears_active() -> None:
    j = InMemoryJournal()
    tx, _ = apply_to_awaiting(j)
    j.keep(tx, active())
    assert j.set_original() == 2
    assert j.active() is None


# ---------------------------------------------------------------- baselines and backups

def test_baseline_is_the_first_snapshot_ever() -> None:
    j = InMemoryJournal()
    tx, _ = apply_to_awaiting(j)
    j.keep(tx, active())
    tx2 = j.begin("apply", None)
    j.record_snapshot(tx2, snap(data=b"new"), b"new")
    assert j.baseline(FILE).old_sha256 == state_sha256(True, b"orig")
    assert j.baseline_backup(FILE) == b"orig"


def test_absent_and_empty_are_distinct() -> None:
    j = InMemoryJournal()
    tx = j.begin("apply", None)
    j.record_snapshot(tx, snap("/fake/absent", existed=False, data=None), None)
    j.record_snapshot(tx, snap("/fake/empty", existed=True, data=b""), b"")
    assert j.baseline_backup("/fake/absent") is None
    assert j.baseline_backup("/fake/empty") == b""


def test_missing_backup_for_existing_file_is_backup_missing() -> None:
    j = InMemoryJournal()
    tx = j.begin("apply", None)
    j.record_snapshot(tx, snap(), None)  # existed, but no backup stored
    with pytest.raises(ScribeSenseError) as exc:
        j.baseline_backup(FILE)
    assert exc.value.code is ErrorCode.BACKUP_MISSING


def test_corrupt_backup_is_backup_corrupt() -> None:
    j = InMemoryJournal()
    tx = j.begin("apply", None)
    item = j.record_snapshot(tx, snap(), b"orig")
    j.corrupt_backup(j.location(FILE).baseline_backup_ref)
    with pytest.raises(ScribeSenseError) as exc:
        j.backup(tx, item)
    assert exc.value.code is ErrorCode.BACKUP_CORRUPT


# ---------------------------------------------------------------- pruning (K9 rule) and obligations

def test_prune_keeps_baselines_unfinished_and_newest() -> None:
    clock = Clock()
    j = InMemoryJournal(clock)
    done = []
    for _ in range(3):
        tx, _ = apply_to_awaiting(j)
        j.keep(tx, active())
        done.append(tx)
    pending, _ = apply_to_awaiting(j)
    j.simulate_crash()
    removed = j.prune_history(clock.now + timedelta(days=1), keep_last=1)
    assert removed == 2  # the two oldest completed; newest + unfinished stay
    assert j.unfinished() == [pending]
    assert j.baseline_backup(FILE) == b"orig"  # baseline survives pruning


def test_prune_keeps_the_newest_completed_apply_and_unresolved_locations() -> None:
    clock = Clock()
    j = InMemoryJournal(clock)
    kept, _ = apply_to_awaiting(j)
    j.keep(kept, active())  # the newest COMPLETED apply
    for _ in range(2):  # newer txs that end REVERTED
        tx, _ = apply_to_awaiting(j)
        j.set_state(tx, TxState.REVERTING)
        j.set_state(tx, TxState.REVERTED)
    assert j.prune_history(clock.now + timedelta(days=1), keep_last=0) == 2
    assert j.state(kept) is TxState.COMPLETED  # K9 D4 / F2: the confirmed apply survives keep_last=0

    j.set_unresolved(FILE, ErrorCode.DRIFT_SKIPPED)
    newer, _ = apply_to_awaiting(j)
    j.keep(newer, active())  # now `kept` is no longer the newest COMPLETED apply ...
    assert j.prune_history(clock.now + timedelta(days=1), keep_last=0) == 0
    assert j.state(kept) is TxState.COMPLETED  # ... but it touched an unresolved location, so it stays


def test_prune_validates_and_respects_the_lock() -> None:
    j = InMemoryJournal()
    with pytest.raises(ValueError):
        j.prune_history(datetime.now(timezone.utc), -1)
    j.begin("apply", None)
    with pytest.raises(ScribeSenseError) as exc:
        j.prune_history(datetime.now(timezone.utc), 0)
    assert exc.value.code is ErrorCode.APPLY_IN_PROGRESS


def test_no_obligations_only_when_nothing_is_unfinished_managed_or_unresolved() -> None:
    j = InMemoryJournal()
    assert isinstance(j.no_obligations(), NoRecoveryObligations)
    tx, _ = apply_to_awaiting(j)
    assert j.no_obligations() is None  # unfinished
    j.keep(tx, active())
    assert j.no_obligations() is None  # managed
    j.clear_managed(FILE)
    j.set_unresolved(FILE, ErrorCode.DRIFT_SKIPPED)
    assert j.no_obligations() is None  # unresolved
    j.set_unresolved(FILE, None)
    assert isinstance(j.no_obligations(), NoRecoveryObligations)
