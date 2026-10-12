"""O4 — fontconfig adapter (M4.2): make fontconfig hand out the generated fonts.

Owns exactly one file: config_home()/fontconfig/conf.d/99-scribesense.conf (K3 rule 10: absolute path).
(The user conf.d is included by the system's 50-user.conf, so these rules run before 60-latin.conf's
preferred-family aliases; "prepend" therefore puts our family ahead of the distro defaults.)

Routing (A13 — explicit routing, no guessing of text purpose):
    generic "sans-serif" and "serif"         -> fonts.reading_family   (reading variant)
    the user's UI font family (gsettings
    org.gnome.desktop.interface font-name)   -> fonts.ui_family        (UI variant)
Never redirected (card: "Must never redirect monospace or icon families"):
    generic "monospace", "emoji", "math"; and any UI family whose name looks monospace / icon / emoji.
    fontconfig's own generic names and aliases ("Sans", "Sans Serif", "Mono", "System UI", ... compared
    case- and blank-insensitively) are never given a UI rule either: the system fonts.conf rewrites them
    to their generic before conf.d runs, so such a rule could never fire (and A13 routes generics to the
    reading variant only).
    That last check is a HEURISTIC on the family name (substrings in MONO_ICON_HINTS, case-insensitive).
    Known consequences, tested: "Source Code Pro", "JetBrains Mono", "Font Awesome", "Noto Color Emoji"
    are skipped; a non-monospace family whose name merely contains one of the hints (e.g. "Monotype
    Corsiva") is skipped too — the safe direction. Families that already are ScribeSense families
    (AccessSans*) are skipped so a rule can never redirect our own fonts. Names with characters that
    XML 1.0 cannot carry (controls, surrogates, U+FFFE/U+FFFF) are skipped: one bad name would make
    fontconfig reject the whole rule file.

Preset switching runs the same sequence without a revert (§2.2), and the GTK adapter (M4.3) rewrites
font-name to the UI family, so on a later apply gsettings no longer names the user's own UI family.
plan() therefore carries forward the named-UI-family redirects of the current rule file (only if the
file is one ScribeSense generated). After a revert the file is gone or the user's own again, so
nothing is carried.

Verification (A14, M4.13 helpers in adapters/verify.py):
    chosen check  `fc-match` (verify.chosen_family) must name our family for sans-serif, serif and each
                  redirected UI family of the current rule file;
    drawn check   Pango in a fresh process (verify.drawn_family_pango("sans-serif")) must draw EVERY run of
                  the fixed sample in the reading family. If the probe cannot run, drawn_ok=False and the
                  detail is CHECK_UNAVAILABLE: fc-match alone is never reported as verified (card M4.13).

External commands go through an injectable runner (default contracts.paths.run_cmd, K3 rule 9) and
the UI-font source is injectable (default reads gsettings through Gio lazily) so tests need neither
real processes nor PyGObject.

Logging: through scribesense.log (allowed under import rule R5). Failures are raised as ScribeSenseError
codes for the controller; the one swallowed failure — the cache refresh after revert — is logged, not
raised (revert must not fail because of it).
"""

from __future__ import annotations

import errno
import os
import re
import stat
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from scribesense.adapters.pango_probe import valid_query
from scribesense.adapters.verify import chosen_family, drawn_family_pango
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
from scribesense.contracts.fonts import READING_FAMILY_PREFIX, UI_FAMILY_PREFIX, FontSet
from scribesense.contracts.paths import config_home, fonts_dir, run_cmd
from scribesense.log import log

#: K3 rule 9 — deadlines for this card (fc-match 10 s, fc-cache 60 s; the Pango probe uses "other").
TIMEOUTS: dict[str, float] = {**DEFAULT_TIMEOUTS, "fc-match": 10.0, "fc-cache": 60.0}
#: Generic family the drawn check lays out (A13: generics -> reading variant).
DRAWN_CHECK_QUERY = "sans-serif"

TARGET_ID = "fontconfig:user"
RULE_FILE_NAME = "99-scribesense.conf"
_RULE_SUFFIX = ("fontconfig", "conf.d", RULE_FILE_NAME)

#: Generic families redirected to the reading variant (A13).
READING_GENERICS: tuple[str, ...] = ("sans-serif", "serif")
#: Generic families that are never redirected.
NEVER_GENERICS: frozenset[str] = frozenset({"monospace", "emoji", "math"})
#: Heuristic: a UI family whose name contains one of these (case-insensitive) is never redirected.
MONO_ICON_HINTS: tuple[str, ...] = (
    "mono", "code", "nerd", "icon", "symbol", "awesome", "emoji", "console", "terminal",
)
_OWN_FAMILY_PREFIX = "accesssans"

#: Pango style / weight / stretch words stripped from the end of a font-name ("Fira Sans Semi-Bold 11").
#: "Roman" and the gravity words (North, South, ...) are deliberately NOT stripped: "Times New Roman".
STYLE_WORDS: frozenset[str] = frozenset(
    w.lower()
    for w in (
        "Regular", "Normal", "Book", "Italic", "Oblique",
        "Thin", "Hairline", "Ultra-Light", "UltraLight", "Extra-Light", "ExtraLight", "Light",
        "Semi-Light", "SemiLight", "Demi-Light", "DemiLight", "Medium",
        "Semi-Bold", "SemiBold", "Demi-Bold", "DemiBold", "Bold", "Extra-Bold", "ExtraBold",
        "Ultra-Bold", "UltraBold", "Black", "Heavy", "Ultra-Black", "UltraBlack", "Ultra-Heavy",
        "UltraHeavy", "Extra-Black", "ExtraBlack",
        "Ultra-Condensed", "UltraCondensed", "Extra-Condensed", "ExtraCondensed", "Condensed",
        "Semi-Condensed", "SemiCondensed", "Semi-Expanded", "SemiExpanded", "Expanded",
        "Extra-Expanded", "ExtraExpanded", "Ultra-Expanded", "UltraExpanded",
        "Small-Caps", "All-Small-Caps", "Petite-Caps", "All-Petite-Caps", "Unicase", "Title-Caps",
    )
)
_SIZE = re.compile(r"^\d+(?:\.\d+)?(?:px)?$", re.IGNORECASE)
#: Characters never allowed in a family we write: everything XML 1.0 cannot carry (C0 controls other
#: than tab/LF/CR, lone surrogates, U+FFFE, U+FFFF) plus tab/LF/CR/DEL, which are never part of a family.
_BAD_CHAR = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff\ufffe\uffff]")
#: fontconfig's generic families and the aliases its fonts.conf assigns to them, as _generic_key() spells them.
FONTCONFIG_GENERICS: frozenset[str] = frozenset({
    "sans-serif", "sans", "sansserif", "serif", "monospace", "mono", "emoji", "math", "system-ui", "systemui",
})
#: Marker in every file render_rules() produces; plan() carries forward rules only from such a file.
_GENERATED_MARK = b"Generated by ScribeSense"

GSETTINGS_SCHEMA = "org.gnome.desktop.interface"
GSETTINGS_KEY = "font-name"

#: Same signature as run_cmd; the result needs only a bytes `.stdout`.
Runner = Callable[[list[str], float], Any]
UiFontSource = Callable[[], list[str]]


# ---------------------------------------------------------------- paths

def rule_path() -> Path:
    """The one location this adapter owns. Computed at call time (the environment may change)."""
    return config_home() / "fontconfig" / "conf.d" / RULE_FILE_NAME


def _check_location(location: str) -> Path:
    """Refuse anything that is not an absolute .../fontconfig/conf.d/99-scribesense.conf path.

    Recovery may hand back a journaled location recorded under a different config_home, so the check
    is on the path's shape, not equality with rule_path(); it still forbids arbitrary files.
    """
    path = Path(location)
    if not path.is_absolute() or path.parts[-3:] != _RULE_SUFFIX or ".." in path.parts:
        raise ValueError("not a fontconfig rule-file location this adapter owns")
    return path


# ---------------------------------------------------------------- UI font names

def parse_font_family(font_name: str) -> list[str]:
    """Family names from a Pango font description ("Fira Sans Semi-Bold 11" -> ["Fira Sans"]).

    Pango syntax: "[FAMILY-LIST] [STYLE-OPTIONS] [SIZE][@VARIATIONS]". The size and trailing style
    words are stripped; a comma-separated family list yields several families. Unparseable -> [].
    """
    text = font_name.split("@", 1)[0].strip()
    if not text:
        return []
    words = text.replace(",", " , ").split()
    if words and _SIZE.match(words[-1]):
        words.pop()
    while words and words[-1].lower() in STYLE_WORDS:
        words.pop()
    families: list[str] = []
    for chunk in " ".join(words).split(","):
        name = " ".join(chunk.split())
        if name:
            families.append(name)
    return families


def read_gsettings_ui_fonts() -> list[str]:
    """Default UI-font source: the effective org.gnome.desktop.interface font-name, or [] on ANY failure.

    Gio is imported lazily (CI has no PyGObject; R2-8 allows Gio). The schema is looked up first
    because Gio.Settings aborts the whole process on a missing schema.
    """
    try:
        from gi.repository import Gio

        source = Gio.SettingsSchemaSource.get_default()
        if source is None:
            return []
        schema = source.lookup(GSETTINGS_SCHEMA, True)
        if schema is None or not schema.has_key(GSETTINGS_KEY):
            return []
        value = Gio.Settings.new(GSETTINGS_SCHEMA).get_value(GSETTINGS_KEY).unpack()
        return [value] if isinstance(value, str) and value.strip() else []
    except Exception:  # noqa: BLE001 — no UI font is a valid outcome, never a crash
        return []


def _generic_key(name: str) -> str:
    """Case- and blank-insensitive spelling, so "Sans Serif" / "SansSerif" / "sans serif" compare equal."""
    return "".join(name.split()).casefold()


def _skip_family(name: str) -> bool:
    low = name.casefold()
    key = _generic_key(name)
    return (
        not key
        or bool(_BAD_CHAR.search(name))  # not representable in XML 1.0
        # One acceptance rule for writer and verifier: a family verify cannot query (format chars such
        # as U+200B / U+00AD, or longer than MAX_QUERY_LEN) gets no rule, else verify could never pass.
        or not valid_query(name)
        or key in NEVER_GENERICS
        or key in FONTCONFIG_GENERICS  # generics + aliases go to the reading variant only (A13)
        or key.startswith(_OWN_FAMILY_PREFIX)
        or any(hint in low for hint in MONO_ICON_HINTS)
    )


def ui_families(font_names: Iterable[str], carried: Iterable[str] = ()) -> list[str]:
    """UI families to redirect, in first-seen order, deduplicated (case-insensitive), heuristic applied.

    font_names are Pango font descriptions (parsed); carried are bare family names from the current
    rule file (not re-parsed: parsing is not idempotent for a family ending in a number). Duplicates are
    dropped case-insensitively, the way a fontconfig <test> compares strings.
    """
    result: list[str] = []
    seen: set[str] = set()
    families: list[str] = []
    for font_name in font_names:
        if isinstance(font_name, str):
            families += parse_font_family(font_name)
    families += [f for f in carried if isinstance(f, str)]
    for family in families:
        key = family.casefold()
        if key in seen or _skip_family(family):
            continue
        seen.add(key)
        result.append(family)
    return result


# ---------------------------------------------------------------- rule file

def _check_fonts(fonts: FontSet) -> None:
    """Never write a rule that points at an arbitrary family (K7 names only)."""
    ok = (
        isinstance(fonts.reading_family, str)
        and isinstance(fonts.ui_family, str)
        and fonts.reading_family.startswith(READING_FAMILY_PREFIX)
        and len(fonts.reading_family) > len(READING_FAMILY_PREFIX)
        and fonts.ui_family.startswith(UI_FAMILY_PREFIX)
        and len(fonts.ui_family) > len(UI_FAMILY_PREFIX)
        and not _BAD_CHAR.search(fonts.reading_family + fonts.ui_family)
    )
    if not ok:
        raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR)


def _match(family: str, generated: str) -> str:
    return (
        '  <match target="pattern">\n'
        f'    <test name="family" qual="any"><string>{escape(family)}</string></test>\n'
        f'    <edit name="family" mode="prepend" binding="strong"><string>{escape(generated)}</string></edit>\n'
        "  </match>\n"
    )


def render_rules(fonts: FontSet, ui: list[str]) -> bytes:
    """The full rule file. Deterministic: same inputs -> same bytes (no timestamps)."""
    _check_fonts(fonts)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        '<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">\n',
        f"<!-- {_GENERATED_MARK.decode()}. Do not edit: this file is replaced on apply and removed by"
        " `scribesense reset`. -->\n",
        "<fontconfig>\n",
        "  <!-- Generic families -> reading variant (A13). monospace, emoji and math are never redirected. -->\n",
    ]
    parts += [_match(g, fonts.reading_family) for g in READING_GENERICS]
    if ui:
        parts.append("  <!-- Named UI font families -> UI variant (A13). -->\n")
        parts += [_match(f, fonts.ui_family) for f in ui]
    parts.append("</fontconfig>\n")
    return "".join(parts).encode("utf-8")


def parse_rules(data: bytes) -> list[tuple[str, str]]:
    """(tested family, generated family) pairs from a rule file; [] if it cannot be parsed."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    pairs = []
    for match in root.iter("match"):
        test = match.find("test/string")
        edit = match.find("edit/string")
        if test is not None and edit is not None and test.text and edit.text:
            pairs.append((test.text, edit.text))
    return pairs


# ---------------------------------------------------------------- durable file ops (§4 Durability)

def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        raise ScribeSenseError(ErrorCode.WRITE_NOT_DURABLE) from None
    try:
        os.fsync(fd)
    except OSError:
        raise ScribeSenseError(ErrorCode.WRITE_NOT_DURABLE) from None
    finally:
        os.close(fd)


def _os_error(exc: OSError) -> ScribeSenseError:
    if isinstance(exc, PermissionError) or exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM):
        return ScribeSenseError(ErrorCode.NOT_WRITABLE)
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return ScribeSenseError(ErrorCode.NO_DISK_SPACE)
    return ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR)


def _make_dirs(directory: Path) -> None:
    """Create missing parents top-down (0o755), fsyncing each new directory's parent."""
    missing: list[Path] = []
    d = directory
    while not d.exists():
        missing.append(d)
        d = d.parent
    for new in reversed(missing):
        try:
            new.mkdir(mode=0o755)
        except FileExistsError:
            continue
        except OSError as exc:
            raise _os_error(exc) from None
        _fsync_dir(new.parent)


def write_durable(path: Path, data: bytes, mode: int) -> None:
    """temp file in the same dir -> write -> chmod -> fsync -> os.replace -> fsync dir.

    The mode is set before the file fsync so the sync also persists it (§4, K3 rule 8): after a crash
    the file can never sit under its final name with mkstemp's 0o600.
    """
    _make_dirs(path.parent)
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    except OSError as exc:
        raise _os_error(exc) from None
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        except OSError as exc:
            raise _os_error(exc) from None
        try:
            os.fchmod(fd, mode)
        except OSError as exc:
            raise _os_error(exc) from None
        try:
            os.fsync(fd)
        except OSError:
            raise ScribeSenseError(ErrorCode.WRITE_NOT_DURABLE) from None
        os.close(fd)
        fd = -1
        try:
            os.replace(tmp, path)
        except OSError as exc:
            raise _os_error(exc) from None
        tmp = ""
        _fsync_dir(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def delete_durable(path: Path) -> None:
    """Remove the file (already missing is fine) and fsync its directory."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _os_error(exc) from None
    _fsync_dir(path.parent)


# ---------------------------------------------------------------- the adapter

class FontconfigAdapter:
    """K3 adapter for the user's fontconfig rules."""

    name = "fontconfig"
    requires_restart = True  # running apps keep their already-resolved fonts
    must_be_closed = False

    def __init__(self, *, runner: Runner = run_cmd, ui_font_source: UiFontSource = read_gsettings_ui_fonts) -> None:
        self._run = runner
        self._ui_font_source = ui_font_source
        self._snapshot_sha: dict[str, str] = {}

    # ---- helpers

    def _target(self) -> Target:
        return Target(self.name, TARGET_ID, "Font rules (fontconfig)", running=False)

    def _check_target(self, target: Target) -> None:
        if target.adapter != self.name or target.target_id != TARGET_ID:
            raise ValueError("target does not belong to the fontconfig adapter")

    def _ui_families(self) -> list[str]:
        try:
            names = self._ui_font_source()
        except Exception:  # noqa: BLE001 — same contract as the default source: failure = no UI fonts
            names = []
        return ui_families(names if isinstance(names, list) else [], self._carried_ui_families())

    def _carried_ui_families(self) -> list[str]:
        """Named UI families the current (ScribeSense-generated) rule file already redirects."""
        st = self.read_state(str(rule_path()))
        if not st.present or st.data is None or _GENERATED_MARK not in st.data:
            return []
        return [fam for fam, gen in parse_rules(st.data) if gen.startswith(UI_FAMILY_PREFIX)]

    def _refresh_cache(self) -> None:
        directory = fonts_dir()
        if directory.is_dir():
            self._run(["fc-cache", str(directory)], TIMEOUTS["fc-cache"])

    # ---- K3

    def detect(self) -> list[Target]:
        return [self._target()]

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
        location = str(rule_path())
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
        payload = render_rules(fonts, self._ui_families())
        return [PlannedWrite(target, str(rule_path()), payload, state_sha256(True, payload))]

    def apply(self, planned: PlannedWrite) -> AppliedItem:
        expected = self._snapshot_sha.get(planned.location)
        if expected is None:
            raise ValueError("apply() for a location that snapshot() did not return (K3 rule 2)")
        if not isinstance(planned.payload, bytes):
            raise ValueError("fontconfig payload must be file bytes")
        path = _check_location(planned.location)
        if self.read_state(planned.location).sha256 != expected:
            raise ScribeSenseError(ErrorCode.CHANGED_DURING_APPLY)  # K3 rule 12
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except FileNotFoundError:
            mode = 0o644
        except OSError:
            raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR) from None
        write_durable(path, planned.payload, mode)
        # Errors here propagate: the intent is journaled, so the controller reverts (card: any failure).
        # run_cmd lets a refused launch (PermissionError) and other launch OSErrors through unchanged;
        # K3 rule 5 needs a ScribeSenseError. Not NOT_WRITABLE: the file was written, fc-cache did not start.
        try:
            self._refresh_cache()
        except OSError:
            raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR) from None
        return AppliedItem(planned.target, planned.location, planned.new_sha256)

    def verify(self, target: Target, fonts: FontSet) -> VerifyResult:
        """Chosen check (fc-match) + drawn check (Pango, fresh process) — A14, M4.13.

        chosen_ok: every checked family (sans-serif, serif, each redirected UI family) resolves to ours.
        drawn_ok:  every run of the fixed sample, laid out as "sans-serif", is drawn in the reading family;
                   False when the probe cannot run (never None for this target).
        detail_code: "OK" only when chosen_ok and drawn_ok are both True; else ADAPTER_VERIFY_FAILED when a
        check ran and disagreed, else CHECK_UNAVAILABLE (a check could not run).
        "Verify passed" means chosen_ok AND drawn_ok is not False (K3 VerifyResult; here drawn_ok is a bool).
        """
        self._check_target(target)
        checks = [(g, fonts.reading_family) for g in READING_GENERICS]
        chosen_ok, mismatch, unavailable = True, False, False
        try:
            st = self.read_state(str(rule_path()))
        except ScribeSenseError:
            chosen_ok, unavailable = False, True
        else:
            if st.present and st.data is not None:
                checks += [(fam, gen) for fam, gen in parse_rules(st.data)
                           if gen == fonts.ui_family and fam not in READING_GENERICS]
            for family, expected in checks:
                try:
                    chosen = chosen_family(target, family, runner=self._run, timeout=TIMEOUTS["fc-match"])
                except (ScribeSenseError, OSError):
                    chosen_ok, unavailable = False, True
                    break
                if chosen != expected:
                    chosen_ok, mismatch = False, True

        try:
            drawn = drawn_family_pango(DRAWN_CHECK_QUERY, runner=self._run, timeout=TIMEOUTS["other"])
        except (ScribeSenseError, OSError):
            drawn_ok = False
            unavailable = True
        else:
            drawn_ok = drawn == (fonts.reading_family,)
            mismatch = mismatch or not drawn_ok

        if chosen_ok and drawn_ok:
            code = "OK"
        elif mismatch:
            code = ErrorCode.ADAPTER_VERIFY_FAILED.value
        else:
            code = ErrorCode.CHECK_UNAVAILABLE.value
        return VerifyResult(target, chosen_ok, drawn_ok, code)

    def revert(self, item: SnapshotItem, backup: bytes | None) -> None:
        """K3 rule 7: restore the ORIGINAL state (existed -> backup bytes + mode; else delete)."""
        if item.kind != "file":
            raise ValueError("fontconfig owns files only")
        path = _check_location(item.location)
        if item.existed:
            if backup is None:
                raise ScribeSenseError(ErrorCode.BACKUP_MISSING)
            mode = item.file_mode if item.file_mode is not None else 0o644
            write_durable(path, backup, mode)  # b"" is a valid empty file
        else:
            delete_durable(path)
        try:
            self._refresh_cache()
        except (ScribeSenseError, OSError) as exc:
            # a stale cache must not fail a revert; fontconfig rescans config on its own
            code = exc.code if isinstance(exc, ScribeSenseError) else ErrorCode.UNEXPECTED_ADAPTER_ERROR
            log(code, "adapter", adapter=self.name, target=TARGET_ID, step="revert-cache-refresh")
