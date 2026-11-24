"""O4 — "Finish when closed" (2026-11-24, built by O4 for the O2 Phase III item, user's go-ahead).

A restore that met an open browser (Zen / Firefox `prefs.js`, a Chromium profile without the extension) is refused
with BROWSER_RUNNING and writes nothing; the journal keeps everything needed to retry. This module finishes it by
itself once that browser is closed, so the user never has to press Original again.

Two pieces:

`only_waiting_for_apps(...)` — the A3 relaxation used by both journals' `begin("apply")`: unfinished work does NOT
    block a new Apply when it is only waiting for an app to close. Exactly: every unfinished tx is a `revert` tx in
    REVERT_FAILED (a reset or a login recovery; those carry no items of their own — what is left is on the location
    records), and every unresolved location says BROWSER_RUNNING or DRIFT_SKIPPED. Anything else — an apply tx whose
    own items failed, a missing adapter, a failed write, an uninstall — still blocks (A3 as before).

`finish_when_closed(journal, adapters)` — one round, run by the service on a background thread:
  1. A location record with `unresolved == BROWSER_RUNNING` is retried only when its target is detected as NOT
     running now (a fresh `detect()`, read-only). It goes through `recovery._restore_baseline` — the same drift rule
     as `reset`: only a value that is still exactly ours is restored; a user change is never overwritten.
  2. Never undo a NEWER change: a location re-managed by the confirmed style (its `managed_font_key` is the active
     font key) is left alone — restoring its baseline would undo the user's latest Keep. With no active style
     (Original) every managed location is restored, which is what the reset asked for.
  3. An `apply` tx in REVERT_FAILED is retried FIRST (`Reverter.finish_tx`, step 1 as in reset and login recovery)
     when its failed items' targets are all closed and one of them was seen running by this watcher (or on the
     watcher's first round, once). A3 still blocks new applies while one exists, so no newer change can be under it.
     A location such a tx still owes is not baseline-restored in the same round (its write would look like drift).
  4. Older `revert` txs in REVERT_FAILED are closed once no location waits for a browser and nothing else is
     unresolved.
  The round runs inside its own `revert` tx (the K4 lock): if an apply, a confirm window or a reset holds the lock
  the round is skipped (nothing waits). Nothing ready → nothing is opened at all.

Never closes, kills or restarts an application (AGENTS.md §5). Logs codes and adapter / target ids only (M4.15).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from scribesense.contracts.adapter import Adapter
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import Journal, LocationRecord, TxId
from scribesense.contracts.states import TxState
from scribesense.log import log
from scribesense.safety.revert import Reverter, RevertReport

#: Unresolved codes that only wait for an app (or record a user change, which is final) — they never block an Apply.
WAITING_CODES = frozenset({ErrorCode.BROWSER_RUNNING, ErrorCode.DRIFT_SKIPPED})


def only_waiting_for_apps(
    unfinished: Iterable[tuple[str, TxState]], unresolved: Iterable[ErrorCode | None]
) -> bool:
    """The A3 relaxation. `unfinished`: (kind, state) of every unfinished tx (non-empty when called);
    `unresolved`: the `unresolved` code of every location record."""
    if any(kind != "revert" or state is not TxState.REVERT_FAILED for kind, state in unfinished):
        return False
    return all(code is None or code in WAITING_CODES for code in unresolved)


@dataclass
class WatchReport:
    restored: list[str] = field(default_factory=list)
    still_waiting: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # a real failure now (code on the location / tx)
    finished_txs: list[TxId] = field(default_factory=list)


@dataclass
class Watcher:
    """Remembers which targets it saw running, so a failed apply tx is retried once per close, not every round."""

    journal: Journal
    adapters: Mapping[str, Adapter]
    _seen_running: set[tuple[str, str]] = field(default_factory=set)
    _tried: set[str] = field(default_factory=set)  # tried while closed, still refused: wait until seen running again
    _first: bool = True  # the first round after start retries closed apply txs once (nobody saw them running)

    # ------------------------------------------------------------- what is waiting

    def _running(self, adapter_names: Iterable[str]) -> set[tuple[str, str]] | None:
        """(adapter, target_id) pairs running now, for these adapters. None if detection failed (try again later)."""
        running: set[tuple[str, str]] = set()
        for name in set(adapter_names):
            adapter = self.adapters.get(name)
            if adapter is None:
                continue
            try:
                running |= {(name, t.target_id) for t in adapter.detect() if t.running}
            except Exception as exc:  # noqa: BLE001 — unclear: treat as still running, retry next round
                log(exc.code if isinstance(exc, ScribeSenseError) else ErrorCode.UNEXPECTED_ADAPTER_ERROR,
                    "watcher", adapter=name)
                return None
        return running

    def _waiting_locations(self) -> list[LocationRecord]:
        return [r for r in self.journal.locations() if r.unresolved is ErrorCode.BROWSER_RUNNING]

    def _failed_apply_txs(self) -> dict[TxId, set[tuple[str, str]]]:
        """apply txs in REVERT_FAILED -> the (adapter, target_id) of their failed items."""
        out: dict[TxId, set[tuple[str, str]]] = {}
        for tx in self.journal.unfinished():
            if _kind(self.journal, tx) != "apply" or self.journal.state(tx) is not TxState.REVERT_FAILED:
                continue
            out[tx] = {(i.snapshot.target.adapter, i.snapshot.target.target_id)
                       for i in self.journal.items(tx) if i.revert_outcome == "failed"}
        return out

    # ------------------------------------------------------------- one round

    def run_once(self) -> WatchReport | None:
        """One round. None: nothing was ready, or the lock is busy (nothing was opened or written)."""
        waiting = self._waiting_locations()
        failed_txs = self._failed_apply_txs()
        if not waiting and not failed_txs:
            self._seen_running.clear()
            self._first = False
            return None
        names = [r.target.adapter for r in waiting] + [a for pairs in failed_txs.values() for a, _ in pairs]
        running = self._running(names)
        if running is None:
            return None
        self._tried -= {r.location for r in waiting if (r.target.adapter, r.target.target_id) in running}
        ready_locations = [r for r in waiting
                           if (r.target.adapter, r.target.target_id) not in running and r.location not in self._tried]
        ready_txs = [tx for tx, pairs in failed_txs.items()
                     if pairs and not (pairs & running) and (self._first or pairs & self._seen_running)]
        self._first = False
        self._seen_running = (self._seen_running | running) - {p for tx in ready_txs for p in failed_txs[tx]}
        if not ready_locations and not ready_txs:
            return None
        return self._finish(ready_locations, ready_txs)

    def _finish(self, locations: list[LocationRecord], txs: list[TxId]) -> WatchReport | None:
        from scribesense.safety.recovery import _restore_baseline  # lazy: recovery imports ipc / lock

        try:
            own = self.journal.begin("revert", None, None)
        except ScribeSenseError:  # APPLY_IN_PROGRESS / LOCK_TIMEOUT: someone else is working; try next round
            return None
        report = WatchReport()
        try:
            self.journal.set_state(own, TxState.REVERTING)
            reverter = Reverter(self.journal, self.adapters)
            for tx in txs:  # step 1 first, as in reset: an undone apply's write would otherwise look like drift
                if self.journal.state(tx) is not TxState.REVERT_FAILED:
                    continue
                if reverter.finish_tx(tx).ok:
                    report.finished_txs.append(tx)
                else:
                    report.failed.append(f"tx:{tx}")
            owed = self._owed_by_failed_applies()
            active = self.journal.active()
            for stale in locations:
                record = self.journal.location(stale.location)  # re-read under the lock
                if record is None or record.unresolved is not ErrorCode.BROWSER_RUNNING:
                    continue
                if record.location in owed:
                    continue  # an unfinished apply still has to undo its own write there first (step 1)
                if active is not None and record.managed_font_key == active.font_key:
                    continue  # re-managed by the confirmed style: never undo a newer Keep
                one = RevertReport()
                _restore_baseline(self.journal, self.adapters, record, one)
                if record.managed_sha256 is None and self._code(record.location) is ErrorCode.BROWSER_RUNNING:
                    self.journal.set_unresolved(record.location, None)  # nothing of ours is left there
                self._sort(record.location, one, report)
            self._close_old_resets(own, report)
            ok = not report.failed
            self.journal.set_state(own, TxState.REVERTED if ok else TxState.REVERT_FAILED)
        except Exception as exc:  # noqa: BLE001 — never leave our own tx open
            log(exc.code if isinstance(exc, ScribeSenseError) else ErrorCode.UNEXPECTED_ADAPTER_ERROR, "watcher")
            if self.journal.state(own) is TxState.REVERTING:
                self.journal.set_state(own, TxState.REVERT_FAILED)
            return None
        return report

    def _owed_by_failed_applies(self) -> set[str]:
        return {i.snapshot.location for tx in self.journal.unfinished() if _kind(self.journal, tx) == "apply"
                for i in self.journal.items(tx) if i.revert_outcome == "failed"}

    def _code(self, location: str) -> ErrorCode | None:
        record = self.journal.location(location)
        return record.unresolved if record is not None else None

    def _sort(self, location: str, one: RevertReport, report: WatchReport) -> None:
        code = self._code(location)
        if location in one.restored or code is None:
            report.restored.append(location)
        elif code is ErrorCode.BROWSER_RUNNING:
            report.still_waiting.append(location)  # refused again: wait until the browser is seen open and closed
            self._tried.add(location)
        elif code is not ErrorCode.DRIFT_SKIPPED:
            report.failed.append(location)

    def _close_old_resets(self, own: TxId, report: WatchReport) -> None:
        if any(r.unresolved is not None and r.unresolved is not ErrorCode.DRIFT_SKIPPED
               for r in self.journal.locations()):
            return
        reverter = Reverter(self.journal, self.adapters)
        for tx in self.journal.unfinished():
            if tx != own and _kind(self.journal, tx) == "revert" and self.journal.state(tx) is TxState.REVERT_FAILED:
                reverter.finish_tx(tx)  # a reset / recovery tx has no items: this only closes it
                report.finished_txs.append(tx)


def finish_when_closed(journal: Journal, adapters: Mapping[str, Adapter]) -> WatchReport | None:
    """Stateless single round (CLI / tests). The service keeps one `Watcher` so it remembers running targets."""
    return Watcher(journal, adapters).run_once()


def _kind(journal: Journal, tx: TxId) -> str:
    """`tx_kind` is on both O4 journals but not in K4; a journal without it is treated as unknown (never retried)."""
    getter = getattr(journal, "tx_kind", None)
    return getter(tx) if getter is not None else "unknown"
