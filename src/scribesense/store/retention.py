"""O3 — M3.2 Retention: prune old TRANSACTION HISTORY at app start-up (K9 pruning rule, decision D4).

    prune(journal, now) -> int      how many transactions were deleted

Policy: a finished transaction is deleted only when it is older than RETENTION_DAYS **and** outside the newest
KEEP_LAST finished ones (so a 40-day-old completed tx goes unless it is among the last 10). The storage rules that
make it safe live in `JournalTables.prune_history` and are tested there: LocationRecords, baseline backups and the
active config are never touched; unfinished transactions, the newest COMPLETED apply, and anything referenced by an
unfinished revert or an unresolved location are kept; a backup goes only when nothing references it.

Preconditions (card): no transaction is active. If any is unfinished — or the journal says the apply lock is held —
nothing is pruned. Failure rule (card): any storage error -> skip pruning and keep every row. Pruning is housekeeping:
it must never stop the app from starting, so storage errors are logged (error code only) and swallowed.

`journal` is any K4 journal: O4's SQLite journal (which holds the lock) or the in-memory double.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Protocol

from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import TxId
from scribesense.log import log
from scribesense.store.core import map_sqlite_error

RETENTION_DAYS = 30
KEEP_LAST = 10


class _Prunable(Protocol):
    def unfinished(self) -> list[TxId]: ...

    def prune_history(self, before: datetime, keep_last: int) -> int: ...


def prune(journal: _Prunable, now: datetime) -> int:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (UTC)")
    try:
        if journal.unfinished():
            log(ErrorCode.APPLY_IN_PROGRESS, "retention", step="skip")  # precondition: no transaction is active
            return 0
        deleted = journal.prune_history(now - timedelta(days=RETENTION_DAYS), KEEP_LAST)
    except ScribeSenseError as exc:
        log(exc.code, "retention", step="prune")
        return 0
    except sqlite3.Error as exc:  # e.g. a constraint failure: the delete was rolled back as one transaction
        log(map_sqlite_error(exc).code, "retention", step="prune")
        return 0
    if deleted:
        log("OK", "retention", step="prune", deleted=deleted)
    return deleted
