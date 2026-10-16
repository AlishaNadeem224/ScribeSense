"""M3.2 Retention — acceptance: the 30-day / last-10 rule, baselines survive, no pruning while a tx is active,
storage errors skip pruning and keep every row. Every policy test runs on BOTH K4 journals (in-memory and SQL)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from harness.table_journal import TableJournal
from scribesense.contracts.adapter import AppliedItem, PlannedWrite, SnapshotItem, Target
from scribesense.contracts.config import Configuration
from scribesense.contracts.coverage import CoverageEntry, CoverageStatus
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import ActiveConfig, TxId
from scribesense.contracts.states import TxState
from scribesense.log import log_path
from scribesense.safety.memory_journal import InMemoryJournal
from scribesense.store import KEEP_LAST, RETENTION_DAYS, Store, prune

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
TARGET = Target("fontconfig", "fontconfig", "fontconfig", False)
FILE = "/home/u/.config/fontconfig/conf.d/scribesense.conf"
CFG = Configuration("Noto Sans", 0.12, 1.75, 1.5, 1.2, 1.0)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class SetClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> SetClock:
    return SetClock()


@pytest.fixture
def store(tmp_path: Path, clock: SetClock) -> Store:
    s = Store(tmp_path / "db" / "s.db")
    s.clock = clock
    return s


@pytest.fixture(params=["memory", "sqlite"])
def journal(request, store: Store, clock: SetClock) -> Any:  # type: ignore[no-untyped-def]
    return InMemoryJournal(clock) if request.param == "memory" else TableJournal(store, clock)


def snapshot(location: str, data: bytes = b"old") -> tuple[SnapshotItem, bytes]:
    return SnapshotItem(TARGET, "file", location, True, None, sha(data), 0o644, None), data


def finished(j: Any, clock: SetClock, when: datetime, location: str, kind: str = "revert") -> TxId:
    clock.now = when
    tx = j.begin(kind, None)
    j.record_snapshot(tx, *snapshot(location))
    j.set_state(tx, TxState.REVERTING)
    j.set_state(tx, TxState.REVERTED)
    return tx


def alive(j: Any, txs: list[TxId]) -> list[bool]:
    out = []
    for t in txs:
        try:
            j.state(t)
            out.append(True)
        except KeyError:
            out.append(False)
    return out


def old(days: float, minute: int = 0) -> datetime:
    return NOW - timedelta(days=days) + timedelta(minutes=minute)


def apply_and_keep(j: Any, clock: SetClock, when: datetime) -> TxId:
    clock.now = when
    tx = j.begin("apply", "default")
    item = j.record_snapshot(tx, *snapshot(FILE, b"original"))
    j.set_state(tx, TxState.SNAPSHOTTED)
    j.record_intent(tx, item, PlannedWrite(TARGET, FILE, b"new", sha(b"new")))
    j.set_state(tx, TxState.APPLYING)
    j.record_applied(tx, item, AppliedItem(TARGET, FILE, sha(b"new")))
    j.mark_retained(tx, item)
    j.set_state(tx, TxState.VERIFYING)
    j.set_state(tx, TxState.AWAITING_CONFIRMATION)
    j.record_coverage(tx, CoverageEntry(Target("gtk", "gtk:desktop", "GTK apps", False), CoverageStatus.COVERED, "OK"))
    j.keep(tx, ActiveConfig(CFG, "k1", "default", 0))
    return tx


def test_the_rule_is_30_days_and_the_last_10() -> None:
    assert (RETENTION_DAYS, KEEP_LAST) == (30, 10)


# ---------------------------------------------------------------- the card's example and the 30-day / last-10 rule

def test_40_day_old_transactions_are_pruned_unless_they_are_in_the_last_10(journal: Any, clock: SetClock) -> None:
    txs = [finished(journal, clock, old(40, i), f"/f{i}") for i in range(15)]
    assert prune(journal, NOW) == 5
    assert alive(journal, txs) == [False] * 5 + [True] * 10  # the newest 10 survive although they are 40 days old


def test_recent_transactions_are_never_pruned_however_many(journal: Any, clock: SetClock) -> None:
    txs = [finished(journal, clock, old(5, i), f"/f{i}") for i in range(15)]
    assert prune(journal, NOW) == 0
    assert all(alive(journal, txs))


def test_old_and_recent_mix(journal: Any, clock: SetClock) -> None:
    olds = [finished(journal, clock, old(40, i), f"/o{i}") for i in range(12)]
    news = [finished(journal, clock, old(2, i), f"/n{i}") for i in range(3)]
    assert prune(journal, NOW) == 5  # 15 finished: the newest 10 = 3 recent + the 7 newest old ones
    assert alive(journal, olds) == [False] * 5 + [True] * 7 and all(alive(journal, news))


def test_ten_or_fewer_old_transactions_are_all_kept(journal: Any, clock: SetClock) -> None:
    txs = [finished(journal, clock, old(100, i), f"/f{i}") for i in range(KEEP_LAST)]
    assert prune(journal, NOW) == 0 and all(alive(journal, txs))


def test_the_age_limit_is_strictly_older_than_30_days(journal: Any, clock: SetClock) -> None:
    exactly = [finished(journal, clock, NOW - timedelta(days=30), f"/e{i}") for i in range(11)]
    assert prune(journal, NOW) == 0 and all(alive(journal, exactly))  # exactly 30 days old: kept
    justover = finished(journal, clock, NOW - timedelta(days=30, seconds=1), "/j")
    assert prune(journal, NOW) == 1 and alive(journal, [justover]) == [False]


def test_running_it_twice_changes_nothing_the_second_time(journal: Any, clock: SetClock) -> None:
    for i in range(14):
        finished(journal, clock, old(40, i), f"/f{i}")
    assert prune(journal, NOW) == 4
    assert prune(journal, NOW) == 0


# ---------------------------------------------------------------- baselines and the other things that must survive

def test_baselines_and_managed_records_survive_even_when_their_transaction_is_pruned(journal: Any, clock: SetClock) -> None:
    first = apply_and_keep(journal, clock, old(60))
    for i in range(20):
        finished(journal, clock, old(50, i), f"/r{i}")
    assert prune(journal, NOW) >= 10
    loc = journal.location(FILE)
    assert (loc.managed_sha256, loc.managed_font_key) == (sha(b"new"), "k1")
    assert journal.baseline_backup(FILE) == b"original"  # the original bytes are still restorable
    assert journal.baseline(FILE).old_sha256 == sha(b"original")
    assert journal.active().font_key == "k1"
    assert first  # (keeping the newest COMPLETED apply itself is D4/F2 — tested on the SQL layer below)


def test_the_newest_completed_apply_itself_survives(store: Store, clock: SetClock) -> None:  # D4 / finding F2
    j = TableJournal(store, clock)
    first = apply_and_keep(j, clock, old(60))
    for i in range(15):
        finished(j, clock, old(35, i), f"/r{i}")
    prune(j, NOW)
    assert j.state(first) is TxState.COMPLETED


def test_the_newest_completed_apply_keeps_its_coverage_report(store: Store, clock: SetClock) -> None:
    j = TableJournal(store, clock)
    apply_and_keep(j, clock, old(60))
    for i in range(12):
        finished(j, clock, old(35, i), f"/r{i}")
    assert prune(j, NOW) == 2  # 13 finished; the newest 10 stay; the oldest two reverts go; the apply is protected
    assert [e.status for e in store.coverage.last_completed_apply()] == [CoverageStatus.COVERED]


def _same_location_history(j: Any, clock: SetClock) -> None:
    """One location snapshotted 13 times: the first backup is its baseline, the other 12 are plain per-tx backups."""
    finished(j, clock, old(60), "/same")
    for i in range(12):
        finished(j, clock, old(50, i), "/same")


def test_a_backup_is_deleted_with_its_transaction_only_when_nothing_else_needs_it(store: Store, clock: SetClock) -> None:
    j = TableJournal(store, clock)
    _same_location_history(j, clock)
    assert store.fetchone("SELECT COUNT(*) FROM backups")[0] == 13
    assert prune(j, NOW) == 3  # 13 finished: the newest 10 stay; the baseline's transaction and two more go
    assert store.fetchone("SELECT COUNT(*) FROM backups")[0] == 11  # two plain backups removed; the baseline's row stays
    assert j.baseline_backup("/same") == b"old"


def test_a_transaction_that_only_holds_a_baseline_backup_leaves_that_backup_behind(store: Store, clock: SetClock) -> None:
    j = TableJournal(store, clock)
    for i in range(12):
        finished(j, clock, old(40, i), f"/own{i}")  # every backup here is also the baseline of its own location
    assert prune(j, NOW) == 2
    assert store.fetchone("SELECT COUNT(*) FROM backups")[0] == 12  # all 12 baselines still have their bytes
    assert all(j.baseline_backup(f"/own{i}") == b"old" for i in range(12))


# ---------------------------------------------------------------- preconditions and failures

def test_nothing_is_pruned_while_a_transaction_is_active(journal: Any, clock: SetClock) -> None:
    txs = [finished(journal, clock, old(40, i), f"/f{i}") for i in range(15)]
    clock.now = NOW
    active_tx = journal.begin("apply", "default")  # unfinished
    assert prune(journal, NOW) == 0
    assert all(alive(journal, txs)) and active_tx in journal.unfinished()
    assert '"APPLY_IN_PROGRESS"' in log_path().read_text()  # skipped, and the reason is logged (code only)


def test_a_storage_error_skips_pruning_and_is_not_raised(journal: Any, clock: SetClock) -> None:
    txs = [finished(journal, clock, old(40, i), f"/f{i}") for i in range(15)]
    with patch.object(journal, "prune_history", side_effect=ScribeSenseError(ErrorCode.STORE_BUSY)):
        assert prune(journal, NOW) == 0  # start-up must never fail because of housekeeping
    assert all(alive(journal, txs))
    assert '"STORE_BUSY"' in log_path().read_text()


def test_a_failure_in_the_middle_of_pruning_deletes_nothing(store: Store, clock: SetClock) -> None:
    j = TableJournal(store, clock)
    _same_location_history(j, clock)
    ids = [r[0] for r in store.fetchall("SELECT id FROM transactions ORDER BY id")]
    store.execute("CREATE TRIGGER nodel BEFORE DELETE ON backups BEGIN SELECT RAISE(ABORT, 'no'); END")
    assert prune(j, NOW) == 0  # the transaction deletes happened, then the backup delete failed -> all rolled back
    assert [r[0] for r in store.fetchall("SELECT id FROM transactions ORDER BY id")] == ids
    assert store.fetchone("SELECT COUNT(*) FROM journal_items")[0] == 13
    assert store.fetchone("SELECT COUNT(*) FROM backups")[0] == 13
    store.execute("DROP TRIGGER nodel")
    assert prune(j, NOW) == 3  # and the next start-up simply tries again


def test_a_naive_clock_value_is_a_programming_error(journal: Any) -> None:
    with pytest.raises(ValueError):
        prune(journal, datetime(2026, 10, 15))


# ---------------------------------------------------------------- privacy of what is logged

def test_the_log_records_a_code_and_a_count_never_locations(journal: Any, clock: SetClock) -> None:
    for i in range(14):
        finished(journal, clock, old(40, i), f"/secret/place/{i}")
    prune(journal, NOW)
    text = log_path().read_text()
    [event] = [json.loads(line) for line in text.splitlines() if '"retention"' in line]
    assert (event["code"], event["category"], event["deleted"]) == ("OK", "retention", 4)
    assert "secret" not in text and "/" not in event.get("step", "")
