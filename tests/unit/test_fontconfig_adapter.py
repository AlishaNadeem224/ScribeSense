"""M4.2 — fontconfig adapter: conformance (M4.1) + card acceptance tests, in the unit sandbox.

No real process is launched: fc-match / fc-cache / the Pango probe go through a fake runner, the UI font
is injected.
"""

from __future__ import annotations

import builtins
import errno
import json
import os
import re
import stat
import sys
import types
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scribesense.adapters import fontconfig as fc
from scribesense.adapters import verify as vf
from scribesense.adapters.conformance import AdapterConformance
from scribesense.adapters.fontconfig import FontconfigAdapter, rule_path
from scribesense.contracts.adapter import Adapter, state_sha256
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import FontSet
from scribesense.contracts.paths import run_cmd

CFG = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.0)
FONTS = FontSet("abc123", "AccessSans-abc123", "AccessSansUI-abc123", (), ("Regular",))
UI_FONTS = ["Fira Sans Semi-Bold 11"]


# ---------------------------------------------------------------- fakes

@dataclass
class FakeProc:
    stdout: bytes
    stderr: bytes = b""
    returncode: int = 0


PROBE = "pango-probe"  # command name the fake gives `python -P -m scribesense.adapters.pango_probe ...`


def command_name(argv: list[str]) -> str:
    if argv[:4] == [sys.executable, "-P", "-m", vf.PROBE_MODULE]:
        return PROBE
    return argv[0]


@dataclass
class FakeRunner:
    """Answers fc-match from `answers` (pattern -> family) and the Pango probe from `drawn` (families for
    the query; default: the family fc-match would choose, i.e. every run drawn in it). Records every call;
    can fail per command (keys: "fc-match", "fc-cache", PROBE)."""

    answers: dict[str, str] = field(default_factory=dict)
    default: str = "DejaVu Sans"
    drawn: list[str] | None = None
    fail: dict[str, ErrorCode] = field(default_factory=dict)  # command -> error to raise
    os_fail: dict[str, OSError] = field(default_factory=dict)  # command -> launch OSError (as run_cmd lets through)
    calls: list[tuple[list[str], float]] = field(default_factory=list)

    def __call__(self, argv: list[str], timeout: float) -> FakeProc:
        self.calls.append((list(argv), timeout))
        name = command_name(argv)
        code = self.fail.get(name)
        if code is not None:
            raise ScribeSenseError(code)
        os_exc = self.os_fail.get(name)
        if os_exc is not None:
            raise os_exc
        if name == "fc-match":
            family = re.sub(r"\\(.)", r"\1", argv[-1])  # undo fontconfig pattern escaping
            return FakeProc(self.answers.get(family, self.default).encode())
        if name == PROBE:
            query, sample = argv[-2:]
            assert sample == vf.SAMPLE  # the probe is never given anything but the fixed sample
            families = self.drawn if self.drawn is not None else [self.answers.get(query, self.default)]
            return FakeProc(json.dumps({"v": 1, "families": families}).encode())
        return FakeProc(b"")

    def commands(self) -> list[str]:
        return [command_name(argv) for argv, _ in self.calls]


def good_runner(**kw: object) -> FakeRunner:
    return FakeRunner(
        answers={"sans-serif": FONTS.reading_family, "serif": FONTS.reading_family, "Fira Sans": FONTS.ui_family},
        **kw,  # type: ignore[arg-type]
    )


def make_adapter(runner: FakeRunner | None = None, ui: list[str] | None = None) -> FontconfigAdapter:
    ui_fonts = list(UI_FONTS if ui is None else ui)
    return FontconfigAdapter(runner=runner or good_runner(), ui_font_source=lambda: list(ui_fonts))


def target(adapter: FontconfigAdapter):  # type: ignore[no-untyped-def]
    return adapter.detect()[0]


def snap_plan_apply(adapter: FontconfigAdapter):  # type: ignore[no-untyped-def]
    t = target(adapter)
    items = adapter.snapshot(t)
    planned = adapter.plan(t, FONTS, CFG)
    for p in planned:
        adapter.apply(p)
    return t, items, planned


# ---------------------------------------------------------------- conformance

START_STATES = {
    "absent": None,
    "present-other-0600": (b'<?xml version="1.0"?>\n<fontconfig><!-- the user\'s own --></fontconfig>\n', 0o600),
    "present-empty": (b"", 0o644),
}


@dataclass
class FontconfigHarness:
    adapter: FontconfigAdapter
    home: Path
    fonts: FontSet = FONTS
    config: Configuration = CFG

    def fingerprint(self) -> dict[str, str]:
        """Every file under the sandbox HOME (so a write anywhere else is caught), with its mode."""
        out: dict[str, str] = {}
        for dirpath, _dirs, files in os.walk(self.home):
            for name in files:
                p = Path(dirpath) / name
                mode = stat.S_IMODE(p.stat().st_mode)
                out[str(p)] = f"{state_sha256(True, p.read_bytes())}:{mode:o}"
        if str(rule_path()) not in out:
            out[str(rule_path())] = state_sha256(False, None)
        return out

    def external_edit(self, location: str) -> None:
        path = Path(location)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"<fontconfig><!-- changed by someone else --></fontconfig>\n")


def _setup(start: str) -> None:
    spec = START_STATES[start]
    if spec is None:
        return
    data, mode = spec
    path = rule_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


class TestFontconfigConformance(AdapterConformance):
    @pytest.fixture(params=sorted(START_STATES))
    def conformance_harness(self, request: pytest.FixtureRequest, sandbox) -> FontconfigHarness:  # type: ignore[no-untyped-def]
        sandbox.fonts_dir.mkdir(parents=True, exist_ok=True)  # so apply/revert run fc-cache (faked)
        _setup(request.param)
        return FontconfigHarness(make_adapter(), sandbox.home)


# ---------------------------------------------------------------- basics

def test_attributes_and_single_target() -> None:
    a = make_adapter()
    _typed: Adapter = a  # static K3 conformance (Adapter is not runtime_checkable)
    assert (a.name, a.requires_restart, a.must_be_closed) == ("fontconfig", True, False)
    assert a.detect() == [fc.Target("fontconfig", "fontconfig:user", "Font rules (fontconfig)", False)]
    assert fc.TIMEOUTS["fc-match"] == 10.0 and fc.TIMEOUTS["fc-cache"] == 60.0
    assert fc.TIMEOUTS["gsettings"] == fc.DEFAULT_TIMEOUTS["gsettings"]


def test_rule_path_follows_config_home(sandbox, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    assert rule_path() == sandbox.config_home / "fontconfig" / "conf.d" / "99-scribesense.conf"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(sandbox.root / "other"))
    assert rule_path() == sandbox.root / "other" / "fontconfig" / "conf.d" / "99-scribesense.conf"


def test_snapshot_item_for_absent_and_present(sandbox) -> None:  # type: ignore[no-untyped-def]
    a = make_adapter()
    [item] = a.snapshot(target(a))
    assert (item.existed, item.file_mode, item.old_value) == (False, None, None)
    assert item.old_sha256 == state_sha256(False, None)
    _setup("present-other-0600")
    [item] = a.snapshot(target(a))
    assert (item.existed, item.file_mode) == (True, 0o600)


def test_read_state_refuses_foreign_locations(sandbox) -> None:  # type: ignore[no-untyped-def]
    a = make_adapter()
    for bad in ("relative/fontconfig/conf.d/99-scribesense.conf", str(sandbox.home / ".bashrc"),
                str(sandbox.config_home / "fontconfig" / "conf.d" / "10-other.conf")):
        with pytest.raises(ValueError):
            a.read_state(bad)


def test_plan_rejects_a_foreign_target() -> None:
    a = make_adapter()
    with pytest.raises(ValueError):
        a.plan(fc.Target("gtk", "gtk:default", "GTK", False), FONTS, CFG)


# ---------------------------------------------------------------- the generated rules

def _rules(payload: bytes) -> list[tuple[str | None, str | None]]:
    """(tested family, generated family) per <match>, checking every rule has the agreed shape."""
    root = ET.fromstring(payload)
    assert root.tag == "fontconfig"
    out = []
    for m in root.findall("match"):
        test, edit = m.find("test"), m.find("edit")
        assert m.get("target") == "pattern"
        assert test is not None and edit is not None
        assert (test.get("name"), test.get("qual")) == ("family", "any")
        assert (edit.get("name"), edit.get("mode"), edit.get("binding")) == ("family", "prepend", "strong")
        out.append((test.findtext("string"), edit.findtext("string")))
    return out


def plan_payload(ui: list[str]) -> bytes:
    a = make_adapter(ui=ui)
    [p] = a.plan(target(a), FONTS, CFG)
    assert isinstance(p.payload, bytes)
    assert p.new_sha256 == state_sha256(True, p.payload)
    assert p.location == str(rule_path())
    return p.payload


def test_generated_xml_parses_and_has_exactly_the_expected_rules() -> None:
    payload = plan_payload(["Fira Sans Semi-Bold 11"])
    assert payload.startswith(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">')
    assert b"Generated by ScribeSense" in payload and b"scribesense reset" in payload
    assert _rules(payload) == [
        ("sans-serif", FONTS.reading_family),
        ("serif", FONTS.reading_family),
        ("Fira Sans", FONTS.ui_family),
    ]


def test_without_ui_fonts_only_generic_rules() -> None:
    assert _rules(plan_payload([])) == [("sans-serif", FONTS.reading_family), ("serif", FONTS.reading_family)]


def test_monospace_icon_emoji_and_own_families_are_never_redirected() -> None:
    ui = ["monospace 11", "Source Code Pro 10", "JetBrains Mono Bold 10", "Font Awesome 6 Free 10",
          "Noto Color Emoji 10", "Symbols Nerd Font 10", "Material Icons 12", "emoji", "math",
          "AccessSansUI-abc123 11", "accesssans-old 11", "sans-serif 11", "Serif 11", "Terminus Console 12"]
    rules = _rules(plan_payload(ui))
    assert rules == [("sans-serif", FONTS.reading_family), ("serif", FONTS.reading_family)]
    tested = {t.lower() for t, _ in rules}
    assert not tested & {"monospace", "emoji", "math"}


#: One family per MONO_ICON_HINTS entry, each matching ONLY that hint, so dropping any hint is caught.
HINT_EXAMPLES = {
    "mono": "Ubuntu Mono", "code": "Source Code Pro", "nerd": "Hack Nerd Font", "icon": "Material Icons",
    "symbol": "Symbola", "awesome": "Font Awesome 6 Free", "emoji": "Noto Color Emoji",
    "console": "Lucida Console", "terminal": "Terminal Sans",
}


def test_every_hint_has_an_example() -> None:
    assert set(HINT_EXAMPLES) == set(fc.MONO_ICON_HINTS)


@pytest.mark.parametrize(("hint", "family"), sorted(HINT_EXAMPLES.items()))
def test_each_mono_icon_hint_alone_prevents_a_redirect(hint: str, family: str) -> None:
    assert [h for h in fc.MONO_ICON_HINTS if h in family.casefold()] == [hint]
    assert _rules(plan_payload([family + " 11"])) == [
        ("sans-serif", FONTS.reading_family), ("serif", FONTS.reading_family)]


@pytest.mark.parametrize(
    "font_name",
    ["Sans 10", "Sans Serif 10", "sans serif 10", "SansSerif 9", "SANS 10", "Mono 10", "System UI 11",
     "system-ui 11", "SystemUI 11", "Monospace Bold 10", "Math 10", "Emoji 10", "Serif Italic 10"],
)
def test_fontconfig_generic_aliases_never_get_a_ui_rule(font_name: str) -> None:
    """fonts.conf rewrites these to their generic before conf.d runs: a UI rule could never fire (A13)."""
    assert fc.ui_families([font_name]) == []
    assert _rules(plan_payload([font_name])) == [
        ("sans-serif", FONTS.reading_family), ("serif", FONTS.reading_family)]


def test_alias_lookalikes_are_still_ui_families() -> None:
    assert fc.ui_families(["Sansation 10", "Open Sans 10", "System Sans 10"]) == [
        "Sansation", "Open Sans", "System Sans"]


def test_ui_families_are_deduplicated_in_order() -> None:
    ui = ["Fira Sans 11", "fira sans Bold 12", "Cantarell 11", "Fira Sans Italic 10"]
    assert [t for t, _ in _rules(plan_payload(ui))][2:] == ["Fira Sans", "Cantarell"]


def test_family_names_are_xml_escaped() -> None:
    nasty = 'Evil</string></test><edit>& "x" \'y\''
    payload = plan_payload([nasty + " 11"])
    rules = _rules(payload)  # still parses
    assert rules[-1] == (nasty, FONTS.ui_family)
    assert b"</string></test><edit>&" not in payload


def test_names_with_control_characters_are_skipped() -> None:
    assert len(_rules(plan_payload(["Bad\x01Font 11"]))) == 2


@pytest.mark.parametrize("bad", ["Foo\ufffeBar", "Foo\uffffBar", "Foo\ud800Bar", "Foo\udfffBar", "Foo\x7fBar"])
def test_names_xml_cannot_carry_are_skipped_and_the_file_stays_well_formed(bad: str) -> None:
    payload = plan_payload([bad + " 10", "Fira Sans 11"])  # no UnicodeEncodeError, no broken XML
    assert _rules(payload) == [
        ("sans-serif", FONTS.reading_family), ("serif", FONTS.reading_family), ("Fira Sans", FONTS.ui_family)]


@pytest.mark.parametrize("bad", ["Foo\u200bBar", "Foo\u00adBar", "Foo\u200eBar", "X" * 201, "X" * 250])
def test_names_verify_cannot_query_get_no_rule(bad: str) -> None:
    """Writer and verifier accept the same names: a family verify would refuse never gets a rule."""
    assert not vf.valid_query(bad)
    assert fc.ui_families([bad + " 10", "Fira Sans 11"]) == ["Fira Sans"]
    assert fc.ui_families([], carried=[bad]) == []  # not carried forward from an older rule file either
    assert _rules(plan_payload([bad + " 10", "Fira Sans 11"])) == [
        ("sans-serif", FONTS.reading_family), ("serif", FONTS.reading_family), ("Fira Sans", FONTS.ui_family)]


def test_longest_queryable_name_still_gets_a_rule() -> None:
    longest = "X" * vf.MAX_QUERY_LEN
    assert fc.ui_families([longest + " 10"]) == [longest]


@pytest.mark.parametrize("bad", ["\ufffe", "\ud800", "\t"])
def test_generated_families_xml_cannot_carry_are_refused(bad: str) -> None:
    for fonts in (FontSet("k", "AccessSans-k" + bad, "AccessSansUI-k", (), ()),
                  FontSet("k", "AccessSans-k", "AccessSansUI-k" + bad, (), ())):
        with pytest.raises(ScribeSenseError) as exc:
            fc.render_rules(fonts, [])
        assert exc.value.code is ErrorCode.UNEXPECTED_ADAPTER_ERROR


def test_payload_is_deterministic() -> None:
    assert plan_payload(UI_FONTS) == plan_payload(UI_FONTS)


@pytest.mark.parametrize(
    "fonts",
    [
        FontSet("k", "DejaVu Sans", "AccessSansUI-k", (), ()),
        FontSet("k", "AccessSans-k", "Cantarell", (), ()),
        FontSet("k", "AccessSans-", "AccessSansUI-k", (), ()),
        FontSet("k", "AccessSansUI-k", "AccessSans-k", (), ()),  # swapped roles
    ],
)
def test_plan_refuses_families_without_k7_prefixes(fonts: FontSet) -> None:
    a = make_adapter()
    with pytest.raises(ScribeSenseError) as exc:
        a.plan(target(a), fonts, CFG)
    assert exc.value.code is ErrorCode.UNEXPECTED_ADAPTER_ERROR


def test_plan_survives_a_failing_ui_font_source() -> None:
    def broken() -> list[str]:
        raise RuntimeError("boom")

    a = FontconfigAdapter(runner=good_runner(), ui_font_source=broken)
    [p] = a.plan(target(a), FONTS, CFG)
    assert len(_rules(p.payload)) == 2  # type: ignore[arg-type]


# ---------------------------------------------------------------- UI font parsing

@pytest.mark.parametrize(
    ("font_name", "families"),
    [
        ("Fira Sans Semi-Bold 11", ["Fira Sans"]),
        ("Cantarell 11", ["Cantarell"]),
        ("Noto Sans Bold Italic 10", ["Noto Sans"]),
        ("Adwaita Sans 11", ["Adwaita Sans"]),
        ("Inter SemiBold Condensed 10.5", ["Inter"]),
        ("Times New Roman, 12", ["Times New Roman"]),
        ("Cantarell, Noto Sans 11", ["Cantarell", "Noto Sans"]),
        ("Ubuntu 11 @wght=400", ["Ubuntu"]),
        ("Ubuntu", ["Ubuntu"]),
        ("Bold 11", []),
        ("", []),
        ("   ", []),
    ],
)
def test_parse_font_family(font_name: str, families: list[str]) -> None:
    assert fc.parse_font_family(font_name) == families


def test_default_ui_font_reader_without_gi_returns_empty(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setitem(sys.modules, "gi", None)
    monkeypatch.setitem(sys.modules, "gi.repository", None)
    real_import = builtins.__import__

    def no_gi(name, *a, **kw):  # type: ignore[no-untyped-def]
        if name == "gi" or name.startswith("gi."):
            raise ImportError(name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_gi)
    assert fc.read_gsettings_ui_fonts() == []


def _fake_gi(monkeypatch, lookup_result, value: object = "Fira Sans 11") -> None:  # type: ignore[no-untyped-def]
    class Variant:
        def unpack(self) -> object:
            return value

    class Settings:
        @staticmethod
        def new(schema: str) -> Settings:
            assert schema == fc.GSETTINGS_SCHEMA
            return Settings()

        def get_value(self, key: str) -> Variant:
            assert key == fc.GSETTINGS_KEY
            return Variant()

    class Source:
        def lookup(self, schema: str, recursive: bool) -> object:
            assert recursive is True
            return lookup_result

    gio = types.SimpleNamespace(
        SettingsSchemaSource=types.SimpleNamespace(get_default=lambda: Source()), Settings=Settings
    )
    repo = types.ModuleType("gi.repository")
    repo.Gio = gio  # type: ignore[attr-defined]
    gi = types.ModuleType("gi")
    gi.repository = repo  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repo)


class _Schema:
    def __init__(self, has: bool = True) -> None:
        self.has = has

    def has_key(self, key: str) -> bool:
        return self.has


def test_default_ui_font_reader_reads_the_effective_value(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _fake_gi(monkeypatch, _Schema())
    assert fc.read_gsettings_ui_fonts() == ["Fira Sans 11"]


@pytest.mark.parametrize(("lookup", "value"), [(None, "Fira Sans 11"), (_Schema(has=False), "x"), (_Schema(), 42), (_Schema(), "")])
def test_default_ui_font_reader_missing_schema_or_odd_value_is_empty(monkeypatch, lookup, value) -> None:  # type: ignore[no-untyped-def]
    _fake_gi(monkeypatch, lookup, value)
    assert fc.read_gsettings_ui_fonts() == []


# ---------------------------------------------------------------- apply

def test_apply_writes_the_rule_file_0644_and_refreshes_cache(sandbox) -> None:  # type: ignore[no-untyped-def]
    sandbox.fonts_dir.mkdir(parents=True)
    runner = good_runner()
    a = make_adapter(runner)
    _, _, [p] = snap_plan_apply(a)
    assert rule_path().read_bytes() == p.payload
    assert stat.S_IMODE(rule_path().stat().st_mode) == 0o644
    assert [argv for argv, _ in runner.calls] == [["fc-cache", str(sandbox.fonts_dir)]]
    assert runner.calls[0][1] == 60.0
    assert sorted(os.listdir(rule_path().parent)) == ["99-scribesense.conf"]  # no temp file left


def test_apply_keeps_the_existing_file_mode(sandbox) -> None:  # type: ignore[no-untyped-def]
    _setup("present-other-0600")
    snap_plan_apply(make_adapter())
    assert stat.S_IMODE(rule_path().stat().st_mode) == 0o600


def test_no_fc_cache_when_fonts_dir_missing(sandbox) -> None:  # type: ignore[no-untyped-def]
    assert not sandbox.fonts_dir.exists()
    runner = good_runner()
    snap_plan_apply(make_adapter(runner))
    assert runner.calls == []


def test_apply_without_snapshot_is_a_programming_error(sandbox) -> None:  # type: ignore[no-untyped-def]
    a = make_adapter()
    [p] = a.plan(target(a), FONTS, CFG)
    with pytest.raises(ValueError):
        a.apply(p)
    assert not rule_path().exists()


def test_apply_propagates_fc_cache_failure_after_writing(sandbox) -> None:  # type: ignore[no-untyped-def]
    """Card: any failure stops the apply; the intent is journaled, so the controller reverts."""
    sandbox.fonts_dir.mkdir(parents=True)
    a = make_adapter(good_runner(fail={"fc-cache": ErrorCode.ADAPTER_TIMEOUT}))
    t = target(a)
    a.snapshot(t)
    [p] = a.plan(t, FONTS, CFG)
    with pytest.raises(ScribeSenseError) as exc:
        a.apply(p)
    assert exc.value.code is ErrorCode.ADAPTER_TIMEOUT


@pytest.mark.parametrize(
    "launch_error",
    [PermissionError(errno.EACCES, "launch refused"), IsADirectoryError(errno.EISDIR, "is a directory"),
     OSError(errno.ENOEXEC, "exec format error")],
    ids=["permission", "isdir", "enoexec"],
)
def test_apply_maps_a_failed_fc_cache_launch_to_a_scribesense_error(sandbox, launch_error: OSError) -> None:  # type: ignore[no-untyped-def]
    """K3 rule 5: run_cmd lets launch OSErrors through unchanged; apply() must still raise a code.

    Not NOT_WRITABLE: the rule file WAS written; the program could not be started.
    """
    sandbox.fonts_dir.mkdir(parents=True)
    a = make_adapter(good_runner(os_fail={"fc-cache": launch_error}))
    t = target(a)
    a.snapshot(t)
    [p] = a.plan(t, FONTS, CFG)
    with pytest.raises(ScribeSenseError) as exc:
        a.apply(p)
    assert exc.value.code is ErrorCode.UNEXPECTED_ADAPTER_ERROR


def _is_regular_fd(fd: int) -> bool:
    return stat.S_ISREG(os.fstat(fd).st_mode)


@pytest.mark.parametrize("start", ["absent", "present-other-0600"])
def test_failed_file_fsync_is_write_not_durable_and_leaves_the_original(sandbox, monkeypatch, start: str) -> None:  # type: ignore[no-untyped-def]
    _setup(start)
    path = rule_path()  # resolved while the sandbox is active; never re-resolved below
    assert path.is_relative_to(sandbox.root)
    path.parent.mkdir(parents=True, exist_ok=True)  # no dirs to create: the FILE fsync is what fails
    before = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None
    a = make_adapter()
    t = target(a)
    a.snapshot(t)
    [p] = a.plan(t, FONTS, CFG)
    real_fsync = os.fsync
    failed: list[int] = []

    def bad_fsync(fd: int) -> None:
        if _is_regular_fd(fd):
            failed.append(fd)
            raise OSError(errno.EIO, "EIO")
        real_fsync(fd)

    with monkeypatch.context() as m:  # never monkeypatch.undo(): it would also undo the sandbox
        m.setattr(fc.os, "fsync", bad_fsync)
        with pytest.raises(ScribeSenseError) as exc:
            a.apply(p)
    assert exc.value.code is ErrorCode.WRITE_NOT_DURABLE
    assert failed, "the file fsync was never reached"
    if before is None:
        assert not path.exists()
    else:
        assert (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) == before
    expected = [] if before is None else [fc.RULE_FILE_NAME]
    assert sorted(os.listdir(path.parent)) == expected  # no .99-scribesense.conf.*.tmp left


def test_failed_directory_fsync_is_write_not_durable(sandbox, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    path = rule_path()
    a = make_adapter()
    t = target(a)
    a.snapshot(t)
    [p] = a.plan(t, FONTS, CFG)
    real_fsync = os.fsync

    def bad_dir_fsync(fd: int) -> None:
        if not _is_regular_fd(fd):
            raise OSError(errno.EIO, "EIO")
        real_fsync(fd)

    with monkeypatch.context() as m:
        m.setattr(fc.os, "fsync", bad_dir_fsync)
        with pytest.raises(ScribeSenseError) as exc:
            a.apply(p)
    assert exc.value.code is ErrorCode.WRITE_NOT_DURABLE
    assert not path.exists()


@pytest.mark.parametrize(("start", "mode"), [("absent", 0o644), ("present-empty", 0o644), ("custom-0640", 0o640)])
def test_mode_is_set_before_the_file_fsync(sandbox, monkeypatch, start: str, mode: int) -> None:  # type: ignore[no-untyped-def]
    """§4 / K3 rule 8: the sync must persist the final mode, never mkstemp's 0o600."""
    if start == "custom-0640":
        path = rule_path()
        path.parent.mkdir(parents=True)
        path.write_bytes(b"<fontconfig/>\n")
        path.chmod(0o640)
    else:
        _setup(start)
    real_fsync = os.fsync
    synced_modes: list[int] = []

    def recording_fsync(fd: int) -> None:
        if _is_regular_fd(fd):
            synced_modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        real_fsync(fd)

    with monkeypatch.context() as m:
        m.setattr(fc.os, "fsync", recording_fsync)
        snap_plan_apply(make_adapter())
    assert synced_modes == [mode]


# ---------------------------------------------------------------- verify

def test_verify_ok_checks_generics_and_ui_families(sandbox) -> None:  # type: ignore[no-untyped-def]
    runner = good_runner()
    a = make_adapter(runner)
    t, _, _ = snap_plan_apply(a)
    runner.calls.clear()
    r = a.verify(t, FONTS)
    assert (r.chosen_ok, r.drawn_ok, r.detail_code) == (True, True, "OK")
    assert [argv for argv, _ in runner.calls] == [
        ["fc-match", "-f", "%{family[0]}", "sans\\-serif"],  # '-' escaped: it starts the size in fc patterns
        ["fc-match", "-f", "%{family[0]}", "serif"],
        ["fc-match", "-f", "%{family[0]}", "Fira Sans"],
        [sys.executable, "-P", "-m", vf.PROBE_MODULE, "sans-serif", vf.SAMPLE],  # drawn check, fresh process
    ]
    assert [timeout for _, timeout in runner.calls] == [10.0, 10.0, 10.0, fc.TIMEOUTS["other"]]


def test_verify_mismatch_on_ui_family(sandbox) -> None:  # type: ignore[no-untyped-def]
    runner = good_runner()
    a = make_adapter(runner)
    t, _, _ = snap_plan_apply(a)
    runner.answers["Fira Sans"] = "Fira Sans"
    r = a.verify(t, FONTS)
    assert (r.chosen_ok, r.drawn_ok, r.detail_code) == (False, True, ErrorCode.ADAPTER_VERIFY_FAILED.value)


@pytest.mark.parametrize("generic", fc.READING_GENERICS)
def test_verify_mismatch_on_a_generic_family_while_ui_is_fine(sandbox, generic: str) -> None:  # type: ignore[no-untyped-def]
    """Every check must match, not just the last one (the UI family is checked last)."""
    runner = good_runner()
    a = make_adapter(runner)
    t, _, _ = snap_plan_apply(a)
    runner.answers[generic] = "DejaVu Sans"
    runner.drawn = [FONTS.reading_family]  # isolate the chosen check
    r = a.verify(t, FONTS)
    assert (r.chosen_ok, r.drawn_ok, r.detail_code) == (False, True, ErrorCode.ADAPTER_VERIFY_FAILED.value)
    assert runner.commands()[-2:] == ["fc-match", PROBE] and runner.calls[-2][0][-1] == "Fira Sans"  # UI still checked


def test_verify_mismatch_when_rules_missing(sandbox) -> None:  # type: ignore[no-untyped-def]
    a = make_adapter(FakeRunner())  # fc-match answers DejaVu Sans for everything
    r = a.verify(target(a), FONTS)
    assert (r.chosen_ok, r.detail_code) == (False, ErrorCode.ADAPTER_VERIFY_FAILED.value)


@pytest.mark.parametrize("code", [ErrorCode.ADAPTER_UNAVAILABLE, ErrorCode.ADAPTER_TIMEOUT, ErrorCode.ADAPTER_COMMAND_FAILED])
def test_verify_check_unavailable(sandbox, code: ErrorCode) -> None:  # type: ignore[no-untyped-def]
    a = make_adapter(good_runner(fail={"fc-match": code}))
    t, _, _ = snap_plan_apply(a)
    r = a.verify(t, FONTS)
    # the chosen check could not run; the drawn check still ran and agreed — never "OK" (M4.13)
    assert (r.chosen_ok, r.drawn_ok, r.detail_code) == (False, True, ErrorCode.CHECK_UNAVAILABLE.value)
    assert a._run.commands().count("fc-match") == 1  # type: ignore[attr-defined]  # stops at the first failure


@pytest.mark.parametrize("drawn", [
    ["DejaVu Sans"],                                  # the original font is drawn
    [FONTS.reading_family, "DejaVu Sans"],            # a mix: some runs fall back
    ["DejaVu Sans", FONTS.reading_family],
    [FONTS.ui_family],                                # the wrong variant
])
def test_verify_drawn_mismatch_fails_even_when_fc_match_agrees(sandbox, drawn: list[str]) -> None:  # type: ignore[no-untyped-def]
    runner = good_runner(drawn=drawn)
    a = make_adapter(runner)
    t, _, _ = snap_plan_apply(a)
    r = a.verify(t, FONTS)
    assert (r.chosen_ok, r.drawn_ok, r.detail_code) == (True, False, ErrorCode.ADAPTER_VERIFY_FAILED.value)


@pytest.mark.parametrize("failure", [
    {"fail": {PROBE: ErrorCode.ADAPTER_COMMAND_FAILED}},  # probe exited non-zero (no gi / no Pango)
    {"fail": {PROBE: ErrorCode.ADAPTER_TIMEOUT}},
    {"fail": {PROBE: ErrorCode.ADAPTER_UNAVAILABLE}},
    {"os_fail": {PROBE: PermissionError(errno.EACCES, "denied")}},
])
def test_verify_drawn_unavailable_is_never_reported_verified(sandbox, failure: dict) -> None:  # type: ignore[no-untyped-def,type-arg]
    """Card must-never: no "verified" from fc-match alone where a drawn check is required (A14)."""
    a = make_adapter(good_runner(**failure))
    t, _, _ = snap_plan_apply(a)
    r = a.verify(t, FONTS)
    assert (r.chosen_ok, r.drawn_ok, r.detail_code) == (True, False, ErrorCode.CHECK_UNAVAILABLE.value)


@pytest.mark.parametrize("stdout", [b"", b"not json", b'{"v": 2, "families": ["x"]}', b'{"v": 1, "families": []}'])
def test_verify_unusable_probe_output_is_check_unavailable(sandbox, stdout: bytes) -> None:  # type: ignore[no-untyped-def]
    runner = good_runner()

    def patched(argv: list[str], timeout: float) -> FakeProc:
        proc = runner(argv, timeout)
        return FakeProc(stdout) if command_name(argv) == PROBE else proc

    a = make_adapter(runner)
    t, _, _ = snap_plan_apply(a)
    a._run = patched
    r = a.verify(t, FONTS)
    assert (r.chosen_ok, r.drawn_ok, r.detail_code) == (True, False, ErrorCode.CHECK_UNAVAILABLE.value)


def test_verify_both_checks_fail_reports_the_mismatch(sandbox) -> None:  # type: ignore[no-untyped-def]
    a = make_adapter(FakeRunner())  # nothing applied: fc-match and Pango both name DejaVu Sans
    r = a.verify(target(a), FONTS)
    assert (r.chosen_ok, r.drawn_ok, r.detail_code) == (False, False, ErrorCode.ADAPTER_VERIFY_FAILED.value)


def test_verify_gives_the_probe_only_the_fixed_sample(sandbox) -> None:  # type: ignore[no-untyped-def]
    runner = good_runner()
    a = make_adapter(runner)
    t, _, _ = snap_plan_apply(a)
    a.verify(t, FONTS)
    [probe] = [argv for argv, _ in runner.calls if command_name(argv) == PROBE]
    assert probe[-1] == vf.SAMPLE and probe[-2] == fc.DRAWN_CHECK_QUERY


def test_fc_match_pattern_escapes_separators() -> None:
    assert vf.escape_fc_pattern("Odd-Name: x,y\\z") == "Odd\\-Name\\: x\\,y\\\\z"
    assert not hasattr(fc, "_fc_pattern")  # one shared escaper (verify.py)


# ---------------------------------------------------------------- preset switching (no revert in between)

FONTS2 = FontSet("def456", "AccessSans-def456", "AccessSansUI-def456", (), ("Regular",))


class SwitchingSource:
    """gsettings font-name as the GTK adapter (M4.3) leaves it: the UI family after the first apply."""

    def __init__(self, *values: list[str]) -> None:
        self.values = list(values)

    def __call__(self) -> list[str]:
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


def test_second_apply_keeps_the_named_ui_family_redirect(sandbox) -> None:  # type: ignore[no-untyped-def]
    runner = good_runner()
    a = FontconfigAdapter(runner=runner, ui_font_source=SwitchingSource(["Fira Sans 11"], ["AccessSansUI-abc123 11"]))
    t, _, _ = snap_plan_apply(a)
    assert ("Fira Sans", FONTS.ui_family) in _rules(rule_path().read_bytes())

    a.snapshot(t)
    [p] = a.plan(t, FONTS2, CFG)  # switch preset: new key, no revert first (§2.2)
    assert _rules(p.payload) == [  # type: ignore[arg-type]
        ("sans-serif", FONTS2.reading_family), ("serif", FONTS2.reading_family), ("Fira Sans", FONTS2.ui_family)]
    a.apply(p)
    runner.answers.update({"sans-serif": FONTS2.reading_family, "serif": FONTS2.reading_family})
    runner.answers["Fira Sans"] = FONTS2.ui_family
    runner.calls.clear()
    r = a.verify(t, FONTS2)
    assert (r.chosen_ok, r.detail_code) == (True, "OK")
    assert runner.calls[-2][0][-1] == "Fira Sans"  # the carried family is still verified (before the drawn check)


def test_carried_and_current_ui_families_are_merged_and_deduplicated(sandbox) -> None:  # type: ignore[no-untyped-def]
    a = FontconfigAdapter(runner=good_runner(),
                          ui_font_source=SwitchingSource(["Fira Sans 11"], ["Cantarell, fira sans 10"]))
    t, _, _ = snap_plan_apply(a)
    a.snapshot(t)
    [p] = a.plan(t, FONTS, CFG)
    assert [f for f, _ in _rules(p.payload)][2:] == ["Cantarell", "fira sans"]  # type: ignore[arg-type]


def test_nothing_is_carried_from_a_file_scribesense_did_not_write(sandbox) -> None:  # type: ignore[no-untyped-def]
    path = rule_path()
    path.parent.mkdir(parents=True)
    path.write_bytes(fc.render_rules(FONTS, ["Fira Sans"]).replace(fc._GENERATED_MARK, b"Hand written"))
    assert _rules(plan_payload([])) == [("sans-serif", FONTS.reading_family), ("serif", FONTS.reading_family)]


def test_nothing_is_carried_after_a_revert(sandbox) -> None:  # type: ignore[no-untyped-def]
    a = FontconfigAdapter(runner=good_runner(), ui_font_source=SwitchingSource(["Fira Sans 11"], []))
    _, [item], _ = snap_plan_apply(a)
    a.revert(item, None)
    [p] = a.plan(target(a), FONTS, CFG)
    assert len(_rules(p.payload)) == 2  # type: ignore[arg-type]


def test_carried_families_still_pass_the_never_redirect_rules(sandbox) -> None:  # type: ignore[no-untyped-def]
    path = rule_path()
    path.parent.mkdir(parents=True)
    path.write_bytes(fc.render_rules(FONTS, ["JetBrains Mono", "Sans", "Inter"]))  # e.g. an older version wrote them
    assert [f for f, _ in _rules(plan_payload([]))][2:] == ["Inter"]


# ---------------------------------------------------------------- revert

def test_revert_restores_bytes_and_mode(sandbox) -> None:  # type: ignore[no-untyped-def]
    _setup("present-other-0600")
    original = rule_path().read_bytes()
    a = make_adapter()
    t = target(a)
    [item] = a.snapshot(t)
    backup = a.read_state(item.location).data
    for p in a.plan(t, FONTS, CFG):
        a.apply(p)
    rule_path().chmod(0o664)  # someone changed the mode meanwhile
    a.revert(item, backup)
    assert rule_path().read_bytes() == original
    assert stat.S_IMODE(rule_path().stat().st_mode) == 0o600


def test_revert_of_absent_file_deletes_it_and_tolerates_missing(sandbox) -> None:  # type: ignore[no-untyped-def]
    a = make_adapter()
    _, [item], _ = snap_plan_apply(a)
    assert rule_path().exists()
    a.revert(item, None)
    assert not rule_path().exists()
    a.revert(item, None)  # already gone: fine


def test_revert_ignores_fc_cache_failure(sandbox) -> None:  # type: ignore[no-untyped-def]
    sandbox.fonts_dir.mkdir(parents=True)
    a = make_adapter()
    _, [item], _ = snap_plan_apply(a)
    failing = good_runner(fail={"fc-cache": ErrorCode.ADAPTER_COMMAND_FAILED})
    a._run = failing
    a.revert(item, None)
    assert not rule_path().exists()
    assert failing.commands() == ["fc-cache"]


def test_revert_refuses_foreign_location(sandbox) -> None:  # type: ignore[no-untyped-def]
    a = make_adapter()
    [item] = a.snapshot(target(a))
    foreign = fc.SnapshotItem(item.target, "file", str(sandbox.home / ".bashrc"), False, None,
                              item.old_sha256, None, None)
    with pytest.raises(ValueError):
        a.revert(foreign, None)


def test_no_real_process_can_start_in_unit_tests() -> None:
    """The default runner is the real run_cmd; the sandbox blocks it, so only fakes can work here."""
    a = FontconfigAdapter(ui_font_source=lambda: [])
    for check in (lambda: vf.chosen_family(target(a), "sans-serif"), lambda: vf.drawn_family_pango("sans-serif"),
                  lambda: a.verify(target(a), FONTS)):
        with pytest.raises(Exception) as exc:  # ProcessLaunchBlocked lives in conftest: match by name
            check()
        assert type(exc.value).__name__ == "ProcessLaunchBlocked"


# ---------------------------------------------------------------- integration (bubblewrap only)

def make_test_font(path: Path, family: str) -> None:
    """A tiny valid TrueType font named `family` (box glyphs for printable ASCII). Pure Python."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    def box() -> object:
        pen = TTGlyphPen(None)
        pen.moveTo((50, 0))
        pen.lineTo((50, 700))
        pen.lineTo((550, 700))
        pen.lineTo((550, 0))
        pen.closePath()
        return pen.glyph()

    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder([".notdef", "space", "box"])
    fb.setupCharacterMap({0x20: "space", **{cp: "box" for cp in range(0x21, 0x7F)}})
    fb.setupGlyf({".notdef": box(), "space": TTGlyphPen(None).glyph(), "box": box()})
    fb.setupHorizontalMetrics({".notdef": (600, 50), "space": (300, 0), "box": (600, 50)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    ps = re.sub(r"[^A-Za-z0-9-]", "", family) + "-Regular"
    fb.setupNameTable({"familyName": family, "styleName": "Regular", "uniqueFontIdentifier": ps,
                       "fullName": f"{family} Regular", "psName": ps})
    fb.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    fb.setupPost()
    path.parent.mkdir(parents=True, exist_ok=True)
    fb.save(str(path))


def test_make_test_font_names_the_family(sandbox) -> None:  # type: ignore[no-untyped-def]
    from fontTools.ttLib import TTFont

    path = sandbox.root / "t.ttf"
    make_test_font(path, FONTS.reading_family)
    font = TTFont(str(path))
    assert font["name"].getDebugName(1) == FONTS.reading_family
    assert font.getBestCmap()[ord("A")] == "box"


#: fontconfig's system configuration; it includes conf.d, whose 50-user.conf includes the user conf.d.
SYSTEM_FONTS_CONF = Path("/etc/fonts/fonts.conf")
PROBE_UI_FAMILY = "ScribeSense Probe"  # not installed anywhere: only our rule can resolve it


def _real(argv: list[str]) -> tuple[str, str]:
    proc = run_cmd(argv, 10.0)
    return (proc.stdout.decode("utf-8", "replace").strip(), proc.stderr.decode("utf-8", "replace"))


def _real_match(family: str) -> str:
    out, err = _real(["fc-match", "-f", "%{family[0]}", vf.escape_fc_pattern(family)])
    assert fc.RULE_FILE_NAME not in err, err  # fontconfig did not reject our rule file
    return out


@pytest.mark.integration
def test_real_fc_match_resolves_to_the_generated_families_and_back(sandbox) -> None:  # type: ignore[no-untyped-def]
    """Card acceptance "Sandbox: fc-match results as above", inside the bwrap runner only.

    The sandbox's FONTCONFIG_FILE (set by conftest, never created there) is written here to include the
    system configuration, so real fontconfig loads the user conf.d exactly as on the desktop. Test fonts
    with the K7 family names are installed into the sandbox fonts_dir. Before apply the generics resolve
    elsewhere; after apply they resolve to our families; after revert they resolve as before.
    """
    assert SYSTEM_FONTS_CONF.is_file(), "the bwrap runner must expose the system fontconfig (read-only)"
    conf = Path(os.environ["FONTCONFIG_FILE"])
    assert conf.is_relative_to(sandbox.root)
    conf.write_text(
        '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">\n'
        f"<fontconfig><include>{SYSTEM_FONTS_CONF}</include></fontconfig>\n"
    )
    for family in (FONTS.reading_family, FONTS.ui_family):
        make_test_font(sandbox.fonts_dir / FONTS.key / f"{family}-Regular.ttf", family)

    checked = (*fc.READING_GENERICS, PROBE_UI_FAMILY)
    before = {f: _real_match(f) for f in checked}
    assert not set(before.values()) & {FONTS.reading_family, FONTS.ui_family}, (
        "without rules the generics must resolve elsewhere (does the runner expose system fonts?)")

    a = FontconfigAdapter(ui_font_source=lambda: [PROBE_UI_FAMILY + " 11"])
    t = target(a)
    # original: the real Pango probe must NOT report the reading family, so verify's drawn check fails
    assert vf.drawn_family_pango(fc.DRAWN_CHECK_QUERY) != (FONTS.reading_family,)
    assert a.verify(t, FONTS).drawn_ok is False
    [item] = a.snapshot(t)
    [p] = a.plan(t, FONTS, CFG)
    assert (PROBE_UI_FAMILY, FONTS.ui_family) in _rules(p.payload)  # type: ignore[arg-type]
    a.apply(p)

    assert {f: _real_match(f) for f in checked} == {
        "sans-serif": FONTS.reading_family, "serif": FONTS.reading_family, PROBE_UI_FAMILY: FONTS.ui_family}
    assert vf.drawn_family_pango(fc.DRAWN_CHECK_QUERY) == (FONTS.reading_family,)
    result = a.verify(t, FONTS)  # chosen (fc-match) + drawn (Pango probe in a fresh process)
    assert (result.chosen_ok, result.drawn_ok, result.detail_code) == (True, True, "OK")

    a.revert(item, None)
    assert not rule_path().exists()
    assert {f: _real_match(f) for f in checked} == before
    assert vf.drawn_family_pango(fc.DRAWN_CHECK_QUERY) != (FONTS.reading_family,)
    assert a.verify(t, FONTS).drawn_ok is False



def test_ignored_cache_refresh_failure_on_revert_is_logged_without_content() -> None:
    """The one swallowed failure (fc-cache after revert) leaves a privacy-safe log line."""
    import json

    from scribesense import log as sslog
    from scribesense.contracts.adapter import SnapshotItem, state_sha256
    from scribesense.contracts import paths as _paths

    _paths.fonts_dir().mkdir(parents=True, exist_ok=True)

    def failing_runner(argv, timeout):  # type: ignore[no-untyped-def]
        raise ScribeSenseError(ErrorCode.ADAPTER_TIMEOUT)

    adapter = FontconfigAdapter(runner=failing_runner, ui_font_source=lambda: [])
    target = adapter.detect()[0]
    item = SnapshotItem(target, "file", str(rule_path()), False, None, state_sha256(False, None), None, None)
    adapter.revert(item, None)  # must not raise
    records = [json.loads(x) for x in sslog.log_path().read_text().splitlines()]
    assert records[-1]["code"] == "ADAPTER_TIMEOUT" and records[-1]["step"] == "revert-cache-refresh"
    assert str(rule_path()) not in sslog.log_path().read_text()
