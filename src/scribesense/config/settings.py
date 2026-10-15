"""M1.1 -- Configuration vocabulary (contract K1, owner O1).

Single definition of every user setting: name, unit, range, step; plus deterministic
step rounding and validation. This module is the source of truth for ranges, steps
and the base-font shortlist. It is pure: no I/O, no clock, no randomness, no font
access, no platform checks.

Must never (per the M1.1 card): import UI, store or adapter code. The only imports
here are the standard library and the frozen K1 contract (`scribesense.contracts.config`),
which owns the types and constants: `Configuration`, `Issue`, `Range`, `RANGES`,
`SHORTLIST`, `STEP_TOLERANCE`, `UI_LINE_RECOMMENDED_MAX` and `IssueCode`. This module
re-exports them and implements the two K1 functions; nothing is defined twice.

Call order (K1 rule 2) is always::

    cfg = round_to_steps(raw_cfg)
    issues = validate(cfg)

`round_to_steps` **never clamps**. An out-of-range value stays out of range so that
`validate` reports it as an ``error`` rather than having it silently corrected
(M1.1 card, "Failure cases"). Defaults live in the preset module (K2), not here:
`Range` deliberately carries no default, and `Configuration` has no field defaults.

What this module does NOT do, by contract:

* It does not define presets or defaults -- that is K2/M1.2.
* It does not read font files, so it cannot check any rule that needs font metrics
  (see `WORD_SCALE_IS_MULTIPLICATIVE` below).
* It does not apply anything. `reading_line`, `ui_line` and `text_scale` are
  application/rendering-layer settings for apps; `base_family`, `letter_em` and
  `word_scale` change the generated glyph advances, and `reading_line`/`ui_line` the
  line metrics of the reading/UI variants (M1.4/M1.5). Nothing here assumes a
  display server can enforce per-application font settings -- Wayland cannot.

Evidence note
-------------
The numeric ranges below come from the frozen K1 contract. Research2 (the O1
consolidated research document) supplies their justification and, in several places,
disagrees with them; every such disagreement is recorded inline next to the field it
affects rather than silently reconciled. Research2's own summary of the evidence base
is that letter- and word-spacing effects are "genuinely contested" (positive, null and
negative results across populations and tasks), that no typeface -- including any
dyslexia-specific one -- is established as superior, and that WCAG/BDA numbers are
*accessibility requirements about reachability*, not proofs of benefit. The ranges
here therefore exist to make values reachable and adjustable, not to assert that any
particular value helps any particular reader.
"""

from __future__ import annotations

import math
from dataclasses import replace
from decimal import Decimal

from scribesense.contracts.config import (
    RANGES,
    SHORTLIST,
    STEP_TOLERANCE,
    UI_LINE_RECOMMENDED_MAX,
    Configuration,
    Issue,
    IssueCode,
    Range,
)

__all__ = [
    "Configuration",
    "Issue",
    "Range",
    "SHORTLIST",
    "RANGES",
    "NUMERIC_FIELDS",
    "STEP_TOLERANCE",
    "UI_LINE_RECOMMENDED_MAX",
    "WORD_SCALE_IS_MULTIPLICATIVE",
    "NON_FINITE",
    "NOT_A_NUMBER",
    "NOT_ON_STEP",
    "OUT_OF_RANGE",
    "UI_LINE_ABOVE_RECOMMENDED",
    "UNKNOWN_FONT",
    "contains",
    "count",
    "decimals_of",
    "is_on_step",
    "positions",
    "round_to_steps",
    "snap",
    "validate",
]


# --------------------------------------------------------------------------- #
# Issue codes (K1 rule 4)
# --------------------------------------------------------------------------- #

NON_FINITE = IssueCode.NON_FINITE.value
"""error -- value is NaN or +/-inf."""

OUT_OF_RANGE = IssueCode.OUT_OF_RANGE.value
"""error -- value lies outside ``RANGES[field]``."""

UNKNOWN_FONT = IssueCode.UNKNOWN_FONT.value
"""error -- ``base_family`` is not a member of ``SHORTLIST``."""

NOT_ON_STEP = IssueCode.NOT_ON_STEP.value
"""error -- guard only: the value was not passed through ``round_to_steps`` first.

Never occurs in the normal flow, because `round_to_steps` always lands on the step
lattice. It exists so that a caller which skips the rounding step is told so, instead
of silently feeding an unrepresentable value to the font patcher (M1.4).
"""

NOT_A_NUMBER = IssueCode.NOT_A_NUMBER.value
"""error -- value is not a real number (``None``, ``str``, ``bool``, ...).

K1 AMENDMENT (M1.1, 2026-10-14; added to `IssueCode`, sign-off pending). Added because
`Configuration` is an ordinary dataclass: its ``float`` annotations are not enforced
at runtime, and a configuration can arrive from a persisted store (M2.x) where a
field may be ``null`` or a string. Two concrete failures motivated it:

* ``letter_em="0.12"`` would raise ``TypeError`` out of `round_to_steps`, violating
  the requirement that invalid input never crashes the contract;
* ``bool`` is a subclass of ``int`` in Python, so ``text_scale=True`` would otherwise
  be read as ``1.0`` -- a *valid* value. That is precisely the "accidentally making
  invalid values valid" failure the module must avoid, so ``bool`` is rejected here
  even though it is numerically coercible.

Downstream effect: callers that switch on issue codes must treat ``NOT_A_NUMBER``
like any other error. Callers that only test ``level == "error"`` are unaffected.
"""

UI_LINE_ABOVE_RECOMMENDED = IssueCode.UI_LINE_ABOVE_RECOMMENDED.value
"""warning -- ``ui_line`` exceeds `UI_LINE_RECOMMENDED_MAX`. Never blocks."""


# --------------------------------------------------------------------------- #
# Tolerances and thresholds
# --------------------------------------------------------------------------- #

# STEP_TOLERANCE = 1e-9 (K1, imported).
# Absolute tolerance for every float comparison in this module (K1 rule 3).
#
# Applies to on-step checks, to the inclusive range bounds, and to the `ui_line`
# warning threshold. No comparison in this module uses exact float equality.
# Measured worst-case off-step residual produced by `round_to_steps` across a
# 1,000,000-value sweep of all five grids is ~3.6e-16, six orders of magnitude
# inside this tolerance.

# UI_LINE_RECOMMENDED_MAX = 1.3 (K1, imported).
# Above this, `validate` emits a warning for ``ui_line`` (K1 rule 4, [D A12]).
#
# Research2 located **no dyslexia-specific evidence for UI line height at all** and
# classes the whole field as "pure engineering judgment ... TBD -- engineering decision
# or user testing required" (Research2 B.1, B.3 *ui_line*). This threshold is therefore
# an engineering guard against UI chrome growing until controls overlap or clip, not an
# evidence-derived optimum. It is a warning and never blocks generation.

WORD_SCALE_IS_MULTIPLICATIVE = True
"""``word_scale`` is a factor on the font's own space advance; 1.0 = unchanged.

This resolves Research2 discrepancy **D-01b**, which the research document left open
as its single most consequential unknown: it could not tell whether ``word_scale``
meant "em added to the space glyph" (additive) or "factor on the default space
advance" (multiplicative), and noted the two imply different patch formulas. K1
settles it -- ``word_scale: 1.0-3.0 x space width`` -- so the multiplicative reading
governs, and `RANGES["word_scale"]` is a factor, not an em increment.

Two consequences follow, and both belong to other modules:

1. **M1.4** must use ``new_space_advance = round(original_space_advance * word_scale)``
   (Research2 E.2.3), not the additive formula in E.2.2.
2. **WCAG 1.4.12's word-spacing floor (0.16 x font size) cannot be expressed as a
   ``word_scale`` here.** Converting it needs the Regular face's space width in em,
   which this module must never load. K2 already covers this: its `WordSpacing`
   ``target_em`` mode performs exactly that conversion inside `resolve()`. A
   configuration reaches the WCAG floor only if the user or a preset went through
   ``target_em``; M1.1 cannot check it and does not claim to.

The same limitation blocks Research2's proposed ``W_WORD_LETTER_COUPLING`` warning
(B.2): its test, ``word_scale < 1.33 * letter_em``, is stated in additive em and is
vacuous under multiplicative semantics -- ``word_scale`` is at least 1.0 while
``1.33 * letter_em`` never exceeds 0.4, so the rule could never fire. It is therefore
NOT implemented here. The underlying finding is real (Galliussi et al. 2020: raising
letter spacing without raising word spacing impaired reading speed), so the check is
recommended for K2 `resolve()` or M1.4, where the space width is known. See the
module report for the handoff.
"""


# --------------------------------------------------------------------------- #
# Range helpers (the `Range` type itself is K1's)
# --------------------------------------------------------------------------- #


def decimals_of(r: Range) -> int:
    """Decimal places implied by ``r.step`` -- the ``decimals_of(step)`` of K1.

    Derived from the step's decimal spelling (``0.02 -> 2``, ``0.1 -> 1``) so the
    rounding precision can never drift out of sync with the step itself.
    """
    exponent = Decimal(str(r.step)).as_tuple().exponent
    return max(0, -int(exponent))


def count(r: Range) -> int:
    """Number of legal positions on ``r``'s grid, bounds included."""
    return int(round((r.max - r.min) / r.step)) + 1


def positions(r: Range) -> tuple[float, ...]:
    """Every legal value, ascending -- the exhaustive list a UI control offers."""
    return tuple(round(r.min + i * r.step, decimals_of(r)) for i in range(count(r)))


def snap(r: Range, value: float) -> float:
    """Round ``value`` onto ``r``'s step lattice. Does not clamp (K1 rule 1).

    Implements ``round(min + round((v - min) / step) * step, decimals_of(step))``
    exactly as K1 rule 1 writes it, with Python's ``round`` (ties to even). An exact tie
    such as ``letter_em`` 0.01 therefore goes to 0.00 while 0.03 goes to 0.04; real
    inputs come from `positions` or a slider and are on the lattice already. Changing
    the tie rule would be a K1 amendment.
    """
    # The trailing ``+ 0.0`` turns -0.0 into 0.0. Both compare equal, so it does
    # not affect validation -- but their *reprs* differ, and M1.5 derives the
    # content key from the canonical decimal text. Without this, a configuration
    # could flip between "-0.0" and "0.0" and silently invalidate a cached font.
    return round(r.min + round((value - r.min) / r.step) * r.step, decimals_of(r)) + 0.0


def contains(r: Range, value: float) -> bool:
    """Inclusive bounds check, tolerant to `STEP_TOLERANCE` (K1 rule 3)."""
    return r.min - STEP_TOLERANCE <= value <= r.max + STEP_TOLERANCE


def is_on_step(r: Range, value: float) -> bool:
    """Whether ``value`` sits on ``r``'s lattice, within `STEP_TOLERANCE`.

    Compares against the distance to the nearest lattice point rather than
    testing ``value == snap(r, value)``, so no exact float equality is used.
    """
    offsets = (value - r.min) / r.step
    return abs(offsets - round(offsets)) * r.step <= STEP_TOLERANCE


# --------------------------------------------------------------------------- #
# The vocabulary itself
# --------------------------------------------------------------------------- #

# SHORTLIST (K1, imported): Carlito, Atkinson Hyperlegible, Lexend, Noto Sans.
# Permitted values for ``base_family`` (K1).
#
# The four families are fixed by the frozen contract. Research2 D.1.4 searched for
# reading-performance evidence for each and reports: "I located no controlled
# reading-performance study for any of them in this review. That absence is itself the
# finding, and it must not be papered over." Per family:
#
# * **Carlito** -- no controlled study. Included on technical grounds (licensing,
#   availability, claimed metric-compatibility with Calibri); Research2 notes the
#   metric-compatibility claim itself is unverified in its sources.
# * **Atkinson Hyperlegible** -- design-rationale source only (Giovine 2025), describing
#   disambiguation of ``i``/``l`` and ``O``/``0``. Research2: "Must not be described as
#   proven to improve reading."
# * **Lexend** -- no source located at all; Research2 calls the widely repeated
#   reading-proficiency claim "unverified in this review".
# * **Noto Sans** -- a coverage family. "Its value to O1 is character coverage, not
#   reading performance."
#
# What *is* supported is the category, not any member: Rello & Baeza-Yates 2016
# (97 participants, 48 with dyslexia, 12 fonts) found sans-serif/monospaced/roman
# significantly outperformed serif/proportional/italic. Against that, Beymer et al.
# 2008 and Arditi & Cho 2005 found no reliable serif/sans difference, and Vecino et al.
# 2024 found users preferred serif -- so Research2 concludes the sans-serif lean is
# "not robust" and that serif families must not be disqualified outright. Wallace et al.
# 2022 (102 + 113 adults) found reading speed varied up to 35% between the fastest and
# slowest fonts, concluding "one font does not fit all"; Sheppard et al. 2023 found 58%
# of 94 children had a stable *personal-best* font variation. The design consequence is
# that this list exists to be chosen from by the user, and no member is a recommendation.
#
# Separately, Research2 A.3 reports that purpose-built dyslexia typefaces are not
# supported by the better-controlled literature (Kuster et al. 2017, n=170 and n=147;
# Wery & Diliberto 2016; Joseph & Powell 2022), and that where Dyslexie showed a 7%
# advantage it vanished once spacing was matched (Marinus et al. 2016, N=39) -- which is
# part of why this application patches spacing rather than shipping a novelty typeface.
# Counter-evidence exists (Franzen et al. 2019; Karatay & Unal 2023, N=3; Brata 2025)
# and is weaker in design and venue; it is recorded, not suppressed.

# RANGES (K1, imported) -- per-field evidence for the frozen values:
#
# Extra tracking added to every eligible glyph's advance, as a fraction of the em.
#
#   K1: 0.00-0.30, step 0.02.
#
# Evidence (Research2 B.3 letter_em) is contested and is summarised here because
# this is the field most likely to be mistaken for a recommendation:
#   * Zorzi et al. 2012 (Italian/French children with dyslexia): "extra-large"
#     spacing, +2.5 pt on a 14 pt body ~= 0.179 em, improved reading on the fly --
#     but spaced sentences were also shorter (sentence-length confound).
#   * Galliussi et al. 2020 (128 children, 64 with dyslexia): *default* letter
#     spacing was read faster than increased (4.12 vs 4.04 syllables/s).
#   * Luniewska et al. 2021 (38 dyslexic + 32 typical, ages 10-14): no effect on
#     accuracy, comprehension or speed -- a null at Zorzi's magnitude.
#   * Korinth et al. 2020 (24 typical adults): fixations shortened, but no WPM
#     benefit and faster readers were slowed.
#   * Wallace et al. 2022: tested -0.1 to 0.4 em; drop-offs in speed,
#     comprehension and preference at the extremes.
#   * WCAG 1.4.12 requires 0.12 x font size to be *reachable* (Giuffrida 2020).
#
# Range decision: the maximum must clear the 0.12 em WCAG floor and the ~0.18 em
# tested magnitude while stopping short of the 0.4 em region where Wallace
# measured drop-offs. K1's 0.30 does that. Research2 B.1 proposed 0.25 instead;
# K1 is frozen and 0.30 remains below the measured drop-off, so the divergence is
# recorded, not applied. Research2 also proposed step 0.01 against K1's 0.02; the
# practical cost is that Galliussi's tested 0.07 em is NOT reachable on this grid
# (0.06 and 0.08 are) -- see the report's contract-findings section.
# -> RANGES["letter_em"] = Range(0.00, 0.30, 0.02)
# Multiplier on the font's own space advance; 1.0 = unchanged.
# See WORD_SCALE_IS_MULTIPLICATIVE for why this is a factor and not an em
# increment, and for why the WCAG word-spacing floor cannot be checked here.
#
#   K1: 1.0-3.0, step 0.25.
#
# Evidence (Research2 B.3 word_scale), all of it stated in *em added*, which is
# why none of it can be transferred to this scale without a font's space width:
#   * Galliussi et al. 2020: +0.27 em word spacing read faster than default
#     (4.12 vs 4.03 syllables/s), with a significant letter x word interaction --
#     raising tracking without raising word spacing impaired speed.
#   * Rayner et al. 2010: interword space drives word-boundary demarcation and
#     saccade targeting.
#   * WCAG 1.4.12 floor: 0.16 x font size (Giuffrida 2020).
#   * Hou et al. 2022 reports the opposite pressure -- a visual-comfort *ceiling*
#     around standard + 0.7 pt (~0.044 em at 16 px), i.e. far below both the WCAG
#     floor and Galliussi's tested value. Research2: "These three figures do not
#     agree," and the optimum is TBD.
#   * Radovan & Perdih 2016 warn that *uneven* inter-word spacing (justified text)
#     confuses readers with dyslexia -- a rendering concern, not a font-metric one.
# The 1.0 floor is the only evidence-backed endpoint: van den Boer & Hakvoort 2014
# found below-default spacing reduced fluency, so sub-default spacing is excluded
# from the range entirely rather than merely discouraged.
# -> RANGES["word_scale"] = Range(1.0, 3.0, 0.25)
# Line height on reading surfaces, as a multiple of font size. Applied by the
# rendering layer; it is not written into the font by M1.4 unless that module
# writes vertical metrics.
#
#   K1: 1.0-2.0, step 0.1.
#
# Evidence (Research2 B.3 reading_line) is a three-way disagreement that the code
# must expose as a range rather than resolve:
#   * WCAG 1.4.12 and the British Dyslexia Association Style Guide: >= 1.5.
#   * Rello et al. 2013 (28 participants with dyslexia, eye-tracked): line spacing
#     across 0.8/1.0/1.4/1.8 produced *no significant main effect*, with marginal
#     impairment at both extremes. In the same study font size did matter.
#   * Hou et al. 2022: comfort ceiling around 1.2x standard.
# Research2: "Guideline floor and experimental optimum are different things and
# must not be conflated." The 1.0 minimum keeps "unchanged" reachable so a user
# can revert; Research2 B.1 proposed a 1.2 floor instead, which would remove that.
# -> RANGES["reading_line"] = Range(1.0, 2.0, 0.1)
# Line height for interface chrome (labels, buttons, menus), as a multiple of
# font size. A UI-only setting: the application's rendering layer applies it.
# No display server, Wayland included, can impose it on other applications.
#
#   K1: 1.0-2.0, step 0.1, warning above UI_LINE_RECOMMENDED_MAX.
#
# Research2 B.3 located no dyslexia-specific evidence for this field whatsoever
# and classes the 1.2-1.8 band it proposed as general typographic practice. K1's
# wider 1.0-2.0 with a warning above 1.3 is the engineering alternative: it keeps
# large values reachable (a user who needs them is not blocked) while flagging
# that UI chrome at that height risks clipping. Confidence: low, by both accounts.
# -> RANGES["ui_line"] = Range(1.0, 2.0, 0.1)
# Global multiplier over the application's base text size; 1.0 = 100%.
# UI/reading-layer only: effective_size = base_size * text_scale. Per K1/K2 and
# Research2 F.3 it is deliberately excluded from the M1.5 content key, so changing
# it must never trigger a font rebuild.
#
#   K1: 1.0-2.0, step 0.05.
#
# Evidence (Research2 B.3 text_scale) -- the best-supported field in the set:
#   * WCAG 1.4.4 requires text to resize to 200% without loss of content or
#     functionality (Boyalakuntla et al. 2021). The 2.0 maximum is that
#     requirement, making it the one endpoint here that is a hard obligation.
#   * Rello et al. 2013 found font size significantly affected readability and
#     understandability where line spacing did not; O'Brien et al. 2005 found
#     readers with dyslexia have higher critical print sizes; Sawyer et al. 2025
#     (N=60) found print size affected reading speed while x-height alone did not.
#     Research2: this supports *enlargeability*, not a fixed size -- hence a
#     neutral 1.0 default in K2 rather than a nonzero one.
#   * Bigham 2014: opportunistic magnification of real pages averaged 1.6x before
#     horizontal scrolling and overlap appeared -- a practical caution about the
#     top of this range that the rendering layer, not this module, must handle.
# Research2 B.1 proposed a 0.5 minimum (a derived 50%-200% band, Scanlon et al.
# 2021). K1 floors at 1.0: no accessibility requirement asks for text *smaller*
# than the base size, and shrinking text is contrary to this application's purpose.
# -> RANGES["text_scale"] = Range(1.0, 2.0, 0.05)
#
# K1: `RANGES` is the single source of truth for ranges and steps -- UI controls, validation and tests.
#
# Keyed by the exact `Configuration` field name. Nothing in the codebase should restate
# a bound or a step; read it from `scribesense.contracts.config.RANGES` (re-exported here).

NUMERIC_FIELDS: tuple[str, ...] = tuple(RANGES)
"""The rounded/validated numeric fields, in the order `validate` reports them."""



# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _is_real_number(value: object) -> bool:
    """Whether ``value`` is an ``int`` or ``float`` usable as a setting.

    ``bool`` is excluded even though it is an ``int`` subclass: ``True`` would
    otherwise read as ``1.0``, which is a *valid* value for four of the five numeric
    fields, turning a type error into a silently accepted configuration.

    ``Decimal`` and ``Fraction`` are excluded too. They are not produced anywhere in
    the pipeline, and accepting them would mean this module's float tolerances
    silently governed exact arithmetic.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --------------------------------------------------------------------------- #
# K1 interface
# --------------------------------------------------------------------------- #


def round_to_steps(c: Configuration) -> Configuration:
    """Return ``c`` with every numeric field snapped onto its step lattice.

    Canonicalisation only -- it is the first half of the K1 call order and must be
    followed by `validate`. Specifically:

    * **It never clamps** (K1 rule 1). A value outside its range is snapped to the
      nearest step of the *extended* lattice and stays out of range, so `validate`
      reports ``OUT_OF_RANGE``. Silently pulling it to the bound would hide a bad
      stored configuration or a bad caller.
    * **Non-finite values pass through unchanged** (K1 rule 1). Rounding NaN or
      +/-inf is undefined; `validate` reports ``NON_FINITE``.
    * **Non-numeric values pass through unchanged**, so a corrupt stored value cannot
      raise out of this function; `validate` reports ``NOT_A_NUMBER``.
    * ``base_family`` is not touched.

    Idempotent: ``round_to_steps(round_to_steps(c)) == round_to_steps(c)``, including
    for out-of-range, non-finite and non-numeric inputs.

    >>> cfg = Configuration("Noto Sans", 0.131, 1.5, 1.5, 1.2, 1.0)
    >>> round_to_steps(cfg).letter_em
    0.14
    """
    updates: dict[str, float] = {}
    for name in NUMERIC_FIELDS:
        value = getattr(c, name)
        if not _is_real_number(value) or not math.isfinite(value):
            continue
        updates[name] = snap(RANGES[name], float(value))
    return replace(c, **updates) if updates else c


def validate(c: Configuration) -> list[Issue]:
    """Check ``c`` and return every issue found; an empty list means it is valid.

    Expects an already-rounded configuration (K1 rule 2). Fields are reported in a
    fixed order -- ``base_family`` first, then `NUMERIC_FIELDS` -- so the result is
    deterministic and directly assertable in tests.

    At most one *error* is reported per numeric field, in precedence order
    ``NOT_A_NUMBER`` > ``NON_FINITE`` > ``OUT_OF_RANGE`` > ``NOT_ON_STEP``. The
    precedence matters for correctness, not just tidiness: every comparison against
    NaN is ``False``, so a range check alone would quietly accept NaN as in-range.
    Non-finite values are therefore detected first and short-circuit the rest.

    ``ui_line`` additionally gets a warning above `UI_LINE_RECOMMENDED_MAX`, emitted
    whenever the value is a finite real number -- independently of any error on that
    field, so the advisory does not disappear when combined with another problem.

    >>> cfg = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.5, 1.0)
    >>> validate(cfg)
    [Issue(level='warning', field='ui_line', code='UI_LINE_ABOVE_RECOMMENDED')]
    """
    issues: list[Issue] = []

    if c.base_family not in SHORTLIST:
        issues.append(Issue("error", "base_family", UNKNOWN_FONT))

    for name in NUMERIC_FIELDS:
        value = getattr(c, name)
        bounds = RANGES[name]

        if not _is_real_number(value):
            issues.append(Issue("error", name, NOT_A_NUMBER))
            continue

        number = float(value)
        if not math.isfinite(number):
            issues.append(Issue("error", name, NON_FINITE))
            continue

        if not contains(bounds, number):
            issues.append(Issue("error", name, OUT_OF_RANGE))
        elif not is_on_step(bounds, number):
            issues.append(Issue("error", name, NOT_ON_STEP))

        if name == "ui_line" and number > UI_LINE_RECOMMENDED_MAX + STEP_TOLERANCE:
            issues.append(Issue("warning", name, UI_LINE_ABOVE_RECOMMENDED))

    return issues
