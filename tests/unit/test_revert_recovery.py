"""M4.11 / M4.12 — K4 rule 4 edge cases beyond the §9 scenarios."""

from __future__ import annotations

import functools
import json
import sys

import pytest

from scribesense.adapters.fake import FakeAdapter, FakeCrash, FakeWorld
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.states import TxState
from scribesense.log import log_path
from scribesense.safety.recovery import recover_at_login
from scribesense.safety.revert import Reverter, RevertReport
from scribesense.safety.testing import fake_fontset, make_recovery_system

FILE = "/fake/home/.config/fontconfig/conf.d/99-scribesense.conf"
KEY = "org.gnome.desktop.interface font-name"
A = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.0)
B = Configuration("Noto Sans", 0.20, 2.5, 1.6, 1.2, 1.0)


_make_recovery_system = make_recovery_system
#: Tests that reach into the in-memory double's internals.
MEMORY_ONLY = {"test_missing_baseline_backup_is_reported_and_keeps_recovery_info"}


@pytest.fixture(autouse=True, params=["memory", "sqlite"])
def journal_kind(request, monkeypatch: pytest.MonkeyPatch) -> str:  # type: ignore[no-untyped-def]
    """Run every test on the in-memory double and on the real SQLite journal (M4.10)."""
    if request.param == "sqlite" and request.node.originalname in MEMORY_ONLY:
        pytest.skip("reaches into the in-memory double's internals")
    monkeypatch.setattr(sys.modules[__name__], "make_recovery_system",
                        functools.partial(_make_recovery_system, journal=request.param))
    return request.param


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


def test_unexpected_adapter_exception_during_revert_is_failed_not_a_crash(world: FakeWorld) -> None:
    original = world.snapshot_all()
    adapter = FakeAdapter(world, {"t": [FILE, KEY]})
    system = make_recovery_system(world, [adapter])
    tx = system.apply(A, keep=False)
    real_revert = adapter.revert

    def broken(item, backup):  # type: ignore[no-untyped-def]
        raise OSError("disk went away")  # not a ScribeSenseError

    adapter.revert = broken  # type: ignore[method-assign]
    report = system.revert(tx)
    assert set(report.failed) == {FILE, KEY}
    assert system.journal.state(tx) is TxState.REVERT_FAILED  # recovery info kept, retried later
    adapter.revert = real_revert  # type: ignore[method-assign]
    assert system.reset().failed == []
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



def test_failed_drawn_check_is_not_retained_even_when_chosen_ok(world: FakeWorld) -> None:
    """chosen_ok=True but drawn_ok=False must count as a failed verify: reverted, never managed."""
    original = world.snapshot_all()
    adapter = FakeAdapter(world, {"t": [FILE, KEY]}, verify_ok=True, drawn_ok=False)
    system = make_recovery_system(world, [adapter])
    tx = system.apply(A, keep=True)
    assert system.managed(FILE) is None and system.managed(KEY) is None
    assert world.snapshot_all() == original
    assert all(not i.retained for i in system.journal.items(tx))


def test_no_drawn_check_still_counts_as_passed(world: FakeWorld) -> None:
    """drawn_ok=None (no drawn check for this target, A14) must not block retaining the write."""
    adapter = FakeAdapter(world, {"t": [FILE, KEY]}, verify_ok=True, drawn_ok=None)
    system = make_recovery_system(world, [adapter])
    system.apply(A, keep=True)
    assert system.managed(FILE) is not None


# ---------------------------------------------------------------- O3 review of M4.11 (2026-10-18)


def _revert_log() -> list[dict]:  # type: ignore[type-arg]
    if not log_path().exists():
        return []
    rows = [json.loads(x) for x in log_path().read_text().splitlines()]
    return [r for r in rows if r["category"] == "revert"]


def test_finish_tx_from_snapshotted_with_an_intent_goes_through_applying(world: FakeWorld) -> None:
    """Note 1: an intent recorded while still SNAPSHOTTED -> SNAPSHOTTED -> APPLYING -> REVERTING, reverted."""
    original = world.snapshot_all()
    adapter = FakeAdapter(world, {"t": [FILE, KEY]})
    system = make_recovery_system(world, [adapter])
    journal = system.journal
    tx = journal.begin("apply", preset_id="default")
    target = adapter.target_for(FILE)
    ids = {}
    for snap in adapter.snapshot(target):
        backup = world.state(snap.location).data if snap.kind == "file" else None
        ids[snap.location] = journal.record_snapshot(tx, snap, backup)
    journal.set_state(tx, TxState.SNAPSHOTTED)
    planned = adapter.plan(target, fake_fontset(A), A)
    for pw in planned:
        journal.record_intent(tx, ids[pw.location], pw)
    adapter.apply(planned[0])  # the write happened; the state never left SNAPSHOTTED
    assert journal.state(tx) is TxState.SNAPSHOTTED
    report = Reverter(journal, system.adapters).finish_tx(tx)
    assert report.ok and report.failed_codes == {}
    assert journal.state(tx) is TxState.REVERTED
    assert world.snapshot_all() == original


def _no_op_revert(item, backup):  # type: ignore[no-untyped-def]
    return None  # "succeeds" but restores nothing


def _os_error_revert(item, backup):  # type: ignore[no-untyped-def]
    raise OSError("disk went away")


@pytest.mark.parametrize(
    ("breakage", "expected"),
    [
        ("missing_adapter", ErrorCode.ADAPTER_UNAVAILABLE),
        ("scribesense_error", ErrorCode.ADAPTER_TIMEOUT),
        ("unexpected_exception", ErrorCode.UNEXPECTED_ADAPTER_ERROR),
        ("restore_did_not_match", ErrorCode.REVERT_ITEM_FAILED),
    ],
)
def test_failed_items_carry_their_error_code(world: FakeWorld, breakage: str, expected: ErrorCode) -> None:
    """Note 2: every failed location says why, with an existing ErrorCode value."""
    adapter = FakeAdapter(world, {"t": [FILE, KEY]})
    system = make_recovery_system(world, [adapter])
    tx = system.apply(A, keep=False)
    adapters = dict(system.adapters)
    if breakage == "missing_adapter":
        adapters = {}
    elif breakage == "scribesense_error":
        adapter.fail_on = {"revert": ErrorCode.ADAPTER_TIMEOUT}
    elif breakage == "unexpected_exception":
        adapter.revert = _os_error_revert  # type: ignore[method-assign]
    else:
        adapter.revert = _no_op_revert  # type: ignore[method-assign]
    report = Reverter(system.journal, adapters).revert_tx(tx)
    assert set(report.failed) == {FILE, KEY}
    assert report.failed_codes == {FILE: expected.value, KEY: expected.value}
    assert not report.ok
    assert system.journal.state(tx) is TxState.REVERT_FAILED


def test_successful_and_drift_items_have_no_error_code(world: FakeWorld) -> None:
    system = make_recovery_system(world, [FakeAdapter(world, {"t": [FILE, KEY]})])
    tx = system.apply(A, keep=False)
    world.external_edit(KEY, "'User Choice 12'")
    report = system.revert(tx)
    assert report.restored == [FILE] and report.skipped == [KEY]
    assert report.failed_codes == {}


def test_merge_keeps_the_error_codes() -> None:
    a = RevertReport(failed=["x"], failed_codes={"x": ErrorCode.ADAPTER_TIMEOUT.value})
    b = RevertReport(restored=["z"], failed=["x", "y"],
                     failed_codes={"x": ErrorCode.REVERT_ITEM_FAILED.value, "y": ErrorCode.BACKUP_MISSING.value})
    a.merge(b)
    assert a.failed == ["x", "y"] and a.restored == ["z"]
    assert a.failed_codes == {"x": ErrorCode.REVERT_ITEM_FAILED.value, "y": ErrorCode.BACKUP_MISSING.value}
    assert not a.ok
    assert RevertReport().failed_codes == {} and RevertReport().ok


def test_reset_report_carries_the_codes_of_unfinished_txs(world: FakeWorld) -> None:
    """recovery merges finish_tx reports, so the codes reach `reset` / login recovery."""
    adapter = FakeAdapter(world, {"t": [FILE, KEY]})
    system = make_recovery_system(world, [adapter])
    system.apply(A, keep=False)
    system.crash()
    adapter.fail_on = {"revert": ErrorCode.ADAPTER_TIMEOUT}
    report = recover_at_login(system.journal, system.adapters)
    assert report.failed_codes == {FILE: "ADAPTER_TIMEOUT", KEY: "ADAPTER_TIMEOUT"}


@pytest.mark.parametrize(
    ("breakage", "expected"),
    [("scribesense_error", ErrorCode.ADAPTER_TIMEOUT), ("unexpected_exception", ErrorCode.UNEXPECTED_ADAPTER_ERROR)],
)
def test_adapter_exception_during_revert_is_logged_with_ids_only(
    world: FakeWorld, breakage: str, expected: ErrorCode
) -> None:
    """Note 3: the code is logged (category "revert", adapter + target ids); no location, no message."""
    adapter = FakeAdapter(world, {"t": [FILE, KEY]})
    system = make_recovery_system(world, [adapter])
    tx = system.apply(A, keep=False)
    if breakage == "scribesense_error":
        adapter.fail_on = {"revert": ErrorCode.ADAPTER_TIMEOUT}
    else:
        adapter.revert = _os_error_revert  # type: ignore[method-assign]
    before = len(_revert_log())
    system.revert(tx)
    rows = _revert_log()[before:]
    assert len(rows) == 2  # one per failed item
    for row in rows:
        assert row["code"] == expected.value
        assert row["adapter"] == "fake" and row["target"] == "t"
        assert "dropped" not in row
    text = log_path().read_text()
    assert FILE not in text and "fontconfig" not in text and "disk went away" not in text
