"""O4 — Flatpak adapter (M4.4): let every installed Flatpak app see the user's fontconfig rule.

Decision (PLAN_v2 C2 #2): a PER-APP user override for EVERY installed app, found on whatever machine it
runs on (no hard-coded app list). The global override (<user dir>/overrides/global) is never read for
writing and never written: "global" is not a valid app id, so it can never be a location here.

Targets: one per installed app, Target("flatpak", "flatpak:<app-id>", "<app-id>", running).
    installed  `flatpak list --app --columns=application` (one id per line)
    running    `flatpak ps --columns=application`
    `flatpak` missing (run_cmd -> ADAPTER_UNAVAILABLE) -> no targets, not an error (card).
    Lines that are not valid app ids (headers, garbage) are skipped; the id rule is the one of
    adapters.verify.flatpak_app_id (reverse-DNS, no leading '-'), so an id can never become an option.

Location (kind "file", K3 rule 10): the per-app USER override keyfile <user dir>/overrides/<app-id>.
    <user dir> = $FLATPAK_USER_DIR if set, else $XDG_DATA_HOME/flatpak, else home()/.local/share/flatpak
    (flatpak's own lookup). Known K8 gap: K8 has no data_home(), so XDG_DATA_HOME is read here directly,
    with paths.home() as the fallback; relative values are ignored (XDG base-dir spec).

Writing: no `flatpak override` call. plan() produces the exact final bytes (K3 rules 2, 11) by editing
the GLib keyfile text in place: every byte outside the one `filesystems` value of group [Context] is
kept (comments, other groups, other keys, line endings). The entry "xdg-config/fontconfig:ro" is appended
to that semicolon-separated list (existing entries and their order kept, trailing ";"). GLib semantics
followed (checked against GLib.KeyFile): leading blanks of a line and blanks around "=" are ignored, a
repeated group is merged, a repeated key -> the LAST one counts (so the last `filesystems` line is
edited), "\\;" is a literal ';' inside an entry.
    No change is planned when the last entry for xdg-config/fontconfig already grants access (":ro",
    rw, ":rw" or ":create"): adding ":ro" after a rw grant would DOWNGRADE the user's own grant (flatpak:
    the last entry for a path wins). A "!xdg-config/fontconfig" entry is followed by ours (ours wins).
    Absent file -> "[Context]\\nfilesystems=xdg-config/fontconfig:ro;\\n".
    A file GLib could not load (not UTF-8, a line that is no group/key/comment, a key before the first
    group, a bad escape in the filesystems value) -> PREFERENCES_UNREADABLE; flatpak cannot use it either.
If the override file is a symlink, writes go through to its target (the symlink is kept). A dangling
symlink is refused (NOT_WRITABLE): creating its target would put a file outside the overrides dir that
revert could not remove. Directories created by apply are left in place on revert (as in M4.2).

Verify (A14): configuration check only — `flatpak run --command=fc-match <app-id>` for sans-serif must
name the reading family (adapters.verify.chosen_family). drawn_ok=None (Flatpak drawn check is
harness-only, A14). The check cannot run -> CHECK_UNAVAILABLE.

External commands only through the injectable runner (default contracts.paths.run_cmd, K3 rule 9).
Logging through scribesense.log: codes only, never paths or app ids.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scribesense.adapters.fontconfig import delete_durable, write_durable
from scribesense.adapters.verify import FLATPAK_PREFIX, chosen_family, flatpak_app_id
from scribesense.contracts import paths
from scribesense.contracts.adapter import (
    DEFAULT_TIMEOUTS,
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
from scribesense.contracts.paths import run_cmd
from scribesense.log import log

#: K3 rule 9 — card sets no own deadlines: list/ps use "other" (10 s), fc-match in the sandbox 20 s.
TIMEOUTS: dict[str, float] = dict(DEFAULT_TIMEOUTS)

ADAPTER_NAME = "flatpak"
LIST_ARGV = ["flatpak", "list", "--app", "--columns=application"]
PS_ARGV = ["flatpak", "ps", "--columns=application"]
VERIFY_QUERY = "sans-serif"

FONTCONFIG_PATH = "xdg-config/fontconfig"
ENTRY = FONTCONFIG_PATH + ":ro"
GROUP = "Context"
KEY = "filesystems"
#: Entries for FONTCONFIG_PATH that already let the app read it (flatpak modes: ro, rw (default), create).
_READ_GRANTS = frozenset({FONTCONFIG_PATH, ENTRY, FONTCONFIG_PATH + ":rw", FONTCONFIG_PATH + ":create"})
_NEW_FILE = f"[{GROUP}]\n{KEY}={ENTRY};\n".encode()
_NEW_FILE_MODE = 0o644  # what flatpak's own g_file_set_contents gives under the usual umask
_BLANKS = " \t\n\v\f\r"  # g_ascii_isspace
_LINES = re.compile(r"[^\n]*\n|[^\n]+")  # GLib lines: split only after "\n"
_ESCAPES = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\", ";": ";"}

#: Same signature as run_cmd; the result needs only a bytes `.stdout`.
Runner = Callable[[list[str], float], Any]


# ---------------------------------------------------------------- paths

def user_dir() -> Path:
    """flatpak's per-user installation dir: $FLATPAK_USER_DIR, else <XDG data home>/flatpak.

    K8 has no data_home() (known gap): XDG_DATA_HOME is read here, falling back to paths.home().
    """
    override = os.environ.get("FLATPAK_USER_DIR")
    if override and os.path.isabs(override):
        return Path(override)
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home and os.path.isabs(data_home):
        return Path(data_home) / "flatpak"
    return paths.home() / ".local" / "share" / "flatpak"


def override_path(app_id: str) -> Path:
    """The per-app user override keyfile (computed at call time; the environment may change)."""
    return user_dir() / "overrides" / app_id


def valid_app_id(app_id: object) -> bool:
    """The rule of adapters.verify.flatpak_app_id (reverse-DNS, <= 255 chars, no leading '-')."""
    if not isinstance(app_id, str):
        return False
    try:
        flatpak_app_id(Target(ADAPTER_NAME, FLATPAK_PREFIX + app_id, app_id, False))
    except ScribeSenseError:
        return False
    return True


def _check_location(location: str) -> Path:
    """Refuse anything that is not an absolute .../overrides/<valid app id> path (never "global").

    Recovery may hand back a location journaled under a different user dir, so the check is on the
    path's shape, not equality with override_path().
    """
    path = Path(location)
    if (not path.is_absolute() or ".." in path.parts or len(path.parts) < 3
            or path.parts[-2] != "overrides" or not valid_app_id(path.name)):
        raise ValueError("not a Flatpak per-app override location this adapter owns")
    return path


def _write_path(path: Path) -> Path:
    """Where bytes for `path` go: through a symlink to its target, so the symlink is preserved."""
    return Path(os.path.realpath(path)) if path.is_symlink() else path


# ---------------------------------------------------------------- GLib keyfile editing

def _unreadable() -> ScribeSenseError:
    return ScribeSenseError(ErrorCode.PREFERENCES_UNREADABLE)


def parse_list(value: str) -> list[str]:
    """A GLib string list ("a;b\\;c;" -> ["a", "b;c"]). Bad escape / trailing backslash -> PREFERENCES_UNREADABLE."""
    items: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\":
            if i + 1 >= len(value) or value[i + 1] not in _ESCAPES:
                raise _unreadable()
            current.append(_ESCAPES[value[i + 1]])
            i += 2
            continue
        if ch == ";":
            items.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    if current:
        items.append("".join(current))
    return items


def _has_read_grant(entries: list[str]) -> bool:
    """Does the LAST entry for xdg-config/fontconfig (flatpak: last wins) let the app read it?"""
    last = None
    for entry in entries:
        if entry.lstrip("!") == FONTCONFIG_PATH or entry.startswith(FONTCONFIG_PATH + ":"):
            last = entry
    return last in _READ_GRANTS


def _split_line(line: str) -> tuple[str, str]:
    """(content, line ending) — GLib drops one '\\r' before '\\n'."""
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    return line, ""


def _group_name(body: str) -> str | None:
    """Group name if `body` (leading blanks stripped) is a GLib group line, else None."""
    if not body.startswith("["):
        return None
    end = body.find("]")
    if end < 0 or body[end + 1:].strip(" \t"):
        return None
    return body[1:end]


def merge_override(data: bytes | None) -> bytes:
    """The override file with ENTRY in [Context] filesystems; every other byte kept. None = absent file."""
    if data is None:
        return _NEW_FILE
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _unreadable() from None
    if "\0" in text:
        raise _unreadable()
    # GLib ends a line only at "\n" (one "\r" before it is dropped, see _split_line); str.splitlines()
    # would also split at a lone "\r", "\x0c", U+2028 etc., which GLib keeps inside the line.
    lines = _LINES.findall(text)
    # pass 1: validate like GLib and find the last [Context] section and the last filesystems line
    group: str | None = None
    last_context_header = -1
    last_context_key = -1  # last key line inside the last [Context] section
    fs_line = -1
    for idx, line in enumerate(lines):
        content, _end = _split_line(line)
        body = content.lstrip(_BLANKS)
        if not body or body.startswith("#"):
            continue
        name = _group_name(body)
        if name is not None:
            group = name
            if name == GROUP:
                last_context_header, last_context_key = idx, -1
            continue
        eq = body.find("=")
        if eq <= 0 or group is None:  # not key=value / empty key / key before the first group
            raise _unreadable()
        if group == GROUP:
            last_context_key = idx
            if body[:eq].rstrip(_BLANKS) == KEY:
                fs_line = idx

    if fs_line >= 0:
        content, end = _split_line(lines[fs_line])
        eq = content.index("=")
        value_start = eq + 1
        while value_start < len(content) and content[value_start] in _BLANKS:
            value_start += 1
        value = content[value_start:]
        if _has_read_grant(parse_list(value)):
            return data
        core = value.rstrip(_BLANKS)  # trailing blanks would glue onto our entry
        trailing = value[len(core):]
        backslashes = len(core[:-1]) - len(core[:-1].rstrip("\\")) if core.endswith(";") else 0
        sep = "" if not core or (core.endswith(";") and backslashes % 2 == 0) else ";"
        lines[fs_line] = content[:value_start] + core + sep + ENTRY + ";" + trailing + end
        return "".join(lines).encode("utf-8")

    new_key = f"{KEY}={ENTRY};"
    if last_context_header >= 0:
        at = last_context_key if last_context_key >= 0 else last_context_header
        content, end = _split_line(lines[at])
        newline = end or "\n"
        lines[at] = content + newline
        lines.insert(at + 1, new_key + newline)
        return "".join(lines).encode("utf-8")

    # no [Context] group: append one, after a blank separator line
    out = text
    if out and not out.endswith("\n"):
        out += "\n"
    if out and not out.endswith("\n\n"):
        out += "\n"
    return (out + f"[{GROUP}]\n{new_key}\n").encode("utf-8")


# ---------------------------------------------------------------- the adapter

class FlatpakAdapter:
    """K3 adapter: one target per installed Flatpak app, one per-app user override file each."""

    name = ADAPTER_NAME
    requires_restart = True  # a running app keeps its sandbox until it is restarted
    must_be_closed = False

    def __init__(self, *, runner: Runner = run_cmd) -> None:
        self._run = runner
        self._snapshot_sha: dict[str, str] = {}

    # ---- helpers

    def _app_id(self, target: Target) -> str:
        if target.adapter != self.name:
            raise ValueError("target does not belong to the flatpak adapter")
        try:
            return flatpak_app_id(target)
        except ScribeSenseError:
            raise ValueError("not a valid Flatpak target id") from None

    def _app_ids(self, argv: list[str]) -> list[str]:
        """Valid, distinct app ids printed one per line by `argv`, in first-seen order."""
        try:
            proc = self._run(list(argv), TIMEOUTS["other"])
        except OSError:  # launch refused etc. (run_cmd lets it through); K3 rule 5 needs a code
            raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR) from None
        out = getattr(proc, "stdout", None)
        text = (out if isinstance(out, bytes) else b"").decode("utf-8", errors="replace")
        ids = [line.strip() for line in text.splitlines()]
        return list(dict.fromkeys(i for i in ids if valid_app_id(i)))

    def _running(self) -> set[str]:
        """Running app ids; unknown (any failure) -> none running (only drives the restart notice)."""
        try:
            return set(self._app_ids(PS_ARGV))
        except ScribeSenseError as exc:
            log(exc.code, "adapter", adapter=self.name, step="detect-running")
            return set()

    # ---- K3

    def detect(self) -> list[Target]:
        try:
            installed = self._app_ids(LIST_ARGV)
        except ScribeSenseError as exc:
            if exc.code is ErrorCode.ADAPTER_UNAVAILABLE:
                return []  # flatpak not installed: no targets, not an error (card)
            raise
        if not installed:
            return []
        running = self._running()
        return [Target(self.name, FLATPAK_PREFIX + app, app, app in running) for app in installed]

    def read_state(self, location: str) -> State:
        path = _check_location(location)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return State("file", False, None, None, state_sha256(False, None))
        except OSError:
            raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR) from None
        return State("file", True, data, None, state_sha256(True, data))

    def snapshot(self, target: Target) -> list[SnapshotItem]:
        location = str(override_path(self._app_id(target)))
        st = self.read_state(location)
        mode = None
        if st.present:
            try:
                mode = stat.S_IMODE(os.stat(location).st_mode)
            except OSError:
                raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR) from None
        self._snapshot_sha[location] = st.sha256
        return [SnapshotItem(target, "file", location, st.present, None, st.sha256, mode, None)]

    def plan(self, target: Target, fonts: FontSet, config: Configuration) -> list[PlannedWrite]:
        location = str(override_path(self._app_id(target)))
        # The override only exposes the fontconfig dir; still refuse a non-K7 family (as M4.2 does).
        if not (isinstance(fonts.reading_family, str) and fonts.reading_family.startswith(READING_FAMILY_PREFIX)
                and len(fonts.reading_family) > len(READING_FAMILY_PREFIX)):
            raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR)
        st = self.read_state(location)
        payload = merge_override(st.data if st.present else None)
        return [PlannedWrite(target, location, payload, state_sha256(True, payload))]

    def apply(self, planned: PlannedWrite) -> AppliedItem:
        expected = self._snapshot_sha.get(planned.location)
        if expected is None:
            raise ValueError("apply() for a location that snapshot() did not return (K3 rule 2)")
        if not isinstance(planned.payload, bytes):
            raise ValueError("flatpak payload must be file bytes")
        path = _check_location(planned.location)
        current = self.read_state(planned.location)
        if current.sha256 != expected:
            raise ScribeSenseError(ErrorCode.CHANGED_DURING_APPLY)  # K3 rule 12
        if not current.present and path.is_symlink():
            raise ScribeSenseError(ErrorCode.NOT_WRITABLE)  # dangling symlink: see module docstring
        if current.present and current.data == planned.payload:
            return AppliedItem(planned.target, planned.location, planned.new_sha256)  # nothing to change
        target_path = _write_path(path)
        try:
            mode = stat.S_IMODE(os.stat(target_path).st_mode)
        except FileNotFoundError:
            mode = _NEW_FILE_MODE
        except OSError:
            raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR) from None
        write_durable(target_path, planned.payload, mode)
        return AppliedItem(planned.target, planned.location, planned.new_sha256)

    def verify(self, target: Target, fonts: FontSet) -> VerifyResult:
        """Configuration check (A14): fc-match inside the app's sandbox names the reading family."""
        self._app_id(target)
        try:
            chosen = chosen_family(target, VERIFY_QUERY, runner=self._run, timeout=TIMEOUTS["flatpak-fc-match"])
        except (ScribeSenseError, OSError):
            return VerifyResult(target, False, None, ErrorCode.CHECK_UNAVAILABLE.value)
        if chosen == fonts.reading_family:
            return VerifyResult(target, True, None, "OK")
        return VerifyResult(target, False, None, ErrorCode.ADAPTER_VERIFY_FAILED.value)

    def revert(self, item: SnapshotItem, backup: bytes | None) -> None:
        """K3 rule 7: restore the ORIGINAL state (existed -> backup bytes + mode; else delete)."""
        if item.kind != "file":
            raise ValueError("flatpak owns files only")
        path = _check_location(item.location)
        if item.existed:
            if backup is None:
                raise ScribeSenseError(ErrorCode.BACKUP_MISSING)
            mode = item.file_mode if item.file_mode is not None else _NEW_FILE_MODE
            write_durable(_write_path(path), backup, mode)  # b"" is a valid empty file
        else:
            delete_durable(path)
