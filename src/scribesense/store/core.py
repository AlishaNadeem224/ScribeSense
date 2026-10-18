"""O3 — The store: one SQLite file, migrations, per-thread connections, transactions (M3.1, K9).

Rules (K9 — signed off by O3 2026-10-15):
  - Every connection: WAL, synchronous=FULL, foreign_keys=ON, busy_timeout=5000. One connection per THREAD;
    never shared across threads or processes.
  - Errors map to codes, never raw sqlite3 text: STORE_BUSY (contention) · STORE_READ_ONLY (permission) ·
    STORE_CORRUPT (integrity) · NO_DISK_SPACE (disk full) · STORE_IO_ERROR (other I/O). A failed write is never
    reported as saved: the exception is raised before the caller can assume success.
  - Migrations: PRAGMA user_version + store/migrations/NNN_name.sql in order; each migration and its version bump
    are ONE transaction; failure -> MIGRATION_FAILED and the version is unchanged. A newer schema than this code
    knows -> SCHEMA_UNSUPPORTED. mode="recovery" NEVER creates the file and NEVER migrates (A2: recovery must not
    change the schema of a store it only needs to read and finish).
  - Only this package imports sqlite3 (import rule R3).
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from scribesense.contracts import paths
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import NoRecoveryObligations
from scribesense.contracts.store import SQLITE_BUSY_TIMEOUT_MS
from scribesense.store.repos import (
    CoverageRepo,
    FontRegistryRepo,
    PresetRepo,
    SampleRepo,
    SettingsRepo,
    utcnow,
)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")

# SQLite primary result codes (the low byte of sqlite_errorcode).
_BUSY, _LOCKED, _READONLY, _IOERR, _CORRUPT, _FULL, _CANTOPEN, _NOTADB = 5, 6, 8, 10, 11, 13, 14, 26


def default_path() -> Path:
    """data_dir()/scribesense.db (K9 decision D6). No path is ever hard-coded (K8)."""
    return paths.data_dir() / "scribesense.db"


def map_sqlite_error(exc: sqlite3.Error) -> ScribeSenseError:
    code = getattr(exc, "sqlite_errorcode", None)
    primary = code & 0xFF if isinstance(code, int) else None
    if primary in (_BUSY, _LOCKED):
        return ScribeSenseError(ErrorCode.STORE_BUSY)
    if primary == _READONLY:
        return ScribeSenseError(ErrorCode.STORE_READ_ONLY)
    if primary == _FULL:
        return ScribeSenseError(ErrorCode.NO_DISK_SPACE)
    if primary in (_CORRUPT, _NOTADB):
        return ScribeSenseError(ErrorCode.STORE_CORRUPT)
    if primary in (_IOERR, _CANTOPEN):
        return ScribeSenseError(ErrorCode.STORE_IO_ERROR)
    return ScribeSenseError(ErrorCode.STORE_IO_ERROR)


class StoreConstraintError(ScribeSenseError):
    """A constraint (CHECK, foreign key, unique, NOT NULL) refused a write — a caller bug or an inconsistent record,
    never a transient storage failure. Carries STORE_CORRUPT (K9: "integrity"); its own type keeps it distinguishable.
    `safety/` may catch it without importing sqlite3 (import rule R3)."""

    def __init__(self) -> None:
        super().__init__(ErrorCode.STORE_CORRUPT)


@contextmanager
def _guard() -> Iterator[None]:
    try:
        yield
    except sqlite3.IntegrityError:
        raise StoreConstraintError() from None
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc) from None


# ---------------------------------------------------------------- migrations

def migration_files(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    """NNN_name.sql files in order. Numbers must be 1..N without gaps, else MIGRATION_FAILED."""
    found = sorted(
        (int(m.group(1)), p) for p in directory.iterdir() if (m := _MIGRATION_NAME.match(p.name))
    )
    if [n for n, _ in found] != list(range(1, len(found) + 1)):
        raise ScribeSenseError(ErrorCode.MIGRATION_FAILED)
    return found


def latest_version(directory: Path = MIGRATIONS_DIR) -> int:
    files = migration_files(directory)
    return files[-1][0] if files else 0


def _statements(script: str) -> list[str]:
    """Split a migration into complete statements (no executescript: it would commit our transaction)."""
    out: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf)
            buf = ""
    if any(ln.strip() and not ln.strip().startswith("--") for ln in buf.splitlines()):
        raise ScribeSenseError(ErrorCode.MIGRATION_FAILED)  # trailing text that is not a complete statement
    return out


def user_version(conn: sqlite3.Connection) -> int:
    with _guard():
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def apply_migrations(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> int:
    """Bring the schema to the newest version. Returns the version reached."""
    files = migration_files(directory)
    latest = files[-1][0] if files else 0
    if user_version(conn) > latest:
        raise ScribeSenseError(ErrorCode.SCHEMA_UNSUPPORTED)
    for number, path in files:
        statements = _statements(path.read_text(encoding="utf-8"))
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if user_version(conn) >= number:  # another process migrated while we waited for the lock
                    conn.execute("ROLLBACK")
                    continue
                for statement in statements:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {number}")
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        except ScribeSenseError:
            raise
        except sqlite3.Error as exc:
            mapped = map_sqlite_error(exc)
            if mapped.code in (ErrorCode.STORE_BUSY, ErrorCode.STORE_READ_ONLY, ErrorCode.NO_DISK_SPACE):
                raise mapped from None
            raise ScribeSenseError(ErrorCode.MIGRATION_FAILED) from None
    return user_version(conn)


# ---------------------------------------------------------------- the store

class Store:
    """K9 Store. Satisfies scribesense.contracts.store.Store."""

    def __init__(self, path: Path, *, mode: Literal["normal", "recovery"] = "normal") -> None:
        if mode not in ("normal", "recovery"):
            raise ValueError("mode must be 'normal' or 'recovery'")
        self.path = Path(path)
        self.mode = mode
        self.clock = utcnow  # tests may replace it
        self._tls = threading.local()
        self._open()
        self.presets = PresetRepo(self)
        self.fonts = FontRegistryRepo(self)
        self.coverage = CoverageRepo(self)
        self.settings = SettingsRepo(self)
        self.samples = SampleRepo(self)

    # ---- opening

    def _open(self) -> None:
        if self.mode == "recovery":
            if not self.path.is_file():
                raise ScribeSenseError(ErrorCode.STORE_IO_ERROR)  # nothing to recover; never create a file
        else:
            created = not self.path.exists()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if created:
                    # Create the file 0600 BEFORE SQLite does, so the -wal / -shm files (which copy the database
                    # file's mode) are private too. An empty file is a valid new SQLite database.
                    os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
            except OSError:
                raise ScribeSenseError(ErrorCode.STORE_IO_ERROR) from None
        conn = self._connect(set_pragmas=False)
        try:
            version = user_version(conn)
            if self.mode == "recovery":
                # The newest schema this code knows is the only one it may act on: no migration here.
                if version < 1 or version > latest_version():
                    raise ScribeSenseError(ErrorCode.SCHEMA_UNSUPPORTED)
                self._set_pragmas(conn)
            else:
                if version > latest_version():
                    raise ScribeSenseError(ErrorCode.SCHEMA_UNSUPPORTED)
                self._set_pragmas(conn)
                apply_migrations(conn)
        except BaseException:
            conn.close()
            self._tls.conn = None
            raise

    def _connect(self, *, set_pragmas: bool = True) -> sqlite3.Connection:
        target = (f"file:{quote(str(self.path))}?mode=rw" if self.mode == "recovery" else str(self.path))
        with _guard():
            conn = sqlite3.connect(
                target, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000, isolation_level=None,
                uri=self.mode == "recovery",
            )
            conn.row_factory = sqlite3.Row
        self._tls.conn = conn
        self._tls.depth = 0
        if set_pragmas:
            self._set_pragmas(conn)
        return conn

    @staticmethod
    def _set_pragmas(conn: sqlite3.Connection) -> None:
        with _guard():
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")

    def conn(self) -> sqlite3.Connection:
        """This thread's connection (created on first use). Never pass it to another thread."""
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = self._connect()
        return conn  # type: ignore[no-any-return]

    def close(self) -> None:
        """Close the calling thread's connection."""
        conn = getattr(self._tls, "conn", None)
        if conn is not None:
            conn.close()
            self._tls.conn = None

    @property
    def journal_mode(self) -> str:
        with _guard():
            return str(self.conn().execute("PRAGMA journal_mode").fetchone()[0])

    # ---- statements (every sqlite error becomes an ErrorCode)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with _guard():
            return self.conn().execute(sql, tuple(params))

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with _guard():
            return self.conn().execute(sql, tuple(params)).fetchall()

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with _guard():
            return self.conn().execute(sql, tuple(params)).fetchone()

    # ---- transactions

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit on exit, rollback on exception. A nested call JOINS the outer transaction: there is no savepoint, so
        if an inner block fails and the caller catches that and carries on, the inner writes are still committed
        with the outer transaction (K9: "nested call joins the outer one"). Never wrap an adapter write in this
        (K9): a transaction must not stay open while a slow external action runs. If COMMIT itself fails the
        transaction is rolled back before the error is raised, so the connection is never left mid-transaction."""
        conn = self.conn()
        depth = self._tls.depth
        if depth == 0:
            with _guard():
                conn.execute("BEGIN IMMEDIATE")
        self._tls.depth = depth + 1
        try:
            yield
        except BaseException:
            self._tls.depth = depth
            if depth == 0 and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        self._tls.depth = depth
        if depth == 0:
            try:
                with _guard():
                    conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    # ---- K9 deletions

    def delete_preferences(self) -> None:
        """Presets, settings and samples. Never recovery data, never the font registry."""
        with self.transaction():
            for table in ("presets", "settings", "recommendation_samples"):
                self.execute(f"DELETE FROM {table}")

    def has_obligations(self) -> bool:
        """True while anything is unfinished, unresolved or managed (what Journal.no_obligations() tests)."""
        row = self.fetchone(
            "SELECT "
            " EXISTS(SELECT 1 FROM transactions WHERE state NOT IN ('COMPLETED','REVERTED','FAILED_CLEAN')) "
            " OR EXISTS(SELECT 1 FROM location_records WHERE managed_sha256 IS NOT NULL OR unresolved IS NOT NULL)"
        )
        return bool(row[0])

    def delete_recovery_data(self, *, precondition: NoRecoveryObligations) -> None:
        """Journal tables only. The token comes from Journal.no_obligations(); it is re-checked inside the
        transaction because the world may have changed since it was issued."""
        if not isinstance(precondition, NoRecoveryObligations):
            raise TypeError("delete_recovery_data needs the token from Journal.no_obligations()")
        with self.transaction():
            if self.has_obligations():
                raise ScribeSenseError(ErrorCode.RECOVERY_OBLIGATIONS_REMAIN)
            for table in ("coverage", "journal_items", "transactions", "location_records", "backups"):
                self.execute(f"DELETE FROM {table}")
            # One permanent row; the revision counter keeps counting (the extension compares revisions).
            self.execute("UPDATE active_config SET config = NULL, font_key = NULL, source_preset_id = NULL")

    def recover_interrupted_builds(self) -> int:
        """Call ONCE at service start-up: a font still 'building' was interrupted -> failed / INTERRUPTED_BUILD.
        Not done on every open: a short-lived CLI process must not fail a build the service is running."""
        return self.fonts.fail_interrupted_builds()
