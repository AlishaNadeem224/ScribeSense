"""M4.4 — Flatpak adapter: conformance (M4.1) + card tests, in the unit sandbox.

No real process is launched: `flatpak list / ps / run` go through a fake runner. The keyfile edits are
cross-checked with GLib's own parser (GLib.KeyFile, in-process) when PyGObject is available.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scribesense.adapters import flatpak as fp
from scribesense.adapters.conformance import AdapterConformance
from scribesense.adapters.flatpak import ENTRY, FlatpakAdapter, merge_override, override_path, user_dir
from scribesense.contracts.adapter import Adapter, Target, state_sha256
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import FontSet
from scribesense.contracts.paths import run_cmd

CFG = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.0)
FONTS = FontSet("abc123", "AccessSans-abc123", "AccessSansUI-abc123", (), ("Regular",))
APPS = ["org.example.Reader", "app.zen_browser.zen", "com.example.Other"]
FIRST = APPS[0]
#: PyGObject's own import of GLib emits this; it is not ours (same filter as the GTK tests).
pytestmark = pytest.mark.filterwarnings("ignore:GLib.unix_signal_add_full is deprecated:DeprecationWarning")

GLOBAL_BYTES = b"[Context]\nunset-environment=X;\n\n[Environment]\nGTK_THEME=\n"


# ---------------------------------------------------------------- fakes

@dataclass
class FakeProc:
    stdout: bytes
    stderr: bytes = b""
    returncode: int = 0


@dataclass
class FakeRunner:
    """`flatpak list` -> installed, `flatpak ps` -> running, `flatpak run --command=fc-match` -> chosen."""

    installed: list[str] = field(default_factory=lambda: list(APPS))
    running: list[str] = field(default_factory=list)
    chosen: str = FONTS.reading_family
    list_out: bytes | None = None  # raw `flatpak list` output (overrides installed)
    fail: dict[str, ErrorCode] = field(default_factory=dict)  # "list" / "ps" / "run" -> error
    os_fail: dict[str, OSError] = field(default_factory=dict)
    calls: list[tuple[list[str], float]] = field(default_factory=list)

    def __call__(self, argv: list[str], timeout: float) -> FakeProc:
        self.calls.append((list(argv), timeout))
        assert argv[0] == "flatpak"
        sub = argv[1]
        if sub in self.fail:
            raise ScribeSenseError(self.fail[sub])
        if sub in self.os_fail:
            raise self.os_fail[sub]
        if sub == "list":
            out = self.list_out if self.list_out is not None else "".join(f"{a}\n" for a in self.installed).encode()
            return FakeProc(out)
        if sub == "ps":
            return FakeProc("".join(f"{a}\n" for a in self.running).encode())
        if sub == "run":
            return FakeProc(self.chosen.encode())
        raise AssertionError(f"unexpected flatpak command {argv!r}")

    def subcommands(self) -> list[str]:
        return [argv[1] for argv, _ in self.calls]


def make_adapter(runner: FakeRunner | None = None) -> FlatpakAdapter:
    return FlatpakAdapter(runner=runner or FakeRunner())


def tgt(app: str = FIRST, running: bool = False) -> Target:
    return Target("flatpak", f"flatpak:{app}", app, running)


def write_override(app: str, data: bytes, mode: int = 0o644) -> Path:
    path = override_path(app)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
    return path


def global_path() -> Path:
    return user_dir() / "overrides" / "global"


def cycle(adapter: FlatpakAdapter, app: str = FIRST):  # type: ignore[no-untyped-def]
    """snapshot (+ backup) -> plan -> apply; returns (item, backup, planned)."""
    t = tgt(app)
    [item] = adapter.snapshot(t)
    backup = adapter.read_state(item.location).data if item.existed else None
    [p] = adapter.plan(t, FONTS, CFG)
    adapter.apply(p)
    return item, backup, p


# ---------------------------------------------------------------- conformance

OTHER_FS = b"[Context]\nfilesystems=home;xdg-download:ro;\nshared=network;\n\n[Environment]\nFOO=bar\n"
START_STATES: dict[str, tuple[bytes, int] | None] = {
    "absent": None,
    "other-filesystems-0600": (OTHER_FS, 0o600),
    "other-groups-only": (b"# kept comment\n[Environment]\nGTK_THEME=Adwaita\n", 0o644),
    "already-ours": (b"[Context]\nfilesystems=xdg-config/fontconfig:ro;\n", 0o644),
    "empty": (b"", 0o644),
    "symlink": (OTHER_FS, 0o640),
}


@dataclass
class FlatpakHarness:
    adapter: FlatpakAdapter
    home: Path
    fonts: FontSet = FONTS
    config: Configuration = CFG

    def fingerprint(self) -> dict[str, str]:
        """Every file under the sandbox HOME (so a write anywhere else is caught), with mode and link.

        A file that an override location symlinks to is reported under that location (writes go through
        the link by design); the link itself must stay a link to the same target.
        """
        out: dict[str, str] = {}
        aliases = {os.path.realpath(override_path(app)): str(override_path(app))
                   for app in APPS if override_path(app).is_symlink()}
        for dirpath, _dirs, files in os.walk(self.home):
            for name in files:
                p = Path(dirpath) / name
                link = f"->{os.readlink(p)}" if p.is_symlink() else ""
                mode = stat.S_IMODE(p.stat().st_mode)
                key = aliases.get(str(p), str(p))
                if link:
                    key = str(p) + " (link)"
                    out[key] = link
                    continue
                out[key] = f"{state_sha256(True, p.read_bytes())}:{mode:o}"
        for app in APPS:
            out.setdefault(str(override_path(app)), state_sha256(False, None))
        return out

    def external_edit(self, location: str) -> None:
        path = Path(location)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"[Context]\nfilesystems=host;\n# changed by flatpak override\n")


def _setup(start: str, home: Path) -> None:
    write_override("global", GLOBAL_BYTES)  # present in every state; must never change
    spec = START_STATES[start]
    if spec is None:
        return
    data, mode = spec
    if start == "symlink":
        real = home / "dotfiles" / "flatpak" / FIRST
        real.parent.mkdir(parents=True)
        real.write_bytes(data)
        real.chmod(mode)
        link = override_path(FIRST)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(real)
        return
    write_override(FIRST, data, mode)


class TestFlatpakConformance(AdapterConformance):
    @pytest.fixture(params=sorted(START_STATES))
    def conformance_harness(self, request: pytest.FixtureRequest, sandbox, monkeypatch) -> FlatpakHarness:  # type: ignore[no-untyped-def]
        monkeypatch.delenv("FLATPAK_USER_DIR", raising=False)
        _setup(request.param, sandbox.home)
        return FlatpakHarness(make_adapter(), sandbox.home)


# ---------------------------------------------------------------- basics and detect

def test_attributes() -> None:
    a = make_adapter()
    _typed: Adapter = a  # static K3 conformance (Adapter is not runtime_checkable)
    assert (a.name, a.requires_restart, a.must_be_closed) == ("flatpak", True, False)
    assert fp.TIMEOUTS["flatpak-fc-match"] == 20.0 and fp.TIMEOUTS["other"] == 10.0


def test_detect_one_target_per_app_with_running_flags() -> None:
    runner = FakeRunner(running=["app.zen_browser.zen", "app.zen_browser.zen"])
    targets = make_adapter(runner).detect()
    assert targets == [tgt(FIRST), tgt("app.zen_browser.zen", True), tgt("com.example.Other")]
    assert runner.calls == [(fp.LIST_ARGV, 10.0), (fp.PS_ARGV, 10.0)]
    assert fp.LIST_ARGV == ["flatpak", "list", "--app", "--columns=application"]
    assert fp.PS_ARGV == ["flatpak", "ps", "--columns=application"]


@pytest.mark.parametrize("out", [b"", b"\n\n", b"Application ID\n", b"   \n"])
def test_detect_no_apps_is_empty_and_skips_ps(out: bytes) -> None:
    runner = FakeRunner(list_out=out)
    assert make_adapter(runner).detect() == []
    assert runner.subcommands() == ["list"]


def test_detect_skips_garbage_and_invalid_ids_and_deduplicates() -> None:
    bad = ["Application ID", "global", "-rf", "--user", "1abc.def", "org..x", "org.x.", ".org.x", "org",
           "org.ex ample.App", "org.example.App;rm", "org/example.App", "../../etc.passwd",
           "a." + "b" * 300, "org.éxample.App"]
    out = "\n".join(["  org.example.Reader  ", *bad, "org.example.Reader", "com.example.Other\r", ""]).encode()
    out += b"\xff\xfe.garbage\n"
    targets = make_adapter(FakeRunner(list_out=out)).detect()
    assert [t.display_name for t in targets] == ["org.example.Reader", "com.example.Other"]


def test_detect_without_flatpak_is_no_targets() -> None:
    runner = FakeRunner(fail={"list": ErrorCode.ADAPTER_UNAVAILABLE})
    assert make_adapter(runner).detect() == []


@pytest.mark.parametrize("code", [ErrorCode.ADAPTER_COMMAND_FAILED, ErrorCode.ADAPTER_TIMEOUT])
def test_detect_other_list_failures_are_raised(code: ErrorCode) -> None:
    with pytest.raises(ScribeSenseError) as exc:
        make_adapter(FakeRunner(fail={"list": code})).detect()
    assert exc.value.code is code


def test_detect_refused_launch_is_a_scribesense_error() -> None:
    with pytest.raises(ScribeSenseError) as exc:
        make_adapter(FakeRunner(os_fail={"list": PermissionError(13, "denied")})).detect()
    assert exc.value.code is ErrorCode.UNEXPECTED_ADAPTER_ERROR


@pytest.mark.parametrize("failure", [{"fail": {"ps": ErrorCode.ADAPTER_COMMAND_FAILED}},
                                     {"fail": {"ps": ErrorCode.ADAPTER_TIMEOUT}},
                                     {"os_fail": {"ps": PermissionError(13, "denied")}}])
def test_detect_running_unknown_means_not_running_and_is_logged(failure: dict, sandbox) -> None:  # type: ignore[no-untyped-def,type-arg]
    from scribesense.log import log_path

    targets = make_adapter(FakeRunner(**failure)).detect()
    assert [t.running for t in targets] == [False, False, False]
    events = [json.loads(line) for line in log_path().read_text().splitlines()]
    assert events[-1]["step"] == "detect-running" and events[-1]["adapter"] == "flatpak"
    assert not any("example" in line for line in log_path().read_text().splitlines())  # no app ids


# ---------------------------------------------------------------- location

def test_user_dir_resolution(sandbox, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("FLATPAK_USER_DIR", raising=False)
    assert user_dir() == sandbox.home / ".local" / "share" / "flatpak"  # sandbox XDG_DATA_HOME
    monkeypatch.setenv("XDG_DATA_HOME", str(sandbox.root / "data"))
    assert user_dir() == sandbox.root / "data" / "flatpak"
    assert override_path(FIRST) == sandbox.root / "data" / "flatpak" / "overrides" / FIRST
    monkeypatch.setenv("FLATPAK_USER_DIR", str(sandbox.root / "fpuser"))
    assert user_dir() == sandbox.root / "fpuser"
    monkeypatch.setenv("FLATPAK_USER_DIR", "")
    assert user_dir() == sandbox.root / "data" / "flatpak"
    monkeypatch.setenv("FLATPAK_USER_DIR", "relative/dir")
    monkeypatch.setenv("XDG_DATA_HOME", "relative/data")
    assert user_dir() == sandbox.home / ".local" / "share" / "flatpak"  # relative values ignored
    monkeypatch.delenv("FLATPAK_USER_DIR")
    monkeypatch.delenv("XDG_DATA_HOME")
    assert user_dir() == sandbox.home / ".local" / "share" / "flatpak"


def test_flatpak_user_dir_is_where_snapshot_and_apply_go(sandbox, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLATPAK_USER_DIR", str(sandbox.root / "fpuser"))
    a = make_adapter()
    item, _backup, p = cycle(a)
    assert item.location == p.location == str(sandbox.root / "fpuser" / "overrides" / FIRST)
    assert Path(p.location).read_bytes() == b"[Context]\nfilesystems=xdg-config/fontconfig:ro;\n"
    assert not (sandbox.home / ".local" / "share" / "flatpak").exists()


def test_snapshot_item_for_absent_and_present(sandbox) -> None:  # type: ignore[no-untyped-def]
    a = make_adapter()
    [item] = a.snapshot(tgt())
    assert (item.kind, item.existed, item.file_mode, item.old_value) == ("file", False, None, None)
    assert item.location == str(override_path(FIRST))
    write_override(FIRST, OTHER_FS, 0o600)
    [item] = a.snapshot(tgt())
    assert (item.existed, item.file_mode, item.old_sha256) == (True, 0o600, state_sha256(True, OTHER_FS))


def test_foreign_locations_and_the_global_override_are_refused(sandbox) -> None:  # type: ignore[no-untyped-def]
    a = make_adapter()
    write_override("global", GLOBAL_BYTES)
    overrides = user_dir() / "overrides"
    bad = [str(overrides / "global"), f"overrides/{FIRST}", str(sandbox.home / ".bashrc"),
           str(overrides / ".." / "overrides" / FIRST), str(user_dir() / "other" / FIRST),
           str(overrides / "-rf"), str(overrides / "noreversedns")]
    for location in bad:
        with pytest.raises(ValueError):
            a.read_state(location)
        with pytest.raises(ValueError):
            a.revert(fp.SnapshotItem(tgt(), "file", location, False, None, state_sha256(False, None), None, None), None)
    assert global_path().read_bytes() == GLOBAL_BYTES


@pytest.mark.parametrize("target", [
    Target("gtk", "gtk:user", "GTK", False),
    Target("flatpak", "flatpak:global", "global", False),
    Target("flatpak", "flatpak:-rf.x", "x", False),
    Target("flatpak", "org.example.Reader", "x", False),
])
def test_foreign_or_invalid_targets_are_refused(target: Target) -> None:
    a = make_adapter()
    for call in (lambda: a.snapshot(target), lambda: a.plan(target, FONTS, CFG), lambda: a.verify(target, FONTS)):
        with pytest.raises(ValueError):
            call()


def test_full_cycle_never_touches_the_global_override_or_other_apps(sandbox) -> None:  # type: ignore[no-untyped-def]
    write_override("global", GLOBAL_BYTES, 0o600)
    write_override("com.example.Other", OTHER_FS)
    a = make_adapter()
    for app in (FIRST, "app.zen_browser.zen"):
        item, backup, _ = cycle(a, app)
        assert ENTRY.encode() in override_path(app).read_bytes()
        a.revert(item, backup)
    assert global_path().read_bytes() == GLOBAL_BYTES and stat.S_IMODE(global_path().stat().st_mode) == 0o600
    assert override_path("com.example.Other").read_bytes() == OTHER_FS
    assert not override_path(FIRST).exists()


# ---------------------------------------------------------------- keyfile merge

def test_absent_and_empty_files_get_a_minimal_context_group() -> None:
    assert merge_override(None) == b"[Context]\nfilesystems=xdg-config/fontconfig:ro;\n"
    assert merge_override(b"") == b"[Context]\nfilesystems=xdg-config/fontconfig:ro;\n"


MERGE_CASES: dict[str, tuple[bytes, bytes]] = {
    "append-with-trailing-sep": (
        b"[Context]\nfilesystems=home;xdg-download:ro;\n",
        b"[Context]\nfilesystems=home;xdg-download:ro;xdg-config/fontconfig:ro;\n"),
    "append-without-trailing-sep": (
        b"[Context]\nfilesystems=home;xdg-download:ro\n",
        b"[Context]\nfilesystems=home;xdg-download:ro;xdg-config/fontconfig:ro;\n"),
    "empty-value": (
        b"[Context]\nfilesystems=\n",
        b"[Context]\nfilesystems=xdg-config/fontconfig:ro;\n"),
    "blanks-around-equals-and-trailing": (
        b"  [Context]\n  filesystems  =  home; \t\n",
        b"  [Context]\n  filesystems  =  home;xdg-config/fontconfig:ro; \t\n"),
    "escaped-semicolon-at-end": (
        b"[Context]\nfilesystems=odd\\;\n",
        b"[Context]\nfilesystems=odd\\;;xdg-config/fontconfig:ro;\n"),
    "escaped-backslash-before-sep": (
        b"[Context]\nfilesystems=odd\\\\;\n",
        b"[Context]\nfilesystems=odd\\\\;xdg-config/fontconfig:ro;\n"),
    "comments-and-other-groups-kept": (
        b"# top comment\n\n[Context]\n# about shared\nshared=network;ipc;\n\n# env comment\n[Environment]\nA=b\n",
        b"# top comment\n\n[Context]\n# about shared\nshared=network;ipc;\nfilesystems=xdg-config/fontconfig:ro;\n"
        b"\n# env comment\n[Environment]\nA=b\n"),
    "empty-context-group": (
        b"[Context]\n[Environment]\nA=b\n",
        b"[Context]\nfilesystems=xdg-config/fontconfig:ro;\n[Environment]\nA=b\n"),
    "no-context-group": (
        b"# c\n[Environment]\nA=b\n",
        b"# c\n[Environment]\nA=b\n\n[Context]\nfilesystems=xdg-config/fontconfig:ro;\n"),
    "no-context-group-no-final-newline": (
        b"[Environment]\nA=b",
        b"[Environment]\nA=b\n\n[Context]\nfilesystems=xdg-config/fontconfig:ro;\n"),
    "comments-only": (
        b"# only a comment\n",
        b"# only a comment\n\n[Context]\nfilesystems=xdg-config/fontconfig:ro;\n"),
    "key-at-eof-without-newline": (
        b"[Context]\nshared=network;",
        b"[Context]\nshared=network;\nfilesystems=xdg-config/fontconfig:ro;\n"),
    "crlf-line-endings": (
        b"[Context]\r\nfilesystems=home;\r\n[Environment]\r\nA=b\r\n",
        b"[Context]\r\nfilesystems=home;xdg-config/fontconfig:ro;\r\n[Environment]\r\nA=b\r\n"),
    "repeated-context-group-last-key-wins": (
        b"[Context]\nfilesystems=home;\n[Environment]\nA=b\n[Context]\nfilesystems=host-os;\n",
        b"[Context]\nfilesystems=home;\n[Environment]\nA=b\n[Context]\nfilesystems=host-os;xdg-config/fontconfig:ro;\n"),
    "repeated-key-last-wins": (
        b"[Context]\nfilesystems=home;\nfilesystems=host;\n",
        b"[Context]\nfilesystems=home;\nfilesystems=host;xdg-config/fontconfig:ro;\n"),
    "localized-key-is-not-the-key": (
        b"[Context]\nfilesystems[de]=x;\n",
        b"[Context]\nfilesystems[de]=x;\nfilesystems=xdg-config/fontconfig:ro;\n"),
    "filesystems-in-other-group-ignored": (
        b"[Other]\nfilesystems=home;\n",
        b"[Other]\nfilesystems=home;\n\n[Context]\nfilesystems=xdg-config/fontconfig:ro;\n"),
    "negated-by-user-is-followed-by-ours": (
        b"[Context]\nfilesystems=!xdg-config/fontconfig;\n",
        b"[Context]\nfilesystems=!xdg-config/fontconfig;xdg-config/fontconfig:ro;\n"),
    "ours-then-negated-again": (
        b"[Context]\nfilesystems=xdg-config/fontconfig:ro;!xdg-config/fontconfig;\n",
        b"[Context]\nfilesystems=xdg-config/fontconfig:ro;!xdg-config/fontconfig;xdg-config/fontconfig:ro;\n"),
    "unicode-elsewhere-kept": (
        "# Grüße\n[Environment]\nNAME=café\n[Context]\nfilesystems=home\n".encode(),
        "# Grüße\n[Environment]\nNAME=café\n[Context]\nfilesystems=home;xdg-config/fontconfig:ro;\n".encode()),
    # GLib ends a line only at "\n": these separators stay inside the comment / value (str.splitlines would split)
    "u2028-inside-comment": (
        "[Context]\nfilesystems=home;\n# note\u2028filesystems=x;\n".encode(),
        "[Context]\nfilesystems=home;xdg-config/fontconfig:ro;\n# note\u2028filesystems=x;\n".encode()),
    "lone-cr-inside-comment": (
        b"[Context]\nfilesystems=home;\n# a\rfilesystems=zzz;\n",
        b"[Context]\nfilesystems=home;xdg-config/fontconfig:ro;\n# a\rfilesystems=zzz;\n"),
    "lone-cr-hides-a-group-header-in-a-comment": (
        b"# a\r[Context]\n[Environment]\nA=b\n",
        b"# a\r[Context]\n[Environment]\nA=b\n\n[Context]\nfilesystems=xdg-config/fontconfig:ro;\n"),
    "u0085-inside-comment-in-context": (
        "[Context]\n# x\u0085shared=ipc;\n".encode(),
        "[Context]\nfilesystems=xdg-config/fontconfig:ro;\n# x\u0085shared=ipc;\n".encode()),
    "u2028-inside-a-value": (
        "[Environment]\nGREETING=hello\u2028world\n".encode(),
        "[Environment]\nGREETING=hello\u2028world\n\n[Context]\nfilesystems=xdg-config/fontconfig:ro;\n".encode()),
    "formfeed-inside-a-value": (
        b"[Environment]\nA=x\x0cy\n",
        b"[Environment]\nA=x\x0cy\n\n[Context]\nfilesystems=xdg-config/fontconfig:ro;\n"),
    "u0085-and-lone-cr-inside-filesystems-comment-neighbour": (
        "[Context]\nfilesystems=home;\n# y\u0085z\r\n".encode(),
        "[Context]\nfilesystems=home;xdg-config/fontconfig:ro;\n# y\u0085z\r\n".encode()),
}

UNCHANGED_CASES = {
    "already-ours": b"[Context]\nfilesystems=xdg-config/fontconfig:ro;\n",
    "already-ours-among-others": b"# c\n[Context]\nfilesystems=home;xdg-config/fontconfig:ro;host-etc;\n",
    "already-ours-no-trailing-sep": b"[Context]\nfilesystems=xdg-config/fontconfig:ro\n",
    "rw-grant-not-downgraded": b"[Context]\nfilesystems=xdg-config/fontconfig;\n",
    "explicit-rw-not-downgraded": b"[Context]\nfilesystems=xdg-config/fontconfig:rw;\n",
    "create-not-downgraded": b"[Context]\nfilesystems=xdg-config/fontconfig:create;\n",
    "regranted-after-negation": b"[Context]\nfilesystems=!xdg-config/fontconfig;xdg-config/fontconfig:ro;\n",
}


@pytest.mark.parametrize("case", sorted(MERGE_CASES))
def test_merge_appends_our_entry_and_keeps_every_other_byte(case: str) -> None:
    before, after = MERGE_CASES[case]
    assert merge_override(before) == after
    assert merge_override(after) == after  # idempotent: no duplicate on a second apply


@pytest.mark.parametrize("case", sorted(UNCHANGED_CASES))
def test_merge_leaves_files_that_already_grant_access_byte_identical(case: str) -> None:
    data = UNCHANGED_CASES[case]
    assert merge_override(data) is data


BAD_FILES = {
    "not-utf8": b"[Context]\nfilesystems=\xff;\n",
    "nul-byte": b"[Context]\nfilesystems=a\0;\n",  # GLib silently cuts the line at NUL; we refuse to edit
    "garbage-line": b"[Context]\nthis is not a key\n",
    "key-before-first-group": b"filesystems=home;\n[Context]\n",
    "empty-key": b"[Context]\n=home;\n",
    "text-after-group-header": b"[Context] x\nfilesystems=home;\n",
    "unclosed-group-header": b"[Context\nfilesystems=home;\n",
    "trailing-backslash": b"[Context]\nfilesystems=home\\\n",
    "invalid-escape": b"[Context]\nfilesystems=ho\\me;\n",
}


@pytest.mark.parametrize("case", sorted(BAD_FILES))
def test_files_glib_cannot_load_are_preferences_unreadable(case: str, sandbox) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ScribeSenseError) as exc:
        merge_override(BAD_FILES[case])
    assert exc.value.code is ErrorCode.PREFERENCES_UNREADABLE
    write_override(FIRST, BAD_FILES[case])
    a = make_adapter()
    a.snapshot(tgt())  # snapshot still works (read-only, raw bytes)
    with pytest.raises(ScribeSenseError) as exc:
        a.plan(tgt(), FONTS, CFG)
    assert exc.value.code is ErrorCode.PREFERENCES_UNREADABLE
    assert override_path(FIRST).read_bytes() == BAD_FILES[case]


@pytest.mark.parametrize("value, items", [
    ("", []), ("a", ["a"]), ("a;", ["a"]), ("a;b;", ["a", "b"]), ("a;;b", ["a", "", "b"]),
    ("a\\;b;c", ["a;b", "c"]), ("\\s\\t\\n\\r\\\\;", [" \t\n\r\\"]),
])
def test_parse_list_follows_glib(value: str, items: list[str]) -> None:
    assert fp.parse_list(value) == items


# -- cross-check with GLib's own keyfile parser (in-process, no command is run)

def _glib():  # type: ignore[no-untyped-def]
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib
    except (ImportError, ValueError):
        pytest.skip("GLib introspection not available")
    return GLib


def _glib_dump(GLib, data: bytes) -> dict[str, dict[str, object]]:  # type: ignore[no-untyped-def]
    kf = GLib.KeyFile()
    kf.load_from_data(data.decode(), len(data), GLib.KeyFileFlags.KEEP_COMMENTS)
    out: dict[str, dict[str, object]] = {}
    for group in kf.get_groups()[0]:
        keys = kf.get_keys(group)[0]
        out[group] = {k: kf.get_value(group, k) for k in keys}
        if group == "Context" and "filesystems" in keys:
            out[group]["filesystems"] = list(kf.get_string_list(group, "filesystems"))
    return out


@pytest.mark.parametrize("case", sorted(MERGE_CASES) + sorted(UNCHANGED_CASES))
def test_glib_reads_the_merged_file_as_intended(case: str) -> None:
    GLib = _glib()
    before = MERGE_CASES[case][0] if case in MERGE_CASES else UNCHANGED_CASES[case]
    after = merge_override(before)
    old, new = _glib_dump(GLib, before), _glib_dump(GLib, after)
    old_fs = list(old.get("Context", {}).get("filesystems", []))  # type: ignore[call-overload]
    new_fs = new["Context"]["filesystems"]
    if case in UNCHANGED_CASES:
        assert new_fs == old_fs and fp._has_read_grant(new_fs)  # type: ignore[arg-type]
    else:
        # our entry goes after the user's entries — before a blank-only tail entry, which GLib keeps
        # ("home; " is ["home", " "]) and which would otherwise glue onto ours
        tail = [e for e in old_fs[-1:] if not e.strip()]
        assert new_fs == [*old_fs[:len(old_fs) - len(tail)], ENTRY, *tail]
    for group, keys in old.items():  # every other group and key reads exactly as before
        for key, value in keys.items():
            if (group, key) != ("Context", "filesystems"):
                assert new[group][key] == value
    assert set(new) == set(old) | {"Context"}


@pytest.mark.parametrize("case", sorted(BAD_FILES))
def test_glib_also_rejects_the_files_we_call_unreadable(case: str) -> None:
    GLib = _glib()
    data = BAD_FILES[case]
    if case == "nul-byte":
        pytest.skip("GLib truncates at NUL instead of failing; the adapter refuses such a file on purpose")
    try:
        text = data.decode()
    except UnicodeDecodeError:
        return  # GLib's Python binding cannot even take it as str
    try:
        kf = GLib.KeyFile()
        kf.load_from_data(text, len(data), GLib.KeyFileFlags.NONE)
        kf.get_string_list("Context", "filesystems")
    except GLib.Error:
        return
    raise AssertionError("GLib accepted a file the adapter refuses")


# ---------------------------------------------------------------- apply / revert

def test_apply_new_file_is_0644_and_revert_deletes_it(sandbox) -> None:  # type: ignore[no-untyped-def]
    a = make_adapter()
    item, backup, p = cycle(a)
    path = override_path(FIRST)
    assert path.read_bytes() == p.payload and stat.S_IMODE(path.stat().st_mode) == 0o644
    a.revert(item, backup)
    assert not path.exists()
    a.revert(item, backup)  # repeatable, already gone is fine
    assert path.parent.is_dir()  # directories created by apply are left in place


def test_apply_keeps_mode_and_revert_restores_bytes_and_mode(sandbox) -> None:  # type: ignore[no-untyped-def]
    write_override(FIRST, OTHER_FS, 0o600)
    a = make_adapter()
    item, backup, _ = cycle(a)
    path = override_path(FIRST)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes().startswith(b"[Context]\nfilesystems=home;xdg-download:ro;xdg-config/fontconfig:ro;\n")
    path.chmod(0o644)
    a.revert(item, backup)
    assert path.read_bytes() == OTHER_FS and stat.S_IMODE(path.stat().st_mode) == 0o600


def test_already_granted_file_is_not_rewritten(sandbox) -> None:  # type: ignore[no-untyped-def]
    data = UNCHANGED_CASES["already-ours"]
    path = write_override(FIRST, data)
    inode = path.stat().st_ino
    a = make_adapter()
    _item, _backup, p = cycle(a)
    assert p.payload == data and path.stat().st_ino == inode


def test_symlinked_override_is_written_through_and_kept(sandbox) -> None:  # type: ignore[no-untyped-def]
    _setup("symlink", sandbox.home)
    real = sandbox.home / "dotfiles" / "flatpak" / FIRST
    link = override_path(FIRST)
    a = make_adapter()
    item, backup, _ = cycle(a)
    assert link.is_symlink() and os.readlink(link) == str(real)
    assert real.read_bytes() == OTHER_FS.replace(b"xdg-download:ro;", b"xdg-download:ro;xdg-config/fontconfig:ro;")
    assert stat.S_IMODE(real.stat().st_mode) == 0o640
    a.revert(item, backup)
    assert link.is_symlink() and real.read_bytes() == OTHER_FS


def test_dangling_symlink_is_refused_and_nothing_created(sandbox) -> None:  # type: ignore[no-untyped-def]
    link = override_path(FIRST)
    link.parent.mkdir(parents=True)
    elsewhere = sandbox.root / "outside" / "file"
    link.symlink_to(elsewhere)
    a = make_adapter()
    a.snapshot(tgt())
    [p] = a.plan(tgt(), FONTS, CFG)
    with pytest.raises(ScribeSenseError) as exc:
        a.apply(p)
    assert exc.value.code is ErrorCode.NOT_WRITABLE
    assert link.is_symlink() and not elsewhere.exists()


def test_apply_without_snapshot_or_with_text_payload_is_a_programming_error(sandbox) -> None:  # type: ignore[no-untyped-def]
    a = make_adapter()
    [p] = a.plan(tgt(), FONTS, CFG)
    with pytest.raises(ValueError):
        a.apply(p)
    a.snapshot(tgt())
    with pytest.raises(ValueError):
        a.apply(fp.PlannedWrite(p.target, p.location, p.payload.decode(), p.new_sha256))  # type: ignore[union-attr]
    assert not override_path(FIRST).exists()


@pytest.mark.parametrize("fonts", [
    FontSet("k", "DejaVu Sans", "AccessSansUI-k", (), ()),
    FontSet("k", "AccessSans-", "AccessSansUI-k", (), ()),
])
def test_plan_refuses_families_without_the_k7_prefix(fonts: FontSet) -> None:
    with pytest.raises(ScribeSenseError) as exc:
        make_adapter().plan(tgt(), fonts, CFG)
    assert exc.value.code is ErrorCode.UNEXPECTED_ADAPTER_ERROR


def test_revert_missing_backup_for_existing_file(sandbox) -> None:  # type: ignore[no-untyped-def]
    write_override(FIRST, OTHER_FS)
    a = make_adapter()
    item, _backup, _ = cycle(a)
    with pytest.raises(ScribeSenseError) as exc:
        a.revert(item, None)
    assert exc.value.code is ErrorCode.BACKUP_MISSING


def _is_regular_fd(fd: int) -> bool:
    return stat.S_ISREG(os.fstat(fd).st_mode)


@pytest.mark.parametrize("start", ["absent", "present"])
def test_failed_file_fsync_is_write_not_durable_and_leaves_the_original(sandbox, monkeypatch, start: str) -> None:  # type: ignore[no-untyped-def]
    if start == "present":
        write_override(FIRST, OTHER_FS)
    a = make_adapter()
    a.snapshot(tgt())
    [p] = a.plan(tgt(), FONTS, CFG)
    real_fsync = os.fsync

    def bad_fsync(fd: int) -> None:
        if _is_regular_fd(fd):
            raise OSError(5, "EIO")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", bad_fsync)
    with pytest.raises(ScribeSenseError) as exc:
        a.apply(p)
    assert exc.value.code is ErrorCode.WRITE_NOT_DURABLE
    path = override_path(FIRST)
    assert (path.read_bytes() == OTHER_FS) if start == "present" else not path.exists()
    assert [n for n in os.listdir(path.parent) if n.endswith(".tmp")] == []


# ---------------------------------------------------------------- verify

def test_verify_ok_runs_fc_match_inside_the_app_sandbox() -> None:
    runner = FakeRunner()
    result = make_adapter(runner).verify(tgt(), FONTS)
    assert (result.target, result.chosen_ok, result.drawn_ok, result.detail_code) == (tgt(), True, None, "OK")
    [(argv, timeout)] = runner.calls
    assert argv == ["flatpak", "run", "--command=fc-match", FIRST, "-f", "%{family[0]}", "sans\\-serif"]
    assert timeout == 20.0


def test_verify_mismatch() -> None:
    result = make_adapter(FakeRunner(chosen="DejaVu Sans")).verify(tgt(), FONTS)
    assert (result.chosen_ok, result.drawn_ok, result.detail_code) == (False, None, "ADAPTER_VERIFY_FAILED")


@pytest.mark.parametrize("failure", [
    {"fail": {"run": ErrorCode.ADAPTER_UNAVAILABLE}}, {"fail": {"run": ErrorCode.ADAPTER_TIMEOUT}},
    {"fail": {"run": ErrorCode.ADAPTER_COMMAND_FAILED}}, {"os_fail": {"run": PermissionError(13, "x")}},
    {"chosen": ""}, {"chosen": "  \n"},
])
def test_verify_check_unavailable(failure: dict) -> None:  # type: ignore[type-arg]
    result = make_adapter(FakeRunner(**failure)).verify(tgt(), FONTS)
    assert (result.chosen_ok, result.drawn_ok, result.detail_code) == (False, None, "CHECK_UNAVAILABLE")


def test_no_real_process_can_start_in_unit_tests() -> None:
    """The default runner is the real run_cmd; the sandbox blocks it, so only fakes can work here."""
    a = FlatpakAdapter()
    with pytest.raises(Exception) as exc:  # ProcessLaunchBlocked lives in conftest: match by name
        a.detect()
    assert type(exc.value).__name__ == "ProcessLaunchBlocked"


# ---------------------------------------------------------------- integration (bubblewrap only)

@pytest.mark.integration
def test_real_flatpak_reads_the_override_we_write(sandbox, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Real `flatpak override --user --show` (read-only) sees our entry, and nothing after revert.

    FLATPAK_USER_DIR points into the sandbox, so the real user installation is never read or written.
    The VM test (M3.5) covers the card's acceptance: fc-match inside a real installed app.
    The probe app is not installed. That `--show` works for such an app is checked by the probe below
    (before anything is written) and is still to be confirmed on the VM (M3.5).
    """
    monkeypatch.setenv("FLATPAK_USER_DIR", str(sandbox.root / "fpuser"))
    app = "org.scribesense.ProbeApp"
    path = write_override(app, b"# user comment\n[Context]\nshared=network;\n\n[Environment]\nA=b\n")

    def show() -> str:
        return run_cmd(["flatpak", "override", "--user", "--show", app], 10.0).stdout.decode()

    try:  # environment probe, on the user's untouched file: only these outcomes may skip
        show()
    except ScribeSenseError as exc:
        if exc.code is ErrorCode.ADAPTER_UNAVAILABLE:
            pytest.skip("no flatpak binary in this runner")
        if exc.code is ErrorCode.ADAPTER_COMMAND_FAILED:
            pytest.skip("`flatpak override --show` refuses an app that is not installed here")
        raise
    a = FlatpakAdapter()
    t = tgt(app)
    [item] = a.snapshot(t)
    backup = path.read_bytes()
    [p] = a.plan(t, FONTS, CFG)
    a.apply(p)

    shown = show()
    assert ENTRY in shown and "shared=network;" in shown and "A=b" in shown
    a.revert(item, backup)
    assert path.read_bytes() == backup
    assert ENTRY not in show()


@pytest.mark.parametrize("code", [ErrorCode.ADAPTER_UNAVAILABLE, ErrorCode.ADAPTER_COMMAND_FAILED])
def test_integration_test_skips_when_the_runner_cannot_show_overrides(code: ErrorCode, sandbox, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A runner without flatpak (or one that refuses a not-installed app) skips; nothing is written."""
    def fake_run_cmd(argv: list[str], timeout: float) -> object:
        raise ScribeSenseError(code)

    monkeypatch.setitem(globals(), "run_cmd", fake_run_cmd)
    with pytest.raises(pytest.skip.Exception):
        test_real_flatpak_reads_the_override_we_write(sandbox, monkeypatch)
    path = sandbox.root / "fpuser" / "overrides" / "org.scribesense.ProbeApp"
    assert ENTRY.encode() not in path.read_bytes()


def test_integration_test_does_not_skip_other_failures(sandbox, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_run_cmd(argv: list[str], timeout: float) -> object:
        raise ScribeSenseError(ErrorCode.ADAPTER_TIMEOUT)

    monkeypatch.setitem(globals(), "run_cmd", fake_run_cmd)
    with pytest.raises(ScribeSenseError):
        test_real_flatpak_reads_the_override_we_write(sandbox, monkeypatch)
