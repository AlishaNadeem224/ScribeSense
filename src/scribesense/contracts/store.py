"""K9 — Store repositories (owner O3). *** DRAFT — O3 SIGN-OFF REQUIRED ***

Do NOT build the store (M3.1), journal (M4.10), revert (M4.11) or the recovery scenarios against
this file until O3 signs off K9 in ARCHITECTURE.md. Other modules may type against it.

Rules (summary — full text in ARCHITECTURE.md K9):
- Every connection: WAL, synchronous=FULL, foreign_keys=ON, busy_timeout=5000; one per thread.
- Reads and writes may raise STORE_BUSY; callers retry a bounded number of times.
- Errors: STORE_BUSY · STORE_READ_ONLY · STORE_CORRUPT · NO_DISK_SPACE · STORE_IO_ERROR.
- Migrations: PRAGMA user_version; store/migrations/NNN_name.sql; recovery mode never migrates.
- Journal-only tables (transactions, journal_items, backups, location_records, active_config,
  coverage) are reached ONLY through K4 Journal.
- Configuration / Preset stored as versioned JSON {"v": 1, ...}; timestamps UTC ISO-8601.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from scribesense.contracts.config import Configuration
from scribesense.contracts.coverage import CoverageEntry
from scribesense.contracts.journal import NoRecoveryObligations
from scribesense.contracts.presets import Preset

FontStatus = Literal["building", "validated", "failed", "removed"]

#: building -> validated | failed · failed -> building (retry) · validated -> removed.
FONT_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "building": frozenset({"validated", "failed"}),
    "failed": frozenset({"building"}),
    "validated": frozenset({"removed"}),
    "removed": frozenset(),
}

SQLITE_BUSY_TIMEOUT_MS = 5000
SERIALIZATION_VERSION = 1


@dataclass(frozen=True)
class FontRegistryEntry:
    key: str
    config: Configuration
    reading_family: str
    ui_family: str
    styles: tuple[str, ...]
    files: tuple[str, ...]
    file_sha256: tuple[str, ...]  # verified before activation; mismatch -> VALIDATION_FAILED
    status: FontStatus
    reason_code: str | None
    created_at: datetime  # UTC, timezone-aware
    last_used_at: datetime


#: Fixed list: name -> (type, default).
SETTINGS: dict[str, tuple[type, object]] = {
    "recommend_opt_in": (bool, False),
    "keybind_reader": (str, "SUPER ALT, R"),
    "keybind_preset_next": (str, "SUPER ALT, P"),
    "keybind_reset": (str, "CTRL ALT SHIFT SUPER, BackSpace"),
    "lock_wait_seconds": (int, 60),  # valid 10–600
}
SETTING_RANGES: dict[str, tuple[int, int]] = {"lock_wait_seconds": (10, 600)}


class PresetRepo(Protocol):
    def list(self) -> list[Preset]: ...  # built-ins first, then user presets by name

    def get(self, id: str) -> Preset | None: ...

    def save(self, p: Preset) -> None: ...  # built-in -> PRESET_READ_ONLY

    def delete(self, id: str) -> bool: ...  # False if not found; built-in -> PRESET_READ_ONLY


class FontRegistryRepo(Protocol):
    def get(self, key: str) -> FontRegistryEntry | None: ...

    def create(self, e: FontRegistryEntry) -> None: ...  # same key, different content -> FONT_IDENTITY_CONFLICT

    def set_status(self, key: str, status: FontStatus, reason: str | None) -> None: ...  # missing -> KeyError

    def touch(self, key: str) -> None: ...  # last_used_at; missing -> KeyError

    def keys(self, status: FontStatus | None = None) -> list[str]: ...

    def mark_removed(self, key: str) -> None: ...  # in use -> FONT_IN_USE


class CoverageRepo(Protocol):
    """READ-ONLY. Writes go through Journal.record_coverage (K4)."""

    def last_completed_apply(self) -> list[CoverageEntry]: ...


class SettingsRepo(Protocol):
    def get(self, name: str) -> object: ...  # unknown -> KeyError; default if never set

    def set(self, name: str, value: object) -> None: ...  # KeyError / TypeError (exact type) / ValueError (range)


class SampleRepo(Protocol):
    def add(self, config: Configuration) -> None: ...  # re-reads recommend_opt_in; off -> SAMPLES_DISABLED

    def delete_all(self) -> None: ...


class Store(Protocol):
    presets: PresetRepo
    fonts: FontRegistryRepo
    coverage: CoverageRepo
    settings: SettingsRepo
    samples: SampleRepo

    def __init__(self, path: Path, *, mode: Literal["normal", "recovery"] = "normal") -> None: ...

    def transaction(self) -> AbstractContextManager[None]:
        """Commit on exit, rollback on exception; nested calls join; never spans an adapter write."""
        ...

    def delete_preferences(self) -> None: ...  # presets, settings, samples — never recovery data

    def delete_recovery_data(self, *, precondition: NoRecoveryObligations) -> None: ...
