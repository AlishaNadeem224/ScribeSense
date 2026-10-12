"""O4 — Adapter conformance suite (M4.1). Every adapter must pass it before review.

TEST SUPPORT ONLY — no production module imports this file. It does not import pytest, so it stays
importable everywhere; pytest collects it through subclasses in the test tree.

How to use (one test file per adapter):

    from scribesense.adapters.conformance import AdapterConformance

    class TestMyAdapterConformance(AdapterConformance):
        @pytest.fixture
        def conformance_harness(self, sandbox):          # parametrize it to cover several start states
            return MyHarness(...)                          # satisfies ConformanceHarness

The harness describes the adapter's test world:
    adapter      the adapter under test, already pointed at the sandbox
    fonts        a FontSet to plan with (a fake one is fine)
    config       a Configuration to plan with
    fingerprint  location -> State.sha256 for EVERY location the adapter could touch, plus anything
                 else observable in the test world. Keys are location strings in K3 rule 10 format.
                 Used to prove read-only steps write nothing and apply/revert write nothing extra.
    external_edit(location)
                 change that location behind the adapter's back (another program / the user).

Rules checked (K3): attributes · detect/snapshot/plan read-only (6, 2) · canonical state and snapshot
items (11, 10) · plan matches snapshot, one write per location, correct hashes (2, 13) · apply writes
exactly the plan and nothing else (2) · change detected right before apply (12) · revert restores the
exact original, incl. absent / unset / empty (7) · revert is repeatable · missing file backup is
BACKUP_MISSING (7) · verify returns a well-formed VerifyResult.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any, Protocol

from scribesense.contracts.adapter import (
    Adapter,
    PlannedWrite,
    SnapshotItem,
    Target,
    VerifyResult,
    state_sha256,
)
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import FontSet

#: K3 rule 10 — settings are "<schema> <key>", e.g. "org.gnome.desktop.interface font-name".
KEY_LOCATION = re.compile(r"^[A-Za-z0-9_.-]+ [A-Za-z0-9_-]+$")
#: K3 rule 10b (amended 2026-10-12): a value inside a JSON file the app rewrites, "<abs path>#<dotted.key.path>".
JSON_KEY_LOCATION = re.compile(r"^/[^#\x00]+#[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")


class ConformanceHarness(Protocol):
    adapter: Adapter
    fonts: FontSet
    config: Configuration

    def fingerprint(self) -> dict[str, str]: ...

    def external_edit(self, location: str) -> None: ...


# ---------------------------------------------------------------- helpers

def expect_error(code: ErrorCode, fn: Callable[..., Any], *args: Any) -> None:
    """Assert fn(*args) raises ScribeSenseError(code)."""
    try:
        fn(*args)
    except ScribeSenseError as exc:
        assert exc.code is code, f"expected {code}, got {exc.code}"
        return
    raise AssertionError(f"expected ScribeSenseError({code}), nothing was raised")


def payload_bytes(payload: bytes | str) -> bytes:
    return payload if isinstance(payload, bytes) else payload.encode()


def first_target(h: ConformanceHarness) -> Target:
    targets = h.adapter.detect()
    assert targets, "harness must set up at least one detectable target"
    return targets[0]


def snapshot_with_backups(h: ConformanceHarness, target: Target) -> list[tuple[SnapshotItem, bytes | None]]:
    """What the controller does before any write: snapshot + file backups (K4 record_snapshot)."""
    result = []
    for item in h.adapter.snapshot(target):
        backup = None
        if item.kind == "file" and item.existed:
            backup = h.adapter.read_state(item.location).data
        result.append((item, backup))
    return result


def apply_all(h: ConformanceHarness, target: Target) -> list[PlannedWrite]:
    planned = h.adapter.plan(target, h.fonts, h.config)
    for p in planned:
        h.adapter.apply(p)
    return planned


def changed(before: dict[str, str], after: dict[str, str]) -> set[str]:
    return {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}


# ---------------------------------------------------------------- the suite

class AdapterConformance:
    """Subclass as `Test<Name>Conformance` and provide a `conformance_harness` fixture."""

    # -- attributes

    def test_declares_k3_attributes(self, conformance_harness: ConformanceHarness) -> None:
        a = conformance_harness.adapter
        assert isinstance(a.name, str) and a.name
        assert isinstance(a.requires_restart, bool)
        assert isinstance(a.must_be_closed, bool)

    def test_detect_returns_unique_targets_of_this_adapter(self, conformance_harness: ConformanceHarness) -> None:
        targets = conformance_harness.adapter.detect()
        assert targets
        assert all(t.adapter == conformance_harness.adapter.name for t in targets)
        ids = [t.target_id for t in targets]
        assert len(ids) == len(set(ids)), "target_id must be unique"

    # -- read-only steps (rules 2, 6)

    def test_detect_snapshot_plan_and_read_state_write_nothing(self, conformance_harness: ConformanceHarness) -> None:
        h = conformance_harness
        before = h.fingerprint()
        target = first_target(h)
        items = h.adapter.snapshot(target)
        for item in items:
            h.adapter.read_state(item.location)
        h.adapter.plan(target, h.fonts, h.config)
        assert h.fingerprint() == before

    # -- canonical state and snapshot items (rules 10, 11)

    def test_snapshot_items_are_canonical(self, conformance_harness: ConformanceHarness) -> None:
        h = conformance_harness
        target = first_target(h)
        items = h.adapter.snapshot(target)
        assert items, "a target must own at least one location"
        locations = [i.location for i in items]
        assert len(locations) == len(set(locations)), "one snapshot item per location"
        for item in items:
            st = h.adapter.read_state(item.location)
            assert item.target == target
            assert item.kind == st.kind
            if item.kind == "file":
                assert os.path.isabs(item.location), f"file location must be absolute: {item.location!r}"
                assert item.old_value is None
                assert (item.file_mode is not None) == item.existed, "file_mode iff the file existed"
            else:
                assert KEY_LOCATION.match(item.location) or JSON_KEY_LOCATION.match(item.location), (
                    f"key location must be '<schema> <key>' or '<abs path>#<dotted.key>': {item.location!r}")
                assert item.file_mode is None
                assert (item.old_value is not None) == item.existed
            assert item.existed == st.present
            assert item.old_sha256 == st.sha256 == state_sha256(st.present, st.data)
            if not st.present:
                assert st.data is None

    # -- plan (rules 2, 13)

    def test_plan_matches_snapshot_and_hashes_payloads(self, conformance_harness: ConformanceHarness) -> None:
        h = conformance_harness
        target = first_target(h)
        snap_locations = {i.location for i in h.adapter.snapshot(target)}
        planned = h.adapter.plan(target, h.fonts, h.config)
        assert planned, "plan must produce at least one write"
        plan_locations = [p.location for p in planned]
        assert len(plan_locations) == len(set(plan_locations)), "one planned write per location"
        assert set(plan_locations) <= snap_locations, "plan may only write locations snapshot returned"
        for p in planned:
            assert p.target == target
            assert p.new_sha256 == state_sha256(True, payload_bytes(p.payload))

    # -- apply (rule 2)

    def test_apply_writes_exactly_the_plan(self, conformance_harness: ConformanceHarness) -> None:
        h = conformance_harness
        target = first_target(h)
        h.adapter.snapshot(target)
        before = h.fingerprint()
        planned = h.adapter.plan(target, h.fonts, h.config)
        for p in planned:
            applied = h.adapter.apply(p)
            assert applied.location == p.location and applied.target == p.target
            assert applied.new_sha256 == p.new_sha256
            assert h.adapter.read_state(p.location).sha256 == p.new_sha256
        assert changed(before, h.fingerprint()) <= {p.location for p in planned}, "apply touched unplanned locations"

    # -- change right before apply (rule 12)

    def test_change_before_apply_is_detected_and_nothing_written(self, conformance_harness: ConformanceHarness) -> None:
        h = conformance_harness
        target = first_target(h)
        h.adapter.snapshot(target)
        planned = h.adapter.plan(target, h.fonts, h.config)[0]
        h.external_edit(planned.location)
        edited = h.adapter.read_state(planned.location).sha256
        expect_error(ErrorCode.CHANGED_DURING_APPLY, h.adapter.apply, planned)
        assert h.adapter.read_state(planned.location).sha256 == edited, "nothing may be written"

    # -- revert (rule 7)

    def test_revert_restores_the_exact_original(self, conformance_harness: ConformanceHarness) -> None:
        h = conformance_harness
        original = h.fingerprint()
        target = first_target(h)
        snaps = snapshot_with_backups(h, target)
        apply_all(h, target)
        for item, backup in snaps:
            h.adapter.revert(item, backup)
        assert h.fingerprint() == original
        for item, _ in snaps:
            st = h.adapter.read_state(item.location)
            assert st.present == item.existed, "absent/unset must be restored as absent/unset, never a default"

    def test_revert_is_repeatable(self, conformance_harness: ConformanceHarness) -> None:
        h = conformance_harness
        original = h.fingerprint()
        target = first_target(h)
        snaps = snapshot_with_backups(h, target)
        apply_all(h, target)
        for _ in range(2):
            for item, backup in snaps:
                h.adapter.revert(item, backup)
        assert h.fingerprint() == original

    def test_missing_backup_for_existing_file_is_backup_missing(self, conformance_harness: ConformanceHarness) -> None:
        h = conformance_harness
        target = first_target(h)
        snaps = snapshot_with_backups(h, target)
        existing_files = [item for item, _ in snaps if item.kind == "file" and item.existed]
        if not existing_files:
            return  # nothing to check in this start state
        apply_all(h, target)
        expect_error(ErrorCode.BACKUP_MISSING, h.adapter.revert, existing_files[0], None)

    # -- verify

    def test_verify_returns_a_well_formed_result(self, conformance_harness: ConformanceHarness) -> None:
        h = conformance_harness
        target = first_target(h)
        h.adapter.snapshot(target)
        apply_all(h, target)
        result = h.adapter.verify(target, h.fonts)
        assert isinstance(result, VerifyResult)
        assert result.target == target
        assert isinstance(result.chosen_ok, bool)
        assert result.drawn_ok is None or isinstance(result.drawn_ok, bool)
        assert isinstance(result.detail_code, str) and result.detail_code
