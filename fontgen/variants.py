"""M1.5 -- Reading and UI variants (owner O1).

    build_variants(config) -> list[Path]

One `Configuration` yields **two variants** -- a reading font bound to
``config.reading_line`` and a UI font bound to ``config.ui_line`` -- in up to four
styles each, all sharing one content key from `fontgen.naming`::

    AccessSans-3f9a1c2b7d10-Regular.ttf       reading, uses reading_line
    AccessSans-3f9a1c2b7d10-Bold.ttf
    AccessSansUI-3f9a1c2b7d10-Regular.ttf     UI, uses ui_line
    AccessSansUI-3f9a1c2b7d10-Bold.ttf

Everything is written into a temporary directory. Nothing here installs, registers or
validates an artifact -- that is M1.6 and M1.7, reached through M1.8.

Why the two variants exist at all
---------------------------------
K7 D5: M1.4 writes the requested line height into ``hhea.lineGap`` and
``OS/2.sTypoLineGap``. One font file can carry exactly one line height, and the
architecture asks for two -- a generous one for reading surfaces, a tighter one for
interface chrome, which is why K1 has both ``reading_line`` and ``ui_line`` and why
``ui_line`` above 1.3 earns a warning (chrome clips). So the two differ in exactly one
input, and `patch` takes that input as its own ``line`` parameter precisely because one
configuration yields two artifacts.

Research2 F.1 requires the binding to be one-way and tested as such: ``reading_line`` is
"read only by the Reading variant" and ``ui_line`` only by the UI variant, with
cross-wiring named as the failure mode. `variant_line` is the single place that choice
is made, and `tests/test_variants.py` asserts both directions.

Styles
------
Regular is required; Bold, Italic and BoldItalic are built only when the base family
ships them [D R2, K7 rule 4]. A missing Regular is the one hard failure
(`BUNDLED_FONT_MISSING`); the rest are reported through `VariantSet.styles`, which is
what K7's ``FontSet.styles`` carries. Nothing is ever synthesised: Research2 F.5 is
explicit that a generator "must not fabricate" a missing style, because algorithmic
emboldening and slanting change glyph shapes, which is outside this module's scope and
has no evidence base behind it.

Platform: fontTools and the stdlib. Paths are built with ``pathlib`` and the temporary
directory comes from ``tempfile``, so this behaves identically on Windows (development)
and Fedora/Wayland (target). No display server, no fontconfig, no subprocess -- font
generation does not depend on a display server at all, and fontconfig enters only at
M1.7's install step.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from config.rules import is_eligible
from config.settings import Configuration, SHORTLIST, validate
from fontgen.naming import (
    FAMILY_PATTERN,
    GENERATOR_VERSION,
    READING_FAMILY_PREFIX,
    UI_FAMILY_PREFIX,
    content_key,
    reading_family,
    source_sha_for,
    ui_family,
    variant_filename,
)
from fontgen.patcher import patch

__all__ = [
    "BUNDLED_FONT_MISSING",
    "FONTS_ROOT",
    "OPTIONAL_STYLES",
    "REQUIRED_STYLE",
    "STYLES",
    "VARIANT_KINDS",
    "BaseFontIneligible",
    "BuildPlan",
    "BundledFontMissing",
    "VariantSet",
    "base_font_files",
    "build_variant_set",
    "build_variants",
    "plan",
    "variant_family",
    "variant_line",
    "variant_of",
]

VariantKind = Literal["reading", "ui"]

VARIANT_KINDS: tuple[VariantKind, ...] = ("reading", "ui")
"""The two variants, in build order. Order is fixed so `build_variants` is stable."""

_KIND_BY_PREFIX: Mapping[str, VariantKind] = {
    READING_FAMILY_PREFIX: "reading",
    UI_FAMILY_PREFIX: "ui",
}

REQUIRED_STYLE = "Regular"
"""The one style every base family must provide [D R2]."""

OPTIONAL_STYLES: tuple[str, ...] = ("Bold", "Italic", "BoldItalic")
"""Built when the base family ships them; their absence is not an error [D R2]."""

STYLES: tuple[str, ...] = (REQUIRED_STYLE, *OPTIONAL_STYLES)
"""Every style this module knows, in the order `VariantSet.styles` reports them.

A fixed order matters beyond tidiness: ``styles`` reaches the user through K7's
``FontSet`` and is compared between builds, so it must not depend on directory
iteration order, which differs between filesystems and between Windows and Fedora.
"""

BUNDLED_FONT_MISSING = "BUNDLED_FONT_MISSING"
"""Reason code for a base family with no usable Regular [K2, M1.5 card]."""

FONTS_ROOT = Path(__file__).resolve().parents[1] / "fonts"
"""Repository root of the bundled font tree, per K2's layout.

Resolved from this file rather than the working directory, so a build behaves the same
whatever the process was started from.
"""

_SUFFIXES = (".ttf", ".otf")

_STYLE_ALIASES: Mapping[str, str] = {
    "regular": "Regular",
    "bold": "Bold",
    "italic": "Italic",
    "oblique": "Italic",
    "bolditalic": "BoldItalic",
    "boldoblique": "BoldItalic",
}
"""Filename style tokens, lowercased, mapped onto `STYLES`.

Oblique is folded into Italic because the two occupy the same slot in a four-style
family -- a face is offered to the user as "italic" whether its designer slanted or
redrew it. Anything else (``Light``, ``SemiBold``, ``Condensed``, a variable font's
``VF``) is ignored rather than guessed at: a four-style family is what K2's manifest
describes, and silently promoting ``SemiBold`` to ``Bold`` would ship a weight the user
did not choose.
"""


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


class BundledFontMissing(Exception):
    """The base family has no usable Regular style.

    The card's only hard failure: "Missing Regular -> ``BUNDLED_FONT_MISSING``; other
    missing styles are not an error". K2 names the same code for `base_font_files`, so
    one code covers "the family is not bundled", "its directory is empty" and "it ships
    everything except a Regular" -- from the caller's side these are one situation, and
    M1.8 maps all of them to ``status="refused"`` with this ``reason_code``.
    """

    code = BUNDLED_FONT_MISSING

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"{BUNDLED_FONT_MISSING}: {detail}")


class BaseFontIneligible(Exception):
    """The Regular style exists but M1.3 refuses it.

    Carries M1.3's own code -- ``BASE_IS_MONOSPACE``, ``NO_LATIN_COVERAGE``,
    ``FONT_UNREADABLE`` and the rest of K7 C1 -- unchanged on ``.code``, so M1.8 can
    pass it straight into ``FontBuildResult.reason_code`` the way K7 already requires
    for a direct `is_eligible` refusal. Re-coding it here would lose the diagnosis; the
    user needs to know the font is monospaced, not merely that something failed.

    Distinct from `BundledFontMissing` because the two mean different things to whoever
    bundled the font: one file is absent, the other is present and unsuitable.
    """

    def __init__(self, style: str, reason_code: str, path: Path) -> None:
        self.style = style
        self.code = reason_code
        self.path = path
        super().__init__(f"{reason_code}: {style} style at {path}")


# --------------------------------------------------------------------------- #
# Base fonts
# --------------------------------------------------------------------------- #


def base_font_files(family: str) -> dict[str, Path]:
    """Bundled ``style -> file`` map for one family, per K2's ``fonts/`` layout.

    **Provisional implementation of a K2 interface, not an M1.5 one.** K2 specifies
    ``base_font_files(family) -> dict[str, Path]`` raising ``BUNDLED_FONT_MISSING``, and
    records that neither it nor the ``fonts/`` tree exists yet -- M1.2 hit the same gap
    and resolved it by injection (K2 B1). M1.5 cannot: the card's interface is
    ``build_variants(config)`` with no second argument, so a default reader has to
    exist. When K2's own lands, import it here and delete this body; the signature and
    the failure code are already its.

    Styles are inferred from file names (``NotoSans-BoldItalic.ttf`` -> ``BoldItalic``,
    a stem with no ``-`` -> ``Regular``), which is the convention the four shortlisted
    upstreams use. That inference is the part K2's ``manifest.toml`` will replace, and
    it is the weaker half: the manifest *declares* the mapping instead of guessing it.
    Where two files claim one style, the first in sorted order wins -- arbitrary, but
    deterministic, which is the property that matters to the key.

    Raises `BundledFontMissing` when the family is unknown, its directory is absent, or
    it ships no Regular.
    """
    if family not in SHORTLIST:
        raise BundledFontMissing(f"{family!r} is not in SHORTLIST")

    directory = FONTS_ROOT / family
    if not directory.is_dir():
        raise BundledFontMissing(f"no bundled directory for {family!r}: {directory}")

    found: dict[str, Path] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in _SUFFIXES:
            continue
        stem = path.stem
        token = stem.rsplit("-", 1)[-1] if "-" in stem else "regular"
        style = _STYLE_ALIASES.get(token.replace(" ", "").lower())
        if style is not None:
            found.setdefault(style, path)

    if REQUIRED_STYLE not in found:
        raise BundledFontMissing(f"{family!r} ships no {REQUIRED_STYLE} style")
    return found


def _usable_styles(
    files: Mapping[str, str | os.PathLike[str]],
) -> tuple[dict[str, Path], tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    """Split a ``style -> file`` map into what can be built, skipped and warned about.

    Eligibility is M1.3's `is_eligible`, the same gate the rest of the pipeline uses --
    the card requires it and it is the only reason a *present* file is dropped. Regular
    is held to a stricter rule than the rest, exactly as the card's failure table says:

    * Regular absent, or not a readable file -> `BundledFontMissing`.
    * Regular present but ineligible         -> `BaseFontIneligible`.
    * an optional style absent               -> skipped, silently; not an error.
    * an optional style ineligible           -> skipped, with its refusal code recorded.

    Treating an ineligible optional style as unavailable rather than fatal follows K7
    rule 4 ("only available styles are built and reported") and the card's "other
    missing styles are not an error". The user gets a working Regular rather than no
    font at all because an italic was corrupt, and the reason is reported rather than
    swallowed. Unknown style names in the mapping are ignored: `STYLES` is the contract.
    """
    built: dict[str, Path] = {}
    skipped: list[tuple[str, str]] = []
    warnings: list[tuple[str, str]] = []

    for style in STYLES:
        raw = files.get(style)
        if raw is None:
            if style == REQUIRED_STYLE:
                raise BundledFontMissing(f"no {REQUIRED_STYLE} style was offered")
            continue

        path = Path(raw)
        verdict = is_eligible(path)
        if not verdict.ok:
            if style == REQUIRED_STYLE:
                raise BaseFontIneligible(style, verdict.reason_code or "", path)
            skipped.append((style, verdict.reason_code or ""))
            continue

        built[style] = path
        warnings.extend((style, code) for code in verdict.warnings)

    return built, tuple(skipped), tuple(warnings)


# --------------------------------------------------------------------------- #
# Variant selection
# --------------------------------------------------------------------------- #


def variant_line(config: Configuration, kind: VariantKind) -> float:
    """The line height for one variant -- the whole of the reading/UI distinction.

    Research2 F.1 names cross-wiring as the failure mode for both bindings, so this is
    deliberately the only place in the module that reads either field, and the only
    place a test has to pin.
    """
    if kind == "reading":
        return config.reading_line
    if kind == "ui":
        return config.ui_line
    raise ValueError(f"unknown variant kind: {kind!r}")


def variant_family(kind: VariantKind, key: str) -> str:
    """``AccessSans-<key>`` for reading, ``AccessSansUI-<key>`` for UI [K7]."""
    if kind == "reading":
        return reading_family(key)
    if kind == "ui":
        return ui_family(key)
    raise ValueError(f"unknown variant kind: {kind!r}")


def variant_of(family: object) -> tuple[VariantKind, str] | None:
    """The inverse of `variant_family`: ``family -> (kind, key)``, or ``None``.

    M1.6 is handed a list of built files and has to work out which variant each one is
    -- and therefore which line height it should carry -- from the family name alone.
    Doing that by slicing the string there would be a second copy of the naming scheme;
    this keeps the forward and reverse directions in one module, as the card requires
    ("owns ... the family names").

    Returns ``None`` for anything that is not a generated family, so a caller cannot
    accidentally treat a system font as one of ours.

    >>> variant_of("AccessSansUI-3f9a1c2b7d10")
    ('ui', '3f9a1c2b7d10')
    >>> variant_of("Noto Sans") is None
    True
    """
    if not isinstance(family, str):
        return None
    match = FAMILY_PATTERN.match(family)
    if match is None:
        return None
    return _KIND_BY_PREFIX[match["prefix"]], match["key"]


@dataclass(frozen=True)
class BuildPlan:
    """What a build *would* produce, worked out before a single font is patched.

    **Contract addition, M1.8 (K7 H1).** M1.8 has to answer "is this already installed?"
    before it does any work, and the answer is addressed by the content key -- which,
    until now, only existed once `build_variant_set` had already patched everything. The
    alternatives were both bad: patch eight fonts and throw them away on a cache hit, or
    re-derive the key inside M1.8, which is the second implementation of K7 E2 that this
    module exists to prevent. Splitting the first three steps out costs nothing and is
    reusable -- `build_variant_set` takes the plan back so the eligibility pass and the
    source hashing happen exactly once per build.

    It is also what M1.8 refuses on: `skipped` and `warnings` are known here, so a user
    whose Italic was dropped learns it whether or not the build went on to succeed.
    """

    key: str
    """The content key both variants will carry -- `fontgen.naming.content_key`."""

    source_sha: str
    """The digest of the surviving source styles, as hashed into the key."""

    sources: Mapping[str, Path]
    """``style -> base file``, only the styles that passed eligibility."""

    styles: tuple[str, ...]
    """The same styles, in `STYLES` order."""

    skipped: tuple[tuple[str, str], ...] = ()
    """``(style, refusal_code)`` for each optional style dropped as ineligible."""

    warnings: tuple[tuple[str, str], ...] = ()
    """``(style, warning_code)`` from M1.3, in style then M1.3 order."""


def plan(
    config: Configuration,
    *,
    files: Mapping[str, str | os.PathLike[str]] | None = None,
) -> BuildPlan:
    """Resolve the sources, apply eligibility and compute the key -- no patching.

    Everything `build_variant_set` does before it writes anything, and nothing else. It
    reads each candidate font (M1.3 parses it) and hashes the survivors, so it is not
    free; it is simply orders of magnitude cheaper than a build, which patches every
    style twice and then renders.

    Raises exactly what `build_variant_set` raises at this stage: ``ValueError`` for an
    unvalidated configuration, `BundledFontMissing` or `BaseFontIneligible` for the base
    font. A caller that plans first therefore sees those refusals before any temporary
    directory exists.
    """
    return _make_plan(config, files)


def _make_plan(
    config: Configuration,
    files: Mapping[str, str | os.PathLike[str]] | None,
) -> BuildPlan:
    errors = [issue for issue in validate(config) if issue.level == "error"]
    if errors:
        detail = ", ".join(f"{i.field}={i.code}" for i in errors)
        raise ValueError(f"build_variants() requires a validated Configuration: {detail}")

    sources = base_font_files(config.base_family) if files is None else dict(files)
    built, skipped, warnings = _usable_styles(sources)
    source_sha = source_sha_for(built)
    return BuildPlan(
        key=content_key(config, source_sha, GENERATOR_VERSION),
        source_sha=source_sha,
        sources=built,
        styles=tuple(style for style in STYLES if style in built),
        skipped=skipped,
        warnings=warnings,
    )


@dataclass(frozen=True)
class VariantSet:
    """Everything one build produced, ready for M1.8 to turn into a K7 ``FontSet``.

    **Contract addition, M1.5.** The card's interface returns ``list[path]``, and
    `build_variants` does exactly that. But K7's ``FontSet`` needs ``key``,
    ``reading_family``, ``ui_family`` and ``styles`` as well, and M1.8 must not re-derive
    them -- re-deriving the key means a second implementation of the formula this module
    is supposed to own, and the two would eventually disagree. So the fuller result is
    available from `build_variant_set` and `build_variants` is a thin wrapper over it.
    Nothing is removed from the card's interface.

    ``skipped`` and ``warnings`` answer K7 C4's open question with data rather than a
    decision: a style dropped as ineligible, and M1.3's non-blocking observations
    (``PARTIAL_LATIN_COVERAGE``, ``ITALIC_ONLY``, ``VARIABLE_FONT``), are visible to the
    caller. Whether ``FontBuildResult`` grows a field to surface them is still M1.8's
    call -- this just makes sure the information survives the build.
    """

    key: str
    """The 12-character content key shared by both variants."""

    reading_family: str
    """``AccessSans-<key>``."""

    ui_family: str
    """``AccessSansUI-<key>``."""

    styles: tuple[str, ...]
    """Styles actually built, in `STYLES` order -> K7 ``FontSet.styles``."""

    files: tuple[Path, ...]
    """Every artifact written: reading variant first, then UI, each in style order."""

    directory: Path
    """The temporary directory holding them. **The caller owns its removal.**"""

    source_sha: str
    """The digest of the base-font styles this build used, for tracing a key."""

    skipped: tuple[tuple[str, str], ...] = ()
    """``(style, reason_code)`` for each optional style dropped as ineligible."""

    warnings: tuple[tuple[str, str], ...] = ()
    """``(style, warning_code)`` from M1.3, in style then M1.3 order."""

    def files_for(self, kind: VariantKind) -> tuple[Path, ...]:
        """The artifacts belonging to one variant, in `STYLES` order."""
        family = variant_family(kind, self.key)
        return tuple(path for path in self.files if path.stem.startswith(f"{family}-"))


# --------------------------------------------------------------------------- #
# The card's interface
# --------------------------------------------------------------------------- #


def build_variant_set(
    config: Configuration,
    *,
    files: Mapping[str, str | os.PathLike[str]] | None = None,
    out_dir: str | os.PathLike[str] | None = None,
    plan: BuildPlan | None = None,
) -> VariantSet:
    """Build both variants and report what was produced.

    ``config``
        A resolved, validated K1 `Configuration` -- the card's precondition is "config
        rounded (M1.1)". An unrounded one is a caller error and raises ``ValueError``,
        for the reason spelled out in `fontgen.naming.content_key`: 0.131 and 0.13 would
        otherwise share a key while producing different bytes.
    ``files``
        A ``style -> path`` map overriding `base_font_files`. Keyword-only with a
        default, so the card's one-argument ``build_variants(config)`` is unchanged.
        It exists because the bundled ``fonts/`` tree is K2's and does not exist yet
        (see `base_font_files`), and because tests need to build against fixtures
        rather than the repository's own fonts.
    ``out_dir``
        Where to write. Defaults to a fresh ``tempfile.mkdtemp()`` -- the card's "writes
        into a temporary directory". Given explicitly, it is created if needed and
        written into as-is, which is what K7's ``build_preview`` needs (a private temp
        dir it deletes when the preview closes) and what M1.7 needs to stage an install.
    ``plan``
        A `BuildPlan` from `plan`, when the caller has already done steps 1-3 -- M1.8
        does, to look the key up in the cache before deciding to build at all. It
        supersedes ``files``: the plan already names the sources that survived
        eligibility, and re-running the gate could only produce a different answer,
        never a better one. Omitted, the plan is computed here as it always was.

    Order of operations, and why it is this order:

    1. Validate the configuration -- a caller error should cost nothing.
    2. Resolve the base files, then run M1.3 eligibility over them. **Before** hashing,
       because the key must describe what is actually built: an ineligible Italic is not
       in the output, so it must not be in the key either, or the same build would get
       two keys depending on whether a file it never used happened to be present.
    3. Hash the surviving sources and compute the key.
    4. Patch each style twice, once per variant, with that variant's line height.

    Raises `BundledFontMissing`, `BaseFontIneligible`, M1.4's ``PatchUnsupported`` or
    ``ValueError``; see `_usable_styles` and `fontgen.patcher.patch`. A failure part way
    through leaves already-written artifacts in the temporary directory -- they are
    inert, carry no installed name and are removed with it. Nothing partial is ever
    *returned*: the exception propagates and there is no `VariantSet` to mistake for a
    complete build.
    """
    prepared = _make_plan(config, files) if plan is None else plan
    built = dict(prepared.sources)
    key = prepared.key

    if out_dir is None:
        directory = Path(tempfile.mkdtemp(prefix=f"scribesense-{key}-"))
    else:
        directory = Path(out_dir)
        directory.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for kind in VARIANT_KINDS:
        family = variant_family(kind, key)
        line = variant_line(config, kind)
        for style in STYLES:
            source = built.get(style)
            if source is None:
                continue
            suffix = source.suffix.lower()
            out_path = directory / variant_filename(
                family, style, suffix if suffix in _SUFFIXES else ".ttf"
            )
            # family_name is what keeps the artifact from shadowing the original
            # (Research2 E.7): it carries the key, so it can never equal the source
            # family -- `patch` rejects the call outright if it ever did.
            patch(source, config, line, out_path, family_name=family)
            written.append(out_path)

    return VariantSet(
        key=key,
        reading_family=reading_family(key),
        ui_family=ui_family(key),
        styles=prepared.styles,
        files=tuple(written),
        directory=directory,
        source_sha=prepared.source_sha,
        skipped=prepared.skipped,
        warnings=prepared.warnings,
    )


def build_variants(
    config: Configuration,
    *,
    files: Mapping[str, str | os.PathLike[str]] | None = None,
    out_dir: str | os.PathLike[str] | None = None,
) -> list[Path]:
    """Build both variants and return their paths -- the card's interface.

    Reading variant first, then UI, each in `STYLES` order, so the list is deterministic
    and position-addressable. For the key, the family names and the styles that were
    built, call `build_variant_set` instead; this is a wrapper over it.

    The caller owns the temporary directory the paths live in. `build_variant_set`
    returns it as ``.directory``; from this function it is ``paths[0].parent``.
    """
    return list(build_variant_set(config, files=files, out_dir=out_dir).files)
