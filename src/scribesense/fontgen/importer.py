"""O1 — W1 user font import (PLAN_v2 Phase III W1; built 2026-11-24 by O4 for O1 with the user's go-ahead).

    import_files(paths, repo=store.user_fonts)          a font file (or several)
    import_folder(folder, repo=store.user_fonts)        the .ttf / .otf files directly in one folder
    import_installed(family, repo=store.user_fonts)     a font already installed on the system (via fc-list)
    installed_families()                                what `import_installed` can offer
    delete_family(family, repo=store.user_fonts)        remove an imported family (refused while in use)
    stored_files(family, repo=store.user_fonts)         K2 lookup for `config.bundled.set_imported_files`

Contract (K1 rule 6, K2 rule 2, K8 `user_fonts_dir`, K9 `UserFontsRepo`, amendment 2026-11-24):

1. **Copy first, then check the copy.** The source bytes (at most `rules.MAX_FONT_BYTES`) are written to a private
   staging file under `user_fonts_dir()` and every check runs on THAT file, so a source that changes after the check
   cannot slip through. Nothing is ever read in place from the system afterwards (K2).
2. **Untrusted parse in a child process with a deadline:** `rules.is_eligible(copy, isolate=True)` — same verdict as
   the bundled fonts (size, tables, not monospace / icon, Latin coverage), plus FONT_PARSE_TIMEOUT. Only after it
   passes is the copy opened here, and only its `name` / `OS/2` / `head` / `fvar` tables are read.
3. **Styles:** only Regular, Bold, Italic, BoldItalic (OS/2 fsSelection / usWeightClass; a variable font is its default
   instance: Regular or Italic). Light, SemiBold, … are refused per file, never promoted. A family needs a Regular
   (in this import or already stored).
4. **Names (K1 rule 6):** the family from the font's own name table. A bundled name (any case) gets " (imported)"
   appended, so a bundled name always means the bundled font. Anything else rule 6 refuses -> IMPORT_NOT_ELIGIBLE.
5. **Stored as** `user_fonts_dir()/<sha256>/<Style>.ttf|.otf` — the user's file name never becomes a path. Dirs 0700,
   files 0600, written durably. A replaced or failed copy is removed when no row refers to it any more.
6. Codes: IMPORT_NOT_ELIGIBLE (the font fails a check — `detail` says which), IMPORT_FAILED (could not copy, could not
   check, or could not store), IMPORTED_FONT_IN_USE (replacing / deleting a family a preset or the active style names).
   Logs carry codes only, never file names or paths (M4.15).
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from scribesense.config import rules
from scribesense.config.settings import imported_name_ok
from scribesense.contracts.config import SHORTLIST
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.paths import run_cmd, user_fonts_dir
from scribesense.log import log

STYLES: tuple[str, ...] = ("Regular", "Bold", "Italic", "BoldItalic")
FONT_SUFFIXES = (".ttf", ".otf")
MAX_FILES = 64  # per import: a folder of hundreds of files is not a family
IMPORTED_SUFFIX = " (imported)"
FC_LIST_TIMEOUT_S = 10.0
STAGING = ".staging"

#: Per-file reasons (shown by the UI next to the file; never logged with the file name).
STYLE_UNSUPPORTED = "STYLE_UNSUPPORTED"  # Light, SemiBold, Condensed, …
BAD_FAMILY_NAME = "BAD_FAMILY_NAME"  # K1 rule 6
NO_REGULAR = "NO_REGULAR"
DUPLICATE_STYLE = "DUPLICATE_STYLE"  # two files for one style in one import: the first (sorted) wins
TOO_MANY_FILES = "TOO_MANY_FILES"

SourceKind = Literal["file", "folder", "installed"]


@dataclass(frozen=True)
class Refused:
    source: str  # the file's base name, for the UI only
    code: ErrorCode
    detail: str  # a rules.* reason or one of the constants above


@dataclass
class ImportReport:
    imported: dict[str, list[str]] = field(default_factory=dict)  # family -> styles added (or already stored)
    refused: list[Refused] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.imported)


@dataclass(frozen=True)
class _Candidate:
    source: str
    staged: Path
    sha256: str
    size: int
    family: str
    style: str
    suffix: str


# ---------------------------------------------------------------- files and folders

def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _staging() -> Path:
    return _private_dir(_private_dir(user_fonts_dir()) / STAGING)


def _clear_staging() -> None:
    """Leftovers of an interrupted import are never used: remove them."""
    folder = user_fonts_dir() / STAGING
    if folder.is_dir():
        for leftover in folder.iterdir():
            leftover.unlink(missing_ok=True)


def _read_capped(path: Path) -> bytes | None:
    """The source bytes, or None when unreadable or over MAX_FONT_BYTES (checked while reading, not by stat)."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(rules.MAX_FONT_BYTES + 1)
    except OSError:
        return None
    return None if len(data) > rules.MAX_FONT_BYTES else data


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


# ---------------------------------------------------------------- reading the checked copy

def _family_and_style(path: Path) -> tuple[str, str | None, str]:
    """(family, style or None if unsupported, suffix) from the CHECKED copy's own tables."""
    from fontTools.ttLib import TTFont

    with TTFont(path, lazy=True) as font:
        family = font["name"].getBestFamilyName() or ""
        suffix = ".otf" if font.sfntVersion == "OTTO" else ".ttf"
        os2 = font["OS/2"] if "OS/2" in font else None
        selection = getattr(os2, "fsSelection", 0) if os2 is not None else 0
        weight = getattr(os2, "usWeightClass", 400) if os2 is not None else 400
        italic = bool(selection & 0x01) or bool(font["head"].macStyle & 0x02)
        if "fvar" in font:
            bold = False  # a variable font is offered as its default instance
        elif selection & 0x20 or weight == 700:
            bold = True
        elif selection & 0x40 or weight == 400:
            bold = False
        else:
            return family, None, suffix  # Light / Medium / SemiBold / Black …: never promoted
    style = ("Bold" if bold else "") + ("Italic" if italic else "")
    return family, style or "Regular", suffix


def display_family(name: str) -> str:
    """K1 rule 6: a bundled name (any case) gets IMPORTED_SUFFIX, so it never shadows the bundled font."""
    name = name.strip()
    if name.casefold() in {s.casefold() for s in SHORTLIST}:
        return name + IMPORTED_SUFFIX
    return name


# ---------------------------------------------------------------- import

def import_files(paths: Sequence[str | os.PathLike[str]], *, repo: Any,
                 source_kind: SourceKind = "file") -> ImportReport:
    """Import the given font files. Never raises for a bad font: every refusal is in the report."""
    report = ImportReport()
    files = sorted({Path(p) for p in paths}, key=lambda p: (p.name, str(p)))
    if len(files) > MAX_FILES:
        report.refused.append(Refused(f"{len(files)} files", ErrorCode.IMPORT_NOT_ELIGIBLE, TOO_MANY_FILES))
        return report
    try:
        _clear_staging()
        staging = _staging()
    except OSError:
        log(ErrorCode.IMPORT_FAILED, "import", step="staging")
        report.refused += [Refused(p.name, ErrorCode.IMPORT_FAILED, "STAGING") for p in files]
        return report
    candidates: list[_Candidate] = []
    try:
        for path in files:
            candidate = _check_one(path, staging, report)
            if candidate is not None:
                candidates.append(candidate)
        _store(candidates, repo, source_kind, report)
    finally:
        _clear_staging()
    return report


def import_folder(folder: str | os.PathLike[str], *, repo: Any) -> ImportReport:
    """The .ttf / .otf files directly inside `folder` (not its sub-folders; symlinks are not followed)."""
    try:
        entries = [e for e in Path(folder).iterdir()
                   if e.suffix.lower() in FONT_SUFFIXES and e.is_file() and not e.is_symlink()]
    except OSError:
        report = ImportReport()
        report.refused.append(Refused(Path(folder).name, ErrorCode.IMPORT_FAILED, "FOLDER_UNREADABLE"))
        return report
    return import_files(entries, repo=repo, source_kind="folder")


def _check_one(path: Path, staging: Path, report: ImportReport) -> _Candidate | None:
    def refuse(code: ErrorCode, detail: str) -> None:
        report.refused.append(Refused(path.name, code, detail))

    data = _read_capped(path)
    if data is None:
        refuse(ErrorCode.IMPORT_NOT_ELIGIBLE, rules.FONT_TOO_LARGE if _too_large(path) else rules.FONT_UNREADABLE)
        return None
    sha = hashlib.sha256(data).hexdigest()
    staged = staging / f"{sha}.tmp"
    try:
        _write_private(staged, data)
    except OSError:
        log(ErrorCode.IMPORT_FAILED, "import", step="copy")
        refuse(ErrorCode.IMPORT_FAILED, "COPY")
        return None
    verdict = rules.is_eligible(staged, isolate=True)  # untrusted: a child process with a deadline (W1.3)
    if not verdict.ok:
        staged.unlink(missing_ok=True)
        refuse(ErrorCode.IMPORT_NOT_ELIGIBLE, verdict.reason_code or rules.FONT_UNREADABLE)
        return None
    try:
        raw_family, style, suffix = _family_and_style(staged)
    except Exception:  # noqa: BLE001 — the child accepted it, this process could not read it: refuse
        staged.unlink(missing_ok=True)
        refuse(ErrorCode.IMPORT_NOT_ELIGIBLE, rules.FONT_UNREADABLE)
        return None
    family = display_family(raw_family)
    if style is None:
        staged.unlink(missing_ok=True)
        refuse(ErrorCode.IMPORT_NOT_ELIGIBLE, STYLE_UNSUPPORTED)
        return None
    if not imported_name_ok(family):
        staged.unlink(missing_ok=True)
        refuse(ErrorCode.IMPORT_NOT_ELIGIBLE, BAD_FAMILY_NAME)
        return None
    return _Candidate(path.name, staged, sha, len(data), family, style, suffix)


def _too_large(path: Path) -> bool:
    try:
        return path.stat().st_size > rules.MAX_FONT_BYTES
    except OSError:
        return False


def _store(candidates: list[_Candidate], repo: Any, source_kind: SourceKind, report: ImportReport) -> None:
    by_family: dict[str, dict[str, _Candidate]] = {}
    for c in candidates:
        slot = by_family.setdefault(c.family, {})
        if c.style in slot:
            report.refused.append(Refused(c.source, ErrorCode.IMPORT_NOT_ELIGIBLE, DUPLICATE_STYLE))
            continue
        slot[c.style] = c
    for family, styles in by_family.items():
        stored = {e.style for e in repo.by_family(family)}
        if "Regular" not in styles and "Regular" not in stored:
            report.refused += [Refused(c.source, ErrorCode.IMPORT_NOT_ELIGIBLE, NO_REGULAR) for c in styles.values()]
            continue
        for style in [s for s in STYLES if s in styles]:  # Regular first: a family is never stored without it
            _store_one(styles[style], repo, source_kind, report)


def _store_one(c: _Candidate, repo: Any, source_kind: SourceKind, report: ImportReport) -> None:
    root = user_fonts_dir()
    previous = [e.sha256 for e in repo.by_family(c.family) if e.style == c.style and e.sha256 != c.sha256]
    file_name = c.style + c.suffix
    target_dir = root / c.sha256
    try:
        _private_dir(target_dir)
        target = target_dir / file_name
        os.replace(c.staged, target)
        os.chmod(target, 0o600)
        repo.add(c.sha256, c.family, c.style, file_name, c.size, source_kind)
    except ScribeSenseError as exc:  # IMPORTED_FONT_IN_USE: the old copy stays, the new one goes
        _remove_unreferenced(repo, [c.sha256])
        report.refused.append(Refused(c.source, exc.code, exc.code.value))
        return
    except (OSError, ValueError):
        log(ErrorCode.IMPORT_FAILED, "import", step="store")
        _remove_unreferenced(repo, [c.sha256])
        report.refused.append(Refused(c.source, ErrorCode.IMPORT_FAILED, "STORE"))
        return
    _remove_unreferenced(repo, previous)  # a replaced style's old copy
    report.imported.setdefault(c.family, []).append(c.style)


def _remove_unreferenced(repo: Any, shas: Iterable[str]) -> None:
    root = user_fonts_dir()
    for sha in shas:
        if repo.get(sha) is None and len(sha) == 64:
            shutil.rmtree(root / sha, ignore_errors=True)


# ---------------------------------------------------------------- lookup and delete

def stored_files(family: str, *, repo: Any) -> Mapping[str, Path] | None:
    """`config.bundled.set_imported_files` provider: style -> stored copy, or None if `family` is not imported."""
    entries = repo.by_family(family)
    if not entries:
        return None
    return {e.style: user_fonts_dir() / e.sha256 / e.file_name for e in entries}


def delete_family(family: str, *, repo: Any) -> bool:
    """Remove an imported family. IMPORTED_FONT_IN_USE (raised) while a preset or the active style names it.
    True when something was removed."""
    shas = repo.delete_family(family)
    _remove_unreferenced(repo, shas)
    return bool(shas)


# ---------------------------------------------------------------- installed fonts

def _fc_list() -> list[tuple[str, Path]]:
    try:
        out = run_cmd(["fc-list", "--format", "%{family[0]}\\t%{file}\\n"], timeout=FC_LIST_TIMEOUT_S).stdout
    except (ScribeSenseError, OSError):
        return []
    pairs = []
    for line in out.decode("utf-8", errors="replace").splitlines():
        family, _, file = line.partition("\t")
        if family and file and Path(file).suffix.lower() in FONT_SUFFIXES:
            pairs.append((family.strip(), Path(file)))
    return pairs


def installed_families() -> list[str]:
    """Families fontconfig lists with a .ttf / .otf file, without ScribeSense's own generated fonts."""
    from scribesense.fontgen.naming import is_generated_family

    return sorted({f for f, _ in _fc_list() if not is_generated_family(f) and not f.startswith("AccessSansUI-")},
                  key=str.casefold)


def import_installed(family: str, *, repo: Any) -> ImportReport:
    """Copy an installed family's files in (K2: never used in place). Same checks as any import."""
    files = [path for name, path in _fc_list() if name == family]
    if not files:
        report = ImportReport()
        report.refused.append(Refused(family, ErrorCode.IMPORT_FAILED, "NOT_INSTALLED"))
        return report
    return import_files(files, repo=repo, source_kind="installed")
