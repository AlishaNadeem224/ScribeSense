"""M4.5 — Qt adapter: conformance (M4.1) + card acceptance tests, in the unit sandbox.

The adapter runs no external command. The session variable QT_QPA_PLATFORMTHEME is removed by the
sandbox and set here per test through monkeypatch only (never the real session).
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from scribesense.adapters import qt
from scribesense.adapters.conformance import AdapterConformance
from scribesense.adapters.qt import QtAdapter, conf_path
from scribesense.contracts.adapter import Adapter, SnapshotItem, Target, state_sha256
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import FontSet
from scribesense.contracts.paths import run_cmd

CFG = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.0)
FONTS = FontSet("abc123", "AccessSans-abc123", "AccessSansUI-abc123", (), ("Regular",))
OURS_11 = b'general="AccessSansUI-abc123,11,-1,5,400,0,0,0,0,0,0,0,0,0,0,1"'

#: Shaped like the dev machine's qt6ct.conf (groups and key names only; values invented): no [Fonts].
DEV_LIKE = (
    b"[Appearance]\ncolor_scheme_path=/usr/share/qt6ct/colors/darker.conf\ncustom_palette=true\n"
    b"icon_theme=Papirus-Dark\nstandard_dialogs=default\nstyle=Fusion\n\n"
    b"[Interface]\nactivate_item_on_single_click=1\nbuttonbox_layout=0\ncursor_flash_time=1000\n"
    b"stylesheets=@Invalid()\ntoolbutton_style=4\nwheel_scroll_lines=3\n\n"
    b"[SettingsWindow]\ngeometry=@ByteArray(\\x1\\xd9\\xd0\\xcb\\0\\x3\\0\\0)\n\n"
    b"[Troubleshooting]\nforce_raster_widgets=1\nignored_applications=@Invalid()\n"
)
WITH_FONTS = (
    b"[Appearance]\nstyle=Fusion\n\n"
    b'[Fonts]\nfixed="JetBrains Mono,10,-1,5,400,0,0,0,0,0,0,0,0,0,0,1"\n'
    b'general="Noto Sans,10.5,-1,5,400,0,0,0,0,0,0,0,0,0,0,1"\n\n'
    b"[Interface]\n; a comment the user wrote\nmenus_have_icons=true\n"
)
QT5_FORM = (
    b"[Appearance]\r\nstyle=Breeze\r\n\r\n"
    b'[Fonts]\r\nfixed="Hack,9,-1,5,50,0,0,0,0,0"\r\ngeneral="Cantarell,9,-1,5,50,0,0,0,0,0"\r\n'
)


@pytest.fixture(autouse=True)
def qt6ct_session(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("QT_QPA_PLATFORMTHEME", "qt6ct")


def target(adapter: QtAdapter) -> Target:
    return adapter.detect()[0]


def write_conf(data: bytes, mode: int = 0o644) -> Path:
    path = conf_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
    return path


def snap_plan_apply(adapter: QtAdapter):  # type: ignore[no-untyped-def]
    t = target(adapter)
    items = adapter.snapshot(t)
    planned = adapter.plan(t, FONTS, CFG)
    for p in planned:
        adapter.apply(p)
    return t, items, planned


def home_fingerprint(home: Path) -> dict[str, str]:
    """Every file under HOME with mode, and every symlink with its target (os.walk does not follow)."""
    out: dict[str, str] = {}
    for dirpath, dirs, files in os.walk(home):
        for name in dirs + files:
            p = Path(dirpath) / name
            if p.is_symlink():
                out[str(p)] = "symlink:" + os.readlink(p)
            elif p.is_file():
                out[str(p)] = f"{state_sha256(True, p.read_bytes())}:{stat.S_IMODE(p.stat().st_mode):o}"
    return out


# ---------------------------------------------------------------- conformance

def _symlink_dir(sb_home: Path, data: bytes | None) -> None:
    real = sb_home / ".mydotfiles" / "qt6ct"
    real.mkdir(parents=True)
    conf_path().parent.parent.mkdir(parents=True, exist_ok=True)
    conf_path().parent.symlink_to(real)
    if data is not None:
        (real / qt.CONF_NAME).write_bytes(data)


def _symlink_file(sb_home: Path, data: bytes) -> None:
    real = sb_home / "dotfiles" / "qt6ct.conf"
    real.parent.mkdir(parents=True)
    real.write_bytes(data)
    real.chmod(0o640)
    conf_path().parent.mkdir(parents=True, exist_ok=True)
    conf_path().symlink_to(real)


START_STATES = {
    "absent": lambda home: None,
    "dev-like-no-fonts-0600": lambda home: write_conf(DEV_LIKE, 0o600),
    "fonts-general-and-fixed": lambda home: write_conf(WITH_FONTS),
    "qt5-form-crlf": lambda home: write_conf(QT5_FORM),
    "empty-file": lambda home: write_conf(b""),
    "symlinked-dir": lambda home: _symlink_dir(home, DEV_LIKE),
    "symlinked-dir-no-file": lambda home: _symlink_dir(home, None),
    "symlinked-file": lambda home: _symlink_file(home, WITH_FONTS),
}


@dataclass
class QtHarness:
    adapter: QtAdapter
    home: Path
    fonts: FontSet = FONTS
    config: Configuration = CFG

    def fingerprint(self) -> dict[str, str]:
        """Every file/symlink under HOME. The file a symlinked location resolves to IS that location, so
        it is keyed by the location (write-through is expected); every other path keeps its own key."""
        out = home_fingerprint(self.home)
        location = str(conf_path())
        real = os.path.realpath(location)
        resolved = out.pop(real, None) if real != location else out.get(location)
        out[location] = f"{self.adapter.read_state(location).sha256}|{resolved}"
        return out

    def external_edit(self, location: str) -> None:
        path = Path(location)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"[Fonts]\ngeneral=\"Someone Else,12\"\n")


class TestQtConformance(AdapterConformance):
    @pytest.fixture(params=sorted(START_STATES))
    def conformance_harness(self, request: pytest.FixtureRequest, sandbox, qt6ct_session) -> QtHarness:  # type: ignore[no-untyped-def]
        START_STATES[request.param](sandbox.home)
        return QtHarness(QtAdapter(), sandbox.home)


# ---------------------------------------------------------------- basics

def test_attributes_and_single_target() -> None:
    a = QtAdapter()
    _typed: Adapter = a  # static K3 conformance
    assert (a.name, a.requires_restart, a.must_be_closed) == ("qt", True, False)
    assert a.detect() == [Target("qt", "qt:qt6ct", "Qt apps (qt6ct)", False)]


def test_conf_path_follows_config_home(sandbox, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    assert conf_path() == sandbox.config_home / "qt6ct" / "qt6ct.conf"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(sandbox.root / "elsewhere"))
    assert conf_path() == sandbox.root / "elsewhere" / "qt6ct" / "qt6ct.conf"


@pytest.mark.parametrize("bad", ["relative/qt6ct/qt6ct.conf", "/tmp/qt6ct.conf", "/tmp/x/qt6ct/other.conf",
                                 "/tmp/a/../qt6ct/qt6ct.conf"])
def test_foreign_locations_are_refused(bad: str) -> None:
    a = QtAdapter()
    with pytest.raises(ValueError):
        a.read_state(bad)
    item = SnapshotItem(target(a), "file", bad, False, None, state_sha256(False, None), None, None)
    with pytest.raises(ValueError):
        a.revert(item, None)


def test_foreign_target_is_refused() -> None:
    a = QtAdapter()
    with pytest.raises(ValueError):
        a.snapshot(Target("gtk", "gtk:user", "x", False))
    with pytest.raises(ValueError):
        a.plan(Target("qt", "qt:other", "x", False), FONTS, CFG)


# ---------------------------------------------------------------- session precondition

@pytest.mark.parametrize("value", [None, "", "qt5ct", "gtk3", "kde", "QT6CT", "qt6ct-kde"])
def test_session_without_qt6ct_is_not_covered_and_nothing_is_written(sandbox, monkeypatch, value) -> None:  # type: ignore[no-untyped-def]
    write_conf(WITH_FONTS)
    if value is None:
        monkeypatch.delenv("QT_QPA_PLATFORMTHEME", raising=False)
    else:
        monkeypatch.setenv("QT_QPA_PLATFORMTHEME", value)
    before = home_fingerprint(sandbox.home)
    env_before = dict(os.environ)
    a = QtAdapter()
    t = target(a)  # still listed, so the coverage report can show the reason
    for step in (lambda: a.snapshot(t), lambda: a.plan(t, FONTS, CFG)):
        with pytest.raises(ScribeSenseError) as exc:
            step()
        assert exc.value.code is ErrorCode.QT6CT_NOT_IN_SESSION
    assert home_fingerprint(sandbox.home) == before
    assert dict(os.environ) == env_before  # the session environment is never changed


def test_environment_is_never_changed_by_a_full_cycle(sandbox) -> None:  # type: ignore[no-untyped-def]
    write_conf(DEV_LIKE)
    env_before = dict(os.environ)
    a = QtAdapter()
    t, [item], _ = snap_plan_apply(a)
    a.verify(t, FONTS)
    a.revert(item, DEV_LIKE)
    assert dict(os.environ) == env_before


def test_revert_works_outside_a_qt6ct_session(sandbox, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    write_conf(WITH_FONTS)
    a = QtAdapter()
    _, [item], _ = snap_plan_apply(a)
    monkeypatch.delenv("QT_QPA_PLATFORMTHEME")  # e.g. recovery from a TTY
    a.revert(item, WITH_FONTS)
    assert conf_path().read_bytes() == WITH_FONTS


# ---------------------------------------------------------------- the edit

def plan_payload(start: bytes | None) -> bytes:
    if start is not None:
        write_conf(start)
    a = QtAdapter()
    [p] = a.plan(target(a), FONTS, CFG)
    assert isinstance(p.payload, bytes)
    return p.payload


def test_dev_like_file_gets_a_fonts_group_appended_and_keeps_every_byte() -> None:
    assert plan_payload(DEV_LIKE) == DEV_LIKE + b"\n[Fonts]\n" + OURS_11 + b"\n"


def test_absent_and_empty_file_get_just_the_fonts_group() -> None:
    assert plan_payload(None) == b"[Fonts]\n" + OURS_11 + b"\n"
    assert plan_payload(b"") == b"[Fonts]\n" + OURS_11 + b"\n"


def test_file_without_final_newline_or_with_blank_end() -> None:
    assert plan_payload(b"[Appearance]\nstyle=Fusion") == (
        b"[Appearance]\nstyle=Fusion\n\n[Fonts]\n" + OURS_11 + b"\n")
    assert plan_payload(b"[Appearance]\nstyle=Fusion\n\n") == (
        b"[Appearance]\nstyle=Fusion\n\n[Fonts]\n" + OURS_11 + b"\n")


def test_existing_general_is_replaced_in_place_and_fixed_never_changes() -> None:
    payload = plan_payload(WITH_FONTS)
    old, new = WITH_FONTS.split(b"\n"), payload.split(b"\n")
    assert len(old) == len(new)
    diff = [i for i, (o, n) in enumerate(zip(old, new, strict=True)) if o != n]
    assert [new[i] for i in diff] == [b'general="AccessSansUI-abc123,10.5,-1,5,400,0,0,0,0,0,0,0,0,0,0,1"']
    assert b'fixed="JetBrains Mono,10,-1,5,400,0,0,0,0,0,0,0,0,0,0,1"' in new  # fixed untouched
    assert b"; a comment the user wrote" in new


def test_qt5_value_is_parsed_crlf_kept_and_qt6_form_written() -> None:
    payload = plan_payload(QT5_FORM)
    assert payload == QT5_FORM.replace(
        b'general="Cantarell,9,-1,5,50,0,0,0,0,0"',
        b'general="AccessSansUI-abc123,9,-1,5,400,0,0,0,0,0,0,0,0,0,0,1"')
    assert b'fixed="Hack,9,-1,5,50,0,0,0,0,0"\r\n' in payload


def test_fonts_group_without_general_gets_the_key_inside_it() -> None:
    start = b'[Appearance]\nstyle=Fusion\n\n[Fonts]\nfixed="Hack,9"\n\n[Interface]\nx=1\n'
    assert plan_payload(start) == (
        b'[Appearance]\nstyle=Fusion\n\n[Fonts]\nfixed="Hack,9"\n' + OURS_11 + b"\n\n[Interface]\nx=1\n")
    assert plan_payload(b'[Fonts]\nfixed="Hack,9"') == b'[Fonts]\nfixed="Hack,9"\n' + OURS_11 + b"\n"
    assert plan_payload(b"[Fonts]\n") == b"[Fonts]\n" + OURS_11 + b"\n"


def test_general_outside_fonts_and_lookalike_keys_are_untouched() -> None:
    start = (b"[Appearance]\ngeneral=keep-me\n\n[Fonts]\n;general=commented\ngeneral_x=1\n"
             b" general = \"Noto Sans,12\"\n\n[Other]\ngeneral=keep-me-too\n")
    payload = plan_payload(start)
    assert payload == start.replace(
        b' general = "Noto Sans,12"',
        b' general = "AccessSansUI-abc123,12,-1,5,400,0,0,0,0,0,0,0,0,0,0,1"')


def test_duplicate_general_keys_are_all_rewritten() -> None:
    start = b'[Fonts]\ngeneral="A,10"\n\n[Interface]\nx=1\n\n[Fonts]\ngeneral="B,13"\n'
    payload = plan_payload(start)
    assert payload.count(b"AccessSansUI-abc123,13,") == 2  # size from the value QSettings reads (last)
    assert b"[Interface]\nx=1\n" in payload


@pytest.mark.parametrize(("value", "size"), [
    (b'"Noto Sans,10.5,-1,5,400,0,0,0,0,0,0,0,0,0,0,1"', 10.5),
    (b'"Cantarell,9,-1,5,50,0,0,0,0,0"', 9.0),
    (b"Noto Sans,14,-1,5,50,0,0,0,0,0", 14.0),  # unquoted: QSettings reads a string list
    (b'"Noto Sans,-1,16,5,400,0,0,0,0,0,0,0,0,0,0,1"', 11.0),  # pixel-sized
    (b'"Noto Sans,999,-1,5,400,0,0,0,0,0,0,0,0,0,0,1"', 11.0),
    (b"@Variant(\\0\\0\\0@\\0\\0\\0\\x12)", 11.0),  # binary QFont (old qt5ct)
    (b'""', 11.0),
    (b"", 11.0),
])
def test_size_of_the_existing_general_font_is_kept(value: bytes, size: float) -> None:
    payload = plan_payload(b"[Fonts]\ngeneral=" + value + b"\n")
    assert payload == b'[Fonts]\ngeneral="' + qt.qfont_string(FONTS.ui_family, size).encode() + b'"\n'


@pytest.mark.parametrize("fonts", [
    FontSet("k", "AccessSans-k", "Fira Sans", (), ()),
    FontSet("k", "AccessSans-k", "AccessSansUI-", (), ()),
    FontSet("k", "AccessSans-k", "AccessSansUI-a,b", (), ()),
    FontSet("k", "AccessSans-k", 'AccessSansUI-a"b', (), ()),
    FontSet("k", "AccessSans-k", "AccessSansUI-a\nb", (), ()),
    FontSet("k", "AccessSans-k", "AccessSansUI-a b", (), ()),
])
def test_plan_refuses_families_that_are_not_ours_or_need_escaping(fonts: FontSet) -> None:
    a = QtAdapter()
    with pytest.raises(ScribeSenseError) as exc:
        a.plan(target(a), fonts, CFG)
    assert exc.value.code is ErrorCode.UNEXPECTED_ADAPTER_ERROR


def test_plan_is_deterministic() -> None:
    write_conf(WITH_FONTS)
    assert plan_payload(None) == plan_payload(None)


# ---------------------------------------------------------------- QFont / QSettings helpers

@pytest.mark.parametrize("size", [6.0, 9.0, 10.5, 11.0, 12.25, 72.0])
def test_qfont_round_trip(size: float) -> None:
    text = qt.qfont_string("AccessSansUI-abc123", size)
    assert len(text.split(",")) == 16  # Qt 6 layout [V]
    assert qt.parse_qfont(text) == ("AccessSansUI-abc123", size)
    assert qt.unquote_value('"' + text + '"') == text


def test_parse_qfont_qt5_and_odd_values() -> None:
    assert qt.parse_qfont("Cantarell,9,-1,5,50,0,0,0,0,0") == ("Cantarell", 9.0)
    assert qt.parse_qfont("Noto Sans") == ("Noto Sans", 11.0)
    assert qt.parse_qfont("Noto Sans,abc") == ("Noto Sans", 11.0)
    assert qt.parse_qfont("@Variant(x)") is None
    assert qt.parse_qfont("") is None and qt.parse_qfont(",11") is None


@pytest.mark.parametrize(("raw", "value"), [
    ('"Noto Sans,11,-1"', "Noto Sans,11,-1"),
    ("Noto Sans, 11, -1", "Noto Sans,11,-1"),
    ('"a\\"b\\\\c"', 'a"b\\c'),
    ('"x\\x41y"', "xAy"),
    ('"a;b" ; trailing comment', "a;b"),
    ("  plain  ", "plain"),
])
def test_unquote_value(raw: str, value: str) -> None:
    assert qt.unquote_value(raw) == value


def test_split_lines_round_trips_every_ending() -> None:
    data = b"a\r\nb\rc\n\nd"
    assert b"".join(qt.split_lines(data)) == data
    assert qt.split_lines(data) == [b"a\r\n", b"b\r", b"c\n", b"\n", b"d"]


def test_bom_before_the_first_group_is_kept() -> None:
    start = b"\xef\xbb\xbf[Fonts]\ngeneral=\"X,12\"\n"
    assert plan_payload(start) == b"\xef\xbb\xbf[Fonts]\n" + OURS_11.replace(b",11,", b",12,") + b"\n"


@pytest.mark.parametrize("header", [b"[Fonts] ; comment", b"[Fonts]junk", b"  [ Fonts ] x"])
def test_group_name_ends_at_the_first_bracket_like_qsettings(header: bytes) -> None:
    # QSettings takes the group name up to the first ']' and ignores the rest of the header line.
    start = header + b'\ngeneral="X,12"\n\n[Other] ;c\ngeneral=keep\n'
    assert qt.current_general(start) == "X,12"
    payload = plan_payload(start)
    assert payload == start.replace(b'general="X,12"', OURS_11.replace(b",11,", b",12,"))
    assert payload.count(b"[Fonts]") == start.count(b"[Fonts]")


def test_group_header_without_closing_bracket_runs_to_the_line_end() -> None:
    start = b'[Fonts\ngeneral="X,12"\n'
    assert qt.current_general(start) == "X,12"


# ---------------------------------------------------------------- apply / symlinks / durability

def test_apply_creates_dir_and_file_0644(sandbox) -> None:  # type: ignore[no-untyped-def]
    a = QtAdapter()
    snap_plan_apply(a)
    assert conf_path().read_bytes() == b"[Fonts]\n" + OURS_11 + b"\n"
    assert stat.S_IMODE(conf_path().stat().st_mode) == 0o644


def test_apply_keeps_the_existing_mode(sandbox) -> None:  # type: ignore[no-untyped-def]
    write_conf(DEV_LIKE, 0o600)
    snap_plan_apply(QtAdapter())
    assert stat.S_IMODE(conf_path().stat().st_mode) == 0o600


def test_apply_without_snapshot_is_a_programming_error(sandbox) -> None:  # type: ignore[no-untyped-def]
    a = QtAdapter()
    [p] = a.plan(target(a), FONTS, CFG)
    with pytest.raises(ValueError):
        a.apply(p)
    assert not conf_path().exists()


def test_symlinked_config_dir_is_preserved(sandbox) -> None:  # type: ignore[no-untyped-def]
    _symlink_dir(sandbox.home, DEV_LIKE)
    link = conf_path().parent
    real = sandbox.home / ".mydotfiles" / "qt6ct" / "qt6ct.conf"
    a = QtAdapter()
    _, [item], _ = snap_plan_apply(a)
    assert link.is_symlink() and os.readlink(link) == str(real.parent)
    assert real.read_bytes() == DEV_LIKE + b"\n[Fonts]\n" + OURS_11 + b"\n"
    assert not any(p.name.endswith(".tmp") for p in real.parent.iterdir())
    a.revert(item, DEV_LIKE)
    assert link.is_symlink() and real.read_bytes() == DEV_LIKE


def test_symlinked_file_is_written_through_and_preserved(sandbox) -> None:  # type: ignore[no-untyped-def]
    _symlink_file(sandbox.home, WITH_FONTS)
    real = sandbox.home / "dotfiles" / "qt6ct.conf"
    a = QtAdapter()
    _, [item], _ = snap_plan_apply(a)
    assert conf_path().is_symlink() and os.readlink(conf_path()) == str(real)
    assert b"AccessSansUI-abc123" in real.read_bytes()
    assert stat.S_IMODE(real.stat().st_mode) == 0o640
    assert item.file_mode == 0o640
    a.revert(item, WITH_FONTS)
    assert conf_path().is_symlink() and real.read_bytes() == WITH_FONTS


def test_failed_file_fsync_is_write_not_durable_and_leaves_the_original(sandbox, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    write_conf(WITH_FONTS)
    a = QtAdapter()
    t = target(a)
    a.snapshot(t)
    [p] = a.plan(t, FONTS, CFG)
    real_fsync = os.fsync

    def bad_fsync(fd: int) -> None:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(5, "EIO")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", bad_fsync)
    with pytest.raises(ScribeSenseError) as exc:
        a.apply(p)
    assert exc.value.code is ErrorCode.WRITE_NOT_DURABLE
    assert conf_path().read_bytes() == WITH_FONTS
    assert [x.name for x in conf_path().parent.iterdir()] == ["qt6ct.conf"]  # temp file cleaned up


# ---------------------------------------------------------------- verify

def test_verify_before_and_after_apply(sandbox) -> None:  # type: ignore[no-untyped-def]
    write_conf(WITH_FONTS)
    a = QtAdapter()
    t = target(a)
    assert a.verify(t, FONTS) == qt.VerifyResult(t, False, None, "ADAPTER_VERIFY_FAILED")
    _, [item], _ = snap_plan_apply(a)
    assert a.verify(t, FONTS) == qt.VerifyResult(t, True, None, "OK")
    a.revert(item, WITH_FONTS)
    assert a.verify(t, FONTS).chosen_ok is False


def test_verify_without_file_fails_and_unreadable_is_check_unavailable(sandbox) -> None:  # type: ignore[no-untyped-def]
    a = QtAdapter()
    t = target(a)
    assert a.verify(t, FONTS).detail_code == "ADAPTER_VERIFY_FAILED"
    conf_path().mkdir(parents=True)  # a directory where the file should be: unreadable
    assert a.verify(t, FONTS) == qt.VerifyResult(t, False, None, "CHECK_UNAVAILABLE")


def test_verify_compares_the_family_exactly(sandbox) -> None:  # type: ignore[no-untyped-def]
    write_conf(b'[Fonts]\ngeneral="AccessSansUI-abc1234,11"\n')
    assert QtAdapter().verify(Target("qt", "qt:qt6ct", "x", False), FONTS).chosen_ok is False


# ---------------------------------------------------------------- revert

def test_revert_restores_bytes_and_mode(sandbox) -> None:  # type: ignore[no-untyped-def]
    write_conf(QT5_FORM, 0o640)
    a = QtAdapter()
    _, [item], _ = snap_plan_apply(a)
    a.revert(item, QT5_FORM)
    assert conf_path().read_bytes() == QT5_FORM
    assert stat.S_IMODE(conf_path().stat().st_mode) == 0o640


def test_revert_of_absent_file_deletes_it_and_tolerates_missing(sandbox) -> None:  # type: ignore[no-untyped-def]
    a = QtAdapter()
    _, [item], _ = snap_plan_apply(a)
    a.revert(item, None)
    assert not conf_path().exists()
    a.revert(item, None)


def test_no_process_is_ever_started(sandbox, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import subprocess

    def boom(*a, **kw):  # type: ignore[no-untyped-def]
        raise AssertionError("the Qt adapter must not start processes")

    monkeypatch.setattr(subprocess.Popen, "__init__", boom)
    write_conf(DEV_LIKE)
    a = QtAdapter()
    t, [item], _ = snap_plan_apply(a)
    a.verify(t, FONTS)
    a.revert(item, DEV_LIKE)


# ---------------------------------------------------------------- integration (bubblewrap only)

QT6CT_PLUGIN = Path("/usr/lib64/qt6/plugins/platformthemes/libqt6ct.so")
_PROBE = (
    "import sys\n"
    "from PyQt6.QtWidgets import QApplication\n"
    "app = QApplication(['probe'])\n"
    "f = app.font()\n"
    "sys.stdout.write(f.family() + '|' + str(f.pointSizeF()))\n"
)


@pytest.mark.integration
def test_real_qt6ct_reads_the_general_font_we_wrote(sandbox) -> None:  # type: ignore[no-untyped-def]
    """[V] confirmation of the QFont layout: a real Qt app with the real qt6ct plugin (offscreen, sandbox
    HOME) reports our family and the kept size as its application font. Needs PyQt6 + qt6ct in the runner.
    """
    pytest.importorskip("PyQt6.QtWidgets")
    if not QT6CT_PLUGIN.is_file():
        pytest.skip("qt6ct platform theme plugin not installed in the runner")
    write_conf(WITH_FONTS)
    a = QtAdapter()
    _, [item], _ = snap_plan_apply(a)
    env = {"QT_QPA_PLATFORMTHEME": "qt6ct", "QT_QPA_PLATFORM": "offscreen"}
    script = sandbox.root / "probe.py"
    script.write_text(_PROBE)
    argv = ["env", *(f"{k}={v}" for k, v in env.items()), sys.executable, "-P", str(script)]
    out = run_cmd(argv, 20.0).stdout.decode()
    assert out == f"{FONTS.ui_family}|10.5"
    a.revert(item, WITH_FONTS)
    out = run_cmd(argv, 20.0).stdout.decode()
    assert out.split("|")[0] == "Noto Sans"
