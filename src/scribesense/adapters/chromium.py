"""O4 — Chromium preferences adapter (M4.7): default fonts in Chromium-family browsers, every profile.

Targets: one per profile, Target("chromium", "<browser>:<profile id>", "<Browser> (<profile dir>)", running).
User-data dirs (only those that exist):
    brave            config_home()/BraveSoftware/Brave-Browser
    chrome           config_home()/google-chrome
    chromium         config_home()/chromium
    brave-flatpak    home()/.var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser
    chromium-flatpak home()/.var/app/org.chromium.Chromium/config/chromium
Profiles: subdirectories "Default" and "Profile *" that contain a "Preferences" file. A profile dir whose
name cannot become an identifier-safe id ("Profile 1" -> "Profile_1"; anything outside [A-Za-z0-9 _.-] is
refused) or whose id would collide with another profile's is skipped: Chrome itself only creates
"Default" and "Profile <n>".

Location (kind "file", K3 rule 10): <user-data dir>/<profile>/Preferences. The only change, made with the
browser CLOSED (Step 3 finding):
    webkit.webprefs.fonts.standard.Zyyy  = fonts.reading_family
    webkit.webprefs.fonts.sansserif.Zyyy = fonts.reading_family
Missing nested dicts are created; every other key and value survives unchanged (parsed comparison).
A Preferences file is NEVER created (a minimal invented one hung Brave, Step 3): missing -> no target /
PROFILE_NOT_FOUND, and revert of a "did not exist" item deletes nothing (ScribeSense created no file).

Serialization follows Chrome's own JSONWriter so an untouched file round-trips byte-for-byte:
compact separators, keys in parsed order, non-ASCII kept as UTF-8, '<' / U+2028 / U+2029 / control
characters escaped as \\uXXXX (uppercase hex), no trailing newline. A key we have to add is inserted at
its sorted (UTF-8 byte order) position when the dict it goes into is sorted — as Chrome writes them —
otherwise appended.

Running: the user-data dir has a "SingletonLock" (a symlink, possibly dangling: os.path.lexists).
must_be_closed=True; apply() and revert() re-check and refuse with BROWSER_RUNNING, writing nothing
(card: "must never edit while running" — revert is an edit too). A stale lock after a crash therefore
reads as running.

Verify (A14): configuration check only — the file parses and both keys name the reading family.
drawn_ok=None: the drawn check (headless browser) belongs to the harness.

No external commands are run by this adapter.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scribesense.adapters.fontconfig import write_durable
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
from scribesense.contracts.paths import config_home, home

ADAPTER_NAME = "chromium"
PREFERENCES = "Preferences"
SINGLETON_LOCK = "SingletonLock"
DEFAULT_PROFILE = "Default"
PROFILE_PREFIX = "Profile "
#: Chrome pref paths we write (dotted pref names are nested dicts in the file).
FONT_KEYS: tuple[tuple[str, ...], ...] = (
    ("webkit", "webprefs", "fonts", "standard", "Zyyy"),
    ("webkit", "webprefs", "fonts", "sansserif", "Zyyy"),
)

_PROFILE_NAME = re.compile(r"^(?:Default|Profile [A-Za-z0-9 _.-]+)$")
_BAD_CHAR = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff\ufffe\uffff]")
#: Characters Chrome's JSONWriter escapes inside strings (EscapeJSONString).
_ESCAPE = re.compile('["\\\\\x00-\x1f<\u2028\u2029\ud800-\udfff]')
_SHORT_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


@dataclass(frozen=True)
class Browser:
    key: str  # target-id prefix, identifier-safe
    display: str  # shown in the coverage report
    flatpak_app: str | None  # None = native install
    suffix: tuple[str, ...]  # user-data dir relative to config_home() (native) / the app's config dir


BROWSERS: tuple[Browser, ...] = (
    Browser("brave", "Brave", None, ("BraveSoftware", "Brave-Browser")),
    Browser("chrome", "Google Chrome", None, ("google-chrome",)),
    Browser("chromium", "Chromium", None, ("chromium",)),
    Browser("brave-flatpak", "Brave Flatpak", "com.brave.Browser", ("BraveSoftware", "Brave-Browser")),
    Browser("chromium-flatpak", "Chromium Flatpak", "org.chromium.Chromium", ("chromium",)),
)
_BY_KEY = {b.key: b for b in BROWSERS}


def user_data_dir(browser: Browser) -> Path:
    """Computed at call time from K8 (the environment may change)."""
    if browser.flatpak_app is None:
        return config_home().joinpath(*browser.suffix)
    return home().joinpath(".var", "app", browser.flatpak_app, "config", *browser.suffix)


def profile_id(dir_name: str) -> str | None:
    """Identifier-safe id for a profile dir ("Profile 1" -> "Profile_1"); None = not a profile dir we handle."""
    if not _PROFILE_NAME.match(dir_name):
        return None
    return dir_name.replace(" ", "_")


def is_running(data_dir: Path) -> bool:
    """Chromium holds a SingletonLock symlink (often 'host-pid', dangling by design) while running."""
    return os.path.lexists(data_dir / SINGLETON_LOCK)


def _check_location(location: str) -> Path:
    """Refuse anything that is not an absolute <known user-data dir>/<profile>/Preferences path.

    Checked on shape (recovery may hand back a location journaled under a different HOME), like M4.2.
    """
    path = Path(location)
    parts = path.parts
    if not path.is_absolute() or ".." in parts or len(parts) < 4 or parts[-1] != PREFERENCES:
        raise ValueError("not a Chromium Preferences location this adapter owns")
    if profile_id(parts[-2]) is None:
        raise ValueError("not a Chromium Preferences location this adapter owns")
    if not any(parts[-2 - len(b.suffix):-2] == b.suffix for b in BROWSERS):
        raise ValueError("not a Chromium Preferences location this adapter owns")
    return path


# ---------------------------------------------------------------- JSON (Chrome's own style)

def _reject_constant(_name: str) -> Any:
    raise ValueError("NaN / Infinity are not JSON")


def parse_preferences(data: bytes) -> dict[str, Any]:
    """Parse a Preferences file; anything Chrome would not have written -> PREFERENCES_UNREADABLE."""
    try:
        text = data.decode("utf-8")  # strict: invalid UTF-8 is refused; json refuses a BOM
        value = json.loads(text, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ScribeSenseError(ErrorCode.PREFERENCES_UNREADABLE) from None
    if not isinstance(value, dict):
        raise ScribeSenseError(ErrorCode.PREFERENCES_UNREADABLE)
    return value


def _escape_char(match: re.Match[str]) -> str:
    ch = match.group(0)
    short = _SHORT_ESCAPES.get(ch)
    return short if short is not None else "\\u%04X" % ord(ch)


def _encode(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append('"' + _ESCAPE.sub(_escape_char, value) + '"')
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, float):
        out.append(repr(value))  # shortest round-trip, "1.0" / "1.3e+16" — what Chrome writes
    elif isinstance(value, dict):
        out.append("{")
        for i, (k, v) in enumerate(value.items()):
            if i:
                out.append(",")
            _encode(k, out)
            out.append(":")
            _encode(v, out)
        out.append("}")
    elif isinstance(value, list):
        out.append("[")
        for i, v in enumerate(value):
            if i:
                out.append(",")
            _encode(v, out)
        out.append("]")
    else:  # parse_preferences never produces anything else
        raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR)


def serialize_preferences(value: dict[str, Any]) -> bytes:
    """Deterministic, Chrome-style bytes (lone surrogates stay escaped, so encoding cannot fail)."""
    out: list[str] = []
    try:
        _encode(value, out)
    except RecursionError:
        raise ScribeSenseError(ErrorCode.PREFERENCES_UNREADABLE) from None
    return "".join(out).encode("utf-8")


def _sort_key(key: str) -> bytes:
    return key.encode("utf-8", "surrogatepass")


def _with_key(d: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    """Copy of d with key set; an existing key keeps its position, a new one goes in sorted order if d is sorted."""
    if key in d:
        new = dict(d)
        new[key] = value
        return new
    keys = list(d)
    if keys == sorted(keys, key=_sort_key):
        items = list(d.items()) + [(key, value)]
        items.sort(key=lambda kv: _sort_key(kv[0]))
        return dict(items)
    new = dict(d)
    new[key] = value
    return new


def _set_path(d: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
    if len(path) == 1:
        return _with_key(d, path[0], value)
    child = d.get(path[0], {})
    if not isinstance(child, dict):
        # e.g. "webkit": "x" — setting our key would destroy the user's value
        raise ScribeSenseError(ErrorCode.PREFERENCES_UNREADABLE)
    return _with_key(d, path[0], _set_path(child, path[1:], value))


def _get_path(d: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(d, dict) or key not in d:
            return None
        d = d[key]
    return d


def with_reading_family(prefs: dict[str, Any], family: str) -> dict[str, Any]:
    """Preferences with ONLY the two font keys set; the input is not modified."""
    for path in FONT_KEYS:
        prefs = _set_path(prefs, path, family)
    return prefs


def _check_fonts(fonts: FontSet) -> None:
    """Never point a browser at an arbitrary family (K7 names only)."""
    family = fonts.reading_family
    if (not isinstance(family, str) or not family.startswith(READING_FAMILY_PREFIX)
            or len(family) <= len(READING_FAMILY_PREFIX) or _BAD_CHAR.search(family)):
        raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR)


# ---------------------------------------------------------------- the adapter

class ChromiumAdapter:
    """K3 adapter for Chromium-family browser profiles (Brave, Chrome, Chromium; native and Flatpak)."""

    name = ADAPTER_NAME
    requires_restart = True
    must_be_closed = True

    def __init__(self) -> None:
        self._snapshot_sha: dict[str, str] = {}

    # ---- helpers

    def _profiles(self, browser: Browser) -> list[tuple[str, str]]:
        """(profile id, profile dir name) for each profile with a Preferences file, sorted, ids unique."""
        data_dir = user_data_dir(browser)
        try:
            names = sorted(os.listdir(data_dir))
        except OSError:
            return []
        found: dict[str, str] = {}
        collided: set[str] = set()
        for name in names:
            pid = profile_id(name)
            if pid is None:
                continue
            try:
                if not (data_dir / name / PREFERENCES).is_file():
                    continue
            except OSError:
                continue
            if pid in found:
                collided.add(pid)
            found[pid] = name
        return [(pid, name) for pid, name in found.items() if pid not in collided]

    def _resolve(self, target: Target) -> Path:
        """Preferences path of a target, re-resolved from disk (read-only)."""
        if target.adapter != self.name or ":" not in target.target_id:
            raise ValueError("target does not belong to the Chromium adapter")
        key, pid = target.target_id.split(":", 1)
        browser = _BY_KEY.get(key)
        if browser is None:
            raise ValueError("target does not belong to the Chromium adapter")
        for found_id, name in self._profiles(browser):
            if found_id == pid:
                return user_data_dir(browser) / name / PREFERENCES
        raise ScribeSenseError(ErrorCode.PROFILE_NOT_FOUND)

    @staticmethod
    def _refuse_if_running(path: Path) -> None:
        if is_running(path.parent.parent):
            raise ScribeSenseError(ErrorCode.BROWSER_RUNNING)

    # ---- K3

    def detect(self) -> list[Target]:
        targets = []
        for browser in BROWSERS:
            running = is_running(user_data_dir(browser))
            for pid, name in self._profiles(browser):
                targets.append(Target(self.name, f"{browser.key}:{pid}", f"{browser.display} ({name})", running))
        return targets

    def read_state(self, location: str) -> State:
        path = _check_location(location)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return State("file", False, None, None, state_sha256(False, None))
        except OSError:
            raise ScribeSenseError(ErrorCode.PREFERENCES_UNREADABLE) from None
        return State("file", True, data, None, state_sha256(True, data))

    def snapshot(self, target: Target) -> list[SnapshotItem]:
        location = str(self._resolve(target))
        st = self.read_state(location)
        if not st.present:
            raise ScribeSenseError(ErrorCode.PROFILE_NOT_FOUND)  # never journal a file we may not create
        try:
            mode = stat.S_IMODE(os.stat(location).st_mode)
        except OSError:
            raise ScribeSenseError(ErrorCode.PREFERENCES_UNREADABLE) from None
        self._snapshot_sha[location] = st.sha256
        return [SnapshotItem(target, "file", location, True, None, st.sha256, mode, None)]

    def plan(self, target: Target, fonts: FontSet, config: Configuration) -> list[PlannedWrite]:
        _check_fonts(fonts)
        location = str(self._resolve(target))
        st = self.read_state(location)
        if not st.present or st.data is None:
            raise ScribeSenseError(ErrorCode.PROFILE_NOT_FOUND)
        payload = serialize_preferences(with_reading_family(parse_preferences(st.data), fonts.reading_family))
        return [PlannedWrite(target, location, payload, state_sha256(True, payload))]

    def apply(self, planned: PlannedWrite) -> AppliedItem:
        expected = self._snapshot_sha.get(planned.location)
        if expected is None:
            raise ValueError("apply() for a location that snapshot() did not return (K3 rule 2)")
        if not isinstance(planned.payload, bytes):
            raise ValueError("Chromium payload must be file bytes")
        path = _check_location(planned.location)
        parse_preferences(planned.payload)  # never write anything a browser could choke on
        self._refuse_if_running(path)
        current = self.read_state(planned.location)
        if current.sha256 != expected:
            raise ScribeSenseError(ErrorCode.CHANGED_DURING_APPLY)  # K3 rule 12
        if not current.present:
            raise ScribeSenseError(ErrorCode.PROFILE_NOT_FOUND)  # never create Preferences
        real = Path(os.path.realpath(path))  # a symlinked Preferences stays a symlink
        try:
            mode = stat.S_IMODE(os.stat(real).st_mode)
        except OSError:
            raise ScribeSenseError(ErrorCode.PREFERENCES_UNREADABLE) from None
        write_durable(real, planned.payload, mode)
        return AppliedItem(planned.target, planned.location, planned.new_sha256)

    def verify(self, target: Target, fonts: FontSet) -> VerifyResult:
        """Configuration check (A14): both font keys name the reading family. drawn_ok=None (harness)."""
        try:
            st = self.read_state(str(self._resolve(target)))
            if not st.present or st.data is None:
                raise ScribeSenseError(ErrorCode.PROFILE_NOT_FOUND)
            prefs = parse_preferences(st.data)
        except (ScribeSenseError, OSError):
            return VerifyResult(target, False, None, ErrorCode.CHECK_UNAVAILABLE.value)
        ok = all(_get_path(prefs, p) == fonts.reading_family for p in FONT_KEYS)
        return VerifyResult(target, ok, None, "OK" if ok else ErrorCode.ADAPTER_VERIFY_FAILED.value)

    def revert(self, item: SnapshotItem, backup: bytes | None) -> None:
        """K3 rule 7: existed -> write the backup bytes + mode back (through a symlink).

        "Did not exist" -> nothing: this adapter never creates a Preferences file, so there is nothing
        ScribeSense created to delete, and deleting a browser's own Preferences would lose the profile's
        settings.
        """
        if item.kind != "file":
            raise ValueError("the Chromium adapter owns files only")
        path = _check_location(item.location)
        if not item.existed:
            return
        if backup is None:
            raise ScribeSenseError(ErrorCode.BACKUP_MISSING)
        self._refuse_if_running(path)
        mode = item.file_mode if item.file_mode is not None else 0o600
        write_durable(Path(os.path.realpath(path)), backup, mode)
