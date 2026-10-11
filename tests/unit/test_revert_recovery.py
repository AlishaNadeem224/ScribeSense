"""M4.11 / M4.12 — K4 rule 4 edge cases beyond the §9 scenarios."""

from __future__ import annotations

import pytest

from scribesense.adapters.fake import FakeAdapter, FakeCrash, FakeWorld
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.states import TxState
from scribesense.safety.recovery import recover_at_login
from scribesense.safety.testing import make_recovery_system

FILE = "/fake/home/.config/fontconfig/conf.d/99-scribesense.conf"
KEY = "org.gnome.desktop.interface font-name"
A = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.0)
B = Configuration("Noto Sans", 0.20, 2.5, 1.6, 1.2, 1.0)


@pytest.fixture
def world() -> FakeWorld:
    w = FakeWorld()
    w.set_file(FILE, b"<fontconfig/>\n")
    w.set_key(KEY, "'Fira Sans 11'")
    return w


def test_crash_after_write_before_record_applied_is_recovered(world: FakeWorld) -> None:
    """Write-ahead intent: the write happened, record_applied never ran — reset still undoes it."""
    original = world.snapshot_all()
    adapter = FakeAdapter(world, {"t": [FILE, KEY]}, crash_after="apply")
    system = make_recovery_system(world, [adapter])
    with pytest.raises(FakeCrash):
        system.apply(A, keep=False)
    item = system.journal.items(system.journal.unfinished()[0])[0]
    assert item.intent is not None and item.applied is None
    system.crash()
    adapter.crash_after = None
    report = system.reset()
    assert report.failed == [] and report.skipped == []
    assert world.snapshot_all() == original


def test_crash_before_any_intent_ends_failed_clean(world: FakeWorld) -> None:
    original = world.snapshot_all()
    adapter = FakeAdapter(world, {"t": [FILE, KEY]}, crash_after="snapshot")
    system = make_recovery_system(world, [adapter])
    with pytest.raises(FakeCrash):
        system.apply(A, keep=False)
    tx = system.journal.unfinished()[0]
    system.crash()
    adapter.crash_after = None
    recover_at_login(system.journal, system.adapters)
    assert system.journal.state(tx) is TxState.FAILED_CLEAN
    assert world.snapshot_all() == original


def test_drift_during_unfinished_tx_is_skipped_never_overwritten(world: FakeWorld) -> None:
    system = make_recovery_system(world, [FakeAdapter(world, {"t": [FILE, KEY]})])
    system.apply(A, keep=False)
    system.crash()
    world.external_edit(KEY, "'User Choice 12'")
    report = recover_at_login(system.journal, system.adapters)
    assert report.skipped == [KEY]
    assert world.state(KEY).data == b"'User Choice 12'"


def test_failed_revert_is_unfinished_and_retried_successfully(world: FakeWorld) -> None:
    original = world.snapshot_all()
    adapter = FakeAdapter(world, {"t": [FILE, KEY]})
    system = make_recovery_system(world, [adapter])
    tx = system.apply(A, keep=False)
    adapter.fail_on = {"revert": ErrorCode.ADAPTER_TIMEOUT}
    report = system.revert(tx)
    assert set(report.failed) == {FILE, KEY}
    assert system.journal.state(tx) is TxState.REVERT_FAILED
    assert tx in system.journal.unfinished()
    adapter.fail_on = {}
    report = system.reset()  # retries the REVERT_FAILED tx, then restores baselines
    assert report.failed == []
    assert system.journal.state(tx) is TxState.REVERTED
    assert world.snapshot_all() == original


def test_login_recovery_never_touches_a_confirmed_configuration(world: FakeWorld) -> None:
    system = make_recovery_system(world, [FakeAdapter(world, {"t": [FILE, KEY]})])
    system.apply(A, keep=True)
    kept = world.snapshot_all()
    managed = system.managed(FILE)
    system.crash()
    report = recover_at_login(system.journal, system.adapters)
    assert report.restored == [] and report.failed == []
    assert world.snapshot_all() == kept
    assert system.managed(FILE) == managed
    assert system.journal.active() is not None


def test_missing_baseline_backup_is_reported_and_keeps_recovery_info(world: FakeWorld) -> None:
    system = make_recovery_system(world, [FakeAdapter(world, {"t": [FILE, KEY]})])
    system.apply(A, keep=True)
    system.journal._backups.clear()  # the stored original is gone
    report = system.reset()
    assert FILE in report.failed
    record = system.journal.location(FILE)
    assert record.unresolved is ErrorCode.BACKUP_MISSING
    assert record.managed_sha256 is not None  # kept for a later retry
    assert system.journal.no_obligations() is None


def test_failed_per_adapter_revert_rolls_back_the_whole_tx(world: FakeWorld) -> None:
    good = FakeAdapter(world, {"good": [FILE]}, name="good")
    bad = FakeAdapter(world, {"bad": [KEY]}, name="bad", verify_ok=False,
                      fail_on={"revert": ErrorCode.ADAPTER_TIMEOUT})
    system = make_recovery_system(world, [good, bad])
    tx = system.apply(A, keep=False)
    assert system.journal.state(tx) in (TxState.REVERTED, TxState.REVERT_FAILED)
    assert system.journal.pending_confirmation() is None  # never reached confirmation (K5)


def test_revert_of_a_finished_tx_is_tx_not_pending(world: FakeWorld) -> None:
    system = make_recovery_system(world, [FakeAdapter(world, {"t": [FILE, KEY]})])
    tx = system.apply(A, keep=True)
    with pytest.raises(ScribeSenseError) as exc:
        system.revert(tx)
    assert exc.value.code is ErrorCode.TX_NOT_PENDING


def test_reset_twice_is_safe(world: FakeWorld) -> None:
    original = world.snapshot_all()
    system = make_recovery_system(world, [FakeAdapter(world, {"t": [FILE, KEY]})])
    system.apply(A, keep=True)
    system.apply(B, keep=True)
    assert system.reset().ok
    second = system.reset()
    assert second.ok and second.restored == []
    assert world.snapshot_all() == original
    assert system.journal.no_obligations() is not None
