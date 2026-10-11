"""O4 — Recovery (M4.12), core logic: `reset` and login recovery (K4 rule 4).

Must never import GTK/Adw or UI modules (A2, enforced by tests/unit/test_import_rules.py).

Built here:   rule 4 step 1 (finish unfinished txs) and step 2 (restore baselines, drift rule),
              report of restored / skipped / failed, set_original() after a full reset.
NOT built yet: lock handling when the lock is already held (K4 rule 6: ask the service over IPC,
              pidfd takeover, bounded wait for other recoveries). Until safety/lock.py and
              safety/ipc.py exist, a held lock surfaces as ScribeSenseError(APPLY_IN_PROGRESS).
"""

from __future__ import annotations

from collections.abc import Mapping

from scribesense.contracts.adapter import Adapter
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import Journal, LocationRecord
from scribesense.contracts.states import TxState
from scribesense.safety.revert import Reverter, RevertReport


def recover_at_login(journal: Journal, adapters: Mapping[str, Adapter]) -> RevertReport:
    """Step 1 only: revert unfinished / unconfirmed txs. Never touches a confirmed configuration (A3)."""
    tx = journal.begin("revert", None, None)
    journal.set_state(tx, TxState.REVERTING)
    reverter = Reverter(journal, adapters)
    report = _finish_unfinished(journal, reverter, exclude=tx)
    journal.set_state(tx, TxState.REVERTED if report.ok else TxState.REVERT_FAILED)
    return report


def reset(journal: Journal, adapters: Mapping[str, Adapter]) -> RevertReport:
    """`scribesense reset`: step 1, then step 2 for every location ScribeSense ever touched."""
    tx = journal.begin("revert", None, "all")
    journal.set_state(tx, TxState.REVERTING)
    reverter = Reverter(journal, adapters)
    report = _finish_unfinished(journal, reverter, exclude=tx)
    for record in journal.locations():
        _restore_baseline(journal, adapters, record, report)
    journal.set_original()  # the report lists anything unresolved; "Original" never implies all restored
    journal.set_state(tx, TxState.REVERTED if report.ok else TxState.REVERT_FAILED)
    return report


def _finish_unfinished(journal: Journal, reverter: Reverter, *, exclude: object) -> RevertReport:
    report = RevertReport()
    for tx in journal.unfinished():
        if tx != exclude:
            report.merge(reverter.finish_tx(tx))
    return report


def _restore_baseline(
    journal: Journal, adapters: Mapping[str, Adapter], record: LocationRecord, report: RevertReport
) -> None:
    loc = record.location
    adapter = adapters.get(record.target.adapter)
    if adapter is None:
        journal.set_unresolved(loc, ErrorCode.ADAPTER_UNAVAILABLE)
        report.failed.append(loc)
        return
    try:
        current = adapter.read_state(loc).sha256
        if current == record.baseline.old_sha256:
            journal.clear_managed(loc)  # already original
            journal.set_unresolved(loc, None)
            return
        if record.managed_sha256 is None:
            return  # not ours: leave it
        if current != record.managed_sha256:
            journal.set_unresolved(loc, ErrorCode.DRIFT_SKIPPED)  # user change: never overwrite
            report.skipped.append(loc)
            return
        adapter.revert(record.baseline, journal.baseline_backup(loc))
        if adapter.read_state(loc).sha256 != record.baseline.old_sha256:
            raise ScribeSenseError(ErrorCode.REVERT_ITEM_FAILED)
        journal.clear_managed(loc)
        journal.set_unresolved(loc, None)
        report.restored.append(loc)
    except ScribeSenseError as exc:
        journal.set_unresolved(loc, exc.code)  # keeps managed + baseline for a later retry
        report.failed.append(loc)
