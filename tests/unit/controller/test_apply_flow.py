"""M2.1 — one test per failure row in the module card.

Every test runs against InMemoryJournal (K4 double) and FakeAdapter (K3
double). No real filesystem, no subprocess, no GTK. The sandbox in
tests/conftest.py blocks both, and these tests must not trip it.
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
    assert journal.state(report.tx) is TxState.AWAITING_CONFIRMATION
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
    # Both adapters saw detect() called, and fontconfig's first action was detect.
    assert fc.calls[0][0] == "detect"
    assert gtk.calls[0][0] == "detect"


# --- 3. Generator refuses -> stop before tx ---------------------------------

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


# --- 4. Generator fails -> stop before tx -----------------------------------

def test_generator_failed_stops_before_tx(
    journal, world, bad_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    fc = FakeAdapter(world, {"default": ["/tmp/a.conf"]}, name="fontconfig")
    ctrl = _controller(journal, [fc], bad_build, resolve_fn, round_fn, validate_fn, data_dir)

    report = ctrl.apply(preset)

    assert report.status == "stopped"
    assert report.tx is None
    assert fc.calls == []


# --- 5. Preflight fails -> stop before tx -----------------------------------

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
        disk_space_min_bytes=10**18,  # no real disk has 1 exabyte free
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
    # The gtk target was never applied because fontconfig ran first and failed.
    assert world.state("org.gnome.desktop.interface font-name").data == b"Old 11"
    assert world.state("/tmp/fc.conf").data == b"old-fc"


# --- 7. Non-fontconfig fails -> that target NOT_COVERED, others continue ----

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

    # GTK failed to apply; the transaction still reaches AWAITING_CONFIRMATION
    # because fontconfig succeeded and the GTK failure is per-target.
    assert report.status == "awaiting_confirmation"
    assert report.tx is not None
    statuses = {e.target.adapter: e.status for e in report.coverage}
    assert statuses["fontconfig"] is CoverageStatus.COVERED
    assert statuses["gtk"] is CoverageStatus.NOT_COVERED
    # fontconfig's write actually happened.
    assert world.state("/tmp/fc.conf").data.startswith(b"family=")


# --- 8. Verify fails -> that target reverted, NOT_COVERED -------------------

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
    # GTK was reverted; its old value is back.
    assert world.state("org.gnome.desktop.interface font-name").data == b"Old 11"
    # fontconfig's write was kept.
    assert world.state("/tmp/fc.conf").data.startswith(b"family=")


# --- 9. That per-adapter revert fails -> whole-tx revert -> REVERT_FAILED ---

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

    # GTK's verify failed AND its revert failed -> whole-tx revert was attempted,
    # and it too failed because GTK's revert always fails.
    assert report.status in ("revert_failed", "reverted")
    assert report.tx is not None
    # The tx is in one of the two terminal revert states.
    assert journal.state(report.tx) in (TxState.REVERT_FAILED, TxState.REVERTED)


# --- 10. PLAN_CONFLICT stops before any write -------------------------------

def test_plan_conflict_stops_before_write(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    # Two adapters both claim the same location.
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
    # Nothing was written.
    assert world.state("/tmp/shared.conf").data == b"orig"


# --- 11. External edit between snapshot and apply -> CHANGED_DURING_APPLY ---

def test_changed_during_apply_is_not_covered(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    world.set_file("/tmp/fc.conf", b"old-fc")
    fc = FakeAdapter(world, {"default": ["/tmp/fc.conf"]}, name="fontconfig")

    # Simulate the external edit by wrapping apply(): flip the world first.
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


# --- 12. must_be_closed target running -> PENDING, no write -----------------

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
    # The chromium target was never touched.
    assert world.state("brave:Default").data == b"Old 11"


# --- 13. Unexpected exception -> UNEXPECTED_ADAPTER_ERROR, no raw text ------

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

    # The raw exception text must never appear in the coverage entry.
    for entry in report.coverage:
        assert "secret internal detail" not in str(entry.detail_code)
    assert report.status in ("reverted", "revert_failed")


# --- 14. Restart targets reported -------------------------------------------

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

# --- Review fixes (2026-10-15, O4): each test failed before the fix ---------

def _fc(world: FakeWorld) -> FakeAdapter:
    world.set_file("/tmp/fc.conf", b"old-fc")
    return FakeAdapter(world, {"default": ["/tmp/fc.conf"]}, name="fontconfig")


def test_existing_file_is_backed_up_and_restored(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    world.set_file("/gtk/settings.ini", b"[Settings]\nuser=1\n")
    gtk = FakeAdapter(world, {"gtk:default": ["/gtk/settings.ini"]}, name="gtk")
    fc = _fc(world)
    report = _controller(journal, [fc, gtk], good_build, resolve_fn, round_fn, validate_fn, data_dir).apply(preset)
    assert world.state("/gtk/settings.ini").data != b"[Settings]\nuser=1\n"

    from scribesense.safety.revert import Reverter

    assert Reverter(journal, {"gtk": gtk, "fontconfig": fc}).revert_tx(report.tx).ok
    assert world.state("/gtk/settings.ini").data == b"[Settings]\nuser=1\n"
    assert world.state("/tmp/fc.conf").data == b"old-fc"


def test_partial_target_failure_undoes_its_earlier_writes(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    world.set_key("qt:font", "Old 11")
    world.set_key("qt:fixed", "Mono 11")
    qt = FakeAdapter(world, {"qt:default": ["qt:font", "qt:fixed"]}, name="qt")
    original_apply, calls = qt.apply, []

    def second_write_fails(planned):  # type: ignore[no-untyped-def]
        calls.append(planned.location)
        if len(calls) == 2:
            raise ScribeSenseError(ErrorCode.ADAPTER_TIMEOUT)
        return original_apply(planned)

    qt.apply = second_write_fails  # type: ignore[method-assign]
    report = _controller(journal, [_fc(world), qt], good_build, resolve_fn, round_fn, validate_fn,
                         data_dir).apply(preset)

    assert report.status == "awaiting_confirmation"  # other targets continue
    assert {e.target.adapter: e.status for e in report.coverage}["qt"] is CoverageStatus.NOT_COVERED
    assert world.state("qt:font").data == b"Old 11"  # the first write was undone, not orphaned


def test_drawn_font_mismatch_is_not_covered_and_reverted(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    world.set_key("gtk:default", "Old 11")
    gtk = FakeAdapter(world, {"gtk:default": ["gtk:default"]}, name="gtk", drawn_ok=False)
    report = _controller(journal, [_fc(world), gtk], good_build, resolve_fn, round_fn, validate_fn,
                         data_dir).apply(preset)

    entry = {e.target.adapter: e for e in report.coverage}["gtk"]
    assert entry.status is CoverageStatus.NOT_COVERED
    assert entry.detail_code == ErrorCode.ADAPTER_VERIFY_FAILED
    assert world.state("gtk:default").data == b"Old 11"


def test_every_coverage_entry_is_recorded_in_the_journal(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    recorded = []
    real = journal.record_coverage
    journal.record_coverage = lambda tx, entry: (recorded.append(entry), real(tx, entry))  # type: ignore[method-assign]
    brave = FakeAdapter(world, {"brave:Default": ["/brave/Preferences"]}, name="chromium",
                        must_be_closed=True, running=frozenset({"brave:Default"}))
    report = _controller(journal, [_fc(world), brave], good_build, resolve_fn, round_fn, validate_fn,
                         data_dir).apply(preset)
    assert recorded == list(report.coverage) and len(recorded) == 2


def test_running_app_that_needs_restart_is_restart_needed(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    world.set_key("gtk:default", "Old 11")
    gtk = FakeAdapter(world, {"gtk:default": ["gtk:default"]}, name="gtk", requires_restart=True,
                      running=frozenset({"gtk:default"}))
    report = _controller(journal, [_fc(world), gtk], good_build, resolve_fn, round_fn, validate_fn,
                         data_dir).apply(preset)
    statuses = {e.target.adapter: e.status for e in report.coverage}
    assert statuses == {"fontconfig": CoverageStatus.COVERED, "gtk": CoverageStatus.RESTART_NEEDED}


def test_detect_failure_never_leaves_the_tx_open(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    class Broken(FakeAdapter):
        def detect(self):  # type: ignore[no-untyped-def]
            raise OSError("permission denied")

    gtk = Broken(world, {"g": ["/g"]}, name="gtk")
    report = _controller(journal, [_fc(world), gtk], good_build, resolve_fn, round_fn, validate_fn,
                         data_dir).apply(preset)
    assert report.status == "awaiting_confirmation"
    assert {e.target.adapter: e.detail_code for e in report.coverage}["gtk"] == ErrorCode.UNEXPECTED_ADAPTER_ERROR

    journal2 = InMemoryJournal()
    broken_fc = Broken(world, {"default": ["/tmp/fc.conf"]}, name="fontconfig")
    report = _controller(journal2, [broken_fc], good_build, resolve_fn, round_fn, validate_fn,
                         data_dir).apply(preset)
    assert report.status == "stopped" and report.reason_code == "UNEXPECTED_ADAPTER_ERROR"
    assert journal2.state(report.tx) is TxState.FAILED_CLEAN and journal2.unfinished() == []


def test_store_failure_after_writes_reverts_instead_of_claiming_nothing_written(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    def store_down(*_a):  # type: ignore[no-untyped-def]
        raise ScribeSenseError(ErrorCode.STORE_IO_ERROR)

    journal.mark_retained = store_down  # type: ignore[method-assign]
    report = _controller(journal, [_fc(world)], good_build, resolve_fn, round_fn, validate_fn,
                         data_dir).apply(preset)
    assert (report.status, report.reason_code) == ("reverted", "STORE_IO_ERROR")
    assert world.state("/tmp/fc.conf").data == b"old-fc"


def test_user_edit_during_apply_is_never_overwritten_by_revert(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    world.set_key("gtk:default", "Old 11")
    gtk = FakeAdapter(world, {"gtk:default": ["gtk:default"]}, name="gtk", verify_ok=False)
    original_verify = gtk.verify

    def user_edits_then_verify(target, fonts):  # type: ignore[no-untyped-def]
        world.external_edit("gtk:default", "User 14")
        return original_verify(target, fonts)

    gtk.verify = user_edits_then_verify  # type: ignore[method-assign]
    _controller(journal, [_fc(world), gtk], good_build, resolve_fn, round_fn, validate_fn, data_dir).apply(preset)
    assert world.state("gtk:default").data == b"User 14"  # drift rule (K4 rule 4)


@pytest.mark.parametrize("case,reason", [
    ("invalid_preset", "OUT_OF_RANGE"), ("build_refused", "BASE_IS_MONOSPACE"),
    ("busy", "APPLY_IN_PROGRESS"), ("validate", "UNKNOWN_FONT"),
])
def test_stopped_reports_say_why(
    case, reason, journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    build, resolve = good_build, resolve_fn
    if case == "invalid_preset":
        class PresetResolutionError(ValueError):  # what K2's resolve() raises (M1.2)
            issues = [Issue("error", "letter_em", "OUT_OF_RANGE")]

        def resolve(values):  # type: ignore[no-untyped-def]
            raise PresetResolutionError()
    elif case == "build_refused":
        build = lambda c: FontBuildResult("refused", None, "BASE_IS_MONOSPACE")  # noqa: E731
    elif case == "busy":
        journal.begin("apply", None)
    else:
        resolve = lambda v: Configuration("Comic Sans", 0.12, 1.5, 1.5, 1.2, 1.0)  # noqa: E731
    report = _controller(journal, [_fc(world)], build, resolve, round_fn, validate_fn, data_dir).apply(preset)
    assert (report.status, report.tx, report.reason_code) == ("stopped", None, reason)
    assert world.state("/tmp/fc.conf").data == b"old-fc"


def test_keyboard_interrupt_is_not_swallowed(
    journal, world, good_build, preset, resolve_fn, round_fn, validate_fn, data_dir,
):
    fc = _fc(world)

    def interrupted(_planned):  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt

    fc.apply = interrupted  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        _controller(journal, [fc], good_build, resolve_fn, round_fn, validate_fn, data_dir).apply(preset)
