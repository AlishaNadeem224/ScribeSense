"""M4.6 — Firefox-family adapter: conformance (M4.1) + card acceptance tests, in the unit sandbox.

No process is launched and no real profile is touched: every profile lives under the sandbox HOME.
The drawn check (headless Firefox draws the reading family) is the render harness (M3.4), not a unit test.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from scribesense.adapters import firefox as ff
from scribesense.adapters.browser_css import browser_css
from scribesense.adapters.conformance import AdapterConformance
from scribesense.adapters.firefox import FirefoxAdapter
from scribesense.contracts.adapter import Adapter, Target, state_sha256
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import FontSet
from scribesense.log import log_path

CFG = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.0)
FONTS = FontSet("abc123", "AccessSans-abc123", "AccessSansUI-abc123", (), ("Regular",))
FONTS2 = FontSet("def456", "AccessSans-def456", "AccessSansUI-def456", (), ("Regular",))
CFG2 = Configuration("Lexend", 0.10, 1.25, 1.8, 1.2, 1.0)

PREF = b'user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);'
USER_JS = b'// my prefs\nuser_pref("browser.startup.page", 3);\nuser_pref("a.b", "x");'  # no final newline
USER_CSS = b'@-moz-document domain(example.org) {\n  body { color: red; }\n}\n'
STALE_JS = (b'user_pref("x.y", 1);\n// >>> ScribeSense (managed) >>>\nuser_pref("old.stuff", false);\n'
            b'// <<< ScribeSense <<<\nuser_pref("z", 2);\n')
STALE_CSS = (b"a { color: blue; }\n/* >>> ScribeSense (managed) >>> */\n:root { font-family: \"AccessSans-old\" !important; }\n"
             b"/* <<< ScribeSense <<< */\nb { color: green; }\n")


ID_RE = re.compile(r"^firefox:[a-z-]+:[0-9a-f]{12}$")


# ---------------------------------------------------------------- sandbox profiles

def tid(label: str, directory: Path) -> str:
    """The expected target id: root label + first 12 hex of sha256(absolute profile path)."""
    return f"firefox:{label}:" + hashlib.sha256(os.fsencode(str(directory))).hexdigest()[:12]


def root_path(label: str) -> Path:
    return {r.label: r.path for r in ff.install_roots()}[label]


def write_ini(root: Path, sections: list[dict[str, str]], extra: str = "") -> None:
    root.mkdir(parents=True, exist_ok=True)
    text = "[General]\nStartWithLastProfile=1\nVersion=2\n\n"
    for i, sec in enumerate(sections):
        text += f"[Profile{i}]\n" + "".join(f"{k}={v}\n" for k, v in sec.items()) + "\n"
    (root / "profiles.ini").write_text(text + extra)


def make_profile(label: str, dirname: str, name: str = "default", *, absolute: Path | None = None,
                 sections: list[dict[str, str]] | None = None) -> Path:
    """One profile in the given root; returns its directory."""
    root = root_path(label)
    if absolute is not None:
        directory = absolute
        sec = {"Name": name, "IsRelative": "0", "Path": str(absolute)}
    else:
        directory = root / dirname
        sec = {"Name": name, "IsRelative": "1", "Path": dirname}
    directory.mkdir(parents=True, exist_ok=True)
    write_ini(root, (sections or []) + [sec])
    return directory


def write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def snap_plan_apply(adapter: FirefoxAdapter, t: Target, fonts: FontSet = FONTS, cfg: Configuration = CFG):  # type: ignore[no-untyped-def]
    items = adapter.snapshot(t)
    planned = adapter.plan(t, fonts, cfg)
    for p in planned:
        adapter.apply(p)
    return items, planned


def fingerprint_home(home: Path) -> dict[str, str]:
    """Every file AND symlink under HOME, with mode (symlinks by their link text)."""
    out: dict[str, str] = {}
    for dirpath, dirs, files in os.walk(home):
        for name in files + [d for d in dirs if (Path(dirpath) / d).is_symlink()]:
            p = Path(dirpath) / name
            if p.is_symlink():
                out[str(p)] = "link:" + os.readlink(p)
            else:
                out[str(p)] = f"{state_sha256(True, p.read_bytes())}:{stat.S_IMODE(p.stat().st_mode):o}"
    return out


# ---------------------------------------------------------------- conformance

START_STATES = ("empty-profile", "existing-files", "stale-block", "two-profiles")


@dataclass
class FirefoxHarness:
    adapter: FirefoxAdapter
    home: Path
    fonts: FontSet = FONTS
    config: Configuration = CFG

    def fingerprint(self) -> dict[str, str]:
        out = fingerprint_home(self.home)
        for t in self.adapter.detect():  # every location, absent ones as absent
            profile = self.adapter._profile(t).path
            for loc in (ff.user_js_path(profile), ff.user_content_path(profile)):
                out.setdefault(str(loc), state_sha256(False, None))
        return out

    def external_edit(self, location: str) -> None:
        path = Path(location)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"/* changed by someone else */\n")


def _setup(start: str) -> None:
    if start == "two-profiles":
        root = root_path("zen-flatpak")
        (root / "aaaa1111.Default Profile").mkdir(parents=True)
        (root / "bbbb2222.Work").mkdir(parents=True)
        write_ini(root, [{"Name": "Default Profile", "IsRelative": "1", "Path": "aaaa1111.Default Profile"},
                         {"Name": "Work", "IsRelative": "1", "Path": "bbbb2222.Work"}])
        write(root / "bbbb2222.Work" / "user.js", USER_JS)
        return
    profile = make_profile("firefox", "abcd1234.default-release")
    if start == "existing-files":
        write(profile / "user.js", USER_JS, 0o600)
        write(profile / "chrome" / "userContent.css", USER_CSS, 0o640)
    elif start == "stale-block":
        write(profile / "user.js", STALE_JS)
        write(profile / "chrome" / "userContent.css", STALE_CSS)


class TestFirefoxConformance(AdapterConformance):
    @pytest.fixture(params=START_STATES)
    def conformance_harness(self, request: pytest.FixtureRequest, sandbox) -> FirefoxHarness:  # type: ignore[no-untyped-def]
        _setup(request.param)
        return FirefoxHarness(FirefoxAdapter(), sandbox.home)


# ---------------------------------------------------------------- detect

def test_attributes() -> None:
    a = FirefoxAdapter()
    _typed: Adapter = a  # static K3 conformance
    assert (a.name, a.requires_restart, a.must_be_closed) == ("firefox", True, True)


def test_no_install_means_no_targets(sandbox) -> None:  # type: ignore[no-untyped-def]
    assert FirefoxAdapter().detect() == []


def test_roots_are_under_home(sandbox) -> None:  # type: ignore[no-untyped-def]
    h = sandbox.home
    assert {r.label: r.path for r in ff.install_roots()} == {
        "firefox": h / ".mozilla" / "firefox",
        "firefox-xdg": h / ".config" / "mozilla" / "firefox",
        "firefox-flatpak": h / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",
        "zen-flatpak": h / ".var" / "app" / "app.zen_browser.zen" / ".zen",
        "zen": h / ".zen",
    }


def test_every_root_and_profile_found_relative_and_absolute(sandbox) -> None:  # type: ignore[no-untyped-def]
    make_profile("firefox", "aaaa.default-release", "default-release")
    elsewhere = sandbox.home / "profiles" / "bbbb.custom"
    make_profile("firefox-flatpak", "", "custom", absolute=elsewhere)
    zen_root = root_path("zen-flatpak")
    (zen_root / "cccc.Default Profile").mkdir(parents=True)
    (zen_root / "dddd.Default (release)").mkdir(parents=True)
    write_ini(zen_root, [{"Name": "Default Profile", "IsRelative": "1", "Path": "cccc.Default Profile"},
                         {"Name": "Default (release)", "IsRelative": "1", "Path": "dddd.Default (release)",
                          "Default": "1"}],
              extra="[Install2953CB39A2589173]\nDefault=cccc.Default Profile\nLocked=1\n")
    make_profile("zen", "eeee.default", "default")
    targets = FirefoxAdapter().detect()
    ids = [t.target_id for t in targets]
    assert ids == [
        tid("firefox", root_path("firefox") / "aaaa.default-release"),
        tid("firefox-flatpak", elsewhere),
        tid("zen-flatpak", zen_root / "cccc.Default Profile"),
        tid("zen-flatpak", zen_root / "dddd.Default (release)"),
        tid("zen", root_path("zen") / "eeee.default"),
    ]
    assert len(set(ids)) == 5
    for t_id in ids:
        assert ID_RE.match(t_id), t_id
    names = [t.display_name for t in targets]
    assert names == ["Firefox (default-release)", "Firefox Flatpak (custom)", "Zen Flatpak (Default Profile)",
                     "Zen Flatpak (Default (release))", "Zen (default)"]
    assert not any(t.running for t in targets)


def test_listed_but_missing_profile_dirs_and_bad_paths_are_skipped(sandbox) -> None:  # type: ignore[no-untyped-def]
    root = root_path("firefox")
    (root / "ok.default").mkdir(parents=True)
    write_ini(root, [{"Name": "gone", "IsRelative": "1", "Path": "missing.default"},
                     {"Name": "escape", "IsRelative": "1", "Path": "../outside"},
                     {"Name": "nopath", "IsRelative": "1"},
                     {"Name": "ok", "IsRelative": "1", "Path": "ok.default"},
                     {"Name": "dup", "IsRelative": "0", "Path": str(root / "ok.default")}])
    (sandbox.home / ".mozilla" / "outside").mkdir()
    assert [t.target_id for t in FirefoxAdapter().detect()] == [tid("firefox", root / "ok.default")]


def test_same_dir_name_in_two_absolute_paths_gets_distinct_ids(sandbox) -> None:  # type: ignore[no-untyped-def]
    a, b = sandbox.home / "x" / "p.default", sandbox.home / "y" / "p.default"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    write_ini(root_path("firefox"), [{"Name": "one", "IsRelative": "0", "Path": str(a)},
                                     {"Name": "two", "IsRelative": "0", "Path": str(b)}])
    ids = [t.target_id for t in FirefoxAdapter().detect()]
    assert ids == [tid("firefox", a), tid("firefox", b)] and len(set(ids)) == 2


# ---------------------------------------------------------------- target id privacy (decision 2026-10-12)

def test_target_ids_carry_no_part_of_the_profile_name(sandbox) -> None:  # type: ignore[no-untyped-def]
    root = root_path("firefox")
    (root / "xxxx.Jane-Work").mkdir(parents=True)
    (root / "qz9k.Jane Private (old)").mkdir(parents=True)
    write_ini(root, [{"Name": "Jane-Work", "IsRelative": "1", "Path": "xxxx.Jane-Work"},
                     {"Name": "Jane Private (old)", "IsRelative": "1", "Path": "qz9k.Jane Private (old)"}])
    targets = FirefoxAdapter().detect()
    assert len(targets) == 2
    for t in targets:
        assert ID_RE.match(t.target_id), t.target_id
        h = t.target_id.rsplit(":", 1)[1]
        for part in ("Jane", "Work", "Private", "old", "xxxx", "qz9k", str(sandbox.home)):
            assert part not in t.target_id
        assert len(h) == 12
    assert [t.display_name for t in targets] == ["Firefox (Jane-Work)", "Firefox (Jane Private (old))"]


def test_target_ids_are_stable_and_derived_from_the_absolute_path(sandbox) -> None:  # type: ignore[no-untyped-def]
    profile = make_profile("firefox", "abcd.default-release")
    first = [t.target_id for t in FirefoxAdapter().detect()]
    second = [t.target_id for t in FirefoxAdapter().detect()]
    (profile / "user.js").write_bytes(USER_JS)  # content changes never change the id
    third = [t.target_id for t in FirefoxAdapter().detect()]
    expected = "firefox:firefox:" + hashlib.sha256(os.fsencode(str(profile))).hexdigest()[:12]
    assert first == second == third == [expected]


def test_symlinked_profile_dir_hashes_the_listed_path_not_the_realpath(sandbox) -> None:  # type: ignore[no-untyped-def]
    real = sandbox.home / "store" / "real.default"
    real.mkdir(parents=True)
    root = root_path("firefox")
    root.mkdir(parents=True)
    os.symlink(real, root / "link.default")
    write_ini(root, [{"Name": "x", "IsRelative": "1", "Path": "link.default"}])
    assert [t.target_id for t in FirefoxAdapter().detect()] == [tid("firefox", root / "link.default")]


def test_distinct_profiles_get_distinct_ids_across_roots(sandbox) -> None:  # type: ignore[no-untyped-def]
    for label in ("firefox", "firefox-xdg", "firefox-flatpak", "zen-flatpak", "zen"):
        root = root_path(label)
        for d in ("a.default", "b.default"):
            (root / d).mkdir(parents=True)
        write_ini(root, [{"Name": "default", "IsRelative": "1", "Path": "a.default"},
                         {"Name": "default", "IsRelative": "1", "Path": "b.default"}])
    ids = [t.target_id for t in FirefoxAdapter().detect()]
    assert len(ids) == 10 and len(set(ids)) == 10


def test_hash_collision_never_gives_two_profiles_one_id(sandbox, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = root_path("firefox")
    for d in ("a.default", "b.default"):
        (root / d).mkdir(parents=True)
    write_ini(root, [{"Name": "a", "IsRelative": "1", "Path": "a.default"},
                     {"Name": "b", "IsRelative": "1", "Path": "b.default"}])
    monkeypatch.setattr(ff, "profile_hash", lambda directory: "0" * 12)
    targets = FirefoxAdapter().detect()
    assert [(t.target_id, t.display_name) for t in targets] == [("firefox:firefox:" + "0" * 12, "Firefox (a)")]
    records = [json.loads(line) for line in log_path().read_text().splitlines()]
    assert [r["reason"] for r in records] == ["target-id-collision"]
    assert "b.default" not in log_path().read_text()


# ---------------------------------------------------------------- XDG install root (decision 2026-10-12)

def test_xdg_root_profiles_are_discovered(sandbox) -> None:  # type: ignore[no-untyped-def]
    xdg = sandbox.home / ".config" / "mozilla" / "firefox"
    assert root_path("firefox-xdg") == xdg
    profile = make_profile("firefox-xdg", "wxyz.default-release", "default-release")
    a = FirefoxAdapter()
    targets = a.detect()
    assert [(t.target_id, t.display_name, t.running) for t in targets] == [
        (tid("firefox-xdg", profile), "Firefox (default-release)", False)]
    items, _ = snap_plan_apply(a, targets[0])
    assert [i.location for i in items] == [str(profile / "user.js"), str(profile / "chrome" / "userContent.css")]
    assert a.verify(targets[0], FONTS).chosen_ok is True


def test_xdg_root_absent_means_no_targets_from_it(sandbox) -> None:  # type: ignore[no-untyped-def]
    (sandbox.home / ".config" / "mozilla").mkdir(parents=True)  # parent exists, firefox/ does not
    make_profile("firefox", "n.default")
    assert [t.target_id.split(":")[1] for t in FirefoxAdapter().detect()] == ["firefox"]


def test_native_and_xdg_roots_side_by_side_are_distinct(sandbox) -> None:  # type: ignore[no-untyped-def]
    native = make_profile("firefox", "same.default")
    xdg = make_profile("firefox-xdg", "same.default")
    ids = [t.target_id for t in FirefoxAdapter().detect()]
    assert ids == [tid("firefox", native), tid("firefox-xdg", xdg)]


def test_xdg_root_symlinked_to_native_is_reported_once(sandbox) -> None:  # type: ignore[no-untyped-def]
    native = make_profile("firefox", "p.default")
    xdg_parent = sandbox.home / ".config" / "mozilla"
    xdg_parent.mkdir(parents=True)
    os.symlink(root_path("firefox"), xdg_parent / "firefox")
    assert [t.target_id for t in FirefoxAdapter().detect()] == [tid("firefox", native)]


@pytest.mark.parametrize("ini", [
    b"\xff\xfe garbage \x00",
    b"[Profile0\nPath=x\n",
    b"no section header\nPath=x\n",
    b"",
    b"[Profile0]\nPath\n",
])
def test_malformed_profiles_ini_gives_no_targets_and_no_crash(sandbox, ini: bytes) -> None:  # type: ignore[no-untyped-def]
    root = root_path("firefox")
    (root / "x").mkdir(parents=True)
    (root / "profiles.ini").write_bytes(ini)
    zen = make_profile("zen", "z.default")  # other roots still work
    assert [t.target_id for t in FirefoxAdapter().detect()] == [tid("zen", zen)]


def test_malformed_ini_log_has_no_content(sandbox) -> None:  # type: ignore[no-untyped-def]
    root = root_path("firefox")
    root.mkdir(parents=True)
    (root / "profiles.ini").write_bytes(b"[Profile0\nName=SecretName\n")
    FirefoxAdapter().detect()
    log_file = log_path()
    assert log_file.is_file(), "a malformed profiles.ini must be logged"
    text = log_file.read_text()
    records = [json.loads(line) for line in text.splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record["code"] == ErrorCode.PROFILE_NOT_FOUND.value
    assert record["category"] == "adapter"
    assert record["adapter"] == "firefox"
    assert record["target"] == "firefox"
    assert record["reason"] == "profiles-ini-malformed"
    assert "dropped" not in record
    assert "SecretName" not in text and str(sandbox.home) not in text


# ---------------------------------------------------------------- running browser

def test_lock_symlink_means_running_and_parentlock_does_not(sandbox) -> None:  # type: ignore[no-untyped-def]
    profile = make_profile("firefox", "abcd.default")
    (profile / ".parentlock").write_bytes(b"")
    a = FirefoxAdapter()
    assert a.detect()[0].running is False
    os.symlink("127.0.1.1:+4242", profile / "lock")  # dangling, as Firefox leaves it
    assert a.detect()[0].running is True


def test_apply_refuses_while_running_and_writes_nothing(sandbox) -> None:  # type: ignore[no-untyped-def]
    profile = make_profile("firefox", "abcd.default")
    write(profile / "user.js", USER_JS)
    a = FirefoxAdapter()
    t = a.detect()[0]
    a.snapshot(t)
    planned = a.plan(t, FONTS, CFG)
    os.symlink("127.0.1.1:+4242", profile / "lock")
    before = fingerprint_home(sandbox.home)
    for p in planned:
        with pytest.raises(ScribeSenseError) as exc:
            a.apply(p)
        assert exc.value.code is ErrorCode.BROWSER_RUNNING
    assert fingerprint_home(sandbox.home) == before
    assert not (profile / "chrome").exists()


def test_revert_is_allowed_while_the_browser_runs(sandbox) -> None:  # type: ignore[no-untyped-def]
    """Decision 2026-10-12: Firefox never rewrites user.js or userContent.css, so reverting while it runs is
    harmless, and a stale lock left by a crash must never block recovery."""
    profile = make_profile("firefox", "abcd.default")
    write(profile / "user.js", USER_JS, 0o600)
    a = FirefoxAdapter()
    t = a.detect()[0]
    items, _ = snap_plan_apply(a, t)
    os.symlink("127.0.1.1:+4242", profile / "lock")
    assert a.detect()[0].running is True
    for item in items:
        a.revert(item, USER_JS if item.existed else None)
    assert (profile / "user.js").read_bytes() == USER_JS
    assert stat.S_IMODE((profile / "user.js").stat().st_mode) == 0o600
    assert not (profile / "chrome" / "userContent.css").exists()
    assert os.path.lexists(profile / "lock")  # the browser's lock is never touched


# ---------------------------------------------------------------- what is written

def _apply_one(sandbox, user_js: bytes | None = None, user_css: bytes | None = None):  # type: ignore[no-untyped-def]
    profile = make_profile("firefox", "abcd.default")
    if user_js is not None:
        write(profile / "user.js", user_js)
    if user_css is not None:
        write(profile / "chrome" / "userContent.css", user_css)
    a = FirefoxAdapter()
    t = a.detect()[0]
    snap_plan_apply(a, t)
    return a, t, profile


def test_fresh_profile_gets_exactly_the_managed_blocks(sandbox) -> None:  # type: ignore[no-untyped-def]
    _, _, profile = _apply_one(sandbox)
    assert (profile / "user.js").read_bytes() == b"// >>> ScribeSense (managed) >>>\n" + PREF + b"\n// <<< ScribeSense <<<\n"
    css = (profile / "chrome" / "userContent.css").read_bytes()
    assert css == (b"/* >>> ScribeSense (managed) >>> */\n" + browser_css(FONTS, CFG).encode()
                   + b"/* <<< ScribeSense <<< */\n")
    assert stat.S_IMODE((profile / "user.js").stat().st_mode) == 0o644


def test_existing_content_is_byte_identical_outside_the_block(sandbox) -> None:  # type: ignore[no-untyped-def]
    _, _, profile = _apply_one(sandbox, USER_JS, USER_CSS)
    js = (profile / "user.js").read_bytes()
    assert js.startswith(USER_JS + b"\n// >>> ScribeSense (managed) >>>\n")  # separator only because no final newline
    assert js.count(PREF) == 1
    css = (profile / "chrome" / "userContent.css").read_bytes()
    assert css.startswith(USER_CSS + b"/* >>> ScribeSense (managed) >>> */\n")
    assert browser_css(FONTS, CFG).encode() in css


def test_stale_block_is_replaced_in_place_and_others_kept(sandbox) -> None:  # type: ignore[no-untyped-def]
    _, _, profile = _apply_one(sandbox, STALE_JS, STALE_CSS)
    js = (profile / "user.js").read_bytes()
    assert js == (b'user_pref("x.y", 1);\n// >>> ScribeSense (managed) >>>\n' + PREF + b"\n// <<< ScribeSense <<<\n"
                  b'user_pref("z", 2);\n')
    css = (profile / "chrome" / "userContent.css").read_bytes()
    assert b"AccessSans-old" not in css
    assert css.startswith(b"a { color: blue; }\n/* >>> ScribeSense (managed) >>> */\n")
    assert css.endswith(b"/* <<< ScribeSense <<< */\nb { color: green; }\n")


def test_reapply_has_no_duplicates_and_switches_preset(sandbox) -> None:  # type: ignore[no-untyped-def]
    a, t, profile = _apply_one(sandbox, USER_JS, USER_CSS)
    first_js = (profile / "user.js").read_bytes()
    snap_plan_apply(a, t, FONTS2, CFG2)
    js = (profile / "user.js").read_bytes()
    css = (profile / "chrome" / "userContent.css").read_bytes()
    assert js == first_js and js.count(b"ScribeSense (managed)") == 1
    assert css.count(b"ScribeSense (managed)") == 1
    assert b"AccessSans-abc123" not in css and browser_css(FONTS2, CFG2).encode() in css
    assert css.startswith(USER_CSS)
    snap_plan_apply(a, t, FONTS2, CFG2)
    assert (profile / "chrome" / "userContent.css").read_bytes() == css  # idempotent


def test_duplicate_blocks_collapse_into_one(sandbox) -> None:  # type: ignore[no-untyped-def]
    block = b"// >>> ScribeSense (managed) >>>\nold\n// <<< ScribeSense <<<\n"
    _, _, profile = _apply_one(sandbox, b"a\n" + block + b"b\n" + block + b"c\n")
    js = (profile / "user.js").read_bytes()
    assert js == b"a\n// >>> ScribeSense (managed) >>>\n" + PREF + b"\n// <<< ScribeSense <<<\nb\nc\n"


@pytest.mark.parametrize("damaged", [
    b"x\n// >>> ScribeSense (managed) >>>\nuser_pref(\"a\", 1);\n",  # start without end
    b"x\n// <<< ScribeSense <<<\n",  # end without start
    b"// >>> ScribeSense (managed) >>>\n// >>> ScribeSense (managed) >>>\n// <<< ScribeSense <<<\n",  # nested
])
def test_damaged_markers_refuse_and_write_nothing(sandbox, damaged: bytes) -> None:  # type: ignore[no-untyped-def]
    profile = make_profile("firefox", "abcd.default")
    write(profile / "user.js", damaged)
    a = FirefoxAdapter()
    t = a.detect()[0]
    a.snapshot(t)
    before = fingerprint_home(sandbox.home)
    with pytest.raises(ScribeSenseError) as exc:
        a.plan(t, FONTS, CFG)
    assert exc.value.code is ErrorCode.PREFERENCES_UNREADABLE
    assert fingerprint_home(sandbox.home) == before


def test_css_comes_from_browser_css(sandbox, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(ff, "browser_css", lambda fonts, config: f"/* marker {fonts.key} {config.reading_line} */\n")
    _, _, profile = _apply_one(sandbox)
    css = (profile / "chrome" / "userContent.css").read_bytes()
    assert css == b"/* >>> ScribeSense (managed) >>> */\n/* marker abc123 1.5 */\n/* <<< ScribeSense <<< */\n"


def test_nothing_written_has_letter_or_word_spacing(sandbox) -> None:  # type: ignore[no-untyped-def]
    a, t, profile = _apply_one(sandbox, USER_JS, USER_CSS)
    for p in a.plan(t, FONTS, CFG):
        assert isinstance(p.payload, bytes)
        low = p.payload.lower()
        assert b"letter-spacing" not in low and b"word-spacing" not in low


def test_plan_refuses_a_foreign_family(sandbox) -> None:  # type: ignore[no-untyped-def]
    make_profile("firefox", "abcd.default")
    a = FirefoxAdapter()
    t = a.detect()[0]
    bad = FontSet("k", "Comic Sans", "AccessSansUI-k", (), ())
    with pytest.raises(ScribeSenseError) as exc:
        a.plan(t, bad, CFG)
    assert exc.value.code is ErrorCode.UNEXPECTED_ADAPTER_ERROR


def test_chrome_dir_is_created_and_left_on_revert(sandbox) -> None:  # type: ignore[no-untyped-def]
    profile = make_profile("firefox", "abcd.default")
    a = FirefoxAdapter()
    t = a.detect()[0]
    items, _ = snap_plan_apply(a, t)
    assert (profile / "chrome").is_dir()
    for item in items:
        a.revert(item, None)
    assert (profile / "chrome").is_dir()
    assert not (profile / "user.js").exists() and not (profile / "chrome" / "userContent.css").exists()


def test_symlinked_user_js_is_written_through_and_link_kept(sandbox) -> None:  # type: ignore[no-untyped-def]
    profile = make_profile("firefox", "abcd.default")
    real = sandbox.home / "dotfiles" / "user.js"
    write(real, USER_JS, 0o600)
    os.symlink(real, profile / "user.js")
    a = FirefoxAdapter()
    t = a.detect()[0]
    items = a.snapshot(t)
    backups = [a.read_state(i.location).data if i.existed else None for i in items]
    for p in a.plan(t, FONTS, CFG):
        a.apply(p)
    assert (profile / "user.js").is_symlink() and os.readlink(profile / "user.js") == str(real)
    assert PREF in real.read_bytes() and stat.S_IMODE(real.stat().st_mode) == 0o600
    for item, backup in zip(items, backups):
        a.revert(item, backup)
    assert (profile / "user.js").is_symlink() and real.read_bytes() == USER_JS


def test_revert_refuses_a_corrupt_backup(sandbox) -> None:  # type: ignore[no-untyped-def]
    profile = make_profile("firefox", "abcd.default")
    write(profile / "user.js", USER_JS)
    a = FirefoxAdapter()
    t = a.detect()[0]
    items, _ = snap_plan_apply(a, t)
    written = (profile / "user.js").read_bytes()
    with pytest.raises(ScribeSenseError) as exc:
        a.revert(items[0], b"not the original")
    assert exc.value.code is ErrorCode.BACKUP_CORRUPT
    assert (profile / "user.js").read_bytes() == written


def test_apply_after_profile_vanished_is_profile_not_found(sandbox) -> None:  # type: ignore[no-untyped-def]
    profile = make_profile("firefox", "abcd.default")
    a = FirefoxAdapter()
    t = a.detect()[0]
    a.snapshot(t)
    planned = a.plan(t, FONTS, CFG)
    profile.rmdir()
    with pytest.raises(ScribeSenseError) as exc:
        a.apply(planned[0])
    assert exc.value.code is ErrorCode.PROFILE_NOT_FOUND
    assert not profile.exists()
    with pytest.raises(ScribeSenseError) as exc:
        a.snapshot(t)
    assert exc.value.code is ErrorCode.PROFILE_NOT_FOUND


def test_change_between_snapshot_and_plan_is_detected(sandbox) -> None:  # type: ignore[no-untyped-def]
    profile = make_profile("firefox", "abcd.default")
    write(profile / "user.js", USER_JS)
    a = FirefoxAdapter()
    t = a.detect()[0]
    a.snapshot(t)
    write(profile / "user.js", b"// edited\n")
    planned = a.plan(t, FONTS, CFG)
    write(profile / "user.js", USER_JS)  # changed back before apply
    with pytest.raises(ScribeSenseError) as exc:
        a.apply(planned[0])
    assert exc.value.code is ErrorCode.CHANGED_DURING_APPLY
    assert (profile / "user.js").read_bytes() == USER_JS


def test_failed_fsync_is_write_not_durable(sandbox, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    make_profile("firefox", "abcd.default")
    a = FirefoxAdapter()
    t = a.detect()[0]
    a.snapshot(t)
    planned = a.plan(t, FONTS, CFG)

    def bad_fsync(fd: int) -> None:
        raise OSError(5, "EIO")

    monkeypatch.setattr(os, "fsync", bad_fsync)
    with pytest.raises(ScribeSenseError) as exc:
        a.apply(planned[0])
    assert exc.value.code is ErrorCode.WRITE_NOT_DURABLE


def test_locations_are_validated(sandbox) -> None:  # type: ignore[no-untyped-def]
    a = FirefoxAdapter()
    for bad in ("relative/user.js", str(sandbox.home / ".bashrc"), str(sandbox.home / "p" / "userContent.css"),
                str(sandbox.home / "p" / ".." / "user.js"), str(sandbox.home / "p" / "chrome" / "userChrome.css")):
        with pytest.raises(ValueError):
            a.read_state(bad)


def test_foreign_or_unknown_target(sandbox) -> None:  # type: ignore[no-untyped-def]
    make_profile("firefox", "abcd.default")
    a = FirefoxAdapter()
    with pytest.raises(ValueError):
        a.snapshot(Target("gtk", "gtk:user", "GTK", False))
    with pytest.raises(ScribeSenseError) as exc:
        a.snapshot(Target("firefox", "firefox:firefox:nope", "x", False))
    assert exc.value.code is ErrorCode.PROFILE_NOT_FOUND


# ---------------------------------------------------------------- verify

def test_verify_ok_after_apply_and_fails_otherwise(sandbox) -> None:  # type: ignore[no-untyped-def]
    a, t, profile = _apply_one(sandbox, USER_JS, USER_CSS)
    r = a.verify(t, FONTS)
    assert (r.chosen_ok, r.drawn_ok, r.detail_code) == (True, None, "OK")
    r = a.verify(t, FONTS2)  # other family
    assert (r.chosen_ok, r.drawn_ok, r.detail_code) == (False, None, ErrorCode.ADAPTER_VERIFY_FAILED.value)
    write(profile / "user.js", USER_JS)  # pref block removed by the user
    assert a.verify(t, FONTS).chosen_ok is False


def test_verify_before_apply_fails(sandbox) -> None:  # type: ignore[no-untyped-def]
    make_profile("firefox", "abcd.default")
    a = FirefoxAdapter()
    r = a.verify(a.detect()[0], FONTS)
    assert (r.chosen_ok, r.drawn_ok, r.detail_code) == (False, None, ErrorCode.ADAPTER_VERIFY_FAILED.value)


def test_verify_profile_gone(sandbox) -> None:  # type: ignore[no-untyped-def]
    profile = make_profile("firefox", "abcd.default")
    a = FirefoxAdapter()
    t = a.detect()[0]
    profile.rmdir()
    r = a.verify(t, FONTS)
    assert (r.chosen_ok, r.detail_code) == (False, ErrorCode.PROFILE_NOT_FOUND.value)

