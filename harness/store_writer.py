"""O3 — one concurrent writer for the K9 two-process test. `python -m harness.store_writer <db> <count> <tag>`.

Opens the store, then commits <count> transactions (one journal row each), retrying a bounded number of times
on STORE_BUSY. Always exits 0 and prints ONE JSON line, because run_cmd drops the output of a failing command:
    {"committed": N, "busy": retries, "errors": [codes]}
Runs only inside the bubblewrap runner (tests/integration/test_store_concurrency.py).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.store import JournalTables, Store

RETRIES = 5


def _retry(action, report: dict) -> bool:  # type: ignore[no-untyped-def]
    for _ in range(RETRIES):
        try:
            action()
            return True
        except ScribeSenseError as exc:
            if exc.code is not ErrorCode.STORE_BUSY:
                report["errors"].append(exc.code.value)
                return False
            report["busy"] += 1
    report["errors"].append(ErrorCode.STORE_BUSY.value)
    return False


def main(argv: list[str]) -> int:
    path, count, tag = Path(argv[0]), int(argv[1]), argv[2]
    report: dict = {"committed": 0, "busy": 0, "errors": []}
    holder: dict = {}

    def open_store() -> None:
        holder["store"] = Store(path)

    if _retry(open_store, report):
        store = holder["store"]
        tables = JournalTables(store)
        for _ in range(count):
            def write() -> None:
                with store.transaction():
                    tables.create_tx("apply", tag)

            if _retry(write, report):
                report["committed"] += 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
