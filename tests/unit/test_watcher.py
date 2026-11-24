"""O4 watcher (safety/watcher.py, 2026-11-24): restores that wait for a browser finish by themselves once it is
closed, A3 no longer blocks an Apply for such waits, and a newer Keep is never undone. Both journals."""

from __future__ import annotations

import pytest

from scribesense.adapters.fake import FakeAdapter, FakeWorld
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.states import TxState
from scribesense.safety.testing import make_recovery_system
from scribesense.safety.watcher import Watcher, finish_when_closed, only_waiting_for_apps

FC = "/fake/home/.config/fontconfig/conf.d/99-scribesense.conf"
ZEN = "/fake/home/.zen/profile/prefs.js"
A = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.0)
B = Configuration("Noto Sans", 0.20, 2.5, 1.6, 1.2, 1.0)


@pytest.fixture(params=["memory", "sqlite"])
def setup(request):  # type: ignore[no-untyped-def]
    world = FakeWorld()
    world.set_file(FC, b"<fontconfig/>\n")
    world.set_file(ZEN, b'user_pref("a", 1);\n')
    zen = FakeAdapter(world, {"zen": [ZEN]}, name="zen")
    system = make_recovery_system(world, [FakeAdapter(world, {"fc": [FC]}, name="fc"), zen],
                                  journal=request.param)
    return world, zen, system


def browser(zen: FakeAdapter, *, open_: bool) -> None:
    """Like Zen's prefs.js: while the browser runs, a revert is refused and writes nothing."""
    zen._running = frozenset({"zen"}) if open_ else frozenset()
    zen.fail_on = {"revert": ErrorCode.BROWSER_RUNNING} if open_ else {}


def reset_with_browser_open(world, zen, system):  # type: ignore[no-untyped-def]
    original = world.snapshot_all()
    system.apply(A, keep=True)
    browser(zen, open_=True)
    report = system.reset()
    assert report.failed == [ZEN]
    assert system.journal.location(ZEN).unresolved is ErrorCode.BROWSER_RUNNING
    return original


# ---------------------------------------------------------------- the A3 relaxation

def test_a_reset_waiting_only_for_a_browser_does_not_block_a_new_apply(setup) -> None:  # type: ignore[no-untyped-def]
    world, zen, system = setup
    reset_with_browser_open(world, zen, system)
    [reset_tx] = system.journal.unfinished()
    assert system.journal.state(reset_tx) is TxState.REVERT_FAILED
    tx = system.apply(B, keep=False)  # was RECOVERY_OBLIGATIONS_REMAIN before 2026-11-24
    assert system.journal.state(tx) is TxState.AWAITING_CONFIRMATION


def test_an_apply_whose_own_revert_failed_still_blocks(setup) -> None:  # type: ignore[no-untyped-def]
    world, zen, system = setup
    tx = system.apply(A, keep=False)
    browser(zen, open_=True)
    assert system.revert(tx).failed == [ZEN]
    with pytest.raises(ScribeSenseError) as exc:
        system.journal.begin("apply", None)
    assert exc.value.code is ErrorCode.RECOVERY_OBLIGATIONS_REMAIN


def test_any_other_unresolved_code_still_blocks(setup) -> None:  # type: ignore[no-untyped-def]
    world, zen, system = setup
    system.apply(A, keep=True)
    zen.fail_on = {"revert": ErrorCode.REVERT_ITEM_FAILED}
    system.reset()
    with pytest.raises(ScribeSenseError) as exc:
        system.journal.begin("apply", None)
    assert exc.value.code is ErrorCode.RECOVERY_OBLIGATIONS_REMAIN


@pytest.mark.parametrize(("unfinished", "unresolved", "allowed"), [
    ([("revert", TxState.REVERT_FAILED)], [ErrorCode.BROWSER_RUNNING, None], True),
    ([("revert", TxState.REVERT_FAILED)], [ErrorCode.DRIFT_SKIPPED], True),
    ([("revert", TxState.REVERT_FAILED)], [ErrorCode.ADAPTER_UNAVAILABLE], False),
    ([("apply", TxState.REVERT_FAILED)], [], False),
    ([("revert", TxState.REVERTING)], [], False),  # a reset still running (crashed mid-way)
    ([("uninstall", TxState.REVERT_FAILED)], [], False),
])
def test_only_waiting_for_apps(unfinished, unresolved, allowed) -> None:  # type: ignore[no-untyped-def]
    assert only_waiting_for_apps(unfinished, unresolved) is allowed


# ---------------------------------------------------------------- finish when closed

def test_nothing_happens_while_the_browser_is_open(setup) -> None:  # type: ignore[no-untyped-def]
    world, zen, system = setup
    reset_with_browser_open(world, zen, system)
    before = world.snapshot_all()
    assert finish_when_closed(system.journal, system.adapters) is None
    assert world.snapshot_all() == before and not system.journal.lock_held


def test_the_restore_finishes_by_itself_once_the_browser_is_closed(setup) -> None:  # type: ignore[no-untyped-def]
    world, zen, system = setup
    original = reset_with_browser_open(world, zen, system)
    browser(zen, open_=False)
    report = finish_when_closed(system.journal, system.adapters)
    assert report is not None and report.restored == [ZEN] and report.failed == []
    assert world.snapshot_all() == original
    record = system.journal.location(ZEN)
    assert record.unresolved is None and record.managed_sha256 is None
    assert system.journal.unfinished() == []  # the old reset tx is closed too
    assert finish_when_closed(system.journal, system.adapters) is None  # nothing left to do


def test_a_newer_keep_is_never_undone(setup) -> None:  # type: ignore[no-untyped-def]
    world, zen, system = setup
    reset_with_browser_open(world, zen, system)
    system.apply(B, keep=True)  # allowed by the relaxation; re-manages the Zen location with B's font
    with_b = world.snapshot_all()
    browser(zen, open_=False)
    finish_when_closed(system.journal, system.adapters)
    assert world.snapshot_all() == with_b


def test_a_user_change_meanwhile_is_left_alone(setup) -> None:  # type: ignore[no-untyped-def]
    world, zen, system = setup
    reset_with_browser_open(world, zen, system)
    world.external_edit(ZEN, 'user_pref("mine", 2);\n')
    browser(zen, open_=False)
    finish_when_closed(system.journal, system.adapters)
    assert world.state(ZEN).data == b'user_pref("mine", 2);\n'
    assert system.journal.location(ZEN).unresolved is ErrorCode.DRIFT_SKIPPED


def test_a_pending_apply_holding_the_lock_skips_the_round(setup) -> None:  # type: ignore[no-untyped-def]
    world, zen, system = setup
    reset_with_browser_open(world, zen, system)
    tx = system.apply(B, keep=False)  # awaiting confirmation: holds the lock
    browser(zen, open_=False)
    assert finish_when_closed(system.journal, system.adapters) is None
    assert system.journal.location(ZEN).unresolved is ErrorCode.BROWSER_RUNNING
    system.revert(tx)  # the confirm window said Revert: the tx ends, the lock is free again
    assert finish_when_closed(system.journal, system.adapters) is not None
    assert system.journal.location(ZEN).unresolved is None


def test_refused_again_waits_for_the_next_open_and_close(setup) -> None:  # type: ignore[no-untyped-def]
    world, zen, system = setup
    reset_with_browser_open(world, zen, system)
    watcher = Watcher(system.journal, system.adapters)
    zen._running = frozenset()  # looks closed, but the adapter still refuses (e.g. a lock not yet gone)
    report = watcher.run_once()
    assert report is not None and report.still_waiting == [ZEN]
    calls = len(zen.calls)
    assert watcher.run_once() is None and len(zen.calls) == calls + 1  # only detect(): no retry loop
    browser(zen, open_=True)
    assert watcher.run_once() is None
    browser(zen, open_=False)
    assert watcher.run_once().restored == [ZEN]  # type: ignore[union-attr]


def test_an_apply_tx_whose_revert_failed_is_retried_after_its_browser_closes(setup) -> None:  # type: ignore[no-untyped-def]
    world, zen, system = setup
    original = world.snapshot_all()
    tx = system.apply(A, keep=False)
    browser(zen, open_=True)
    system.revert(tx)
    watcher = Watcher(system.journal, system.adapters)
    assert watcher.run_once() is None  # seen running
    browser(zen, open_=False)
    report = watcher.run_once()
    assert report is not None and report.finished_txs == [tx]
    assert system.journal.state(tx) is TxState.REVERTED and world.snapshot_all() == original
    system.journal.begin("apply", None)  # A3 satisfied again


def test_after_a_restart_a_closed_failed_apply_is_retried_once_then_waits(setup) -> None:  # type: ignore[no-untyped-def]
    """First round after the service starts: nobody saw the browser running, so it is retried once (step 1 before the
    baseline, or the apply's own write would look like a user change)."""
    world, zen, system = setup
    original = reset_with_browser_open(world, zen, system)
    tx = system.apply(B, keep=False)  # allowed by the relaxation
    system.revert(tx)  # browser still open: the apply's own revert fails too
    browser(zen, open_=False)
    report = Watcher(system.journal, system.adapters).run_once()
    assert report is not None and report.finished_txs[0] == tx and report.restored == [ZEN]
    assert world.snapshot_all() == original and system.journal.unfinished() == []
