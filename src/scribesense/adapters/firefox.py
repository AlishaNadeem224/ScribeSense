"""O4 — Firefox-family adapter (M4.6): restyle web pages in Firefox and Zen, every profile (F1, F3, A16).

Install roots (only those that exist; all computed from contracts.paths.home()):
    firefox          native Firefox     home()/.mozilla/firefox
    firefox-xdg      native Firefox     home()/.config/mozilla/firefox   (XDG layout of newer Firefox versions)
    firefox-flatpak  Flatpak Firefox    home()/.var/app/org.mozilla.firefox/.mozilla/firefox
    zen-flatpak      Flatpak Zen        home()/.var/app/app.zen_browser.zen/.zen
    zen              native Zen         home()/.zen
Each root's profiles.ini lists [ProfileN] sections (Name=, IsRelative=1/0, Path=). One Target per profile
directory that exists: target_id "firefox:<root label>:<h>", <h> = first 12 hex digits of sha256 of the profile
directory's absolute path as discovered (symlinks NOT resolved; os.fsencode bytes). Profile directory names can
hold a name the user chose ("xxxx.Jane-Work") and target ids may be logged, so no part of the name is in the id;
the human-readable "<browser> (<profile name>)" is only in display_name (shown in the UI, never logged). The id
is stable per profile directory. A malformed or unreadable profiles.ini yields no targets for that root (logged
by code, never by content), never a crash.

Locations per profile (K3 rule 10, kind "file", absolute paths):
    <profile>/user.js                  ONE managed block enabling userContent.css:
                                         // >>> ScribeSense (managed) >>>
                                         user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);
                                         // <<< ScribeSense <<<
    <profile>/chrome/userContent.css   ONE managed block holding browser_css(fonts, config) (M4.8; this module
                                       never writes CSS of its own, so no letter/word spacing can get in).
    <profile>/prefs.js#toolkit.legacyUserProfileCustomizations.stylesheets
                                       ONE key location (K3 rule 10b, kind "key", amended 2026-11-19): Firefox
                                       copies the user.js pref into prefs.js and rewrites prefs.js itself, so
                                       deleting the user.js block alone left the pref behind after revert (VM
                                       check 2026-10-18). The key is the unit: apply writes
                                       `user_pref("<name>", true);`, revert removes that line again (or puts the
                                       user's own old value back); every other line is the browser's. State.data
                                       is the value text (b"true"); value_type "prefs-js". A missing prefs.js (a
                                       profile that never ran) is created with just that line, and deleted again
                                       on revert when nothing else was added.
Every byte outside the managed block is preserved. Re-applying replaces the block in place (duplicates are
collapsed into one at the first block's position); a first apply appends it at the end, after a newline if
the file does not end with one. Unbalanced markers (start without end, or the reverse) are treated as a
damaged file: PREFERENCES_UNREADABLE, nothing written. The chrome/ directory is created when missing and left
on revert (like M4.2's conf.d). Symlinked files are written through to their target, so the link survives.

Running browser: Firefox keeps a "lock" symlink in the profile while it runs and removes it on a clean exit
(.parentlock persists, so it is not used). Target.running = adapters.browser_lock.lock_is_live(<profile>/lock): a
lock left behind by a crash or a Flatpak quit is stale (Flatpak: the app is not in `flatpak ps`; native: a loopback
"<ip>:+<pid>" whose pid is gone) — added 2026-11-22; anything unclear still reads as running.
must_be_closed=False, requires_restart=True (decision changed 2026-11-22 by O4, user's go-ahead; was True since
2026-10-12): apply writes even while the browser runs and the controller reports RESTART_NEEDED, because Firefox
never rewrites user.js or userContent.css and reads both only at start — so nothing can race, and the user is never
asked to close the browser. The prefs.js key is written too: if the running browser drops it when it rewrites
prefs.js on exit, user.js sets the pref again at the next start (and revert then finds it as ours). revert() of
user.js and userContent.css never checks the lock (a stale lock must never block recovery); revert() of the
prefs.js key DOES refuse with BROWSER_RUNNING while the browser runs (as M4.7): Firefox would write the line back
from memory on exit; recovery retries once it is closed.

Verify (A14): configuration check only — both files contain our current block, the CSS block naming
fonts.reading_family. drawn_ok=None: the drawn check is the render harness (M3.4).

The only external command is `flatpak ps` (through run_cmd, for a Flatpak profile's lock). Logging carries codes and fixed identifiers only — never paths, profile names
or font names.
"""

from __future__ import annotations

import configparser
import hashlib
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scribesense.adapters.browser_css import browser_css
from scribesense.adapters.browser_lock import lock_is_live
from scribesense.adapters.fontconfig import delete_durable, write_durable
from scribesense.contracts.adapter import (
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
from scribesense.contracts.fonts import READING_FAMILY_PREFIX, FontSet
from scribesense.contracts.paths import home
from scribesense.log import log

ADAPTER_NAME = "firefox"
PROFILES_INI = "profiles.ini"
LOCK_NAME = "lock"  # symlink present only while the browser runs (".parentlock" persists: never used)
USER_JS = "user.js"
CHROME_DIR = "chrome"
USER_CONTENT = "userContent.css"
PREFS_JS = "prefs.js"
NEW_FILE_MODE = 0o644
NEW_PREFS_MODE = 0o600  # Firefox creates prefs.js private

STYLESHEET_PREF = "toolkit.legacyUserProfileCustomizations.stylesheets"
PREF_VALUE = b"true"
VALUE_TYPE = "prefs-js"
_KEY_SEP = "#"

USERJS_START = b"// >>> ScribeSense (managed) >>>"
USERJS_END = b"// <<< ScribeSense <<<"
USERJS_BODY = b'user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);\n'
CSS_START = b"/* >>> ScribeSense (managed) >>> */"
CSS_END = b"/* <<< ScribeSense <<< */"

_PROFILE_SECTION = re.compile(r"^Profile\d+$")
#: One prefs.js line setting STYLESHEET_PREF; group 1 = the value (bool, int or a JSON-style string).
_PREF_LINE = re.compile(
    rb'^\s*user_pref\(\s*"' + re.escape(STYLESHEET_PREF.encode()) + rb'"\s*,\s*'
    rb'(true|false|-?[0-9]+|"(?:[^"\\]|\\.)*")\s*\)\s*;\s*$')
#: Any line naming STYLESHEET_PREF at all (to refuse shapes _PREF_LINE cannot parse).
_PREF_NAMED = re.compile(rb'user_pref\(\s*"' + re.escape(STYLESHEET_PREF.encode()) + rb'"')
TARGET_HASH_LEN = 12


# ---------------------------------------------------------------- install roots and profiles

@dataclass(frozen=True)
class InstallRoot:
    label: str  # fixed identifier, part of target_id
    browser: str  # shown in the coverage report
    path: Path


@dataclass(frozen=True)
class Profile:
    root: InstallRoot
    target_id: str
    name: str  # profiles.ini Name= (display only, never logged)
    path: Path


def install_roots() -> list[InstallRoot]:
    """Every Firefox-family install root this card covers, in a fixed order (existing or not)."""
    h = home()
    return [
        InstallRoot("firefox", "Firefox", h / ".mozilla" / "firefox"),
        InstallRoot("firefox-xdg", "Firefox", h / ".config" / "mozilla" / "firefox"),
        InstallRoot("firefox-flatpak", "Firefox Flatpak",
                    h / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox"),
        InstallRoot("zen-flatpak", "Zen Flatpak", h / ".var" / "app" / "app.zen_browser.zen" / ".zen"),
        InstallRoot("zen", "Zen", h / ".zen"),
    ]


def profile_hash(directory: Path) -> str:
    """First TARGET_HASH_LEN hex digits of sha256 of the profile directory's absolute path (not realpath'd)."""
    return hashlib.sha256(os.fsencode(str(directory))).hexdigest()[:TARGET_HASH_LEN]


def target_id_for(root: InstallRoot, directory: Path) -> str:
    """"firefox:<root label>:<profile_hash>" — carries no part of the profile name."""
    return f"{ADAPTER_NAME}:{root.label}:{profile_hash(directory)}"


def parse_profiles_ini(data: bytes) -> list[tuple[str, str, bool | None]]:
    """(name, path, is_relative) per [ProfileN] section, in file order. is_relative None = key missing.

    Raises ValueError for a file that cannot be parsed (bad encoding or INI syntax)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("profiles.ini is not UTF-8") from None
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # keep key case
    try:
        parser.read_string(text)
    except configparser.Error:
        raise ValueError("profiles.ini is not valid INI") from None
    out = []
    for section in parser.sections():
        if not _PROFILE_SECTION.match(section):
            continue
        sec = parser[section]
        path = sec.get("Path", "").strip()
        if not path:
            continue
        rel = sec.get("IsRelative")
        is_relative = None if rel is None else rel.strip() == "1"
        out.append((sec.get("Name", "").strip(), path, is_relative))
    return out


def _profile_dir(root: Path, path: str, is_relative: bool | None) -> Path | None:
    """Absolute profile directory, or None for a path this adapter refuses ("..", NUL, empty)."""
    if "\0" in path:
        return None
    raw = Path(path)
    relative = (not raw.is_absolute()) if is_relative is None else is_relative
    if relative and raw.is_absolute():
        return None
    if not relative and not raw.is_absolute():
        return None
    full = root / raw if relative else raw
    if ".." in full.parts or full == full.parent:
        return None
    return full


def discover_profiles(roots: list[InstallRoot]) -> list[Profile]:
    """Every existing profile directory listed in an existing profiles.ini. Read-only, never raises for
    bad input. The same directory listed twice (or reachable from two roots) is reported once."""
    found: list[Profile] = []
    seen: set[str] = set()
    ids: set[str] = set()
    for root in roots:
        ini = root.path / PROFILES_INI
        try:
            data = ini.read_bytes()
        except FileNotFoundError:
            continue
        except OSError:
            log(ErrorCode.PROFILE_NOT_FOUND, "adapter", adapter=ADAPTER_NAME, target=root.label,
                reason="profiles-ini-unreadable")
            continue
        try:
            entries = parse_profiles_ini(data)
        except ValueError:
            log(ErrorCode.PROFILE_NOT_FOUND, "adapter", adapter=ADAPTER_NAME, target=root.label,
                reason="profiles-ini-malformed")
            continue
        for name, path, is_relative in entries:
            directory = _profile_dir(root.path, path, is_relative)
            if directory is None or not directory.is_dir():
                continue
            key = os.path.realpath(directory)
            if key in seen:
                continue
            seen.add(key)
            target_id = target_id_for(root, directory)
            if target_id in ids:  # 48-bit hash collision: never let two profiles share one id
                log(ErrorCode.PROFILE_NOT_FOUND, "adapter", adapter=ADAPTER_NAME, target=root.label,
                    reason="target-id-collision")
                continue
            ids.add(target_id)
            found.append(Profile(root, target_id, name or directory.name, directory))
    return found


# ---------------------------------------------------------------- locations

def user_js_path(profile: Path) -> Path:
    return profile / USER_JS


def user_content_path(profile: Path) -> Path:
    return profile / CHROME_DIR / USER_CONTENT


def prefs_js_path(profile: Path) -> Path:
    return profile / PREFS_JS


def pref_location(profile: Path) -> str:
    """K3 rule 10b location of the stylesheet pref: '<profile>/prefs.js#<pref name>'."""
    return f"{prefs_js_path(profile)}{_KEY_SEP}{STYLESHEET_PREF}"


def split_pref_location(location: str) -> tuple[Path, Path]:
    """(prefs.js path, profile dir) for '<abs profile>/prefs.js#<STYLESHEET_PREF>'; ValueError otherwise."""
    if location.count(_KEY_SEP) != 1:
        raise ValueError("not a Firefox prefs.js key location this adapter owns")
    path_text, key = location.split(_KEY_SEP)
    path = Path(path_text)
    if (key != STYLESHEET_PREF or not path.is_absolute() or ".." in path.parts or "\0" in location
            or path.name != PREFS_JS or len(path.parts) < 3):
        raise ValueError("not a Firefox prefs.js key location this adapter owns")
    return path, path.parent


def is_pref_location(location: str) -> bool:
    return _KEY_SEP in location


def pref_lines(data: bytes) -> list[int]:
    """Indexes of the lines setting STYLESHEET_PREF. Raises PREFERENCES_UNREADABLE for a line that names the
    pref in a shape this module cannot parse (never guess, never write over it)."""
    found = []
    for i, line in enumerate(data.splitlines(keepends=True)):
        if _PREF_LINE.match(line):
            found.append(i)
        elif _PREF_NAMED.search(line):
            raise ScribeSenseError(ErrorCode.PREFERENCES_UNREADABLE)
    return found


def pref_value(data: bytes) -> bytes | None:
    """The value Firefox would use (the last line wins), or None when the pref is not set."""
    lines = data.splitlines(keepends=True)
    found = pref_lines(data)
    if not found:
        return None
    match = _PREF_LINE.match(lines[found[-1]])
    if match is None:  # cannot happen: pref_lines returns matching lines only (explicit, not an assert)
        raise ScribeSenseError(ErrorCode.PREFERENCES_UNREADABLE)
    return match.group(1)


def set_pref(data: bytes, value: bytes | None) -> bytes:
    """data with every STYLESHEET_PREF line removed and, unless value is None, one
    `user_pref("<name>", <value>);` line where the first one was (or appended)."""
    lines = data.splitlines(keepends=True)
    found = set(pref_lines(data))
    new_line = b'user_pref("' + STYLESHEET_PREF.encode() + b'", ' + value + b");\n" if value is not None else None
    out: list[bytes] = []
    for i, line in enumerate(lines):
        if i in found:
            if new_line is not None and i == min(found):
                out.append(new_line)
            continue
        out.append(line)
    if new_line is not None and not found:
        if out and not out[-1].endswith(b"\n"):
            out[-1] += b"\n"
        out.append(new_line)
    return b"".join(out)


def _check_location(location: str) -> tuple[Path, Path]:
    """(file path, profile dir) for a location this adapter owns; ValueError otherwise.

    Shape check (absolute, no "..", .../user.js or .../chrome/userContent.css): recovery may hand back a
    location journaled under a different home, so equality with a discovered profile is not required."""
    path = Path(location)
    if not path.is_absolute() or ".." in path.parts or "\0" in location:
        raise ValueError("not a Firefox profile location this adapter owns")
    if path.name == USER_JS and len(path.parts) >= 3:
        return path, path.parent
    if path.parts[-2:] == (CHROME_DIR, USER_CONTENT) and len(path.parts) >= 4:
        return path, path.parent.parent
    raise ValueError("not a Firefox profile location this adapter owns")


def is_running(profile: Path) -> bool:
    return lock_is_live(profile / LOCK_NAME, "firefox")


# ---------------------------------------------------------------- managed blocks

def _is_marker(line: bytes, marker: bytes) -> bool:
    return line.strip() == marker


def find_blocks(data: bytes, start: bytes, end: bytes) -> list[tuple[int, int]]:
    """(first line, last line) index pairs of every complete managed block (lines kept with endings).

    Raises ScribeSenseError(PREFERENCES_UNREADABLE) for unbalanced or nested markers."""
    blocks: list[tuple[int, int]] = []
    open_at: int | None = None
    for i, line in enumerate(data.splitlines(keepends=True)):
        if _is_marker(line, start):
            if open_at is not None:
                raise ScribeSenseError(ErrorCode.PREFERENCES_UNREADABLE)
            open_at = i
        elif _is_marker(line, end):
            if open_at is None:
                raise ScribeSenseError(ErrorCode.PREFERENCES_UNREADABLE)
            blocks.append((open_at, i))
            open_at = None
    if open_at is not None:
        raise ScribeSenseError(ErrorCode.PREFERENCES_UNREADABLE)
    return blocks


def block_bodies(data: bytes, start: bytes, end: bytes) -> list[bytes]:
    """The content between the markers of every managed block."""
    lines = data.splitlines(keepends=True)
    return [b"".join(lines[s + 1:e]) for s, e in find_blocks(data, start, end)]


def render_block(start: bytes, body: bytes, end: bytes) -> bytes:
    if body and not body.endswith(b"\n"):
        body += b"\n"
    return start + b"\n" + body + end + b"\n"


def splice_block(data: bytes, block: bytes, start: bytes, end: bytes) -> bytes:
    """data with every managed block removed and `block` put where the first one was (or appended)."""
    blocks = find_blocks(data, start, end)
    if not blocks:
        prefix = data if not data or data.endswith(b"\n") else data + b"\n"
        return prefix + block
    lines = data.splitlines(keepends=True)
    inside = {i for s, e in blocks for i in range(s, e + 1)}
    first = blocks[0][0]
    out: list[bytes] = []
    for i, line in enumerate(lines):
        if i == first:
            out.append(block)
        if i not in inside:
            out.append(line)
    return b"".join(out)


def user_js_block() -> bytes:
    return render_block(USERJS_START, USERJS_BODY, USERJS_END)


def user_content_block(fonts: FontSet, config: Configuration) -> bytes:
    return render_block(CSS_START, browser_css(fonts, config).encode("utf-8"), CSS_END)


# ---------------------------------------------------------------- the adapter

RootsSource = Callable[[], list[InstallRoot]]


class FirefoxAdapter:
    """K3 adapter for Firefox-family browsers (Firefox, Zen; native and Flatpak), one target per profile."""

    name = ADAPTER_NAME
    requires_restart = True  # user.js and userContent.css are read at browser start only
    must_be_closed = False  # 2026-11-22: user.js / userContent.css are never rewritten by the browser (docstring)

    def __init__(self, *, roots: RootsSource = install_roots) -> None:
        self._roots = roots
        self._snapshot_sha: dict[str, str] = {}
        self._plan_base: dict[tuple[str, str], str] = {}  # (location, new sha) -> sha the payload was built on

    # ---- helpers

    def _profiles(self) -> list[Profile]:
        return discover_profiles(self._roots())

    def _profile(self, target: Target) -> Profile:
        if target.adapter != self.name:
            raise ValueError("target does not belong to the Firefox adapter")
        for profile in self._profiles():
            if profile.target_id == target.target_id:
                return profile
        raise ScribeSenseError(ErrorCode.PROFILE_NOT_FOUND)

    def _target(self, profile: Profile) -> Target:
        display = f"{profile.root.browser} ({profile.name})"
        return Target(self.name, profile.target_id, display, is_running(profile.path))

    @staticmethod
    def _locations(profile: Path) -> list[str]:
        return [str(user_js_path(profile)), str(user_content_path(profile))]

    @staticmethod
    def _mode(path: Path) -> int | None:
        try:
            return stat.S_IMODE(os.stat(path).st_mode)
        except FileNotFoundError:
            return None
        except OSError:
            raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR) from None

    @staticmethod
    def _write_target(path: Path) -> Path:
        """Where the bytes go: through a symlink to its target, so the link itself is preserved."""
        return Path(os.path.realpath(path)) if path.is_symlink() else path

    def _read_back(self, location: str, expected: str) -> None:
        if self.read_state(location).sha256 != expected:
            log(ErrorCode.UNEXPECTED_ADAPTER_ERROR, "adapter", adapter=self.name, step="read-back")
            raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR)

    # ---- K3

    def detect(self) -> list[Target]:
        return [self._target(p) for p in self._profiles()]

    def read_state(self, location: str) -> State:
        if is_pref_location(location):
            return self._read_pref(location)
        path, _ = _check_location(location)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return State("file", False, None, None, state_sha256(False, None))
        except OSError:
            raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR) from None
        return State("file", True, data, None, state_sha256(True, data))

    def _read_pref(self, location: str) -> State:
        path, _ = split_pref_location(location)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            data = b""
        except OSError:
            raise ScribeSenseError(ErrorCode.PREFERENCES_UNREADABLE) from None
        value = pref_value(data)
        return State("key", value is not None, value, VALUE_TYPE, state_sha256(value is not None, value))

    def snapshot(self, target: Target) -> list[SnapshotItem]:
        profile = self._profile(target)
        items = []
        pref = pref_location(profile.path)
        st = self.read_state(pref)
        self._snapshot_sha[pref] = st.sha256
        old = st.data.decode("utf-8") if st.data is not None else None
        pref_item = SnapshotItem(target, "key", pref, st.present, old, st.sha256, None, VALUE_TYPE)
        for location in self._locations(profile.path):
            st = self.read_state(location)
            mode = self._mode(Path(location)) if st.present else None
            if st.present and mode is None:  # vanished between the read and the stat
                raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR)
            self._snapshot_sha[location] = st.sha256
            items.append(SnapshotItem(target, "file", location, st.present, None, st.sha256, mode, None))
        return [*items, pref_item]

    def plan(self, target: Target, fonts: FontSet, config: Configuration) -> list[PlannedWrite]:
        profile = self._profile(target)
        if not fonts.reading_family.startswith(READING_FAMILY_PREFIX):
            raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR)  # never point at an arbitrary family
        blocks = [
            (USERJS_START, user_js_block(), USERJS_END),
            (CSS_START, user_content_block(fonts, config), CSS_END),
        ]
        planned = []
        for location, (start, block, end) in zip(self._locations(profile.path), blocks):
            st = self.read_state(location)
            payload = splice_block(st.data or b"", block, start, end)
            new_sha = state_sha256(True, payload)
            self._plan_base[(location, new_sha)] = st.sha256
            planned.append(PlannedWrite(target, location, payload, new_sha))
        self.read_state(pref_location(profile.path))  # an unparsable pref line -> refuse now, before any intent
        planned.append(PlannedWrite(target, pref_location(profile.path), PREF_VALUE.decode(),
                                    state_sha256(True, PREF_VALUE)))
        return planned

    def apply(self, planned: PlannedWrite) -> AppliedItem:
        if is_pref_location(planned.location):
            return self._apply_pref(planned)
        path, profile = _check_location(planned.location)
        expected = self._snapshot_sha.get(planned.location)
        if expected is None:
            raise ValueError("apply() for a location that snapshot() did not return (K3 rule 2)")
        if not isinstance(planned.payload, bytes):
            raise ValueError("Firefox payload must be file bytes")
        if not profile.is_dir():
            raise ScribeSenseError(ErrorCode.PROFILE_NOT_FOUND)  # never create a profile directory
        base = self._plan_base.get((planned.location, planned.new_sha256), expected)
        if self.read_state(planned.location).sha256 != expected or base != expected:
            raise ScribeSenseError(ErrorCode.CHANGED_DURING_APPLY)  # K3 rule 12
        destination = self._write_target(path)
        mode = self._mode(destination)
        write_durable(destination, planned.payload, NEW_FILE_MODE if mode is None else mode)
        self._read_back(planned.location, planned.new_sha256)
        return AppliedItem(planned.target, planned.location, planned.new_sha256)

    def _apply_pref(self, planned: PlannedWrite) -> AppliedItem:
        path, profile = split_pref_location(planned.location)
        expected = self._snapshot_sha.get(planned.location)
        if expected is None:
            raise ValueError("apply() for a location that snapshot() did not return (K3 rule 2)")
        value = planned.payload.encode() if isinstance(planned.payload, str) else planned.payload
        if value != PREF_VALUE:
            raise ValueError("the stylesheet pref is only ever set to true")
        if not profile.is_dir():
            raise ScribeSenseError(ErrorCode.PROFILE_NOT_FOUND)
        if self.read_state(planned.location).sha256 != expected:
            raise ScribeSenseError(ErrorCode.CHANGED_DURING_APPLY)  # K3 rule 12, this key only
        self._write_prefs(path, value)
        self._read_back(planned.location, planned.new_sha256)
        return AppliedItem(planned.target, planned.location, planned.new_sha256)

    def _write_prefs(self, path: Path, value: bytes | None) -> None:
        """Set (or, with None, remove) the pref in prefs.js; only those lines change. A prefs.js left empty by
        a removal is deleted (it was created by apply for a profile that never ran)."""
        destination = self._write_target(path)
        try:
            data = destination.read_bytes()
        except FileNotFoundError:
            data = None
        except OSError:
            raise ScribeSenseError(ErrorCode.PREFERENCES_UNREADABLE) from None
        new = set_pref(data or b"", value)
        if data is not None and new == data:
            return
        if not new:
            if data is not None:
                delete_durable(destination)
            return
        mode = self._mode(destination)
        write_durable(destination, new, NEW_PREFS_MODE if mode is None else mode)

    def verify(self, target: Target, fonts: FontSet) -> VerifyResult:
        """Configuration check (A14): user.js holds our block exactly; userContent.css holds exactly one
        block naming fonts.reading_family. drawn_ok=None (render harness, M3.4)."""
        try:
            profile = self._profile(target)
            user_js = self.read_state(str(user_js_path(profile.path)))
            css = self.read_state(str(user_content_path(profile.path)))
        except ScribeSenseError as exc:
            code = exc.code if exc.code is ErrorCode.PROFILE_NOT_FOUND else ErrorCode.CHECK_UNAVAILABLE
            return VerifyResult(target, False, None, code.value)
        try:
            js_bodies = block_bodies(user_js.data or b"", USERJS_START, USERJS_END)
            css_bodies = block_bodies(css.data or b"", CSS_START, CSS_END)
        except ScribeSenseError:
            return VerifyResult(target, False, None, ErrorCode.ADAPTER_VERIFY_FAILED.value)
        family = f'"{fonts.reading_family}"'.encode()
        ok = (
            fonts.reading_family.startswith(READING_FAMILY_PREFIX)
            and js_bodies == [USERJS_BODY]
            and len(css_bodies) == 1
            and b"font-family: " + family in css_bodies[0]
        )
        return VerifyResult(target, ok, None, "OK" if ok else ErrorCode.ADAPTER_VERIFY_FAILED.value)

    def revert(self, item: SnapshotItem, backup: bytes | None) -> None:
        """K3 rule 7: restore the ORIGINAL (existed -> backup bytes + mode, through a symlink; else delete).
        The chrome/ directory is left in place. The prefs.js key: the old value back, or the line removed;
        refused with BROWSER_RUNNING while the browser runs (it rewrites prefs.js)."""
        if item.kind == "key":
            path, profile = split_pref_location(item.location)
            if is_running(profile):
                raise ScribeSenseError(ErrorCode.BROWSER_RUNNING)
            if item.existed:
                if item.old_value is None:
                    raise ScribeSenseError(ErrorCode.BACKUP_MISSING)
                old = item.old_value.encode("utf-8")
                if state_sha256(True, old) != item.old_sha256 or not _PREF_LINE.match(
                        b'user_pref("' + STYLESHEET_PREF.encode() + b'", ' + old + b");"):
                    raise ScribeSenseError(ErrorCode.BACKUP_CORRUPT)
                self._write_prefs(path, old)
            else:
                self._write_prefs(path, None)
            self._read_back(item.location, item.old_sha256)
            return
        if item.kind != "file":
            raise ValueError("the Firefox adapter owns files only")
        path, _ = _check_location(item.location)
        if item.existed:
            if backup is None:
                raise ScribeSenseError(ErrorCode.BACKUP_MISSING)
            if state_sha256(True, backup) != item.old_sha256:
                raise ScribeSenseError(ErrorCode.BACKUP_CORRUPT)  # never write bytes that are not the original
            mode = item.file_mode if item.file_mode is not None else NEW_FILE_MODE
            write_durable(self._write_target(path), backup, mode)
        else:
            delete_durable(self._write_target(path))  # what apply created, through a (dangling) symlink
        self._read_back(item.location, item.old_sha256)
