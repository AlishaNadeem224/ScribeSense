"""M4.1 — The conformance suite, run against the fake adapter, and proven to catch broken adapters."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from scribesense.adapters.conformance import AdapterConformance
from scribesense.adapters.fake import FakeAdapter, FakeWorld
from scribesense.contracts.adapter import AppliedItem, PlannedWrite, SnapshotItem, Target
from scribesense.contracts.config import Configuration
from scribesense.contracts.fonts import FontSet

FILE = "/fake/home/.config/fontconfig/conf.d/99-scribesense.conf"
KEY = "org.gnome.desktop.interface font-name"
CFG = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.0)
FONTS = FontSet("abc123", "AccessSans-abc123", "AccessSansUI-abc123", (), ("Regular",))

START_STATES = {
    "present": lambda w: (w.set_file(FILE, b"<fontconfig/>\n", mode=0o600), w.set_key(KEY, "'Fira Sans 11'")),
    "absent": lambda w: (w.set_file(FILE, None), w.set_key(KEY, None)),
    "empty": lambda w: (w.set_file(FILE, b""), w.set_key(KEY, "''")),
}


@dataclass
class FakeHarness:
    adapter: FakeAdapter
    world: FakeWorld
    fonts: FontSet = FONTS
    config: Configuration = CFG

    def fingerprint(self) -> dict[str, str]:
        return self.world.snapshot_all()

    def external_edit(self, location: str) -> None:
        self.world.external_edit(location, b"'changed by someone else'")


def make_harness(start: str, adapter_cls: type[FakeAdapter] = FakeAdapter) -> FakeHarness:
    world = FakeWorld()
    START_STATES[start](world)
    return FakeHarness(adapter_cls(world, {"t1": [FILE, KEY]}), world)


class TestFakeAdapterConformance(AdapterConformance):
    @pytest.fixture(params=sorted(START_STATES))
    def conformance_harness(self, request: pytest.FixtureRequest) -> FakeHarness:
        return make_harness(request.param)


# ------------------------------------------------- the suite must FAIL for broken adapters

class WritesOnSnapshot(FakeAdapter):
    def snapshot(self, target: Target) -> list[SnapshotItem]:
        items = super().snapshot(target)
        self.world._write(FILE, b"oops")
        return items


class WrongPlannedHash(FakeAdapter):
    def plan(self, target, fonts, config):  # type: ignore[no-untyped-def]
        return [PlannedWrite(p.target, p.location, p.payload, "0" * 64) for p in super().plan(target, fonts, config)]


class IgnoresExternalChange(FakeAdapter):
    def apply(self, planned: PlannedWrite) -> AppliedItem:
        self._snapshot_sha[planned.location] = self.world.state(planned.location).sha256  # skips rule 12
        return super().apply(planned)


class TouchesExtraLocation(FakeAdapter):
    def apply(self, planned: PlannedWrite) -> AppliedItem:
        applied = super().apply(planned)
        self.world._write("/fake/home/.bashrc", b"surprise")
        return applied


class RevertWritesDefault(FakeAdapter):
    def revert(self, item: SnapshotItem, backup: bytes | None) -> None:
        if item.kind == "key" and not item.existed:
            self.world._write(item.location, b"'Cantarell 11'")  # writes a default instead of unsetting
            return
        super().revert(item, backup)


class RevertIgnoresMissingBackup(FakeAdapter):
    def revert(self, item: SnapshotItem, backup: bytes | None) -> None:
        if item.kind == "file" and item.existed and backup is None:
            self.world._write(item.location, None)  # silently deletes instead of BACKUP_MISSING
            return
        super().revert(item, backup)


BROKEN = [
    (WritesOnSnapshot, "present", "test_detect_snapshot_plan_and_read_state_write_nothing"),
    (WrongPlannedHash, "present", "test_plan_matches_snapshot_and_hashes_payloads"),
    (IgnoresExternalChange, "present", "test_change_before_apply_is_detected_and_nothing_written"),
    (TouchesExtraLocation, "present", "test_apply_writes_exactly_the_plan"),
    (RevertWritesDefault, "absent", "test_revert_restores_the_exact_original"),
    (RevertIgnoresMissingBackup, "present", "test_missing_backup_for_existing_file_is_backup_missing"),
]


@pytest.mark.parametrize("adapter_cls, start, check", BROKEN, ids=[b[0].__name__ for b in BROKEN])
def test_suite_catches_broken_adapter(adapter_cls: type[FakeAdapter], start: str, check: str) -> None:
    harness = make_harness(start, adapter_cls)
    with pytest.raises(AssertionError):
        getattr(AdapterConformance(), check)(harness)


@pytest.mark.parametrize("start", sorted(START_STATES))
def test_every_check_passes_for_the_correct_fake(start: str) -> None:
    """Guard against a check that fails for the correct adapter (which would make the above meaningless)."""
    suite = AdapterConformance()
    for name in dir(suite):
        if name.startswith("test_"):
            getattr(suite, name)(make_harness(start))
