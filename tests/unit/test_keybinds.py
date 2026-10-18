"""M4.14 — Keybind writer (Hyprland, Lua + classic). Sandbox only: fake runner, temp HOME/XDG."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.store import SETTINGS
from scribesense.safety import keybinds as kb
from scribesense.safety.keybinds import (
    HyprlandBackend,
    Keybind,
    conflicts,
    default_keybinds,
    format_keybind,
    install_keybinds,
    keybind_status,
    parse_keybind,
    remove_keybinds,
)

CMD = "/opt/scribe sense/bin/scribesense"  # a space on purpose: must be shell-quoted


class FakeRunner:
    """Stands in for run_cmd: records argv + timeout, answers hyprctl binds -j / reload."""

    def __init__(self, binds: object = (), binds_error: ErrorCode | None = None,
                 reload_error: ErrorCode | None = None, raw: bytes | None = None,
                 binds_raise: Exception | None = None, reload_raise: Exception | None = None) -> None:
        self.calls: list[tuple[list[str], float]] = []
        self.binds = list(binds)
        self.binds_error = binds_error
        self.reload_error = reload_error
        self.raw = raw
        self.binds_raise = binds_raise  # raw exception, as run_cmd lets PermissionError/OSError through
        self.reload_raise = reload_raise

    def __call__(self, argv: list[str], timeout: float) -> SimpleNamespace:
        self.calls.append((list(argv), timeout))
        if argv == ["hyprctl", "binds", "-j"]:
            if self.binds_raise is not None:
                raise self.binds_raise
            if self.binds_error:
                raise ScribeSenseError(self.binds_error)
            return SimpleNamespace(stdout=self.raw if self.raw is not None else json.dumps(self.binds).encode())
        if argv == ["hyprctl", "reload"]:
            if self.reload_raise is not None:
                raise self.reload_raise
            if self.reload_error:
                raise ScribeSenseError(self.reload_error)
            return SimpleNamespace(stdout=b"ok")
        raise AssertionError(f"unexpected command {argv!r}")

    @property
    def reloads(self) -> int:
        return sum(1 for argv, _ in self.calls if argv == ["hyprctl", "reload"])


def live(modmask: int, key: str, arg: str = "kitty", *, dispatcher: str = "exec", submap: str = "",
         mouse: bool = False, keycode: int = 0) -> dict[str, object]:
    """One entry shaped like Hyprland's `hyprctl binds -j` output."""
    return {
        "locked": False, "mouse": mouse, "release": False, "repeat": False, "longPress": False,
        "non_consuming": False, "has_description": False, "modmask": modmask, "submap": submap,
        "key": key, "keycode": keycode, "catch_all": False, "description": "", "dispatcher": dispatcher,
        "arg": arg,
    }


@pytest.fixture
def running(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hyprland "running": the sandbox removes HYPRLAND_INSTANCE_SIGNATURE, so tests expecting a reload set it."""
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "sandbox_fake_signature")


@pytest.fixture
def hypr(sandbox) -> Path:  # type: ignore[no-untyped-def]
    d = sandbox.config_home / "hypr"
    d.mkdir(parents=True)
    return d


def own_lua(sandbox) -> Path:  # type: ignore[no-untyped-def]
    return sandbox.config_home / "scribesense" / "hyprland.lua"


def own_conf(sandbox) -> Path:  # type: ignore[no-untyped-def]
    return sandbox.config_home / "scribesense" / "hyprland.conf"


def fingerprint(root: Path) -> dict[str, object]:
    """Every entry under root: symlink target, or bytes + mode."""
    out: dict[str, object] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            rel = str(p.relative_to(root))
            if p.is_symlink():
                out[rel] = ("link", os.readlink(p))
            elif p.is_file():
                out[rel] = ("file", p.read_bytes(), p.stat().st_mode & 0o7777)
            else:
                out[rel] = ("dir",)
    return out


ML4W_LUA = (
    b'require("functions")\n'
    b"-- CUSTOM\n"
    b'local f = io.open(os.getenv("HOME") .. "/.config/hypr/custom.lua", "r")\n'
    b"if f then\n    f:close()\n    require(\"custom\")\nend\n"
    b'require("hyprland-gui")\n'
)
PLAIN_LUA = b'-- my config\nhl.bind("SUPER + Q", hl.dsp.exec_cmd("kitty"))\n'
CLASSIC = b"$mod = SUPER\nbind = $mod, Q, exec, kitty\n"


# ---------------------------------------------------------------- model / parsing

def test_defaults_come_from_k9_settings() -> None:
    binds = default_keybinds()
    assert [b.action for b in binds] == ["reader", "preset_next", "reset"]
    assert binds[0] == Keybind("reader", ("SUPER", "ALT"), "R")
    assert binds[1] == Keybind("preset_next", ("SUPER", "ALT"), "P")
    assert binds[2] == Keybind("reset", ("CTRL", "ALT", "SHIFT", "SUPER"), "BackSpace")
    for b in binds:
        assert parse_keybind(str(SETTINGS[kb.SETTING_KEYS[b.action]][1]), b.action) == b


def test_mods_are_normalised_aliases_and_order() -> None:
    b = parse_keybind("control win+alt shift, x", "reset")
    assert b.mods == ("SUPER", "CTRL", "ALT", "SHIFT") and b.key == "x"
    assert Keybind("reader", ("MOD4", "MOD1"), "R").mods == ("SUPER", "ALT")
    assert Keybind("reader", ["alt", "ALT", "super"], "R").mods == ("SUPER", "ALT")
    assert Keybind("reader", ("SUPER", "ALT"), "R").modmask == 64 | 8


def test_format_round_trips() -> None:
    for b in default_keybinds():
        assert parse_keybind(format_keybind(b), b.action) == b
    assert format_keybind(default_keybinds()[2]) == "SUPER CTRL ALT SHIFT, BackSpace"


@pytest.mark.parametrize("text", [
    "SUPER ALT, R\"", "SUPER ALT, R\nexec-once = rm", "SUPER, R, exec, x", "SUPER ALT, {R}",
    "SUPER ALT, Back Space", "SUPER ALT, R)", "SUPER ALT R", "", ", R", "HYPER, R", "SUPER ALT, ",
    "SUPER ALT, R;", "SUPER ALT, R#", "SUPER\tALT, R", "SUPER ALT, R'", "SUPER ALT, R\\",
    "SUPER ALT, $mod", "SUPER ALT, R + Q", "SUPER ALT, R\n", "SUPER ALT, R\r", "SUPER ALT, R\r\n",
])
def test_injection_and_garbage_refused(text: str) -> None:
    with pytest.raises(ValueError):
        parse_keybind(text, "reader")


@pytest.mark.parametrize("kwargs", [
    {"action": "shell", "mods": ("SUPER",), "key": "R"},
    {"action": "reader", "mods": (), "key": "R"},
    {"action": "reader", "mods": "SUPER", "key": "R"},
    {"action": "reader", "mods": ("SUPER",), "key": 'R")'},
    {"action": "reader", "mods": ("SUPER",), "key": ""},
    {"action": "reader", "mods": ("SUPER",), "key": "R\n"},  # "$" would accept a final newline
    {"action": "reader", "mods": ("SUPER",), "key": "R\r"},
    {"action": "reader", "mods": ("SUPER", "CAPS"), "key": "R"},
])
def test_keybind_validation(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        Keybind(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("command", ["scribesense", "bin/scribesense", "/a\nb", "/a,b", "/a#b", "/a\x00b", ""])
def test_bad_commands_refused(command: str) -> None:
    with pytest.raises(ValueError):
        kb.check_command(command)


def test_duplicate_actions_or_combos_refused(hypr: Path) -> None:
    r = Keybind("reader", ("SUPER", "ALT"), "R")
    with pytest.raises(ValueError):
        kb.render_own_file("lua", [r, r], CMD)
    with pytest.raises(ValueError):
        kb.render_own_file("lua", [r, Keybind("reset", ("ALT", "SUPER"), "r")], CMD)


def test_lua_string_escaping() -> None:
    assert kb.lua_string('a"b\\c') == '"a\\"b\\\\c"'
    assert kb.lua_string("x\ny\x7f") == '"x\\010y\\127"'


# ---------------------------------------------------------------- rendering

def test_render_lua_exact() -> None:
    data = kb.render_own_file("lua", default_keybinds()[:1], "/usr/bin/scribesense").decode()
    lines = data.splitlines()
    assert lines[0].startswith("-- Generated by ScribeSense")
    assert lines[2] == ('hl.bind("SUPER + ALT + R", hl.dsp.exec_cmd("/usr/bin/scribesense read --selection"),'
                        ' { description = "ScribeSense: open the selected text in the reader" })')
    assert lines[3] == 'hl.on("hyprland.start", function() hl.exec_cmd("/usr/bin/scribesense recover --login") end)'
    assert data.endswith("\n") and len(lines) == 4


def test_render_classic_exact() -> None:
    data = kb.render_own_file("classic", default_keybinds(), "/usr/bin/scribesense").decode()
    assert data.splitlines()[2:] == [
        "bind = SUPER ALT, R, exec, /usr/bin/scribesense read --selection",
        "bind = SUPER ALT, P, exec, /usr/bin/scribesense preset next",
        "bind = SUPER CTRL ALT SHIFT, BackSpace, exec, /usr/bin/scribesense reset",
        "exec-once = /usr/bin/scribesense recover --login",
    ]


def test_render_quotes_command_and_is_deterministic() -> None:
    a = kb.render_own_file("lua", default_keybinds(), CMD)
    assert a == kb.render_own_file("lua", default_keybinds(), CMD)
    assert b"\"'/opt/scribe sense/bin/scribesense' read --selection\"" in a
    classic = kb.render_own_file("classic", default_keybinds(), CMD)
    assert b"exec, '/opt/scribe sense/bin/scribesense' reset\n" in classic


def test_render_without_login_hook() -> None:
    assert b"hl.on(" not in kb.render_own_file("lua", default_keybinds(), CMD, include_login_hook=False)
    assert b"exec-once" not in kb.render_own_file("classic", default_keybinds(), CMD, include_login_hook=False)


# ---------------------------------------------------------------- Lua: ML4W custom hook

def test_lua_ml4w_hook_existing_custom_lua(sandbox, hypr: Path, running: None) -> None:  # type: ignore[no-untyped-def]
    (hypr / "hyprland.lua").write_bytes(ML4W_LUA)
    custom = b'hl.bind("SUPER + CTRL + ALT + E", hl.dsp.exec_cmd("x"))\n'
    (hypr / "custom.lua").write_bytes(custom)
    runner = FakeRunner()
    status = install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(runner))

    assert (hypr / "hyprland.lua").read_bytes() == ML4W_LUA  # never edited
    data = (hypr / "custom.lua").read_bytes()
    assert data.startswith(custom)  # every byte before the block unchanged
    assert data[len(custom):] == kb.render_block("lua", own_lua(sandbox))
    assert f'pcall(dofile, "{own_lua(sandbox)}")'.encode() in data
    assert own_lua(sandbox).read_bytes() == kb.render_own_file("lua", default_keybinds(), CMD)
    assert status.format == "lua" and status.hook_file == hypr / "custom.lua" and status.block == "clean"
    assert status.own_file == own_lua(sandbox) and status.own_file_present and status.reload == "reloaded"
    assert runner.calls == [(["hyprctl", "binds", "-j"], 10.0), (["hyprctl", "reload"], 10.0)]

    assert remove_keybinds(backend=HyprlandBackend(runner)) is True
    assert (hypr / "custom.lua").read_bytes() == custom
    assert not own_lua(sandbox).exists()
    assert runner.reloads == 2


def test_lua_ml4w_hook_missing_custom_lua_is_created_then_deleted(sandbox, hypr: Path) -> None:  # type: ignore[no-untyped-def]
    (hypr / "hyprland.lua").write_bytes(ML4W_LUA)
    before = fingerprint(sandbox.home)
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    data = (hypr / "custom.lua").read_bytes()
    assert data == kb.render_block("lua", own_lua(sandbox), "created")
    assert (hypr / "custom.lua").stat().st_mode & 0o777 == 0o644
    remove_keybinds(backend=HyprlandBackend(FakeRunner()))
    after = fingerprint(sandbox.home)
    after.pop(".config/scribesense", None)  # our (now empty) directory may remain
    assert after == before


def test_commented_require_custom_is_not_a_hook(hypr: Path) -> None:
    (hypr / "hyprland.lua").write_bytes(b'-- require("custom")\nlocal x = 1\n')
    assert HyprlandBackend(FakeRunner()).hook_file("lua") == hypr / "hyprland.lua"


# ---------------------------------------------------------------- Lua without hook / classic

def test_lua_without_hook_appends_to_hyprland_lua(sandbox, hypr: Path) -> None:  # type: ignore[no-untyped-def]
    (hypr / "hyprland.lua").write_bytes(PLAIN_LUA)
    before = fingerprint(sandbox.home)
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    data = (hypr / "hyprland.lua").read_bytes()
    assert data == PLAIN_LUA + kb.render_block("lua", own_lua(sandbox))
    assert not (hypr / "custom.lua").exists()
    remove_keybinds(backend=HyprlandBackend(FakeRunner()))
    after = fingerprint(sandbox.home)
    after.pop(".config/scribesense", None)
    assert after == before


def test_classic_config(sandbox, hypr: Path) -> None:  # type: ignore[no-untyped-def]
    (hypr / "hyprland.conf").write_bytes(CLASSIC)
    status = install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    assert status.format == "classic" and status.hook_file == hypr / "hyprland.conf"
    data = (hypr / "hyprland.conf").read_bytes()
    assert data == CLASSIC + (
        b"# >>> ScribeSense (managed, do not edit) >>>\n"
        + f"source = {own_conf(sandbox)}\n".encode()
        + b"# <<< ScribeSense <<<\n"
    )
    assert own_conf(sandbox).read_bytes() == kb.render_own_file("classic", default_keybinds(), CMD)
    assert not own_lua(sandbox).exists()
    remove_keybinds(backend=HyprlandBackend(FakeRunner()))
    assert (hypr / "hyprland.conf").read_bytes() == CLASSIC
    assert not own_conf(sandbox).exists()


def test_lua_wins_over_classic(hypr: Path) -> None:
    (hypr / "hyprland.lua").write_bytes(PLAIN_LUA)
    (hypr / "hyprland.conf").write_bytes(CLASSIC)
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    assert (hypr / "hyprland.conf").read_bytes() == CLASSIC


@pytest.mark.parametrize("original", [b"bind = SUPER, Q, exec, kitty", b"", b"\n\n", b"x\r\n"])
def test_remove_restores_exact_bytes_including_missing_final_newline(hypr: Path, original: bytes) -> None:
    (hypr / "hyprland.conf").write_bytes(original)
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    data = (hypr / "hyprland.conf").read_bytes()
    assert data.startswith(original)
    remove_keybinds(backend=HyprlandBackend(FakeRunner()))
    assert (hypr / "hyprland.conf").read_bytes() == original


def test_text_added_after_block_is_kept_on_remove(hypr: Path) -> None:
    (hypr / "hyprland.conf").write_bytes(b"a = 1")  # no final newline -> "+newline" variant
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    with open(hypr / "hyprland.conf", "ab") as f:
        f.write(b"b = 2\n")
    remove_keybinds(backend=HyprlandBackend(FakeRunner()))
    assert (hypr / "hyprland.conf").read_bytes() == b"a = 1\nb = 2\n"  # lines never joined


def test_file_mode_preserved(hypr: Path) -> None:
    (hypr / "hyprland.conf").write_bytes(CLASSIC)
    (hypr / "hyprland.conf").chmod(0o600)
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    assert (hypr / "hyprland.conf").stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------- symlinks

def test_symlinked_hypr_dir_and_file_are_preserved(sandbox) -> None:  # type: ignore[no-untyped-def]
    dots = sandbox.home / "dotfiles" / "hypr"
    dots.mkdir(parents=True)
    (dots / "hyprland.lua").write_bytes(ML4W_LUA)
    (sandbox.home / "dotfiles" / "custom-real.lua").write_bytes(b"-- mine\n")
    (dots / "custom.lua").symlink_to("../custom-real.lua")
    (sandbox.config_home / "hypr").symlink_to(dots)

    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    assert (sandbox.config_home / "hypr").is_symlink()
    assert (dots / "custom.lua").is_symlink()
    real = sandbox.home / "dotfiles" / "custom-real.lua"
    assert real.read_bytes() == b"-- mine\n" + kb.render_block("lua", own_lua(sandbox))
    assert (dots / "hyprland.lua").read_bytes() == ML4W_LUA

    remove_keybinds(backend=HyprlandBackend(FakeRunner()))
    assert (dots / "custom.lua").is_symlink() and real.read_bytes() == b"-- mine\n"


def test_symlinked_hypr_dir_custom_created_in_target(sandbox) -> None:  # type: ignore[no-untyped-def]
    dots = sandbox.home / "dotfiles" / "hypr"
    dots.mkdir(parents=True)
    (dots / "hyprland.lua").write_bytes(ML4W_LUA)
    (sandbox.config_home / "hypr").symlink_to(dots)
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    assert (sandbox.config_home / "hypr").is_symlink()
    assert (dots / "custom.lua").is_file() and not (dots / "custom.lua").is_symlink()


def test_dangling_symlink_into_missing_dir_refused(sandbox, hypr: Path) -> None:  # type: ignore[no-untyped-def]
    (hypr / "hyprland.lua").write_bytes(ML4W_LUA)
    (hypr / "custom.lua").symlink_to(sandbox.home / "nowhere" / "custom.lua")
    with pytest.raises(ScribeSenseError) as e:
        install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    assert e.value.code == ErrorCode.NOT_WRITABLE
    assert not (sandbox.home / "nowhere").exists()


# ---------------------------------------------------------------- idempotence / drift

def test_install_twice_one_block_and_new_binds_rewrite_only_our_file(sandbox, hypr: Path) -> None:  # type: ignore[no-untyped-def]
    (hypr / "hyprland.lua").write_bytes(PLAIN_LUA)
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    first = (hypr / "hyprland.lua").read_bytes()
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    assert (hypr / "hyprland.lua").read_bytes() == first
    assert first.count(b">>> ScribeSense") == 1

    changed = [parse_keybind("SUPER SHIFT, F9", "reader")]
    install_keybinds(changed, CMD, backend=HyprlandBackend(FakeRunner()))
    assert (hypr / "hyprland.lua").read_bytes() == first
    assert own_lua(sandbox).read_bytes() == kb.render_own_file("lua", changed, CMD)


def test_block_moves_when_custom_hook_appears(hypr: Path) -> None:
    (hypr / "hyprland.lua").write_bytes(PLAIN_LUA)
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    # The user switches to an ML4W-style config: rewrite the part before our block.
    data = (hypr / "hyprland.lua").read_bytes()
    (hypr / "hyprland.lua").write_bytes(ML4W_LUA + data[len(PLAIN_LUA):])
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    assert (hypr / "hyprland.lua").read_bytes() == ML4W_LUA  # stale include removed: one include only
    assert b"pcall(dofile" in (hypr / "custom.lua").read_bytes()


def _edit_block(path: Path) -> bytes:
    data = path.read_bytes().replace(b"pcall(dofile", b"pcall(loadfile").replace(b"source = ", b"source=")
    path.write_bytes(data)
    return data


@pytest.mark.parametrize("fmt", ["lua", "classic"])
def test_hand_edited_block_is_never_touched(sandbox, hypr: Path, fmt: str) -> None:  # type: ignore[no-untyped-def]
    entry = hypr / ("hyprland.lua" if fmt == "lua" else "hyprland.conf")
    entry.write_bytes(PLAIN_LUA if fmt == "lua" else CLASSIC)
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    edited = _edit_block(entry)
    assert keybind_status(backend=HyprlandBackend(FakeRunner())).block == "drifted"

    before = fingerprint(sandbox.home)
    runner = FakeRunner()
    with pytest.raises(ScribeSenseError) as e:
        install_keybinds([parse_keybind("SUPER, F1", "reader")], CMD, backend=HyprlandBackend(runner))
    assert e.value.code == ErrorCode.DRIFT_SKIPPED
    assert fingerprint(sandbox.home) == before and runner.reloads == 0

    with pytest.raises(ScribeSenseError) as e:
        remove_keybinds(backend=HyprlandBackend(FakeRunner()))
    assert e.value.code == ErrorCode.DRIFT_SKIPPED
    assert entry.read_bytes() == edited
    assert (own_lua(sandbox) if fmt == "lua" else own_conf(sandbox)).exists()  # may still be included


def test_duplicate_or_broken_markers_are_drift(sandbox, hypr: Path) -> None:  # type: ignore[no-untyped-def]
    block = kb.render_block("classic", own_conf(sandbox))
    for data in (CLASSIC + block + block, CLASSIC + block.split(b"\n", 1)[0] + b"\n", b"# <<< ScribeSense <<<\n"):
        assert kb.find_block(data, "classic", own_conf(sandbox)).state == "drifted"
    assert kb.find_block(CLASSIC + block, "classic", own_conf(sandbox)).state == "clean"
    assert kb.find_block(CLASSIC, "classic", own_conf(sandbox)).state == "absent"


def test_remove_skips_drift_but_removes_clean_blocks(sandbox, hypr: Path, running: None) -> None:  # type: ignore[no-untyped-def]
    (hypr / "hyprland.conf").write_bytes(CLASSIC + b"# >>> ScribeSense (managed, do not edit) >>>\nsource=x\n")
    (hypr / "hyprland.lua").write_bytes(PLAIN_LUA)
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    runner = FakeRunner()
    with pytest.raises(ScribeSenseError) as e:
        remove_keybinds(backend=HyprlandBackend(runner))
    assert e.value.code == ErrorCode.DRIFT_SKIPPED
    assert (hypr / "hyprland.lua").read_bytes() == PLAIN_LUA and not own_lua(sandbox).exists()
    assert runner.reloads == 1


def test_remove_with_nothing_installed_is_a_no_op(sandbox) -> None:  # type: ignore[no-untyped-def]
    runner = FakeRunner()
    assert remove_keybinds(backend=HyprlandBackend(runner)) is False
    assert runner.calls == []


# O3 review note (1), 2026-10-18: the empty config_home()/scribesense/ is removed, nothing else.

@pytest.mark.parametrize("name, original", [("hyprland.lua", PLAIN_LUA), ("hyprland.conf", CLASSIC)],
                         ids=["lua", "classic"])
def test_remove_deletes_our_dir_when_empty(sandbox, hypr: Path, name: str, original: bytes) -> None:  # type: ignore[no-untyped-def]
    (hypr / name).write_bytes(original)
    before = fingerprint(sandbox.home)
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    own_dir = sandbox.config_home / "scribesense"
    assert own_dir.is_dir()
    assert remove_keybinds(backend=HyprlandBackend(FakeRunner())) is True
    assert not own_dir.exists()
    assert fingerprint(sandbox.home) == before  # exactly the state before install: nothing else removed


def test_remove_keeps_our_dir_when_it_holds_anything_else(sandbox, hypr: Path) -> None:  # type: ignore[no-untyped-def]
    (hypr / "hyprland.conf").write_bytes(CLASSIC)
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    own_dir = sandbox.config_home / "scribesense"
    (own_dir / "notes.txt").write_bytes(b"mine")
    (own_dir / "sub").mkdir()  # an empty subdirectory is still "something else"
    remove_keybinds(backend=HyprlandBackend(FakeRunner()))
    assert not own_conf(sandbox).exists()
    assert (own_dir / "notes.txt").read_bytes() == b"mine" and (own_dir / "sub").is_dir()
    assert sorted(p.name for p in own_dir.iterdir()) == ["notes.txt", "sub"]


def test_remove_keeps_our_dir_when_drift_keeps_our_file(sandbox, hypr: Path) -> None:  # type: ignore[no-untyped-def]
    (hypr / "hyprland.conf").write_bytes(CLASSIC)
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    data = (hypr / "hyprland.conf").read_bytes()
    (hypr / "hyprland.conf").write_bytes(data.replace(b"source = ", b"source=  "))  # hand-edited block
    with pytest.raises(ScribeSenseError) as e:
        remove_keybinds(backend=HyprlandBackend(FakeRunner()))
    assert e.value.code == ErrorCode.DRIFT_SKIPPED
    assert own_conf(sandbox).is_file()  # the edited block may still include it: file and dir stay


def test_remove_never_removes_a_symlinked_dir_or_its_target(sandbox, hypr: Path) -> None:  # type: ignore[no-untyped-def]
    target = sandbox.home / "dotfiles" / "scribesense"
    target.mkdir(parents=True)
    (sandbox.config_home / "scribesense").symlink_to(target)
    (hypr / "hyprland.conf").write_bytes(CLASSIC)
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    remove_keybinds(backend=HyprlandBackend(FakeRunner()))
    assert (sandbox.config_home / "scribesense").is_symlink() and target.is_dir()
    assert list(target.iterdir()) == []


def test_remove_with_nothing_installed_drops_a_leftover_empty_dir(sandbox) -> None:  # type: ignore[no-untyped-def]
    own_dir = sandbox.config_home / "scribesense"
    own_dir.mkdir(parents=True)
    runner = FakeRunner()
    assert remove_keybinds(backend=HyprlandBackend(runner)) is False  # not a change Hyprland sees
    assert runner.calls == [] and not own_dir.exists() and sandbox.config_home.is_dir()


# ---------------------------------------------------------------- conflicts

REALISTIC = [
    live(64, "Q", "kitty"),
    live(72, "r", "rofi -show run"),  # SUPER+ALT+r: conflicts with our reader bind
    live(64 | 8, "P", "/usr/local/bin/scribesense preset next"),  # our own earlier install: ignored
    live(64 | 8, "P", "x", submap="resize"),  # other submap: ignored
    live(64, "mouse:272", "movewindow", dispatcher="movewindow", mouse=True),
    live(64, "", "x", keycode=36),  # keycode-only bind: not comparable by name
    live(1 | 4 | 8 | 64, "BACKSPACE", "", dispatcher="exit"),  # case-insensitive key
    "garbage",
    {"modmask": True, "key": "R"},
]


def test_conflicts_from_realistic_hyprctl_json(hypr: Path) -> None:
    runner = FakeRunner(REALISTIC)
    found = conflicts(default_keybinds(), backend=HyprlandBackend(runner))
    assert [(c.ours.action, c.theirs.key, c.theirs.dispatcher) for c in found] == [
        ("reader", "r", "exec"), ("reset", "BACKSPACE", "exit"),
    ]
    assert found[1].theirs.mods == ("SUPER", "CTRL", "ALT", "SHIFT")
    assert runner.calls == [(["hyprctl", "binds", "-j"], 10.0)]


def test_install_refuses_on_conflict_and_writes_nothing(sandbox, hypr: Path) -> None:  # type: ignore[no-untyped-def]
    (hypr / "hyprland.lua").write_bytes(PLAIN_LUA)
    before = fingerprint(sandbox.home)
    runner = FakeRunner(REALISTIC)
    with pytest.raises(ScribeSenseError) as e:
        install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(runner))
    assert e.value.code == ErrorCode.KEYBIND_CONFLICT
    assert fingerprint(sandbox.home) == before and runner.reloads == 0


def test_own_command_detection() -> None:
    assert kb.is_own_command("'/opt/scribe sense/bin/scribesense' read --selection")
    assert kb.is_own_command("/usr/bin/scribesense reset")
    assert not kb.is_own_command("/usr/bin/scribesense reset; rm -rf x")
    assert not kb.is_own_command("/usr/bin/notscribesense reset")
    assert not kb.is_own_command("'unterminated")


@pytest.mark.parametrize("code", [ErrorCode.ADAPTER_UNAVAILABLE, ErrorCode.ADAPTER_COMMAND_FAILED,
                                  ErrorCode.ADAPTER_TIMEOUT])
def test_hyprctl_unusable_is_check_unavailable(hypr: Path, code: ErrorCode) -> None:
    with pytest.raises(ScribeSenseError) as e:
        conflicts(default_keybinds(), backend=HyprlandBackend(FakeRunner(binds_error=code)))
    assert e.value.code == ErrorCode.CHECK_UNAVAILABLE


@pytest.mark.parametrize("raw", [b"HYPRLAND_INSTANCE_SIGNATURE not set", b'{"a": 1}', b"\xff\xfe"])
def test_unparseable_hyprctl_output_is_check_unavailable(raw: bytes) -> None:
    with pytest.raises(ScribeSenseError) as e:
        conflicts(default_keybinds(), backend=HyprlandBackend(FakeRunner(raw=raw)))
    assert e.value.code == ErrorCode.CHECK_UNAVAILABLE


def test_install_without_check_needs_explicit_permission(sandbox, hypr: Path) -> None:  # type: ignore[no-untyped-def]
    (hypr / "hyprland.lua").write_bytes(PLAIN_LUA)
    before = fingerprint(sandbox.home)
    with pytest.raises(ScribeSenseError) as e:
        install_keybinds(default_keybinds(), CMD,
                         backend=HyprlandBackend(FakeRunner(binds_error=ErrorCode.ADAPTER_UNAVAILABLE)))
    assert e.value.code == ErrorCode.CHECK_UNAVAILABLE and fingerprint(sandbox.home) == before
    install_keybinds(default_keybinds(), CMD, allow_unchecked=True,
                     backend=HyprlandBackend(FakeRunner(binds_error=ErrorCode.ADAPTER_UNAVAILABLE)))
    assert own_lua(sandbox).exists()


# ---------------------------------------------------------------- reload / availability / status

def test_reload_failure_reported_but_files_stay(sandbox, hypr: Path, running: None) -> None:  # type: ignore[no-untyped-def]
    (hypr / "hyprland.lua").write_bytes(PLAIN_LUA)
    runner = FakeRunner(reload_error=ErrorCode.ADAPTER_COMMAND_FAILED)
    with pytest.raises(ScribeSenseError) as e:
        install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(runner))
    assert e.value.code == ErrorCode.ADAPTER_COMMAND_FAILED
    assert own_lua(sandbox).exists() and b"pcall(dofile" in (hypr / "hyprland.lua").read_bytes()
    with pytest.raises(ScribeSenseError):
        remove_keybinds(backend=HyprlandBackend(runner))
    assert (hypr / "hyprland.lua").read_bytes() == PLAIN_LUA and not own_lua(sandbox).exists()


# ---------------------------------------------------------------- Hyprland not running (team decision item 4)

@pytest.mark.parametrize("value", [None, ""])
def test_not_running_skips_reload_silently(sandbox, hypr: Path, monkeypatch: pytest.MonkeyPatch,
                                           value: str | None) -> None:  # type: ignore[no-untyped-def]
    if value is None:
        monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    else:
        monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", value)
    (hypr / "hyprland.lua").write_bytes(PLAIN_LUA)
    # Even a runner whose reload would fail: it must not be called at all.
    runner = FakeRunner(reload_error=ErrorCode.ADAPTER_COMMAND_FAILED)
    status = install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(runner))
    assert status.reload == "skipped" and status.block == "clean" and status.own_file_present
    assert runner.reloads == 0
    assert runner.calls == [(["hyprctl", "binds", "-j"], 10.0)]
    assert own_lua(sandbox).read_bytes() == kb.render_own_file("lua", default_keybinds(), CMD)
    assert remove_keybinds(backend=HyprlandBackend(runner)) is True  # no error either
    assert runner.reloads == 0 and (hypr / "hyprland.lua").read_bytes() == PLAIN_LUA


def test_running_is_read_at_call_time(hypr: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (hypr / "hyprland.lua").write_bytes(PLAIN_LUA)
    runner = FakeRunner()
    backend = HyprlandBackend(runner)
    assert install_keybinds(default_keybinds(), CMD, backend=backend).reload == "skipped"
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "sandbox_fake_signature")
    assert install_keybinds(default_keybinds(), CMD, backend=backend).reload == "reloaded"
    assert runner.reloads == 1


def test_running_reload_failure_still_raised(sandbox, hypr: Path, running: None) -> None:  # type: ignore[no-untyped-def]
    (hypr / "hyprland.conf").write_bytes(CLASSIC)
    runner = FakeRunner(reload_error=ErrorCode.ADAPTER_TIMEOUT)
    with pytest.raises(ScribeSenseError) as e:
        install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(runner))
    assert e.value.code == ErrorCode.ADAPTER_TIMEOUT and runner.reloads == 1
    assert own_conf(sandbox).exists()  # files stay written


def test_not_running_conflict_check_still_unavailable(sandbox, hypr: Path) -> None:  # type: ignore[no-untyped-def]
    # hyprctl without a live Hyprland prints an error instead of JSON.
    raw = b"HYPRLAND_INSTANCE_SIGNATURE not set! (is hyprland running?)"
    with pytest.raises(ScribeSenseError) as e:
        conflicts(default_keybinds(), backend=HyprlandBackend(FakeRunner(raw=raw)))
    assert e.value.code == ErrorCode.CHECK_UNAVAILABLE
    (hypr / "hyprland.lua").write_bytes(PLAIN_LUA)
    before = fingerprint(sandbox.home)
    with pytest.raises(ScribeSenseError) as e:
        install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner(raw=raw)))
    assert e.value.code == ErrorCode.CHECK_UNAVAILABLE and fingerprint(sandbox.home) == before
    runner = FakeRunner(raw=raw)
    status = install_keybinds(default_keybinds(), CMD, allow_unchecked=True, backend=HyprlandBackend(runner))
    assert status.reload == "skipped" and runner.reloads == 0 and own_lua(sandbox).exists()


def test_keybind_status_reports_no_reload(hypr: Path) -> None:
    (hypr / "hyprland.conf").write_bytes(CLASSIC)
    assert keybind_status(backend=HyprlandBackend(FakeRunner())).reload is None


def test_no_hyprland_config_is_adapter_unavailable(sandbox) -> None:  # type: ignore[no-untyped-def]
    before = fingerprint(sandbox.home)
    with pytest.raises(ScribeSenseError) as e:
        install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    assert e.value.code == ErrorCode.ADAPTER_UNAVAILABLE
    assert fingerprint(sandbox.home) == before
    status = keybind_status(backend=HyprlandBackend(FakeRunner()))
    assert status.format is None and status.block == "absent" and status.hook_file is None


def test_status_before_and_after(sandbox, hypr: Path) -> None:  # type: ignore[no-untyped-def]
    (hypr / "hyprland.conf").write_bytes(CLASSIC)
    s = keybind_status(backend=HyprlandBackend(FakeRunner()))
    assert (s.backend, s.format, s.block, s.own_file_present) == ("hyprland", "classic", "absent", False)
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    s = keybind_status(backend=HyprlandBackend(FakeRunner()))
    assert (s.block, s.own_file_present, s.own_file) == ("clean", True, own_conf(sandbox))


def test_default_backend_uses_run_cmd(monkeypatch: pytest.MonkeyPatch, hypr: Path, running: None) -> None:
    runner = FakeRunner()
    monkeypatch.setattr(kb, "run_cmd", runner)
    (hypr / "hyprland.lua").write_bytes(PLAIN_LUA)
    install_keybinds(default_keybinds(), CMD)
    assert runner.reloads == 1


def test_classic_refuses_unrepresentable_own_path(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    odd = sandbox.home / "cfg#x"
    (odd / "hypr").mkdir(parents=True)
    (odd / "hypr" / "hyprland.conf").write_bytes(CLASSIC)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(odd))
    with pytest.raises(ScribeSenseError) as e:
        install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    assert e.value.code == ErrorCode.ADAPTER_UNAVAILABLE
    assert (odd / "hypr" / "hyprland.conf").read_bytes() == CLASSIC and not (odd / "scribesense").exists()


def test_logs_carry_no_paths_or_commands(sandbox, hypr: Path) -> None:  # type: ignore[no-untyped-def]
    from scribesense.log import log_path

    (hypr / "hyprland.lua").write_bytes(PLAIN_LUA)
    with pytest.raises(ScribeSenseError):
        install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner(REALISTIC)))
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    _edit_block(hypr / "hyprland.lua")
    with pytest.raises(ScribeSenseError):
        remove_keybinds(backend=HyprlandBackend(FakeRunner()))
    text = log_path().read_text()
    assert "KEYBIND_CONFLICT" in text and "DRIFT_SKIPPED" in text
    assert not re.search(r"/(opt|home|tmp)", text)
    assert "scribe sense" not in text and str(sandbox.home) not in text


def test_newline_key_never_reaches_a_config_line() -> None:
    with pytest.raises(ValueError):
        kb.render_own_file("classic", [Keybind("reader", ("SUPER",), "R\n")], "/usr/bin/scribesense")


# ---------------------------------------------------------------- Lua hook file ending in `return` (KB-2)

MODULE_LUA = b"local M = {}\nM.x = 1\nreturn M\n"


@pytest.mark.parametrize(("data", "expected"), [
    (b"", None),
    (b"local a = 1\n", None),
    (MODULE_LUA, len(b"local M = {}\nM.x = 1\n")),
    (b"local M = {}\n  return M -- done\n-- trailing comment\n", len(b"local M = {}\n")),
    (b"local M = {}\nreturn {\n  a = 1,\n}\n", len(b"local M = {}\n")),
    (b"x = 1\nreturn function() return 2 end", len(b"x = 1\n")),
    (b"local function f()\n  return 1\nend\n", None),  # return inside a function
    (b"if x then\n  return\nend\nlocal y = 2\n", None),
    (b"while true do return end\nfor i = 1, 2 do\nend\nrepeat\nuntil true\n", None),
    (b"-- return M\nlocal s = 'return'\nlocal t = \"a\\\"return\"\n", None),
    (b"local s = [[\nreturn M\n]]\n--[==[\nreturn M\n]==]\n", None),
    (b"local s = \"a\\z\n   b\"\nreturn s\n", len(b"local s = \"a\\z\n   b\"\n")),
    (b"#!/usr/bin/lua return\nreturn 1\n", len(b"#!/usr/bin/lua return\n")),
    (b"local t = {}\nt.endx = 0x1e return_ = 1\n", None),
])
def test_lua_final_return(data: bytes, expected: int | None) -> None:
    assert kb.lua_final_return(data) == expected


@pytest.mark.parametrize("data", [
    b"local M = {}\nfoo() return M\n",  # return not first on its line
    b"local s = [[\nreturn M\n",  # unterminated long string
    b"--[[ unterminated\nreturn M\n",
    b"local s = 'abc\nreturn M\n",  # unterminated short string
    b"function f()\nreturn M\n",  # unbalanced blocks
    b"end\nreturn M\n",
    b"return 1\nreturn 2\n",
])
def test_lua_final_return_refuses_what_it_cannot_analyse(data: bytes) -> None:
    with pytest.raises(ScribeSenseError) as e:
        kb.lua_final_return(data)
    assert e.value.code == ErrorCode.ADAPTER_UNAVAILABLE


def test_custom_lua_module_ending_in_return_gets_block_before_return(sandbox, hypr: Path) -> None:  # type: ignore[no-untyped-def]
    (hypr / "hyprland.lua").write_bytes(ML4W_LUA)
    (hypr / "custom.lua").write_bytes(MODULE_LUA)
    before = fingerprint(sandbox.home)
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    data = (hypr / "custom.lua").read_bytes()
    assert data == b"local M = {}\nM.x = 1\n" + kb.render_block("lua", own_lua(sandbox)) + b"return M\n"
    assert data.rstrip().endswith(b"return M")  # nothing after the final return
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    assert (hypr / "custom.lua").read_bytes() == data  # idempotent
    assert keybind_status(backend=HyprlandBackend(FakeRunner())).block == "clean"
    remove_keybinds(backend=HyprlandBackend(FakeRunner()))
    after = fingerprint(sandbox.home)
    after.pop(".config/scribesense", None)
    assert after == before


def test_hyprland_lua_ending_in_return_without_final_newline(sandbox, hypr: Path) -> None:  # type: ignore[no-untyped-def]
    (hypr / "hyprland.lua").write_bytes(b"local a = 1\nreturn")
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    assert (hypr / "hyprland.lua").read_bytes() == (
        b"local a = 1\n" + kb.render_block("lua", own_lua(sandbox)) + b"return")
    remove_keybinds(backend=HyprlandBackend(FakeRunner()))
    assert (hypr / "hyprland.lua").read_bytes() == b"local a = 1\nreturn"


def test_unanalysable_lua_hook_refused_before_anything_is_written(sandbox, hypr: Path) -> None:  # type: ignore[no-untyped-def]
    (hypr / "hyprland.lua").write_bytes(ML4W_LUA)
    (hypr / "custom.lua").write_bytes(b"local M = {}\nfoo() return M\n")
    before = fingerprint(sandbox.home)
    runner = FakeRunner()
    with pytest.raises(ScribeSenseError) as e:
        install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(runner))
    assert e.value.code == ErrorCode.ADAPTER_UNAVAILABLE
    assert fingerprint(sandbox.home) == before and runner.reloads == 0  # not even our own file


# ---------------------------------------------------------------- remove: drift + reload failure (KB-3)

def test_remove_reports_drift_even_when_reload_fails(sandbox, hypr: Path, running: None) -> None:  # type: ignore[no-untyped-def]
    (hypr / "hyprland.conf").write_bytes(CLASSIC + b"# >>> ScribeSense (managed, do not edit) >>>\nsource=x\n")
    (hypr / "hyprland.lua").write_bytes(PLAIN_LUA)
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    runner = FakeRunner(reload_error=ErrorCode.ADAPTER_UNAVAILABLE)  # e.g. uninstall from a TTY
    with pytest.raises(ScribeSenseError) as e:
        remove_keybinds(backend=HyprlandBackend(runner))
    assert e.value.code == ErrorCode.DRIFT_SKIPPED
    assert isinstance(e.value.__cause__, ScribeSenseError)
    assert e.value.__cause__.code == ErrorCode.ADAPTER_UNAVAILABLE
    assert (hypr / "hyprland.lua").read_bytes() == PLAIN_LUA and not own_lua(sandbox).exists()
    assert runner.reloads == 1


# ---------------------------------------------------------------- refused launches (KB-5)

@pytest.mark.parametrize("exc", [PermissionError(13, "denied"), OSError(8, "exec format")])
def test_refused_hyprctl_launch_is_check_unavailable(sandbox, hypr: Path, exc: OSError) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ScribeSenseError) as e:
        conflicts(default_keybinds(), backend=HyprlandBackend(FakeRunner(binds_raise=exc)))
    assert e.value.code == ErrorCode.CHECK_UNAVAILABLE
    (hypr / "hyprland.lua").write_bytes(PLAIN_LUA)
    before = fingerprint(sandbox.home)
    with pytest.raises(ScribeSenseError) as e:
        install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner(binds_raise=exc)))
    assert e.value.code == ErrorCode.CHECK_UNAVAILABLE and fingerprint(sandbox.home) == before


@pytest.mark.parametrize("exc", [PermissionError(13, "denied"), OSError(8, "exec format")])
def test_refused_reload_launch_is_adapter_command_failed(sandbox, hypr: Path, exc: OSError,
                                                         running: None) -> None:  # type: ignore[no-untyped-def]
    (hypr / "hyprland.lua").write_bytes(PLAIN_LUA)
    with pytest.raises(ScribeSenseError) as e:
        install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner(reload_raise=exc)))
    assert e.value.code == ErrorCode.ADAPTER_COMMAND_FAILED
    assert own_lua(sandbox).exists()  # files stay written: they apply at next login
    with pytest.raises(ScribeSenseError) as e:
        remove_keybinds(backend=HyprlandBackend(FakeRunner(reload_raise=exc)))
    assert e.value.code == ErrorCode.ADAPTER_COMMAND_FAILED
    assert (hypr / "hyprland.lua").read_bytes() == PLAIN_LUA


# ---------------------------------------------------------------- status over every candidate (KB-6)

def _install_then_switch_to_ml4w(hypr: Path) -> None:
    (hypr / "hyprland.lua").write_bytes(PLAIN_LUA)
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    data = (hypr / "hyprland.lua").read_bytes()
    (hypr / "hyprland.lua").write_bytes(ML4W_LUA + data[len(PLAIN_LUA):])  # hook is now custom.lua


def test_status_reports_drift_in_another_candidate_file(hypr: Path) -> None:
    _install_then_switch_to_ml4w(hypr)
    _edit_block(hypr / "hyprland.lua")
    s = keybind_status(backend=HyprlandBackend(FakeRunner()))
    assert s.hook_file == hypr / "custom.lua" and s.block == "drifted"
    with pytest.raises(ScribeSenseError) as e:  # what the status predicts
        install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    assert e.value.code == ErrorCode.DRIFT_SKIPPED


def test_status_reports_stale_clean_block(hypr: Path) -> None:
    _install_then_switch_to_ml4w(hypr)
    s = keybind_status(backend=HyprlandBackend(FakeRunner()))
    assert s.hook_file == hypr / "custom.lua"
    assert s.block == "clean" and s.stale_files == (hypr / "hyprland.lua",)
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    s = keybind_status(backend=HyprlandBackend(FakeRunner()))
    assert s.block == "clean" and s.stale_files == ()


def test_reset_and_recovery_never_remove_keybinds() -> None:
    safety = Path(kb.__file__).parent
    for name in ("recovery.py", "revert.py"):
        assert "remove_keybinds" not in (safety / name).read_text()


# ---------------------------------------------------------------- real Lua (integration)

STUB = """
hl = {dsp = {}}
function hl.dsp.exec_cmd(c) return c end
function hl.bind(combo, cmd, opts) print("BIND|" .. combo .. "|" .. cmd .. "|" .. opts.description) end
function hl.on(ev, f) print("ON|" .. ev); f() end
function hl.exec_cmd(c) print("EXEC|" .. c) end
dofile(arg[1])
"""


@pytest.mark.integration
def test_generated_lua_loads_in_a_real_interpreter(sandbox, hypr: Path) -> None:  # type: ignore[no-untyped-def]
    from scribesense.contracts.paths import run_cmd

    lua = shutil.which("lua") or shutil.which("luajit")
    if lua is None:
        pytest.skip("no Lua interpreter")
    (hypr / "hyprland.lua").write_bytes(b"local a = 1")  # no final newline
    weird = '/opt/a "b" \\c $d/scribesense'
    install_keybinds(default_keybinds(), weird, backend=HyprlandBackend(FakeRunner()))
    stub = sandbox.root / "stub.lua"
    stub.write_text(STUB)
    out = run_cmd([lua, str(stub), str(hypr / "hyprland.lua")], 10.0).stdout.decode().splitlines()
    q = "'/opt/a \"b\" \\c $d/scribesense'"
    assert out == [
        f"BIND|SUPER + ALT + R|{q} read --selection|{kb.DESCRIPTIONS['reader']}",
        f"BIND|SUPER + ALT + P|{q} preset next|{kb.DESCRIPTIONS['preset_next']}",
        f"BIND|SUPER + CTRL + ALT + SHIFT + BackSpace|{q} reset|{kb.DESCRIPTIONS['reset']}",
        "ON|hyprland.start",
        f"EXEC|{q} recover --login",
    ]
    own_lua(sandbox).unlink()  # pcall guard: a missing file never breaks the config
    assert run_cmd([lua, str(stub), str(hypr / "hyprland.lua")], 10.0).stdout == b""


REQUIRE_STUB = """
package.path = arg[1] .. "/?.lua;" .. package.path
hl = {dsp = {}}
function hl.dsp.exec_cmd(c) return c end
function hl.bind(combo, cmd, opts) print("BIND|" .. combo) end
function hl.on(ev, f) end
local m = require("custom")
print("RET|" .. tostring(m.x))
"""


@pytest.mark.integration
def test_custom_module_ending_in_return_still_loads(sandbox, hypr: Path) -> None:  # type: ignore[no-untyped-def]
    from scribesense.contracts.paths import run_cmd

    lua = shutil.which("lua") or shutil.which("luajit")
    if lua is None:
        pytest.skip("no Lua interpreter")
    (hypr / "hyprland.lua").write_bytes(ML4W_LUA)
    (hypr / "custom.lua").write_bytes(MODULE_LUA)
    install_keybinds(default_keybinds(), CMD, backend=HyprlandBackend(FakeRunner()))
    stub = sandbox.root / "require_stub.lua"
    stub.write_text(REQUIRE_STUB)
    out = run_cmd([lua, str(stub), str(hypr)], 10.0).stdout.decode().splitlines()
    assert out == ["BIND|SUPER + ALT + R", "BIND|SUPER + ALT + P",
                   "BIND|SUPER + CTRL + ALT + SHIFT + BackSpace", "RET|1"]
