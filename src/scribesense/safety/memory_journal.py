"""O4 — In-memory K4 Journal (test double for M4.10).

Implements every K4 method and rule that does not depend on storage, entirely in Python objects —
no database, no K9 schema. Revert (M4.11), recovery (M4.12) and the §9 recovery scenarios run against
it now; the real SQLite journal (after O3's K9 sign-off) must pass the same tests.

What "durable" means here: the InMemoryJournal object itself plays the role of the database — it
survives `simulate_crash()`. Only process-local state (the lock) is lost, as the OS would release a
real flock when the process dies.

Lock (K4 rule 1, simplified): an in-process flag, held from begin() until the holding transaction
reaches a terminal state or REVERT_FAILED (the holding operation is over; the tx stays unfinished and is
retried by recovery). The real cross-process flock + owner record is safety/lock.py (not built yet).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Literal

from scribesense.contracts.adapter import AppliedItem, PlannedWrite, SnapshotItem
from scribesense.contracts.coverage import CoverageEntry
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import (
    ActiveConfig,
    ItemId,
    JournalItem,
    LocationRecord,
    NoRecoveryObligations,
    RevertOutcome,
    TxId,
    TxKind,
)
from scribesense.contracts.states import TERMINAL, UNFINISHED, TxState, can_transition
from scribesense.log import log


class IllegalTransition(ValueError):
    """A K5 transition that is not allowed (programming error, not a user-facing failure)."""


@dataclass
class _Tx:
    id: TxId
    kind: TxKind
    state: TxState
    preset_id: str | None
    reverts: TxId | Literal["all"] | None
    created_at: datetime
    updated_at: datetime


@dataclass
class _Item:
    id: ItemId
    tx: TxId
    snapshot: SnapshotItem
    backup_ref: str | None
    intent: PlannedWrite | None = None
    applied: AppliedItem | None = None
    revert_outcome: RevertOutcome | None = None
    retained: bool = False


@dataclass
class _Backup:
    data: bytes
    sha256: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)



def notify_style_changed(callback: Callable[[int], None] | None, revision: int) -> None:
    """Run the `on_style_changed` hook after a committed Keep / Original. The change is already durable, so
    a failing hook (e.g. no extension listening) is logged and ignored — it never breaks or undoes it."""
    if callback is None:
        return
    try:
        callback(revision)
    except Exception:  # noqa: BLE001
        log(ErrorCode.SERVICE_UNREACHABLE, "journal", step="style_changed")


class InMemoryJournal:
    """K4 Journal without storage. Satisfies scribesense.contracts.journal.Journal."""

    def __init__(self, clock: Callable[[], datetime] = _utcnow, *,
                 on_style_changed: Callable[[int], None] | None = None) -> None:
        self._clock = clock
        self._on_style_changed = on_style_changed  # K11 push hook, see notify_style_changed
        self._txs: dict[TxId, _Tx] = {}
        self._items: dict[ItemId, _Item] = {}
        self._backups: dict[str, _Backup] = {}
        self._locations: dict[str, LocationRecord] = {}
        self._coverage: list[tuple[TxId, CoverageEntry]] = []
        self._active: ActiveConfig | None = None
        self._revision = 0
        self._next_tx = 1
        self._next_item = 1
        self._next_backup = 1
        self._lock_owner: TxId | None = None  # process-local (lost on crash)

    # ------------------------------------------------------------- test support

    def simulate_crash(self) -> None:
        """The process died: the OS releases the lock; everything recorded stays."""
        self._lock_owner = None

    def corrupt_backup(self, ref: str) -> None:
        """Test support: damage a stored backup so BACKUP_CORRUPT can be exercised."""
        self._backups[ref].data = self._backups[ref].data + b"\0corrupt"

    @property
    def lock_held(self) -> bool:
        return self._lock_owner is not None

    # ------------------------------------------------------------- helpers

    def _tx(self, tx: TxId) -> _Tx:
        try:
            return self._txs[tx]
        except KeyError:
            raise KeyError(f"unknown tx {tx}") from None

    def _item(self, tx: TxId, item_id: ItemId) -> _Item:
        item = self._items.get(item_id)
        if item is None or item.tx != tx:
            raise KeyError(f"item {item_id} is not part of tx {tx}")
        return item

    def _store_backup(self, data: bytes) -> str:
        ref = f"backup-{self._next_backup}"
        self._next_backup += 1
        self._backups[ref] = _Backup(data, hashlib.sha256(data).hexdigest())
        return ref

    def _read_backup(self, ref: str | None) -> bytes | None:
        if ref is None:
            return None
        backup = self._backups.get(ref)
        if backup is None:
            raise ScribeSenseError(ErrorCode.BACKUP_MISSING)
        if hashlib.sha256(backup.data).hexdigest() != backup.sha256:
            raise ScribeSenseError(ErrorCode.BACKUP_CORRUPT)
        return backup.data

    def _release_if_owner(self, tx: TxId, state: TxState) -> None:
        if self._lock_owner == tx and (state in TERMINAL or state is TxState.REVERT_FAILED):
            self._lock_owner = None

    # ------------------------------------------------------------- K4 Journal

    def begin(
        self,
        kind: TxKind,
        preset_id: str | None,
        reverts: TxId | Literal["all"] | None = None,
    ) -> TxId:
        if self._lock_owner is not None:
            raise ScribeSenseError(ErrorCode.APPLY_IN_PROGRESS)
        tx = TxId(self._next_tx)
        self._next_tx += 1
        now = self._clock()
        self._txs[tx] = _Tx(tx, kind, TxState.CREATED, preset_id, reverts, now, now)
        self._lock_owner = tx
        return tx

    def set_state(self, tx: TxId, state: TxState) -> None:
        record = self._tx(tx)
        if state is TxState.COMPLETED:
            raise IllegalTransition("COMPLETED is reached only through keep()")
        if not can_transition(record.state, state):
            raise IllegalTransition(f"{record.state} -> {state} is not allowed (K5)")
        record.state = state
        record.updated_at = self._clock()
        self._release_if_owner(tx, state)

    def record_snapshot(self, tx: TxId, item: SnapshotItem, file_backup: bytes | None) -> ItemId:
        self._tx(tx)
        ref = self._store_backup(file_backup) if file_backup is not None else None
        item_id = ItemId(self._next_item)
        self._next_item += 1
        self._items[item_id] = _Item(item_id, tx, item, ref)
        if item.location not in self._locations:  # baseline = first snapshot ever (K4 baseline rule)
            self._locations[item.location] = LocationRecord(
                location=item.location,
                target=item.target,
                baseline=item,
                baseline_backup_ref=ref,
                managed_sha256=None,
                managed_font_key=None,
                unresolved=None,
            )
        return item_id

    def record_intent(self, tx: TxId, item_id: ItemId, planned: PlannedWrite) -> None:
        item = self._item(tx, item_id)
        if planned.location != item.snapshot.location:
            raise ValueError("intent location differs from the snapshot location")
        item.intent = planned

    def record_applied(self, tx: TxId, item_id: ItemId, item: AppliedItem) -> None:
        record = self._item(tx, item_id)
        if record.intent is None:
            raise ValueError("record_applied before record_intent (K4 rule 2)")
        record.applied = item

    def record_revert(self, tx: TxId, item_id: ItemId, outcome: RevertOutcome) -> None:
        self._item(tx, item_id).revert_outcome = outcome

    def record_coverage(self, tx: TxId, entry: CoverageEntry) -> None:
        self._tx(tx)
        self._coverage.append((tx, entry))

    def state(self, tx: TxId) -> TxState:
        return self._tx(tx).state

    def pending_confirmation(self) -> TxId | None:
        pending = [t.id for t in self._txs.values() if t.state is TxState.AWAITING_CONFIRMATION]
        return pending[-1] if pending else None

    def unfinished(self) -> list[TxId]:
        return sorted(t.id for t in self._txs.values() if t.state in UNFINISHED)

    def items(self, tx: TxId) -> list[JournalItem]:
        self._tx(tx)
        return [
            JournalItem(i.id, i.snapshot, i.intent, i.applied, i.revert_outcome, i.retained)
            for i in sorted(self._items.values(), key=lambda i: i.id)
            if i.tx == tx
        ]

    def backup(self, tx: TxId, item_id: ItemId) -> bytes | None:
        return self._read_backup(self._item(tx, item_id).backup_ref)

    def baseline(self, location: str) -> SnapshotItem | None:
        record = self._locations.get(location)
        return record.baseline if record else None

    def baseline_backup(self, location: str) -> bytes | None:
        record = self._locations.get(location)
        if record is None:
            return None
        if record.baseline.kind == "file" and record.baseline.existed and record.baseline_backup_ref is None:
            raise ScribeSenseError(ErrorCode.BACKUP_MISSING)
        return self._read_backup(record.baseline_backup_ref)

    def location(self, location: str) -> LocationRecord | None:
        return self._locations.get(location)

    def locations(self) -> list[LocationRecord]:
        return [self._locations[k] for k in sorted(self._locations)]

    def mark_retained(self, tx: TxId, item_id: ItemId) -> None:
        item = self._item(tx, item_id)
        if item.applied is None:
            raise ValueError("only an applied write can be retained")
        item.retained = True

    def keep(self, tx: TxId, active: ActiveConfig) -> int:
        """One atomic step (K4 rule 3): COMPLETED + managed for RETAINED items + active + revision."""
        record = self._tx(tx)
        if record.state is not TxState.AWAITING_CONFIRMATION:
            raise ScribeSenseError(ErrorCode.TX_NOT_PENDING)
        updates = {}
        for item in self._items.values():
            if item.tx == tx and item.retained and item.applied is not None:
                loc = self._locations[item.snapshot.location]
                updates[loc.location] = replace(
                    loc, managed_sha256=item.applied.new_sha256, managed_font_key=active.font_key
                )
        # --- commit (nothing above can fail half-way) ---
        self._locations.update(updates)
        self._revision += 1
        self._active = replace(active, style_revision=self._revision)
        record.state = TxState.COMPLETED
        record.updated_at = self._clock()
        self._release_if_owner(tx, TxState.COMPLETED)
        notify_style_changed(self._on_style_changed, self._revision)
        return self._revision

    def clear_managed(self, location: str) -> None:
        loc = self._locations[location]
        self._locations[location] = replace(loc, managed_sha256=None, managed_font_key=None)

    def set_unresolved(self, location: str, code: ErrorCode | None) -> None:
        loc = self._locations[location]
        self._locations[location] = replace(loc, unresolved=code)

    def active(self) -> ActiveConfig | None:
        return self._active

    def set_original(self) -> int:
        self._revision += 1
        self._active = None
        notify_style_changed(self._on_style_changed, self._revision)
        return self._revision

    def style_revision(self) -> int:
        return self._revision

    def prune_history(self, before: datetime, keep_last: int) -> int:
        """K9 pruning rule (D4): terminal txs only, never location records, their backups or the active config.

        Kept: the newest `keep_last` terminal txs, the newest COMPLETED apply, txs an unfinished revert refers to,
        and txs with an item at a location that is still unresolved — the same rules as O3's SQL layer
        (`store.journal_tables.JournalTables.prune_history`), so the double and the real journal agree.
        """
        if keep_last < 0:
            raise ValueError("keep_last must be >= 0")
        if self._lock_owner is not None:
            raise ScribeSenseError(ErrorCode.APPLY_IN_PROGRESS)
        ordered = sorted(self._txs.values(), key=lambda t: (t.created_at, t.id))
        finished = [t for t in ordered if t.state in TERMINAL]
        # "always keeping the last N" counts finished transactions only (K4 retention rule)
        newest = {t.id for t in finished[len(finished) - keep_last:]} if keep_last else set()
        referenced = {t.reverts for t in self._txs.values() if t.state in UNFINISHED and isinstance(t.reverts, int)}
        completed = [t for t in finished if t.kind == "apply" and t.state is TxState.COMPLETED]
        if completed:
            referenced.add(completed[-1].id)  # the confirmed configuration's own tx (K9 D4, finding F2)
        unresolved = {loc for loc, r in self._locations.items() if r.unresolved is not None}
        referenced |= {i.tx for i in self._items.values() if i.snapshot.location in unresolved}
        doomed = {
            t.id for t in ordered
            if t.state in TERMINAL and t.created_at < before and t.id not in newest and t.id not in referenced
        }
        for tx in doomed:
            del self._txs[tx]
        for item_id in [i.id for i in self._items.values() if i.tx in doomed]:
            del self._items[item_id]
        self._coverage = [(tx, e) for tx, e in self._coverage if tx not in doomed]
        live_refs = {i.backup_ref for i in self._items.values()} | {
            r.baseline_backup_ref for r in self._locations.values()
        }
        for ref in [r for r in self._backups if r not in live_refs]:
            del self._backups[ref]
        return len(doomed)

    def no_obligations(self) -> NoRecoveryObligations | None:
        if self.unfinished():
            return None
        if any(r.managed_sha256 is not None or r.unresolved is not None for r in self._locations.values()):
            return None
        return NoRecoveryObligations()
