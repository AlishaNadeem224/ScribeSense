"""O4 — Qt adapter (M4.5): Qt application fonts through qt6ct, only when the session already uses it.

Precondition (card): the session already sets QT_QPA_PLATFORMTHEME=qt6ct. The environment is read at call
time (os.environ) and NEVER changed (card: must never; §4 Session). When the session does not use qt6ct,
snapshot() and plan() raise QT6CT_NOT_IN_SESSION, which the controller maps to NOT_COVERED with the reason
shown. detect() always returns the one target so it appears in the coverage report.

Target: one, "qt:qt6ct". Location (K3 rule 10, kind "file"): config_home()/qt6ct/qt6ct.conf.
The file or its directory may be a symlink into a dotfiles folder: reads follow it, writes go to
os.path.realpath(location) so the symlink itself is preserved.

What is written (A13 — qt6ct's general font is an identified UI setting -> UI variant):
    [Fonts] general  -> fonts.ui_family, keeping the size of the current general font (else 11)
    [Fonts] fixed    is NEVER touched (monospace), nor is any other key, group, comment or byte.
The [Fonts] group is added when missing (common: qt6ct writes it only after the user picks a font).

Editing is line based (QSettings files do not round-trip through configparser): every line other than
our own `general=` line keeps its exact bytes, line endings and order. Duplicate `general` keys inside
[Fonts] groups are all rewritten, so whichever one QSettings honours names our family.

Confirmed on the dev machine (qt6ct 0.11, Qt 6.11, by inspecting the installed binaries' symbols/strings):
group "Fonts", keys "general" / "fixed", values read with QVariant::toString() + QFont::fromString().
NOT confirmed against a running qt6ct: the exact field layout we write — see qfont_string() [V].

Verify (A14): configuration check only — the file read back names our family as [Fonts] general.
drawn_ok=None: a drawn check for Qt is harness-only (A14).
No external command is run, so this adapter has no runner.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

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
from scribesense.contracts.fonts import UI_FAMILY_PREFIX, FontSet
from scribesense.contracts.paths import config_home

TARGET_ID = "qt:qt6ct"
SESSION_VAR = "QT_QPA_PLATFORMTHEME"
SESSION_VALUE = "qt6ct"
CONF_DIR = "qt6ct"
CONF_NAME = "qt6ct.conf"
FONTS_GROUP = b"Fonts"
GENERAL_KEY = b"general"
NEVER_TOUCHED = (b"fixed",)  # documentation + tests: the monospace font stays the user's
DEFAULT_SIZE = 11.0
NEW_FILE_MODE = 0o644

#: Our families are "AccessSansUI-<hex key>" (A19). Anything else — and anything QSettings would need to
#: escape (quotes, backslashes, commas, controls) — is refused, so the written value is always plain.
_FAMILY_OK = re.compile(r"^[A-Za-z0-9._-]+$")
_LINE = re.compile(rb"[^\r\n]*(?:\r\n|\r|\n)?")
_BOM = b"\xef\xbb\xbf"


# ---------------------------------------------------------------- paths

def conf_path() -> Path:
    """The one location this adapter owns. Computed at call time (the environment may change)."""
    return config_home() / CONF_DIR / CONF_NAME


def _check_location(location: str) -> Path:
    """Refuse anything that is not an absolute .../qt6ct/qt6ct.conf path.

    Shape check, not equality with conf_path(): recovery may hand back a location journaled under a
    different config_home. It still forbids arbitrary files.
    """
    path = Path(location)
    if not path.is_absolute() or path.parts[-2:] != (CONF_DIR, CONF_NAME) or ".." in path.parts:
        raise ValueError("not a qt6ct.conf location this adapter owns")
    return path


def _real(path: Path) -> Path:
    """Write-through target: a symlinked file or directory (dotfiles) stays a symlink."""
    return Path(os.path.realpath(path))


def session_uses_qt6ct() -> bool:
    return os.environ.get(SESSION_VAR) == SESSION_VALUE


# ---------------------------------------------------------------- QFont strings  [V]

def qfont_string(family: str, size: float) -> str:
    """The value we write for [Fonts] general: QFont::toString() in the Qt 6 layout.  [V]

    [V] Field layout NOT confirmed against a running qt6ct — this is the ONE place to correct it:
        family, pointSizeF, pixelSize(-1), styleHint(5 = AnyStyle), weight(400 = Normal, Qt 6 scale),
        style(0), underline(0), strikeOut(0), fixedPitch(0), 0 (former rawMode), capitalization(0),
        letterSpacingType(0), letterSpacing(0), wordSpacing(0), stretch(0 = AnyStretch),
        styleStrategy(1 = PreferDefault).
    Qt 5 wrote 10 fields with weight 50; QFont::fromString accepts both, we always write the Qt 6 form.
    """
    return f"{family},{format_size(size)},-1,5,400,0,0,0,0,0,0,0,0,0,0,1"


def parse_qfont(value: str) -> tuple[str, float] | None:
    """(family, point size) from a QFont::toString() value (Qt 5 or Qt 6 layout); None if not one.

    QFont::fromString splits on ',' too, so a family can never contain a comma. A pixel-sized font
    (point size -1) or an out-of-range size gives DEFAULT_SIZE. "@Variant(...)" (binary QFont, very old
    qt5ct) is not parsed: None.
    """
    if value.startswith("@"):
        return None
    fields = value.split(",")
    family = fields[0].strip()
    if not family:
        return None
    size = DEFAULT_SIZE
    if len(fields) > 1:
        try:
            parsed = float(fields[1])
        except ValueError:
            parsed = DEFAULT_SIZE
        if 1 <= parsed <= 200:
            size = parsed
    return family, size


def format_size(size: float) -> str:
    return str(int(size)) if size == int(size) else f"{size:g}"


# ---------------------------------------------------------------- QSettings INI values

_SIMPLE_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
                   "\\": "\\", '"': '"', "'": "'", "?": "?"}


def unquote_value(raw: str) -> str:
    """The string QSettings reads for an INI value: quotes removed, escapes resolved, ';' ends the value
    outside quotes, and an unquoted comma list (a QStringList) joined back with ','.  [V approximate]

    Enough to recover a QFont string's family and size; not a full QSettings implementation.
    """
    out: list[str] = []
    i, quoted = 0, False
    while i < len(raw):
        ch = raw[i]
        if ch == '"':
            quoted = not quoted
        elif ch == ";" and not quoted:
            break
        elif ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt == "x":
                m = re.match(r"[0-9A-Fa-f]{1,4}", raw[i + 2:])
                if m:
                    out.append(chr(int(m.group(0), 16)))
                    i += 2 + len(m.group(0))
                    continue
            out.append(_SIMPLE_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        else:
            out.append(ch)
        i += 1
    text = "".join(out)
    return ",".join(part.strip() for part in text.split(",")) if "," in text else text.strip()


# ---------------------------------------------------------------- line-based editing

def split_lines(data: bytes) -> list[bytes]:
    """Lines WITH their own endings (\\r\\n, \\r or \\n); joining them gives back exactly `data`."""
    return [m.group(0) for m in _LINE.finditer(data) if m.group(0)]


def _content(line: bytes) -> bytes:
    return line.rstrip(b"\r\n")


def _eol(line: bytes) -> bytes:
    return line[len(_content(line)):]


def _section(line: bytes) -> bytes | None:
    text = _content(line).strip()
    if text.startswith(_BOM):
        text = text[len(_BOM):].strip()
    if not text.startswith(b"["):
        return None
    # Like QSettings: the group name runs to the first ']' (or the line end); the rest is ignored.
    end = text.find(b"]")
    return (text[1:end] if end != -1 else text[1:]).strip()


def _key(line: bytes) -> bytes | None:
    text = _content(line)
    if b"=" not in text:
        return None
    return text.split(b"=", 1)[0].strip()


def scan(lines: list[bytes]) -> tuple[list[int], list[int]]:
    """(indices of [Fonts] header lines, indices of `general=` lines inside a [Fonts] group)."""
    headers: list[int] = []
    general: list[int] = []
    current: bytes | None = None
    for i, line in enumerate(lines):
        name = _section(line)
        if name is not None:
            current = name
            if name == FONTS_GROUP:
                headers.append(i)
            continue
        if current == FONTS_GROUP and _key(line) == GENERAL_KEY:
            general.append(i)
    return headers, general


def current_general(data: bytes) -> str | None:
    """The [Fonts] general value QSettings would read (last occurrence), unquoted; None if unset."""
    lines = split_lines(data)
    _, general = scan(lines)
    if not general:
        return None
    raw = _content(lines[general[-1]]).split(b"=", 1)[1].decode("utf-8", "replace")
    return unquote_value(raw)


def edit_general(data: bytes, value: str) -> bytes:
    """`data` with [Fonts] general set to `value` (quoted); every other byte kept.

    Existing general lines are rewritten in place (key spelling, spacing after '=' and line ending kept).
    Otherwise the key is inserted after the last non-blank line of the last [Fonts] group, or a new
    [Fonts] group is appended (after a blank separator line) when there is none.
    """
    encoded = b'"' + value.encode("ascii") + b'"'
    lines = split_lines(data)
    nl = b"\r\n" if b"\r\n" in data else b"\n"
    headers, general = scan(lines)
    if general:
        for i in general:
            text = _content(lines[i])
            key, rest = text.split(b"=", 1)
            lead = rest[: len(rest) - len(rest.lstrip(b" \t"))]
            lines[i] = key + b"=" + lead + encoded + _eol(lines[i])
        return b"".join(lines)

    new_line = GENERAL_KEY + b"=" + encoded + nl
    if headers:
        start = headers[-1]
        end = start + 1
        while end < len(lines) and _section(lines[end]) is None:
            end += 1
        insert_at = end
        while insert_at > start + 1 and not _content(lines[insert_at - 1]).strip():
            insert_at -= 1
        prev = lines[insert_at - 1]
        if not _eol(prev):  # last line of the file had no ending: give it one, bytes before it unchanged
            lines[insert_at - 1] = prev + nl
        lines.insert(insert_at, new_line)
        return b"".join(lines)

    prefix = data
    if prefix:
        if not prefix.endswith((b"\n", b"\r")):
            prefix += nl
        if _content(split_lines(prefix)[-1]).strip():
            prefix += nl  # blank line between groups, as QSettings writes them
    return prefix + b"[" + FONTS_GROUP + b"]" + nl + new_line


# ---------------------------------------------------------------- the adapter

def _check_fonts(fonts: FontSet) -> None:
    """Never point qt6ct at an arbitrary family (K7 names only, nothing QSettings must escape)."""
    family = fonts.ui_family
    if (not isinstance(family, str) or not family.startswith(UI_FAMILY_PREFIX)
            or len(family) <= len(UI_FAMILY_PREFIX) or not _FAMILY_OK.match(family)):
        raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR)


class QtAdapter:
    """K3 adapter for Qt applications through an existing qt6ct session (qt6ct.conf [Fonts] general)."""

    name = "qt"
    requires_restart = True  # Qt apps read qt6ct.conf at startup
    must_be_closed = False

    def __init__(self) -> None:
        self._snapshot_sha: dict[str, str] = {}

    # ---- helpers

    def _target(self) -> Target:
        return Target(self.name, TARGET_ID, "Qt apps (qt6ct)", running=False)

    def _check_target(self, target: Target) -> None:
        if target.adapter != self.name or target.target_id != TARGET_ID:
            raise ValueError("target does not belong to the Qt adapter")

    @staticmethod
    def _require_session() -> None:
        if not session_uses_qt6ct():
            raise ScribeSenseError(ErrorCode.QT6CT_NOT_IN_SESSION)

    # ---- K3

    def detect(self) -> list[Target]:
        return [self._target()]  # always listed; a non-qt6ct session surfaces as QT6CT_NOT_IN_SESSION

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
        self._check_target(target)
        self._require_session()
        location = str(conf_path())
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
        self._check_target(target)
        self._require_session()
        _check_fonts(fonts)
        location = str(conf_path())
        st = self.read_state(location)
        data = st.data if st.present and st.data is not None else b""
        size = DEFAULT_SIZE
        current = current_general(data)
        parsed = parse_qfont(current) if current is not None else None
        if parsed is not None:
            size = parsed[1]
        payload = edit_general(data, qfont_string(fonts.ui_family, size))
        return [PlannedWrite(target, location, payload, state_sha256(True, payload))]

    def apply(self, planned: PlannedWrite) -> AppliedItem:
        expected = self._snapshot_sha.get(planned.location)
        if expected is None:
            raise ValueError("apply() for a location that snapshot() did not return (K3 rule 2)")
        if not isinstance(planned.payload, bytes):
            raise ValueError("qt payload must be file bytes")
        path = _check_location(planned.location)
        if self.read_state(planned.location).sha256 != expected:
            raise ScribeSenseError(ErrorCode.CHANGED_DURING_APPLY)  # K3 rule 12
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except FileNotFoundError:
            mode = NEW_FILE_MODE
        except OSError:
            raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR) from None
        write_durable(_real(path), planned.payload, mode)
        return AppliedItem(planned.target, planned.location, planned.new_sha256)

    def verify(self, target: Target, fonts: FontSet) -> VerifyResult:
        """Configuration check (A14): qt6ct.conf read back names our UI family as [Fonts] general."""
        self._check_target(target)
        try:
            st = self.read_state(str(conf_path()))
        except ScribeSenseError:
            return VerifyResult(target, False, None, ErrorCode.CHECK_UNAVAILABLE.value)
        current = current_general(st.data) if st.present and st.data is not None else None
        parsed = parse_qfont(current) if current is not None else None
        ok = parsed is not None and parsed[0] == fonts.ui_family
        return VerifyResult(target, ok, None, "OK" if ok else ErrorCode.ADAPTER_VERIFY_FAILED.value)

    def revert(self, item: SnapshotItem, backup: bytes | None) -> None:
        """K3 rule 7: restore the ORIGINAL state (existed -> backup bytes + mode; else delete).

        Never checks the session: recovery may run outside it.
        """
        if item.kind != "file":
            raise ValueError("the Qt adapter owns files only")
        path = _check_location(item.location)
        if item.existed:
            if backup is None:
                raise ScribeSenseError(ErrorCode.BACKUP_MISSING)
            mode = item.file_mode if item.file_mode is not None else NEW_FILE_MODE
            write_durable(_real(path), backup, mode)
        else:
            delete_durable(_real(path))
