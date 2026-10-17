"""M2.1 — one test per failure row in the module card.

Every test runs against InMemoryJournal (K4 double) and FakeAdapter (K3
double). No real filesystem, no subprocess, no GTK. The sandbox in
tests/conftest.py blocks both, and these tests must not trip it.

M2.3 (simplified) means the transaction is committed immediately on success,
so `apply()` returns with the journal in COMPLETED, not AWAITING_CONFIRMATION.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from scribesense.adapters.fake import FakeAdapter, FakeWorld
from scribesense.contracts.config import Configuration, Issue
from scribesense.contracts.coverage import CoverageStatus
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import FontBuildResult, FontSet
from scribesense.contracts.presets import Preset, PresetValues
from scribesense.contracts.states import TxState
from scribesense.controller.apply_flow import ApplyController
from scribesense.safety.memory_journal import InMemoryJournal


def _controller(
    journal: InMemoryJournal,
    adapters: list,
    build: Callable[[Configuration], FontBuildResult],
    resolve_fn,
    round_fn,
    validate_fn,
    data_dir,
) -> ApplyController:
    return ApplyController(
        journal=journal,
        adapters=adapters,
        generator=build,
        resolve=resolve_fn,
        round_to_steps=round_fn,
        validate=validate_fn,
        disk_space_min_bytes=0,
        data_dir=data_dir,
    )


# --- 1. Happy path, one adapter ---------------------------------------------

def test_happy_path_one_adapter(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    world.set_file("/tmp/a.conf", b"old")
    fc = FakeAdapter(world, {"default": ["/tmp/a.conf"]}, name="fontconfig")
    ctrl = _controller(journal, [fc], good_build, resolve_fn, round_fn, validate_fn, data_dir)

    report = ctrl.apply(preset)

    assert report.status == "awaiting_confirmation"
    assert report.tx is not None
    # M2.3 simplified: committed immediately, no countdown window.
    assert journal.state(report.tx) is TxState.COMPLETED
    assert journal.active() is not None
    assert world.state("/tmp/a.conf").data.startswith(b"family=")
    assert all(e.status is CoverageStatus.COVERED for e in report.coverage)
    items = journal.items(report.tx)
    assert len(items) == 1
    assert items[0].retained is True


# --- 2. Happy path, two adapters --------------------------------------------

def test_happy_path_two_adapters(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    world.set_file("/tmp/fc.conf", b"old-fc")
    world.set_key("org.gnome.desktop.interface font-name", "Old 11")
    fc = FakeAdapter(world, {"default": ["/tmp/fc.conf"]}, name="fontconfig")
    gtk = FakeAdapter(world, {"gtk": ["org.gnome.desktop.interface font-name"]}, name="gtk")
    ctrl = _controller(
        journal, [gtk, fc], good_build, resolve_fn, round_fn, validate_fn, data_dir
    )

    report = ctrl.apply(preset)

    assert report.status == "awaiting_confirmation"
    assert len(report.coverage) == 2
    assert all(e.status is CoverageStatus.COVERED for e in report.coverage)
    assert fc.calls[0][0] == "detect"
    assert gtk.calls[0][0] == "detect"


# --- 3. Generator refuses ---------------------------------------------------

def test_generator_refuses_stops_before_tx(
    journal, world, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    def refused(config: Configuration) -> FontBuildResult:
        return FontBuildResult("refused", None, "BASE_IS_MONOSPACE")

    fc = FakeAdapter(world, {"default": ["/tmp/a.conf"]}, name="fontconfig")
    ctrl = _controller(journal, [fc], refused, resolve_fn, round_fn, validate_fn, data_dir)

    report = ctrl.apply(preset)

    assert report.status == "stopped"
    assert report.tx is None
    assert journal.unfinished() == []
    assert fc.calls == []


# --- 4. Generator fails -----------------------------------------------------

def test_generator_failed_stops_before_tx(
    journal, world, bad_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    fc = FakeAdapter(world, {"default": ["/tmp/a.conf"]}, name="fontconfig")
    ctrl = _controller(journal, [fc], bad_build, resolve_fn, round_fn, validate_fn, data_dir)

    report = ctrl.apply(preset)

    assert report.status == "stopped"
    assert report.tx is None
    assert fc.calls == []


# --- 5. Preflight fails -----------------------------------------------------

def test_preflight_no_disk_space(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, tmp_path,
):
    fc = FakeAdapter(world, {"default": ["/tmp/a.conf"]}, name="fontconfig")
    d = tmp_path / "data"
    d.mkdir()
    ctrl = ApplyController(
        journal=journal,
        adapters=[fc],
        generator=good_build,
        resolve=resolve_fn,
        round_to_steps=round_fn,
        validate=validate_fn,
        disk_space_min_bytes=10**18,
        data_dir=lambda: d,
    )

    report = ctrl.apply(preset)

    assert report.status == "stopped"
    assert report.tx is None
    assert fc.calls == []


# --- 6. fontconfig apply fails -> whole tx reverted -------------------------

def test_fontconfig_fail_reverts_whole_tx(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    world.set_file("/tmp/fc.conf", b"old-fc")
    world.set_key("org.gnome.desktop.interface font-name", "Old 11")
    fc = FakeAdapter(
        world, {"default": ["/tmp/fc.conf"]},
        name="fontconfig", fail_on={"apply": ErrorCode.ADAPTER_TIMEOUT},
    )
    gtk = FakeAdapter(
        world, {"gtk": ["org.gnome.desktop.interface font-name"]}, name="gtk",
    )
    ctrl = _controller(
        journal, [gtk, fc], good_build, resolve_fn, round_fn, validate_fn, data_dir
    )

    report = ctrl.apply(preset)

    assert report.status == "reverted"
    assert report.tx is not None
    assert journal.state(report.tx) is TxState.REVERTED
    assert world.state("org.gnome.desktop.interface font-name").data == b"Old 11"
    assert world.state("/tmp/fc.conf").data == b"old-fc"


# --- 7. Non-fontconfig fails -> NOT_COVERED, others continue ---------------

def test_non_fontconfig_fail_is_not_covered_and_others_continue(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    world.set_file("/tmp/fc.conf", b"old-fc")
    world.set_key("org.gnome.desktop.interface font-name", "Old 11")
    fc = FakeAdapter(world, {"default": ["/tmp/fc.conf"]}, name="fontconfig")
    gtk = FakeAdapter(
        world, {"gtk": ["org.gnome.desktop.interface font-name"]},
        name="gtk", fail_on={"apply": ErrorCode.ADAPTER_TIMEOUT},
    )
    ctrl = _controller(
        journal, [gtk, fc], good_build, resolve_fn, round_fn, validate_fn, data_dir
    )

    report = ctrl.apply(preset)

    assert report.status == "awaiting_confirmation"
    assert report.tx is not None
    statuses = {e.target.adapter: e.status for e in report.coverage}
    assert statuses["fontconfig"] is CoverageStatus.COVERED
    assert statuses["gtk"] is CoverageStatus.NOT_COVERED
    assert world.state("/tmp/fc.conf").data.startswith(b"family=")


# --- 8. Verify fails -> that target reverted, NOT_COVERED ------------------

def test_verify_fail_reverts_only_that_target(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    world.set_file("/tmp/fc.conf", b"old-fc")
    world.set_key("org.gnome.desktop.interface font-name", "Old 11")
    fc = FakeAdapter(world, {"default": ["/tmp/fc.conf"]}, name="fontconfig")
    gtk = FakeAdapter(
        world, {"gtk": ["org.gnome.desktop.interface font-name"]},
        name="gtk", verify_ok=False,
    )
    ctrl = _controller(
        journal, [gtk, fc], good_build, resolve_fn, round_fn, validate_fn, data_dir
    )

    report = ctrl.apply(preset)

    assert report.status == "awaiting_confirmation"
    statuses = {e.target.adapter: e.status for e in report.coverage}
    assert statuses["fontconfig"] is CoverageStatus.COVERED
    assert statuses["gtk"] is CoverageStatus.NOT_COVERED
    assert world.state("org.gnome.desktop.interface font-name").data == b"Old 11"
    assert world.state("/tmp/fc.conf").data.startswith(b"family=")


# --- 9. Failed per-adapter revert escalates to whole-tx revert -------------

def test_failed_per_adapter_revert_escalates_to_whole_tx_revert(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    world.set_file("/tmp/fc.conf", b"old-fc")
    world.set_key("org.gnome.desktop.interface font-name", "Old 11")
    fc = FakeAdapter(world, {"default": ["/tmp/fc.conf"]}, name="fontconfig")
    gtk = FakeAdapter(
        world, {"gtk": ["org.gnome.desktop.interface font-name"]},
        name="gtk", verify_ok=False, fail_on={"revert": ErrorCode.ADAPTER_TIMEOUT},
    )
    ctrl = _controller(
        journal, [gtk, fc], good_build, resolve_fn, round_fn, validate_fn, data_dir
    )

    report = ctrl.apply(preset)

    assert report.status in ("revert_failed", "reverted")
    assert report.tx is not None
    assert journal.state(report.tx) in (TxState.REVERT_FAILED, TxState.REVERTED)


# --- 10. PLAN_CONFLICT stops before any write ------------------------------

def test_plan_conflict_stops_before_write(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    world.set_file("/tmp/shared.conf", b"orig")
    a1 = FakeAdapter(world, {"t1": ["/tmp/shared.conf"]}, name="fontconfig")
    a2 = FakeAdapter(world, {"t2": ["/tmp/shared.conf"]}, name="gtk")
    ctrl = _controller(
        journal, [a1, a2], good_build, resolve_fn, round_fn, validate_fn, data_dir
    )

    report = ctrl.apply(preset)

    assert report.status == "stopped"
    assert report.tx is not None
    assert journal.state(report.tx) is TxState.FAILED_CLEAN
    assert world.state("/tmp/shared.conf").data == b"orig"


# --- 11. External edit between snapshot and apply -> CHANGED_DURING_APPLY --

def test_changed_during_apply_is_not_covered(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    world.set_file("/tmp/fc.conf", b"old-fc")
    fc = FakeAdapter(world, {"default": ["/tmp/fc.conf"]}, name="fontconfig")

    original_apply = fc.apply

    def apply_with_external_edit(planned):
        world.external_edit("/tmp/fc.conf", b"externally-changed")
        return original_apply(planned)

    fc.apply = apply_with_external_edit  # type: ignore[method-assign]

    ctrl = _controller(journal, [fc], good_build, resolve_fn, round_fn, validate_fn, data_dir)
    report = ctrl.apply(preset)

    assert report.status == "reverted"
    statuses = [e.status for e in report.coverage]
    assert CoverageStatus.NOT_COVERED in statuses


# --- 12. must_be_closed target running -> PENDING -------------------------

def test_must_be_closed_running_target_is_pending(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    world.set_key("brave:Default", "Old 11")
    chromium = FakeAdapter(
        world,
        {"brave:Default": ["brave:Default"]},
        name="chromium",
        must_be_closed=True,
        running=frozenset({"brave:Default"}),
    )
    fc = FakeAdapter(world, {"default": ["/tmp/fc.conf"]}, name="fontconfig")
    world.set_file("/tmp/fc.conf", b"old-fc")
    ctrl = _controller(
        journal, [fc, chromium], good_build, resolve_fn, round_fn, validate_fn, data_dir
    )

    report = ctrl.apply(preset)

    assert report.status == "awaiting_confirmation"
    statuses = {e.target.adapter: e.status for e in report.coverage}
    assert statuses["chromium"] is CoverageStatus.PENDING
    assert world.state("brave:Default").data == b"Old 11"


# --- 13. Unexpected exception -> UNEXPECTED_ADAPTER_ERROR -----------------

def test_unexpected_exception_maps_to_unexpected_adapter_error(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    world.set_file("/tmp/fc.conf", b"old-fc")
    fc = FakeAdapter(world, {"default": ["/tmp/fc.conf"]}, name="fontconfig")

    def explode(planned):
        raise RuntimeError("secret internal detail")

    fc.apply = explode  # type: ignore[method-assign]
    ctrl = _controller(journal, [fc], good_build, resolve_fn, round_fn, validate_fn, data_dir)

    report = ctrl.apply(preset)

    for entry in report.coverage:
        assert "secret internal detail" not in str(entry.detail_code)
    assert report.status in ("reverted", "revert_failed")


# --- 14. Restart targets reported -----------------------------------------

def test_restart_targets_reported(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    world.set_key("gtk:default", "Old 11")
    gtk = FakeAdapter(
        world,
        {"gtk:default": ["gtk:default"]},
        name="gtk",
        requires_restart=True,
        running=frozenset({"gtk:default"}),
    )
    fc = FakeAdapter(world, {"default": ["/tmp/fc.conf"]}, name="fontconfig")
    world.set_file("/tmp/fc.conf", b"old-fc")
    ctrl = _controller(
        journal, [gtk, fc], good_build, resolve_fn, round_fn, validate_fn, data_dir
    )

    report = ctrl.apply(preset)

    assert report.status == "awaiting_confirmation"
    assert len(report.restart_targets) == 1
    assert report.restart_targets[0].adapter == "gtk"


# --- 15. M2.3 simplified: transaction is committed, journal.active set ----

def test_apply_commits_immediately_no_countdown(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    world.set_file("/tmp/a.conf", b"old")
    fc = FakeAdapter(world, {"default": ["/tmp/a.conf"]}, name="fontconfig")
    ctrl = _controller(journal, [fc], good_build, resolve_fn, round_fn, validate_fn, data_dir)

    report = ctrl.apply(preset)

    assert report.status == "awaiting_confirmation"
    assert report.tx is not None
    assert journal.state(report.tx) is TxState.COMPLETED
    active = journal.active()
    assert active is not None
    assert active.font_key == report.fonts.key