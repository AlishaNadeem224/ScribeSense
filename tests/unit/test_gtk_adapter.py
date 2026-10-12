"""M4.3 — GTK adapter. All settings go through a fake store; real Gio is used only with the in-memory
backend (GSETTINGS_BACKEND=memory from the sandbox) and only to read — never against the user's session."""

from __future__ import annotations

import builtins
from dataclasses import dataclass, field
from typing import Any

import pytest

from scribesense.adapters import gtk
from scribesense.adapters.conformance import AdapterConformance
from scribesense.adapters.gtk import (
    DOCUMENT_FONT_NAME,
    FONT_NAME,
    LOCATIONS,
    SCHEMA,
    TEXT_SCALE,
    GioSettingsStore,
    GtkAdapter,
    KeyValue,
    format_double,
    format_string,
)
from scribesense.contracts.adapter import state_sha256
from scribesense.contracts.config import RANGES, Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import FontSet

FONTS = FontSet("abc123", "AccessSans-abc123", "AccessSansUI-abc123", (), ("Regular",))
CFG = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.15)
MONO = "monospace-font-name"
DEFAULTS = {FONT_NAME: "Adwaita Sans 11", DOCUMENT_FONT_NAME: "Adwaita Sans 11",
            TEXT_SCALE: 1.0, MONO: "Adwaita Mono 11"}
TYPES = {FONT_NAME: "s", DOCUMENT_FONT_NAME: "s", TEXT_SCALE: "d", MONO: "s"}


def _canonical(value_type: str, value: Any) -> str:
    return format_double(float(value)) if value_type == "d" else format_string(str(value))


def _parse(value_type: str, text: str) -> Any:
    if value_type == "d":
        return float(text)
    assert text[0] in "'\"" and text[-1] == text[0]
    return text[1:-1].replace("\\'", "'").replace("\\\\", "\\")


@dataclass
class FakeStore:
    """In-memory org.gnome.desktop.interface. user = keys explicitly set; DEFAULTS otherwise."""

    user: dict[str, Any] = field(default_factory=dict)
    schema_present: bool = True
    calls: list[tuple[str, ...]] = field(default_factory=list)
    fail_with: BaseException | None = None
    lag_reads: int = 0  # simulate the settings service committing a moment after gsettings returns
    ignore_writes: bool = False
    _pending: list[tuple[str, Any]] = field(default_factory=list)

    def schema_exists(self, schema: str) -> bool:
        return self.schema_present and schema == SCHEMA

    def _settle(self) -> None:
        if self._pending and self.lag_reads <= 0:
            for key, value in self._pending:
                if value is None:
                    self.user.pop(key, None)
                else:
                    self.user[key] = value
            self._pending.clear()
        self.lag_reads -= 1

    def user_value(self, schema: str, key: str) -> KeyValue | None:
        self._settle()
        if key not in self.user:
            return None
        return KeyValue(_canonical(TYPES[key], self.user[key]), TYPES[key], self.user[key])

    def effective_value(self, schema: str, key: str) -> KeyValue:
        value = self.user.get(key, DEFAULTS[key])
        return KeyValue(_canonical(TYPES[key], value), TYPES[key], value)

    def canonical(self, value_type: str, value: Any) -> str:
        return _canonical(value_type, value)

    def _write(self, key: str, value: Any) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        if self.ignore_writes:
            return
        self._pending.append((key, value))
        if self.lag_reads <= 0:
            self._settle()

    def set(self, schema: str, key: str, text: str) -> None:
        self.calls.append(("set", key, text))
        self._write(key, _parse(TYPES[key], text))

    def reset(self, schema: str, key: str) -> None:
        self.calls.append(("reset", key))
        self._write(key, None)


START_STATES = {
    "all-unset": {},
    "all-set": {FONT_NAME: "Fira Sans Semi-Bold 11", DOCUMENT_FONT_NAME: "Cantarell 12", TEXT_SCALE: 1.1,
                MONO: "Fira Code 10"},
    "mixed": {FONT_NAME: "Inter 10.5", MONO: "JetBrains Mono 11"},
}


@dataclass
class GtkHarness:
    store: FakeStore
    adapter: GtkAdapter
    fonts: FontSet = FONTS
    config: Configuration = CFG

    def fingerprint(self) -> dict[str, str]:
        out = {}
        for key in (FONT_NAME, DOCUMENT_FONT_NAME, TEXT_SCALE, MONO):
            kv = self.store.user_value(SCHEMA, key)
            out[f"{SCHEMA} {key}"] = (state_sha256(False, None) if kv is None
                                      else state_sha256(True, kv.text.encode()))
        return out

    def external_edit(self, location: str) -> None:
        key = location.split(" ")[1]
        self.store.user[key] = 1.75 if key == TEXT_SCALE else "Comic Neue 14"


def make(start: str = "all-set", **store_kw: Any) -> GtkHarness:
    store = FakeStore(user=dict(START_STATES[start]), **store_kw)
    return GtkHarness(store, GtkAdapter(store, sleep=lambda _s: None))


class TestGtkConformance(AdapterConformance):
    @pytest.fixture(params=sorted(START_STATES))
    def conformance_harness(self, request: pytest.FixtureRequest) -> GtkHarness:
        return make(request.param)


# ------------------------------------------------------------------ plan contents

def _plan(h: GtkHarness) -> dict[str, str]:
    t = h.adapter.detect()[0]
    h.adapter.snapshot(t)
    return {p.location.split(" ")[1]: p.payload for p in h.adapter.plan(t, FONTS, CFG)}


def test_plan_writes_ui_reading_and_scale_and_keeps_sizes() -> None:
    planned = _plan(make("all-set"))
    assert planned[FONT_NAME] == "'AccessSansUI-abc123 11'"
    assert planned[DOCUMENT_FONT_NAME] == "'AccessSans-abc123 12'"
    assert planned[TEXT_SCALE] == format_double(1.15)


def test_fractional_and_default_sizes() -> None:
    planned = _plan(make("mixed"))  # font-name "Inter 10.5", document-font-name default "Adwaita Sans 11"
    assert planned[FONT_NAME] == "'AccessSansUI-abc123 10.5'"
    assert planned[DOCUMENT_FONT_NAME] == "'AccessSans-abc123 11'"


@pytest.mark.parametrize("desc, size", [("Fira Sans Semi-Bold 11", 11.0), ("Inter 10.5", 10.5), ("Cantarell", 11.0),
                                        ("Weird 0", 11.0), ("Huge 999", 11.0), ("Pixel 12px", 12.0), ("", 11.0)])
def test_size_parsing(desc: str, size: float) -> None:
    assert gtk.size_of(desc) == size


def test_monospace_is_never_planned_or_touched() -> None:
    h = make("all-set")
    assert all("monospace" not in loc for loc in LOCATIONS)
    planned = _plan(h)
    assert MONO not in planned
    for p in h.adapter.plan(h.adapter.detect()[0], FONTS, CFG):
        h.adapter.apply(p)
    assert h.store.user[MONO] == "Fira Code 10"
    assert all(call[1] != MONO for call in h.store.calls)


def test_family_prefix_is_validated() -> None:
    h = make()
    t = h.adapter.detect()[0]
    h.adapter.snapshot(t)
    with pytest.raises(ScribeSenseError) as exc:
        h.adapter.plan(t, FontSet("k", "Comic Sans", "AccessSansUI-k", (), ()), CFG)
    assert exc.value.code is ErrorCode.UNEXPECTED_ADAPTER_ERROR


# ------------------------------------------------------------------ canonical text = what GLib prints

def test_double_formatting_rules() -> None:
    assert format_double(1.0) == "1.0"
    assert format_double(1.25) == "1.25"
    assert format_double(1.1) == "1.1000000000000001"
    with pytest.raises(ScribeSenseError):
        format_double(float("nan"))


def _glib() -> Any:
    gi = pytest.importorskip("gi")
    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    return GLib


def test_double_formatting_matches_real_glib_for_every_text_scale_step() -> None:
    GLib = _glib()
    r = RANGES["text_scale"]
    steps = [round(r.min + i * r.step, 2) for i in range(int(round((r.max - r.min) / r.step)) + 1)]
    for v in steps:
        assert format_double(v) == GLib.Variant("d", v).print_(False), v


@pytest.mark.parametrize("value", ["AccessSansUI-abc 11", "it's", 'say "hi"', "both ' and \"", "back\\slash"])
def test_string_formatting_matches_real_glib(value: str) -> None:
    GLib = _glib()
    assert format_string(value) == GLib.Variant("s", value).print_(False)


# ------------------------------------------------------------------ apply / revert behaviour

def test_revert_unsets_keys_that_were_unset() -> None:
    h = make("all-unset")
    t = h.adapter.detect()[0]
    snaps = h.adapter.snapshot(t)
    for p in h.adapter.plan(t, FONTS, CFG):
        h.adapter.apply(p)
    h.store.calls.clear()
    for s in snaps:
        h.adapter.revert(s, None)
    assert h.store.user == {}
    assert {c[0] for c in h.store.calls} == {"reset"}  # never "set back to the default"


def test_revert_writes_old_text_for_keys_that_were_set() -> None:
    h = make("all-set")
    before = dict(h.store.user)
    t = h.adapter.detect()[0]
    snaps = h.adapter.snapshot(t)
    for p in h.adapter.plan(t, FONTS, CFG):
        h.adapter.apply(p)
    for s in snaps:
        h.adapter.revert(s, None)
    assert h.store.user == before


def test_read_back_waits_for_a_lagging_settings_service() -> None:
    h = make("all-set", lag_reads=3)
    t = h.adapter.detect()[0]
    h.store.lag_reads = 0
    h.adapter.snapshot(t)
    plan = h.adapter.plan(t, FONTS, CFG)
    h.store.lag_reads = 3
    h.adapter.apply(plan[0])  # succeeds after a few polls
    assert h.store.user_value(SCHEMA, FONT_NAME).text == plan[0].payload


def test_write_that_never_lands_is_command_failed() -> None:
    h = make("all-set", ignore_writes=True)
    ticks = iter(range(1000))
    h.adapter._clock = lambda: next(ticks) * 0.5  # deadline passes after a couple of polls
    t = h.adapter.detect()[0]
    h.adapter.snapshot(t)
    with pytest.raises(ScribeSenseError) as exc:
        h.adapter.apply(h.adapter.plan(t, FONTS, CFG)[0])
    assert exc.value.code is ErrorCode.ADAPTER_COMMAND_FAILED


@pytest.mark.parametrize("error, code", [
    (ScribeSenseError(ErrorCode.ADAPTER_TIMEOUT), ErrorCode.ADAPTER_TIMEOUT),
    (PermissionError("no exec"), ErrorCode.UNEXPECTED_ADAPTER_ERROR),
])
def test_write_errors_are_scribesense_errors(error: BaseException, code: ErrorCode) -> None:
    h = make("all-set", fail_with=error)
    t = h.adapter.detect()[0]
    h.adapter.snapshot(t)
    with pytest.raises(ScribeSenseError) as exc:
        h.adapter.apply(h.adapter.plan(t, FONTS, CFG)[0])
    assert exc.value.code is code


def test_missing_schema_is_schema_missing_but_detect_still_reports_the_target() -> None:
    h = make(schema_present=False)
    t = h.adapter.detect()[0]
    assert t.target_id == "gtk:user"
    with pytest.raises(ScribeSenseError) as exc:
        h.adapter.snapshot(t)
    assert exc.value.code is ErrorCode.SCHEMA_MISSING


def test_unknown_locations_are_refused() -> None:
    h = make()
    with pytest.raises(ValueError):
        h.adapter.read_state(f"{SCHEMA} {MONO}")


# ------------------------------------------------------------------ verify

def test_verify_ok_and_mismatch() -> None:
    h = make("all-set")
    t = h.adapter.detect()[0]
    h.adapter.snapshot(t)
    assert h.adapter.verify(t, FONTS).chosen_ok is False
    for p in h.adapter.plan(t, FONTS, CFG):
        h.adapter.apply(p)
    ok = h.adapter.verify(t, FONTS)
    assert (ok.chosen_ok, ok.drawn_ok, ok.detail_code) == (True, None, "OK")
    other = FontSet("zzz", "AccessSans-zzz", "AccessSansUI-zzz", (), ())
    bad = h.adapter.verify(t, other)
    assert (bad.chosen_ok, bad.detail_code) == (False, ErrorCode.ADAPTER_VERIFY_FAILED.value)


def test_verify_unavailable_when_settings_cannot_be_read() -> None:
    h = make()
    t = h.adapter.detect()[0]
    h.store.schema_present = False

    def boom(schema: str, key: str) -> KeyValue:
        raise ScribeSenseError(ErrorCode.SCHEMA_MISSING)

    h.store.effective_value = boom  # type: ignore[method-assign]
    r = h.adapter.verify(t, FONTS)
    assert (r.chosen_ok, r.detail_code) == (False, ErrorCode.CHECK_UNAVAILABLE.value)


# ------------------------------------------------------------------ the production store

def test_gio_store_without_pygobject_is_adapter_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def no_gi(name: str, *a: Any, **kw: Any) -> Any:
        if name == "gi" or name.startswith("gi."):
            raise ImportError("no gi")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_gi)
    with pytest.raises(ScribeSenseError) as exc:
        GioSettingsStore().schema_exists(SCHEMA)
    assert exc.value.code is ErrorCode.ADAPTER_UNAVAILABLE


def test_gio_store_writes_through_gsettings_commands_only() -> None:
    calls: list[list[str]] = []
    store = GioSettingsStore(runner=lambda argv, timeout: calls.append(argv))
    store.set(SCHEMA, TEXT_SCALE, "1.25")
    store.reset(SCHEMA, FONT_NAME)
    assert calls == [["gsettings", "set", SCHEMA, TEXT_SCALE, "1.25"], ["gsettings", "reset", SCHEMA, FONT_NAME]]


def test_gio_store_reads_user_values_from_the_memory_backend() -> None:
    """Real Gio, in-memory backend (sandbox GSETTINGS_BACKEND=memory): unset -> None; set -> exact text."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    store = GioSettingsStore(runner=lambda *_: None)
    if not store.schema_exists(SCHEMA):
        pytest.skip("org.gnome.desktop.interface schema not installed")
    assert store.user_value(SCHEMA, TEXT_SCALE) is None
    Gio.Settings.new(SCHEMA).set_value(TEXT_SCALE, GLib.Variant("d", 1.15))
    kv = store.user_value(SCHEMA, TEXT_SCALE)
    assert kv is not None and kv.text == format_double(1.15) and kv.value_type == "d"
    Gio.Settings.new(SCHEMA).reset(TEXT_SCALE)
    assert store.user_value(SCHEMA, TEXT_SCALE) is None


@pytest.mark.integration
def test_real_gsettings_apply_and_revert_in_the_sandbox() -> None:
    """Inside the bubblewrap runner (GSETTINGS_BACKEND=keyfile): real `gsettings` writes, Gio reads back,
    revert restores set and unset keys exactly. Skipped everywhere else."""
    adapter = GtkAdapter()
    t = adapter.detect()[0]
    snaps = adapter.snapshot(t)
    for p in adapter.plan(t, FONTS, CFG):
        adapter.apply(p)
    assert adapter.verify(t, FONTS).chosen_ok
    for s in snaps:
        adapter.revert(s, None)
    assert [adapter.read_state(s.location).sha256 for s in snaps] == [s.old_sha256 for s in snaps]
