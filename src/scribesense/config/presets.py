"""M1.2 -- Presets and preset resolution (contract K2, owner O1).

A preset stores the user's **intent**; `resolve` turns it into one concrete
`Configuration` (K1). Only the resolved `Configuration` is used for building fonts --
there is never a second authoritative value. [D A9, A10]

This module owns the built-in values and the resolution rules. It does **not** own the
configuration vocabulary: every range, step, tolerance and validation rule is imported
from `scribesense.config.settings` (M1.1). No bound and no validation logic is restated here.

Persistence is M3.1's job -- nothing in this module reads or writes storage, and since
2026-11-14 nothing here *serialises* either. This module once carried `to_record` /
`from_record` for K9 D9's versioned JSON shape; M3.1 shipped `store/serialize.py` with the
same shape, `PresetRepo.save()` calls that, and O1's pair was left with no caller and no
test. Two implementations of one record shape is a second authority on what a preset
legally contains -- the drift `to_record`'s own docstring warned about one level down --
so the pair was removed (PLAN_v2 2026-11-14, W7). What stays here is the *model*: which
kinds exist, which ids are reserved, and `is_storable`. A built-in is never stored -- a
built-in is code. It is
platform-independent: no display-server logic. The only file it reads is the bundled
Regular face, to measure its space width for ``target_em`` (K2 ``resolve``). The
`ui_line` and `text_scale` fields it carries are applied by the application's
rendering layer; nothing here assumes Wayland can impose them on other applications.

The three preset kinds
----------------------
``builtin``
    The three presets in `BUILTINS`, with fixed IDs. Owned by O1, never editable, and
    never overwritable by a user preset.
``user``
    A saved, named preset owned by the user and stored by M3.1. Gets a ``uuid4`` ID.
``custom``
    The working configuration the user is currently editing -- the "Custom" entry in a
    preset list. Per Research2 C.4, Custom "is not a fixed configuration and must not be
    given default values", so `custom_preset` requires explicit values and this module
    defines no Custom constant.

    Custom is **saved, not transient** (K9 D3, confirmed 2026-10-16). It is one row
    under the reserved id `CUSTOM_ID`, upserted whenever the user edits it and deleted
    when they clear it. Treating it as throwaway state was the earlier reading, and it
    loses the user's work on every restart -- which is the one thing the personalisation
    evidence behind offering Custom at all says must not happen (see `custom_preset`).
    What stays true is that Custom is *editing state*, not a catalogue entry: the store
    does not return it from ``PresetRepo.list()``, and `catalogue` appends it separately.

`ORIGINAL` is **not** a preset at all. See the note on it below.

The types (`WordSpacing`, `PresetValues`, `Preset`) and the constants `BUILTIN_IDS` and
`ORIGINAL` are K2's (`scribesense.contracts.presets`) and are re-exported unchanged.

Evidence note
-------------
The built-in values below are fixed by the frozen K2 table. Research2 Deliverable C
recommends a materially different set, and that disagreement is recorded in full on
`BUILTINS` rather than reconciled. The short version: the research argues the Default
preset should change nothing, because the letter-spacing evidence contains positive,
null and negative results and "a nonzero default would impose an unproven intervention
on every user". K2's Default is not neutral. K2 governs; the conflict is reported.

No preset in this module is claimed to be better than any other for any reader. No
study has compared these bundles (Research2 A.2, C.3).
"""

from __future__ import annotations

import math
import uuid
from dataclasses import replace
from typing import Callable, Literal

from scribesense.config import bundled
from scribesense.config.bundled import base_font_files
from scribesense.config.settings import (
    NON_FINITE,
    NOT_A_NUMBER,
    RANGES,
    Configuration,
    Issue,
    round_to_steps,
    snap_up,
    validate,
)
from scribesense.contracts.presets import (
    BUILTIN_IDS,
    ORIGINAL,
    Preset,
    PresetValues,
    WordSpacing,
)

__all__ = [
    "BUILTINS",
    "BUILTIN_IDS",
    "CUSTOM_ID",
    "DEFAULT_ID",
    "HIGH_SEPARATION_ID",
    "MEASURED_SPACE_WIDTH_EM",
    "ORIGINAL",
    "READING_ID",
    "REFERENCE_SPACE_WIDTH_EM",
    "RESERVED_IDS",
    "STORABLE_KINDS",
    "BuiltinOverwriteError",
    "Preset",
    "PresetResolutionError",
    "PresetValues",
    "SpaceWidthUnavailable",
    "WordSpacing",
    "base_font_files",
    "builtin",
    "catalogue",
    "custom_preset",
    "default_space_width_em",
    "is_original",
    "is_storable",
    "new_user_preset",
    "resolve",
    "space_width_source",
    "with_base_family",
    "with_word_multiplier",
]


# --------------------------------------------------------------------------- #
# Sentinel and identifiers
# --------------------------------------------------------------------------- #

# ORIGINAL (K2, imported) = "original".
# Sentinel meaning "revert everything" -- **not a preset**. [D]
#
# Per K2 and Research2 C.5, `ORIGINAL` selects the unmodified source font binary and
# bypasses the transformation pipeline entirely. It is therefore:
#
# * excluded from preset enumeration -- it never appears in `BUILTINS` or `catalogue`;
# * excluded from preset validation -- it has no field values to validate;
# * implemented as a short-circuit by the caller, which returns the original font,
#   **not** as a `Configuration` with all fields set to neutral values.
#
# That last distinction is the one that matters and the one most likely to be
# implemented wrongly. Zeroing the six fields is *not* equivalent to Original: a
# neutral-valued configuration still goes through the pipeline, which may substitute a
# different family or apply baseline normalisation. Original must not reach `resolve`.
#
# Its purpose is to be the honest comparison baseline for user testing, and an escape
# hatch if a generated variant misbehaves.

DEFAULT_ID = "default"
READING_ID = "reading"
HIGH_SEPARATION_ID = "high-separation"

# BUILTIN_IDS (K2, imported) = (DEFAULT_ID, READING_ID, HIGH_SEPARATION_ID): the three
# fixed built-in IDs (K2 rule 1). Names are display-only; IDs are not.

CUSTOM_ID = "custom"
"""ID of the Custom working set. Reserved so a saved preset cannot take it.

Custom is **persisted**, not transient: K9 D3 stores it as exactly one row carrying this
reserved id (see `STORABLE_KINDS`). Stored is not the same as listed -- it is editing
state, not a catalogue entry, so `PresetRepo.list()` leaves it out while
`PresetRepo.list_all()` returns it. Anything deciding which generated fonts are still in
use (K7 rule 3, "a saved preset") must read `list_all()`, or the clean-up deletes the
font of the user's own current configuration.
"""

RESERVED_IDS: frozenset[str] = frozenset(BUILTIN_IDS) | {CUSTOM_ID, ORIGINAL}
"""IDs a **user** preset may never claim.

Enforces the card's "must never: let a user preset overwrite a built-in", and extends
the same protection to the Custom slot and the Original sentinel, which would be just
as broken to shadow.

Reserved is about *who may claim an id*, not about *what may be stored*. `CUSTOM_ID` is
reserved precisely so that the one preset entitled to it -- the `custom`-kind working
set -- is the only thing that can ever occupy that row; see `STORABLE_KINDS`.
"""

STORABLE_KINDS: frozenset[str] = frozenset({"user", "custom"})
"""The preset kinds that are persisted (K9 D3).

``builtin`` is excluded because the three built-ins are *code* (`BUILTINS`): storing a
copy would create a second authoritative value, and a stale row could then shadow a
corrected built-in after an upgrade. `ORIGINAL` is excluded because it is not a preset.

Everything else is saved, ``custom`` included. The custom row is a single upsert on
`CUSTOM_ID`: there is exactly one working set, so saving it twice replaces it rather
than accumulating rows, and deleting it is how the user clears their edits.
"""


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class PresetResolutionError(ValueError):
    """`resolve` produced a configuration that `validate` rejected.

    Carries the offending `Configuration` and the K1 issues so a caller can show the
    user exactly which field failed, rather than a bare message. Only ``error``-level
    issues raise; warnings never do.
    """

    def __init__(self, config: Configuration, issues: list[Issue]) -> None:
        self.config = config
        self.issues = issues
        detail = ", ".join(f"{i.field}={i.code}" for i in issues)
        super().__init__(f"preset does not resolve to a valid configuration: {detail}")


class SpaceWidthUnavailable(LookupError):
    """A usable space width could not be obtained for a family.

    Raised when a ``target_em`` conversion is asked for and the provider returns
    something unusable (non-finite, zero or negative). Zero is called out separately
    because it would otherwise be a division by zero inside the conversion.
    """


class BuiltinOverwriteError(ValueError):
    """A user preset tried to claim a reserved ID. See `RESERVED_IDS`."""


# `PresetRecordError` was removed with `to_record` / `from_record` (2026-11-14). The store
# raises `ScribeSenseError(STORE_CORRUPT)` for a record it cannot read, which is the same
# distinction this class drew: a row that parses but no longer resolves -- a family dropped
# from `SHORTLIST`, say -- is an outdated preset, not a corrupt one, and `resolve()` reports
# it as `PresetResolutionError`. Conflating the two would make an unreadable file and an
# intact-but-stale one look alike to the caller.


# --------------------------------------------------------------------------- #
# Space width -- the one external metric resolution needs
# --------------------------------------------------------------------------- #

REFERENCE_SPACE_WIDTH_EM = 0.25
"""Fallback space advance, in em, used when a family's real metric is unknown.

**This is a provisional engineering constant, not a measurement of any font.**

Provenance: Research2 §0.2 (D-01d) reasons about the multiplicative band "for a font
whose default space is ~=0.25 em", and immediately adds that it "depends entirely on
the font". 0.25 em is also the classic typographic quarter-em word space. It is used
here so that resolution is total -- every preset resolves to something -- rather than
because any of the four shortlisted families has been measured.

Why this matters more than it looks. The conversion is
``word_scale = 1 + target_em / space_width``, and the result is then snapped to K1's
0.25 step. For the Default preset's +0.16 em target, the snapped result flips at a
space width of **0.256 em**:

    space width > 0.256 em  ->  word_scale 1.5
    space width <= 0.256 em ->  word_scale 1.75

Real sans-serif space widths sit right on top of that boundary. **Measured, now that all
four families are bundled** (2026-11-14): Carlito 0.226 em, Noto Sans 0.260, Atkinson
Hyperlegible 0.280, Lexend 0.280. So Carlito resolves to 1.75 and the other three to 1.50,
and the space glyph grows by +0.170, +0.130, +0.140 and +0.140 em respectively -- **three
of the four land below the 0.16 em WCAG 1.4.12 floor the Default preset is built on**,
because rounding to the *nearest* step rounds down. That is open item W6.5, not a property
of the fonts; see `docs/research2-conformance-O1.md` 9.1.

This constant is no longer reached by any bundled family. It still fires when a family's
file is unreadable, and it is what a user-imported font (W1) would fall back to before its
metrics are read. See `MEASURED_SPACE_WIDTH_EM` and `space_width_source`, which is how a
caller tells a measurement from this guess.
"""

MEASURED_SPACE_WIDTH_EM: dict[str, float] = {}
"""Real space advances in em, per family, measured from the **bundled** font files.

Filled lazily by `default_space_width_em` from the Regular face K2's `base_font_files`
returns: ``space_advance / unitsPerEm`` (`bundled.space_width_em`). Values are measured,
never estimated. A family that is not bundled yet is **not** cached, so it falls back to
`REFERENCE_SPACE_WIDTH_EM` until its files land -- `space_width_source` says which.
"""


def default_space_width_em(family: str) -> float:
    """Space advance in em for ``family``: measured if known, else the fallback.

    This is the default provider used by `resolve`. Inject a different one (for a real
    font-metric reader, or for a test) via ``resolve(..., space_width_em=...)``.
    """
    if family not in MEASURED_SPACE_WIDTH_EM:
        try:
            MEASURED_SPACE_WIDTH_EM[family] = bundled.space_width_em(family)
        except (bundled.BundledFontMissing, ValueError, OSError):
            return REFERENCE_SPACE_WIDTH_EM
    return MEASURED_SPACE_WIDTH_EM[family]


def space_width_source(family: str) -> Literal["measured", "fallback"]:
    """Whether `default_space_width_em` would return a real measurement or a guess.

    Exists so the UI and the report can be honest about it: a `target_em` preset
    resolved from a fallback width should not be presented as a precise value.
    """
    default_space_width_em(family)
    return "measured" if family in MEASURED_SPACE_WIDTH_EM else "fallback"


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #


# WordSpacing, PresetValues and Preset are K2's (imported). On `WordSpacing`:
#
# How a preset expresses word spacing (K2).
#
# Two modes, and the difference is the whole point of storing intent rather than a
# resolved number:
#
# ``"target_em"``
#     ``value`` is the extra space, in em, the user wants added to the word gap.
#     Because different families have different native space widths, this is
#     **recalculated whenever the base font changes** -- the stored em target stays
#     put and the resolved multiplier moves.
# ``"multiplier"``
#     ``value`` is a factor on the font's own space advance; 1.0 means unchanged.
#     This is **kept as-is across a font change**, preserving the user's relative
#     intent.
#
# Research2 classes the family→word-spacing dependency (row P-BF → P-WS) as
# HYP/DIR, enforcement WARN, confidence **Low**, and its change-impact rule R-07 is
# marked *Provisional*: "preserve letter_em (relative); re-derive word_scale only if
# multiplicative". The K2 UI rule implemented here is consistent with that, but it
# is an architectural choice, not an evidence-backed one.
#
# K2's `WordSpacing` does not check ``mode``; `resolve` rejects an unknown one.


# --------------------------------------------------------------------------- #
# Built-ins
# --------------------------------------------------------------------------- #

BUILTINS: tuple[Preset, ...] = (
    Preset(
        id=DEFAULT_ID,
        name="Default",
        values=PresetValues(
            base_family="Noto Sans",
            letter_em=0.12,
            word=WordSpacing("target_em", 0.16),
            reading_line=1.5,
            ui_line=1.2,
            text_scale=1.0,
        ),
        kind="builtin",
    ),
    Preset(
        id=READING_ID,
        name="Reading",
        values=PresetValues(
            base_family="Noto Sans",
            letter_em=0.12,
            word=WordSpacing("multiplier", 1.5),
            reading_line=1.8,
            ui_line=1.2,
            text_scale=1.0,
        ),
        kind="builtin",
    ),
    Preset(
        id=HIGH_SEPARATION_ID,
        name="High Separation",
        values=PresetValues(
            base_family="Noto Sans",
            letter_em=0.20,
            word=WordSpacing("multiplier", 2.5),
            reading_line=1.6,
            ui_line=1.2,
            text_scale=1.0,
        ),
        kind="builtin",
    ),
)
"""Default, Reading, High Separation -- exactly the K2 table. [D]

K2 marks these "draft-accepted". Every value below is provisional in the research
sense: **no study has compared these three bundles against each other or against an
unmodified font**, so none is claimed to be better than another for any reader
(Research2 A.2, C.3; decision register K-13/K-14/K-15).

Per-field evidence, and where K2 and Research2 disagree:

``base_family`` = Noto Sans (all three)
    Holding the family constant across the presets is deliberate: it means the only
    thing changing between them is spacing, so a user's outcome can be attributed to
    spacing rather than to the typeface (Research2 C.3). Research2 D.1.4 located **no
    controlled reading-performance study for Noto Sans** -- "its value to O1 is
    character coverage, not reading performance". The supported claim is the
    *category* (sans-serif), from Rello & Baeza-Yates 2016 (97 participants, 48 with
    dyslexia), and even that is contested by Beymer et al. 2008 and Arditi & Cho 2005.

``letter_em`` = 0.12 / 0.12 / 0.20
    0.12 em is the WCAG 1.4.12 floor (Giuffrida 2020) -- a *reachability requirement*,
    not a demonstrated benefit. 0.20 em sits just above Zorzi et al. 2012's tested
    "extra-large" condition (+2.5 pt on a 14 pt body, ~=0.179 em), which Research2
    recommends as 0.18; K2 chose 0.20, which K1's 0.02 grid supports. Note that
    Łuniewska et al. 2021 found **no effect** at approximately Zorzi's magnitude, and
    Galliussi et al. 2020 (128 children) found default letter spacing read *faster*
    than increased. The spread of findings is the reason these are offered as presets
    to try, not as a recommendation.

``word`` = target +0.16 em / 1.5x / 2.5x
    0.16 em is the WCAG 1.4.12 word-spacing floor. Expressing Default as a
    ``target_em`` is what lets that guideline figure survive a font change, since
    WCAG states it in em and `word_scale` is a multiplier -- see
    `REFERENCE_SPACE_WIDTH_EM` for why the resolved number is not yet settled.
    Galliussi et al. 2020 found increased word spacing read faster than default
    (4.12 vs 4.03 syllables/s) and that it interacts with letter spacing; against
    that, Hou et al. 2022 reports a visual-comfort *ceiling* far below the WCAG floor.
    Research2: "These three figures do not agree."

``reading_line`` = 1.5 / 1.8 / 1.6
    1.5 is the WCAG 1.4.12 and British Dyslexia Association floor. **Reading's 1.8 is
    the value Rello et al. 2013 flagged as marginally impairing** at the top of its
    tested 0.8-1.8 range -- that study found no significant main effect of line
    spacing at all. Research2 deliberately capped its own High Separation at 1.6
    "deliberately not 1.8". K2 nonetheless places 1.8 on Reading. Recorded as a
    conflict for the owner; K1 permits the value, so it resolves and validates.

``ui_line`` = 1.2 (all three)
    Below K1's 1.3 advisory threshold, so no built-in emits a warning. Research2 found
    **no dyslexia-specific evidence for UI line height whatsoever**; it is engineering
    judgment on both sides. Research2 keeps reading and UI line heights formally
    independent (matrix row P-RL vs P-UL: INDEP).

``text_scale`` = 1.0 (all three)
    Enlargement is an orthogonal, personal need, and bundling it would make presets
    harder to reason about (Research2 C.3). The literature supports *enlargeability*
    (WCAG 1.4.4's 200%; Rello et al. 2013; O'Brien et al. 2005; Sawyer et al. 2025),
    not a nonzero default.

**Unreconciled conflict with Research2 Deliverable C.** Research2 recommends
Default = (platform sans, 0.00 em, neutral word, 1.4, 1.4, 1.0) -- a preset that
"changes nothing", on the explicit grounds that "a nonzero default would impose an
unproven intervention on every user", and that Default's purpose is "to be the honest
baseline against which the other presets can be compared". K2's Default instead ships
WCAG-minimum spacing switched on. Functionally, K2's Default is close to Research2's
*Reading* preset, and K2 has no neutral preset at all -- `ORIGINAL` is the only way to
get unmodified text, and it bypasses the pipeline rather than being selectable
alongside the others. This is a product decision, not a technical one, so it is
reported rather than changed: K2's values are frozen and the M1.2 card's acceptance
test requires that values match the K2 table.
"""


def builtin(preset_id: str) -> Preset:
    """Look up a built-in by its fixed ID. Raises ``KeyError`` if there is none."""
    for preset in BUILTINS:
        if preset.id == preset_id:
            return preset
    raise KeyError(f"no built-in preset with id {preset_id!r}")


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def _word_scale_from_target_em(
    target_em: float, family: str, space_width_em: Callable[[str], float]
) -> float:
    """Convert an em target to a multiplier: ``1 + target_em / space_width``, rounded **up**.

    Rounded up, not to nearest (W6.5, decided by O1 2026-11-14). ``target_em`` means "add
    at least this much to the word gap", and its one use is the WCAG 1.4.12 floor of
    0.16 em. Nearest-rounding delivers less than was asked for whenever the quotient falls
    in the lower half of a step, which on Noto Sans is exactly what happened: the Default
    preset documented itself as meeting the floor and added 0.130 em.

    Measured on the bundled families, before -> after::

        Carlito   0.226 em space   1.75 -> 1.75   +0.170 em   (was already above)
        Noto Sans 0.260 em space   1.50 -> 1.75   +0.130 -> +0.195 em
        Atkinson  0.280 em space   1.50 -> 1.75   +0.140 -> +0.210 em
        Lexend    0.280 em space   1.50 -> 1.75   +0.140 -> +0.210 em

    The step still lives in exactly one place: `settings.snap_up` takes the `Range`, and
    `resolve`'s `round_to_steps` is then a no-op on an already-on-grid value.

    It does not clamp. A target that ceilings past ``word_scale``'s maximum stays out of
    range and `validate` refuses it -- "this font's space is too narrow to add that much
    within the range" is a true answer, and quietly reducing it to the maximum would not be.
    """
    width = space_width_em(family)
    if not isinstance(width, (int, float)) or isinstance(width, bool):
        raise SpaceWidthUnavailable(
            f"space width for {family!r} is not a number: {width!r}"
        )
    width = float(width)
    if not math.isfinite(width):
        raise SpaceWidthUnavailable(f"space width for {family!r} is not finite")
    if width <= 0.0:
        raise SpaceWidthUnavailable(
            f"space width for {family!r} must be positive, got {width!r}"
        )
    return snap_up(RANGES["word_scale"], 1.0 + target_em / width)


def resolve(
    values: PresetValues,
    *,
    space_width_em: Callable[[str], float] | None = None,
) -> Configuration:
    """Turn preset intent into one concrete, canonical `Configuration` (K2).

    Steps, in order:

    1. ``target_em`` word spacing is converted to a multiplier,
       ``1 + value / space_width_em(base_family)``. ``multiplier`` values pass
       through untouched.
    2. `round_to_steps` snaps every numeric field onto its K1 lattice -- including
       word spacing onto the 0.25 step. The step is **not** restated here.
    3. `validate` checks the result. Any ``error`` raises `PresetResolutionError`;
       ``warning`` issues do not, and are left for the caller to surface.

    ``space_width_em`` is a provider ``family -> em``. It defaults to
    `default_space_width_em`, which falls back to `REFERENCE_SPACE_WIDTH_EM` for
    unmeasured families. It is only consulted in ``target_em`` mode, so a preset using
    ``multiplier`` resolves without any font metric at all.

    Warnings are not errors: a preset with ``ui_line`` above 1.3 resolves normally and
    the caller can run `validate` on the returned configuration to display the
    advisory. Nothing here clamps or silently substitutes a user's value -- out-of-range
    input raises rather than being quietly corrected.

    >>> cfg = resolve(BUILTINS[0].values)
    >>> cfg.base_family, cfg.letter_em, cfg.reading_line
    ('Noto Sans', 0.12, 1.5)
    """
    provider = default_space_width_em if space_width_em is None else space_width_em
    word = values.word

    if word.mode not in ("target_em", "multiplier"):
        raise ValueError(f"WordSpacing.mode must be 'target_em' or 'multiplier', got {word.mode!r}")
    if word.mode == "target_em":
        if not isinstance(word.value, (int, float)) or isinstance(word.value, bool):
            raise PresetResolutionError(
                _configuration(values, word.value),
                [Issue("error", "word_scale", NOT_A_NUMBER)],
            )
        if not math.isfinite(float(word.value)):
            raise PresetResolutionError(
                _configuration(values, word.value),
                [Issue("error", "word_scale", NON_FINITE)],
            )
        word_scale = _word_scale_from_target_em(
            float(word.value), values.base_family, provider
        )
    else:
        word_scale = word.value

    config = round_to_steps(_configuration(values, word_scale))
    issues = validate(config)
    errors = [i for i in issues if i.level == "error"]
    if errors:
        raise PresetResolutionError(config, errors)
    return config


def _configuration(values: PresetValues, word_scale: float) -> Configuration:
    """Assemble a `Configuration` from preset values plus an already-chosen scale."""
    return Configuration(
        base_family=values.base_family,
        letter_em=values.letter_em,
        word_scale=word_scale,
        reading_line=values.reading_line,
        ui_line=values.ui_line,
        text_scale=values.text_scale,
    )


# --------------------------------------------------------------------------- #
# The K2 UI rules
# --------------------------------------------------------------------------- #


def with_base_family(values: PresetValues, family: str) -> PresetValues:
    """Change the base font, honouring the K2 rule about word spacing. [D A9]

    "Changing the base font keeps ``multiplier`` values unchanged and recalculates
    ``target_em`` ones."

    Both halves of that rule are satisfied by storing intent and changing nothing
    else, which is why this function is a plain field replacement:

    * a ``multiplier`` resolves to the same ``word_scale`` regardless of family, so it
      is kept by construction;
    * a ``target_em`` is re-divided by the **new** family's space width on the next
      `resolve`, so it is recalculated by construction.

    This is worth stating because the tempting implementation -- converting the target
    to a multiplier here and storing that -- is wrong. It would freeze the conversion
    against the old font and silently destroy the user's em intent on the *next* font
    change. The recalculation must stay deferred to `resolve`.
    """
    return replace(values, base_family=family)


def with_word_multiplier(values: PresetValues, multiplier: float) -> PresetValues:
    """Apply a manual word-spacing edit, switching that field to ``multiplier``. [D A9]

    "A manual word-spacing edit switches that field to ``multiplier`` mode."

    The user has just expressed a relative intent by dragging a control, so the stored
    intent becomes relative too, and subsequent font changes preserve it rather than
    recomputing it from an em target the user is no longer expressing.
    """
    return replace(values, word=WordSpacing("multiplier", multiplier))


# --------------------------------------------------------------------------- #
# User and custom presets
# --------------------------------------------------------------------------- #


def new_user_preset(name: str, values: PresetValues) -> Preset:
    """Create a saved user preset with a fresh ``uuid4`` ID (K2 rule 1).

    The ID is generated, never supplied, so a user preset cannot be born holding a
    reserved ID. Storage is M3.1's responsibility; this only builds the object.
    """
    return Preset(id=str(uuid.uuid4()), name=name, values=values, kind="user")


def custom_preset(values: PresetValues, name: str = "Custom") -> Preset:
    """Wrap the user's current working values as the saved Custom entry.

    Always carries `CUSTOM_ID`, which is what makes it a single upserted row rather than
    an accumulating pile: there is one working set, and saving it again replaces it.
    Hand it to ``store.presets.save()``; ``store.presets.get(CUSTOM_ID)`` returns it on the
    next launch, to be passed to `catalogue` as its ``custom`` argument.

    Takes explicit values and has no default set: per Research2 C.4, "Custom is not a
    fixed configuration and must not be given default values". Offering it at all is
    backed by the personalisation evidence -- self-set text improved comprehension over
    pre-set text (Krivec et al. 2019); a personalised-parameter procedure outperformed
    standard settings (Lorusso et al. 2024); 58% of 94 children had a stable
    personal-best variation yielding higher comprehension accuracy (Sheppard et al.
    2023); a personalised ranking model raised predicted reading speed by 14-25 WPM
    (Cai et al. 2022). That evidence is about *personalisation working*, not about any
    particular value -- and it is also why the entry is persisted rather than dropped on
    exit: a personal best that has to be rediscovered at every launch is not one.
    """
    return Preset(id=CUSTOM_ID, name=name, values=values, kind="custom")


def catalogue(
    user_presets: tuple[Preset, ...] = (),
    custom: Preset | None = None,
) -> tuple[Preset, ...]:
    """Built-ins, then the user's saved presets, then their saved Custom entry.

    Enforces the card's "must never: let a user preset overwrite a built-in". A user
    preset that claims a reserved ID, or that declares ``kind="builtin"``, raises
    `BuiltinOverwriteError` instead of shadowing or replacing the real one. Shadowing
    would be the worse failure of the two, because the built-in would still appear in
    the list while resolving to someone else's values.

    ``custom`` is the restored working set -- typically ``store.presets.get(CUSTOM_ID)`` --
    and is passed separately rather than inside ``user_presets`` for two reasons: it
    holds a reserved id that no user preset may claim, and the store does not return it
    from ``PresetRepo.list()`` (K9 D3), because it is editing state rather than a
    catalogue entry. It is appended last so the list reads built-in → saved → "where I
    left off", and omitting it (``None``) means the user has no saved edits, not that
    they are being discarded.

    `ORIGINAL` is not included: it is a sentinel, not a preset, and is excluded from
    enumeration by contract.
    """
    seen: set[str] = set()
    for preset in user_presets:
        if preset.kind == "builtin":
            raise BuiltinOverwriteError(
                f"preset {preset.id!r} declares kind='builtin'; only BUILTINS may"
            )
        if preset.id in RESERVED_IDS:
            raise BuiltinOverwriteError(
                f"preset id {preset.id!r} is reserved and cannot be used by a"
                f" user preset (reserved: {sorted(RESERVED_IDS)})"
            )
        if preset.id in seen:
            raise BuiltinOverwriteError(f"duplicate user preset id {preset.id!r}")
        seen.add(preset.id)
    if custom is None:
        return BUILTINS + tuple(user_presets)
    _check_custom(custom)
    return BUILTINS + tuple(user_presets) + (custom,)


def _check_custom(preset: Preset) -> None:
    """The two things that make a preset the Custom row rather than something else.

    Both are checked rather than coerced. Silently relabelling a user preset as the
    working set, or a working set as a user preset, would lose one of them on the next
    save -- and the save is an upsert, so the loss would be immediate and total.
    """
    if preset.kind != "custom":
        raise BuiltinOverwriteError(
            f"the custom entry must declare kind='custom', got {preset.kind!r}"
        )
    if preset.id != CUSTOM_ID:
        raise BuiltinOverwriteError(
            f"the custom entry must carry id {CUSTOM_ID!r}, got {preset.id!r}"
        )


# --------------------------------------------------------------------------- #
# Storage -- owned by M3.1, not here
# --------------------------------------------------------------------------- #
#
# `to_record` / `from_record` lived here until 2026-11-14. They converted a saved preset to
# and from K9 D9's versioned JSON, and they worked -- but M3.1 shipped `store/serialize.py`
# with the same shape and the same fixed-decimal spelling, `PresetRepo.save()` calls that,
# and O1's pair had no caller and no test (PLAN_v2 W7). Checked before removing: the two
# produced identical `values` payloads, so nothing was lost and nothing was silently wrong.
#
# They were removed rather than tested because the duplication was the defect. Two
# implementations of one record shape, in two owners' modules, is a second authority on what
# a preset legally contains -- and the one that drifts is the one a font gets built from.
# `to_record`'s own docstring made that argument about storing a resolved `Configuration`
# beside its intent; it applies to the record shape itself.
#
# What this module still owns is the model the store serialises: `STORABLE_KINDS`,
# `RESERVED_IDS`, `CUSTOM_ID`, `is_storable` and `_check_custom`. `PresetRepo` re-states that
# policy inline next to its SQL rather than asking `is_storable`; worth reconciling, but it
# is M3.1's call and not a reason to keep a second serialiser alive here.
#
# Recoverable from git (this file, before 2026-11-14) if the decision is ever revisited.


def is_storable(preset: Preset) -> bool:
    """Whether ``preset`` is one M3.1 persists -- ``user`` or ``custom``, not a built-in.

    A predicate rather than a comment, so the store can ask this module instead of
    re-deciding the policy next to its SQL. It does not yet: `PresetRepo.save()` re-states
    the same rules inline. Kept because the policy belongs to K2 wherever it is enforced,
    and because one predicate is not the duplication problem the serialisers were -- but
    it is a loose end, and M3.1's to tie.
    """
    return preset.kind in STORABLE_KINDS


def is_original(value: object) -> bool:
    """Whether ``value`` is the `ORIGINAL` sentinel rather than a preset or ID.

    Callers short-circuit on this *before* touching `resolve`: Original has no field
    values and must never be turned into a `Configuration`.
    """
    return value == ORIGINAL


# Guard the invariant that the rest of the module depends on. Cheap, and it fails at
# import time rather than in whichever downstream module first trusted it.
assert tuple(p.id for p in BUILTINS) == BUILTIN_IDS, "BUILTINS and BUILTIN_IDS disagree"
assert all(p.kind == "builtin" for p in BUILTINS), "a non-builtin leaked into BUILTINS"
assert set(RANGES), "M1.1 ranges failed to import"
