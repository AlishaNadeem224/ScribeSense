"""M3.1 — store core: file creation, pragmas, migrations (K9 tests 8, 9), transactions, error mapping, deletions."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import threading
from pathlib import Path

import pytest

from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import NoRecoveryObligations
from scribesense.store import Store, apply_migrations, default_path, latest_version
from scribesense.store import core as store_core
from scribesense.store.core import StoreConstraintError


def code_of(exc_info) -> ErrorCode:  # type: ignore[no-untyped-def]
    return exc_info.value.code


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "data" / "scribesense.db"


# ---------------------------------------------------------------- creation and pragmas

def test_default_path_uses_the_k8_data_dir(sandbox) -> None:  # type: ignore[no-untyped-def]
    assert default_path() == sandbox.data_dir / "scribesense.db"
    Store(default_path()).close()
    assert default_path().is_file()


def test_file_and_directory_are_private(db: Path) -> None:
    Store(db).close()
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700


def test_every_connection_has_the_k9_pragmas(db: Path) -> None:
    store = Store(db)

    def pragmas() -> tuple[str, int, int, int]:
        c = store.conn()
        return (c.execute("PRAGMA journal_mode").fetchone()[0], c.execute("PRAGMA synchronous").fetchone()[0],
                c.execute("PRAGMA foreign_keys").fetchone()[0], c.execute("PRAGMA busy_timeout").fetchone()[0])

    expected = ("wal", 2, 1, 5000)  # synchronous 2 = FULL
    assert pragmas() == expected
    seen: dict[str, object] = {}
    t = threading.Thread(target=lambda: seen.update(p=pragmas(), c=id(store.conn())))
    t.start()
    t.join()
    assert seen["p"] == expected
    assert seen["c"] != id(store.conn())  # one connection per thread, never shared


# ---------------------------------------------------------------- migrations (K9 test 8)

def test_migration_from_empty_reaches_the_latest_version(db: Path) -> None:
    store = Store(db)
    assert store.fetchone("PRAGMA user_version")[0] == latest_version() == 1
    tables = {r[0] for r in store.fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"presets", "settings", "recommendation_samples", "font_registry", "transactions", "journal_items",
            "backups", "location_records", "active_config", "coverage"} <= tables
    assert store.fetchone("SELECT COUNT(*) FROM active_config")[0] == 1  # the one permanent row


def test_reopening_does_not_migrate_again(db: Path) -> None:
    Store(db).close()
    store = Store(db)
    assert store.fetchone("PRAGMA user_version")[0] == 1


def _migrations(tmp_path: Path, files: dict[str, str]) -> Path:
    d = tmp_path / "migrations"
    d.mkdir()
    for name, text in files.items():
        (d / name).write_text(text)
    return d


def _raw(tmp_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(tmp_path / "m.db", isolation_level=None)


def test_each_previous_version_migrates_forward(tmp_path: Path) -> None:
    d = _migrations(tmp_path, {"001_a.sql": "CREATE TABLE a (x);", "002_b.sql": "CREATE TABLE b (y);",
                               "003_c.sql": "ALTER TABLE a ADD COLUMN z;"})
    for start in (0, 1, 2):
        conn = sqlite3.connect(tmp_path / f"m{start}.db", isolation_level=None)
        if start:
            apply_migrations(conn, _migrations_upto(tmp_path, start))
        assert apply_migrations(conn, d) == 3
        assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"a", "b"}


def _migrations_upto(tmp_path: Path, n: int) -> Path:
    d = tmp_path / f"upto{n}"
    d.mkdir()
    texts = ["CREATE TABLE a (x);", "CREATE TABLE b (y);"]
    for i in range(n):
        (d / f"{i + 1:03d}_m.sql").write_text(texts[i])
    return d


def test_failed_migration_leaves_the_version_and_the_schema_unchanged(tmp_path: Path) -> None:
    d = _migrations(tmp_path, {"001_a.sql": "CREATE TABLE a (x);",
                               "002_bad.sql": "CREATE TABLE b (y);\nINSERT INTO missing_table VALUES (1);"})
    conn = _raw(tmp_path)
    with pytest.raises(ScribeSenseError) as err:
        apply_migrations(conn, d)
    assert err.value.code is ErrorCode.MIGRATION_FAILED
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1  # 001 stays; 002 left no trace at all
    assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"a"}
    assert not conn.in_transaction


def test_migration_numbers_must_be_contiguous(tmp_path: Path) -> None:
    d = _migrations(tmp_path, {"001_a.sql": "CREATE TABLE a (x);", "003_c.sql": "CREATE TABLE c (z);"})
    with pytest.raises(ScribeSenseError) as err:
        apply_migrations(_raw(tmp_path), d)
    assert err.value.code is ErrorCode.MIGRATION_FAILED


def test_migration_with_trailing_garbage_is_refused(tmp_path: Path) -> None:
    d = _migrations(tmp_path, {"001_a.sql": "CREATE TABLE a (x);\nCREATE TABLE half ("})
    conn = _raw(tmp_path)
    with pytest.raises(ScribeSenseError):
        apply_migrations(conn, d)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0


# ---------------------------------------------------------------- schema versions and recovery mode (K9 test 9)

def _newer_db(db: Path) -> bytes:
    store = Store(db)
    store.execute("PRAGMA user_version = 99")
    store.close()
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return db.read_bytes()


def test_normal_mode_refuses_a_newer_schema(db: Path) -> None:
    _newer_db(db)
    with pytest.raises(ScribeSenseError) as err:
        Store(db)
    assert err.value.code is ErrorCode.SCHEMA_UNSUPPORTED


def test_recovery_mode_on_a_newer_schema_writes_nothing(db: Path) -> None:
    before = _newer_db(db)
    with pytest.raises(ScribeSenseError) as err:
        Store(db, mode="recovery")
    assert err.value.code is ErrorCode.SCHEMA_UNSUPPORTED
    assert db.read_bytes() == before
    assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == 99


def test_recovery_mode_never_creates_the_file(db: Path) -> None:
    with pytest.raises(ScribeSenseError) as err:
        Store(db, mode="recovery")
    assert err.value.code is ErrorCode.STORE_IO_ERROR
    assert not db.exists() and not db.parent.exists()


def test_recovery_mode_never_migrates_an_unmigrated_file(db: Path) -> None:
    db.parent.mkdir(parents=True)
    sqlite3.connect(db).close()  # an empty database: version 0
    with pytest.raises(ScribeSenseError) as err:
        Store(db, mode="recovery")
    assert err.value.code is ErrorCode.SCHEMA_UNSUPPORTED
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0


def test_recovery_mode_opens_a_current_store_and_can_write(db: Path) -> None:
    Store(db).close()
    store = Store(db, mode="recovery")
    store.settings.set("lock_wait_seconds", 120)  # recovery may finish work; it just never changes the schema
    assert store.settings.get("lock_wait_seconds") == 120


def test_unknown_mode_is_a_programming_error(db: Path) -> None:
    with pytest.raises(ValueError):
        Store(db, mode="fast")  # type: ignore[arg-type]


# ---------------------------------------------------------------- transactions

def test_transaction_commits_and_rolls_back(db: Path) -> None:
    store = Store(db)
    with store.transaction():
        store.settings.set("lock_wait_seconds", 90)
    assert store.settings.get("lock_wait_seconds") == 90
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.settings.set("lock_wait_seconds", 30)
            raise RuntimeError("boom")
    assert store.settings.get("lock_wait_seconds") == 90  # rolled back


def test_nested_transaction_joins_the_outer_one(db: Path) -> None:
    store = Store(db)
    with pytest.raises(RuntimeError):
        with store.transaction():
            with store.transaction():
                store.settings.set("lock_wait_seconds", 45)  # repository writes join too
            assert store.settings.get("lock_wait_seconds") == 45
            raise RuntimeError("outer fails after the inner finished")
    assert store.settings.get("lock_wait_seconds") == 60  # the default again: nothing was committed


def test_a_second_writer_gets_store_busy_not_corruption(db: Path) -> None:
    a = Store(db)
    b = Store(db)
    b.execute("PRAGMA busy_timeout = 50")  # keep the test fast
    with a.transaction():
        a.settings.set("lock_wait_seconds", 70)
        with pytest.raises(ScribeSenseError) as err:
            with b.transaction():
                pass
        assert err.value.code is ErrorCode.STORE_BUSY
    assert b.settings.get("lock_wait_seconds") == 70
    assert b.fetchone("PRAGMA integrity_check")[0] == "ok"


# ---------------------------------------------------------------- error mapping

@pytest.mark.parametrize("sqlite_code,expected", [
    (sqlite3.SQLITE_BUSY, ErrorCode.STORE_BUSY),
    (sqlite3.SQLITE_LOCKED, ErrorCode.STORE_BUSY),
    (sqlite3.SQLITE_READONLY, ErrorCode.STORE_READ_ONLY),
    (sqlite3.SQLITE_FULL, ErrorCode.NO_DISK_SPACE),
    (sqlite3.SQLITE_CORRUPT, ErrorCode.STORE_CORRUPT),
    (sqlite3.SQLITE_NOTADB, ErrorCode.STORE_CORRUPT),
    (sqlite3.SQLITE_IOERR, ErrorCode.STORE_IO_ERROR),
    (sqlite3.SQLITE_CANTOPEN, ErrorCode.STORE_IO_ERROR),
    (sqlite3.SQLITE_IOERR | (12 << 8), ErrorCode.STORE_IO_ERROR),  # extended code
])
def test_sqlite_errors_become_error_codes(sqlite_code: int, expected: ErrorCode) -> None:
    exc = sqlite3.OperationalError("raw sqlite text that must never reach a user")
    exc.sqlite_errorcode = sqlite_code  # type: ignore[attr-defined]
    mapped = store_core.map_sqlite_error(exc)
    assert mapped.code is expected and "raw sqlite" not in str(mapped)


def test_a_garbage_file_is_reported_as_corrupt(db: Path) -> None:
    db.parent.mkdir(parents=True)
    db.write_bytes(os.urandom(4096))
    with pytest.raises(ScribeSenseError) as err:
        Store(db)
    assert err.value.code is ErrorCode.STORE_CORRUPT


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_a_read_only_store_is_reported_as_read_only(db: Path) -> None:
    Store(db).close()
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    db.chmod(0o400)
    store = Store(db)  # opening a WAL store writes nothing, so it succeeds; the first WRITE is what fails
    assert store.settings.get("lock_wait_seconds") == 60  # reads still work
    with pytest.raises(ScribeSenseError) as err:
        store.settings.set("lock_wait_seconds", 90)
    assert err.value.code is ErrorCode.STORE_READ_ONLY
    assert store.settings.get("lock_wait_seconds") == 60  # and nothing was claimed as saved


def test_constraint_violations_become_a_store_error_that_safety_can_catch(db: Path) -> None:
    store = Store(db)
    with pytest.raises(StoreConstraintError) as err:
        store.execute("INSERT INTO presets VALUES ('x','x','builtin','{}','t','t')")
    assert isinstance(err.value, ScribeSenseError) and err.value.code is ErrorCode.STORE_CORRUPT  # K9: "integrity"
    assert "constraint" not in str(err.value).lower()  # no raw sqlite text, and safety/ never sees sqlite3
    assert store.fetchone("SELECT COUNT(*) FROM presets")[0] == 0


def test_a_constraint_failure_inside_a_transaction_rolls_everything_back(db: Path) -> None:
    store = Store(db)
    with pytest.raises(StoreConstraintError):
        with store.transaction():
            store.settings.set("lock_wait_seconds", 99)
            store.execute("INSERT INTO journal_items (tx_id, location, target_adapter, target_id, target_display_name, "
                          "kind, existed, old_sha256) VALUES (999, 'l', 'a', 't', 'd', 'file', 1, 'h')")  # no such tx
    assert store.settings.get("lock_wait_seconds") == 60


def test_a_failed_commit_never_leaves_the_connection_in_a_transaction(db: Path) -> None:
    store = Store(db)
    store.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    store.execute("CREATE TABLE child (pid INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)")
    with pytest.raises(StoreConstraintError):
        with store.transaction():
            store.execute("INSERT INTO child VALUES (42)")  # accepted now, refused only at COMMIT
    assert not store.conn().in_transaction  # rolled back, not left open
    assert store.fetchone("SELECT COUNT(*) FROM child")[0] == 0
    with store.transaction():  # and the connection is usable straight away
        store.settings.set("lock_wait_seconds", 80)
    assert store.settings.get("lock_wait_seconds") == 80


def test_nested_transactions_have_no_savepoint(db: Path) -> None:
    """Documented K9 behaviour: a nested call JOINS the outer one. If the caller catches an inner failure and goes
    on, the inner writes are committed with the outer transaction (O4 review note 1; M4.10 never catches inside one)."""
    store = Store(db)
    with store.transaction():
        try:
            with store.transaction():
                store.settings.set("lock_wait_seconds", 75)
                raise RuntimeError("inner fails, caller swallows it")
        except RuntimeError:
            pass
    assert store.settings.get("lock_wait_seconds") == 75


def test_the_wal_and_shm_files_are_private_too(db: Path) -> None:
    store = Store(db)
    store.settings.set("lock_wait_seconds", 70)  # a write, so the -wal file exists
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(db) + suffix)
        assert path.exists(), suffix
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, (suffix, oct(path.stat().st_mode))


# ---------------------------------------------------------------- deletions

def test_delete_preferences_keeps_fonts_and_recovery_data(db: Path) -> None:
    from scribesense.contracts.presets import Preset, PresetValues, WordSpacing

    store = Store(db)
    store.settings.set("recommend_opt_in", True)
    store.presets.save(Preset("u1", "Mine", PresetValues("Noto Sans", 0.12, WordSpacing("multiplier", 1.5), 1.5, 1.2, 1.0), "user"))
    store.execute("INSERT INTO transactions (kind,state,created_at,updated_at) VALUES ('apply','COMPLETED','t','t')")
    store.delete_preferences()
    assert store.fetchone("SELECT COUNT(*) FROM presets")[0] == 0
    assert store.fetchone("SELECT COUNT(*) FROM settings")[0] == 0
    assert store.fetchone("SELECT COUNT(*) FROM transactions")[0] == 1


def test_delete_recovery_data_needs_the_token(db: Path) -> None:
    store = Store(db)
    with pytest.raises(TypeError):
        store.delete_recovery_data(precondition=object())  # type: ignore[arg-type]


def test_delete_recovery_data_refuses_while_anything_is_unfinished_unresolved_or_managed(db: Path) -> None:
    store = Store(db)
    token = NoRecoveryObligations()
    store.execute("INSERT INTO transactions (kind,state,created_at,updated_at) VALUES ('apply','APPLYING','t','t')")
    with pytest.raises(ScribeSenseError) as err:
        store.delete_recovery_data(precondition=token)
    assert err.value.code is ErrorCode.RECOVERY_OBLIGATIONS_REMAIN
    store.execute("UPDATE transactions SET state = 'REVERTED'")
    store.execute("INSERT INTO location_records (location,target_adapter,target_id,target_display_name,kind,existed,"
                  "old_sha256,managed_sha256) VALUES ('/x','a','t','d','file',1,'h','m')")
    with pytest.raises(ScribeSenseError):
        store.delete_recovery_data(precondition=token)  # managed
    store.execute("UPDATE location_records SET managed_sha256 = NULL, unresolved = 'DRIFT_SKIPPED'")
    with pytest.raises(ScribeSenseError):
        store.delete_recovery_data(precondition=token)  # unresolved
    assert store.fetchone("SELECT COUNT(*) FROM location_records")[0] == 1  # nothing was deleted


def test_delete_recovery_data_clears_the_journal_but_keeps_the_revision_counter(db: Path) -> None:
    store = Store(db)
    store.execute("INSERT INTO transactions (kind,state,created_at,updated_at) VALUES ('apply','COMPLETED','t','t')")
    store.execute("INSERT INTO backups (sha256, bytes) VALUES (?, ?)", (hashlib.sha256(b"x").hexdigest(), b"x"))
    store.execute("UPDATE active_config SET config='{}', font_key='k', style_revision=7")
    store.delete_recovery_data(precondition=NoRecoveryObligations())
    for table in ("transactions", "backups", "journal_items", "coverage", "location_records"):
        assert store.fetchone(f"SELECT COUNT(*) FROM {table}")[0] == 0
    row = store.fetchone("SELECT config, font_key, style_revision FROM active_config")
    assert (row["config"], row["font_key"], row["style_revision"]) == (None, None, 7)
    assert store.fetchone("SELECT COUNT(*) FROM active_config")[0] == 1
