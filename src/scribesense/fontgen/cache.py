"""M1.7 -- Font installation, cache lookup and removal (owner O1).

    install(key, paths) -> list[Path]
    is_installed(key) -> bool
    remove(key) -> bool

This module is the only thing in ScribeSense that writes into the user's font
directory. It takes the artifacts M1.5 built and M1.6 passed, moves them under
``~/.local/share/fonts/scribesense/<key>/``, refreshes fontconfig, and checks that the
system can actually see what was installed. It generates nothing and validates nothing:
the card's precondition is "validation ok", and duplicating either here would mean two
implementations that can disagree about whether a font is fit to install.

What "installed" means
----------------------
Not "the files were copied". A font is installed when

1. ``fonts_dir()/<key>/`` exists and contains every artifact of that build,
2. each artifact's bytes still hash to what was installed, and
3. `MANIFEST_NAME` -- written by this module -- says so.

The manifest is a **contract addition** (K7 G1) and it exists because without it
`is_installed` cannot answer the question it is asked. A directory listing says what is
there; it cannot say what is *missing*, because the expected file set depends on which
styles the base family happened to ship (K7 rule 4) and that is not recoverable from
the key. Research2 H.2 requires exactly this -- "return True only if the key directory,
the expected artifact file(s), and the manifest are all present and consistent" -- and
Research2 I.1 requires that a ``status="cached"`` return rest on "a successful integrity
check on the existing artifact, not merely the existence of a registry row". A truncated
or half-copied artifact therefore reads as *not installed* and is replaced on the next
build, rather than being served as a cache hit.

Atomicity, and what it is honestly worth
----------------------------------------
Artifacts are copied into a staging directory inside `fonts_dir()`, fsynced, and then
the whole directory is renamed into place in one step, so no reader ever sees a
half-populated ``<key>/``. Research2 H.4 is emphatic that this must not be described as
unconditionally atomic -- POSIX does not define crash outcomes (Bornholt et al. 2016),
soft updates do not make rename atomic (LeBlanc et al. 2024), and a return value is
consistent with several different persistence states (Borrill 2026). So the write order
is temp-write -> fsync file -> fsync staging directory -> rename -> fsync parent, every
step is fallible, and `reconcile` exists for the case where a crash leaves both paths
behind. What is actually guaranteed is weaker and sufficient: **no partially populated
key directory is ever published**, and a crash leaves either the previous installation
or none, never a mixture.

Cache refresh and visibility
----------------------------
``fc-cache`` is run against `fonts_dir()` after every write and every removal -- the
card's postcondition. A copy that fontconfig has not indexed is not an installation the
user can use, and the card's acceptance test is "visible to ``fc-match`` right after
install", so `install_set` *asks* ``fc-match`` rather than assuming. Neither step can
fail the install on its own: the bytes on disk are correct and fontconfig rescans a
directory whose mtime moved, so a missing or failing ``fc-cache`` is reported as a
degraded install (`W_INSTALL_CACHE_REFRESH_FAILED`, `W_INSTALL_NOT_RESOLVED`) exactly as
Research2 H.3 prescribes, not as `INSTALL_FAILED` with the work thrown away.

This is the one place in the package that deliberately uses the user's **real**
fontconfig. M1.6 must not (its must-never), because it validates an artifact in
isolation; M1.7 must, because the question here is whether the system as the user has
configured it can see the font.

Platform. Linux/XDG throughout: `fonts_dir()` honours ``XDG_DATA_HOME``, permissions are
POSIX mode bits, the cache tool is fontconfig's. Nothing here touches a display server,
so Wayland and X11 are equally irrelevant -- fontconfig is display-server-independent,
and no global desktop setting is read or written. The module imports and runs on Windows
so the suite can exercise it there, but ``fc-cache``/``fc-match`` and directory ``fsync``
do not exist on Windows and are skipped with a warning; Fedora is the target and the
unverified items are listed in K7 G7.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from scribesense.contracts import paths
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.fontgen.naming import (
    GENERATOR_VERSION,
    KEY_LENGTH,
    fc_pattern,
    file_sha256,
    is_content_key,
    reading_family,
    ui_family,
    variant_filename,
)
from scribesense.fontgen.validate import ValidationResult, run_text
from scribesense.fontgen.variants import STYLES, VARIANT_KINDS, variant_of

__all__ = [
    "DIR_MODE",
    "FC_CACHE",
    "FC_MATCH",
    "FILE_MODE",
    "INSTALL_FAILED",
    "INSTALL_CAUSE_CODES",
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
    "REMOVE_CODES",
    "STAGING_DIR_NAME",
    "W_INSTALL_CACHE_REFRESH_FAILED",
    "W_INSTALL_NOT_RESOLVED",
    "W_INSTALL_STALE_STAGING",
    "Fontconfig",
    "FontconfigError",
    "FontconfigTool",
    "InstallFailed",
    "InstallResult",
    "ReconcileReport",
    "RemoveRefused",
    "fonts_dir",
    "install",
    "install_set",
    "installation_problem",
    "installed_files",
    "installed_keys",
    "is_installed",
    "key_dir",
    "manifest_path",
    "read_manifest",
    "reconcile",
    "remove",
]


# --------------------------------------------------------------------------- #
# Where things go
# --------------------------------------------------------------------------- #


MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1

STAGING_DIR_NAME = ".staging"
"""Where incoming and outgoing directories live while they are being moved.

Inside `fonts_dir()` because an atomic rename requires one filesystem and the must-never
forbids writing anywhere else; dot-prefixed because fontconfig skips directory entries
whose name begins with ``.``, so a staged, not-yet-published artifact cannot be indexed.
That last point is a **Fedora verification item** (K7 G7): if a fontconfig build ever
scans dot-directories, a crashed install's leftovers would be visible under the same
family name as the real one. Two things already bound the window -- `reconcile` runs at
the start of every `install_set` and the staging directory is gone before ``fc-cache``
is ever invoked -- but the check is owed.
"""

DIR_MODE = 0o755
FILE_MODE = 0o644
"""Explicit modes, because a loose ``umask`` is a real configuration (Research2 H.3,
"no world-writable paths"). Readable by the user's own session -- which is what
fontconfig, pango and every application font lookup run as -- and writable by nobody
else. Set explicitly after creation so the process umask cannot widen them.
"""

FC_CACHE = "fc-cache"
FC_MATCH = "fc-match"
SUBPROCESS_TIMEOUT = 60.0

STALE_STAGING_SECONDS = 3600.0
"""How old an abandoned staging directory must be before `reconcile` removes it.

There is no reliable way to ask "is another process still installing this?" -- a pid can
be reused, and K7 rule 1 serialises the install step anyway -- so age is the test. An
hour is far longer than any install (a copy of at most eight files) and far shorter than
the interval between a crash and the next build, so the rule cannot delete live work and
cannot leave rubbish behind indefinitely.
"""


def fonts_dir() -> Path:
    """The only directory this module may write: K8's `contracts.paths.fonts_dir()`.

    ``$SCRIBESENSE_FONTS_DIR``, else ``~/.local/share/fonts/scribesense``. Delegating
    rather than re-deriving it is the point -- the whole of this module's safety
    argument is that one function names the one directory, and the adapters (M4.x),
    the test sandbox (M3.3) and this cache must all agree on which one that is.

    The directory is not created here. A lookup (`is_installed`) must not have the side
    effect of creating anything; `install_set` creates it when it actually installs.
    """
    return paths.fonts_dir()


def key_dir(key: str) -> Path:
    """``fonts_dir()/<key>`` for a well-formed, confined key.

    Two checks, in this order, before any caller can use the path:

    * **The key is a content key** -- `fontgen.naming.is_content_key`, 12 lowercase hex
      digits and nothing else. Twelve hex digits cannot contain ``/``, ``\\``, ``..``, a
      drive letter, a space, a control character or a Unicode separator, so Research2
      H.3's traversal and fuzzing rows are answered by the character set rather than by
      a sanitiser that has to be right about every escape.
    * **The resolved path is still a child of the resolved font directory** -- belt and
      braces for the case the character set cannot see: a symlink planted *at* the
      destination. ``<key>`` itself being a symlink is refused outright, even if it
      points back inside, because installing through it would write somewhere this
      module does not own and removing it would delete somebody else's directory.

    Raises ``ValueError``; `install_set` and `remove` translate that into their own
    refusal codes, because a caller error and a filesystem hazard are reported
    differently even when the path is the same.
    """
    if not is_content_key(key):
        raise ValueError(
            f"key must be {KEY_LENGTH} lowercase hex characters, got {key!r}"
        )
    root = fonts_dir()
    target = root / key
    if target.is_symlink():
        raise ValueError(f"{target} is a symlink; refusing to install through it")
    resolved_root = Path(os.path.realpath(root))
    if Path(os.path.realpath(target)).parent != resolved_root:
        raise ValueError(f"{target} resolves outside {resolved_root}")
    return target


def manifest_path(key: str) -> Path:
    """``fonts_dir()/<key>/manifest.json``."""
    return key_dir(key) / MANIFEST_NAME


def _staging_dir() -> Path:
    return fonts_dir() / STAGING_DIR_NAME


# --------------------------------------------------------------------------- #
# Reason codes
# --------------------------------------------------------------------------- #

INSTALL_FAILED = ErrorCode.INSTALL_FAILED.value
"""The card's named failure, and the only code M1.8 puts in ``FontBuildResult``.

Every way an install can fail carries this code, so the K7 mapping is one line
(Research2 §0: ``INSTALL_FAILED`` is fixed by the architecture, not a recommendation).
The *class* of failure is on `InstallFailed.cause_code`, which satisfies the other
requirement -- Research2 H.3, "distinct machine-readable codes per failure class ... the
caller distinguishes refusal from failure" -- without inventing a second user-visible
vocabulary that K7 has no field for.
"""

INSTALL_INVALID_KEY = "INSTALL_INVALID_KEY"
INSTALL_PATH_ESCAPE = "INSTALL_PATH_ESCAPE"
INSTALL_NOT_VALIDATED = "INSTALL_NOT_VALIDATED"
INSTALL_NO_FILES = "INSTALL_NO_FILES"
INSTALL_SOURCE_MISSING = "INSTALL_SOURCE_MISSING"
INSTALL_SOURCE_NOT_REGULAR = "INSTALL_SOURCE_NOT_REGULAR"
INSTALL_FOREIGN_ARTIFACT = "INSTALL_FOREIGN_ARTIFACT"
INSTALL_KEY_MISMATCH = "INSTALL_KEY_MISMATCH"
INSTALL_DUPLICATE_FILE = "INSTALL_DUPLICATE_FILE"
INSTALL_IO_ERROR = "INSTALL_IO_ERROR"
INSTALL_PERMISSION_DENIED = "INSTALL_PERMISSION_DENIED"
INSTALL_INCOMPLETE = "INSTALL_INCOMPLETE"

INSTALL_CAUSE_CODES: tuple[str, ...] = (
    INSTALL_INVALID_KEY,
    INSTALL_PATH_ESCAPE,
    INSTALL_NOT_VALIDATED,
    INSTALL_NO_FILES,
    INSTALL_SOURCE_MISSING,
    INSTALL_SOURCE_NOT_REGULAR,
    INSTALL_FOREIGN_ARTIFACT,
    INSTALL_KEY_MISMATCH,
    INSTALL_DUPLICATE_FILE,
    INSTALL_IO_ERROR,
    INSTALL_PERMISSION_DENIED,
    INSTALL_INCOMPLETE,
)
"""Every `InstallFailed.cause_code`, in the order the checks that produce them run.

The checks are ordered cheapest-and-most-diagnostic first, and nothing touches the
filesystem until all of the argument checks have passed -- Research2 H.3's traversal row
requires "refuse; no filesystem change", which is only true if the refusal happens
before the staging directory is created.
"""

REMOVE_INVALID_KEY = "REMOVE_INVALID_KEY"
REMOVE_PATH_ESCAPE = "REMOVE_PATH_ESCAPE"
REMOVE_IN_PROGRESS = "REMOVE_IN_PROGRESS"
REMOVE_REFERENCED = "REMOVE_REFERENCED"
REMOVE_IO_ERROR = "REMOVE_IO_ERROR"

REMOVE_CODES: tuple[str, ...] = (
    REMOVE_INVALID_KEY,
    REMOVE_PATH_ESCAPE,
    REMOVE_IN_PROGRESS,
    REMOVE_REFERENCED,
    REMOVE_IO_ERROR,
)

W_INSTALL_CACHE_REFRESH_FAILED = "W_INSTALL_CACHE_REFRESH_FAILED"
"""``fc-cache`` is missing, failed or timed out. The files are installed and correct."""

W_INSTALL_NOT_RESOLVED = "W_INSTALL_NOT_RESOLVED"
"""``fc-match`` does not (yet) resolve an installed family to the installed file."""

W_INSTALL_STALE_STAGING = "W_INSTALL_STALE_STAGING"
"""An abandoned staging directory from an earlier crash was cleared away."""


class InstallFailed(ScribeSenseError):
    """An install did not happen, and nothing was left behind.

    ``code`` is always `INSTALL_FAILED` -- what M1.8 passes into
    ``FontBuildResult.reason_code`` -- and ``cause_code`` is the class of failure from
    `INSTALL_CAUSE_CODES`. The shape follows M1.4's `PatchUnsupported` (K7 D2) and M1.5's
    `BundledFontMissing`: an exception rather than a sentinel return, because the card's
    interface is ``install(key, paths) -> list[path]`` and there is no room in a list for
    a failure that a caller could then forget to check.

    Raising it is a promise about the filesystem as well as about the return value: by
    the time it reaches the caller, either the previous installation of this key is
    intact or there is none, and no staging directory survives.
    """

    def __init__(self, cause_code: str, detail: str) -> None:
        super().__init__(ErrorCode.INSTALL_FAILED)
        self.cause_code = cause_code
        self.detail = detail  # diagnosis only; the message is the bare code (errors contract)


class RemoveRefused(ScribeSenseError):
    """A removal was refused, and nothing was deleted.

    Distinct from `InstallFailed` because the two are answered differently: an install
    failure is a build that did not complete, while a refused removal usually means the
    font is still in use and the correct response is to leave it alone. ``reason_code``
    is one of `REMOVE_CODES`; ``code`` is ``FONT_IN_USE`` for a referenced or in-progress
    key and ``INSTALL_FAILED`` (the font directory could not be changed) otherwise.
    """

    def __init__(self, code: str, detail: str) -> None:
        in_use = code in (REMOVE_REFERENCED, REMOVE_IN_PROGRESS)
        super().__init__(ErrorCode.FONT_IN_USE if in_use else ErrorCode.INSTALL_FAILED)
        self.reason_code = code
        self.detail = detail


# --------------------------------------------------------------------------- #
# The fontconfig seam
# --------------------------------------------------------------------------- #


class FontconfigError(Exception):
    """``fc-cache`` or ``fc-match`` is absent, failed, or timed out."""


class FontconfigTool(Protocol):
    """The external font-cache tools, behind one seam.

    It exists for the same reason M1.6's `Renderer` does: the tools are POSIX and the
    development machine is not, and a test must be able to *induce* a cache-refresh
    failure (Research2 H.5, "fc-cache failure reported as degraded, state consistent")
    without breaking the user's font cache. It is not an escape hatch -- a substituted
    tool can only ever add warnings to an install, never turn a failed one into a
    success, because the files are already on disk before either method is called.
    """

    def missing_tools(self) -> tuple[str, ...]:
        """Executables this tool needs and cannot find."""

    def refresh(self, directory: Path) -> str:
        """Re-index ``directory``. Raises `FontconfigError`."""

    def resolve_family(self, family: str) -> tuple[str, str]:
        """``(resolved family, resolved file)`` for ``family``. Raises `FontconfigError`."""


class Fontconfig:
    """`FontconfigTool` driving the real ``fc-cache`` and ``fc-match``.

    Deliberately run against the user's **real** configuration, with the environment
    untouched apart from a pinned locale. That is the opposite of M1.6, and the
    difference is the point: M1.6 asks "is this artifact correct in isolation", M1.7 asks
    "can this user's system see it", and a private configuration would make the second
    question unanswerable. The risk M1.6's F6 documents -- a fontconfig build that
    ignores ``FONTCONFIG_FILE`` -- does not arise here, because nothing is being isolated.

    ``fc-cache -f <dir>`` is scoped to `fonts_dir()`, per Research2 H.3 and the card's
    example. Scoping matters: a bare ``fc-cache -f`` would rebuild the user's entire font
    cache, which is slow and is not this module's business.
    """

    def __init__(
        self,
        *,
        fc_cache: str | None = None,
        fc_match: str | None = None,
        timeout: float = SUBPROCESS_TIMEOUT,
    ) -> None:
        self.fc_cache = fc_cache or shutil.which(FC_CACHE)
        self.fc_match = fc_match or shutil.which(FC_MATCH)
        self.timeout = timeout

    def missing_tools(self) -> tuple[str, ...]:
        missing = []
        if not self.fc_cache:
            missing.append(FC_CACHE)
        if not self.fc_match:
            missing.append(FC_MATCH)
        return tuple(missing)

    def _run(self, argv: Sequence[str]) -> str:
        try:
            return run_text(argv, {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}, self.timeout)
        except ScribeSenseError as exc:
            raise FontconfigError(f"{Path(argv[0]).name} failed: {exc.code}") from exc
        except OSError as exc:
            raise FontconfigError(f"{argv[0]} could not be run: {exc}") from exc

    def refresh(self, directory: Path) -> str:
        if not self.fc_cache:
            raise FontconfigError(f"{FC_CACHE} is not installed")
        return self._run([self.fc_cache, "-f", str(directory)]).strip()

    def resolve_family(self, family: str) -> tuple[str, str]:
        """What fontconfig would actually use for ``family``.

        Both halves are returned and both are checked by the caller, for the reason M1.6
        gives: fontconfig substitutes rather than failing, so an artifact it cannot see
        comes back as a *different family*, while a shadowing font comes back as the
        right family at the wrong *path*.
        """
        if not self.fc_match:
            raise FontconfigError(f"{FC_MATCH} is not installed")
        out = self._run([self.fc_match, "--format=%{family}\n%{file}\n", fc_pattern(family)])
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        if len(lines) < 2:
            raise FontconfigError(f"{FC_MATCH} gave no family and file for {family!r}")
        return lines[0], lines[1]


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class InstallResult:
    """Everything one install produced, for M1.8 to turn into a K7 ``FontSet``.

    **Contract addition, M1.7 (K7 G2),** and the same shape M1.5 used for `VariantSet`
    and for the same reason: the card's interface returns ``list[path]`` and `install`
    returns exactly that, but a path list cannot carry the two things a caller needs
    besides the paths -- the degraded-install warnings Research2 H.3 requires to be
    *reported*, and the families that were installed, which M1.8 needs for ``FontSet``
    and must not re-derive. Nothing is removed from the card's interface.
    """

    key: str
    directory: Path
    files: tuple[Path, ...]
    reading_family: str
    ui_family: str
    styles: tuple[str, ...]
    status: str
    """``"installed"`` for a fresh write, ``"unchanged"`` for the idempotent no-op."""

    cache_refreshed: bool
    """Whether ``fc-cache`` completed. ``False`` means degraded, never failed."""

    resolved: bool | None
    """Whether every installed family resolves to an installed file through
    ``fc-match``. ``None`` when the tool is absent and the question was not asked."""

    warnings: tuple[str, ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)
    """Human-readable context for the warnings: the ``fc-cache`` error, what each
    family resolved to instead. Diagnosis of a degraded install is otherwise guesswork.
    """


@dataclass(frozen=True)
class ReconcileReport:
    """What a reconciliation pass found. Research2 H.3: "reconcile by scanning on
    startup"; H.5 requires a test for recovery after a simulated crash."""

    removed_staging: tuple[str, ...] = ()
    """Abandoned staging or trash directories cleared away."""

    invalid_keys: tuple[str, ...] = ()
    """Key directories that exist but fail `is_installed`.

    **Reported, not deleted.** They are inert -- fontconfig may index a leftover
    artifact, but it carries the key in its family name, so nothing resolves to it
    unless something asks for that key -- and a rebuild of the same key replaces the
    directory wholesale. Deleting on the strength of a hash mismatch would make a bug in
    the integrity check destructive, which is the wrong direction for a module whose
    must-never is about deleting the wrong thing. `remove` deletes; this reports.
    """

    unreadable: tuple[str, ...] = ()
    """Entries that could not be inspected at all (permissions, I/O)."""


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


def _identify(path: Path) -> tuple[str, str, str, str]:
    """``(family, kind, style, key)`` for one artifact, from its name alone.

    The name is parsed by *reconstructing* it: whatever is extracted is fed back through
    M1.5's `variant_filename` and must reproduce the name exactly. So this module never
    gets a say in what an artifact is called -- if M1.5's scheme changes, the check fails
    loudly here instead of this module quietly accepting a name it half-understands.
    """
    family, _, style = path.stem.rpartition("-")
    identity = variant_of(family)
    if identity is None or style not in STYLES:
        raise ValueError(f"not a generated artifact name: {path.name}")
    kind, key = identity
    if variant_filename(family, style, path.suffix.lower()) != path.name:
        raise ValueError(f"not a generated artifact name: {path.name}")
    return family, kind, style, key


def _order(entry: tuple[str, str, str]) -> tuple[int, int, str]:
    """Reading variant first, then UI, each in `STYLES` order -- M1.5's build order.

    The caller's argument order is deliberately *not* preserved: two callers passing the
    same files in different orders must produce the same manifest, or a reinstall would
    look like a change. Sorting here also makes `install`'s return value identical on a
    fresh install and on an idempotent no-op, which is what lets M1.8 treat the two the
    same way.
    """
    name, kind, style = entry
    return (VARIANT_KINDS.index(kind), STYLES.index(style), name)


def _manifest_for(key: str, sources: Sequence[Path]) -> dict[str, Any]:
    """The manifest describing an install of ``sources`` under ``key``.

    Pure, and deliberately free of anything that moves on its own: no timestamp, no
    hostname, no path to where the artifacts came from. Research2 H.3's idempotence row
    is "install twice; assert identical bytes", and a manifest carrying an install time
    would fail it -- worse, it would make two identical installs *look* different and
    defeat the no-op path below. The build identity is already in the key.
    """
    entries = []
    seen: dict[str, Path] = {}
    for path in sources:
        try:
            _family, kind, style, artifact_key = _identify(path)
        except ValueError as exc:
            raise InstallFailed(INSTALL_FOREIGN_ARTIFACT, str(exc)) from exc
        if artifact_key != key:
            raise InstallFailed(
                INSTALL_KEY_MISMATCH,
                f"{path.name} belongs to key {artifact_key}, not {key}",
            )
        if path.name in seen:
            raise InstallFailed(
                INSTALL_DUPLICATE_FILE,
                f"{path.name} given twice ({seen[path.name]} and {path})",
            )
        seen[path.name] = path
        entries.append((path.name, kind, style))

    entries.sort(key=_order)
    ordered = [seen[name] for name, _kind, _style in entries]
    files = []
    for path in ordered:
        try:
            files.append(
                {
                    "name": path.name,
                    "size": path.stat().st_size,
                    "sha256": file_sha256(path),
                }
            )
        except PermissionError as exc:
            raise InstallFailed(INSTALL_PERMISSION_DENIED, f"{path}: {exc}") from exc
        except OSError as exc:
            raise InstallFailed(INSTALL_SOURCE_MISSING, f"{path}: {exc}") from exc

    return {
        "manifest_version": MANIFEST_VERSION,
        "key": key,
        "generator_version": GENERATOR_VERSION,
        "reading_family": reading_family(key),
        "ui_family": ui_family(key),
        "styles": sorted({style for _n, _k, style in entries}, key=STYLES.index),
        "files": files,
    }


def _manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """Canonical JSON: sorted keys, fixed indentation, trailing newline, UTF-8.

    Deterministic so that reinstalling the same build writes byte-identical bytes, which
    is what makes the comparison in `install_set` a comparison of content rather than of
    formatting.
    """
    return (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")


def read_manifest(key: str) -> dict[str, Any] | None:
    """The installed manifest for ``key``, or ``None`` if there is not a readable one.

    Never raises for a malformed key, an absent directory or corrupt JSON: this is a
    lookup, and Research2 H.3's fuzzing row has a corrupted registry handing this module
    arbitrary strings. A caller that needs to know *why* asks `installation_problem`.
    """
    try:
        path = manifest_path(key)
    except ValueError:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- #
# Cache lookup
# --------------------------------------------------------------------------- #


def installation_problem(key: str) -> str | None:
    """Why ``key`` is not installed, or ``None`` if it is. The whole of `is_installed`.

    Every clause is a way a cache hit could be wrong, and each is checked against the
    filesystem rather than against a record:

    * the key is malformed, or its directory is a symlink or escapes `fonts_dir()`;
    * the directory or the manifest is absent, unreadable or not this version;
    * the manifest describes a different key, or no files at all;
    * a listed artifact is missing, is not a regular file, or has the wrong size;
    * a listed artifact's bytes no longer hash to what was installed;
    * the directory holds a font file the manifest does not list.

    The last two are what make this an integrity check rather than a directory listing,
    as Research2 I.1 requires of anything that can produce ``status="cached"``. A
    truncated file passes an existence test and fails this one -- Research2 H.3's
    "directory exists but the artifact is missing or truncated -> report not installed;
    allow reinstall".

    Cost: one SHA-256 over at most eight files, which is the same work M1.5 already does
    per build over the *sources*. Paying it on every lookup is deliberate -- a cache hit
    that skips verification is exactly the "registry row says so" shortcut the research
    rules out -- but it does mean `is_installed` is not free and should not be called in
    a loop.
    """
    try:
        directory = key_dir(key)
    except ValueError as exc:
        return str(exc)
    if not directory.is_dir():
        return f"{directory} does not exist"

    manifest = read_manifest(key)
    if manifest is None:
        return f"{directory / MANIFEST_NAME} is missing or unreadable"
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        return f"manifest version {manifest.get('manifest_version')!r} is not supported"
    if manifest.get("key") != key:
        return f"manifest names key {manifest.get('key')!r}, not {key!r}"

    listed = manifest.get("files")
    if not isinstance(listed, list) or not listed:
        return "manifest lists no files"

    expected: set[str] = set()
    for entry in listed:
        if not isinstance(entry, dict):
            return f"malformed manifest entry: {entry!r}"
        name = entry.get("name")
        if not isinstance(name, str) or Path(name).name != name or not name:
            return f"malformed artifact name in manifest: {name!r}"
        expected.add(name)
        path = directory / name
        if path.is_symlink() or not path.is_file():
            return f"{path} is missing or not a regular file"
        try:
            size = path.stat().st_size
            if size != entry.get("size"):
                return f"{path} is {size} bytes, expected {entry.get('size')}"
            if file_sha256(path) != entry.get("sha256"):
                return f"{path} does not match its recorded digest"
        except OSError as exc:
            return f"{path} could not be read: {exc}"

    try:
        strays = sorted(
            item.name
            for item in directory.iterdir()
            if item.name != MANIFEST_NAME and item.name not in expected
        )
    except OSError as exc:
        return f"{directory} could not be listed: {exc}"
    if strays:
        return f"{directory} holds unlisted files: {', '.join(strays)}"
    return None


def is_installed(key: str) -> bool:
    """Whether ``key`` is completely and verifiably installed -- the card's interface.

    See `installation_problem` for what is checked and why. ``False`` for a malformed
    key, so a corrupted registry row reads as a cache miss and triggers a rebuild rather
    than an exception on the lookup path.
    """
    return installation_problem(key) is None


def installed_files(key: str) -> list[Path]:
    """The installed artifacts for ``key``, in M1.5's build order; ``[]`` if not installed.

    M1.8 needs this on the *cached* path: K7's ``FontSet.files`` is "installed file
    paths", and on a cache hit there is no `InstallResult` to take them from. Re-deriving
    them from the key in M1.8 would mean a second copy of the naming scheme and of this
    module's layout, which is precisely what K7 E4 refused for the key itself.
    """
    if not is_installed(key):
        return []
    manifest = read_manifest(key) or {}
    directory = key_dir(key)
    return [directory / entry["name"] for entry in manifest["files"]]


def installed_keys() -> list[str]:
    """Every key with a directory under `fonts_dir()`, installed or not, sorted.

    Includes directories that fail `is_installed`, because the callers are reconciliation
    and M1.8's ``remove_unused`` -- both of which need to see the rubbish in order to deal
    with it. Anything that is not a well-formed key is not ours and is never listed.
    """
    try:
        return sorted(
            item.name
            for item in fonts_dir().iterdir()
            if item.is_dir() and not item.is_symlink() and is_content_key(item.name)
        )
    except OSError:
        return []


# --------------------------------------------------------------------------- #
# Filesystem primitives
# --------------------------------------------------------------------------- #


def _fsync_dir(path: Path) -> bool:
    """Flush a directory entry. ``False`` where the platform cannot (Windows).

    Research2 H.4's required ordering ends with "fsync the directory": without it the
    rename may be durable in the page cache and not on the disk, which is the Subversion
    failure Frost et al. 2007 describe. Windows has no directory file descriptor, so the
    development platform simply does not get this guarantee -- one of the things that
    makes Fedora the only place the crash-safety argument actually holds (K7 G7).
    """
    if os.name != "posix":
        return False
    try:
        handle = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(handle)
        return True
    except OSError:
        return False
    finally:
        os.close(handle)


def _copy_into(source: Path, destination: Path) -> None:
    """Copy one artifact and make it durable before anybody can see it."""
    with open(source, "rb") as reader, open(destination, "wb") as writer:
        shutil.copyfileobj(reader, writer, 1 << 20)
        writer.flush()
        os.fsync(writer.fileno())
    os.chmod(destination, FILE_MODE)


def _write_manifest(directory: Path, manifest: Mapping[str, Any]) -> None:
    path = directory / MANIFEST_NAME
    with open(path, "wb") as writer:
        writer.write(_manifest_bytes(manifest))
        writer.flush()
        os.fsync(writer.fileno())
    os.chmod(path, FILE_MODE)


def _discard(path: Path) -> None:
    """Remove a staging directory, swallowing failures.

    Used on the cleanup paths only, where something has already gone wrong: a rollback
    that raises would replace the real diagnosis with the cleanup's. What is left behind
    is a dot-directory `reconcile` sweeps later.
    """
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001 -- cleanup must not mask the original failure
        pass


def _new_staging(prefix: str) -> Path:
    """A fresh, private directory inside `fonts_dir()/.staging`, on one filesystem.

    The pid is in the name so that `reconcile` can tell this process's work in progress
    from an earlier process's leftovers, and the counter keeps two installs of the same
    key in one process from colliding.
    """
    staging = _staging_dir()
    staging.mkdir(parents=True, exist_ok=True)
    # The application directory and the staging area are ours and get our modes; the
    # XDG directories above them belong to the user and are left at the system default.
    for directory in (fonts_dir(), staging):
        try:
            os.chmod(directory, DIR_MODE)
        except OSError:
            pass
    index = 0
    while True:
        candidate = staging / f"{prefix}.{os.getpid()}.{index}"
        try:
            candidate.mkdir()
        except FileExistsError:
            index += 1
            continue
        os.chmod(candidate, DIR_MODE)
        return candidate


def _staging_entries(key: str | None = None) -> list[Path]:
    """Staging directories, optionally only those belonging to one key."""
    try:
        entries = sorted(_staging_dir().iterdir())
    except OSError:
        return []
    if key is None:
        return [item for item in entries if item.is_dir()]
    return [
        item
        for item in entries
        if item.is_dir() and item.name.split(".")[0] in (key, f"{key}~old")
    ]


# --------------------------------------------------------------------------- #
# The card's interface
# --------------------------------------------------------------------------- #


def install_set(
    key: str,
    paths: Iterable[str | os.PathLike[str]],
    *,
    validation: ValidationResult | None = None,
    tools: FontconfigTool | None = None,
) -> InstallResult:
    """Install the artifacts in ``paths`` as ``key``, and report what happened.

    ``key``
        The M1.5 content key the artifacts were built under. Every file must carry it in
        its family name; a mismatch is `INSTALL_KEY_MISMATCH`, because installing one
        build's artifacts under another build's key would make every future cache hit
        serve the wrong font.
    ``paths``
        The validated artifacts, normally ``VariantSet.files``. They are **copied**, not
        moved: M1.5 documents that the caller owns the temporary directory, and M1.6's
        precondition is that validation happened while the files were still in it.
    ``validation``
        M1.6's `ValidationResult` for exactly these files. Keyword-only with a default so
        the card's two-argument ``install(key, paths)`` works, but **required in
        substance** -- the card's precondition is "validation ok" and Research2 G.6 gates
        installation on "a complete validation pass, with no shortcut path". When it is
        passed and ``ok`` is false the install is refused with `INSTALL_NOT_VALIDATED`;
        when it is omitted, this module has no way to tell and the caller is asserting
        it. M1.8 has the result in hand and must pass it. Nothing is re-validated here:
        that is M1.6's module and duplicating it would create a second opinion.
    ``tools``
        The ``fc-cache``/``fc-match`` seam; see `FontconfigTool`.

    The sequence, and why it is this one:

    1. Check the key, the validation result and every source file. Nothing has touched
       the filesystem yet, so a refusal here is Research2 H.3's "no filesystem change".
    2. Hash the sources and build the manifest -- needed for the install anyway, and it
       is what makes step 3 possible.
    3. **Idempotence.** If the key is already installed with exactly this manifest, stop:
       no copy, no rename, no ``fc-cache``. Research2 H.3 -- "install on a correct key is
       a no-op success". The resolution check still runs, because a correct installation
       the system cannot see is still a problem worth reporting.
    4. Copy into a staging directory, fsync each file, write the manifest, fsync the
       directory.
    5. Move any existing ``<key>/`` aside, rename staging into place, fsync the parent,
       then delete what was moved aside. If the second rename fails the first is undone,
       so the previous installation survives a failed replacement.
    6. ``fc-cache`` over `fonts_dir()`, then ``fc-match`` for both families.

    Steps 1-5 either complete or leave the previous state; only step 6 can be partial,
    and a partial step 6 is a warning rather than a failure (Research2 H.3's
    degraded-mode row). Raises `InstallFailed` for everything else.
    """
    sources = [Path(path) for path in paths]
    warnings: list[str] = []
    detail: dict[str, Any] = {}

    if validation is not None and not validation.ok:
        raise InstallFailed(
            INSTALL_NOT_VALIDATED,
            f"validation reported {validation.reason_code}; refusing to install",
        )
    try:
        directory = key_dir(key)
    except ValueError as exc:
        cause = INSTALL_INVALID_KEY if not is_content_key(key) else INSTALL_PATH_ESCAPE
        raise InstallFailed(cause, str(exc)) from exc
    if not sources:
        raise InstallFailed(INSTALL_NO_FILES, f"no artifacts given for key {key}")

    for path in sources:
        if path.is_symlink() or not path.is_file():
            code = (
                INSTALL_SOURCE_NOT_REGULAR
                if path.is_symlink() or path.exists()
                else INSTALL_SOURCE_MISSING
            )
            raise InstallFailed(code, f"{path} is not a regular file")

    manifest = _manifest_for(key, sources)
    by_name = {path.name: path for path in sources}
    ordered = [by_name[entry["name"]] for entry in manifest["files"]]

    swept, _unreadable = _sweep_staging()
    if swept:
        warnings.append(W_INSTALL_STALE_STAGING)
        detail["stale_staging"] = swept

    if is_installed(key) and read_manifest(key) == manifest:
        return _finish(
            key,
            manifest,
            status="unchanged",
            tools=tools,
            warnings=warnings,
            detail=detail,
            refresh=False,
        )

    conflicts = _staging_entries(key)
    if conflicts:
        # K7 rule 1 serialises the install step; this is what happens if a caller does
        # not, and refusing is the only safe answer -- two processes renaming into one
        # directory is the race nothing here can make atomic.
        raise InstallFailed(
            INSTALL_INCOMPLETE,
            f"another install of {key} is in progress ({conflicts[0].name})",
        )

    try:
        staged = _new_staging(key)
    except PermissionError as exc:
        raise InstallFailed(
            INSTALL_PERMISSION_DENIED, f"{_staging_dir()}: {exc}"
        ) from exc
    except OSError as exc:
        raise InstallFailed(INSTALL_IO_ERROR, f"{_staging_dir()}: {exc}") from exc

    try:
        for path in ordered:
            _copy_into(path, staged / path.name)
        _write_manifest(staged, manifest)
        _fsync_dir(staged)
    except PermissionError as exc:
        _discard(staged)
        raise InstallFailed(INSTALL_PERMISSION_DENIED, f"{exc}") from exc
    except OSError as exc:
        # Disk full, a vanished source, a read error part way through a copy. The
        # staging directory is the only thing that was written and it goes with the
        # failure, so the installed state is exactly what it was.
        _discard(staged)
        raise InstallFailed(INSTALL_IO_ERROR, f"{exc}") from exc

    displaced: Path | None = None
    try:
        if directory.exists():
            displaced = _new_staging(f"{key}~old")
            displaced.rmdir()
            os.rename(directory, displaced)
        os.rename(staged, directory)
    except OSError as exc:
        _discard(staged)
        extra = ""
        if displaced is not None and displaced.exists():
            # The previous installation is the only copy there is; put it back. If even
            # that fails it is left in the staging area rather than deleted -- a font
            # that is hard to find beats a font that is gone.
            try:
                os.rename(displaced, directory)
            except OSError as undo:
                extra = (
                    f"; the previous installation is at {displaced} and could not be "
                    f"restored: {undo}"
                )
            else:
                displaced = None
        raise InstallFailed(
            INSTALL_IO_ERROR, f"publishing {directory}: {exc}{extra}"
        ) from exc

    _fsync_dir(fonts_dir())
    if displaced is not None:
        _discard(displaced)

    return _finish(
        key,
        manifest,
        status="installed",
        tools=tools,
        warnings=warnings,
        detail=detail,
        refresh=True,
    )


def _finish(
    key: str,
    manifest: Mapping[str, Any],
    *,
    status: str,
    tools: FontconfigTool | None,
    warnings: list[str],
    detail: dict[str, Any],
    refresh: bool,
) -> InstallResult:
    """Refresh the cache, check the font is visible, and assemble the result.

    The two steps are separate questions and are reported separately. ``fc-cache``
    failing says the index was not rebuilt; ``fc-match`` resolving elsewhere says the
    font is not reachable *whatever* the index says -- the card's acceptance test is the
    second one, and Research2 H.3 asks for resolution to be "re-verified" precisely when
    the first one failed. So a refresh failure is never allowed to skip the check: if the
    font resolves anyway (fontconfig rescans a directory whose mtime moved), the install
    is degraded on paper and working in practice, and the result says both.
    """
    tool = tools if tools is not None else Fontconfig()
    directory = key_dir(key)
    families = (manifest["reading_family"], manifest["ui_family"])
    refreshed = _refresh(tool, warnings, detail) if refresh else False

    resolved = _resolution_ok(tool, families, directory, detail)
    if resolved is False and not refreshed:
        # Correct files the system cannot see. Rebuilding the index is the one repair
        # available here, and it is worth one attempt before reporting a degraded
        # install: this is the path a no-op reinstall takes after the user's cache was
        # cleared, and the path a failed first refresh takes.
        if _refresh(tool, warnings, detail):
            refreshed = True
            detail.pop("resolution", None)
            resolved = _resolution_ok(tool, families, directory, detail)
    if resolved is False:
        warnings.append(W_INSTALL_NOT_RESOLVED)

    return InstallResult(
        key=key,
        directory=directory,
        files=tuple(directory / entry["name"] for entry in manifest["files"]),
        reading_family=manifest["reading_family"],
        ui_family=manifest["ui_family"],
        styles=tuple(manifest["styles"]),
        status=status,
        cache_refreshed=refreshed,
        resolved=resolved,
        warnings=tuple(warnings),
        detail=detail,
    )


def _refresh(
    tool: FontconfigTool, warnings: list[str], detail: dict[str, Any]
) -> bool:
    """``fc-cache`` over `fonts_dir()`. ``False``, with a warning, if it did not run."""
    try:
        tool.refresh(fonts_dir())
    except FontconfigError as exc:
        if W_INSTALL_CACHE_REFRESH_FAILED not in warnings:
            warnings.append(W_INSTALL_CACHE_REFRESH_FAILED)
        detail["cache_refresh"] = str(exc)
        return False
    return True


def _resolution_ok(
    tool: FontconfigTool,
    families: Sequence[str],
    directory: Path,
    detail: dict[str, Any],
) -> bool | None:
    """Does every installed family resolve to a file in ``directory``?

    ``None`` means the question could not be asked -- ``fc-match`` is absent or errored
    -- which is reported in ``detail`` but is not evidence either way. Both halves of
    the answer are checked for the reason `Fontconfig.resolve_family` gives.
    """
    answer: bool | None = None
    for family in families:
        try:
            got_family, got_file = tool.resolve_family(family)
        except FontconfigError as exc:
            detail.setdefault("resolution", {})[family] = str(exc)
            continue
        ok = got_family == family and _within(Path(got_file), directory)
        answer = ok if answer is None else (answer and ok)
        if not ok:
            detail.setdefault("resolution", {})[family] = f"{got_family} -> {got_file}"
    return answer


def _within(path: Path, directory: Path) -> bool:
    """Whether ``path`` is inside ``directory``, both resolved through symlinks."""
    try:
        return Path(os.path.realpath(path)).parent == Path(os.path.realpath(directory))
    except OSError:
        return False


def install(
    key: str,
    paths: Iterable[str | os.PathLike[str]],
    *,
    validation: ValidationResult | None = None,
    tools: FontconfigTool | None = None,
) -> list[Path]:
    """Install ``paths`` as ``key`` and return the installed paths -- the card's interface.

    Reading variant first, then UI, each in `STYLES` order, identical on a fresh install
    and on an idempotent reinstall. For the warnings a degraded install carries, and for
    the families and styles M1.8 needs, call `install_set`; this is a wrapper over it.

    Raises `InstallFailed` (``code == "INSTALL_FAILED"``) and leaves nothing behind.
    """
    return list(install_set(key, paths, validation=validation, tools=tools).files)


# --------------------------------------------------------------------------- #
# Removal
# --------------------------------------------------------------------------- #


def remove(
    key: str,
    *,
    is_referenced: Callable[[str], bool] | None = None,
    tools: FontconfigTool | None = None,
) -> bool:
    """Delete ``key``'s directory and nothing else. ``True`` if anything was removed.

    ``is_referenced``
        An optional "is this font still in use?" predicate. **The policy is not M1.7's.**
        K7 rule 3 defines what keeps a font alive -- the active configuration, a saved
        preset, an unfinished transaction, a ``LocationRecord``, a live setting found by
        inspecting a target's font fields -- and every one of those lives in a module
        this one must not import (M1.8, M3.x). None of them exist in the repository yet
        (see the report below), so there is nothing to call and nothing to guess at.
        What M1.7 can do is refuse when it is *told*, and refuse when its own evidence
        says the key is busy, which is what this parameter and the in-progress check
        below are. Research2 H.3 requires both rows ("refuse removal while referenced",
        "treat keys with an open transaction marker as pinned"); M1.8's ``remove_unused``
        is where K7 rule 3's "if usage can't be determined, keep it" is decided.

    Removing an absent key is a success that removed nothing -- Research2 H.3's
    idempotence row, "remove on an absent key returns success" -- so a crash between a
    removal and whatever records it cannot wedge the caller.

    What protects everything else on disk:

    * the key must be a content key, so no traversal can name a path outside;
    * ``<key>`` must not be a symlink and must resolve inside `fonts_dir()`, so a planted
      link cannot redirect the deletion;
    * only ``<key>/`` is deleted, never `fonts_dir()` itself, never a sibling key, never
      anything in ``~/.local/share/fonts`` that this module did not install. The user's
      own fonts and the bundled originals under ``fonts/`` are not reachable from here at
      all -- this function cannot name a path outside one directory.

    The directory is renamed into the staging area first and deleted afterwards, so the
    font disappears from fontconfig's view in one step rather than losing files one at a
    time. ``shutil.rmtree`` does not follow directory symlinks, so a link planted *inside*
    a key directory is unlinked rather than followed.

    Raises `RemoveRefused` with one of `REMOVE_CODES`; the filesystem is untouched when
    it does.
    """
    try:
        directory = key_dir(key)
    except ValueError as exc:
        code = REMOVE_INVALID_KEY if not is_content_key(key) else REMOVE_PATH_ESCAPE
        raise RemoveRefused(code, str(exc)) from exc

    busy = _staging_entries(key)
    if busy:
        raise RemoveRefused(
            REMOVE_IN_PROGRESS,
            f"an install of {key} is in progress ({busy[0].name})",
        )
    if is_referenced is not None and is_referenced(key):
        raise RemoveRefused(REMOVE_REFERENCED, f"{key} is still referenced")

    if not directory.exists():
        return False

    trash: Path | None = None
    try:
        trash = _new_staging(f"{key}~old")
        trash.rmdir()
        os.rename(directory, trash)
    except OSError as exc:
        if trash is not None:
            _discard(trash)
        raise RemoveRefused(REMOVE_IO_ERROR, f"{directory}: {exc}") from exc
    _fsync_dir(fonts_dir())
    shutil.rmtree(trash, ignore_errors=True)

    tool = tools if tools is not None else Fontconfig()
    try:
        tool.refresh(fonts_dir())
    except FontconfigError:
        # The card's postcondition is "fc-cache after every write" and this was one, but
        # the font is gone either way: a stale cache entry for a deleted file makes
        # fontconfig fall back, it does not resurrect the font. Reporting it would need a
        # return channel the card's `remove(key)` does not have, and failing the removal
        # after the files are gone would be a lie.
        pass
    return True


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #


def reconcile() -> ReconcileReport:
    """Clear up after an interrupted install. Safe to call at any time.

    Research2 H.3 asks for reconciliation "by scanning on startup" and H.4 for a design
    that copes with "both the temp and final paths surviving a crash". Both reduce to one
    job here, because a published key directory is complete by construction: sweep the
    staging area. A staging directory is abandoned when it was made by another process
    and has not been touched for `STALE_STAGING_SECONDS`; this process's own directories
    are never swept, so a reconciliation cannot delete an install that is running.

    Key directories that fail `is_installed` are *reported* and left alone -- see
    `ReconcileReport.invalid_keys`.

    The staging sweep runs at the start of every `install_set`; this function is the
    whole pass, for the application to run at startup. Nothing outside
    `fonts_dir()/.staging` is ever deleted by it.
    """
    removed, unreadable = _sweep_staging()
    invalid = [key for key in installed_keys() if not is_installed(key)]
    return ReconcileReport(
        removed_staging=tuple(removed),
        invalid_keys=tuple(invalid),
        unreadable=tuple(unreadable),
    )


def _sweep_staging() -> tuple[list[str], list[str]]:
    """The staging half of `reconcile`: ``(removed, unreadable)``.

    Separate from `reconcile` because `install_set` runs it on every call and must not
    pay for the integrity scan of every other installed key to do so.
    """
    removed: list[str] = []
    unreadable: list[str] = []
    now = time.time()
    mine = os.getpid()

    for entry in _staging_entries():
        parts = entry.name.split(".")
        try:
            owner = int(parts[1]) if len(parts) > 1 else -1
        except ValueError:
            owner = -1
        if owner == mine:
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError as exc:
            unreadable.append(f"{entry.name}: {exc}")
            continue
        if age < STALE_STAGING_SECONDS:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        if entry.exists():
            unreadable.append(f"{entry.name}: could not be removed")
        else:
            removed.append(entry.name)
    return removed, unreadable
