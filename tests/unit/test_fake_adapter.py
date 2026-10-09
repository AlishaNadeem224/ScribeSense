"""The fake adapter (M4.1) follows the K3 rules, so other owners can trust it in their tests."""

from __future__ import annotations

import pytest

from scribesense.adapters.fake import FakeAdapter, FakeCrash, FakeWorld
from scribesense.contracts.adapter import Adapter, state_sha256
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import FontSet

FILE = "/fake/home/.config/app/fonts.conf"
KEY = "org.gnome.desktop.interface font-name"
CFG = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.0)
FONTS = FontSet("abc123", "AccessSans-abc123", "AccessSansUI-abc123", (), ("Regular",))


@pytest.fixture
def world() -> FakeWorld:
    w = FakeWorld()
    w.set_file(FILE, b"original\n", mode=0o600)
    w.set_key(KEY, "'Fira Sans 11'")
    return w


@pytest.fixture
def adapter(world: FakeWorld) -> FakeAdapter:
    return FakeAdapter(world, {"t1": [FILE, KEY]})


def _apply_all(adapter: FakeAdapter) -> list:  # type: ignore[type-arg]
    target = adapter.detect()[0]
    snaps = adapter.snapshot(target)
    applied = [adapter.apply(p) for p in adapter.plan(target, FONTS, CFG)]
    return [snaps, applied]


def test_satisfies_the_adapter_protocol(adapter: FakeAdapter) -> None:
    a: Adapter = adapter  # static check; attributes present at runtime
    for attr in ("name", "requires_restart", "must_be_closed", "detect", "read_state", "snapshot",
                 "plan", "apply", "verify", "revert"):
        assert hasattr(a, attr)


def test_snapshot_and_plan_are_read_only(adapter: FakeAdapter, world: FakeWorld) -> None:
    before = world.snapshot_all()
    target = adapter.detect()[0]
    adapter.snapshot(target)
    adapter.plan(target, FONTS, CFG)
    assert world.snapshot_all() == before


def test_planned_hash_equals_state_after_apply(adapter: FakeAdapter, world: FakeWorld) -> None:
    target = adapter.detect()[0]
    adapter.snapshot(target)
    for p in adapter.plan(target, FONTS, CFG):
        applied = adapter.apply(p)
        assert applied.new_sha256 == p.new_sha256 == world.state(p.location).sha256


def test_apply_refuses_location_not_snapshotted(adapter: FakeAdapter) -> None:
    target = adapter.detect()[0]
    planned = adapter.plan(target, FONTS, CFG)[0]
    with pytest.raises(ValueError):
        adapter.apply(planned)


def test_changed_during_apply(adapter: FakeAdapter, world: FakeWorld) -> None:
    target = adapter.detect()[0]
    adapter.snapshot(target)
    planned = adapter.plan(target, FONTS, CFG)[0]
    world.external_edit(planned.location, b"someone else\n")
    with pytest.raises(ScribeSenseError) as exc:
        adapter.apply(planned)
    assert exc.value.code is ErrorCode.CHANGED_DURING_APPLY
    assert world.state(planned.location).data == b"someone else\n"  # nothing written


def test_revert_restores_existing_file_and_key(adapter: FakeAdapter, world: FakeWorld) -> None:
    original = world.snapshot_all()
    snaps, _ = _apply_all(adapter)
    assert world.snapshot_all() != original
    for s in snaps:
        adapter.revert(s, b"original\n" if s.kind == "file" else None)
    assert world.snapshot_all() == original
    assert world.mode(FILE) == 0o600


def test_revert_deletes_file_that_did_not_exist(world: FakeWorld) -> None:
    world.set_file("/fake/new.conf", None)
    adapter = FakeAdapter(world, {"t": ["/fake/new.conf"]})
    snaps, _ = _apply_all(adapter)
    assert world.state("/fake/new.conf").present
    adapter.revert(snaps[0], None)
    assert world.state("/fake/new.conf").sha256 == state_sha256(False, None)


def test_revert_unsets_key_that_was_unset(world: FakeWorld) -> None:
    world.set_key("org.x k", None)
    adapter = FakeAdapter(world, {"t": ["org.x k"]})
    snaps, _ = _apply_all(adapter)
    adapter.revert(snaps[0], None)
    assert not world.state("org.x k").present  # unset, not "written back to a default"


def test_revert_restores_present_but_empty_file(world: FakeWorld) -> None:
    world.set_file("/fake/empty.conf", b"")
    adapter = FakeAdapter(world, {"t": ["/fake/empty.conf"]})
    snaps, _ = _apply_all(adapter)
    adapter.revert(snaps[0], b"")
    st = world.state("/fake/empty.conf")
    assert st.present and st.data == b""


def test_missing_backup_for_existing_file_is_an_error(adapter: FakeAdapter) -> None:
    snaps, _ = _apply_all(adapter)
    file_snap = next(s for s in snaps if s.kind == "file")
    with pytest.raises(ScribeSenseError) as exc:
        adapter.revert(file_snap, None)
    assert exc.value.code is ErrorCode.BACKUP_MISSING


@pytest.mark.parametrize("step", ["detect", "snapshot", "plan", "apply", "verify", "revert", "read_state"])
def test_fail_on_raises_the_injected_code(world: FakeWorld, step: str) -> None:
    adapter = FakeAdapter(world, {"t": [FILE]}, fail_on={step: ErrorCode.ADAPTER_TIMEOUT})
    with pytest.raises(ScribeSenseError) as exc:
        target = adapter.detect()[0] if step != "detect" else None
        if step == "detect":
            adapter.detect()
        elif step == "read_state":
            adapter.read_state(FILE)
        else:
            snaps = adapter.snapshot(target)  # type: ignore[arg-type]
            plans = adapter.plan(target, FONTS, CFG)  # type: ignore[arg-type]
            adapter.apply(plans[0])
            adapter.verify(target, FONTS)  # type: ignore[arg-type]
            adapter.revert(snaps[0], b"original\n")
    assert exc.value.code is ErrorCode.ADAPTER_TIMEOUT


def test_crash_after_apply_leaves_the_write_in_place(world: FakeWorld) -> None:
    adapter = FakeAdapter(world, {"t": [FILE]}, crash_after="apply")
    target = adapter.detect()[0]
    adapter.snapshot(target)
    planned = adapter.plan(target, FONTS, CFG)[0]
    with pytest.raises(FakeCrash):
        adapter.apply(planned)
    assert world.state(FILE).sha256 == planned.new_sha256  # written, but never acknowledged


def test_verify_failure_is_reported_not_raised(world: FakeWorld) -> None:
    adapter = FakeAdapter(world, {"t": [FILE]}, verify_ok=False)
    result = adapter.verify(adapter.detect()[0], FONTS)
    assert result.chosen_ok is False and result.detail_code == ErrorCode.ADAPTER_VERIFY_FAILED


def test_unknown_step_name_is_rejected(world: FakeWorld) -> None:
    with pytest.raises(ValueError):
        FakeAdapter(world, {"t": [FILE]}, fail_on={"aply": ErrorCode.ADAPTER_TIMEOUT})


def test_running_targets_are_reported(world: FakeWorld) -> None:
    adapter = FakeAdapter(world, {"brave:Default": [FILE]}, must_be_closed=True,
                          running=frozenset({"brave:Default"}))
    assert adapter.detect()[0].running is True
