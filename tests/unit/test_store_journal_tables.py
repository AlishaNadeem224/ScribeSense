"""M3.1 — journal tables (decision D5): parity with O4's in-memory K4 journal, keep() atomicity (F4), retention
(K9 test 10, D4/F2), the permanent active_config row (F3), obligations (test 11), location shapes (D7)."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scribesense.adapters.conformance import JSON_KEY_LOCATION, KEY_LOCATION
from scribesense.contracts.adapter import AppliedItem, PlannedWrite, SnapshotItem, Target
from scribesense.contracts.config import Configuration
from scribesense.contracts.coverage import CoverageEntry, CoverageStatus
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import ActiveConfig, NoRecoveryObligations, TxId
from scribesense.contracts.states import TxState
from scribesense.safety.memory_journal import InMemoryJournal
from harness.table_journal import TableJournal
from scribesense.store import IllegalTransition, JournalTables, Store
from scribesense.store import journal_tables as jt

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
CFG = Configuration("Noto Sans", 0.12, 1.75, 1.5, 1.2, 1.0)
TARGET = Target("fontconfig", "fontconfig", "fontconfig", False)
KEY_T = Target("gtk", "gtk:desktop", "GTK apps", False)
FILE = "/home/u/.config/fontconfig/conf.d/scribesense.conf"
KEY = "org.gnome.desktop.interface font-name"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> Store:
    s = Store(tmp_path / "db" / "s.db")
    s.clock = clock
    return s


@pytest.fixture
def table(store: Store, clock: Clock) -> TableJournal:
    return TableJournal(store, clock)


@pytest.fixture(params=["memory", "sqlite"])
def journal(request, store: Store, clock: Clock) -> Any:  # type: ignore[no-untyped-def]
    return InMemoryJournal(clock) if request.param == "memory" else TableJournal(store, clock)


def file_item(location: str = FILE, existed: bool = True, data: bytes = b"old") -> tuple[SnapshotItem, bytes | None]:
    return (SnapshotItem(TARGET, "file", location, existed, None, sha(data) if existed else sha(b"absent"),
                         0o644 if existed else None, None), data if existed else None)


def key_item(value: str | None = "'Sans 11'") -> SnapshotItem:
    return SnapshotItem(KEY_T, "key", KEY, value is not None, value, sha((value or "").encode()), None, "s")


def apply_one(j: Any, *, state_path: bool = True, data: bytes = b"old", new: bytes = b"new") -> tuple[TxId, Any]:
    tx = j.begin("apply", "default")
    snap, backup = file_item(data=data)
    item = j.record_snapshot(tx, snap, backup)
    j.set_state(tx, TxState.SNAPSHOTTED)
    j.record_intent(tx, item, PlannedWrite(TARGET, FILE, new, sha(new)))
    j.set_state(tx, TxState.APPLYING)
    j.record_applied(tx, item, AppliedItem(TARGET, FILE, sha(new)))
    j.mark_retained(tx, item)
    j.set_state(tx, TxState.VERIFYING)
    j.set_state(tx, TxState.AWAITING_CONFIRMATION)
    return tx, item


def active(font_key: str = "k1") -> ActiveConfig:
    return ActiveConfig(CFG, font_key, "default", 0)


# ---------------------------------------------------------------- parity: both journals behave the same

def test_apply_keep_flow_gives_the_same_observable_state(journal: Any) -> None:
    tx, item = apply_one(journal)
    assert journal.pending_confirmation() == tx and journal.unfinished() == [tx]
    revision = journal.keep(tx, active())
    assert revision == 1 and journal.state(tx) is TxState.COMPLETED
    assert journal.unfinished() == [] and journal.pending_confirmation() is None
    [got] = journal.items(tx)
    assert got.snapshot.old_sha256 == sha(b"old") and got.retained and got.applied.new_sha256 == sha(b"new")  # type: ignore[union-attr]
    assert got.intent.payload == b"new"  # type: ignore[union-attr]  # D1
    loc = journal.location(FILE)
    assert (loc.managed_sha256, loc.managed_font_key, loc.unresolved) == (sha(b"new"), "k1", None)
    assert loc.baseline == journal.baseline(FILE) and journal.baseline_backup(FILE) == b"old"
    assert journal.backup(tx, item) == b"old"
    a = journal.active()
    assert (a.font_key, a.source_preset_id, a.style_revision, a.config) == ("k1", "default", 1, CFG)


def test_second_apply_keeps_the_first_baseline(journal: Any) -> None:
    tx1, _ = apply_one(journal, data=b"original", new=b"A")
    journal.keep(tx1, active("kA"))
    tx2, _ = apply_one(journal, data=b"A", new=b"B")
    journal.keep(tx2, active("kB"))
    assert journal.baseline_backup(FILE) == b"original"  # baseline = the FIRST snapshot ever (R2-1)
    assert journal.baseline(FILE).old_sha256 == sha(b"original")  # type: ignore[union-attr]
    assert journal.location(FILE).managed_sha256 == sha(b"B")  # type: ignore[union-attr]
    assert journal.active().style_revision == 2  # type: ignore[union-attr]


def test_keep_only_marks_retained_items_managed(journal: Any) -> None:
    tx = journal.begin("apply", "default")
    a = journal.record_snapshot(tx, key_item(), None)
    b, data = file_item()
    f = journal.record_snapshot(tx, b, data)
    for i, loc, t in ((a, KEY, KEY_T), (f, FILE, TARGET)):
        journal.record_intent(tx, i, PlannedWrite(t, loc, "x" if loc == KEY else b"y", sha(b"z")))
        journal.record_applied(tx, i, AppliedItem(t, loc, sha(b"z")))
    journal.mark_retained(tx, f)  # only the file was verified and kept
    for s in (TxState.SNAPSHOTTED, TxState.APPLYING, TxState.VERIFYING, TxState.AWAITING_CONFIRMATION):
        journal.set_state(tx, s)
    journal.keep(tx, active())
    assert journal.location(KEY).managed_sha256 is None
    assert journal.location(FILE).managed_sha256 == sha(b"z")


def test_keep_needs_a_pending_confirmation(journal: Any) -> None:
    tx = journal.begin("apply", "default")
    with pytest.raises(ScribeSenseError) as err:
        journal.keep(tx, active())
    assert err.value.code is ErrorCode.TX_NOT_PENDING


def test_k5_transitions_are_enforced(journal: Any) -> None:
    tx = journal.begin("apply", "default")
    with pytest.raises(ValueError):
        journal.set_state(tx, TxState.COMPLETED)  # only keep() may complete
    with pytest.raises(ValueError):
        journal.set_state(tx, TxState.VERIFYING)  # CREATED -> VERIFYING is not allowed
    journal.set_state(tx, TxState.FAILED_CLEAN)
    with pytest.raises(ValueError):
        journal.set_state(tx, TxState.SNAPSHOTTED)  # terminal


def test_intent_applied_and_retained_rules(journal: Any) -> None:
    tx = journal.begin("apply", "default")
    snap, data = file_item()
    item = journal.record_snapshot(tx, snap, data)
    with pytest.raises(ValueError):
        journal.record_applied(tx, item, AppliedItem(TARGET, FILE, "x"))  # before the intent (K4 rule 2)
    with pytest.raises(ValueError):
        journal.mark_retained(tx, item)  # not applied
    with pytest.raises(ValueError):
        journal.record_intent(tx, item, PlannedWrite(TARGET, "/other", b"x", "h"))  # wrong location
    with pytest.raises(KeyError):
        journal.items(TxId(999))
    with pytest.raises(KeyError):
        journal.record_intent(TxId(999), item, PlannedWrite(TARGET, FILE, b"x", "h"))


def test_set_original_clears_active_and_counts_the_revision(journal: Any) -> None:
    tx, _ = apply_one(journal)
    journal.keep(tx, active())
    assert journal.set_original() == 2 and journal.active() is None
    tx2, _ = apply_one(journal, data=b"new", new=b"newer")
    assert journal.keep(tx2, active("k2")) == 3  # the counter survived "Original" (F3)


def test_no_obligations_follows_the_journal(journal: Any) -> None:
    assert journal.no_obligations() is not None  # empty
    tx, _ = apply_one(journal)
    assert journal.no_obligations() is None  # unfinished
    journal.keep(tx, active())
    assert journal.no_obligations() is None  # managed
    journal.clear_managed(FILE)
    assert journal.no_obligations() is not None
    journal.set_unresolved(FILE, ErrorCode.DRIFT_SKIPPED)
    assert journal.no_obligations() is None  # unresolved
    journal.set_unresolved(FILE, None)
    assert journal.no_obligations() is not None


def test_revert_outcome_and_coverage_are_recorded(journal: Any) -> None:
    tx, item = apply_one(journal)
    journal.record_revert(tx, item, "skipped_drift")
    assert journal.items(tx)[0].revert_outcome == "skipped_drift"
    journal.record_coverage(tx, CoverageEntry(KEY_T, CoverageStatus.COVERED, "OK"))


# ---------------------------------------------------------------- stored details

def test_a_planned_write_keeps_text_and_bytes_payloads(table: TableJournal) -> None:
    tx = table.begin("apply", "default")
    a = table.record_snapshot(tx, key_item(), None)
    snap, data = file_item()
    b = table.record_snapshot(tx, snap, data)
    table.record_intent(tx, a, PlannedWrite(KEY_T, KEY, "'Foo 11'", "h1"))
    table.record_intent(tx, b, PlannedWrite(TARGET, FILE, b"\x00\xffbytes", "h2"))
    first, second = table.items(tx)
    assert (first.intent.payload, second.intent.payload) == ("'Foo 11'", b"\x00\xffbytes")  # type: ignore[union-attr]
    assert first.snapshot.old_value == "'Sans 11'" and first.snapshot.value_type == "s"
    assert second.snapshot.file_mode == 0o644 and second.intent.target == replace_running(TARGET)  # type: ignore[union-attr]


def replace_running(t: Target) -> Target:
    return Target(t.adapter, t.target_id, t.display_name, False)  # `running` is never stored (D2)


def test_running_is_not_stored_and_reads_back_false(table: TableJournal) -> None:
    tx = table.begin("apply", "default")
    running = Target("gtk", "gtk:desktop", "GTK apps", True)
    table.record_snapshot(tx, SnapshotItem(running, "key", KEY, True, "'x'", "h", None, "s"), None)
    assert table.items(tx)[0].snapshot.target.running is False
    assert table.location(KEY).target.running is False  # type: ignore[union-attr]


def test_backups_are_verified_when_read(table: TableJournal, store: Store) -> None:
    tx, item = apply_one(table)
    store.execute("UPDATE backups SET bytes = ? WHERE id = (SELECT backup_id FROM journal_items WHERE id = ?)",
                  (b"damaged", item))
    with pytest.raises(ScribeSenseError) as err:
        table.backup(tx, item)
    assert err.value.code is ErrorCode.BACKUP_CORRUPT
    with pytest.raises(ScribeSenseError) as err:
        table.baseline_backup(FILE)  # the baseline shares that row
    assert err.value.code is ErrorCode.BACKUP_CORRUPT


def test_an_existing_file_without_a_backup_is_backup_missing(table: TableJournal) -> None:
    tx = table.begin("apply", "default")
    snap, _ = file_item()
    table.record_snapshot(tx, snap, None)  # existed, but no bytes were recorded
    with pytest.raises(ScribeSenseError) as err:
        table.baseline_backup(FILE)
    assert err.value.code is ErrorCode.BACKUP_MISSING
    assert table.baseline_backup("/nowhere") is None


def test_absent_and_empty_files_are_distinct(table: TableJournal) -> None:
    tx = table.begin("apply", "default")
    table.record_snapshot(tx, *file_item("/a", existed=False))
    table.record_snapshot(tx, *file_item("/b", existed=True, data=b""))
    assert table.baseline_backup("/a") is None
    assert table.baseline_backup("/b") == b""  # present but empty


def test_coverage_for_a_tx_and_reverts_are_stored(table: TableJournal) -> None:
    first = table.begin("apply", "default")
    second = table.begin("revert", None, first)
    third = table.begin("uninstall", None, "all")
    assert table.t.tx_info(second)["reverts"] == first and table.t.tx_info(third)["reverts"] == "all"
    assert table.t.tx_info(first)["reverts"] is None and table.t.tx_kind(third) == "uninstall"
    table.record_coverage(first, CoverageEntry(KEY_T, CoverageStatus.NOT_COVERED, ErrorCode.ADAPTER_TIMEOUT))
    table.record_coverage(first, CoverageEntry(KEY_T, CoverageStatus.COVERED, "OK"))  # the latest wins
    [entry] = table.t.coverage(first)
    assert (entry.status, entry.detail_code) == (CoverageStatus.COVERED, "OK")
    table.record_coverage(first, CoverageEntry(KEY_T, CoverageStatus.NOT_COVERED, ErrorCode.ADAPTER_TIMEOUT))
    assert table.t.coverage(first)[0].detail_code is ErrorCode.ADAPTER_TIMEOUT


def test_bad_input_is_refused(table: TableJournal) -> None:
    with pytest.raises(ValueError):
        table.begin("explode", None)
    with pytest.raises(ValueError):
        table.begin("revert", None, "some")
    tx = table.begin("apply", None)
    with pytest.raises(ValueError):
        table.record_revert(tx, table.record_snapshot(tx, key_item(), None), "maybe")  # type: ignore[arg-type]


# ---------------------------------------------------------------- keep() is one atomic step (F4)

def test_a_failure_inside_keep_changes_nothing(table: TableJournal, store: Store) -> None:
    tx, _ = apply_one(table)
    before = (table.location(FILE), table.active(), table.t.style_revision(), table.state(tx))
    store.execute("CREATE TRIGGER boom BEFORE UPDATE ON transactions WHEN NEW.state = 'COMPLETED' "
                  "BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(Exception):
        table.keep(tx, active())  # fails at the very last statement
    assert (table.location(FILE), table.active(), table.t.style_revision(), table.state(tx)) == before
    assert table.location(FILE).managed_sha256 is None  # type: ignore[union-attr]
    store.execute("DROP TRIGGER boom")
    assert table.keep(tx, active()) == 1  # and it still works afterwards


def test_an_unstorable_configuration_fails_before_anything_is_touched(table: TableJournal) -> None:
    tx, _ = apply_one(table)
    bad = ActiveConfig(Configuration("Noto Sans", float("nan"), 1.75, 1.5, 1.2, 1.0), "k", None, 0)
    with pytest.raises(ValueError):
        table.keep(tx, bad)
    assert table.state(tx) is TxState.AWAITING_CONFIRMATION and table.active() is None


def test_a_committed_keep_survives_closing_and_reopening(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "db" / "s.db"
    s = Store(path)
    s.clock = clock
    tx, _ = apply_one(TableJournal(s, clock))
    TableJournal(s, clock).keep(tx, active())
    s.close()
    j = TableJournal(Store(path, mode="recovery"), clock)
    assert j.state(tx) is TxState.COMPLETED and j.active().font_key == "k1"  # type: ignore[union-attr]


# ---------------------------------------------------------------- retention (K9 test 10, D4, F2)

def finished_tx(table: TableJournal, location: str, kind: str = "apply", end: TxState = TxState.REVERTED) -> TxId:
    tx = table.begin(kind, None)  # type: ignore[arg-type]
    table.record_snapshot(tx, *file_item(location))
    table.set_state(tx, TxState.FAILED_CLEAN if end is TxState.FAILED_CLEAN else TxState.REVERTING)
    if end is not TxState.FAILED_CLEAN:
        table.set_state(tx, end)
    return tx


def test_prune_deletes_old_finished_transactions_outside_the_newest_n(table: TableJournal, store: Store) -> None:
    old = [finished_tx(table, f"/f{i}") for i in range(5)]
    cutoff = table.t.tx_info(old[-1])["created_at"] + timedelta(seconds=1)  # everything so far is "old"
    recent = finished_tx(table, "/recent")
    deleted = table.prune_history(cutoff, keep_last=2)
    assert deleted == 4  # 6 finished; the newest 2 overall are old[4] and `recent`; the other 4 are older than the cutoff
    survivors = {r[0] for r in store.fetchall("SELECT id FROM transactions")}
    assert survivors == {old[4], recent}
    assert store.fetchone("SELECT COUNT(*) FROM journal_items WHERE tx_id IN (?,?,?,?)", tuple(old[:4]))[0] == 0  # cascade


def test_prune_never_touches_location_records_their_backups_or_the_active_config(table: TableJournal, store: Store) -> None:
    tx, _ = apply_one(table)
    table.keep(tx, active())
    for i in range(3):
        finished_tx(table, f"/g{i}")
    table.prune_history(T0 + timedelta(days=3650), keep_last=0)
    assert table.baseline_backup(FILE) == b"old"  # the baseline survives even though its tx may be gone
    assert table.location(FILE).managed_sha256 == sha(b"new")  # type: ignore[union-attr]
    assert table.active().font_key == "k1"  # type: ignore[union-attr]
    assert store.fetchone("SELECT COUNT(*) FROM location_records")[0] == 4


def test_prune_keeps_unfinished_transactions(table: TableJournal) -> None:
    unfinished = table.begin("apply", None)
    table.record_snapshot(unfinished, *file_item("/u"))
    assert table.prune_history(T0 + timedelta(days=3650), keep_last=0) == 0
    assert table.unfinished() == [unfinished]


def test_prune_always_keeps_the_newest_completed_apply(table: TableJournal, store: Store) -> None:  # finding F2
    tx, _ = apply_one(table)
    table.keep(tx, active())
    table.t.add_coverage(tx, CoverageEntry(KEY_T, CoverageStatus.COVERED, "OK"))
    for i in range(12):  # more later transactions than keep_last: it falls out of "the newest N"
        finished_tx(table, f"/r{i}", kind="revert")
    assert table.prune_history(T0 + timedelta(days=3650), keep_last=3) >= 9
    assert table.state(tx) is TxState.COMPLETED  # still there
    assert [e.status for e in store.coverage.last_completed_apply()] == [CoverageStatus.COVERED]  # report intact


def test_prune_keeps_transactions_an_unfinished_revert_points_at(table: TableJournal) -> None:
    target = finished_tx(table, "/t")
    pending = table.begin("revert", None, target)
    assert table.prune_history(T0 + timedelta(days=3650), keep_last=0) == 0  # referenced by an unfinished revert
    assert table.state(target) is TxState.REVERTED
    table.set_state(pending, TxState.REVERTING)
    table.set_state(pending, TxState.REVERTED)
    assert table.prune_history(T0 + timedelta(days=3650), keep_last=0) == 2  # now both can go


def test_prune_keeps_transactions_touching_an_unresolved_location(table: TableJournal) -> None:
    tx = finished_tx(table, "/stuck")
    table.set_unresolved("/stuck", ErrorCode.REVERT_ITEM_FAILED)
    other = finished_tx(table, "/fine")
    assert table.prune_history(T0 + timedelta(days=3650), keep_last=0) == 1
    assert table.state(tx) is TxState.REVERTED
    with pytest.raises(KeyError):
        table.state(other)


def test_prune_deletes_a_backup_only_when_nothing_references_it(table: TableJournal, store: Store) -> None:
    first = finished_tx(table, "/same")  # becomes the baseline of /same
    second = table.begin("apply", None)
    table.record_snapshot(second, *file_item("/same", data=b"later"))  # a second backup for the same location
    table.set_state(second, TxState.FAILED_CLEAN)
    assert store.fetchone("SELECT COUNT(*) FROM backups")[0] == 2
    table.prune_history(T0 + timedelta(days=3650), keep_last=0)
    assert store.fetchone("SELECT COUNT(*) FROM backups")[0] == 1  # only the baseline's row is left
    assert table.baseline_backup("/same") == b"old"
    assert first and second


def test_prune_argument_and_order_rules(table: TableJournal) -> None:
    with pytest.raises(ValueError):
        table.prune_history(T0, -1)
    with pytest.raises(ValueError):
        table.prune_history(datetime(2026, 1, 1), 1)  # naive datetime
    assert table.prune_history(T0, 5) == 0  # nothing to do


def test_prune_matches_the_in_memory_journal(store: Store, clock: Clock) -> None:
    memory, sql = InMemoryJournal(clock), TableJournal(store, clock)

    def run(j: Any) -> list[bool]:
        txs = [finished_tx(j, f"/p{i}") if isinstance(j, TableJournal) else _mem_finished(j, f"/p{i}") for i in range(6)]
        j.prune_history(T0 + timedelta(days=3650), 2)
        out = []
        for t in txs:
            try:
                j.state(t)
                out.append(True)
            except KeyError:
                out.append(False)
        return out

    clock.now = T0
    a = run(memory)
    clock.now = T0
    b = run(sql)
    assert a == b == [False, False, False, False, True, True]


def _mem_finished(j: InMemoryJournal, location: str) -> TxId:
    tx = j.begin("apply", None)
    j.record_snapshot(tx, *file_item(location))
    j.set_state(tx, TxState.REVERTING)
    j.set_state(tx, TxState.REVERTED)
    return tx


# ---------------------------------------------------------------- stored locations (D7)

@pytest.mark.parametrize("location", [
    "org.gnome.desktop.interface font-name",
    "org.gnome.desktop.interface text-scaling-factor",
    "/home/u/.config/BraveSoftware/Brave-Browser/Default/Preferences#webkit.webprefs.fonts.standard.Zyyy",
    "/home/u/.config/fontconfig/conf.d/99-scribesense.conf",
    "/home/u/.var/app/app.zen_browser.zen/.mozilla/firefox/p/chrome/userContent.css",
])
def test_managed_location_shapes_are_accepted(location: str) -> None:
    assert jt.is_managed_location_shape(location)


@pytest.mark.parametrize("location", ["", "relative/file.txt", "two words extra", "/bad\x00path", "/line\nbreak", "x" * 5000])
def test_other_shapes_are_not(location: str) -> None:
    assert not jt.is_managed_location_shape(location)


def test_the_shapes_agree_with_o4s_conformance_patterns() -> None:
    for pattern, sample in ((KEY_LOCATION, "org.gnome.desktop.interface font-name"),
                            (JSON_KEY_LOCATION, "/p/Preferences#a.b-c.d")):
        assert pattern.match(sample) and jt.is_managed_location_shape(sample)
    assert jt._KEY_LOCATION.pattern == KEY_LOCATION.pattern
    assert jt._JSON_KEY_LOCATION.pattern == JSON_KEY_LOCATION.pattern
    assert not re.search(r"user|document", jt._FILE_LOCATION.pattern)
