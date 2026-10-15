"""M1.3 -- Font eligibility rules and Latin glyph detection (owner O1).

Decides whether a source font may be patched by M1.4, and identifies which glyphs may
receive Latin spacing changes.

    is_eligible(path) -> Eligibility(ok, reason_code)
    latin_glyphs(font) -> set[str]

This module owns three definitions, per the M1.3 card: **"monospace"**, **"icon
font"**, and **"Latin glyph"**. Nothing else in the codebase should re-derive them.

Two hard rules from the card drive the design:

* **Must never return ok for a monospace or icon font.** Both checks therefore fail
  *safe*: a font that looks monospaced on any one of three independent signals is
  refused. A false refusal costs the user one font from a shortlist; a false
  acceptance ships a broken variant, because uniform-advance fonts are exactly the
  ones where adding tracking destroys the grid the font exists to provide.
* **The decision is deterministic for the same file.** Checks run in a fixed order,
  every set is ordered before it is reduced, and nothing consults the clock, the
  environment, the display server, or the filename.

Platform notes. Inspection is pure `fontTools` plus `pathlib`, so it is identical on
Windows (development) and Fedora/Wayland (target). Eligibility never touches a display
server or a font cache -- a font is eligible or not regardless of whether fontconfig
can see it.

Scope: Latin only
-----------------
Research2 D.5 states O1's scope plainly: findings about Latin letter spacing, word
spacing and letterform distinctiveness "are not generalised to Hindi, Devanagari,
Arabic, CJK, or any other writing system. This is a scope limitation, not a claim that
other scripts are unaffected by typography." Three sourced reasons: complex scripts
need layout rules beyond advance widths (Plaice et al. 2010; Nishikimi et al. 2007); a
Latin-script result does not transfer (Alexeeva et al. 2020 found Cyrillic LexiaD
improved letter-feature extraction but gave *worse* lexical access than PT Sans); and
an Arabic line-spacing result measured in centimetres (Alotaibi 2017) "has no valid em
conversion and must not be converted".

The research then names the enforcement point: "the patcher must assert zero advance
delta for every non-Latin glyph." `latin_glyphs` is what makes that assertion possible,
and it is deliberately conservative -- see its docstring on shared glyphs.

A note on what eligibility does *not* mean
------------------------------------------
Passing these checks means a font is *technically patchable*. It is not a claim that
the font is good for readers with dyslexia. Research2 found no controlled
reading-performance study for any of the four shortlisted families, and found that
purpose-built dyslexia typefaces show no consistent advantage in the better-controlled
literature (Kuster et al. 2017; Wery & Diliberto 2016; Joseph & Powell 2022), with
Marinus et al. 2016's 7% advantage vanishing once spacing was matched. Eligibility is
an engineering gate, nothing more.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.unicodedata import script

__all__ = [
    "BASE_IS_ICON_FONT",
    "BASE_IS_MONOSPACE",
    "CORE_LATIN_CODEPOINTS",
    "Eligibility",
    "FONT_TOO_LARGE",
    "FONT_UNREADABLE",
    "ICON_PUA_SHARE",
    "ITALIC_ONLY",
    "MAX_FONT_BYTES",
    "MIN_CORE_LATIN_RATIO",
    "MISSING_TABLE",
    "MONOSPACE_MIN_SAMPLE",
    "NO_LATIN_COVERAGE",
    "NO_UPM",
    "PARTIAL_LATIN_COVERAGE",
    "REQUIRED_TABLES",
    "VARIABLE_FONT",
    "is_eligible",
    "is_icon_font",
    "is_monospace",
    "latin_glyphs",
    "open_font",
]


# --------------------------------------------------------------------------- #
# Reason codes
# --------------------------------------------------------------------------- #
#
# Naming. The card fixes two codes verbatim -- FONT_UNREADABLE and BASE_IS_MONOSPACE --
# and K7's FontBuildResult quotes "BASE_IS_MONOSPACE" as a reason_code, so the whole
# vocabulary follows that bare SCREAMING_SNAKE style. Research2 D.4 proposes the same
# conditions under an E_*/W_* prefix scheme; that scheme is explicitly labelled "a
# recommendation unless the architecture names them" (Research2 §0.1), and here the
# architecture does. The mapping is recorded so the research table stays traceable:
#
#   Research2 code            this module             note
#   ----------------------    --------------------   ------------------------------
#   E_FORMAT_UNREADABLE       FONT_UNREADABLE        card-named
#   E_CORRUPT                 FONT_UNREADABLE        merged; see FONT_UNREADABLE
#   E_PARSE_TIMEOUT           FONT_TOO_LARGE         partial; see MAX_FONT_BYTES
#   E_MISSING_TABLE           MISSING_TABLE
#   E_NO_UPM                  NO_UPM
#   E_NO_LATIN                NO_LATIN_COVERAGE
#   E_NON_LATIN_ONLY          NO_LATIN_COVERAGE      merged; same observable condition
#   W_PARTIAL_COVERAGE        PARTIAL_LATIN_COVERAGE warning
#   W_ICON_FONT               BASE_IS_ICON_FONT      *escalated to a refusal* -- card
#   W_ITALIC_ONLY             ITALIC_ONLY            warning
#   E_VARIABLE_UNSUPPORTED    VARIABLE_FONT          warning; M1.4 decides -- see below
#   E_LICENCE                 (not implemented)      K2 verifies licences at bundling

FONT_UNREADABLE = "FONT_UNREADABLE"
"""Refusal: the file is missing, is not a font, or cannot be parsed.

Card-named, and the card's required behaviour for "unreadable/corrupt font". Research2
separates ``E_FORMAT_UNREADABLE`` (not a font) from ``E_CORRUPT`` (damage inside an
otherwise valid container). They are merged here because the distinction is not
reliably observable: fontTools raises an assortment of exception types from both
cases, and a truncated file can fail at either the container or the table level
depending on where the truncation landed. Splitting them would mean guessing, and the
caller's response is identical either way. Returning a *code* rather than raising is
what the card requires -- errors are never swallowed and never reported as eligible.
"""

FONT_TOO_LARGE = "FONT_TOO_LARGE"
"""Refusal: the file exceeds `MAX_FONT_BYTES` and was never parsed.

The portable half of Research2's ``E_PARSE_TIMEOUT``. See `MAX_FONT_BYTES` for what
this does and does not protect against.
"""

MISSING_TABLE = "MISSING_TABLE"
"""Refusal: a table in `REQUIRED_TABLES` is absent."""

NO_UPM = "NO_UPM"
"""Refusal: ``head.unitsPerEm`` is absent or <= 0.

Every em conversion downstream divides by it -- M1.4 computes
``delta_units = round(letter_em * unitsPerEm)`` -- so a zero or missing value makes
the whole patch undefined (Research2 D.2, D.6).
"""

BASE_IS_ICON_FONT = "BASE_IS_ICON_FONT"
"""Refusal: the font is an icon/symbol font. See `is_icon_font`."""

BASE_IS_MONOSPACE = "BASE_IS_MONOSPACE"
"""Refusal: the font is monospaced. Card-named. See `is_monospace`."""

NO_LATIN_COVERAGE = "NO_LATIN_COVERAGE"
"""Refusal: too few core Latin letters to be worth patching, or none at all.

Covers both of Research2's ``E_NO_LATIN`` and ``E_NON_LATIN_ONLY``. They are merged
because a non-Latin-only font *is* a font with zero Latin coverage: there is no
additional observation that distinguishes them, and inventing a separate code would
imply a distinction the check cannot actually make.
"""

PARTIAL_LATIN_COVERAGE = "PARTIAL_LATIN_COVERAGE"
"""Warning: a subset font missing some core Latin letters.

Eligible, but Research2 D.3 is explicit that "coverage gaps in subset fonts must be
reported, not silently rendered" -- missing glyphs otherwise surface to the reader as
tofu (Moran & Cysouw 2017).
"""

ITALIC_ONLY = "ITALIC_ONLY"
"""Warning: the face is italic/oblique only.

Eligible, but flagged: Rello & Baeza-Yates 2016 (97 participants, 48 with dyslexia)
found italic among the styles that *underperformed* roman. Research2 classes this as
"evidence-informed" and recommends excluding such a face from the default family
rather than refusing it -- the choice stays with the user and with M1.2's shortlist.
"""

VARIABLE_FONT = "VARIABLE_FONT"
"""Warning: the font has an ``fvar`` axis table.

Not a refusal **here**, deliberately. Research2 D.4 lists variable fonts as
"Depends on policy -- ``E_VARIABLE_UNSUPPORTED`` if it cannot be flattened; otherwise
eligible", and the architecture's policy is to flatten per weight (Research2 E.4,
decision K-30). Whether flattening succeeds is only knowable by attempting
instantiation, which is M1.4's job and needs the target weight. M1.3 reports the fact
and refuses to guess; M1.4 returns ``PATCH_UNSUPPORTED`` with no output artifact if
instantiation fails.
"""


# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #

REQUIRED_TABLES: tuple[str, ...] = ("head", "hhea", "hmtx", "maxp", "cmap", "name")
"""Tables without which the font cannot be patched (Research2 D.2).

``head`` carries ``unitsPerEm``; ``hmtx`` holds the horizontal advances that are the
only thing M1.4 edits; ``hhea`` gives ``numberOfHMetrics``; ``maxp`` the glyph count;
``cmap`` the codepoint-to-glyph mapping `latin_glyphs` depends on; ``name`` the family
identity M1.5 renames.

``OS/2`` and ``post`` are deliberately **not** required. Research2 describes them as
"needed for metric and naming work" rather than minimal, and their absence only costs
two of the three monospace signals, which `is_monospace` already tolerates.
"""

MAX_FONT_BYTES = 32 * 1024 * 1024
"""Size cap applied *before* parsing, as a parse budget.

Research2 D.2/D.6 require a parse budget on security grounds: font parsers crash and
are a documented attack surface (Liang et al. 2017; Endignoux et al. 2016), corrupt
and adversarial font files are a known input class (Park et al. 2026; Kim et al.
2026), and malicious web fonts are demonstrably feasible (Mueller et al. 2019).

**Limitation, stated plainly.** This is a size cap, not the timeout Research2 asks
for. A small but pathological file -- a decompression bomb, or a table whose declared
counts force quadratic work -- can still consume unbounded time inside fontTools. A
real wall-clock budget needs process isolation (parse in a subprocess, kill on
timeout), which is a deployment decision rather than a rule, so it is deferred rather
than faked here. 32 MB comfortably exceeds any Latin-only text face.
"""

MONOSPACE_MIN_SAMPLE = 8
"""Minimum distinct Latin letter glyphs before uniform advances imply monospace.

Without a floor, a two-glyph subset would be "monospaced" by coincidence.
"""

ICON_PUA_SHARE = 0.5
"""Share of cmap entries in a Private Use Area above which a font is an icon font."""

MIN_CORE_LATIN_RATIO = 0.5
"""Fraction of `CORE_LATIN_CODEPOINTS` required to clear `NO_LATIN_COVERAGE`.

Below this a font is not usefully a Latin text face, whether because it is a
non-Latin font or an aggressive subset. Research2 calls this "the configured
threshold" without proposing a number; 0.5 is an engineering choice, not a finding.
"""

CORE_LATIN_CODEPOINTS: frozenset[int] = frozenset(
    list(range(ord("A"), ord("Z") + 1)) + list(range(ord("a"), ord("z") + 1))
)
"""The 52 unaccented ASCII letters -- the yardstick for "is this a Latin text face".

A deliberately minimal probe set. It is **not** the definition of a Latin glyph: that
comes from the Unicode Script property in `latin_glyphs`, which covers accented forms,
Latin Extended, and so on. This set only answers the coverage question.
"""

_PUA_RANGES: tuple[tuple[int, int], ...] = (
    (0xE000, 0xF8FF),  # Basic Multilingual Plane PUA
    (0xF0000, 0xFFFFD),  # Supplementary PUA-A
    (0x100000, 0x10FFFD),  # Supplementary PUA-B
)


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Eligibility:
    """The verdict on one font file.

    ``ok``
        Whether M1.4 may patch this font.
    ``reason_code``
        The refusal code when ``ok`` is False; ``None`` when ``ok`` is True. Exactly
        one refusal is reported -- the first that applies, in the fixed order
        documented on `is_eligible`.
    ``warnings``
        Non-blocking observations about an eligible font, ordered deterministically.
        Always empty when ``ok`` is False.

    **Contract extension (M1.3, 2026-10-14).** The card specifies
    ``Eligibility(ok, reason_code)``. ``warnings`` is added as a third field with a
    default, so the card's two-argument positional construction is unchanged and no
    caller is forced to adapt. It is needed because three of the conditions the
    research requires -- partial coverage, italic-only, variable -- are explicitly
    "True + warning" rows in Research2 D.4, and Research2 D.3 requires coverage gaps
    to be "reported, not silently rendered". With only ``ok`` and ``reason_code``
    there is nowhere to report them, and the alternatives are both worse: refusing
    these fonts would contradict the research, and dropping the observations would
    silently ship a font with known gaps.
    """

    ok: bool
    reason_code: str | None = None
    warnings: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if self.ok and self.reason_code is not None:
            raise ValueError("an eligible font must not carry a refusal reason_code")
        if not self.ok and not self.reason_code:
            raise ValueError("a refusal must carry a reason_code")
        if not self.ok and self.warnings:
            raise ValueError("a refusal must not carry warnings")


# --------------------------------------------------------------------------- #
# Latin glyph detection
# --------------------------------------------------------------------------- #


def latin_glyphs(font: TTFont) -> set[str]:
    """Glyph **names** whose only cmap codepoints are Latin-script.

    Membership comes from the Unicode Script property
    (``fontTools.unicodedata.script``), as Research2 D.4 requires -- "derive membership
    from the Unicode script property via cmap plus script data, not a hard-coded block
    list". So Greek, Cyrillic, Devanagari, Arabic and CJK are excluded by the data, and
    Latin Extended and accented forms are included without being enumerated here.

    **Glyph names, not characters.** Research2 leaves this open ("TBD -- confirm whether
    elements are glyph names or Unicode characters; characters are stable across fonts,
    glyph names are not"). Glyph names are correct for this pipeline: the consumer is
    M1.4, which edits ``hmtx``, and ``hmtx`` is keyed by glyph name. Research2's own
    patch formula iterates the same way -- ``new_advance(g) for g in latin_glyphs(font)``.
    The instability the research warns about is real but irrelevant here, because the
    set is derived from, and applied to, one specific font within a single operation;
    it is never persisted or compared across fonts.

    **Shared glyphs are excluded, deliberately.** A glyph reachable from *any*
    non-Latin codepoint is left out even when it is also reachable from a Latin one.
    Fonts really do share one glyph between homoglyphs -- Latin ``A`` (U+0041), Greek
    ``Alpha`` (U+0391) and Cyrillic ``A`` (U+0410) can all map to the same outline.
    Widening that glyph would change Greek and Cyrillic rendering, breaking the hard
    invariant from Research2 D.5 that the patcher "must assert zero advance delta for
    every non-Latin glyph". Script-neutral codepoints (Common, e.g. space, digits and
    ASCII punctuation) are treated the same way for the same reason: they are shared
    with every script, so a shared glyph is not safe to widen.

    The practical consequence, which **M1.4 must be aware of**: digits, punctuation and
    the space are *not* in this set, because their script is Common rather than Latin.
    Tracking applied only to letters leaves punctuation unspaced. The space glyph is
    handled separately by ``word_scale``, but digits and punctuation are not handled at
    all. Widening the set is a contract decision for M1.4, not a fix to make here --
    see the module report.

    Returns an empty set for a font with no cmap or no Latin coverage. Glyph names not
    present in the font's glyph order are filtered out, so a damaged cmap pointing at
    nonexistent glyphs cannot leak a bad name into the patcher.
    """
    try:
        cmap = font.getBestCmap()
    except Exception:
        return set()
    if not cmap:
        return set()

    known = set(font.getGlyphOrder())
    latin: set[str] = set()
    shared: set[str] = set()
    for codepoint, name in cmap.items():
        if name not in known:
            continue
        if script(chr(codepoint)) == "Latn":
            latin.add(name)
        else:
            shared.add(name)
    return latin - shared


def _core_latin_coverage(font: TTFont) -> float:
    """Fraction of `CORE_LATIN_CODEPOINTS` the font maps to a real glyph."""
    try:
        cmap = font.getBestCmap()
    except Exception:
        return 0.0
    if not cmap:
        return 0.0
    known = set(font.getGlyphOrder())
    present = sum(
        1
        for codepoint in CORE_LATIN_CODEPOINTS
        if cmap.get(codepoint) in known
    )
    return present / len(CORE_LATIN_CODEPOINTS)


# --------------------------------------------------------------------------- #
# Icon fonts
# --------------------------------------------------------------------------- #


def _in_pua(codepoint: int) -> bool:
    return any(low <= codepoint <= high for low, high in _PUA_RANGES)


def is_icon_font(font: TTFont) -> bool:
    """Whether the font is an icon/symbol font rather than a text face.

    Research2 D.4/D.6 define the signal as "high PUA share, absence of basic letters",
    and the accessibility reason it matters: icons shipped as fonts may not be exposed
    to assistive technology, and screen readers may announce the font name instead of
    the content (Scanlon et al. 2021). Such a font must never reach a reader as body
    text, which is why the card escalates this to a refusal.

    Two independent signals, either sufficient:

    1. At least `ICON_PUA_SHARE` of the cmap lies in a Private Use Area. This is how
       icon sets are built -- there is no standard codepoint for a "save" pictogram.
    2. The font maps PUA codepoints and has **no** core Latin letters at all. This
       catches a small icon set whose PUA share is diluted by, say, a block of
       punctuation.

    A text face that happens to carry a handful of PUA glyphs is not caught by either,
    which is the intended asymmetry.
    """
    try:
        cmap = font.getBestCmap()
    except Exception:
        return False
    if not cmap:
        return False

    pua = sum(1 for codepoint in cmap if _in_pua(codepoint))
    if pua == 0:
        return False
    if pua / len(cmap) >= ICON_PUA_SHARE:
        return True
    return not any(codepoint in CORE_LATIN_CODEPOINTS for codepoint in cmap)


# --------------------------------------------------------------------------- #
# Monospace
# --------------------------------------------------------------------------- #


def is_monospace(font: TTFont) -> bool:
    """Whether the font is monospaced.

    The card's must-never rule makes this a fail-safe check: **any** of three
    independent signals is enough to refuse.

    1. ``post.isFixedPitch`` is non-zero -- the font's own declaration.
    2. PANOSE says proportion 9 (monospaced) within family 2 (Latin Text). Used only
       as a positive signal, never as evidence of the negative: Doyle 2005 surveyed
       13,000 TTF files across nine compendia and found only three supplied adequate
       classification information, with the two classification systems mapping poorly
       onto each other (Research2 D.2). That makes PANOSE unreliable when it is
       *absent or zero*, which is exactly the case this check ignores.
    3. Measured: every cmap'd Latin letter glyph carries an identical non-zero
       advance, over a sample of at least `MONOSPACE_MIN_SAMPLE` distinct glyphs.
       This is the signal that actually catches fonts in the wild, since 1 and 2 are
       frequently unset.

    The asymmetry is deliberate. A false positive costs the user one font from the
    shortlist; a false negative ships a patched monospace font, where adding tracking
    destroys the fixed grid that is the font's entire purpose, and where the advance
    uniformity downstream code may assume no longer holds.
    """
    try:
        post = font.get("post")
        if post is not None and getattr(post, "isFixedPitch", 0):
            return True
    except Exception:
        pass

    try:
        os2 = font.get("OS/2")
        panose = getattr(os2, "panose", None) if os2 is not None else None
        if panose is not None:
            if getattr(panose, "bFamilyType", 0) == 2 and (
                getattr(panose, "bProportion", 0) == 9
            ):
                return True
    except Exception:
        pass

    try:
        cmap = font.getBestCmap()
        hmtx = font["hmtx"]
    except Exception:
        return False
    if not cmap:
        return False

    # Sorted so the sample -- and therefore the verdict -- cannot depend on dict order.
    names = sorted(
        {
            name
            for codepoint, name in cmap.items()
            if codepoint in CORE_LATIN_CODEPOINTS
        }
    )
    advances: set[int] = set()
    for name in names:
        try:
            advances.add(hmtx[name][0])
        except Exception:
            return False
    if len(names) < MONOSPACE_MIN_SAMPLE:
        return False
    return len(advances) == 1 and 0 not in advances


def _is_italic_only(font: TTFont) -> bool:
    """Whether the face declares itself italic/oblique.

    Reads both the ``head.macStyle`` italic bit and ``OS/2.fsSelection``, because
    fonts in the wild set one without the other.
    """
    try:
        head = font.get("head")
        if head is not None and getattr(head, "macStyle", 0) & 0x02:
            return True
    except Exception:
        pass
    try:
        os2 = font.get("OS/2")
        if os2 is not None and getattr(os2, "fsSelection", 0) & 0x01:
            return True
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def open_font(data: bytes) -> TTFont:
    """Parse font bytes into a `TTFont`, handling TrueType Collections.

    Raises whatever fontTools raises -- callers in this module contain it. Exposed
    because `latin_glyphs` takes an already-open font, so a caller needs a supported
    way to produce one.
    """
    font_number = 0 if data[:4] == b"ttcf" else -1
    return TTFont(BytesIO(data), lazy=True, fontNumber=font_number)


def _read(path: Path) -> bytes | str:
    """Read the file, or return a refusal code."""
    try:
        if not path.is_file():
            return FONT_UNREADABLE
        if path.stat().st_size > MAX_FONT_BYTES:
            return FONT_TOO_LARGE
        return path.read_bytes()
    except (OSError, ValueError):
        # Unreadable for any filesystem reason: absent, a directory, a permission
        # error, a dangling symlink, an illegal name on this platform.
        return FONT_UNREADABLE


# --------------------------------------------------------------------------- #
# The card's interface
# --------------------------------------------------------------------------- #


def is_eligible(path: str | os.PathLike[str]) -> Eligibility:
    """Decide whether the font at ``path`` may be patched.

    Checks run in this fixed order, and the **first** refusal is the one reported:

    1. readable, and within `MAX_FONT_BYTES`        -> `FONT_UNREADABLE`, `FONT_TOO_LARGE`
    2. parses as an sfnt container                   -> `FONT_UNREADABLE`
    3. `REQUIRED_TABLES` all present                 -> `MISSING_TABLE`
    4. those tables actually decode                  -> `FONT_UNREADABLE`
    5. ``head.unitsPerEm`` > 0                       -> `NO_UPM`
    6. not an icon font                              -> `BASE_IS_ICON_FONT`
    7. not monospaced                                -> `BASE_IS_MONOSPACE`
    8. enough core Latin coverage                    -> `NO_LATIN_COVERAGE`

    The order is part of the contract, because several conditions co-occur and the
    code must be stable for a given file. Icon precedes monospace because an icon font
    that *declares* fixed pitch satisfies both, and the more specific diagnosis is the
    more useful one. (The measured monospace signal samples Latin letters, which an
    icon font has none of, so the two checks overlap less than they appear to.) Both
    precede the coverage check, because an icon or monospace font usually fails
    coverage too, and "this is an icon font" tells a user more than "this font lacks
    Latin letters".

    An eligible font may carry warnings: `PARTIAL_LATIN_COVERAGE`, `ITALIC_ONLY`,
    `VARIABLE_FONT`, always in that order.

    Deterministic for the same bytes (a card postcondition): no clock, no environment,
    no filename inspection, and every set is sorted before it is reduced.

    >>> is_eligible("/nonexistent/font.ttf")
    Eligibility(ok=False, reason_code='FONT_UNREADABLE', warnings=())
    """
    data = _read(Path(path))
    if isinstance(data, str):
        return Eligibility(False, data)

    try:
        font = open_font(data)
    except Exception:
        # Deliberately broad. fontTools raises TTLibError, struct.error, OverflowError,
        # UnicodeDecodeError, KeyError and more depending on where a malformed file
        # fails, and Research2 D.2 treats corrupt and adversarial fonts as an expected
        # input class. Containment here is the point; nothing is swallowed, because the
        # refusal is still reported with a code.
        return Eligibility(False, FONT_UNREADABLE)

    try:
        missing = [name for name in REQUIRED_TABLES if not font.has_key(name)]
        if missing:
            return Eligibility(False, MISSING_TABLE)

        try:
            # lazy=True defers decoding, so corruption *inside* a table only surfaces
            # when it is touched. Force the ones the pipeline depends on.
            for name in REQUIRED_TABLES:
                font[name]
            font.getBestCmap()
            font.getGlyphOrder()
        except Exception:
            return Eligibility(False, FONT_UNREADABLE)

        units_per_em = getattr(font["head"], "unitsPerEm", 0)
        if not isinstance(units_per_em, int) or units_per_em <= 0:
            return Eligibility(False, NO_UPM)

        if is_icon_font(font):
            return Eligibility(False, BASE_IS_ICON_FONT)

        if is_monospace(font):
            return Eligibility(False, BASE_IS_MONOSPACE)

        coverage = _core_latin_coverage(font)
        if coverage < MIN_CORE_LATIN_RATIO or not latin_glyphs(font):
            return Eligibility(False, NO_LATIN_COVERAGE)

        warnings: list[str] = []
        if coverage < 1.0:
            warnings.append(PARTIAL_LATIN_COVERAGE)
        if _is_italic_only(font):
            warnings.append(ITALIC_ONLY)
        if font.has_key("fvar"):
            warnings.append(VARIABLE_FONT)
        return Eligibility(True, None, tuple(warnings))
    finally:
        try:
            font.close()
        except Exception:
            pass
