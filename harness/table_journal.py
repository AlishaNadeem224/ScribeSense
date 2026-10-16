"""O3 — test-support: the K4 `Journal` names mapped onto `store.JournalTables` (no lock, no IPC).

This is what O4's SQLite journal (M4.10) does for persistence; tests use it to run the same scenarios on the
in-memory double and on the SQL layer (tests/unit/test_store_journal_tables.py, test_retention.py).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from scribesense.contracts.journal import NoRecoveryObligations
from scribesense.store import JournalTables, Store


class TableJournal:
    """Test-only: the K4 names mapped onto JournalTables — what O4's SQLite journal will do (minus lock/IPC)."""

    def __init__(self, store: Store, clock: Callable[[], datetime]) -> None:
        self.t = JournalTables(store, clock)
        self.store = store

    def begin(self, kind, preset_id, reverts=None): return self.t.create_tx(kind, preset_id, reverts)  # type: ignore[no-untyped-def]
    def set_state(self, tx, state): self.t.set_tx_state(tx, state)  # type: ignore[no-untyped-def]
    def record_snapshot(self, tx, item, data): return self.t.add_item(tx, item, data)  # type: ignore[no-untyped-def]
    def record_intent(self, tx, i, p): self.t.set_intent(tx, i, p)  # type: ignore[no-untyped-def]
    def record_applied(self, tx, i, a): self.t.set_applied(tx, i, a)  # type: ignore[no-untyped-def]
    def record_revert(self, tx, i, o): self.t.set_revert_outcome(tx, i, o)  # type: ignore[no-untyped-def]
    def record_coverage(self, tx, e): self.t.add_coverage(tx, e)  # type: ignore[no-untyped-def]
    def mark_retained(self, tx, i): self.t.mark_retained(tx, i)  # type: ignore[no-untyped-def]
    def state(self, tx): return self.t.tx_state(tx)  # type: ignore[no-untyped-def]
    def unfinished(self): return self.t.unfinished()  # type: ignore[no-untyped-def]
    def pending_confirmation(self): return self.t.pending_confirmation()  # type: ignore[no-untyped-def]
    def items(self, tx): return self.t.items(tx)  # type: ignore[no-untyped-def]
    def backup(self, tx, i): return self.t.item_backup(tx, i)  # type: ignore[no-untyped-def]
    def baseline(self, loc): return self.t.baseline(loc)  # type: ignore[no-untyped-def]
    def baseline_backup(self, loc): return self.t.baseline_backup(loc)  # type: ignore[no-untyped-def]
    def location(self, loc): return self.t.location(loc)  # type: ignore[no-untyped-def]
    def locations(self): return self.t.locations()  # type: ignore[no-untyped-def]
    def keep(self, tx, active): return self.t.keep(tx, active)  # type: ignore[no-untyped-def]
    def clear_managed(self, loc): self.t.clear_managed(loc)  # type: ignore[no-untyped-def]
    def set_unresolved(self, loc, code): self.t.set_unresolved(loc, code)  # type: ignore[no-untyped-def]
    def active(self): return self.t.active()  # type: ignore[no-untyped-def]
    def set_original(self): return self.t.set_original()  # type: ignore[no-untyped-def]
    def prune_history(self, before, keep_last): return self.t.prune_history(before, keep_last)  # type: ignore[no-untyped-def]

    def no_obligations(self):  # type: ignore[no-untyped-def]
        return None if self.t.has_obligations() else NoRecoveryObligations()
