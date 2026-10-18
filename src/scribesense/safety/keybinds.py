"""O4 — Keybind writer (M4.14). Not a K3 adapter: install (M3.8) and the settings screen (M2.7) call it.

Team decisions D1–D5 (2026-10-12):
  Model     Keybind(action, mods, key) is format-neutral. Actions are a closed set (ACTIONS); the K9
            SETTINGS strings ("SUPER ALT, R", ...) parse into it (parse_keybind / default_keybinds).
            Anything that could inject config (quotes, commas, newlines, braces, spaces in the key) is
            refused with ValueError.
  D2        ScribeSense owns ONE generated file outside the user's dotfiles:
            config_home()/scribesense/hyprland.lua (Lua config) or .../hyprland.conf (classic config).
            It holds the binds and (D4) the login-recovery hook (`<cmd> recover --login`, A3).
            remove_keybinds() also removes config_home()/scribesense/ when it is empty afterwards
            (never when it holds anything else; never anything outside it).
  D1        Exactly one managed include block in one hook file, between marker comment lines:
              Lua      hook = hypr/custom.lua if hyprland.lua loads a "custom" module (ML4W-style rice;
                       custom.lua is created when missing), else hypr/hyprland.lua itself.
                       Block body: pcall(dofile, "<our file>")  — a missing/broken file never breaks
                       the user's config.
              classic  hook = hypr/hyprland.conf. Block body: source = <our file>
            The block is appended at the end — except in a Lua hook file ending in a top-level
            `return` (e.g. a module loaded with require): Lua allows nothing after it, so the block
            goes on its own lines just before that `return` (lua_final_return); a Lua file that
            cannot be analysed safely is refused (ADAPTER_UNAVAILABLE, nothing written).
            Every byte outside the block is preserved; installing twice
            keeps one block; writes are atomic + durable and go THROUGH symlinks (realpath), so a
            dotfiles symlink stays a symlink. A block whose bytes differ from what we would write
            (hand-edited, duplicated, broken markers) is never touched: DRIFT_SKIPPED.
            Exact restoration: two facts are not visible in the user's bytes and are carried by the
            start-marker line itself — "+newline" (we added a newline because the file did not end
            with one) and "+created" (we created the hook file). remove_keybinds() uses them to give
            back the file byte-for-byte (and to delete a hook file we created if it is empty again).
  D5        The command is a parameter (absolute path of the scribesense executable); it is shell-quoted
            (shlex) and then escaped for Lua / classic syntax.
  D3        After install/remove, `hyprctl reload` through the injectable runner (10 s). A reload
            failure is raised (the run_cmd code) but the files stay written: they apply at next login.
            Item 4 (2026-10-12): Hyprland not running (HYPRLAND_INSTANCE_SIGNATURE unset or empty, read
            at call time) -> the reload is skipped silently; the files apply at the next login.
            install_keybinds() reports which happened in KeybindStatus.reload ("reloaded" / "skipped").
  Conflicts `hyprctl binds -j` (read-only). Same modmask + same key (case-insensitive) in the global
            submap = conflict; binds that run a ScribeSense command are ignored. hyprctl unusable ->
            CHECK_UNAVAILABLE (also when Hyprland is not running: the check needs a live Hyprland;
            install_keybinds(allow_unchecked=True) still installs). install_keybinds() refuses on
            conflict (KEYBIND_CONFLICT).

`scribesense reset` / login recovery must NOT remove keybinds (M4.12 "Must never"): nothing in
safety.recovery or safety.revert calls remove_keybinds(); only uninstall (M3.8) does.

Other desktops: DesktopBackend is the seam; HyprlandBackend is the only implementation.
Privacy: logs carry codes and fixed identifiers only — never paths, commands or bind text.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol

from scribesense.adapters.fontconfig import delete_durable, write_durable
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.paths import config_home, run_cmd
from scribesense.contracts.store import SETTINGS
from scribesense.log import log

# ---------------------------------------------------------------- model

#: action -> scribesense arguments (K10).
ACTIONS: dict[str, tuple[str, ...]] = {
    "reader": ("read", "--selection"),
    "preset_next": ("preset", "next"),
    "reset": ("reset",),
}
#: action -> K9 settings key holding its keybind text.
SETTING_KEYS: dict[str, str] = {
    "reader": "keybind_reader",
    "preset_next": "keybind_preset_next",
    "reset": "keybind_reset",
}
DESCRIPTIONS: dict[str, str] = {
    "reader": "ScribeSense: open the selected text in the reader",
    "preset_next": "ScribeSense: switch to the next preset",
    "reset": "ScribeSense: reset (restore original settings)",
}
LOGIN_ARGS: tuple[str, ...] = ("recover", "--login")

#: Canonical modifier order and the Hyprland modmask bit of each.
MOD_ORDER: tuple[str, ...] = ("SUPER", "CTRL", "ALT", "SHIFT")
MOD_BITS: dict[str, int] = {"SHIFT": 1, "CAPS": 2, "CTRL": 4, "ALT": 8, "MOD2": 16, "MOD3": 32, "SUPER": 64, "MOD5": 128}
_DISPLAY_ORDER: tuple[str, ...] = (*MOD_ORDER, "CAPS", "MOD2", "MOD3", "MOD5")
MOD_ALIASES: dict[str, str] = {
    "SUPER": "SUPER", "WIN": "SUPER", "LOGO": "SUPER", "MOD4": "SUPER", "META": "SUPER",
    "CTRL": "CTRL", "CONTROL": "CTRL",
    "ALT": "ALT", "MOD1": "ALT",
    "SHIFT": "SHIFT",
}
# Always used with fullmatch(): "$" would also accept a trailing "\n" (config-line injection).
_KEY = re.compile(r"[A-Za-z0-9_]{1,64}")
_BIND_TEXT = re.compile(r"[A-Za-z0-9_ +,]{1,200}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def normalize_mod(name: str) -> str:
    """'control' -> 'CTRL', 'win' -> 'SUPER', ...; anything else -> ValueError."""
    if not isinstance(name, str):
        raise ValueError("modifier must be a string")
    mod = MOD_ALIASES.get(name.strip().upper())
    if mod is None:
        raise ValueError("unknown modifier")
    return mod


@dataclass(frozen=True)
class Keybind:
    """One ScribeSense keybind. mods are normalised to MOD_ORDER and deduplicated on creation."""

    action: str
    mods: tuple[str, ...]
    key: str

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError("unknown keybind action")
        if isinstance(self.mods, str) or not isinstance(self.mods, Iterable):
            raise ValueError("mods must be a sequence of modifier names")
        mods = {normalize_mod(m) for m in self.mods}
        if not mods:
            raise ValueError("a keybind needs at least one modifier")
        if not isinstance(self.key, str) or not _KEY.fullmatch(self.key):
            raise ValueError("invalid key name")
        object.__setattr__(self, "mods", tuple(m for m in MOD_ORDER if m in mods))

    @property
    def modmask(self) -> int:
        mask = 0
        for m in self.mods:
            mask |= MOD_BITS[m]
        return mask

    def combo(self) -> tuple[int, str]:
        """What Hyprland matches on: modmask + key (case-insensitive)."""
        return self.modmask, self.key.casefold()


def parse_keybind(text: str, action: str) -> Keybind:
    """K9 settings text ("SUPER ALT, R") -> Keybind for `action`. Mods may be separated by spaces or '+'.

    The action is a parameter because the settings text holds only the combination (the settings key
    names the action: SETTING_KEYS). Refuses anything outside [A-Za-z0-9_ +,] with ValueError.
    """
    if not isinstance(text, str) or not _BIND_TEXT.fullmatch(text) or text.count(",") != 1:
        raise ValueError("invalid keybind text")
    mods_part, key_part = text.split(",")
    key = key_part.strip()
    if " " in key or "+" in key:
        raise ValueError("invalid key name")
    mods = tuple(m for m in mods_part.replace("+", " ").split() if m)
    return Keybind(action, mods, key)


def format_keybind(bind: Keybind) -> str:
    """Keybind -> K9 settings text ("SUPER ALT, R"); parse_keybind() round-trips it."""
    return f"{' '.join(bind.mods)}, {bind.key}"


def default_keybinds() -> tuple[Keybind, ...]:
    """The K9 SETTINGS defaults, one bind per action, in ACTIONS order."""
    return tuple(parse_keybind(str(SETTINGS[SETTING_KEYS[a]][1]), a) for a in ACTIONS)


def _check_binds(binds: Sequence[Keybind]) -> tuple[Keybind, ...]:
    result = tuple(binds)
    if not all(isinstance(b, Keybind) for b in result):
        raise ValueError("binds must be Keybind objects")
    if len({b.action for b in result}) != len(result):
        raise ValueError("one bind per action")
    if len({b.combo() for b in result}) != len(result):
        raise ValueError("two binds use the same key combination")
    return result


def check_command(command: str) -> str:
    """The scribesense executable: an absolute path with nothing that a config line could not carry.

    Commas and '#' are refused too: classic Hyprland syntax splits on ',' and comments on '#'.
    """
    if not isinstance(command, str) or not command or not os.path.isabs(command):
        raise ValueError("command must be an absolute path")
    if _CONTROL.search(command) or "," in command or "#" in command or not command.isprintable():
        raise ValueError("command contains characters a config line cannot carry")
    return command


def shell_command(command: str, args: Sequence[str]) -> str:
    """The shell line Hyprland runs (exec goes through /bin/sh -c)."""
    return shlex.join([check_command(command), *args])


def lua_string(text: str) -> str:
    """A double-quoted Lua string literal: backslash, quote and control characters escaped (\\ddd)."""
    out = ['"']
    for ch in text:
        code = ord(ch)
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\{code:03d}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


# ---------------------------------------------------------------- generated files

Format = Literal["lua", "classic"]
GENERATED_MARK = "Generated by ScribeSense"
_HEADER = (
    f"{GENERATED_MARK}. Do not edit: this file is rewritten by ScribeSense and removed by"
    " `scribesense uninstall`.",
    "`scribesense reset` does not remove these keybinds.",
)


def render_own_file(fmt: Format, binds: Sequence[Keybind], command: str, *, include_login_hook: bool = True) -> bytes:
    """Our whole file for `fmt`. Deterministic: same inputs -> same bytes."""
    binds = _check_binds(binds)
    check_command(command)
    lines: list[str] = []
    if fmt == "lua":
        lines += [f"-- {h}" for h in _HEADER]
        for b in binds:
            combo = " + ".join((*b.mods, b.key))
            cmd = shell_command(command, ACTIONS[b.action])
            lines.append(
                f"hl.bind({lua_string(combo)}, hl.dsp.exec_cmd({lua_string(cmd)}),"
                f" {{ description = {lua_string(DESCRIPTIONS[b.action])} }})"
            )
        if include_login_hook:
            login = shell_command(command, LOGIN_ARGS)
            lines.append(f'hl.on("hyprland.start", function() hl.exec_cmd({lua_string(login)}) end)')
    elif fmt == "classic":
        lines += [f"# {h}" for h in _HEADER]
        for b in binds:
            lines.append(f"bind = {' '.join(b.mods)}, {b.key}, exec, {shell_command(command, ACTIONS[b.action])}")
        if include_login_hook:
            lines.append(f"exec-once = {shell_command(command, LOGIN_ARGS)}")
    else:
        raise ValueError("unknown format")
    return ("\n".join(lines) + "\n").encode("utf-8")


Variant = Literal["plain", "newline", "created"]
_COMMENT: dict[str, str] = {"lua": "--", "classic": "#"}
_START = ">>> ScribeSense (managed, do not edit) >>>"
_END = "<<< ScribeSense <<<"
_VARIANT_SUFFIX: dict[str, str] = {"plain": "", "newline": " +newline", "created": " +created"}


def _encode(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


def block_body(fmt: Format, own_file: Path) -> str:
    """The include line. Raises ADAPTER_UNAVAILABLE if the path cannot be written into the config."""
    path = str(own_file)
    if not os.path.isabs(path) or _CONTROL.search(path):
        raise ScribeSenseError(ErrorCode.ADAPTER_UNAVAILABLE)
    if fmt == "lua":
        return f"pcall(dofile, {lua_string(path)})"
    if "#" in path or path != path.strip():
        raise ScribeSenseError(ErrorCode.ADAPTER_UNAVAILABLE)
    return f"source = {path}"


def render_block(fmt: Format, own_file: Path, variant: Variant = "plain") -> bytes:
    c = _COMMENT[fmt]
    start = f"{c} {_START}{_VARIANT_SUFFIX[variant]}"
    return _encode(f"{start}\n{block_body(fmt, own_file)}\n{c} {_END}\n")


BlockState = Literal["absent", "clean", "drifted"]


@dataclass(frozen=True)
class Block:
    state: BlockState
    begin: int = 0  # byte span of the clean block (start-marker line .. end-marker line incl. "\n")
    end: int = 0
    variant: Variant = "plain"


def find_block(data: bytes, fmt: Format, own_file: Path) -> Block:
    """Locate our block. Anything but exactly one block byte-equal to a render_block() variant is drift."""
    c = _encode(_COMMENT[fmt])
    starts = [m.start() for m in re.finditer(rb"(?m)^[ \t]*" + re.escape(c + b" " + _encode(_START)), data)]
    ends = [m.start() for m in re.finditer(rb"(?m)^[ \t]*" + re.escape(c + b" " + _encode(_END)), data)]
    if not starts and not ends:
        return Block("absent")
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        return Block("drifted")
    begin = starts[0]
    nl = data.find(b"\n", ends[0])
    end = len(data) if nl < 0 else nl + 1
    region = data[begin:end]
    for variant in ("plain", "newline", "created"):
        if region == render_block(fmt, own_file, variant):  # type: ignore[arg-type]
            return Block("clean", begin, end, variant)  # type: ignore[arg-type]
    return Block("drifted")


_LUA_LONG_OPEN = re.compile(rb"\[(=*)\[")
_LUA_WORD = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*")
_LUA_NUMBER = re.compile(rb"[0-9.](?:[eEpP][+-]|[0-9A-Za-z_.])*")
_LUA_OPENERS = frozenset({b"function", b"do", b"if", b"repeat"})  # while/for open with their `do`
_LUA_CLOSERS = frozenset({b"end", b"until"})
_LUA_SPACE = frozenset(b" \t\r\n\f\v")


def _lua_refuse() -> ScribeSenseError:
    log(ErrorCode.ADAPTER_UNAVAILABLE, "keybinds", step="install", reason="lua_unparsed")
    return ScribeSenseError(ErrorCode.ADAPTER_UNAVAILABLE)


def _lua_skip_long(data: bytes, m: re.Match[bytes]) -> int:
    close = b"]" + m.group(1) + b"]"
    j = data.find(close, m.end())
    if j < 0:
        raise _lua_refuse()
    return j + len(close)


def _lua_skip_short(data: bytes, i: int) -> int:
    quote, j, n = data[i], i + 1, len(data)
    while j < n:
        ch = data[j]
        if ch == quote:
            return j + 1
        if ch == 0x0A:
            break  # an unescaped newline ends a short string: syntax error
        if ch == 0x5C:  # backslash
            nxt = data[j + 1: j + 2]
            if nxt == b"z":  # \z skips the following whitespace, newlines included
                j += 2
                while j < n and data[j] in _LUA_SPACE:
                    j += 1
                continue
            j += 3 if data[j + 1: j + 3] == b"\r\n" else 2
            continue
        j += 1
    raise _lua_refuse()


def lua_final_return(data: bytes) -> int | None:
    """Byte offset of the line holding a top-level `return` (Lua allows nothing after it), or None.

    A minimal Lua lexer: comments, short and long strings and numbers are skipped; block depth is
    counted from function/do/if/repeat .. end/until. Raises ADAPTER_UNAVAILABLE when the file cannot be
    analysed safely (unterminated string or comment, unbalanced blocks, two top-level returns, or the
    top-level `return` is not the first token on its line): inserting there could break the config.
    """
    i, n, depth = 0, len(data), 0
    ret: int | None = None
    if data.startswith(b"#"):  # first line skipped by Lua's loadfile (shebang)
        nl = data.find(b"\n")
        i = n if nl < 0 else nl
    while i < n:
        ch = data[i]
        if ch in _LUA_SPACE:
            i += 1
            continue
        if data.startswith(b"--", i):
            m = _LUA_LONG_OPEN.match(data, i + 2)
            if m:
                i = _lua_skip_long(data, m)
            else:
                nl = data.find(b"\n", i)
                i = n if nl < 0 else nl
            continue
        if ch == 0x5B:  # "["
            m = _LUA_LONG_OPEN.match(data, i)
            i = _lua_skip_long(data, m) if m else i + 1
            continue
        if ch in (0x22, 0x27):  # " '
            i = _lua_skip_short(data, i)
            continue
        m = _LUA_WORD.match(data, i)
        if m:
            word = m.group()
            if word in _LUA_OPENERS:
                depth += 1
            elif word in _LUA_CLOSERS:
                depth -= 1
                if depth < 0:
                    raise _lua_refuse()
            elif word == b"return" and depth == 0:
                if ret is not None:
                    raise _lua_refuse()
                ret = i
            i = m.end()
            continue
        if 0x30 <= ch <= 0x39 or (ch == 0x2E and data[i + 1: i + 2].isdigit()):
            m = _LUA_NUMBER.match(data, i)
            i = m.end() if m else i + 1
            continue
        i += 1
    if depth != 0:
        raise _lua_refuse()
    if ret is None:
        return None
    line_start = data.rfind(b"\n", 0, ret) + 1
    if data[line_start:ret].strip(b" \t"):
        raise _lua_refuse()
    return line_start


def insert_block(data: bytes | None, fmt: Format, own_file: Path) -> bytes:
    """Hook-file bytes with our block added (data None = the file does not exist yet).

    Appended at the end, except in a Lua file ending in a top-level `return` (a module loaded with
    require): there the block goes on its own lines just before that `return`, so the file still parses.
    """
    if data is None:
        return render_block(fmt, own_file, "created")
    if fmt == "lua":
        pos = lua_final_return(data)
        if pos is not None:
            return data[:pos] + render_block(fmt, own_file, "plain") + data[pos:]
    if data and not data.endswith(b"\n"):
        return data + b"\n" + render_block(fmt, own_file, "newline")
    return data + render_block(fmt, own_file, "plain")


def strip_block(data: bytes, block: Block) -> bytes | None:
    """Hook-file bytes without our (clean) block; None = delete the file (we created it, nothing else left)."""
    head, tail = data[: block.begin], data[block.end:]
    if block.variant == "newline" and not tail and head.endswith(b"\n"):
        head = head[:-1]
    result = head + tail
    if block.variant == "created" and not result:
        return None
    return result


# ---------------------------------------------------------------- live binds / conflicts

@dataclass(frozen=True)
class LiveBind:
    """One bind as `hyprctl binds -j` reports it. For display only — never logged."""

    modmask: int
    key: str
    dispatcher: str
    arg: str
    description: str = ""

    @property
    def mods(self) -> tuple[str, ...]:
        return tuple(name for name in _DISPLAY_ORDER if self.modmask & MOD_BITS[name])


@dataclass(frozen=True)
class Conflict:
    ours: Keybind
    theirs: LiveBind


def parse_hyprctl_binds(raw: bytes | str) -> list[LiveBind]:
    """Global-submap keyboard binds from `hyprctl binds -j`. Not JSON / not a list -> CHECK_UNAVAILABLE."""
    try:
        items = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ScribeSenseError(ErrorCode.CHECK_UNAVAILABLE) from None
    if not isinstance(items, list):
        raise ScribeSenseError(ErrorCode.CHECK_UNAVAILABLE)
    result: list[LiveBind] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mask, key = item.get("modmask"), item.get("key")
        if isinstance(mask, bool) or not isinstance(mask, int) or not isinstance(key, str) or not key:
            continue  # keycode-only (code:NN) binds cannot be compared by name
        if item.get("mouse") is True or item.get("submap", "") not in ("", None):
            continue

        def text(name: str) -> str:
            v = item.get(name)
            return v if isinstance(v, str) else ""

        result.append(LiveBind(mask, key, text("dispatcher"), text("arg"), text("description")))
    return result


def is_own_command(arg: str) -> bool:
    """True if a bind runs `<.../scribesense> <one of our action args>` (our own earlier install)."""
    try:
        tokens = shlex.split(arg)
    except ValueError:
        return False
    if not tokens or os.path.basename(tokens[0]) != "scribesense":
        return False
    return tuple(tokens[1:]) in set(ACTIONS.values())


def find_conflicts(binds: Sequence[Keybind], live: Iterable[LiveBind]) -> list[Conflict]:
    live = [lb for lb in live if not is_own_command(lb.arg)]
    out: list[Conflict] = []
    for b in binds:
        mask, key = b.combo()
        out += [Conflict(b, lb) for lb in live if lb.modmask == mask and lb.key.casefold() == key]
    return out


# ---------------------------------------------------------------- backends

#: What install_keybinds() did about `hyprctl reload`: "reloaded" (it ran and succeeded) or "skipped"
#: (Hyprland not running — the files apply at the next login). A failed reload raises instead.
ReloadOutcome = Literal["reloaded", "skipped"]


@dataclass(frozen=True)
class KeybindStatus:
    backend: str
    format: Format | None  # None: no supported config found
    own_file: Path | None
    own_file_present: bool
    hook_file: Path | None
    #: Over every candidate hook file of the format: "drifted" if any holds a hand-edited block (install
    #: and remove then refuse / skip), else "clean" if any includes our file, else "absent".
    block: BlockState
    #: Candidate files other than hook_file that still hold a clean block (install moves it to hook_file).
    stale_files: tuple[Path, ...] = ()
    #: Set only on the status install_keybinds() returns; None from keybind_status() (no reload tried).
    reload: ReloadOutcome | None = None


class DesktopBackend(Protocol):
    """Seam for other desktops. All file work is the backend's; the public functions orchestrate."""

    name: str

    def status(self) -> KeybindStatus: ...
    def write(self, binds: Sequence[Keybind], command: str, include_login_hook: bool) -> None: ...
    def unwrite(self) -> tuple[bool, bool]: ...  # (changed anything, drift left untouched)
    def live_binds(self) -> list[LiveBind]: ...
    def activate(self) -> bool: ...  # True: reloaded; False: skipped (desktop not running)


Runner = Callable[[list[str], float], Any]
#: hyprctl deadlines (seconds).
TIMEOUTS: dict[str, float] = {"hyprctl": 10.0}
_REQUIRE_CUSTOM = re.compile(rb"""(?m)^[^\n-]*\brequire\s*\(?\s*["']custom["']""")


def _read(path: Path) -> bytes | None:
    """File bytes (through symlinks), None if absent. A non-regular file -> NOT_WRITABLE."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise ScribeSenseError(ErrorCode.NOT_WRITABLE) from None
    if not stat.S_ISREG(st.st_mode):
        raise ScribeSenseError(ErrorCode.NOT_WRITABLE)
    try:
        return path.read_bytes()
    except OSError:
        raise ScribeSenseError(ErrorCode.NOT_WRITABLE) from None


def _real(path: Path) -> Path:
    """Where a write to `path` must land so a symlink (to the file or any parent dir) is preserved."""
    real = Path(os.path.realpath(path))
    if not real.parent.is_dir():
        raise ScribeSenseError(ErrorCode.NOT_WRITABLE)  # dangling link into a missing directory
    return real


def _write_through(path: Path, data: bytes) -> None:
    real = _real(path)
    try:
        mode = stat.S_IMODE(os.stat(real).st_mode)
    except FileNotFoundError:
        mode = 0o644
    except OSError:
        raise ScribeSenseError(ErrorCode.NOT_WRITABLE) from None
    write_durable(real, data, mode)


class HyprlandBackend:
    """Hyprland, Lua (hyprland.lua) or classic (hyprland.conf) config. Paths are computed per call."""

    name = "hyprland"

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner = runner

    def _run(self, argv: list[str]) -> Any:
        runner = self._runner if self._runner is not None else run_cmd
        return runner(argv, TIMEOUTS["hyprctl"])

    # paths
    @staticmethod
    def hypr_dir() -> Path:
        return config_home() / "hypr"

    @staticmethod
    def own_dir() -> Path:
        """config_home()/scribesense/ — the directory of our generated file (D2)."""
        return config_home() / "scribesense"

    @staticmethod
    def own_file(fmt: Format) -> Path:
        return HyprlandBackend.own_dir() / ("hyprland.lua" if fmt == "lua" else "hyprland.conf")

    def detect(self) -> Format:
        d = self.hypr_dir()
        if (d / "hyprland.lua").is_file():
            return "lua"
        if (d / "hyprland.conf").is_file():
            return "classic"
        raise ScribeSenseError(ErrorCode.ADAPTER_UNAVAILABLE)

    def hook_file(self, fmt: Format) -> Path:
        d = self.hypr_dir()
        if fmt == "classic":
            return d / "hyprland.conf"
        entry = _read(d / "hyprland.lua") or b""
        return d / "custom.lua" if _REQUIRE_CUSTOM.search(entry) else d / "hyprland.lua"

    def _candidates(self, fmt: Format) -> list[Path]:
        d = self.hypr_dir()
        return [d / "hyprland.lua", d / "custom.lua"] if fmt == "lua" else [d / "hyprland.conf"]

    # protocol
    def status(self) -> KeybindStatus:
        try:
            fmt = self.detect()
        except ScribeSenseError:
            return KeybindStatus(self.name, None, None, False, None, "absent")
        own, hook = self.own_file(fmt), self.hook_file(fmt)
        states: dict[Path, BlockState] = {}
        for path in dict.fromkeys([hook, *self._candidates(fmt)]):
            data = _read(path)
            states[path] = find_block(data, fmt, own).state if data is not None else "absent"
        values = set(states.values())
        block: BlockState = "drifted" if "drifted" in values else "clean" if "clean" in values else "absent"
        stale = tuple(p for p, st in states.items() if p != hook and st == "clean")
        return KeybindStatus(self.name, fmt, own, own.is_file(), hook, block, stale)

    def write(self, binds: Sequence[Keybind], command: str, include_login_hook: bool) -> None:
        fmt = self.detect()
        own, hook = self.own_file(fmt), self.hook_file(fmt)
        content = render_own_file(fmt, binds, command, include_login_hook=include_login_hook)
        render_block(fmt, own)  # path representable? (raises before anything is written)
        # Plan everything first: any drift -> refuse before the first write.
        hook_data = _read(hook)
        hook_block = find_block(hook_data, fmt, own) if hook_data is not None else Block("absent")
        drift = hook_block.state == "drifted"
        stale: list[tuple[Path, bytes, Block]] = []
        for other in self._candidates(fmt):
            if other == hook:
                continue
            data = _read(other)
            if data is None:
                continue
            blk = find_block(data, fmt, own)
            if blk.state == "clean":
                stale.append((other, data, blk))  # an older hook location: one include only
            drift = drift or blk.state == "drifted"
        if drift:
            log(ErrorCode.DRIFT_SKIPPED, "keybinds", step="install", kind=fmt)
            raise ScribeSenseError(ErrorCode.DRIFT_SKIPPED)
        new_hook = insert_block(hook_data, fmt, own) if hook_block.state == "absent" else None
        # Our file first, then the include: a crash in between leaves only an unused file.
        if _read(own) != content:
            write_durable(own, content, 0o644)
        if new_hook is not None:
            _write_through(hook, new_hook)
        for other, data, blk in stale:
            self._strip(other, data, blk)

    def _strip(self, path: Path, data: bytes, block: Block) -> None:
        new = strip_block(data, block)
        if new is None:
            delete_durable(_real(path))
        else:
            _write_through(path, new)

    def unwrite(self) -> tuple[bool, bool]:
        """Remove our blocks (both formats, every candidate hook file) and our files. Drift is skipped.

        Then config_home()/scribesense/ itself is removed if it is EMPTY (O3 review note (1),
        2026-10-18) — see _remove_own_dir_if_empty.
        """
        changed = drifted = False
        for fmt in ("lua", "classic"):
            own = self.own_file(fmt)  # type: ignore[arg-type]
            keep_own = False
            for path in self._candidates(fmt):  # type: ignore[arg-type]
                data = _read(path)
                if data is None:
                    continue
                blk = find_block(data, fmt, own)  # type: ignore[arg-type]
                if blk.state == "clean":
                    self._strip(path, data, blk)
                    changed = True
                elif blk.state == "drifted":
                    # The hand-edited block may still include our file: keep both.
                    keep_own = drifted = True
                    log(ErrorCode.DRIFT_SKIPPED, "keybinds", step="remove", kind=fmt)
            if not keep_own and own.exists():
                delete_durable(own)
                changed = True
        self._remove_own_dir_if_empty()
        return changed, drifted

    def _remove_own_dir_if_empty(self) -> None:
        """os.rmdir(config_home()/scribesense) — best effort, never anything else.

        rmdir removes only an empty directory and never follows a symlink, so a directory that still
        holds anything (a drifted block's file, another module's or the user's files) or a symlinked
        one is left as it is; any OSError is ignored. Not counted as a change (Hyprland never reads
        the directory itself) and not fsynced: a crash at worst leaves an empty directory behind.
        """
        try:
            os.rmdir(self.own_dir())
        except OSError:
            pass

    def live_binds(self) -> list[LiveBind]:
        try:
            proc = self._run(["hyprctl", "binds", "-j"])
        except (ScribeSenseError, OSError):  # OSError: launch refused (run_cmd lets it through)
            raise ScribeSenseError(ErrorCode.CHECK_UNAVAILABLE) from None
        return parse_hyprctl_binds(getattr(proc, "stdout", b"") or b"")

    @staticmethod
    def running() -> bool:
        """Hyprland is running for this session: HYPRLAND_INSTANCE_SIGNATURE set and non-empty (read now)."""
        return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))

    def activate(self) -> bool:
        """`hyprctl reload` -> True. Hyprland not running -> skipped silently, False (applies at next login).

        run_cmd codes propagate; a refused launch (OSError) -> ADAPTER_COMMAND_FAILED.
        """
        if not self.running():
            return False
        try:
            self._run(["hyprctl", "reload"])
        except ScribeSenseError as exc:
            log(exc.code, "keybinds", step="reload")
            raise
        except OSError:
            log(ErrorCode.ADAPTER_COMMAND_FAILED, "keybinds", step="reload")
            raise ScribeSenseError(ErrorCode.ADAPTER_COMMAND_FAILED) from None
        return True


# ---------------------------------------------------------------- public API

def _backend(backend: DesktopBackend | None) -> DesktopBackend:
    return backend if backend is not None else HyprlandBackend()


def conflicts(binds: Sequence[Keybind], *, backend: DesktopBackend | None = None) -> list[Conflict]:
    """Live binds that use the same combination as one of `binds` (ScribeSense's own are ignored)."""
    return find_conflicts(_check_binds(binds), _backend(backend).live_binds())


def install_keybinds(
    binds: Sequence[Keybind],
    command: str,
    *,
    include_login_hook: bool = True,
    allow_unchecked: bool = False,
    backend: DesktopBackend | None = None,
) -> KeybindStatus:
    """Write our file + the include block, then reload. Idempotent.

    Hyprland not running (e.g. installing from a TTY or before the first login): the conflict check
    needs a live Hyprland, so this raises CHECK_UNAVAILABLE and writes NOTHING by default. The
    installer (M3.8) must pass allow_unchecked=True ON PURPOSE in that case — a deliberate choice to
    install without a conflict check, never a default. allow_unchecked only skips an unavailable
    check: a conflict that IS found still refuses (KEYBIND_CONFLICT).

    Returns the status after the write with `reload` set: "reloaded", or "skipped" when Hyprland is not
    running (HYPRLAND_INSTANCE_SIGNATURE unset/empty) — not an error, the files apply at the next login.
    Raises ValueError (bad binds/command), KEYBIND_CONFLICT (nothing written; conflicts() names them),
    CHECK_UNAVAILABLE (hyprctl unusable and allow_unchecked is False; nothing written),
    ADAPTER_UNAVAILABLE (no Hyprland config, or a Lua hook file that cannot be analysed safely;
    nothing written), DRIFT_SKIPPED (hand-edited block; nothing written),
    write errors (NOT_WRITABLE, NO_DISK_SPACE, WRITE_NOT_DURABLE), or the reload's run_cmd code
    (files stay written and apply at next login).
    """
    binds = _check_binds(binds)
    check_command(command)
    be = _backend(backend)
    try:
        found = find_conflicts(binds, be.live_binds())
    except ScribeSenseError as exc:
        if exc.code != ErrorCode.CHECK_UNAVAILABLE or not allow_unchecked:
            log(exc.code, "keybinds", step="conflicts")
            raise
        found = []
    if found:
        log(ErrorCode.KEYBIND_CONFLICT, "keybinds", step="install", reason=found[0].ours.action)
        raise ScribeSenseError(ErrorCode.KEYBIND_CONFLICT)
    be.write(binds, command, include_login_hook)
    reloaded = be.activate()
    return replace(be.status(), reload="reloaded" if reloaded else "skipped")


def remove_keybinds(*, backend: DesktopBackend | None = None) -> bool:
    """Uninstall only (M3.8) — never called by reset or login recovery.

    Removes every clean block and our files, restoring each hook file byte-for-byte, then
    config_home()/scribesense/ if it is now empty (kept if it holds anything else). Returns True if
    anything changed (then reloads; skipped silently when Hyprland is not running). Hand-edited blocks are left untouched, as is the file they may
    include, and DRIFT_SKIPPED is raised after the rest was removed.

    Order when several things go wrong: DRIFT_SKIPPED wins (the user must be told a block is left);
    a reload failure is then attached as its __cause__. Without drift the reload's code is raised.
    """
    be = _backend(backend)
    changed, drifted = be.unwrite()
    reload_error: ScribeSenseError | None = None
    if changed:
        try:
            be.activate()
        except ScribeSenseError as exc:
            reload_error = exc
    if drifted:
        raise ScribeSenseError(ErrorCode.DRIFT_SKIPPED) from reload_error
    if reload_error is not None:
        raise reload_error
    return changed


def keybind_status(*, backend: DesktopBackend | None = None) -> KeybindStatus:
    """Format, our file (and whether it exists), hook file, block absent / clean / drifted. `reload` is None."""
    return _backend(backend).status()
