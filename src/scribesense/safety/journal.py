"""O4 — Journal (M4.10): the K4 Journal on the SQLite store, plus the apply lock.

Persistence is O3's `store.JournalTables` (K9 D5: the SQL lives in `store/`, import rule R3); this module adds
what K4 gives the journal itself:

  Lock (K4 rule 1)   `begin()` takes `lock_path()` (`safety.lock.ApplyLock`) and holds it until the tx reaches a
                     terminal state (or REVERT_FAILED), including the whole confirmation period. Another holder →
                     `APPLY_IN_PROGRESS`. If the caller already holds the lock — `reset` / `recover --login` take it
                     through `recovery.LockPolicy` before calling `begin("revert")` — the journal uses it and leaves
                     releasing it to the caller. A crash releases the flock through the OS; the unfinished tx stays
                     in the database for login recovery (`unfinished()`).
  Durability (rule 2) every `record_*` is committed by the store before it returns (WAL, synchronous=FULL).
  Keep (rule 3)      `keep()` is one database transaction (JournalTables.keep).
  K5                 illegal transitions are refused by JournalTables (`IllegalTransition`, a ValueError).

"Cannot persist → apply refused before any write" (M4.10 card): a store error in `begin()` releases the lock and
propagates as `ScribeSenseError(STORE_*)`, before the controller has snapshotted or written anything.

Behaviour is kept identical to the in-memory double (`safety.memory_journal.InMemoryJournal`); the tests run the
same journal, revert and recovery scenarios on both.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal

from scribesense.contracts.adapter import AppliedItem, PlannedWrite, SnapshotItem
from scribesense.contracts.coverage import CoverageEntry
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import (
    ActiveConfig,
    ItemId,
    JournalItem,
    LocationRecord,
    LockRole,
    NoRecoveryObligations,
    RevertOutcome,
    TxId,
    TxKind,
)
from scribesense.contracts.states import TERMINAL, TxState
from scribesense.safety.lock import ApplyLock
from scribesense.store import JournalTables, Store


class SqliteJournal:
    """K4 Journal backed by the store. Satisfies scribesense.contracts.journal.Journal.

    One instance per process, sharing that process's `ApplyLock` (flock is per open file, so a second
    `ApplyLock` in the same process would not see this one as the same owner).
    """

    def __init__(
        self,
        store: Store,
        lock: ApplyLock,
        *,
        role: LockRole = "service",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._t = JournalTables(store, clock)
        self._lock = lock
        self._role: LockRole = role
        self._active_tx: TxId | None = None  # the tx this process opened and that still holds the lock
        self._release_on_end = False  # True only if begin() took the lock (not the caller)

    @property
    def lock_held(self) -> bool:
        """True while a tx opened by this journal is not yet terminal."""
        return self._active_tx is not None

    # ------------------------------------------------------------- lock

    def begin(
        self,
        kind: TxKind,
        preset_id: str | None,
        reverts: TxId | Literal["all"] | None = None,
    ) -> TxId:
        if self._active_tx is not None:
            raise ScribeSenseError(ErrorCode.APPLY_IN_PROGRESS)
        took_lock = False
        if not self._lock.held:
            if not self._lock.try_acquire(self._role):
                raise ScribeSenseError(ErrorCode.APPLY_IN_PROGRESS)
            took_lock = True
        try:
            tx = self._t.create_tx(kind, preset_id, reverts)
        except BaseException:
            if took_lock:
                self._lock.release()
            raise
        self._active_tx, self._release_on_end = tx, took_lock
        return tx

    def _ended(self, tx: TxId, state: TxState) -> None:
        if self._active_tx == tx and (state in TERMINAL or state is TxState.REVERT_FAILED):
            self._active_tx = None
            if self._release_on_end:
                self._lock.release()
            self._release_on_end = False

    def set_state(self, tx: TxId, state: TxState) -> None:
        self._t.set_tx_state(tx, state)
        self._ended(tx, state)

    def keep(self, tx: TxId, active: ActiveConfig) -> int:
        revision = self._t.keep(tx, active)
        self._ended(tx, TxState.COMPLETED)
        return revision

    def prune_history(self, before: datetime, keep_last: int) -> int:
        """K9 pruning; refused while any tx is active here or another process holds the apply lock."""
        if keep_last < 0:
            raise ValueError("keep_last must be >= 0")
        if self._active_tx is not None or (not self._lock.held and self._lock.is_locked()):
            raise ScribeSenseError(ErrorCode.APPLY_IN_PROGRESS)
        return self._t.prune_history(before, keep_last)

    # ------------------------------------------------------------- records (K4 rule 2: committed on return)

    def record_snapshot(self, tx: TxId, item: SnapshotItem, file_backup: bytes | None) -> ItemId:
        return self._t.add_item(tx, item, file_backup)

    def record_intent(self, tx: TxId, item_id: ItemId, planned: PlannedWrite) -> None:
        self._t.set_intent(tx, item_id, planned)

    def record_applied(self, tx: TxId, item_id: ItemId, item: AppliedItem) -> None:
        self._t.set_applied(tx, item_id, item)

    def record_revert(self, tx: TxId, item_id: ItemId, outcome: RevertOutcome) -> None:
        self._t.set_revert_outcome(tx, item_id, outcome)

    def record_coverage(self, tx: TxId, entry: CoverageEntry) -> None:
        self._t.add_coverage(tx, entry)

    def mark_retained(self, tx: TxId, item_id: ItemId) -> None:
        self._t.mark_retained(tx, item_id)

    def clear_managed(self, location: str) -> None:
        self._t.clear_managed(location)

    def set_unresolved(self, location: str, code: ErrorCode | None) -> None:
        self._t.set_unresolved(location, code)

    def set_original(self) -> int:
        return self._t.set_original()

    # ------------------------------------------------------------- reads

    def state(self, tx: TxId) -> TxState:
        return self._t.tx_state(tx)

    def pending_confirmation(self) -> TxId | None:
        return self._t.pending_confirmation()

    def unfinished(self) -> list[TxId]:
        return self._t.unfinished()

    def items(self, tx: TxId) -> list[JournalItem]:
        return self._t.items(tx)

    def backup(self, tx: TxId, item_id: ItemId) -> bytes | None:
        return self._t.item_backup(tx, item_id)

    def baseline(self, location: str) -> SnapshotItem | None:
        return self._t.baseline(location)

    def baseline_backup(self, location: str) -> bytes | None:
        return self._t.baseline_backup(location)

    def location(self, location: str) -> LocationRecord | None:
        return self._t.location(location)

    def locations(self) -> list[LocationRecord]:
        return self._t.locations()

    def active(self) -> ActiveConfig | None:
        return self._t.active()

    def style_revision(self) -> int:
        return self._t.style_revision()

    def no_obligations(self) -> NoRecoveryObligations | None:
        return None if self._t.has_obligations() else NoRecoveryObligations()
