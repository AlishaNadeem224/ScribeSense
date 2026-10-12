"""M4.7 — Chromium preferences adapter: conformance (M4.1) + card acceptance tests, in the unit sandbox.

No process is launched (the adapter runs no external commands). Preferences files are written in Chrome's
own format (compact, sorted keys, '<' as \\u003C, no trailing newline — checked against a real Brave file's
structure, never its values). The headless-browser test is @pytest.mark.integration (bubblewrap only).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scribesense.adapters import chromium as cr
from scribesense.adapters.chromium import ChromiumAdapter
from scribesense.adapters.conformance import AdapterConformance, snapshot_with_backups
from scribesense.contracts.adapter import Adapter, SnapshotItem, Target, state_sha256
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import FontSet
from scribesense.contracts.paths import config_home, home, run_cmd

CFG = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.0)
FONTS = FontSet("abc123", "AccessSans-abc123", "AccessSansUI-abc123", (), ("Regular",))
FAM = FONTS.reading_family

# Chrome-style files (bytes exactly as Chrome's JSONWriter would write them).
NO_WEBKIT = (
    b'{"browser":{"window_placement":{"bottom":1000,"maximized":false}},"intl":{"selected_languages":"en-US,en"},'
    b'"profile":{"exit_type":"Normal","name":"Person 1"},"zoom":{"default":1.0}}'
)
NO_WEBKIT_APPLIED = (
    b'{"browser":{"window_placement":{"bottom":1000,"maximized":false}},"intl":{"selected_languages":"en-US,en"},'
    b'"profile":{"exit_type":"Normal","name":"Person 1"},'
    b'"webkit":{"webprefs":{"fonts":{"sansserif":{"Zyyy":"AccessSans-abc123"},"standard":{"Zyyy":"AccessSans-abc123"}}}},'
    b'"zoom":{"default":1.0}}'
)
WITH_FONTS = (
    b'{"webkit":{"webprefs":{"default_font_size":18,"fonts":{"fixed":{"Zyyy":"DejaVu Sans Mono"},'
    b'"sansserif":{"Arab":"Noto Naskh Arabic","Zyyy":"Arial"},"serif":{"Zyyy":"Times"},'
    b'"standard":{"Hani":"Noto Sans CJK SC","Zyyy":"Times"}}}}}'
)
UNICODE = (
    '{"bookmark_bar":{"show_on_all_tabs":false},'
    '"profile":{"name":"Zoë 李","tagline":"a\\u003Cb \\u2028 tab\\t q\\"x\\" back\\\\ c\\u001F"},'
    '"translate":{"blocked":["ру","中文"]},"x":[1,2.5,-3,1.3432629852567852e+16,null,true,{}]}'
).encode("utf-8")

START_STATES: dict[str, tuple[bytes, int]] = {
    "no-webkit": (NO_WEBKIT, 0o600),
    "fonts-other-scripts": (WITH_FONTS, 0o600),
    "unicode-elsewhere": (UNICODE, 0o644),
    "empty-object": (b"{}", 0o600),
}


# ---------------------------------------------------------------- helpers

def brave_dir() -> Path:
    return config_home() / "BraveSoftware" / "Brave-Browser"


def make_profile(data_dir: Path, profile: str = "Default", data: bytes = NO_WEBKIT, mode: int = 0o600) -> Path:
    path = data_dir / profile / "Preferences"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
    return path


def set_running(data_dir: Path) -> None:
    os.symlink("myhost-12345", data_dir / "SingletonLock")  # dangling, like the real lock


def target_for(adapter: ChromiumAdapter, target_id: str) -> Target:
    [t] = [t for t in adapter.detect() if t.target_id == target_id]
    return t


def snap_plan_apply(adapter: ChromiumAdapter, t: Target) -> tuple[list[SnapshotItem], bytes]:
    items = adapter.snapshot(t)
    [p] = adapter.plan(t, FONTS, CFG)
    adapter.apply(p)
    assert isinstance(p.payload, bytes)
    return items, p.payload


def expected_after(original: dict[str, Any]) -> dict[str, Any]:
    """Independent model of the edit: the two keys set, nested dicts created, nothing else changed."""
    out = json.loads(json.dumps(original))
    for script_dict in ("standard", "sansserif"):
        out.setdefault("webkit", {}).setdefault("webprefs", {}).setdefault("fonts", {}).setdefault(script_dict, {})["Zyyy"] = FAM
    return out


def fingerprint_tree(root: Path) -> dict[str, str]:
    """Every entry under root, symlinks recorded as links (target + content) so replacing one is caught."""
    out: dict[str, str] = {}
    for dirpath, dirs, files in os.walk(root):
        for name in files + [d for d in dirs if os.path.islink(os.path.join(dirpath, d))]:
            p = Path(dirpath) / name
            if p.is_symlink():
                if p.is_file():  # the target's mode too: a write through the link must keep it
                    content = f"{state_sha256(True, p.read_bytes())}:{stat.S_IMODE(p.stat().st_mode):o}"
                else:
                    content = "dangling"
                out[str(p)] = f"L:{os.readlink(p)}:{content}"
            else:
                out[str(p)] = f"{state_sha256(True, p.read_bytes())}:{stat.S_IMODE(p.stat().st_mode):o}"
    return out


# ---------------------------------------------------------------- conformance

@dataclass
class ChromiumHarness:
    adapter: ChromiumAdapter
    home: Path
    fonts: FontSet = FONTS
    config: Configuration = CFG

    def fingerprint(self) -> dict[str, str]:
        return fingerprint_tree(self.home)

    def external_edit(self, location: str) -> None:
        Path(location).write_bytes(b'{"changed_by":"someone else"}')


class TestChromiumConformance(AdapterConformance):
    @pytest.fixture(params=sorted(START_STATES) + ["symlinked-outside-home", "several-browsers"])
    def conformance_harness(self, request: pytest.FixtureRequest, sandbox) -> ChromiumHarness:  # type: ignore[no-untyped-def]
        if request.param == "symlinked-outside-home":
            real = sandbox.root / "dotfiles" / "Preferences"
            real.parent.mkdir(parents=True)
            real.write_bytes(WITH_FONTS)
            real.chmod(0o640)
            (brave_dir() / "Default").mkdir(parents=True)
            os.symlink(real, brave_dir() / "Default" / "Preferences")
        elif request.param == "several-browsers":
            make_profile(brave_dir(), "Default", UNICODE)
            make_profile(brave_dir(), "Profile 1", WITH_FONTS)
            make_profile(config_home() / "chromium", "Default", NO_WEBKIT)
            make_profile(home() / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser", "Default", NO_WEBKIT)
        else:
            data, mode = START_STATES[request.param]
            make_profile(brave_dir(), "Default", data, mode)
        return ChromiumHarness(ChromiumAdapter(), sandbox.home)


# ---------------------------------------------------------------- attributes and detection

def test_attributes() -> None:
    a = ChromiumAdapter()
    _typed: Adapter = a  # static K3 conformance (Adapter is not runtime_checkable)
    assert (a.name, a.requires_restart, a.must_be_closed) == ("chromium", True, True)


def test_no_browsers_no_targets() -> None:
    assert ChromiumAdapter().detect() == []


def test_detects_every_browser_and_profile_including_flatpak(sandbox) -> None:  # type: ignore[no-untyped-def]
    flat_brave = sandbox.home / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser"
    flat_chromium = sandbox.home / ".var/app/org.chromium.Chromium/config/chromium"
    make_profile(brave_dir(), "Default")
    make_profile(brave_dir(), "Profile 1")
    make_profile(brave_dir(), "Profile 12")
    make_profile(sandbox.config_home / "google-chrome", "Default")
    make_profile(sandbox.config_home / "chromium", "Profile 3")
    make_profile(flat_brave, "Default")
    make_profile(flat_chromium, "Default")
    targets = ChromiumAdapter().detect()
    assert [(t.target_id, t.display_name, t.running) for t in targets] == [
        ("brave:Default", "Brave (Default)", False),
        ("brave:Profile_1", "Brave (Profile 1)", False),
        ("brave:Profile_12", "Brave (Profile 12)", False),
        ("chrome:Default", "Google Chrome (Default)", False),
        ("chromium:Profile_3", "Chromium (Profile 3)", False),
        ("brave-flatpak:Default", "Brave Flatpak (Default)", False),
        ("chromium-flatpak:Default", "Chromium Flatpak (Default)", False),
    ]
    assert all(t.adapter == "chromium" for t in targets)
    assert all(re.fullmatch(r"[A-Za-z0-9_.:+-]{1,64}", t.target_id) for t in targets)  # log identifier shape


def test_flatpak_location_comes_from_home(sandbox) -> None:  # type: ignore[no-untyped-def]
    path = make_profile(sandbox.home / ".var/app/org.chromium.Chromium/config/chromium")
    a = ChromiumAdapter()
    [item] = a.snapshot(target_for(a, "chromium-flatpak:Default"))
    assert item.location == str(path)


def test_skips_non_profiles_and_profiles_without_preferences(sandbox) -> None:  # type: ignore[no-untyped-def]
    make_profile(brave_dir(), "Default")
    for other in ("System Profile", "Guest Profile", "Crashpad", "Profile_9", "Profile"):
        make_profile(brave_dir(), other)
    (brave_dir() / "Profile 2").mkdir()  # no Preferences -> no target, and none is ever created
    (brave_dir() / "Profile 4" / "Preferences").mkdir(parents=True)  # a directory, not a file
    make_profile(brave_dir(), "Profile bad")
    make_profile(brave_dir(), "Profile ünï")  # not identifier-safe
    before = fingerprint_tree(sandbox.home)
    ids = [t.target_id for t in ChromiumAdapter().detect()]
    assert ids == ["brave:Default", "brave:Profile_bad"]
    assert not (brave_dir() / "Profile 2" / "Preferences").exists()
    assert fingerprint_tree(sandbox.home) == before


def test_colliding_profile_ids_are_skipped(sandbox) -> None:  # type: ignore[no-untyped-def]
    make_profile(brave_dir(), "Profile a b")
    make_profile(brave_dir(), "Profile a_b")
    make_profile(brave_dir(), "Default")
    assert [t.target_id for t in ChromiumAdapter().detect()] == ["brave:Default"]


def test_running_is_a_singleton_lock_symlink_even_dangling(sandbox) -> None:  # type: ignore[no-untyped-def]
    make_profile(brave_dir(), "Default")
    make_profile(brave_dir(), "Profile 1")
    make_profile(sandbox.config_home / "chromium")
    set_running(brave_dir())
    running = {t.target_id: t.running for t in ChromiumAdapter().detect()}
    assert running == {"brave:Default": True, "brave:Profile_1": True, "chromium:Default": False}


# ---------------------------------------------------------------- locations

def test_read_state_refuses_foreign_locations(sandbox) -> None:  # type: ignore[no-untyped-def]
    a = ChromiumAdapter()
    for bad in (
        "relative/BraveSoftware/Brave-Browser/Default/Preferences",
        str(sandbox.home / ".bashrc"),
        str(brave_dir() / "Default" / "Secure Preferences"),
        str(brave_dir() / "Default" / "Bookmarks"),
        str(brave_dir() / "System Profile" / "Preferences"),
        str(sandbox.config_home / "other-browser" / "Default" / "Preferences"),
        str(brave_dir() / ".." / "Brave-Browser" / "Default" / "Preferences"),
    ):
        with pytest.raises(ValueError):
            a.read_state(bad)


def test_foreign_target_is_refused() -> None:
    a = ChromiumAdapter()
    for t in (Target("gtk", "brave:Default", "x", False), Target("chromium", "edge:Default", "x", False),
              Target("chromium", "nocolon", "x", False)):
        with pytest.raises(ValueError):
            a.plan(t, FONTS, CFG)


def test_vanished_profile_is_profile_not_found_and_nothing_created(sandbox) -> None:  # type: ignore[no-untyped-def]
    path = make_profile(brave_dir())
    a = ChromiumAdapter()
    t = target_for(a, "brave:Default")
    path.unlink()
    for call in (lambda: a.snapshot(t), lambda: a.plan(t, FONTS, CFG)):
        with pytest.raises(ScribeSenseError) as exc:
            call()
        assert exc.value.code is ErrorCode.PROFILE_NOT_FOUND
    assert not path.exists()


def test_preferences_vanishing_after_snapshot_is_changed_and_not_recreated(sandbox) -> None:  # type: ignore[no-untyped-def]
    path = make_profile(brave_dir())
    a = ChromiumAdapter()
    t = target_for(a, "brave:Default")
    a.snapshot(t)
    [p] = a.plan(t, FONTS, CFG)
    path.unlink()
    with pytest.raises(ScribeSenseError) as exc:
        a.apply(p)
    assert exc.value.code is ErrorCode.CHANGED_DURING_APPLY
    assert not path.exists()


def test_apply_without_snapshot_is_refused(sandbox) -> None:  # type: ignore[no-untyped-def]
    make_profile(brave_dir())
    a = ChromiumAdapter()
    [p] = a.plan(target_for(a, "brave:Default"), FONTS, CFG)
    with pytest.raises(ValueError):
        ChromiumAdapter().apply(p)


# ---------------------------------------------------------------- the edit itself

def plan_payload(data: bytes) -> bytes:
    make_profile(brave_dir(), "Default", data)
    a = ChromiumAdapter()
    [p] = a.plan(target_for(a, "brave:Default"), FONTS, CFG)
    assert isinstance(p.payload, bytes)
    assert p.new_sha256 == state_sha256(True, p.payload)
    return p.payload


def test_sets_exactly_the_two_keys_creating_missing_dicts_in_chrome_order() -> None:
    assert plan_payload(NO_WEBKIT) == NO_WEBKIT_APPLIED


def test_existing_fonts_and_other_scripts_are_kept() -> None:
    out = json.loads(plan_payload(WITH_FONTS))
    fonts = out["webkit"]["webprefs"]["fonts"]
    assert fonts == {
        "fixed": {"Zyyy": "DejaVu Sans Mono"},
        "sansserif": {"Arab": "Noto Naskh Arabic", "Zyyy": FAM},
        "serif": {"Zyyy": "Times"},
        "standard": {"Hani": "Noto Sans CJK SC", "Zyyy": FAM},
    }
    assert out["webkit"]["webprefs"]["default_font_size"] == 18


@pytest.mark.parametrize("start", sorted(START_STATES))
def test_every_other_key_and_value_survives(start: str) -> None:
    original = json.loads(START_STATES[start][0])
    assert json.loads(plan_payload(START_STATES[start][0])) == expected_after(original)


def test_untouched_bytes_round_trip_exactly_in_chrome_style() -> None:
    """Unicode stays raw UTF-8, '<' / U+2028 / control chars stay escaped as Chrome writes them, floats keep form."""
    out = plan_payload(UNICODE)
    webkit = b'"webkit":{"webprefs":{"fonts":{"sansserif":{"Zyyy":"AccessSans-abc123"},"standard":{"Zyyy":"AccessSans-abc123"}}}},'
    assert out == UNICODE.replace(b'"x":[', webkit + b'"x":[')
    assert "Zoë 李".encode() in out and b"\\u003C" in out and b"<" not in out
    assert not out.endswith(b"\n")


def test_unsorted_dicts_keep_their_order_and_new_keys_are_appended() -> None:
    out = plan_payload(b'{"zeta":1,"alpha":{"b":1,"a":2}}')
    assert out == (b'{"zeta":1,"alpha":{"b":1,"a":2},"webkit":{"webprefs":{"fonts":{"sansserif":{"Zyyy":"AccessSans-abc123"},'
                   b'"standard":{"Zyyy":"AccessSans-abc123"}}}}}')


def test_existing_value_keeps_its_position() -> None:
    data = b'{"webkit":{"webprefs":{"fonts":{"standard":{"Zyyy":"Old","Arab":"A"},"sansserif":{"Zyyy":"Old"}}}}}'
    out = plan_payload(data)
    assert out == data.replace(b'"Old"', b'"AccessSans-abc123"')


def test_plan_is_deterministic_and_idempotent(sandbox) -> None:  # type: ignore[no-untyped-def]
    path = make_profile(brave_dir(), "Default", UNICODE)
    a = ChromiumAdapter()
    t = target_for(a, "brave:Default")
    [p1] = a.plan(t, FONTS, CFG)
    [p2] = a.plan(t, FONTS, CFG)
    assert p1 == p2
    snap_plan_apply(a, t)
    [p3] = a.plan(t, FONTS, CFG)
    assert p3.payload == path.read_bytes() == p1.payload


def test_serializer_round_trips_lone_surrogates_and_big_numbers() -> None:
    data = b'{"a":"\\uD800x","b":123456789012345678901234567890,"c":-0.5,"d":1e-07}'
    value = cr.parse_preferences(data)
    assert cr.parse_preferences(cr.serialize_preferences(value)) == value


@pytest.mark.parametrize("bad", [
    b"",
    b"not json",
    b'{"truncated":',
    b"[1,2,3]",
    b'"a string"',
    b'{"a":NaN}',
    b'{"a":Infinity}',
    b"\xef\xbb\xbf{}",  # BOM
    b'{"a":"\xff"}',  # invalid UTF-8
    b'{"webkit":"not a dict"}',
    b'{"webkit":{"webprefs":{"fonts":{"standard":["x"]}}}}',
    b'{"webkit":null}',
    b"[" * 100000 + b"]" * 100000,
])
def test_unreadable_preferences_are_refused_and_left_untouched(sandbox, bad: bytes) -> None:  # type: ignore[no-untyped-def]
    path = make_profile(brave_dir(), "Default", bad)
    before = fingerprint_tree(sandbox.home)
    a = ChromiumAdapter()
    t = target_for(a, "brave:Default")
    a.snapshot(t)
    with pytest.raises(ScribeSenseError) as exc:
        a.plan(t, FONTS, CFG)
    assert exc.value.code is ErrorCode.PREFERENCES_UNREADABLE
    assert path.read_bytes() == bad
    assert fingerprint_tree(sandbox.home) == before
    result = a.verify(t, FONTS)
    parses = bad.startswith(b'{"webkit"')  # valid JSON whose shape we refuse to edit: a plain mismatch
    expected = ErrorCode.ADAPTER_VERIFY_FAILED if parses else ErrorCode.CHECK_UNAVAILABLE
    assert (result.chosen_ok, result.detail_code) == (False, expected.value)


def test_apply_refuses_a_payload_that_is_not_preferences(sandbox) -> None:  # type: ignore[no-untyped-def]
    path = make_profile(brave_dir())
    a = ChromiumAdapter()
    t = target_for(a, "brave:Default")
    a.snapshot(t)
    [p] = a.plan(t, FONTS, CFG)
    forged = type(p)(p.target, p.location, b"{}garbage", state_sha256(True, b"{}garbage"))
    with pytest.raises(ScribeSenseError) as exc:
        a.apply(forged)
    assert exc.value.code is ErrorCode.PREFERENCES_UNREADABLE
    assert path.read_bytes() == NO_WEBKIT


@pytest.mark.parametrize("family", ["Arial", "AccessSans-", "AccessSansUI-abc123", "AccessSans-a\nb", ""])
def test_only_reading_families_are_written(sandbox, family: str) -> None:  # type: ignore[no-untyped-def]
    make_profile(brave_dir())
    a = ChromiumAdapter()
    fonts = FontSet("k", family, "AccessSansUI-k", (), ())
    with pytest.raises(ScribeSenseError) as exc:
        a.plan(target_for(a, "brave:Default"), fonts, CFG)
    assert exc.value.code is ErrorCode.UNEXPECTED_ADAPTER_ERROR


# ---------------------------------------------------------------- browser running

def test_running_browser_is_never_edited(sandbox) -> None:  # type: ignore[no-untyped-def]
    path = make_profile(brave_dir())
    a = ChromiumAdapter()
    t = target_for(a, "brave:Default")
    items = a.snapshot(t)
    [p] = a.plan(t, FONTS, CFG)
    set_running(brave_dir())
    before = fingerprint_tree(sandbox.home)
    with pytest.raises(ScribeSenseError) as exc:
        a.apply(p)
    assert exc.value.code is ErrorCode.BROWSER_RUNNING
    assert fingerprint_tree(sandbox.home) == before
    with pytest.raises(ScribeSenseError) as exc:
        a.revert(items[0], NO_WEBKIT)
    assert exc.value.code is ErrorCode.BROWSER_RUNNING
    assert path.read_bytes() == NO_WEBKIT


def test_another_browser_running_does_not_block(sandbox) -> None:  # type: ignore[no-untyped-def]
    path = make_profile(brave_dir())
    make_profile(sandbox.config_home / "chromium")
    set_running(sandbox.config_home / "chromium")
    a = ChromiumAdapter()
    snap_plan_apply(a, target_for(a, "brave:Default"))
    assert path.read_bytes() == NO_WEBKIT_APPLIED


# ---------------------------------------------------------------- apply / revert details

def test_each_profile_is_its_own_target_and_location(sandbox) -> None:  # type: ignore[no-untyped-def]
    p0 = make_profile(brave_dir(), "Default", NO_WEBKIT)
    p1 = make_profile(brave_dir(), "Profile 1", WITH_FONTS)
    a = ChromiumAdapter()
    snap_plan_apply(a, target_for(a, "brave:Profile_1"))
    assert p0.read_bytes() == NO_WEBKIT
    assert json.loads(p1.read_bytes())["webkit"]["webprefs"]["fonts"]["standard"]["Zyyy"] == FAM


def test_mode_is_preserved(sandbox) -> None:  # type: ignore[no-untyped-def]
    path = make_profile(brave_dir(), mode=0o640)
    a = ChromiumAdapter()
    t = target_for(a, "brave:Default")
    [(item, backup)] = snapshot_with_backups(_Harness(a), t)
    snap_plan_apply(a, t)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    path.chmod(0o600)
    a.revert(item, backup)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640 and path.read_bytes() == NO_WEBKIT


@dataclass
class _Harness:
    adapter: ChromiumAdapter


def test_symlinked_preferences_stay_a_symlink(sandbox) -> None:  # type: ignore[no-untyped-def]
    real = sandbox.root / "dotfiles" / "Preferences"
    real.parent.mkdir(parents=True)
    real.write_bytes(NO_WEBKIT)
    real.chmod(0o640)
    link = brave_dir() / "Default" / "Preferences"
    link.parent.mkdir(parents=True)
    os.symlink(real, link)
    a = ChromiumAdapter()
    t = target_for(a, "brave:Default")
    [(item, backup)] = snapshot_with_backups(_Harness(a), t)
    assert item.file_mode == 0o640  # the real file's mode, not the link's
    snap_plan_apply(a, t)
    assert link.is_symlink() and real.read_bytes() == NO_WEBKIT_APPLIED
    assert stat.S_IMODE(real.stat().st_mode) == 0o640
    a.revert(item, backup)
    assert link.is_symlink() and real.read_bytes() == NO_WEBKIT
    assert stat.S_IMODE(real.stat().st_mode) == 0o640


def test_adapter_source_has_no_hidden_line_breaks_or_noncharacters() -> None:
    """U+2028/U+2029 and friends in the source are line breaks to editors and str.splitlines()."""
    text = Path(cr.__file__).read_text(encoding="utf-8")
    assert len(text.splitlines()) == len(text.split("\n")) - (1 if text.endswith("\n") else 0)
    bad = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\ufeff\ufffe\uffff]")
    assert not bad.search(text)


def test_escape_patterns_still_cover_the_chrome_escape_set() -> None:
    for ch in ('"', "\\", "\x00", "\x1f", "<", "\u2028", "\u2029", "\ud800", "\udfff"):
        assert cr._ESCAPE.fullmatch(ch), repr(ch)
    for ch in ("a", "\x7f", ">", "\u2027", "\u202a", "\ue000", "\u00e9"):
        assert not cr._ESCAPE.fullmatch(ch), repr(ch)
    for ch in ("\x00", "\x1f", "\x7f", "\ud800", "\ufffe", "\uffff"):
        assert cr._BAD_CHAR.search(ch), repr(ch)
    for ch in ("a", "\u00e9", "\ufffd", "\u2028"):
        assert not cr._BAD_CHAR.search(ch), repr(ch)


def test_revert_of_a_never_existing_file_deletes_nothing(sandbox) -> None:  # type: ignore[no-untyped-def]
    path = make_profile(brave_dir())
    t = Target("chromium", "brave:Default", "Brave (Default)", False)
    item = SnapshotItem(t, "file", str(path), False, None, state_sha256(False, None), None, None)
    ChromiumAdapter().revert(item, None)
    assert path.read_bytes() == NO_WEBKIT


def test_failed_fsync_is_write_not_durable_and_original_kept(sandbox, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    path = make_profile(brave_dir())
    a = ChromiumAdapter()
    t = target_for(a, "brave:Default")
    a.snapshot(t)
    [p] = a.plan(t, FONTS, CFG)

    def broken_fsync(_fd: int) -> None:
        raise OSError(5, "I/O error")

    monkeypatch.setattr(os, "fsync", broken_fsync)
    with pytest.raises(ScribeSenseError) as exc:
        a.apply(p)
    assert exc.value.code is ErrorCode.WRITE_NOT_DURABLE
    assert path.read_bytes() == NO_WEBKIT
    assert sorted(os.listdir(path.parent)) == ["Preferences"]  # temp file cleaned up


# ---------------------------------------------------------------- verify

def test_verify_is_a_configuration_check(sandbox) -> None:  # type: ignore[no-untyped-def]
    path = make_profile(brave_dir(), "Default", WITH_FONTS)
    a = ChromiumAdapter()
    t = target_for(a, "brave:Default")
    before = a.verify(t, FONTS)
    assert (before.chosen_ok, before.drawn_ok, before.detail_code) == (False, None, "ADAPTER_VERIFY_FAILED")
    snap_plan_apply(a, t)
    after = a.verify(t, FONTS)
    assert (after.target, after.chosen_ok, after.drawn_ok, after.detail_code) == (t, True, None, "OK")
    # only one of the two keys -> not OK
    prefs = json.loads(path.read_bytes())
    prefs["webkit"]["webprefs"]["fonts"]["sansserif"]["Zyyy"] = "Arial"
    path.write_bytes(json.dumps(prefs).encode())
    assert a.verify(t, FONTS).chosen_ok is False
    path.unlink()
    gone = a.verify(t, FONTS)
    assert (gone.chosen_ok, gone.detail_code) == (False, "CHECK_UNAVAILABLE")


def test_no_process_is_ever_started(sandbox, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import subprocess

    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("the Chromium adapter must not start processes")

    monkeypatch.setattr(subprocess.Popen, "__init__", refuse)
    make_profile(brave_dir())
    a = ChromiumAdapter()
    t = target_for(a, "brave:Default")
    [(item, backup)] = snapshot_with_backups(_Harness(a), t)
    snap_plan_apply(a, t)
    a.verify(t, FONTS)
    a.revert(item, backup)


# ---------------------------------------------------------------- integration (bubblewrap only)

def _brave_binary() -> str | None:
    for name in ("brave-browser", "brave", "brave-browser-stable"):
        found = shutil.which(name)
        if found:
            return found
    return None


@pytest.mark.integration
def test_real_brave_keeps_the_edit_and_still_starts(sandbox) -> None:  # type: ignore[no-untyped-def]
    """Brave writes its own Preferences (never an invented one), the adapter edits it while closed, and
    Brave starts again without hanging and keeps both keys. The drawn check (headless Brave draws the
    reading family for sans-serif) still needs a CDP/screenshot probe — see the card's acceptance test."""
    binary = _brave_binary()
    if binary is None:
        pytest.skip("Brave is not installed in the integration sandbox")
    data_dir = brave_dir()
    argv = [binary, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
            f"--user-data-dir={data_dir}", "--dump-dom", "about:blank"]
    run_cmd(argv, 60.0)
    if not (data_dir / "Default" / "Preferences").is_file():
        pytest.skip("headless Brave did not write a Preferences file")
    a = ChromiumAdapter()
    t = target_for(a, "brave:Default")
    assert not t.running
    snap_plan_apply(a, t)
    run_cmd(argv, 60.0)  # must not hang (ADAPTER_TIMEOUT would raise)
    assert a.verify(t, FONTS).chosen_ok
