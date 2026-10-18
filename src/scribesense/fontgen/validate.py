"""M1.6 -- Generated-font validation (owner O1).

    validate_font(paths, config) -> ValidationResult(ok, measurements, reason_code)

Checks that a *built but not yet installed* font really carries the spacing it was
asked for, before M1.8 is allowed to call it a success and M1.7 is allowed to install
it. A file existing is not evidence that it was patched: Research2 G.6 is the reason
this module gates installation rather than merely reporting --

    "activation makes the artifact visible to the whole system through fontconfig. An
    unvalidated artifact that resolves under a colliding family name could be
    substituted into unrelated applications, and a partially patched artifact would
    render inconsistently while being indistinguishable by hash from a correct one."

Two kinds of check, and the difference matters
----------------------------------------------
**Structural** checks read the artifact back with fontTools and compare integers
against the source font. They involve no measurement, so Research2 G.4 gives them a
tolerance of exactly zero, and they need no external tool -- they run identically on
Windows and on Fedora, with or without a display server.

**Rendering** checks drive ``pango-view`` and ``fc-match`` through a *private*
fontconfig configuration. They are the only way to answer two questions fontTools
cannot: does this family resolve to *this file* rather than to some system font, and
does the shaped, rasterised text actually come out the predicted width. They need the
real tools, and when those are missing the result is a failure with
`VALIDATION_TOOL_MISSING` -- never a pass. A validation that quietly skipped half of
itself would be worse than no validation, because M1.8 would install on the strength
of it.

What this module does **not** claim
-----------------------------------
Research2 G.2, verbatim and deliberately quoted here rather than paraphrased: a passing
suite verifies "that the artifact is structurally valid; that the family resolves to
this artifact and not to a system font; that the configured metrics are present and
exactly as computed; that non-Latin advances are untouched; that the font renders; and
that no content is lost at 200% scale". It does **not** verify "that the font improves
reading speed, accuracy, or comprehension ... A passing validation suite is not evidence
of dyslexia-related reading benefit, and no automated rendering test may be presented
as such."

Isolation -- the card's must-never
----------------------------------
"Must never: use the user's real fontconfig." Every subprocess is given a
``FONTCONFIG_FILE`` pointing at a configuration this module wrote into its own
temporary directory, listing only the directories holding the artifacts under test. No
``<include>`` of ``/etc/fonts`` or ``~/.config/fontconfig``; ``XDG_CACHE_HOME`` is
redirected too, so fontconfig cannot write a cache into the user's home either. Tindall
2018 is the cited reason the isolation has to be deliberate: fontconfig's cascade will
substitute a font the caller did not ask for, and the only reliable way to stop it is a
configuration that never mentions the system's.

Platform. fontTools and the stdlib for the structural half; ``pango-view`` and
``fc-match`` for the rendering half. ``pango-view`` renders to a PNG through cairo's
image surface with ``-q``, so it needs no display server -- the rendering checks are
Wayland-compatible because they never touch a display at all. The rendering half runs for
real on Fedora/Ubuntu in the bubblewrap sandbox (tests/unit/fontgen/test_fontgen_integration.py)
and in O3's probes; unit tests use an injected renderer. `RENDER_TOLERANCE_PX_PER_GLYPH` was
first tuned on synthetic fonts — re-checked against the bundled fonts (O3's M1.6 review).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from xml.sax.saxutils import escape

from fontTools.ttLib import TTFont

from scribesense.config.rules import latin_glyphs, open_font
from scribesense.config.settings import Configuration, validate
from scribesense.contracts.errors import ScribeSenseError
from scribesense.contracts.paths import run_cmd
from scribesense.fontgen.naming import fc_pattern
from scribesense.fontgen.patcher import (
    LIGATURE_FEATURES,
    SPACE_CODEPOINT,
    USE_TYPO_METRICS,
    letter_delta_units,
    line_gap_units,
    space_advance,
)
from scribesense.fontgen.variants import (
    REQUIRED_STYLE,
    STYLES,
    BundledFontMissing,
    base_font_files,
    variant_line,
    variant_of,
)

__all__ = [
    "FC_MATCH",
    "PANGO_VIEW",
    "REFERENCE_DPI",
    "REFERENCE_SIZE_PT",
    "REFERENCE_TEXT",
    "RENDER_TOLERANCE_PX_PER_GLYPH",
    "SCALE_FACTOR",
    "VALIDATION_FAILED",
    "PangoRenderer",
    "RenderFailed",
    "RenderedSize",
    "Renderer",
    "ValidationResult",
    "validate_font",
]


# --------------------------------------------------------------------------- #
# Reason codes
# --------------------------------------------------------------------------- #

VALIDATION_FAILED = "VALIDATION_FAILED"
"""A rendered measurement is outside tolerance -- the card's named failure.

The card's failure row is "out of tolerance -> ``VALIDATION_FAILED``", and that is
exactly the case this code covers: a *measured* quantity disagreeing with the analytic
prediction by more than the tolerance this module owns. Research2 G.5's vocabulary has
no code for it, because G.5 enumerates the structural failures; the card's code fills
the gap, so the two sets compose without overlapping.
"""

VALIDATION_MISSING_FILE = "VALIDATION_MISSING_FILE"
VALIDATION_PARSE_FAILED = "VALIDATION_PARSE_FAILED"
VALIDATION_MISSING_REGULAR = "VALIDATION_MISSING_REGULAR"
VALIDATION_UNKNOWN_STYLE = "VALIDATION_UNKNOWN_STYLE"
VALIDATION_KEY_MISMATCH = "VALIDATION_KEY_MISMATCH"
VALIDATION_FAMILY_COLLISION = "VALIDATION_FAMILY_COLLISION"
VALIDATION_SOURCE_UNAVAILABLE = "VALIDATION_SOURCE_UNAVAILABLE"
VALIDATION_ADVANCE_MISMATCH = "VALIDATION_ADVANCE_MISMATCH"
VALIDATION_NON_LATIN_MODIFIED = "VALIDATION_NON_LATIN_MODIFIED"
VALIDATION_LINE_METRIC_MISMATCH = "VALIDATION_LINE_METRIC_MISMATCH"
VALIDATION_LIGATURES_PRESENT = "VALIDATION_LIGATURES_PRESENT"
VALIDATION_TOOL_MISSING = "VALIDATION_TOOL_MISSING"
VALIDATION_RESOLUTION_MISMATCH = "VALIDATION_RESOLUTION_MISMATCH"
VALIDATION_RENDER_FAILED = "VALIDATION_RENDER_FAILED"
VALIDATION_REFLOW_FAILED = "VALIDATION_REFLOW_FAILED"
VALIDATION_ERROR = "VALIDATION_ERROR"

FAILURE_CODES: tuple[str, ...] = (
    VALIDATION_MISSING_FILE,
    VALIDATION_PARSE_FAILED,
    VALIDATION_UNKNOWN_STYLE,
    VALIDATION_KEY_MISMATCH,
    VALIDATION_FAMILY_COLLISION,
    VALIDATION_MISSING_REGULAR,
    VALIDATION_SOURCE_UNAVAILABLE,
    VALIDATION_ADVANCE_MISMATCH,
    VALIDATION_NON_LATIN_MODIFIED,
    VALIDATION_LINE_METRIC_MISMATCH,
    VALIDATION_LIGATURES_PRESENT,
    VALIDATION_TOOL_MISSING,
    VALIDATION_RESOLUTION_MISMATCH,
    VALIDATION_RENDER_FAILED,
    VALIDATION_FAILED,
    VALIDATION_REFLOW_FAILED,
    VALIDATION_ERROR,
)
"""Every failure code, in the order the checks that produce them run.

The order is contractual, exactly as M1.3's refusal order is: several conditions
co-occur -- an unpatched font fails the advance check *and* would fail the rendered
width check -- and the reported code must be stable for a given set of inputs or it
cannot be used to diagnose anything. The **first** failing check is the one reported,
and checks only run once everything they depend on has passed.

All but `VALIDATION_FAILED` and `VALIDATION_ERROR` come from Research2 G.5, with three
additions argued at their definitions: `VALIDATION_LINE_METRIC_MISMATCH` (G.5 has no
code for the field that distinguishes the reading variant from the UI one),
`VALIDATION_TOOL_MISSING` (G.5 assumes the tools are there) and
`VALIDATION_SOURCE_UNAVAILABLE` (G.1 steps 6-7 compare against the source, and G.5 does
not say what happens when it cannot be read).
"""

W_VALIDATION_KERNING_CHANGED = "W_VALIDATION_KERNING_CHANGED"
"""Kerning pairs were lost. Reported, never thresholded -- Research2 G.4."""

W_VALIDATION_TEMP_LEAK = "W_VALIDATION_TEMP_LEAK"
"""The temporary directory could not be removed. Research2 G.5: "True + warning"."""


# --------------------------------------------------------------------------- #
# Tolerances -- "Data ownership: owns the tolerance values [P]"
# --------------------------------------------------------------------------- #

REFERENCE_TEXT = "The quick brown fox jumps over the lazy dog"
"""The fixed reference corpus (Research2 G.1 step 4, the card's "fixed sample").

A pangram, so every lowercase Latin letter contributes an advance, with eight spaces so
that ``word_scale`` is exercised in the same measurement as ``letter_em``. Fixed as a
constant because the expected width is derived from it: changing the string changes
every stored measurement, which is why it is not a parameter.
"""

REFERENCE_SIZE_PT = 24.0
"""Rendering size. Large enough that per-glyph integer-pixel rounding is a small
fraction of each advance, small enough to stay in the range a reading surface uses."""

REFERENCE_DPI = 96
"""Pinned, because a measurement in pixels is meaningless without it. Passed to
``pango-view`` explicitly so the host's DPI -- which on Wayland is a per-output,
per-session property -- cannot reach the result."""

SCALE_FACTOR = 2.0
"""WCAG 1.4.4: text must resize to 200% without loss of content or functionality
(Boyalakuntla et al. 2021, cited in K1's ``text_scale`` notes). Research2 G.1 step 8."""

RENDER_TOLERANCE_PX_PER_GLYPH = 1.0
MIN_RENDER_TOLERANCE_PX = 2.0
"""Tolerance for rasterisation-derived widths, in pixels per glyph of the sample.

**This is a derived upper bound, not a measurement, and it is the one number in this
module that must be re-derived before release.** Research2 G.4 is explicit that it will
not supply a figure -- "none is derivable from the sources reviewed and inventing one
would be fabrication" -- and prescribes the procedure instead: render the same string
N times with the same artifact *in the target environment*, set the tolerance to the
observed spread, and re-derive it whenever pango, freetype, fontconfig or the DPI
changes. That procedure needs ``pango-view`` on Fedora and has **not** been carried
out; see K7 F7.

What this number is, in the meantime: a renderer that hints metrics rounds each glyph's
advance to a whole pixel, so a string of G glyphs can differ from the analytic sum by up
to G/2 px from rounding alone, and the comparison here is between two such renders (the
artifact and the source), so the bound doubles to G. One pixel per glyph is therefore an
upper bound on rounding error rather than a guess at the observed spread, and it is
deliberately loose: it can only ever admit a font it should have rejected, never reject
a correct one, and the failure it has to catch is three orders of magnitude larger --
an unpatched font is off by ``letter_em x upm`` on *every* glyph, around 25% of the
total width at the default configuration, against a tolerance of about 6%. A test pins
that separation so this constant cannot be tightened or loosened into uselessness
without someone noticing.

Research2 G.4's note on hinting is why the measurement is advisory in character even
when it passes: TrueType instructions can modify advance widths a posteriori
(Haralambous & Plaice 2003), so a rendered width disagreeing with the analytic one "may
indicate a hinting override rather than a patching error". The structural checks, which
admit no tolerance at all, are what actually prove the patch applied; the rendered
measurement proves the font reaches a renderer and comes out roughly the right size.
"""

PANGO_VIEW = "pango-view"
FC_MATCH = "fc-match"
SUBPROCESS_TIMEOUT = 60.0
NON_LATIN_SAMPLE_LIMIT = 8


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ValidationResult:
    """The verdict on one built font set.

    ``ok``
        Whether M1.8 may treat the build as a success and M1.7 may install it. False
        means **never activated** -- the card's failure row.
    ``measurements``
        Everything measured, JSON-serialisable throughout, because the card's
        postcondition is that the result is "recorded on the font registry entry (via
        M1.8 -> M3.1)" and a registry entry has to survive being written to disk. Paths
        are strings, not `Path`s, for the same reason. Research2 left the contents TBD
        ("the contents of measurements require confirmation") and proposed the table in
        G.4; the keys are documented on `validate_font`.
    ``reason_code``
        ``None`` when ``ok``; otherwise the first failing check's code from
        `FAILURE_CODES`.
    ``warnings``
        Non-blocking observations: `W_VALIDATION_KERNING_CHANGED`,
        `W_VALIDATION_TEMP_LEAK`.

    **Contract extension (M1.6).** The card specifies
    ``ValidationResult(ok, measurements, reason_code)``. ``warnings`` is appended with a
    default, so three-argument positional construction is unchanged and no caller is
    forced to adapt -- the same resolution M1.3 used for `Eligibility`, and for the same
    reason: Research2 G.5 has two "True + warning" rows and there is otherwise nowhere
    to put them. Collapsing a warning into ``ok=False`` would refuse fonts the research
    says to accept; dropping it would hide that kerning was lost.
    """

    ok: bool
    measurements: Mapping[str, Any] = field(default_factory=dict)
    reason_code: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.ok and self.reason_code is not None:
            raise ValueError("a passing validation must not carry a reason_code")
        if not self.ok and not self.reason_code:
            raise ValueError("a failed validation must carry a reason_code")


# --------------------------------------------------------------------------- #
# Rendering seam
# --------------------------------------------------------------------------- #


class RenderFailed(Exception):
    """A rendering tool failed, timed out, or produced something unreadable."""


@dataclass(frozen=True)
class RenderedSize:
    """The pixel extent of a rendered sample."""

    width_px: int
    height_px: int


class Renderer(Protocol):
    """The external rendering stack, behind one seam.

    It exists so that the rendering checks can be driven in tests without
    ``pango-view`` -- which cannot be installed on the Windows development machine --
    and so that the subprocess layer is replaceable if the project ever moves off
    pango. It is **not** an escape hatch: `validate_font` asks any renderer for
    `missing_tools` first and fails with `VALIDATION_TOOL_MISSING` if the answer is
    non-empty, so substituting a renderer cannot turn a skipped check into a pass.
    """

    def missing_tools(self) -> tuple[str, ...]:
        """Executables this renderer needs and cannot find."""

    def resolve_family(self, family: str, *, config_file: Path) -> tuple[str, str]:
        """``(resolved family, resolved file path)`` for ``family``, via fontconfig."""

    def measure(
        self,
        *,
        family: str,
        text: str,
        size_pt: float,
        config_file: Path,
        workdir: Path,
    ) -> RenderedSize:
        """Render ``text`` and return the extent of the result."""


class PangoRenderer:
    """`Renderer` driving the real ``pango-view`` and ``fc-match``.

    Both are invoked with an environment whose ``FONTCONFIG_FILE`` is the private
    configuration, so neither can see the user's fonts or their configuration. Output
    goes to a PNG, whose ``IHDR`` gives the extent -- the alternative, parsing
    ``pango-view``'s human-readable annotation output, is not a stable interface.

    ``-q`` keeps ``pango-view`` from opening a window: it renders through cairo's image
    surface, so the whole path is headless and neither Wayland nor X11 is involved.

    **Not executed in development.** ``pango-view`` does not exist on Windows, so this
    class is covered by unit tests of its argument and environment construction only;
    its end-to-end behaviour is unverified. K7 F7 lists what that leaves open.
    """

    def __init__(
        self,
        *,
        pango_view: str | None = None,
        fc_match: str | None = None,
        dpi: int = REFERENCE_DPI,
        timeout: float = SUBPROCESS_TIMEOUT,
    ) -> None:
        self.pango_view = pango_view or shutil.which(PANGO_VIEW)
        self.fc_match = fc_match or shutil.which(FC_MATCH)
        self.dpi = dpi
        self.timeout = timeout
        self._renders = 0

    def missing_tools(self) -> tuple[str, ...]:
        missing = []
        if not self.pango_view:
            missing.append(PANGO_VIEW)
        if not self.fc_match:
            missing.append(FC_MATCH)
        return tuple(missing)

    def environment(self, config_file: Path) -> dict[str, str]:
        """The subprocess environment -- the card's must-never, in one place.

        ``FONTCONFIG_FILE`` is what fontconfig actually reads; with it set, nothing
        else is loaded, because the file this module writes contains no ``<include>``.
        ``FONTCONFIG_PATH`` is redirected as well so that a relative include could not
        reach the system directory, and ``XDG_CACHE_HOME`` so that fontconfig writes
        its cache beside the configuration instead of into the user's home -- "must
        never use the user's real fontconfig" covers writing to it, not only reading.

        The locale is pinned because Pango chooses shaping and fallback by language,
        and a measurement that moved with ``LANG`` would not be comparable between a
        developer's machine and a user's.
        """
        return {**os.environ, **self.overrides(config_file)}

    def overrides(self, config_file: Path) -> dict[str, str]:
        """The variables `environment` changes; what `_run` actually passes."""
        config_file = Path(config_file)
        return {
            "FONTCONFIG_FILE": str(config_file),
            "FONTCONFIG_PATH": str(config_file.parent),
            "XDG_CACHE_HOME": str(config_file.parent / "cache"),
            "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8",
        }

    def _run(self, argv: Sequence[str], config_file: Path) -> str:
        try:
            return run_text(argv, self.overrides(config_file), self.timeout)
        except ScribeSenseError as exc:
            raise RenderFailed(f"{Path(argv[0]).name} failed: {exc.code}") from exc
        except OSError as exc:
            raise RenderFailed(f"{argv[0]} could not be run: {exc}") from exc

    def resolve_family(self, family: str, *, config_file: Path) -> tuple[str, str]:
        """Ask fontconfig which file it would use for ``family``.

        Two values come back, because both can be wrong independently: fontconfig
        *always* substitutes rather than failing, so a missing artifact shows up as a
        different family, while a shadowing system font shows up as the right family at
        the wrong path. Checking only one of them would miss half the failures.

        **Why the caller verifies both rather than trusting the configuration.** The
        only ``fc-match`` available on the development machine -- the one MiKTeX ships
        for Windows -- **ignores ``FONTCONFIG_FILE`` completely**: pointed at a
        nonexistent configuration it neither errors nor changes behaviour, and it
        resolved a generated family to ``C:/Windows/Fonts/verdana.ttf``. That is a
        property of that build rather than of the configuration this module writes, and
        it says nothing about Fedora. But it does establish that isolation can fail
        *silently*, which is why the resolution check compares the returned family and
        the returned path against what was expected instead of assuming the private
        configuration worked. Under the leaky binary above, validation fails with
        `VALIDATION_RESOLUTION_MISMATCH` -- the correct outcome -- rather than
        validating a system font.
        """
        if not self.fc_match:
            raise RenderFailed(f"{FC_MATCH} is not installed")
        out = self._run(
            [self.fc_match, "--format=%{family}\n%{file}\n", fc_pattern(family)], config_file
        )
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        if len(lines) < 2:
            raise RenderFailed(f"{FC_MATCH} gave no family and file for {family!r}")
        return lines[0], lines[1]

    def measure(
        self,
        *,
        family: str,
        text: str,
        size_pt: float,
        config_file: Path,
        workdir: Path,
    ) -> RenderedSize:
        if not self.pango_view:
            raise RenderFailed(f"{PANGO_VIEW} is not installed")
        self._renders += 1
        out_path = Path(workdir) / f"render-{self._renders:03d}.png"
        self._run(
            [
                self.pango_view,
                "-q",
                "--margin=0",
                f"--dpi={self.dpi}",
                f"--font={family} {size_pt:g}",
                f"--text={text}",
                "-o",
                str(out_path),
            ],
            config_file,
        )
        try:
            data = out_path.read_bytes()
        except OSError as exc:
            raise RenderFailed(f"{PANGO_VIEW} wrote no output: {exc}") from exc
        return _png_size(data)


def _png_size(data: bytes) -> RenderedSize:
    """Width and height from a PNG ``IHDR``, without an image library.

    The header is fixed-layout and the first chunk is required by the specification to
    be ``IHDR``, so this is a complete read of what is needed rather than a shortcut.
    """
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise RenderFailed("rendered output is not a PNG")
    return RenderedSize(
        int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    )


def run_text(argv: Sequence[str], overrides: Mapping[str, str], timeout: float) -> str:
    """Run ``argv`` through `run_cmd` (K3 rule 9) with ``overrides`` added to the environment.

    `run_cmd` takes no environment, so the variables go in front as ``env NAME=value ...``;
    the child still inherits everything else. Returns decoded stdout. Every failure --
    missing tool, non-zero exit, timeout -- raises `ScribeSenseError` with run_cmd's code.
    """
    finished = run_cmd(["env", *(f"{k}={v}" for k, v in overrides.items()), *argv], timeout=timeout)
    return finished.stdout.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Private fontconfig
# --------------------------------------------------------------------------- #


def private_fontconfig(directories: Iterable[Path], cache_dir: Path) -> str:
    """A fontconfig configuration that can see **only** ``directories``.

    No ``<include>`` of ``/etc/fonts/conf.d`` and none of ``~/.config/fontconfig``: a
    configuration that mentions neither cannot inherit the user's substitution rules,
    their font directories, or their cache. That is the card's must-never and Research2
    G.3's requirement that resolution "still returns the application artifact path" even
    when a system family of the same name exists -- it does, because no system family is
    visible to resolve to.

    ``<cachedir>`` is inside the temporary directory, so the run leaves nothing behind
    in the user's home even though fontconfig wants to cache what it scans.
    """
    lines = ['<?xml version="1.0"?>', "<fontconfig>"]
    for directory in directories:
        lines.append(f"  <dir>{escape(str(Path(directory).resolve()))}</dir>")
    lines.append(f"  <cachedir>{escape(str(Path(cache_dir).resolve()))}</cachedir>")
    lines.append("</fontconfig>")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Structural inspection
# --------------------------------------------------------------------------- #


@dataclass
class _Artifact:
    """One built file, identified and opened."""

    path: Path
    font: TTFont
    family: str
    kind: str
    key: str
    style: str


def _name(font: TTFont, name_id: int) -> str | None:
    table = font.get("name")
    return table.getDebugName(name_id) if table is not None else None


#: Name records that identify a family to fontconfig / font menus. Copyright, trademark, designer,
#: description and licence records (0, 7-14) may and do mention the source, so they are not checked.
_IDENTIFYING_NAME_IDS = (1, 3, 4, 6, 16, 17, 21, 22, 25)


def _source_name_leak(source: TTFont, artifact: TTFont) -> int | None:
    """The first identifying name record of ``artifact`` that still names the source family, or None."""
    names = set()
    for name_id in (1, 16, 21, 25):  # family, typographic family, WWS family, variations PS prefix
        value = _name(source, name_id)
        if value:
            names |= {value.casefold(), value.replace(" ", "").casefold()}
    table = artifact.get("name")
    if table is None or not names:
        return None
    for record in table.names:
        if record.nameID in _IDENTIFYING_NAME_IDS:
            text = record.toUnicode().casefold()
            if any(n and n in text for n in names):
                return record.nameID
    return None


def _style_from_filename(path: Path, family: str) -> str | None:
    """``AccessSans-<key>-Bold.ttf`` -> ``Bold``, using M1.5's own file naming."""
    stem = path.stem
    prefix = f"{family}-"
    if not stem.startswith(prefix):
        return None
    style = stem[len(prefix):]
    return style if style in STYLES else None


def _kerning_pairs(font: TTFont) -> int:
    """How many kerning pairs the font carries, legacy ``kern`` plus GPOS.

    A count, not a pair list: Research2 E.10 asks for a diff and G.4 says it is
    "reported, not thresholded", so the cheap aggregate is what the measurement needs.
    M1.4 touches neither table, so any change is a surprise worth surfacing.
    """
    total = 0
    kern = font.get("kern")
    if kern is not None:
        for table in getattr(kern, "kernTables", []):
            total += len(getattr(table, "kernTable", {}) or {})

    gpos = font.get("GPOS")
    table = getattr(gpos, "table", None)
    lookups = getattr(getattr(table, "LookupList", None), "Lookup", None) or []
    for lookup in lookups:
        for sub in getattr(lookup, "SubTable", []) or []:
            if getattr(sub, "LookupType", None) == 2 or hasattr(sub, "PairSet"):
                for pair_set in getattr(sub, "PairSet", []) or []:
                    total += getattr(pair_set, "PairValueCount", 0)
    return total


def _has_feature(font: TTFont, tags: Iterable[str]) -> bool:
    gpos_or_gsub = font.get("GSUB")
    table = getattr(gpos_or_gsub, "table", None)
    feature_list = getattr(table, "FeatureList", None)
    if feature_list is None:
        return False
    wanted = set(tags)
    return any(
        record.FeatureTag in wanted for record in feature_list.FeatureRecord or []
    )


def _non_latin_sample(font: TTFont, latin: set[str]) -> str:
    """A short, deterministic string of non-Latin characters the font actually covers.

    Derived from the font rather than hard-coded, so the check works for any base
    family: a hard-coded Devanagari sample would silently degenerate to ``.notdef``
    boxes in a font without Devanagari and prove nothing. Codepoints are sorted, so the
    sample -- and therefore the measurement -- cannot depend on dict order.
    """
    try:
        cmap = font.getBestCmap()
    except Exception:
        return ""
    sample = []
    for codepoint in sorted(cmap):
        if len(sample) >= NON_LATIN_SAMPLE_LIMIT:
            break
        if cmap[codepoint] in latin or codepoint == SPACE_CODEPOINT:
            continue
        character = chr(codepoint)
        category = unicodedata.category(character)
        if category[0] in "ZC" or category == "Mn":
            # Whitespace, control and combining marks render to nothing measurable.
            continue
        sample.append(character)
    return "".join(sample)


# --------------------------------------------------------------------------- #
# The card's interface
# --------------------------------------------------------------------------- #


def validate_font(
    paths: Sequence[str | os.PathLike[str]],
    config: Configuration,
    *,
    sources: Mapping[str, str | os.PathLike[str]] | None = None,
    renderer: Renderer | None = None,
) -> ValidationResult:
    """Validate the built artifacts in ``paths`` against ``config``.

    ``paths``
        The files M1.5 produced -- `fontgen.variants.build_variants`' return value.
        They must still be in their temporary directory: the card's precondition is
        "fonts in a temporary directory, not installed", and Research2 G.6 explains
        that validating after installation would be validating something the whole
        system can already see.
    ``config``
        The same validated K1 `Configuration` the fonts were built from. An invalid one
        raises ``ValueError`` rather than returning a failure, matching M1.4 `patch` and
        M1.5 `content_key`: a caller error is not a property of the font.
    ``sources``
        The base fonts the artifacts were patched from, ``style -> path``. Keyword-only
        with a default of `base_font_files(config.base_family)`. **Required in
        substance, not optional:** Research2 G.1 steps 6-7 compare the artifact against
        the source, and the expected Latin advance is ``source advance + delta`` -- it
        cannot be predicted from the configuration alone. M1.8 has the mapping already
        and should pass it; the default exists so the card's two-argument call works.
    ``renderer``
        The rendering stack, defaulting to `PangoRenderer`. See `Renderer`.

    Checks run in `FAILURE_CODES` order and the **first** failure is reported, because
    later checks depend on earlier ones -- there is no point measuring a rendered width
    for a file that did not parse, and an unpatched font would fail both the advance
    check and the width check with the second code being the less informative one.

    ``measurements`` keys, which are what reaches the registry through M1.8 and M3.1:

    ``config``, ``key``, ``families``, ``styles``
        What was validated, and the identity it was validated under.
    ``checks``
        One ``{"name", "ok", "code", "detail"}`` entry per check, in run order,
        including the ones that passed. A registry entry that records only the failure
        cannot later answer "was the kerning diff ever looked at".
    ``artifacts``
        Per file: path, family, variant, style, units per em, the expected and observed
        Latin delta and space advance, both line gaps, and the kerning pair counts.
    ``render``
        Tool paths, the reference text, size and DPI, every measured extent, the
        expected and measured width ratios, the tolerance used, and the 200% comparison.
        ``available: false`` with ``missing_tools`` when the tools are absent.

    Returns, never raises, for anything that is a property of the fonts or the tools.
    The temporary directory is removed on every exit path including an exception or a
    ``KeyboardInterrupt`` (Research2 G.1 step 10); a removal that fails is reported as
    `W_VALIDATION_TEMP_LEAK` rather than being allowed to turn a good result bad.
    """
    errors = [issue for issue in validate(config) if issue.level == "error"]
    if errors:
        detail = ", ".join(f"{i.field}={i.code}" for i in errors)
        raise ValueError(f"validate_font() requires a validated Configuration: {detail}")

    measurements: dict[str, Any] = {
        "config": {
            "base_family": config.base_family,
            "letter_em": config.letter_em,
            "word_scale": config.word_scale,
            "reading_line": config.reading_line,
            "ui_line": config.ui_line,
        },
        "checks": [],
        "artifacts": [],
        "render": {"available": False},
    }
    warnings: list[str] = []
    opened: list[TTFont] = []
    workdir = Path(tempfile.mkdtemp(prefix="scribesense-validate-"))

    try:
        code = _run_checks(
            [Path(path) for path in paths],
            config,
            sources,
            renderer if renderer is not None else PangoRenderer(),
            measurements,
            warnings,
            opened,
            workdir,
        )
    except Exception as exc:  # noqa: BLE001 -- see below
        # Deliberately broad, and deliberately a *failure*. fontTools raises anything
        # from struct.error to AssertionError on a damaged font, and the one outcome
        # this module may never produce is an exception escaping to M1.8, which would
        # leave the build in neither the "validated" nor the "refused" state. A bug
        # here is reported as a bug (VALIDATION_ERROR), not as a pass.
        code = VALIDATION_ERROR
        _record(measurements, "internal", False, VALIDATION_ERROR, repr(exc))
    finally:
        for font in opened:
            try:
                font.close()
            except Exception:
                pass
        try:
            shutil.rmtree(workdir)
        except OSError as exc:
            warnings.append(W_VALIDATION_TEMP_LEAK)
            _record(measurements, "cleanup", True, None, f"{workdir}: {exc}")
        measurements["temp_dir_removed"] = not workdir.exists()

    return ValidationResult(
        ok=code is None,
        measurements=measurements,
        reason_code=code,
        warnings=tuple(warnings),
    )


def _record(
    measurements: dict[str, Any],
    name: str,
    ok: bool,
    code: str | None,
    detail: str = "",
) -> str | None:
    measurements["checks"].append(
        {"name": name, "ok": ok, "code": code, "detail": detail}
    )
    return code


def _run_checks(
    paths: list[Path],
    config: Configuration,
    sources: Mapping[str, str | os.PathLike[str]] | None,
    renderer: Renderer,
    measurements: dict[str, Any],
    warnings: list[str],
    opened: list[TTFont],
    workdir: Path,
) -> str | None:
    """Every check, in order. Returns the first failing code, or ``None``."""
    artifacts, code = _identify(paths, measurements, opened)
    if code is not None:
        return code

    source_fonts, code = _open_sources(artifacts, config, sources, measurements, opened)
    if code is not None:
        return code

    for artifact in artifacts:
        code = _check_structure(
            artifact, source_fonts[artifact.style], config, measurements, warnings
        )
        if code is not None:
            return code

    return _check_rendering(
        artifacts, source_fonts, config, renderer, measurements, workdir
    )


def _identify(
    paths: list[Path], measurements: dict[str, Any], opened: list[TTFont]
) -> tuple[list[_Artifact], str | None]:
    """Open each file and work out which variant and style it is."""
    if not paths:
        return [], _record(
            measurements, "inputs", False, VALIDATION_MISSING_FILE, "no paths given"
        )

    for path in paths:
        if not path.is_file():
            return [], _record(
                measurements, "inputs", False, VALIDATION_MISSING_FILE, str(path)
            )
    _record(measurements, "inputs", True, None, f"{len(paths)} file(s)")

    artifacts: list[_Artifact] = []
    for path in paths:
        try:
            font = open_font(path.read_bytes())
            opened.append(font)
            font.getGlyphOrder()
            font["hmtx"]
        except Exception as exc:
            return [], _record(
                measurements, "parse", False, VALIDATION_PARSE_FAILED,
                f"{path.name}: {exc!r}",
            )

        family = _name(font, 1) or ""
        parsed = variant_of(family)
        if parsed is None:
            # Either the patch left the source family in place -- the must-never M1.4
            # and M1.5 both carry -- or someone renamed the file's family by hand.
            # Either way it can collide with a system font, which is G.5's collision.
            return [], _record(
                measurements, "family", False, VALIDATION_FAMILY_COLLISION,
                f"{path.name}: {family!r} is not a generated family name",
            )
        kind, key = parsed

        style = _style_from_filename(path, family)
        if style is None:
            return [], _record(
                measurements, "style", False, VALIDATION_UNKNOWN_STYLE,
                f"{path.name}: no known style for family {family!r}",
            )
        artifacts.append(_Artifact(path, font, family, kind, key, style))

    keys = {artifact.key for artifact in artifacts}
    if len(keys) != 1:
        return [], _record(
            measurements, "key", False, VALIDATION_KEY_MISMATCH,
            f"artifacts carry {len(keys)} different keys: {sorted(keys)}",
        )
    _record(measurements, "key", True, None, keys.pop())

    families: dict[str, list[str]] = {}
    for artifact in artifacts:
        families.setdefault(artifact.family, []).append(artifact.style)
    for family, styles in sorted(families.items()):
        if REQUIRED_STYLE not in styles:
            return [], _record(
                measurements, "styles", False, VALIDATION_MISSING_REGULAR,
                f"{family} has {sorted(styles)}",
            )
    _record(measurements, "styles", True, None, "Regular present in every family")

    measurements["key"] = artifacts[0].key
    measurements["families"] = {
        artifact.kind: artifact.family for artifact in artifacts
    }
    measurements["styles"] = sorted({artifact.style for artifact in artifacts})
    return artifacts, None


def _open_sources(
    artifacts: list[_Artifact],
    config: Configuration,
    sources: Mapping[str, str | os.PathLike[str]] | None,
    measurements: dict[str, Any],
    opened: list[TTFont],
) -> tuple[dict[str, TTFont], str | None]:
    """Open the base font behind each style, or explain why it cannot be done."""
    if sources is None:
        try:
            sources = base_font_files(config.base_family)
        except BundledFontMissing as exc:
            return {}, _record(
                measurements, "sources", False, VALIDATION_SOURCE_UNAVAILABLE, str(exc)
            )

    fonts: dict[str, TTFont] = {}
    for style in sorted({artifact.style for artifact in artifacts}):
        raw = sources.get(style)
        if raw is None:
            return {}, _record(
                measurements, "sources", False, VALIDATION_SOURCE_UNAVAILABLE,
                f"no source font for style {style!r}",
            )
        try:
            font = open_font(Path(raw).read_bytes())
            opened.append(font)
            font.getGlyphOrder()
            font["hmtx"]
        except Exception as exc:
            return {}, _record(
                measurements, "sources", False, VALIDATION_SOURCE_UNAVAILABLE,
                f"{raw}: {exc!r}",
            )
        fonts[style] = font

    measurements["source_files"] = {
        style: str(sources[style]) for style in sorted(fonts)
    }
    _record(measurements, "sources", True, None, f"{len(fonts)} source font(s)")
    return fonts, None


def _check_structure(
    artifact: _Artifact,
    source: TTFont,
    config: Configuration,
    measurements: dict[str, Any],
    warnings: list[str],
) -> str | None:
    """The zero-tolerance half: integers read back and compared with the source.

    Research2 G.4 gives every one of these a tolerance of exactly 0 -- "integer design
    unit assertions involve no measurement and therefore admit no tolerance" -- and
    they are what actually prove the patch applied. The rendering checks that follow
    prove the artifact reaches a renderer; these prove it is correct.
    """
    name = artifact.path.name
    source_hmtx = source["hmtx"]
    artifact_hmtx = artifact.font["hmtx"]

    upm = artifact.font["head"].unitsPerEm
    source_upm = source["head"].unitsPerEm
    if upm != source_upm:
        return _record(
            measurements, f"upm:{name}", False, VALIDATION_ADVANCE_MISMATCH,
            f"units per em changed: {source_upm} -> {upm}",
        )

    delta = letter_delta_units(config.letter_em, upm)
    latin = latin_glyphs(source)
    observed_deltas: set[int] = set()
    for glyph in sorted(latin):
        try:
            expected = max(0, source_hmtx[glyph][0] + delta)
            actual = artifact_hmtx[glyph][0]
        except KeyError as exc:
            return _record(
                measurements, f"advances:{name}", False, VALIDATION_ADVANCE_MISMATCH,
                f"glyph missing from the artifact: {exc}",
            )
        if actual != expected:
            return _record(
                measurements, f"advances:{name}", False, VALIDATION_ADVANCE_MISMATCH,
                f"{glyph}: expected {expected}, found {actual}",
            )
        observed_deltas.add(actual - source_hmtx[glyph][0])
    _record(measurements, f"advances:{name}", True, None, f"{len(latin)} Latin glyphs")

    space = source.getBestCmap().get(SPACE_CODEPOINT)
    expected_space = (
        max(0, space_advance(source_hmtx[space][0], config.word_scale))
        if space
        else None
    )
    actual_space = artifact_hmtx[space][0] if space else None
    if space and actual_space != expected_space:
        return _record(
            measurements, f"space:{name}", False, VALIDATION_ADVANCE_MISMATCH,
            f"space advance: expected {expected_space}, found {actual_space}",
        )
    _record(measurements, f"space:{name}", True, None, str(actual_space))

    # Research2 D.5's hard invariant: every advance outside the Latin set is untouched.
    for glyph in sorted(set(source.getGlyphOrder()) - latin - {space}):
        try:
            if artifact_hmtx[glyph][0] != source_hmtx[glyph][0]:
                return _record(
                    measurements, f"non_latin:{name}", False,
                    VALIDATION_NON_LATIN_MODIFIED,
                    f"{glyph}: {source_hmtx[glyph][0]} -> {artifact_hmtx[glyph][0]}",
                )
        except KeyError as exc:
            return _record(
                measurements, f"non_latin:{name}", False,
                VALIDATION_NON_LATIN_MODIFIED, f"glyph missing: {exc}",
            )
    _record(measurements, f"non_latin:{name}", True, None, "unchanged")

    line = variant_line(config, artifact.kind)
    hhea_source, hhea_artifact = source["hhea"], artifact.font["hhea"]
    expected_gap = line_gap_units(line, upm, hhea_source.ascent, hhea_source.descent)
    if hhea_artifact.lineGap != expected_gap:
        return _record(
            measurements, f"line:{name}", False, VALIDATION_LINE_METRIC_MISMATCH,
            f"hhea.lineGap for {artifact.kind} line {line}: "
            f"expected {expected_gap}, found {hhea_artifact.lineGap}",
        )
    if hhea_artifact.ascent != hhea_source.ascent or (
        hhea_artifact.descent != hhea_source.descent
    ):
        return _record(
            measurements, f"line:{name}", False, VALIDATION_LINE_METRIC_MISMATCH,
            "ascender or descender moved",
        )

    os2_source, os2_artifact = source.get("OS/2"), artifact.font.get("OS/2")
    typo_gap = None
    if os2_source is not None and os2_artifact is not None:
        typo_gap = line_gap_units(
            line, upm, os2_source.sTypoAscender, os2_source.sTypoDescender
        )
        if os2_artifact.sTypoLineGap != typo_gap:
            return _record(
                measurements, f"line:{name}", False, VALIDATION_LINE_METRIC_MISMATCH,
                f"OS/2.sTypoLineGap: expected {typo_gap}, "
                f"found {os2_artifact.sTypoLineGap}",
            )
        typo_bit = bool(os2_artifact.fsSelection & USE_TYPO_METRICS)
        if os2_artifact.version >= 4 and not typo_bit:
            # Without the bit set, the sTypo* edit above is invisible to most
            # renderers, so the line height would silently not apply.
            return _record(
                measurements, f"line:{name}", False, VALIDATION_LINE_METRIC_MISMATCH,
                "USE_TYPO_METRICS is not set",
            )
        if os2_artifact.version < 4 and typo_bit:
            # M1.4 raises v2/v3 to 4 and never sets the bit below that: an undefined bit.
            return _record(
                measurements, f"line:{name}", False, VALIDATION_LINE_METRIC_MISMATCH,
                f"USE_TYPO_METRICS set on OS/2 version {os2_artifact.version}",
            )
    _record(measurements, f"line:{name}", True, None, f"lineGap {expected_gap}")

    if _has_feature(artifact.font, LIGATURE_FEATURES):
        return _record(
            measurements, f"ligatures:{name}", False, VALIDATION_LIGATURES_PRESENT,
            f"{sorted(LIGATURE_FEATURES)} still present in GSUB",
        )
    _record(measurements, f"ligatures:{name}", True, None, "removed")

    source_family = _name(source, 1)
    if artifact.family == source_family or _name(artifact.font, 16) is not None:
        return _record(
            measurements, f"naming:{name}", False, VALIDATION_FAMILY_COLLISION,
            f"artifact family {artifact.family!r} vs source {source_family!r}",
        )
    leaked = _source_name_leak(source, artifact.font)
    if leaked is not None:
        # O3's M1.6 review: a source family left in e.g. record 21 (WWS) makes fontconfig list
        # the generated font under the SOURCE name too — refused here explicitly, not only by
        # the rendering check by luck.
        return _record(
            measurements, f"naming:{name}", False, VALIDATION_FAMILY_COLLISION,
            f"name record {leaked} still carries the source family",
        )
    _record(measurements, f"naming:{name}", True, None, artifact.family)

    source_pairs = _kerning_pairs(source)
    artifact_pairs = _kerning_pairs(artifact.font)
    if artifact_pairs < source_pairs and W_VALIDATION_KERNING_CHANGED not in warnings:
        # Reported, never thresholded (Research2 G.4/E.10). M1.4 does not touch kerning,
        # so this would mean the ligature re-indexing reached further than intended --
        # worth surfacing, not worth refusing a font over.
        warnings.append(W_VALIDATION_KERNING_CHANGED)

    measurements["artifacts"].append(
        {
            "path": str(artifact.path),
            "family": artifact.family,
            "variant": artifact.kind,
            "style": artifact.style,
            "units_per_em": upm,
            "letter_delta_expected": delta,
            "letter_delta_observed": sorted(observed_deltas),
            "latin_glyphs": len(latin),
            "space_advance_expected": expected_space,
            "space_advance_observed": actual_space,
            "line_ratio": line,
            "hhea_line_gap": hhea_artifact.lineGap,
            "os2_typo_line_gap": typo_gap,
            "kerning_pairs_source": source_pairs,
            "kerning_pairs_artifact": artifact_pairs,
        }
    )
    return None


def _check_rendering(
    artifacts: list[_Artifact],
    source_fonts: Mapping[str, TTFont],
    config: Configuration,
    renderer: Renderer,
    measurements: dict[str, Any],
    workdir: Path,
) -> str | None:
    """The rendering half: private fontconfig, resolution, extent, 200% scale.

    Two configurations are written, not one. G.1 step 2 requires a configuration
    containing "ONLY the application font directory for this key", and the reference
    measurement is a *ratio* against the source font (the card's "expected width ratio
    vs the base font"), which has to be rendered too. Giving the source its own
    configuration keeps the artifact's isolation claim exactly as stated -- the
    resolution check runs against a configuration in which no other font exists at all.
    """
    render: dict[str, Any] = measurements["render"]
    missing = tuple(renderer.missing_tools())
    if missing:
        render["available"] = False
        render["missing_tools"] = list(missing)
        # Not a skip. The rendering checks are part of the contract, so a build whose
        # rendering could not be verified is not a validated build -- M1.8 must refuse
        # it rather than install on the strength of the structural half alone.
        return _record(
            measurements, "tools", False, VALIDATION_TOOL_MISSING,
            f"not installed: {', '.join(missing)}",
        )
    _record(measurements, "tools", True, None, "pango-view and fc-match found")
    render["available"] = True

    artifact_dirs = sorted({artifact.path.parent for artifact in artifacts})
    source_dirs = sorted(
        {Path(path).parent for path in measurements.get("source_files", {}).values()}
    )
    artifact_config = workdir / "artifact-fonts.conf"
    source_config = workdir / "source-fonts.conf"
    artifact_config.write_text(
        private_fontconfig(artifact_dirs, workdir / "cache"), encoding="utf-8"
    )
    source_config.write_text(
        private_fontconfig(source_dirs, workdir / "cache"), encoding="utf-8"
    )
    render["fontconfig"] = {
        "artifact_config": str(artifact_config),
        "source_config": str(source_config),
        "artifact_dirs": [str(path) for path in artifact_dirs],
    }

    regulars = [a for a in artifacts if a.style == REQUIRED_STYLE]
    try:
        resolutions = {}
        for artifact in regulars:
            family, path = renderer.resolve_family(
                artifact.family, config_file=artifact_config
            )
            resolutions[artifact.family] = {"family": family, "file": path}
            if family != artifact.family:
                # fontconfig substitutes rather than failing, so a wrong family here
                # means the artifact was not found at all.
                return _record(
                    measurements, "resolution", False, VALIDATION_RESOLUTION_MISMATCH,
                    f"{artifact.family!r} resolved to family {family!r}",
                )
            if not _same_file(Path(path), artifact.path):
                return _record(
                    measurements, "resolution", False, VALIDATION_RESOLUTION_MISMATCH,
                    f"{artifact.family!r} resolved to {path!r}, "
                    f"expected {str(artifact.path)!r}",
                )
        render["resolution"] = resolutions
        _record(
            measurements, "resolution", True, None,
            f"{len(resolutions)} family/families resolved to their own files",
        )

        source_font = source_fonts[REQUIRED_STYLE]
        source_family = _name(source_font, 1) or ""
        source_size = renderer.measure(
            family=source_family,
            text=REFERENCE_TEXT,
            size_pt=REFERENCE_SIZE_PT,
            config_file=source_config,
            workdir=workdir,
        )
        measured: dict[str, Any] = {}
        for artifact in regulars:
            measured[artifact.family] = renderer.measure(
                family=artifact.family,
                text=REFERENCE_TEXT,
                size_pt=REFERENCE_SIZE_PT,
                config_file=artifact_config,
                workdir=workdir,
            )
        scaled = {
            artifact.family: renderer.measure(
                family=artifact.family,
                text=REFERENCE_TEXT,
                size_pt=REFERENCE_SIZE_PT * SCALE_FACTOR,
                config_file=artifact_config,
                workdir=workdir,
            )
            for artifact in regulars
        }
        non_latin_text = _non_latin_sample(source_font, latin_glyphs(source_font))
        non_latin: dict[str, RenderedSize] = {}
        if non_latin_text:
            non_latin["source"] = renderer.measure(
                family=source_family,
                text=non_latin_text,
                size_pt=REFERENCE_SIZE_PT,
                config_file=source_config,
                workdir=workdir,
            )
            for artifact in regulars:
                non_latin[artifact.family] = renderer.measure(
                    family=artifact.family,
                    text=non_latin_text,
                    size_pt=REFERENCE_SIZE_PT,
                    config_file=artifact_config,
                    workdir=workdir,
                )
    except RenderFailed as exc:
        return _record(
            measurements, "render", False, VALIDATION_RENDER_FAILED, str(exc)
        )

    expected_ratio = _advance_ratio(source_font, config, REFERENCE_TEXT)
    tolerance = _tolerance_px(len(REFERENCE_TEXT))
    render.update(
        {
            "reference_text": REFERENCE_TEXT,
            "size_pt": REFERENCE_SIZE_PT,
            "dpi": REFERENCE_DPI,
            "scale_factor": SCALE_FACTOR,
            "tolerance_px": tolerance,
            "source_width_px": source_size.width_px,
            "expected_width_ratio": expected_ratio,
            "artifact_width_px": {
                family: size.width_px for family, size in measured.items()
            },
            "scaled_width_px": {
                family: size.width_px for family, size in scaled.items()
            },
            "non_latin_text": non_latin_text,
            "non_latin_width_px": {
                family: size.width_px for family, size in non_latin.items()
            },
        }
    )

    _record(measurements, "render", True, None, f"{1 + len(measured) * 2} render(s)")

    for family, size in measured.items():
        expected_px = source_size.width_px * expected_ratio
        render.setdefault("measured_width_ratio", {})[family] = (
            size.width_px / source_size.width_px if source_size.width_px else None
        )
        if abs(size.width_px - expected_px) > tolerance:
            return _record(
                measurements, f"width:{family}", False, VALIDATION_FAILED,
                f"rendered width {size.width_px}px, expected "
                f"{expected_px:.1f}px +/- {tolerance:.1f}px "
                f"(source {source_size.width_px}px x ratio {expected_ratio:.4f})",
            )
        _record(measurements, f"width:{family}", True, None, f"{size.width_px}px")

    for family, size in scaled.items():
        expected_px = measured[family].width_px * SCALE_FACTOR
        if abs(size.width_px - expected_px) > _tolerance_px(
            len(REFERENCE_TEXT), SCALE_FACTOR
        ):
            # Content lost or clipped at the WCAG target scale: a layout that dropped
            # or truncated glyphs would not grow in proportion. A proxy for G.1 step 8,
            # not a full overlap analysis -- see K7 F7.
            return _record(
                measurements, f"reflow:{family}", False, VALIDATION_REFLOW_FAILED,
                f"at {SCALE_FACTOR:g}x the width is {size.width_px}px, "
                f"expected about {expected_px:.1f}px",
            )
        _record(measurements, f"reflow:{family}", True, None, f"{size.width_px}px")

    for family, size in non_latin.items():
        if family == "source":
            continue
        if size.width_px != non_latin["source"].width_px:
            return _record(
                measurements, f"non_latin_render:{family}", False,
                VALIDATION_NON_LATIN_MODIFIED,
                f"non-Latin sample rendered {size.width_px}px, "
                f"source {non_latin['source'].width_px}px",
            )
        _record(
            measurements, f"non_latin_render:{family}", True, None, f"{size.width_px}px"
        )
    if not non_latin_text:
        _record(
            measurements, "non_latin_render", True, None,
            "skipped: the base font has no non-Latin coverage to compare",
        )
    return None


def _advance_ratio(source: TTFont, config: Configuration, text: str) -> float:
    """Analytic width ratio of the patched sample to the unpatched one.

    Summed from ``hmtx`` in design units, which is exactly what a renderer lays out
    before rounding, so the prediction and the measurement are the same quantity in
    different units. Characters the font cannot map are skipped on both sides.
    """
    cmap = source.getBestCmap()
    hmtx = source["hmtx"]
    latin = latin_glyphs(source)
    delta = letter_delta_units(config.letter_em, source["head"].unitsPerEm)

    before = after = 0
    for character in text:
        glyph = cmap.get(ord(character))
        if glyph is None or glyph not in hmtx.metrics:
            continue
        advance = hmtx[glyph][0]
        before += advance
        if ord(character) == SPACE_CODEPOINT:
            after += max(0, space_advance(advance, config.word_scale))
        elif glyph in latin:
            after += max(0, advance + delta)
        else:
            after += advance
    return after / before if before else 1.0


def _tolerance_px(glyph_count: int, scale: float = 1.0) -> float:
    """The pixel allowance for a rendered width. See `RENDER_TOLERANCE_PX_PER_GLYPH`."""
    return max(
        MIN_RENDER_TOLERANCE_PX,
        glyph_count * RENDER_TOLERANCE_PX_PER_GLYPH * scale,
    )


def _same_file(first: Path, second: Path) -> bool:
    """Whether two paths name the same file, tolerating symlinks and ``/tmp`` aliases."""
    try:
        return os.path.samefile(first, second)
    except OSError:
        return first.resolve() == second.resolve()
