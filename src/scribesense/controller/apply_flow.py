"""M2.1 — Apply controller.

Runs one apply from start to finish (ARCHITECTURE.md §2.2), driving the K5
transaction states. The controller NEVER writes to a setting directly: every
write goes through an adapter, and every adapter call is preceded by a journal
record (K3 rule 1, K3 rule 2).

The controller's job ends at AWAITING_CONFIRMATION. Who commits the
transaction afterwards (M2.3 countdown, or O4's RecoverySystem) is that
caller's responsibility. This separation is the contract that O4's recovery
tests assume.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from scribesense.contracts.adapter import (
    Adapter,
    PlannedWrite,
    SnapshotItem,
    Target,
)
from scribesense.contracts.config import Configuration, Issue
from scribesense.contracts.coverage import CoverageEntry
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import FontBuildResult, FontSet
from scribesense.contracts.journal import ItemId, Journal, TxId
from scribesense.contracts.presets import Preset, PresetValues
from scribesense.contracts.states import TxState

#: Adapter name that must be applied first (K3 §7, ARCHITECTURE.md step 8).
_FIRST_ADAPTER = "fontconfig"

#: Default minimum free space in data_dir() before preflight refuses.
_DEFAULT_MIN_DISK_BYTES = 100 * 1024 * 1024  # 100 MB


@dataclass(frozen=True)
class Problem:
    """A preflight failure. `code` is always an ErrorCode."""

    code: ErrorCode


@dataclass(frozen=True)
class ApplyReport:
    """Result of one apply.

    status:
      awaiting_confirmation — writes succeeded; the transaction is in
                              AWAITING_CONFIRMATION. The caller (M2.3 countdown
                              window, or O4's RecoverySystem.apply()) will
                              call journal.keep() or revert based on the user's
                              answer.
      stopped               — nothing was written; tx is FAILED_CLEAN (or no tx).
      reverted              — a hard failure caused a whole-tx revert; it succeeded.
      revert_failed         — a hard failure caused a whole-tx revert; some items
                              could not be restored (K5 REVERT_FAILED). Recovery
                              will retry at next login.
    """

    tx: TxId | None
    status: Literal["awaiting_confirmation", "stopped", "reverted", "revert_failed"]
    coverage: tuple[CoverageEntry, ...]
    fonts: FontSet | None
    restart_targets: tuple[Target, ...]


@dataclass
class _PlannedTarget:
    """Per-target bookkeeping during one apply."""

    adapter: Adapter
    target: Target
    items: list[SnapshotItem]
    planned: list[PlannedWrite]
    item_ids: list[ItemId]
    applied: bool = False
    reverted: bool = False
    failed: bool = False
    skipped: bool = False


class ApplyController:
    """Runs one apply. Construct with the journal, the adapters, and the
    injected K1/K2/K7 callables; call apply() once per apply."""

    def __init__(
        self,
        *,
        journal: Journal,
        adapters: Sequence[Adapter],
        generator: Callable[[Configuration], FontBuildResult],
        resolve: Callable[[PresetValues], Configuration],
        round_to_steps: Callable[[Configuration], Configuration],
        validate: Callable[[Configuration], list[Issue]],
        disk_space_min_bytes: int = _DEFAULT_MIN_DISK_BYTES,
        data_dir: Callable[[], Path] | None = None,
    ) -> None:
        self._journal = journal
        # fontconfig first (K3 §7), then the others in the order given.
        self._adapters = sorted(adapters, key=lambda a: (a.name != _FIRST_ADAPTER,))
        self._generator = generator
        self._resolve = resolve
        self._round_to_steps = round_to_steps
        self._validate = validate
        self._min_disk = disk_space_min_bytes
        self._data_dir = data_dir

    # ------------------------------------------------------------------ preflight

    def preflight(self, config: Configuration) -> list[Problem]:
        """Cheap checks before opening a transaction. Does not write anything."""
        problems: list[Problem] = []
        if not self._adapters:
            problems.append(Problem(ErrorCode.ADAPTER_UNAVAILABLE))
        if self._data_dir is not None and self._min_disk > 0:
            try:
                usage = shutil.disk_usage(str(self._data_dir()))
                if usage.free < self._min_disk:
                    problems.append(Problem(ErrorCode.NO_DISK_SPACE))
            except OSError:
                problems.append(Problem(ErrorCode.NOT_WRITABLE))
        return problems

    # ------------------------------------------------------------------ apply

    def apply(self, preset: Preset) -> ApplyReport:
        """Run one apply. See module docstring for the invariants."""
        # 1. Resolve the preset's intent into a concrete configuration.
        try:
            config = self._round_to_steps(self._resolve(preset.values))
        except ScribeSenseError:
            return ApplyReport(
                tx=None, status="stopped", coverage=(), fonts=None, restart_targets=(),
            )

        # 2. Validate. Any error -> stop before any tx.
        issues = self._validate(config)
        if any(i.level == "error" for i in issues):
            return ApplyReport(
                tx=None, status="stopped", coverage=(), fonts=None, restart_targets=(),
            )

        # 3. Preflight.
        problems = self.preflight(config)
        if problems:
            return ApplyReport(
                tx=None, status="stopped", coverage=(), fonts=None, restart_targets=(),
            )

        # 4. Build or fetch fonts (K7). Failure -> stop before any tx.
        build = self._generator(config)
        if build.status not in ("built", "cached") or build.fonts is None:
            return ApplyReport(
                tx=None, status="stopped", coverage=(), fonts=None, restart_targets=(),
            )
        fonts = build.fonts

        # 5. Open the transaction.
        tx = self._journal.begin("apply", preset_id=preset.id)

        try:
            return self._run_transaction(tx, config, fonts)
        except ScribeSenseError:
            self._safe_failed_clean(tx)
            return ApplyReport(
                tx=tx, status="stopped", coverage=(), fonts=fonts, restart_targets=(),
            )

    # ------------------------------------------------------------------ internals

    def _run_transaction(
        self, tx: TxId, config: Configuration, fonts: FontSet
    ) -> ApplyReport:
        coverage: list[CoverageEntry] = []
        restart_targets: list[Target] = []

        # 6. Detect targets per adapter.
        per_adapter: list[tuple[Adapter, list[Target]]] = []
        for adapter in self._adapters:
            targets = adapter.detect()
            per_adapter.append((adapter, list(targets)))

        # 7. PLAN_CONFLICT check: no two targets may own the same location.
        self._check_plan_conflict(per_adapter, fonts, config)

        # 8. Snapshot and record, per target, before any apply.
        plan: list[_PlannedTarget] = []
        for adapter, targets in per_adapter:
            for target in targets:
                if adapter.must_be_closed and target.running:
                    coverage.append(self._pending_entry(target))
                    continue
                items = adapter.snapshot(target)
                item_ids = [
                    self._journal.record_snapshot(tx, item, file_backup=None)
                    for item in items
                ]
                planned = adapter.plan(target, fonts, config)
                if len(planned) != len(items):
                    raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR)
                for pw, iid in zip(planned, item_ids):
                    self._journal.record_intent(tx, iid, pw)
                plan.append(
                    _PlannedTarget(
                        adapter=adapter,
                        target=target,
                        items=items,
                        planned=planned,
                        item_ids=item_ids,
                    )
                )

        # All snapshots recorded -> SNAPSHOTTED.
        self._journal.set_state(tx, TxState.SNAPSHOTTED)

        # 9. Apply every planned target. fontconfig first (already sorted).
        self._journal.set_state(tx, TxState.APPLYING)

        seen: list[_PlannedTarget] = list(plan)
        for pt in plan:
            if pt.adapter.name == _FIRST_ADAPTER:
                self._apply_target_or_revert_all(tx, pt, coverage, seen)
                if pt.failed:
                    return self._final_report(tx, coverage, fonts, restart_targets)
            else:
                self._apply_target(tx, pt, coverage)

        # 10. Verify every applied target.
        self._journal.set_state(tx, TxState.VERIFYING)
        for pt in plan:
            if pt.failed or pt.skipped:
                continue
            self._verify_target(tx, pt, fonts, coverage, restart_targets, seen)

        # 11. If a verify-triggered revert escalated and the tx is already
        #     terminal, do not attempt AWAITING_CONFIRMATION.
        current = self._journal.state(tx)
        if current in (TxState.REVERTED, TxState.REVERT_FAILED):
            return self._final_report(tx, coverage, fonts, restart_targets)

        # 12. Ready for the caller (M2.3 countdown or RecoverySystem) to keep or revert.
        self._journal.set_state(tx, TxState.AWAITING_CONFIRMATION)
        return self._final_report(tx, coverage, fonts, restart_targets)

    def _apply_target_or_revert_all(
        self,
        tx: TxId,
        pt: _PlannedTarget,
        coverage: list[CoverageEntry],
        seen: list[_PlannedTarget],
    ) -> None:
        """Apply the first adapter's target. On any failure, revert the whole tx."""
        try:
            for pw, iid in zip(pt.planned, pt.item_ids):
                applied = pt.adapter.apply(pw)
                self._journal.record_applied(tx, iid, applied)
            pt.applied = True
        except BaseException as exc:  # noqa: BLE001
            pt.failed = True
            coverage.append(self._coverage_from_exception(pt.target, exc))
            self._revert_whole_tx(tx, seen)

    def _apply_target(
        self, tx: TxId, pt: _PlannedTarget, coverage: list[CoverageEntry]
    ) -> None:
        """Apply one non-fontconfig target. A failure marks it NOT_COVERED only."""
        try:
            for pw, iid in zip(pt.planned, pt.item_ids):
                applied = pt.adapter.apply(pw)
                self._journal.record_applied(tx, iid, applied)
            pt.applied = True
        except BaseException as exc:  # noqa: BLE001
            pt.failed = True
            coverage.append(self._coverage_from_exception(pt.target, exc))

    def _verify_target(
        self,
        tx: TxId,
        pt: _PlannedTarget,
        fonts: FontSet,
        coverage: list[CoverageEntry],
        restart_targets: list[Target],
        seen: list[_PlannedTarget],
    ) -> None:
        if not pt.applied:
            return
        try:
            result = pt.adapter.verify(pt.target, fonts)
        except BaseException as exc:  # noqa: BLE001
            coverage.append(self._coverage_from_exception(pt.target, exc))
            self._revert_one_target(tx, pt, coverage, seen)
            return
        if result.chosen_ok:
            for iid in pt.item_ids:
                self._journal.mark_retained(tx, iid)
            coverage.append(self._covered_entry(pt.target))
            if pt.adapter.requires_restart and pt.target.running:
                restart_targets.append(pt.target)
        else:
            coverage.append(self._not_covered_entry(pt.target, result.detail_code))
            self._revert_one_target(tx, pt, coverage, seen)

    def _revert_one_target(
        self,
        tx: TxId,
        pt: _PlannedTarget,
        coverage: list[CoverageEntry],
        seen: list[_PlannedTarget],
    ) -> None:
        """Revert one target. If any revert fails, escalate to a whole-tx revert."""
        revert_failed = False
        for item, iid in zip(pt.items, pt.item_ids):
            try:
                backup = self._journal.backup(tx, iid)
                pt.adapter.revert(item, backup)
                self._journal.record_revert(tx, iid, "reverted")
            except BaseException:  # noqa: BLE001
                self._journal.record_revert(tx, iid, "failed")
                revert_failed = True
        if revert_failed:
            others = [p for p in seen if p is not pt and p.applied and not p.reverted]
            self._revert_whole_tx(tx, others)

    def _revert_whole_tx(self, tx: TxId, seen: list[_PlannedTarget]) -> None:
        """Revert every applied target we have seen. Result: REVERTED or REVERT_FAILED."""
        self._journal.set_state(tx, TxState.REVERTING)
        any_failed = False
        for pt in seen:
            if pt.reverted or not pt.applied:
                continue
            for item, iid in zip(pt.items, pt.item_ids):
                try:
                    backup = self._journal.backup(tx, iid)
                    pt.adapter.revert(item, backup)
                    self._journal.record_revert(tx, iid, "reverted")
                except BaseException:  # noqa: BLE001
                    self._journal.record_revert(tx, iid, "failed")
                    any_failed = True
            pt.reverted = True
        self._journal.set_state(
            tx, TxState.REVERT_FAILED if any_failed else TxState.REVERTED
        )

    def _check_plan_conflict(
        self,
        per_adapter: list[tuple[Adapter, list[Target]]],
        fonts: FontSet,
        config: Configuration,
    ) -> None:
        seen: dict[str, str] = {}
        for adapter, targets in per_adapter:
            for target in targets:
                for pw in adapter.plan(target, fonts, config):
                    owner = f"{adapter.name}:{target.target_id}"
                    if pw.location in seen and seen[pw.location] != owner:
                        raise ScribeSenseError(ErrorCode.PLAN_CONFLICT)
                    seen[pw.location] = owner

    def _safe_failed_clean(self, tx: TxId) -> None:
        try:
            self._journal.set_state(tx, TxState.FAILED_CLEAN)
        except Exception:  # noqa: BLE001
            pass

    def _final_report(
        self,
        tx: TxId,
        coverage: list[CoverageEntry],
        fonts: FontSet,
        restart_targets: list[Target],
    ) -> ApplyReport:
        state = self._journal.state(tx)
        if state is TxState.REVERTED:
            status: Literal[
                "awaiting_confirmation", "stopped", "reverted", "revert_failed"
            ] = "reverted"
        elif state is TxState.REVERT_FAILED:
            status = "revert_failed"
        elif state is TxState.AWAITING_CONFIRMATION:
            status = "awaiting_confirmation"
        else:
            status = "stopped"
        return ApplyReport(
            tx=tx,
            status=status,
            coverage=tuple(coverage),
            fonts=fonts,
            restart_targets=tuple(restart_targets),
        )

    # Coverage helpers — one place, so the mapping is auditable.
    def _covered_entry(self, target: Target) -> CoverageEntry:
        from scribesense.controller import coverage as _c
        return _c.covered(target)

    def _pending_entry(self, target: Target) -> CoverageEntry:
        from scribesense.controller import coverage as _c
        return _c.pending(target)

    def _not_covered_entry(self, target: Target, code: ErrorCode | str) -> CoverageEntry:
        from scribesense.controller import coverage as _c
        return _c.not_covered(target, code)

    def _coverage_from_exception(self, target: Target, exc: BaseException) -> CoverageEntry:
        from scribesense.controller import coverage as _c
        return _c.not_covered_from_exception(target, exc)