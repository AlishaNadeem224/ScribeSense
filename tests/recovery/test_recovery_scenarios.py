"""§9 recovery slice: one fake adapter world, the real journal/revert/recovery.

Scenarios 1–4 and 7 are written; 5 and 6 need the extension (M4.9) and stay skipped.
All of them skip with a BLOCKED reason until O4 provides make_recovery_system (see conftest.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scribesense.adapters.fake import FakeAdapter, FakeWorld
from scribesense.contracts.config import Configuration

pytestmark = pytest.mark.recovery

FILE = "/fake/home/.config/fontconfig/conf.d/99-scribesense.conf"
KEY = "org.gnome.desktop.interface font-name"
OTHER = "/fake/home/.config/qt6ct/qt6ct.conf"

A = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.0)
B = Configuration("Noto Sans", 0.20, 2.5, 1.6, 1.2, 1.0)


@pytest.fixture
def world() -> FakeWorld:
    w = FakeWorld()
    w.set_file(FILE, None)  # originally absent
    w.set_key(KEY, "'Fira Sans 11'")  # originally set by the user
    w.set_file(OTHER, b"[Fonts]\ngeneral=Fira Sans\n")
    return w


def _assert_clean(report) -> None:  # type: ignore[no-untyped-def]
    assert report.failed == [], report.failed
    assert report.skipped == [], report.skipped


def test_s1_keep_a_apply_b_revert_b_reset_gives_original(make_system, world: FakeWorld) -> None:  # type: ignore[no-untyped-def]
    original = world.snapshot_all()
    system = make_system(world, [FakeAdapter(world, {"t": [FILE, KEY, OTHER]})])
    system.apply(A, keep=True)
    tx_b = system.apply(B, keep=False)
    system.revert(tx_b)
    report = system.reset()
    _assert_clean(report)
    assert world.snapshot_all() == original


def test_s2_crash_before_keep_then_reset_gives_original(make_system, world: FakeWorld) -> None:  # type: ignore[no-untyped-def]
    original = world.snapshot_all()
    system = make_system(world, [FakeAdapter(world, {"t": [FILE, KEY, OTHER]})])
    system.apply(A, keep=True)
    system.apply(B, keep=False)  # B written, never kept
    system.crash()
    report = system.reset()  # step 1 recovers B -> A's state, step 2 restores baselines
    _assert_clean(report)
    assert world.snapshot_all() == original


def test_s3_rolled_back_adapter_keeps_previous_managed(make_system, world: FakeWorld) -> None:  # type: ignore[no-untyped-def]
    good = FakeAdapter(world, {"good": [FILE, KEY]}, name="good")
    bad = FakeAdapter(world, {"bad": [OTHER]}, name="bad", verify_ok=False)
    other_original = world.state(OTHER).sha256
    system = make_system(world, [good, bad])
    system.apply(A, keep=True)
    assert system.managed(FILE) is not None and system.managed(KEY) is not None
    assert system.managed(OTHER) is None  # verify failed -> rolled back -> never managed
    assert world.state(OTHER).sha256 == other_original
    managed_file_after_a = system.managed(FILE)
    system.apply(B, keep=True)
    assert system.managed(FILE) != managed_file_after_a
    assert system.managed(OTHER) is None


def test_s4_prune_history_keeps_baselines_restorable(make_system, world: FakeWorld) -> None:  # type: ignore[no-untyped-def]
    original = world.snapshot_all()
    system = make_system(world, [FakeAdapter(world, {"t": [FILE, KEY, OTHER]})])
    system.apply(A, keep=True)
    system.apply(B, keep=True)
    system.prune(datetime.now(timezone.utc) + timedelta(days=365), 0)
    report = system.reset()
    _assert_clean(report)
    assert world.snapshot_all() == original


@pytest.mark.skip(reason="needs the browser extension (M4.9) — §9 scenario 5")
def test_s5_crash_after_keep_before_extension_notified() -> None:
    raise AssertionError("not implemented")


@pytest.mark.skip(reason="needs the browser extension (M4.9) — §9 scenario 6")
def test_s6_standalone_reset_extension_shows_original() -> None:
    raise AssertionError("not implemented")


def test_s7_user_edit_is_skipped_and_reported(make_system, world: FakeWorld) -> None:  # type: ignore[no-untyped-def]
    original = world.snapshot_all()
    system = make_system(world, [FakeAdapter(world, {"t": [FILE, KEY, OTHER]})])
    system.apply(A, keep=True)
    world.external_edit(KEY, "'Comic Sans 14'")  # the user changes a managed setting
    report = system.reset()
    assert report.skipped == [KEY]
    assert report.failed == []
    assert world.state(KEY).data == b"'Comic Sans 14'"  # never overwritten
    for loc in (FILE, OTHER):
        assert world.state(loc).sha256 == original[loc]
