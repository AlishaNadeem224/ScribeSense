"""O4 — GTK adapter (M4.3): GTK/GNOME fonts and text size through gsettings.

Target: one, "gtk:user". Locations (K3 rule 10, kind "key"), schema org.gnome.desktop.interface:
    font-name            -> UI variant      (A13: identified UI setting)
    document-font-name   -> reading variant
    text-scaling-factor  -> config.text_scale
    monospace-font-name  is NEVER touched (card: must never).

Reading (R2-8, decided 2026-10-12): Gio.Settings.get_user_value() — returns nothing for an unset key, so
"present" means "explicitly set by the user" (K3 rule 11) and follows the active GSETTINGS_BACKEND (the
test sandbox and production behave the same). Since 2026-10-15 every read runs in a FRESH process
(gsettings_probe): a long-running process saw stale values after `gsettings set` (O3 CI finding). The schema
is looked up first, because constructing Gio.Settings for a missing schema aborts the process.
Writing: `gsettings set <schema> <key> <value>` (type-checked) and `gsettings reset` for an unset original,
through run_cmd (K3 rule 9). After a write the value is read back; it must equal the planned hash.

Canonical text: State.data for a key is the GVariant text exactly as GLib prints it. Planned payloads are
canonicalized the same way (GLib prints the double 1.1 as "1.1000000000000001"), otherwise a correct write
would never match its plan.

Font sizes: font-name / document-font-name keep the size of the user's current effective value
(e.g. "Fira Sans Semi-Bold 11" -> "AccessSansUI-<key> 11"); default 11 when it can't be read.

Verify (A14): configuration check only — the effective values name our families. drawn_ok=None: GTK's
drawn check is harness-only (A14); Pango's drawn check runs on the fontconfig target.
All storage access goes through an injectable SettingsStore so tests need neither PyGObject nor a session.
"""

from __future__ import annotations

import json
import math
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from scribesense.contracts.adapter import (
    DEFAULT_TIMEOUTS,
    AppliedItem,
    PlannedWrite,
    SnapshotItem,
    State,
    Target,
    VerifyResult,
    state_sha256,
)
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import READING_FAMILY_PREFIX, UI_FAMILY_PREFIX, FontSet
from scribesense.contracts.paths import run_cmd
from scribesense.log import log

SCHEMA = "org.gnome.desktop.interface"
FONT_NAME = "font-name"
DOCUMENT_FONT_NAME = "document-font-name"
TEXT_SCALE = "text-scaling-factor"
NEVER_TOUCHED = ("monospace-font-name",)
KEYS = (FONT_NAME, DOCUMENT_FONT_NAME, TEXT_SCALE)
LOCATIONS = tuple(f"{SCHEMA} {k}" for k in KEYS)
TYPES = {FONT_NAME: "s", DOCUMENT_FONT_NAME: "s", TEXT_SCALE: "d"}
TARGET_ID = "gtk:user"
DEFAULT_SIZE = 11.0
READ_BACK_TIMEOUT_S = 1.0
READ_BACK_POLL_S = 0.05

_SIZE = re.compile(r"\s(\d+(?:\.\d+)?)(?:px)?\s*$")

Runner = Callable[[list[str], float], Any]


# ----------------------------------------------------------------- canonical GVariant text

def format_double(value: float) -> str:
    """GVariant text of a double exactly as GLib prints it ("%.17g", plus ".0" for whole numbers)."""
    if not math.isfinite(value):
        raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR)
    text = "%.17g" % value
    if not any(c in text for c in ".eEn"):
        text += ".0"
    return text


def format_string(value: str) -> str:
    """GVariant text of a string as GLib prints it: single quotes, or double quotes when the text contains
    a single quote; backslashes and the chosen quote character are escaped (checked against real GLib)."""
    quote = '"' if "'" in value else "'"
    escaped = value.replace("\\", "\\\\").replace(quote, "\\" + quote)
    return quote + escaped + quote


def size_of(font_desc: str) -> float:
    """Size at the end of a Pango font description ("Fira Sans Semi-Bold 11" -> 11.0)."""
    match = _SIZE.search(" " + font_desc)
    if not match:
        return DEFAULT_SIZE
    size = float(match.group(1))
    return size if 1 <= size <= 200 else DEFAULT_SIZE


def family_of(font_desc: str) -> str:
    """Family part of a font description we wrote ("AccessSansUI-abc 11" -> "AccessSansUI-abc")."""
    match = _SIZE.search(" " + font_desc)
    return font_desc[: match.start()].strip() if match else font_desc.strip()


def format_size(size: float) -> str:
    return str(int(size)) if size == int(size) else f"{size:g}"


# ----------------------------------------------------------------- settings storage

@dataclass(frozen=True)
class KeyValue:
    text: str  # canonical GVariant text
    value_type: str  # GVariant type string, e.g. "s", "d"
    value: Any  # unpacked Python value


class SettingsStore(Protocol):
    def schema_exists(self, schema: str) -> bool: ...

    def user_value(self, schema: str, key: str) -> KeyValue | None: ...  # None = unset

    def effective_value(self, schema: str, key: str) -> KeyValue: ...

    def canonical(self, value_type: str, value: Any) -> str: ...

    def set(self, schema: str, key: str, text: str) -> None: ...

    def reset(self, schema: str, key: str) -> None: ...


class GioSettingsStore:
    """Production store. Reads (R2-8, Gio get_user_value) run in a FRESH process — the gsettings_probe module —
    so a long-running process never sees a stale cached backend (O3 CI finding 2026-10-15; see that module).
    Writes go through `gsettings set/reset` via run_cmd. Only canonical() runs in-process (pure GLib formatting)."""

    PROBE_MODULE = "scribesense.adapters.gsettings_probe"

    def __init__(self, runner: Runner = run_cmd) -> None:
        self._run = runner
        self._schema_present: dict[str, bool] = {}  # a schema doesn't appear or vanish during one run

    def _glib(self) -> Any:
        try:
            import gi

            gi.require_version("GLib", "2.0")
            from gi.repository import GLib
        except (ImportError, ValueError):
            raise ScribeSenseError(ErrorCode.ADAPTER_UNAVAILABLE) from None
        return GLib

    def _probe(self, schema: str, keys: tuple[str, ...]) -> dict[str, Any]:
        argv = [sys.executable, "-P", "-m", self.PROBE_MODULE, schema, *keys]
        try:
            done = self._run(argv, DEFAULT_TIMEOUTS["gsettings"])
        except ScribeSenseError:
            raise
        except OSError:
            raise ScribeSenseError(ErrorCode.ADAPTER_UNAVAILABLE) from None
        try:
            result = json.loads(done.stdout.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, AttributeError):
            raise ScribeSenseError(ErrorCode.ADAPTER_COMMAND_FAILED) from None
        if not isinstance(result, dict) or result.get("v") != 1 or not isinstance(result.get("values"), dict):
            raise ScribeSenseError(ErrorCode.ADAPTER_COMMAND_FAILED)
        self._schema_present[schema] = bool(result.get("schema"))
        if keys and not result.get("schema"):
            raise ScribeSenseError(ErrorCode.SCHEMA_MISSING)
        if keys and set(result["values"]) != set(keys):
            raise ScribeSenseError(ErrorCode.ADAPTER_COMMAND_FAILED)
        return result

    def schema_exists(self, schema: str) -> bool:
        if schema not in self._schema_present:
            self._probe(schema, ())
        return self._schema_present[schema]

    def user_value(self, schema: str, key: str) -> KeyValue | None:
        entry = self._probe(schema, (key,))["values"][key]
        if entry["user"] is None:
            return None
        return KeyValue(entry["user"], entry["type"], entry["user_value"])

    def effective_value(self, schema: str, key: str) -> KeyValue:
        entry = self._probe(schema, (key,))["values"][key]
        return KeyValue(entry["effective"], entry["type"], entry["value"])

    def canonical(self, value_type: str, value: Any) -> str:
        return self._glib().Variant(value_type, value).print_(False)

    def set(self, schema: str, key: str, text: str) -> None:
        self._run(["gsettings", "set", schema, key, text], DEFAULT_TIMEOUTS["gsettings"])

    def reset(self, schema: str, key: str) -> None:
        self._run(["gsettings", "reset", schema, key], DEFAULT_TIMEOUTS["gsettings"])


# ----------------------------------------------------------------- the adapter

def _split(location: str) -> tuple[str, str]:
    if location not in LOCATIONS:
        raise ValueError(f"not a GTK adapter location: {location!r}")
    schema, key = location.split(" ")
    return schema, key


class GtkAdapter:
    """K3 Adapter for GTK/GNOME applications (org.gnome.desktop.interface)."""

    name = "gtk"
    requires_restart = True  # fonts: yes; text scaling applies live in GTK, but fonts need a relaunch
    must_be_closed = False
    TIMEOUTS: dict[str, float] = {"gsettings": DEFAULT_TIMEOUTS["gsettings"]}

    def __init__(self, store: SettingsStore | None = None, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.store: SettingsStore = store if store is not None else GioSettingsStore()
        self._snapshot_sha: dict[str, str] = {}
        self._clock = clock
        self._sleep = sleep

    def _target(self) -> Target:
        return Target(self.name, TARGET_ID, "GTK and GNOME apps", running=False)

    def _check_target(self, target: Target) -> None:
        if target.adapter != self.name or target.target_id != TARGET_ID:
            raise ValueError("target does not belong to the GTK adapter")

    def _require_schema(self) -> None:
        if not self.store.schema_exists(SCHEMA):
            raise ScribeSenseError(ErrorCode.SCHEMA_MISSING)

    # ---- K3

    def detect(self) -> list[Target]:
        return [self._target()]  # read-only; a missing schema surfaces as SCHEMA_MISSING -> NOT_COVERED

    def read_state(self, location: str) -> State:
        schema, key = _split(location)
        self._require_schema()
        kv = self.store.user_value(schema, key)
        if kv is None:
            return State("key", False, None, TYPES[key], state_sha256(False, None))
        data = kv.text.encode()
        return State("key", True, data, kv.value_type, state_sha256(True, data))

    def snapshot(self, target: Target) -> list[SnapshotItem]:
        self._check_target(target)
        items = []
        for location in LOCATIONS:
            st = self.read_state(location)
            self._snapshot_sha[location] = st.sha256
            items.append(SnapshotItem(
                target=target, kind="key", location=location, existed=st.present,
                old_value=st.data.decode() if st.data is not None else None,
                old_sha256=st.sha256, file_mode=None, value_type=st.value_type,
            ))
        return items

    def plan(self, target: Target, fonts: FontSet, config: Configuration) -> list[PlannedWrite]:
        self._check_target(target)
        if not fonts.ui_family.startswith(UI_FAMILY_PREFIX) or not fonts.reading_family.startswith(READING_FAMILY_PREFIX):
            raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR)  # never point at an arbitrary family
        self._require_schema()
        values = {
            FONT_NAME: f"{fonts.ui_family} {format_size(self._current_size(FONT_NAME))}",
            DOCUMENT_FONT_NAME: f"{fonts.reading_family} {format_size(self._current_size(DOCUMENT_FONT_NAME))}",
            TEXT_SCALE: float(config.text_scale),
        }
        planned = []
        for key in KEYS:
            text = self.store.canonical(TYPES[key], values[key])
            planned.append(PlannedWrite(target, f"{SCHEMA} {key}", text, state_sha256(True, text.encode())))
        return planned

    def _current_size(self, key: str) -> float:
        try:
            value = self.store.effective_value(SCHEMA, key).value
        except ScribeSenseError:
            return DEFAULT_SIZE
        return size_of(value) if isinstance(value, str) else DEFAULT_SIZE

    def apply(self, planned: PlannedWrite) -> AppliedItem:
        schema, key = _split(planned.location)
        expected = self._snapshot_sha.get(planned.location)
        if expected is None:
            raise ValueError("apply() for a location that snapshot() did not return (K3 rule 2)")
        if self.read_state(planned.location).sha256 != expected:
            raise ScribeSenseError(ErrorCode.CHANGED_DURING_APPLY)  # K3 rule 12
        text = planned.payload if isinstance(planned.payload, str) else planned.payload.decode()
        self._write(lambda: self.store.set(schema, key, text))
        self._read_back(planned.location, planned.new_sha256)
        return AppliedItem(planned.target, planned.location, planned.new_sha256)

    def verify(self, target: Target, fonts: FontSet) -> VerifyResult:
        """Configuration check (A14): effective font-name / document-font-name name our families."""
        self._check_target(target)
        try:
            ui = self.store.effective_value(SCHEMA, FONT_NAME).value
            doc = self.store.effective_value(SCHEMA, DOCUMENT_FONT_NAME).value
        except (ScribeSenseError, OSError):
            return VerifyResult(target, False, None, ErrorCode.CHECK_UNAVAILABLE.value)
        ok = (isinstance(ui, str) and family_of(ui) == fonts.ui_family
              and isinstance(doc, str) and family_of(doc) == fonts.reading_family)
        return VerifyResult(target, ok, None, "OK" if ok else ErrorCode.ADAPTER_VERIFY_FAILED.value)

    def revert(self, item: SnapshotItem, backup: bytes | None) -> None:
        """K3 rule 7: write the old value back, or UNSET a key that was unset (never write a default)."""
        if item.kind != "key":
            raise ValueError("the GTK adapter owns settings keys only")
        schema, key = _split(item.location)
        if item.existed:
            if item.old_value is None:
                raise ScribeSenseError(ErrorCode.BACKUP_MISSING)
            text = item.old_value
            self._write(lambda: self.store.set(schema, key, text))
        else:
            self._write(lambda: self.store.reset(schema, key))
        self._read_back(item.location, item.old_sha256)

    # ---- helpers

    def _write(self, action: Callable[[], None]) -> None:
        try:
            action()
        except ScribeSenseError:
            raise
        except OSError:
            raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR) from None

    def _read_back(self, location: str, expected_sha: str) -> None:
        """The settings service may commit a moment after gsettings returns: poll briefly."""
        deadline = self._clock() + READ_BACK_TIMEOUT_S
        while True:
            if self.read_state(location).sha256 == expected_sha:
                return
            if self._clock() >= deadline:
                log(ErrorCode.ADAPTER_COMMAND_FAILED, "adapter", adapter=self.name, target=TARGET_ID,
                    step="read-back", setting=location.split(" ")[1])
                raise ScribeSenseError(ErrorCode.ADAPTER_COMMAND_FAILED)
            self._sleep(READ_BACK_POLL_S)
