"""O4 — Revert (M4.11): undo transactions with the K4 rule 4 comparisons.

Works against any K4 Journal (the in-memory one today, the SQLite one after K9). Adapters are looked up
by `Target.adapter` name. Never overwrites a value the user changed (drift rule); a failed item keeps
everything needed to retry and is reported with its ErrorCode (K8). An adapter exception is logged
(M4.15: code + adapter/target ids only, never a location).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from scribesense.contracts.adapter import Adapter
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import ItemId, Journal, JournalItem, RevertOutcome, TxId
from scribesense.contracts.states import TERMINAL, TxState
from scribesense.log import log


@dataclass
class RevertReport:
    """Locations by outcome. `restored` lists only locations where a restore was performed or confirmed.

    `failed_codes` maps a failed location to the ErrorCode value saying why (M4.11 review note 2). Filled
    by the tx revert below; a location in `failed` may be missing from it (e.g. a baseline restore).
    """

    restored: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # drift: changed by someone else, left alone
    failed: list[str] = field(default_factory=list)  # recovery information kept; retry later
    failed_codes: dict[str, str] = field(default_factory=dict)  # location -> ErrorCode value

    def merge(self, other: RevertReport) -> None:
        self.restored += [x for x in other.restored if x not in self.restored]
        self.skipped += [x for x in other.skipped if x not in self.skipped]
        self.failed += [x for x in other.failed if x not in self.failed]
        self.failed_codes.update(other.failed_codes)

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
        if state is TxState.SNAPSHOTTED:
            # An intent is recorded, so a write may have happened. K5 has no SNAPSHOTTED -> REVERTING:
            # take the two legal steps through APPLYING (M4.11 review note 1).
            self.journal.set_state(tx, TxState.APPLYING)
            state = TxState.APPLYING
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
            outcome, code = self._revert_one(tx, item)
            self.journal.record_revert(tx, item.item_id, outcome)
            {"reverted": report.restored, "skipped_drift": report.skipped, "failed": report.failed}[outcome].append(loc)
            if code is not None:
                report.failed_codes[loc] = code.value
        return report

    def _revert_one(self, tx: TxId, item: JournalItem) -> tuple[RevertOutcome, ErrorCode | None]:
        """K4 rule 4 for one item. Returns the outcome and, for "failed", the ErrorCode saying why."""
        target = item.snapshot.target
        adapter = self.adapters.get(target.adapter)
        if adapter is None:
            return "failed", ErrorCode.ADAPTER_UNAVAILABLE
        try:
            current = adapter.read_state(item.snapshot.location).sha256
            if current == item.snapshot.old_sha256:
                return "reverted", None  # already the snapshot state
            if item.intent is None:
                return "reverted", None  # nothing was planned, so nothing was written
            if current != item.intent.new_sha256:
                return "skipped_drift", None  # someone else changed it: never overwrite
            adapter.revert(item.snapshot, self.journal.backup(tx, item.item_id))
            after = adapter.read_state(item.snapshot.location).sha256
            if after == item.snapshot.old_sha256:
                return "reverted", None
            return "failed", ErrorCode.REVERT_ITEM_FAILED
        except Exception as exc:  # noqa: BLE001 — any adapter failure is "failed": the item keeps its recovery data
            code = exc.code if isinstance(exc, ScribeSenseError) else ErrorCode.UNEXPECTED_ADAPTER_ERROR
            # M4.15: the code and identifiers only — never the location (a user path) or the message.
            log(code, "revert", adapter=target.adapter, target=target.target_id)
            return "failed", code
