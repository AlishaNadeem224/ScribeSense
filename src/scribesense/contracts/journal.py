"""K4 — Journal (owner O4, O4-only). FROZEN 2026-10-09 · amended R2.

Implemented by O4 in scribesense.safety (M4.10) — BLOCKED until K9 is signed off by O3.

Rules (summary — full text in ARCHITECTURE.md K4):
1. Lock: contracts.paths.lock_path() via fcntl.flock; stable file; holder writes a LockOwner record
   into it. Held from begin() until a terminal state, incl. the whole confirmation period.
2. Durability: every record_* commits before returning. Per item:
   snapshot -> record_snapshot -> plan -> record_intent -> apply -> record_applied -> verify.
3. Keep: the service calls keep() — ONE atomic DB transaction — and only then acknowledges.
   `managed` changes only for retained writes.
4. Recovery: step 1 finish unfinished txs (snapshot / no intent / intent / drift); step 2 (reset,
   uninstall only) restore baselines via LocationRecord (baseline / managed / not ours / drift).
   Absent vs empty vs missing backup are distinct. After a full reset: set_original().
5. IPC: Unix socket contracts.paths.socket_path(), same user only, JSON lines ≤ MAX_MESSAGE_BYTES
   (contracts.messages). Not needed when no service holds the lock.
6. Reset when the lock is held: owner=service -> ask (5 s reply deadline) else pidfd takeover of the
   service + its process group; owner=reset/recover/uninstall -> never interrupted, bounded wait
   (lock_wait_seconds, default 60) -> LOCK_TIMEOUT. Always re-read the journal after acquiring.
Baselines are never pruned automatically; retention prunes transaction history only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, NewType, Protocol

from scribesense.contracts.adapter import AppliedItem, PlannedWrite, SnapshotItem, Target
from scribesense.contracts.config import Configuration
from scribesense.contracts.coverage import CoverageEntry
from scribesense.contracts.errors import ErrorCode
from scribesense.contracts.states import TxState

TxId = NewType("TxId", int)
ItemId = NewType("ItemId", int)

TxKind = Literal["apply", "revert", "uninstall"]
RevertOutcome = Literal["reverted", "skipped_drift", "failed"]
LockRole = Literal["service", "reset", "recover", "uninstall"]

#: K4 rule 6 — reply deadline when asking a running service to reset.
SERVICE_REPLY_DEADLINE_S = 5.0
#: K4 rule 6 — SIGTERM -> grace -> SIGKILL, then bounded wait for the process group to be gone.
TAKEOVER_GRACE_S = 2.0
TAKEOVER_GROUP_WAIT_S = 10.0


@dataclass(frozen=True)
class LockOwner:
    """Written into the lock file by the holder. Identification only — the flock is the lock."""

    role: LockRole
    pid: int
    start_time: int


@dataclass(frozen=True)
class JournalItem:
    item_id: ItemId
    snapshot: SnapshotItem  # incl. existed / old_value / old_sha256
    intent: PlannedWrite | None  # None -> no write was ever planned for this item
    applied: AppliedItem | None  # None -> write not confirmed (may or may not have happened)
    revert_outcome: RevertOutcome | None
    retained: bool  # True only if this write passed verify and is still applied


@dataclass(frozen=True)
class LocationRecord:
    """PERMANENT, one per location — not transaction history."""

    location: str
    target: Target
    baseline: SnapshotItem  # state before ScribeSense first managed it
    baseline_backup_ref: str | None  # reference to the stored original bytes (files)
    managed_sha256: str | None  # last successfully KEPT ScribeSense state; None = not managed
    managed_font_key: str | None
    unresolved: ErrorCode | None  # set when a reset/revert failed or was drift-skipped


@dataclass(frozen=True)
class ActiveConfig:
    """What is actually confirmed — not a preset ID. None (Journal.active()) = Original."""

    config: Configuration
    font_key: str
    source_preset_id: str | None  # informational only
    style_revision: int


class NoRecoveryObligations:
    """Token proving nothing is unfinished, unresolved or managed.

    Only Journal.no_obligations() may create one; Store.delete_recovery_data() requires it.
    """

    __slots__ = ()


class Journal(Protocol):
    def begin(
        self,
        kind: TxKind,
        preset_id: str | None,
        reverts: TxId | Literal["all"] | None = None,
    ) -> TxId:
        """Takes the lock; raises APPLY_IN_PROGRESS if held."""
        ...

    def set_state(self, tx: TxId, state: TxState) -> None: ...  # only K5 transitions

    def record_snapshot(self, tx: TxId, item: SnapshotItem, file_backup: bytes | None) -> ItemId: ...

    def record_intent(self, tx: TxId, item_id: ItemId, planned: PlannedWrite) -> None: ...  # BEFORE the write

    def record_applied(self, tx: TxId, item_id: ItemId, item: AppliedItem) -> None: ...  # after the write

    def record_revert(self, tx: TxId, item_id: ItemId, outcome: RevertOutcome) -> None: ...

    def record_coverage(self, tx: TxId, entry: CoverageEntry) -> None: ...  # the ONLY coverage writer

    def state(self, tx: TxId) -> TxState: ...

    def pending_confirmation(self) -> TxId | None: ...

    def unfinished(self) -> list[TxId]: ...

    def items(self, tx: TxId) -> list[JournalItem]: ...

    def backup(self, tx: TxId, item_id: ItemId) -> bytes | None: ...

    def baseline(self, location: str) -> SnapshotItem | None: ...

    def baseline_backup(self, location: str) -> bytes | None: ...  # BACKUP_MISSING / BACKUP_CORRUPT

    def location(self, location: str) -> LocationRecord | None: ...

    def locations(self) -> list[LocationRecord]: ...

    def mark_retained(self, tx: TxId, item_id: ItemId) -> None: ...

    def keep(self, tx: TxId, active: ActiveConfig) -> int:
        """ONE DB transaction: tx -> COMPLETED · managed records for RETAINED items · active config ·
        style_revision += 1. Returns the new revision."""
        ...

    def clear_managed(self, location: str) -> None: ...

    def set_unresolved(self, location: str, code: ErrorCode | None) -> None: ...

    def active(self) -> ActiveConfig | None: ...

    def set_original(self) -> int: ...  # active = None, style_revision += 1

    def prune_history(self, before: datetime, keep_last: int) -> int: ...

    def no_obligations(self) -> NoRecoveryObligations | None: ...
