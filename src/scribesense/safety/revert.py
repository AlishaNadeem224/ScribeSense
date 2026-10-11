"""O4 — Revert (M4.11): undo transactions with the K4 rule 4 comparisons.

Works against any K4 Journal (the in-memory one today, the SQLite one after K9). Adapters are looked up
by `Target.adapter` name. Never overwrites a value the user changed (drift rule); a failed item keeps
everything needed to retry and is reported.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from scribesense.contracts.adapter import Adapter
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import ItemId, Journal, JournalItem, RevertOutcome, TxId
from scribesense.contracts.states import TERMINAL, TxState


@dataclass
class RevertReport:
    """Locations by outcome. `restored` lists only locations where a restore was performed or confirmed."""

    restored: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # drift: changed by someone else, left alone
    failed: list[str] = field(default_factory=list)  # recovery information kept; retry later

    def merge(self, other: RevertReport) -> None:
        self.restored += [x for x in other.restored if x not in self.restored]
        self.skipped += [x for x in other.skipped if x not in self.skipped]
        self.failed += [x for x in other.failed if x not in self.failed]

    @property
    def ok(self) -> bool:
        return not self.failed


class Reverter:
    def __init__(self, journal: Journal, adapters: Mapping[str, Adapter]) -> None:
        self.journal = journal
        self.adapters = dict(adapters)

    # ------------------------------------------------------------- step 1 (K4 rule 4)

    def revert_items(self, tx: TxId, item_ids: Iterable[ItemId]) -> RevertReport:
        """Revert selected items of one tx (e.g. one adapter after a failed verify). No state change."""
        wanted = set(item_ids)
        items = [i for i in self.journal.items(tx) if i.item_id in wanted]
        return self._revert(tx, items)

    def finish_tx(self, tx: TxId) -> RevertReport:
        """Bring an unfinished tx to REVERTED / REVERT_FAILED / FAILED_CLEAN. Safe to repeat."""
        state = self.journal.state(tx)
        if state in TERMINAL:
            raise ScribeSenseError(ErrorCode.TX_NOT_PENDING)
        if state in (TxState.CREATED, TxState.SNAPSHOTTED) and not any(
            i.intent is not None for i in self.journal.items(tx)
        ):
            # Nothing was ever planned, so nothing was written.
            self.journal.set_state(tx, TxState.FAILED_CLEAN)
            return RevertReport()
        if state is not TxState.REVERTING:
            self.journal.set_state(tx, TxState.REVERTING)
        report = self._revert(tx, self.journal.items(tx))
        self.journal.set_state(tx, TxState.REVERTED if report.ok else TxState.REVERT_FAILED)
        return report

    def revert_tx(self, tx: TxId) -> RevertReport:
        """User pressed Revert, or the confirmation timed out."""
        return self.finish_tx(tx)

    def _revert(self, tx: TxId, items: list[JournalItem]) -> RevertReport:
        report = RevertReport()
        for item in sorted(items, key=lambda i: i.item_id, reverse=True):  # undo the latest write first
            loc = item.snapshot.location
            if item.revert_outcome in ("reverted", "skipped_drift"):
                (report.restored if item.revert_outcome == "reverted" else report.skipped).append(loc)
                continue
            outcome = self._revert_one(tx, item)
            self.journal.record_revert(tx, item.item_id, outcome)
            {"reverted": report.restored, "skipped_drift": report.skipped, "failed": report.failed}[outcome].append(loc)
        return report

    def _revert_one(self, tx: TxId, item: JournalItem) -> RevertOutcome:
        adapter = self.adapters.get(item.snapshot.target.adapter)
        if adapter is None:
            return "failed"
        try:
            current = adapter.read_state(item.snapshot.location).sha256
            if current == item.snapshot.old_sha256:
                return "reverted"  # already the snapshot state
            if item.intent is None:
                return "reverted"  # nothing was planned, so nothing was written
            if current != item.intent.new_sha256:
                return "skipped_drift"  # someone else changed it: never overwrite
            adapter.revert(item.snapshot, self.journal.backup(tx, item.item_id))
            after = adapter.read_state(item.snapshot.location).sha256
            return "reverted" if after == item.snapshot.old_sha256 else "failed"
        except ScribeSenseError:
            return "failed"
