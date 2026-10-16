"""O3 — SQL layer for the journal tables (K9 decision D5, accepted by O4 2026-10-15).

Only `store/` may import sqlite3 (import rule R3), so O4's SQLite journal (M4.10, `safety/journal.py`) does NOT
write SQL: it owns the cross-process lock, the K5 state rules and the IPC, and calls this class for persistence.
The method names follow K4's `Journal` so the mapping is one line each. Behaviour matches O4's in-memory double
(`safety/memory_journal.py`) — the same tests must pass on both.

Tables: transactions · journal_items · backups · location_records · active_config · coverage.
Rules kept here (K4 / K9):
  - Every method commits before it returns (record_* are durable); `keep()` is ONE transaction.
  - The first snapshot ever taken of a location becomes its LocationRecord baseline (R2-1); it shares the backup
    row with the item, and a backup row cannot be deleted while anything references it (FK RESTRICT).
  - `Target.running` is not stored; it reads back as False (D2). A planned write keeps its payload (D1).
  - active_config is ONE permanent row; `style_revision` survives "Original" (F3).
  - Retention never deletes location records, their backups, active_config, the newest COMPLETED apply (F2),
    unfinished transactions, or transactions referenced by an unfinished revert / an unresolved location (D4).
  - Stored locations are tool-config locations only (D7); never user documents, text or window titles.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from scribesense.contracts.adapter import AppliedItem, PlannedWrite, SnapshotItem, Target
from scribesense.contracts.coverage import CoverageEntry
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import (
    ActiveConfig,
    ItemId,
    JournalItem,
    LocationRecord,
    RevertOutcome,
    TxId,
    TxKind,
)
from scribesense.contracts.states import TERMINAL, UNFINISHED, TxState, can_transition
from scribesense.store import serialize as ser
from scribesense.store.core import Store
from scribesense.store.repos import coverage_from_row

_UNFINISHED_SQL = "('" + "','".join(sorted(s.value for s in UNFINISHED)) + "')"
_TERMINAL_SQL = "('" + "','".join(sorted(s.value for s in TERMINAL)) + "')"


#: D7 — the shapes of locations ScribeSense itself manages (K3 rules 10 and 10b, plus plain files). Mirrors
#: adapters/conformance.py (a test keeps the two in step). Not enforced by add_item: O4's fake adapter uses free names.
_KEY_LOCATION = re.compile(r"^[A-Za-z0-9_.-]+ [A-Za-z0-9_-]+$")
_JSON_KEY_LOCATION = re.compile(r"^/[^#\x00]+#[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")
_FILE_LOCATION = re.compile(r"^/[^\x00\n\r]+$")


def is_managed_location_shape(location: str) -> bool:
    """True for "<schema> <key>", "<abs path>#<dotted.key>" and an absolute file path."""
    return bool(isinstance(location, str) and len(location) <= 4096 and (
        _KEY_LOCATION.match(location) or _JSON_KEY_LOCATION.match(location) or _FILE_LOCATION.match(location)))


class IllegalTransition(ValueError):
    """A K5 transition that is not allowed (a programming error, not a user-facing failure)."""


def _corrupt() -> ScribeSenseError:
    return ScribeSenseError(ErrorCode.STORE_CORRUPT)


class JournalTables:
    def __init__(self, store: Store, clock: Callable[[], datetime] | None = None) -> None:
        self._s = store
        self._clock = clock or (lambda: store.clock())

    def _now(self) -> str:
        return ser.ts_to_text(self._clock())

    # ------------------------------------------------------------ transactions

    def create_tx(self, kind: TxKind, preset_id: str | None,
                  reverts: TxId | Literal["all"] | None = None) -> TxId:
        if kind not in ("apply", "revert", "uninstall"):
            raise ValueError(f"unknown transaction kind {kind!r}")
        reverts_tx = reverts if isinstance(reverts, int) and not isinstance(reverts, bool) else None
        if reverts is not None and reverts != "all" and reverts_tx is None:
            raise ValueError("reverts must be a TxId, 'all' or None")
        now = self._now()
        cur = self._s.execute(
            "INSERT INTO transactions (kind, state, preset_id, reverts_tx, reverts_all, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (kind, TxState.CREATED.value, preset_id, reverts_tx, 1 if reverts == "all" else 0, now, now))
        return TxId(int(cur.lastrowid))  # type: ignore[arg-type]

    def _tx_row(self, tx: TxId) -> Any:
        row = self._s.fetchone("SELECT * FROM transactions WHERE id = ?", (tx,))
        if row is None:
            raise KeyError(f"unknown tx {tx}")
        return row

    def tx_state(self, tx: TxId) -> TxState:
        try:
            return TxState(self._tx_row(tx)["state"])
        except ValueError:
            raise _corrupt() from None

    def tx_kind(self, tx: TxId) -> TxKind:
        return self._tx_row(tx)["kind"]  # type: ignore[no-any-return]

    def tx_info(self, tx: TxId) -> dict[str, Any]:
        """kind, preset_id, reverts (TxId | 'all' | None), created_at, updated_at."""
        row = self._tx_row(tx)
        reverts: TxId | Literal["all"] | None = (
            "all" if row["reverts_all"] else (TxId(row["reverts_tx"]) if row["reverts_tx"] is not None else None))
        return {"kind": row["kind"], "preset_id": row["preset_id"], "reverts": reverts,
                "created_at": ser.ts_from_text(row["created_at"]), "updated_at": ser.ts_from_text(row["updated_at"])}

    def set_tx_state(self, tx: TxId, state: TxState) -> None:
        if state is TxState.COMPLETED:
            raise IllegalTransition("COMPLETED is reached only through keep()")
        with self._s.transaction():
            current = self.tx_state(tx)
            if not can_transition(current, state):
                raise IllegalTransition(f"{current} -> {state} is not allowed (K5)")
            self._s.execute("UPDATE transactions SET state = ?, updated_at = ? WHERE id = ?",
                            (state.value, self._now(), tx))

    def pending_confirmation(self) -> TxId | None:
        row = self._s.fetchone("SELECT id FROM transactions WHERE state = ? ORDER BY id DESC LIMIT 1",
                               (TxState.AWAITING_CONFIRMATION.value,))
        return None if row is None else TxId(row["id"])

    def unfinished(self) -> list[TxId]:
        rows = self._s.fetchall(f"SELECT id FROM transactions WHERE state IN {_UNFINISHED_SQL} ORDER BY id")
        return [TxId(r["id"]) for r in rows]

    # ------------------------------------------------------------ items and backups

    @staticmethod
    def _target(row: Any) -> Target:
        return Target(row["target_adapter"], row["target_id"], row["target_display_name"], False)

    def _snapshot(self, row: Any) -> SnapshotItem:
        return SnapshotItem(
            target=self._target(row), kind=row["kind"], location=row["location"], existed=bool(row["existed"]),
            old_value=row["old_value"], old_sha256=row["old_sha256"], file_mode=row["file_mode"],
            value_type=row["value_type"])

    def add_item(self, tx: TxId, item: SnapshotItem, file_backup: bytes | None) -> ItemId:
        """One snapshot: backup row (if bytes), the item, and — first time this location is seen — its baseline."""
        if item.kind not in ("file", "key"):
            raise ValueError("snapshot kind must be 'file' or 'key'")
        with self._s.transaction():
            self._tx_row(tx)
            backup_id: int | None = None
            if file_backup is not None:
                backup_id = int(self._s.execute(
                    "INSERT INTO backups (sha256, bytes) VALUES (?, ?)",
                    (hashlib.sha256(file_backup).hexdigest(), file_backup)).lastrowid)  # type: ignore[arg-type]
            target = (item.target.adapter, item.target.target_id, item.target.display_name)
            fields = (item.kind, 1 if item.existed else 0, item.old_value, item.old_sha256, item.file_mode,
                      item.value_type)
            item_id = int(self._s.execute(
                "INSERT INTO journal_items (tx_id, location, target_adapter, target_id, target_display_name, kind, "
                "existed, old_value, old_sha256, file_mode, value_type, backup_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (tx, item.location, *target, *fields, backup_id)).lastrowid)  # type: ignore[arg-type]
            self._s.execute(
                "INSERT OR IGNORE INTO location_records (location, target_adapter, target_id, target_display_name, "
                "kind, existed, old_value, old_sha256, file_mode, value_type, baseline_backup_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (item.location, *target, *fields, backup_id))
            return ItemId(item_id)

    def _item_row(self, tx: TxId, item_id: ItemId) -> Any:
        row = self._s.fetchone("SELECT * FROM journal_items WHERE id = ? AND tx_id = ?", (item_id, tx))
        if row is None:
            raise KeyError(f"item {item_id} is not part of tx {tx}")
        return row

    def set_intent(self, tx: TxId, item_id: ItemId, planned: PlannedWrite) -> None:
        with self._s.transaction():
            row = self._item_row(tx, item_id)
            if planned.location != row["location"]:
                raise ValueError("intent location differs from the snapshot location")
            text = planned.payload if isinstance(planned.payload, str) else None
            blob = planned.payload if isinstance(planned.payload, bytes) else None
            if text is None and blob is None:
                raise ValueError("a planned write's payload is bytes or str")
            self._s.execute("UPDATE journal_items SET intent_sha256 = ?, intent_text = ?, intent_blob = ? "
                            "WHERE id = ?", (planned.new_sha256, text, blob, item_id))

    def set_applied(self, tx: TxId, item_id: ItemId, applied: AppliedItem) -> None:
        with self._s.transaction():
            row = self._item_row(tx, item_id)
            if row["intent_sha256"] is None:
                raise ValueError("record_applied before record_intent (K4 rule 2)")
            self._s.execute("UPDATE journal_items SET applied_sha256 = ? WHERE id = ?",
                            (applied.new_sha256, item_id))

    def set_revert_outcome(self, tx: TxId, item_id: ItemId, outcome: RevertOutcome) -> None:
        if outcome not in ("reverted", "skipped_drift", "failed"):
            raise ValueError(f"unknown revert outcome {outcome!r}")
        with self._s.transaction():
            self._item_row(tx, item_id)
            self._s.execute("UPDATE journal_items SET revert_outcome = ? WHERE id = ?", (outcome, item_id))

    def mark_retained(self, tx: TxId, item_id: ItemId) -> None:
        with self._s.transaction():
            if self._item_row(tx, item_id)["applied_sha256"] is None:
                raise ValueError("only an applied write can be retained")
            self._s.execute("UPDATE journal_items SET retained = 1 WHERE id = ?", (item_id,))

    def items(self, tx: TxId) -> list[JournalItem]:
        self._tx_row(tx)
        out: list[JournalItem] = []
        for row in self._s.fetchall("SELECT * FROM journal_items WHERE tx_id = ? ORDER BY id", (tx,)):
            snapshot = self._snapshot(row)
            intent = None
            if row["intent_sha256"] is not None:
                payload = row["intent_text"] if row["intent_text"] is not None else bytes(row["intent_blob"])
                intent = PlannedWrite(snapshot.target, row["location"], payload, row["intent_sha256"])
            applied = (None if row["applied_sha256"] is None
                       else AppliedItem(snapshot.target, row["location"], row["applied_sha256"]))
            out.append(JournalItem(ItemId(row["id"]), snapshot, intent, applied,
                                   row["revert_outcome"], bool(row["retained"])))
        return out

    def _read_backup(self, backup_id: int | None) -> bytes | None:
        if backup_id is None:
            return None
        row = self._s.fetchone("SELECT sha256, bytes FROM backups WHERE id = ?", (backup_id,))
        if row is None:
            raise ScribeSenseError(ErrorCode.BACKUP_MISSING)
        data = bytes(row["bytes"])
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ScribeSenseError(ErrorCode.BACKUP_CORRUPT)
        return data

    def item_backup(self, tx: TxId, item_id: ItemId) -> bytes | None:
        return self._read_backup(self._item_row(tx, item_id)["backup_id"])

    # ------------------------------------------------------------ coverage

    def add_coverage(self, tx: TxId, entry: CoverageEntry) -> None:
        with self._s.transaction():
            self._tx_row(tx)
            self._s.execute(
                "INSERT OR REPLACE INTO coverage (tx_id, target_adapter, target_id, target_display_name, status, "
                "detail_code) VALUES (?,?,?,?,?,?)",
                (tx, entry.target.adapter, entry.target.target_id, entry.target.display_name,
                 str(entry.status.value), str(entry.detail_code.value if isinstance(entry.detail_code, ErrorCode)
                                              else entry.detail_code)))

    def coverage(self, tx: TxId) -> list[CoverageEntry]:
        return [coverage_from_row(r) for r in self._s.fetchall(
            "SELECT * FROM coverage WHERE tx_id = ? ORDER BY target_adapter, target_id", (tx,))]

    # ------------------------------------------------------------ location records

    def _record(self, row: Any) -> LocationRecord:
        unresolved = None
        if row["unresolved"] is not None:
            try:
                unresolved = ErrorCode(row["unresolved"])
            except ValueError:
                raise _corrupt() from None
        baseline = self._snapshot(row)
        return LocationRecord(
            location=row["location"], target=baseline.target, baseline=baseline,
            baseline_backup_ref=None if row["baseline_backup_id"] is None else str(row["baseline_backup_id"]),
            managed_sha256=row["managed_sha256"], managed_font_key=row["managed_font_key"], unresolved=unresolved)

    def location(self, location: str) -> LocationRecord | None:
        row = self._s.fetchone("SELECT * FROM location_records WHERE location = ?", (location,))
        return None if row is None else self._record(row)

    def locations(self) -> list[LocationRecord]:
        return [self._record(r) for r in self._s.fetchall("SELECT * FROM location_records ORDER BY location")]

    def baseline(self, location: str) -> SnapshotItem | None:
        record = self.location(location)
        return None if record is None else record.baseline

    def baseline_backup(self, location: str) -> bytes | None:
        row = self._s.fetchone("SELECT kind, existed, baseline_backup_id FROM location_records WHERE location = ?",
                               (location,))
        if row is None:
            return None
        if row["kind"] == "file" and row["existed"] and row["baseline_backup_id"] is None:
            raise ScribeSenseError(ErrorCode.BACKUP_MISSING)
        return self._read_backup(row["baseline_backup_id"])

    def clear_managed(self, location: str) -> None:
        cur = self._s.execute("UPDATE location_records SET managed_sha256 = NULL, managed_font_key = NULL "
                              "WHERE location = ?", (location,))
        if cur.rowcount == 0:
            raise KeyError(location)

    def set_unresolved(self, location: str, code: ErrorCode | None) -> None:
        cur = self._s.execute("UPDATE location_records SET unresolved = ? WHERE location = ?",
                              (None if code is None else code.value, location))
        if cur.rowcount == 0:
            raise KeyError(location)

    # ------------------------------------------------------------ keep and the active configuration

    def keep(self, tx: TxId, active: ActiveConfig) -> int:
        """ONE transaction (K4 rule 3): COMPLETED · managed for RETAINED items · active config · revision + 1."""
        config_json = ser.config_to_json(active.config)  # fail before touching anything
        with self._s.transaction():
            if self.tx_state(tx) is not TxState.AWAITING_CONFIRMATION:
                raise ScribeSenseError(ErrorCode.TX_NOT_PENDING)
            self._s.execute(
                "UPDATE location_records SET managed_sha256 = ("
                "  SELECT i.applied_sha256 FROM journal_items i WHERE i.tx_id = ? AND i.retained = 1 "
                "  AND i.applied_sha256 IS NOT NULL AND i.location = location_records.location ORDER BY i.id DESC LIMIT 1),"
                " managed_font_key = ? "
                "WHERE location IN (SELECT location FROM journal_items WHERE tx_id = ? AND retained = 1 "
                "                  AND applied_sha256 IS NOT NULL)",
                (tx, active.font_key, tx))
            revision = self.style_revision() + 1
            self._s.execute(
                "UPDATE active_config SET config = ?, font_key = ?, source_preset_id = ?, style_revision = ? "
                "WHERE id = 1", (config_json, active.font_key, active.source_preset_id, revision))
            self._s.execute("UPDATE transactions SET state = ?, updated_at = ? WHERE id = ?",
                            (TxState.COMPLETED.value, self._now(), tx))
            return revision

    def style_revision(self) -> int:
        row = self._s.fetchone("SELECT style_revision FROM active_config WHERE id = 1")
        if row is None:
            raise _corrupt()
        return int(row["style_revision"])

    def active(self) -> ActiveConfig | None:
        row = self._s.fetchone("SELECT * FROM active_config WHERE id = 1")
        if row is None:
            raise _corrupt()
        if row["config"] is None:
            return None
        return ActiveConfig(config=ser.config_from_json(row["config"]), font_key=row["font_key"],
                            source_preset_id=row["source_preset_id"], style_revision=int(row["style_revision"]))

    def set_original(self) -> int:
        with self._s.transaction():
            revision = self.style_revision() + 1
            self._s.execute("UPDATE active_config SET config = NULL, font_key = NULL, source_preset_id = NULL, "
                            "style_revision = ? WHERE id = 1", (revision,))
            return revision

    # ------------------------------------------------------------ retention (D4) and obligations

    def prune_history(self, before: datetime, keep_last: int) -> int:
        """Delete finished transactions older than `before` that are outside the newest `keep_last` finished ones,
        not the newest COMPLETED apply, and not referenced by an unfinished revert or an unresolved location.
        Backups go only when nothing references them. Order: (created_at, id). Returns the count.
        The caller (journal) holds the apply lock; this method does not know about it."""
        if keep_last < 0:
            raise ValueError("keep_last must be >= 0")
        cutoff = ser.ts_to_text(before)
        with self._s.transaction():
            doomed = [r["id"] for r in self._s.fetchall(
                f"""
                SELECT t.id FROM transactions t
                WHERE t.state IN {_TERMINAL_SQL}
                  AND t.created_at < ?
                  AND t.id NOT IN (SELECT id FROM transactions WHERE state IN {_TERMINAL_SQL}
                                   ORDER BY created_at DESC, id DESC LIMIT ?)
                  AND t.id NOT IN (SELECT id FROM transactions WHERE kind = 'apply' AND state = 'COMPLETED'
                                   ORDER BY created_at DESC, id DESC LIMIT 1)
                  AND t.id NOT IN (SELECT reverts_tx FROM transactions
                                   WHERE state IN {_UNFINISHED_SQL} AND reverts_tx IS NOT NULL)
                  AND t.id NOT IN (SELECT i.tx_id FROM journal_items i
                                   JOIN location_records l ON l.location = i.location WHERE l.unresolved IS NOT NULL)
                ORDER BY t.created_at, t.id
                """, (cutoff, keep_last))]
            for tx_id in doomed:
                self._s.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))  # items + coverage cascade
            self._s.execute(
                "DELETE FROM backups WHERE id NOT IN (SELECT backup_id FROM journal_items WHERE backup_id IS NOT NULL)"
                " AND id NOT IN (SELECT baseline_backup_id FROM location_records WHERE baseline_backup_id IS NOT NULL)")
            return len(doomed)

    def has_obligations(self) -> bool:
        """True while anything is unfinished, unresolved or managed (Journal.no_obligations() returns None then)."""
        return self._s.has_obligations()
