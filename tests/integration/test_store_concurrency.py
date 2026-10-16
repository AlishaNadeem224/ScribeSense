"""M3.1 / K9 test 7 — several processes write the same store at once: each one either waits and succeeds or gets
STORE_BUSY (and retries a bounded number of times); nothing is corrupted. Runs only in the bubblewrap runner.

The processes are real (started through run_cmd, one per thread), so this is genuine multi-process contention on one
WAL file in the sandbox's tmpfs — an in-memory database or a thread test would prove nothing.
"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from scribesense.contracts.paths import run_cmd
from scribesense.store import Store

pytestmark = pytest.mark.integration

WRITERS = 3
PER_WRITER = 40


def test_concurrent_processes_never_corrupt_the_store(sandbox) -> None:  # type: ignore[no-untyped-def]
    db = sandbox.data_dir / "scribesense.db"

    def writer(tag: str) -> dict:
        done = run_cmd(["/usr/bin/python3", "-m", "harness.store_writer", str(db), str(PER_WRITER), tag], 120.0)
        return json.loads(done.stdout.decode().strip().splitlines()[-1])

    with ThreadPoolExecutor(WRITERS) as pool:
        reports = list(pool.map(writer, [f"w{i}" for i in range(WRITERS)]))

    assert [r["errors"] for r in reports] == [[] for _ in reports], reports  # nobody gave up
    assert [r["committed"] for r in reports] == [PER_WRITER] * WRITERS
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == WRITERS * PER_WRITER
    per_tag = dict(conn.execute("SELECT preset_id, COUNT(*) FROM transactions GROUP BY preset_id").fetchall())
    assert per_tag == {f"w{i}": PER_WRITER for i in range(WRITERS)}  # every commit landed, none twice
    assert Store(db).fetchone("PRAGMA user_version")[0] == 1  # opened concurrently, migrated exactly once
