"""O3 — K9 repositories: presets, font registry, coverage (read-only), settings, samples (M3.1).

All SQL for these tables lives here; callers never see a cursor. Content ownership stays with the writing
module (font registry rows: M1.8; presets: M2.5 / M1.2). Decisions D3, D6, D8, D9 are in ARCHITECTURE.md (K9).
Built-in presets are NOT stored: they come from `scribesense.config.presets.BUILTINS` so they cannot drift (D3).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from scribesense.contracts.adapter import Target
from scribesense.contracts.config import Configuration
from scribesense.contracts.coverage import CoverageEntry, CoverageStatus
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.presets import BUILTIN_IDS, ORIGINAL, Preset
from scribesense.contracts.store import (
    FONT_STATUS_TRANSITIONS,
    SETTING_RANGES,
    SETTINGS,
    FontRegistryEntry,
)
from scribesense.log import log
from scribesense.store import serialize as ser

if TYPE_CHECKING:
    from scribesense.store.core import Store

CUSTOM_ID = "custom"
_TERMINAL_STATES = "('COMPLETED','REVERTED','FAILED_CLEAN')"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _corrupt() -> ScribeSenseError:
    return ScribeSenseError(ErrorCode.STORE_CORRUPT)


# ---------------------------------------------------------------- presets

class PresetRepo:
    """User presets and the one `custom` row. Built-ins are read-only and live in code; `original` is a sentinel."""

    def __init__(self, store: Store) -> None:
        self._s = store

    @staticmethod
    def _builtins() -> tuple[Preset, ...]:
        from scribesense.config.presets import BUILTINS  # lazy: keeps import order free of cycles

        return tuple(BUILTINS)

    def list(self) -> list[Preset]:
        """The catalogue: built-ins first, then user presets by name. The `custom` row is NOT included.

        Custom is editing state, not a catalogue entry — "stored ≠ listed" (K9 D3, confirmed by O1 on 2026-10-18; K9: "built-ins
        first, then user presets by name"). Read it with `get("custom")`. Anything that must see EVERY stored preset — above all the
        font clean-up (K7 rule 3) — uses `list_all()`.
        """
        rows = self._s.fetchall(
            "SELECT id, name, kind, data FROM presets WHERE kind = 'user' ORDER BY name COLLATE NOCASE, name, id"
        )
        return [*self._builtins(), *(ser.preset_from_row(r["id"], r["name"], r["kind"], r["data"]) for r in rows)]

    def list_all(self) -> list[Preset]:
        """EVERY preset the store knows: built-ins, the `custom` row if it exists, then user presets by name.

        This is what anything that decides whether a font is still in use must read (K7 rule 3: "a saved preset" includes
        Custom): a caller that used `list()` and missed the custom row would let the clean-up delete the font of the
        user's own current configuration. Not in the frozen K9 protocol — an addition of M3.1.
        """
        rows = self._s.fetchall(
            "SELECT id, name, kind, data FROM presets "
            "ORDER BY CASE kind WHEN 'custom' THEN 0 ELSE 1 END, name COLLATE NOCASE, name, id"
        )
        return [*self._builtins(), *(ser.preset_from_row(r["id"], r["name"], r["kind"], r["data"]) for r in rows)]

    def get(self, id: str) -> Preset | None:
        for builtin in self._builtins():
            if builtin.id == id:
                return builtin
        if id == ORIGINAL:
            return None  # a sentinel, never a preset
        row = self._s.fetchone("SELECT id, name, kind, data FROM presets WHERE id = ?", (id,))
        return None if row is None else ser.preset_from_row(row["id"], row["name"], row["kind"], row["data"])

    def save(self, p: Preset) -> None:
        from scribesense.config.presets import RESERVED_IDS

        if p.kind == "builtin" or p.id in BUILTIN_IDS:
            raise ScribeSenseError(ErrorCode.PRESET_READ_ONLY)
        if p.id == ORIGINAL:
            raise ValueError("'original' is a sentinel, not a preset")
        if p.kind == "custom" and p.id != CUSTOM_ID:
            raise ValueError("the custom preset has the reserved id 'custom'")
        if p.kind == "user" and p.id in RESERVED_IDS:
            raise ValueError("a user preset cannot use a reserved id")
        if p.kind not in ("user", "custom"):
            raise ValueError("preset kind must be 'user' or 'custom'")
        if not isinstance(p.id, str) or not p.id or not isinstance(p.name, str) or not p.name:
            raise ValueError("a preset needs a non-empty id and name")
        data = ser.preset_values_to_json(p.values)
        now = ser.ts_to_text(self._s.clock())
        self._s.execute(
            "INSERT INTO presets (id, name, kind, data, created_at, updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, kind = excluded.kind, "
            "data = excluded.data, updated_at = excluded.updated_at",
            (p.id, p.name, p.kind, data, now, now),
        )

    def delete(self, id: str) -> bool:
        if id in BUILTIN_IDS:
            raise ScribeSenseError(ErrorCode.PRESET_READ_ONLY)
        return self._s.execute("DELETE FROM presets WHERE id = ?", (id,)).rowcount > 0


# ---------------------------------------------------------------- font registry

class FontRegistryRepo:
    """Generated-font records (content owner: M1.8). Status flow: building -> validated | failed;
    failed -> building (retry); validated -> removed."""

    def __init__(self, store: Store) -> None:
        self._s = store

    # -- reading

    @staticmethod
    def _entry(row: Any) -> FontRegistryEntry:
        return FontRegistryEntry(
            key=row["key"],
            config=ser.config_from_json(row["config"]),
            reading_family=row["reading_family"],
            ui_family=row["ui_family"],
            styles=ser.array_from_json(row["styles"]),
            files=ser.array_from_json(row["files"]),
            file_sha256=ser.array_from_json(row["file_sha256"]),
            status=row["status"],
            reason_code=row["reason_code"],
            created_at=ser.ts_from_text(row["created_at"]),
            last_used_at=ser.ts_from_text(row["last_used_at"]),
        )

    def get(self, key: str) -> FontRegistryEntry | None:
        row = self._s.fetchone("SELECT * FROM font_registry WHERE key = ?", (key,))
        return None if row is None else self._entry(row)

    def keys(self, status: str | None = None) -> list[str]:
        if status is None:
            rows = self._s.fetchall("SELECT key FROM font_registry ORDER BY key")
        else:
            rows = self._s.fetchall("SELECT key FROM font_registry WHERE status = ? ORDER BY key", (status,))
        return [r["key"] for r in rows]

    # -- writing

    @staticmethod
    def _identity(e: FontRegistryEntry) -> tuple[object, ...]:
        """What makes two entries the SAME font set (D8): the font key's numeric inputs — never `text_scale`, never the
        `base_family` label — plus the generated families, styles, files and hashes."""
        return (ser.font_identity(e.config), e.reading_family, e.ui_family, e.styles, e.files, e.file_sha256)

    def create(self, e: FontRegistryEntry) -> None:
        if e.status not in FONT_STATUS_TRANSITIONS:
            raise ValueError(f"unknown font status {e.status!r}")
        if not e.key or not e.reading_family or not e.ui_family:
            raise ValueError("a registry entry needs a key and both family names")
        if len(e.files) != len(e.file_sha256):
            raise ValueError("files and file_sha256 must have the same length")
        values = (
            e.key, ser.config_to_json(e.config), e.reading_family, e.ui_family, ser.array_to_json(e.styles),
            ser.array_to_json(e.files), ser.array_to_json(e.file_sha256), e.status, e.reason_code,
            ser.ts_to_text(e.created_at), ser.ts_to_text(e.last_used_at),
        )
        with self._s.transaction():
            existing = self.get(e.key)
            if existing is not None:
                if self._identity(existing) != self._identity(e):
                    raise ScribeSenseError(ErrorCode.FONT_IDENTITY_CONFLICT)
                if existing.config.base_family != e.config.base_family:
                    # Same bytes, renamed label: not a conflict. The stored label stays; the event is only noted.
                    log("OK", "store", step="font_identity", reason="family_label_changed")
                return  # the same font set again: nothing to change
            self._s.execute(
                "INSERT INTO font_registry (key, config, reading_family, ui_family, styles, files, file_sha256, "
                "status, reason_code, created_at, last_used_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", values)

    def set_status(self, key: str, status: str, reason: str | None) -> None:
        with self._s.transaction():
            row = self._s.fetchone("SELECT status FROM font_registry WHERE key = ?", (key,))
            if row is None:
                raise KeyError(key)
            if status not in FONT_STATUS_TRANSITIONS.get(row["status"], frozenset()):
                raise ValueError(f"font status {row['status']} -> {status} is not allowed")
            self._s.execute("UPDATE font_registry SET status = ?, reason_code = ? WHERE key = ?",
                            (status, reason, key))

    def touch(self, key: str) -> None:
        cur = self._s.execute("UPDATE font_registry SET last_used_at = ? WHERE key = ?",
                              (ser.ts_to_text(self._s.clock()), key))
        if cur.rowcount == 0:
            raise KeyError(key)

    def fail_interrupted_builds(self) -> int:
        """A font left in 'building' was interrupted: -> failed / INTERRUPTED_BUILD. Returns how many."""
        return self._s.execute(
            "UPDATE font_registry SET status = 'failed', reason_code = ? WHERE status = 'building'",
            (ErrorCode.INTERRUPTED_BUILD.value,),
        ).rowcount

    def in_use(self, entry: FontRegistryEntry) -> bool:
        """K7 rule 3 references this store can see. If a check cannot be made the answer is True (keep)."""
        s = self._s
        if s.fetchone("SELECT 1 FROM active_config WHERE font_key = ?", (entry.key,)):
            return True
        if s.fetchone("SELECT 1 FROM location_records WHERE managed_font_key = ?", (entry.key,)):
            return True
        # unfinished transactions: a planned write that names one of this font's families
        if s.fetchone(
            "SELECT 1 FROM journal_items i JOIN transactions t ON t.id = i.tx_id "
            f"WHERE t.state NOT IN {_TERMINAL_STATES} AND ("
            " instr(i.intent_text, ?) > 0 OR instr(i.intent_text, ?) > 0 OR"
            " instr(i.intent_blob, ?) > 0 OR instr(i.intent_blob, ?) > 0)",
            (entry.reading_family, entry.ui_family,
             entry.reading_family.encode(), entry.ui_family.encode()),
        ):
            return True
        return self._referenced_by_a_preset(entry.config)

    def _referenced_by_a_preset(self, config: Configuration) -> bool:
        from scribesense.config.presets import resolve  # O1: preset intent -> concrete Configuration

        wanted = ser.preset_font_match(config)
        for preset in self._s.presets.list_all():  # built-ins and EVERY saved preset, incl. the custom row
            try:
                if ser.preset_font_match(resolve(preset.values)) == wanted:
                    return True
            except Exception:  # noqa: BLE001 — cannot tell whether this preset needs the font: keep it
                return True
        return False

    def mark_removed(self, key: str) -> None:
        with self._s.transaction():
            entry = self.get(key)
            if entry is None:
                raise KeyError(key)
            if entry.status == "removed":
                return
            if self.in_use(entry):
                raise ScribeSenseError(ErrorCode.FONT_IN_USE)
            self.set_status(key, "removed", None)


# ---------------------------------------------------------------- coverage (read-only)

class CoverageRepo:
    """READ-ONLY. Writes go through Journal.record_coverage (K4) -> store/journal_tables.py."""

    def __init__(self, store: Store) -> None:
        self._s = store

    def last_completed_apply(self) -> list[CoverageEntry]:
        """Coverage of the newest COMPLETED apply — historical, not proof of what is active now."""
        rows = self._s.fetchall(
            "SELECT c.* FROM coverage c WHERE c.tx_id = ("
            " SELECT id FROM transactions WHERE kind = 'apply' AND state = 'COMPLETED'"
            " ORDER BY created_at DESC, id DESC LIMIT 1) ORDER BY c.target_adapter, c.target_id")
        return [coverage_from_row(r) for r in rows]


def coverage_from_row(row: Any) -> CoverageEntry:
    try:
        status = CoverageStatus(row["status"])
    except ValueError:
        raise _corrupt() from None
    detail: ErrorCode | str
    try:
        detail = ErrorCode(row["detail_code"])
    except ValueError:
        detail = row["detail_code"]
    return CoverageEntry(
        target=Target(row["target_adapter"], row["target_id"], row["target_display_name"], False),
        status=status,
        detail_code=detail,
    )


# ---------------------------------------------------------------- settings

class SettingsRepo:
    def __init__(self, store: Store) -> None:
        self._s = store

    @staticmethod
    def _check(name: str, value: object) -> None:
        if name not in SETTINGS:
            raise KeyError(name)
        wanted, _default = SETTINGS[name]
        if type(value) is not wanted:  # exact type: True is not an int
            raise TypeError(f"setting {name} must be {wanted.__name__}")
        bounds = SETTING_RANGES.get(name)
        if bounds is not None and not bounds[0] <= value <= bounds[1]:  # type: ignore[operator]
            raise ValueError(f"setting {name} must be between {bounds[0]} and {bounds[1]}")

    def get(self, name: str) -> object:
        if name not in SETTINGS:
            raise KeyError(name)
        row = self._s.fetchone("SELECT value FROM settings WHERE name = ?", (name,))
        if row is None:
            return SETTINGS[name][1]
        try:
            value = json.loads(row["value"])
            self._check(name, value)
        except (ValueError, TypeError):
            raise _corrupt() from None  # a stored value that no longer satisfies its own rule
        return value

    def set(self, name: str, value: object) -> None:
        self._check(name, value)
        with self._s.transaction():
            self._s.execute(
                "INSERT INTO settings (name, value) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                (name, json.dumps(value)),
            )
            if name == "recommend_opt_in" and value is False:
                self._s.samples.delete_all()  # opting out also deletes what was already collected


# ---------------------------------------------------------------- recommendation samples

class SampleRepo:
    """Opt-in usage samples: a Configuration and a time — never text, never an app name."""

    def __init__(self, store: Store) -> None:
        self._s = store

    def add(self, config: Configuration) -> None:
        with self._s.transaction():
            if self._s.settings.get("recommend_opt_in") is not True:  # re-read on every call
                raise ScribeSenseError(ErrorCode.SAMPLES_DISABLED)
            self._s.execute("INSERT INTO recommendation_samples (config, created_at) VALUES (?, ?)",
                            (ser.config_to_json(config), ser.ts_to_text(self._s.clock())))

    def delete_all(self) -> None:
        self._s.execute("DELETE FROM recommendation_samples")
