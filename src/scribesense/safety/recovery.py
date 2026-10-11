"""O4 — Recovery (M4.12), core logic: `reset` and login recovery (K4 rule 4).

Must never import GTK/Adw or UI modules (A2, enforced by tests/unit/test_import_rules.py).

Rule 4: step 1 (finish unfinished txs) and step 2 (restore baselines, drift rule); report of
restored / skipped / failed; set_original() after a full reset.

Rule 6 (when an ApplyLock is passed): take the cross-process lock first.
  - lock free                      -> take it.
  - held by the SERVICE            -> ask it over IPC to reset (5 s reply deadline). Acknowledged ->
                                      wait (bounded) for it to finish and release. No reply, or it never
                                      releases -> pidfd takeover of the service + its process group.
  - held by reset/recover/uninstall
    or owner unknown               -> never interrupted: bounded wait, then LOCK_TIMEOUT (nothing killed).
  After acquiring, the journal is RE-READ and rule 4 runs — never assume the other process finished.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from scribesense.contracts.adapter import Adapter
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import (
    SERVICE_REPLY_DEADLINE_S,
    Journal,
    LocationRecord,
    LockOwner,
    LockRole,
)
from scribesense.contracts.states import TxState
from scribesense.safety import ipc
from scribesense.safety.lock import ApplyLock, take_over_service
from scribesense.safety.revert import Reverter, RevertReport

DEFAULT_LOCK_WAIT_S = 60.0  # K9 setting lock_wait_seconds (10–600)
TAKEOVER_ACQUIRE_S = 5.0  # after a successful takeover the OS releases the lock almost immediately


def _ask_service_to_reset(timeout: float) -> bool:
    """True if a running service acknowledged the reset request over IPC."""
    try:
        reply = ipc.request({"type": "reset"}, timeout=timeout)
    except ScribeSenseError:
        return False
    return reply.get("ok") is True


@dataclass
class LockPolicy:
    """How recovery takes the lock. Defaults are the production behaviour; tests may override."""

    lock: ApplyLock
    role: LockRole
    wait_s: float = DEFAULT_LOCK_WAIT_S
    reply_deadline_s: float = SERVICE_REPLY_DEADLINE_S
    ask_service: Callable[[float], bool] = _ask_service_to_reset
    take_over: Callable[[LockOwner], bool] = take_over_service
    on_status: Callable[[str], None] = field(default=lambda _msg: None)  # CLI prints factual status


def acquire_for_recovery(policy: LockPolicy) -> None:
    """K4 rule 6. Raises ScribeSenseError(LOCK_TIMEOUT) if the lock can't be obtained safely."""
    lock = policy.lock
    if lock.try_acquire(policy.role):
        return
    owner = lock.read_owner()
    if owner is None or owner.role != "service":
        who = owner.role if owner else "an unknown process"
        policy.on_status(f"Waiting for {who} to finish (up to {policy.wait_s:.0f} s)…")
        lock.acquire(policy.role, policy.wait_s)  # never interrupt another recovery
        return
    if policy.ask_service(policy.reply_deadline_s):
        policy.on_status("The ScribeSense service is resetting; waiting for it to finish…")
        try:
            lock.acquire(policy.role, policy.wait_s)
            return
        except ScribeSenseError:
            policy.on_status("The service did not finish; taking over.")
    else:
        policy.on_status("The ScribeSense service is not responding; taking over.")
    if not policy.take_over(owner):
        raise ScribeSenseError(ErrorCode.LOCK_TIMEOUT)  # safety checks refused to signal anything
    lock.acquire(policy.role, TAKEOVER_ACQUIRE_S)


def recover_at_login(
    journal: Journal, adapters: Mapping[str, Adapter], *, policy: LockPolicy | None = None
) -> RevertReport:
    """Step 1 only: revert unfinished / unconfirmed txs. Never touches a confirmed configuration (A3)."""
    if policy is not None:
        acquire_for_recovery(policy)
    try:
        tx = journal.begin("revert", None, None)
        journal.set_state(tx, TxState.REVERTING)
        reverter = Reverter(journal, adapters)
        report = _finish_unfinished(journal, reverter, exclude=tx)
        journal.set_state(tx, TxState.REVERTED if report.ok else TxState.REVERT_FAILED)
        return report
    finally:
        if policy is not None:
            policy.lock.release()


def reset(
    journal: Journal, adapters: Mapping[str, Adapter], *, policy: LockPolicy | None = None
) -> RevertReport:
    """`scribesense reset`: step 1, then step 2 for every location ScribeSense ever touched."""
    if policy is not None:
        acquire_for_recovery(policy)
    try:
        tx = journal.begin("revert", None, "all")
        journal.set_state(tx, TxState.REVERTING)
        reverter = Reverter(journal, adapters)
        report = _finish_unfinished(journal, reverter, exclude=tx)
        for record in journal.locations():
            _restore_baseline(journal, adapters, record, report)
        journal.set_original()  # the report lists anything unresolved; "Original" never implies all restored
        journal.set_state(tx, TxState.REVERTED if report.ok else TxState.REVERT_FAILED)
        return report
    finally:
        if policy is not None:
            policy.lock.release()


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
