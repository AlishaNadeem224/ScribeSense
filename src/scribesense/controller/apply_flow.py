"""M2.1 — Apply controller.

Runs one apply from start to finish (ARCHITECTURE.md §2.2), driving the K5
transaction states. The controller NEVER writes to a setting directly: every
write goes through an adapter, and every adapter call is preceded by a journal
record (K3 rule 1, K3 rule 2).

Dependency injection: K1's round_to_steps/validate, K2's resolve, and K7's
build are injected as callables. In production, controller/__init__.py wires
in the real modules. In tests, conftest.py provides stubs that match the
frozen signatures.

Safety invariants enforced here (each is checked by a test):
  - No write without a preceding record_snapshot (with the file's backup bytes)
    and record_intent (K4 rule 2).
  - Every revert goes through M4.11's Reverter, so the K4 rule 4 comparisons
    apply: a location someone else changed is never overwritten.
  - fontconfig runs first; its failure stops and reverts the whole tx.
  - A failed per-adapter revert escalates to a whole-tx revert (K5).
  - PLAN_CONFLICT is detected before any adapter's apply() is called.
  - The controller never closes or restarts an app.
  - After any verify-triggered revert escalates to a terminal state, the
    controller does NOT attempt AWAITING_CONFIRMATION (K5 forbids the
    REVERTED/REVERT_FAILED -> AWAITING_CONFIRMATION transition).
  - A target "passes" verify only if chosen_ok and drawn_ok is not False (M4.13).
  - Once a tx is open, no exception escapes apply(): the tx always ends
    FAILED_CLEAN (nothing written), REVERTED/REVERT_FAILED, or
    AWAITING_CONFIRMATION — never stuck holding the apply lock.
  - Every coverage entry is written through Journal.record_coverage (K4).
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
from scribesense.safety.revert import Reverter

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
      awaiting_confirmation — writes succeeded, transaction is waiting for the
                              user's Keep/Revert (M2.3 owns the window).
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
    #: Why the apply stopped or reverted, for the UI: an ErrorCode value (e.g.
    #: "NO_DISK_SPACE", "BASE_IS_MONOSPACE") or a K1 IssueCode value for an
    #: invalid configuration. None when status is "awaiting_confirmation".
    reason_code: str | None = None


@dataclass
class _PlannedTarget:
    """Per-target bookkeeping during one apply."""

    adapter: Adapter
    target: Target
    items: list[SnapshotItem]
    planned: list[PlannedWrite]
    item_ids: list[ItemId]
    applied: bool = False
    failed: bool = False


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
        """Cheap checks before opening a transaction. Does not write anything.

        Order: adapters present, data dir writable, enough disk space. A
        fontconfig-specific check is not done here — its failure is detected
        during apply, where the whole-tx revert already protects us.
        """
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
        # 1. Resolve the preset's intent into a concrete configuration. K2's
        #    resolve() raises ValueError (with .issues) for an invalid preset.
        try:
            config = self._round_to_steps(self._resolve(preset.values))
        except ScribeSenseError as exc:
            return self._stopped(exc.code.value)
        except (ValueError, LookupError) as exc:
            issues = getattr(exc, "issues", None) or [None]
            return self._stopped(issues[0].code if issues[0] else "INVALID_CONFIGURATION")

        # 2. Validate. Any error -> stop before any tx.
        errors = [i for i in self._validate(config) if i.level == "error"]
        if errors:
            return self._stopped(str(errors[0].code))

        # 3. Preflight.
        problems = self.preflight(config)
        if problems:
            return self._stopped(problems[0].code.value)

        # 4. Build or fetch fonts (K7). Failure -> stop before any tx.
        build = self._generator(config)
        if build.status not in ("built", "cached") or build.fonts is None:
            return self._stopped(build.reason_code or ErrorCode.UNEXPECTED_ADAPTER_ERROR.value)
        fonts = build.fonts

        # 5. Open the transaction (takes the apply lock, K4 rule 1).
        try:
            tx = self._journal.begin("apply", preset_id=preset.id)
        except ScribeSenseError as exc:  # APPLY_IN_PROGRESS / LOCK_TIMEOUT
            return self._stopped(exc.code.value, fonts=fonts)

        coverage: list[CoverageEntry] = []
        try:
            return self._run_transaction(tx, config, fonts, coverage)
        except Exception as exc:  # noqa: BLE001 — the tx must never be left open
            code = exc.code.value if isinstance(exc, ScribeSenseError) else ErrorCode.UNEXPECTED_ADAPTER_ERROR.value
            self._abort(tx)
            return self._final_report(tx, coverage, fonts, [], reason_code=code)

    # ------------------------------------------------------------------ internals

    def _run_transaction(
        self, tx: TxId, config: Configuration, fonts: FontSet, coverage: list[CoverageEntry]
    ) -> ApplyReport:
        restart_targets: list[Target] = []

        # 6–7. Detect, snapshot (with backups) and plan every target. Nothing is
        #      written yet. fontconfig is required: if it cannot be prepared the
        #      whole apply stops (the exception reaches apply(), which aborts).
        plan: list[_PlannedTarget] = []
        for adapter in self._adapters:
            try:
                targets = list(adapter.detect())
            except Exception as exc:  # noqa: BLE001
                if adapter.name == _FIRST_ADAPTER:
                    raise
                self._cover(tx, coverage, self._coverage_from_exception(self._whole_adapter(adapter), exc))
                continue
            for target in targets:
                if adapter.must_be_closed and target.running:
                    self._cover(tx, coverage, self._pending_entry(target))
                    continue
                try:
                    pt = self._prepare(tx, adapter, target, fonts, config)
                except Exception as exc:  # noqa: BLE001
                    if adapter.name == _FIRST_ADAPTER:
                        raise
                    self._cover(tx, coverage, self._coverage_from_exception(target, exc))
                    continue
                plan.append(pt)

        # PLAN_CONFLICT: no two targets may own the same location. Checked on the
        # plans just made, before any intent is recorded or anything is written.
        self._check_plan_conflict(plan)

        # All snapshots recorded -> SNAPSHOTTED.
        self._journal.set_state(tx, TxState.SNAPSHOTTED)

        # 8–9. Apply every planned target. fontconfig first (already sorted).
        self._journal.set_state(tx, TxState.APPLYING)
        for pt in plan:
            if not self._apply_target(tx, pt, coverage):
                if pt.adapter.name == _FIRST_ADAPTER or self._journal.state(tx) is not TxState.APPLYING:
                    # fontconfig failed, or undoing a failed target failed: roll back everything.
                    self._revert_whole_tx(tx)
                    return self._final_report(tx, coverage, fonts, restart_targets,
                                              reason_code=self._last_code(coverage))

        # 10. Verify every applied target.
        self._journal.set_state(tx, TxState.VERIFYING)
        for pt in plan:
            if not pt.applied:
                continue
            if not self._verify_target(tx, pt, fonts, coverage, restart_targets):
                # The per-target revert failed (K5): roll back the whole tx.
                self._revert_whole_tx(tx)
                return self._final_report(tx, coverage, fonts, restart_targets,
                                          reason_code=ErrorCode.REVERT_ITEM_FAILED.value)

        # 11–12. Ready for the confirm flow (M2.3).
        self._journal.set_state(tx, TxState.AWAITING_CONFIRMATION)
        return self._final_report(tx, coverage, fonts, restart_targets)

    def _prepare(
        self, tx: TxId, adapter: Adapter, target: Target, fonts: FontSet, config: Configuration
    ) -> _PlannedTarget:
        """snapshot -> record_snapshot (with backup) -> plan, for one target (K4 rule 2)."""
        items = adapter.snapshot(target)
        item_ids = [
            self._journal.record_snapshot(tx, item, self._file_backup(adapter, item))
            for item in items
        ]
        planned = adapter.plan(target, fonts, config)
        by_location = {item.location: iid for item, iid in zip(items, item_ids)}
        if any(pw.location not in by_location for pw in planned):
            # K3 rule 2: an adapter may only write a location it snapshotted.
            raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR)
        return _PlannedTarget(
            adapter=adapter,
            target=target,
            items=items,
            planned=planned,
            item_ids=[by_location[pw.location] for pw in planned],
        )

    @staticmethod
    def _file_backup(adapter: Adapter, item: SnapshotItem) -> bytes | None:
        """The bytes revert needs to restore an existing file (K4 rule 2: committed before any write)."""
        if item.kind != "file" or not item.existed:
            return None
        data = adapter.read_state(item.location).data
        if data is None:
            raise ScribeSenseError(ErrorCode.BACKUP_MISSING)
        return data

    def _apply_target(self, tx: TxId, pt: _PlannedTarget, coverage: list[CoverageEntry]) -> bool:
        """Apply one target. On failure: NOT_COVERED, and its partial writes are undone.

        Returns True if applied. For fontconfig the caller then rolls back the
        whole tx; for others the apply continues unless undoing failed, which
        moves the tx to REVERTING/REVERT_FAILED (the caller checks the state).
        """
        try:
            for pw, iid in zip(pt.planned, pt.item_ids):
                self._journal.record_intent(tx, iid, pw)  # BEFORE the write
                self._journal.record_applied(tx, iid, pt.adapter.apply(pw))
            pt.applied = True
            return True
        except Exception as exc:  # noqa: BLE001
            pt.failed = True
            self._cover(tx, coverage, self._coverage_from_exception(pt.target, exc))
            if pt.adapter.name != _FIRST_ADAPTER and not self._reverter().revert_items(tx, pt.item_ids).ok:
                self._revert_whole_tx(tx)
            return False

    def _verify_target(
        self,
        tx: TxId,
        pt: _PlannedTarget,
        fonts: FontSet,
        coverage: list[CoverageEntry],
        restart_targets: list[Target],
    ) -> bool:
        """Verify one target. Returns False only if undoing a failed target failed."""
        try:
            result = pt.adapter.verify(pt.target, fonts)
            passed = result.chosen_ok and result.drawn_ok is not False  # M4.13
            detail: ErrorCode | str = result.detail_code
        except Exception as exc:  # noqa: BLE001
            self._cover(tx, coverage, self._coverage_from_exception(pt.target, exc))
            return self._reverter().revert_items(tx, pt.item_ids).ok
        if passed:
            for iid in pt.item_ids:
                self._journal.mark_retained(tx, iid)
            if pt.adapter.requires_restart and pt.target.running:
                restart_targets.append(pt.target)
                self._cover(tx, coverage, self._restart_entry(pt.target))
            else:
                self._cover(tx, coverage, self._covered_entry(pt.target))
            return True
        if detail in ("", "OK"):
            detail = ErrorCode.ADAPTER_VERIFY_FAILED  # e.g. chosen ok but another font drawn
        self._cover(tx, coverage, self._not_covered_entry(pt.target, detail))
        return self._reverter().revert_items(tx, pt.item_ids).ok

    def _reverter(self) -> Reverter:
        return Reverter(self._journal, {a.name: a for a in self._adapters})

    def _revert_whole_tx(self, tx: TxId) -> None:
        """Roll back everything this tx wrote (M4.11). Result: REVERTED or REVERT_FAILED."""
        if self._journal.state(tx) not in (TxState.REVERTED, TxState.REVERT_FAILED):
            self._reverter().finish_tx(tx)

    def _abort(self, tx: TxId) -> None:
        """An unexpected failure with the tx open. FAILED_CLEAN if nothing was planned,
        else a whole-tx revert. If even that fails the tx stays unfinished, so login
        recovery and `scribesense reset` retry it (K5)."""
        try:
            if self._journal.state(tx) not in (TxState.REVERTED, TxState.REVERT_FAILED, TxState.FAILED_CLEAN):
                self._reverter().finish_tx(tx)
        except Exception:  # noqa: BLE001 — never let cleanup mask the original failure
            pass

    def _check_plan_conflict(self, plan: list[_PlannedTarget]) -> None:
        seen: dict[str, str] = {}
        for pt in plan:
            owner = f"{pt.adapter.name}:{pt.target.target_id}"
            for pw in pt.planned:
                if pw.location in seen and seen[pw.location] != owner:
                    raise ScribeSenseError(ErrorCode.PLAN_CONFLICT)
                seen[pw.location] = owner

    def _cover(self, tx: TxId, coverage: list[CoverageEntry], entry: CoverageEntry) -> None:
        """Report one coverage entry and persist it (Journal.record_coverage is the only writer)."""
        coverage.append(entry)
        self._journal.record_coverage(tx, entry)

    @staticmethod
    def _last_code(coverage: list[CoverageEntry]) -> str | None:
        if not coverage:
            return None
        code = coverage[-1].detail_code
        return code.value if isinstance(code, ErrorCode) else str(code)

    @staticmethod
    def _whole_adapter(adapter: Adapter) -> Target:
        """A stand-in target for an adapter whose detect() failed, so it still appears in the report."""
        return Target(adapter=adapter.name, target_id=f"{adapter.name}:*", display_name=adapter.name, running=False)

    @staticmethod
    def _stopped(reason_code: str, *, fonts: FontSet | None = None) -> ApplyReport:
        """Nothing was written and no tx is open."""
        return ApplyReport(tx=None, status="stopped", coverage=(), fonts=fonts,
                           restart_targets=(), reason_code=reason_code)

    def _final_report(
        self,
        tx: TxId,
        coverage: list[CoverageEntry],
        fonts: FontSet,
        restart_targets: list[Target],
        *,
        reason_code: str | None = None,
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
            reason_code = None
        else:
            status = "stopped"
        return ApplyReport(
            tx=tx,
            status=status,
            coverage=tuple(coverage),
            fonts=fonts,
            restart_targets=tuple(restart_targets),
            reason_code=reason_code,
        )

    # Coverage helpers — one place, so the mapping is auditable.
    def _covered_entry(self, target: Target) -> CoverageEntry:
        from scribesense.controller import coverage as _c

        return _c.covered(target)

    def _restart_entry(self, target: Target) -> CoverageEntry:
        from scribesense.controller import coverage as _c

        return _c.restart_needed(target)

    def _pending_entry(self, target: Target) -> CoverageEntry:
        from scribesense.controller import coverage as _c

        return _c.pending(target)

    def _not_covered_entry(self, target: Target, code: ErrorCode | str) -> CoverageEntry:
        from scribesense.controller import coverage as _c

        return _c.not_covered(target, code)

    def _coverage_from_exception(self, target: Target, exc: BaseException) -> CoverageEntry:
        from scribesense.controller import coverage as _c

        return _c.not_covered_from_exception(target, exc)
