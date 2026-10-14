"""M1.2 -- Presets and preset resolution (contract K2, owner O1).

A preset stores the user's **intent**; `resolve` turns it into one concrete
`Configuration` (K1). Only the resolved `Configuration` is used for building fonts --
there is never a second authoritative value. [D A9, A10]

This module owns the built-in values and the resolution rules. It does **not** own the
configuration vocabulary: every range, step, tolerance and validation rule is imported
from `config.settings` (M1.1). No bound and no validation logic is restated here.

Persistence is M3.1's job -- nothing in this module reads or writes storage. It is
platform-independent: stdlib imports only, no paths, no display-server logic. The
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
    The transient working configuration the user is currently editing -- the "Custom"
    entry in a preset list. Per Research2 C.4, Custom "is not a fixed configuration
    and must not be given default values", so `custom_preset` requires explicit
    values and this module defines no Custom constant.

`ORIGINAL` is **not** a preset at all. See its docstring.

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
from dataclasses import dataclass, replace
from typing import Callable, Literal

from config.settings import (
    NON_FINITE,
    NOT_A_NUMBER,
    RANGES,
    Configuration,
    Issue,
    round_to_steps,
    validate,
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
    "BuiltinOverwriteError",
    "Preset",
    "PresetResolutionError",
    "PresetValues",
    "SpaceWidthUnavailable",
    "WordSpacing",
    "builtin",
    "catalogue",
    "custom_preset",
    "default_space_width_em",
    "is_original",
    "new_user_preset",
    "resolve",
    "space_width_source",
    "with_base_family",
    "with_word_multiplier",
]


# --------------------------------------------------------------------------- #
# Sentinel and identifiers
# --------------------------------------------------------------------------- #

ORIGINAL = "original"
"""Sentinel meaning "revert everything" -- **not a preset**. [D]

Per K2 and Research2 C.5, `ORIGINAL` selects the unmodified source font binary and
bypasses the transformation pipeline entirely. It is therefore:

* excluded from preset enumeration -- it never appears in `BUILTINS` or `catalogue`;
* excluded from preset validation -- it has no field values to validate;
* implemented as a short-circuit by the caller, which returns the original font,
  **not** as a `Configuration` with all fields set to neutral values.

That last distinction is the one that matters and the one most likely to be
implemented wrongly. Zeroing the six fields is *not* equivalent to Original: a
neutral-valued configuration still goes through the pipeline, which may substitute a
different family or apply baseline normalisation. Original must not reach `resolve`.

Its purpose is to be the honest comparison baseline for user testing, and an escape
hatch if a generated variant misbehaves.
"""

DEFAULT_ID = "default"
READING_ID = "reading"
HIGH_SEPARATION_ID = "high-separation"

BUILTIN_IDS: frozenset[str] = frozenset({DEFAULT_ID, READING_ID, HIGH_SEPARATION_ID})
"""The three fixed built-in IDs (K2 rule 1). Names are display-only; IDs are not."""

CUSTOM_ID = "custom"
"""ID of the transient Custom entry. Reserved so a saved preset cannot take it."""

RESERVED_IDS: frozenset[str] = BUILTIN_IDS | {CUSTOM_ID, ORIGINAL}
"""IDs a user preset may never claim.

Enforces the card's "must never: let a user preset overwrite a built-in", and extends
the same protection to the Custom slot and the Original sentinel, which would be just
as broken to shadow.
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

Real sans-serif space widths sit right on top of that boundary, so the Default
preset's resolved word spacing is genuinely undetermined until the bundled fonts are
measured. See `MEASURED_SPACE_WIDTH_EM`.
"""

MEASURED_SPACE_WIDTH_EM: dict[str, float] = {}
"""Real space advances in em, per family, measured from the **bundled** font files.

Deliberately empty. [V] Populate it from the Regular face of each bundled family once
K2's ``base_font_files()`` and the ``fonts/`` tree exist (neither does yet -- see the
module report). Values must be measured, never estimated: ``space_advance / unitsPerEm``
for the ``space`` glyph of the Regular face.

Until then every family falls back to `REFERENCE_SPACE_WIDTH_EM`, which means the
``target_em`` machinery is structurally correct but numerically provisional, and
`with_base_family` cannot yet produce a different result per family.
"""


def default_space_width_em(family: str) -> float:
    """Space advance in em for ``family``: measured if known, else the fallback.

    This is the default provider used by `resolve`. Inject a different one (for a real
    font-metric reader, or for a test) via ``resolve(..., space_width_em=...)``.
    """
    return MEASURED_SPACE_WIDTH_EM.get(family, REFERENCE_SPACE_WIDTH_EM)


def space_width_source(family: str) -> Literal["measured", "fallback"]:
    """Whether `default_space_width_em` would return a real measurement or a guess.

    Exists so the UI and the report can be honest about it: a `target_em` preset
    resolved from a fallback width should not be presented as a precise value.
    """
    return "measured" if family in MEASURED_SPACE_WIDTH_EM else "fallback"


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WordSpacing:
    """How a preset expresses word spacing (K2).

    Two modes, and the difference is the whole point of storing intent rather than a
    resolved number:

    ``"target_em"``
        ``value`` is the extra space, in em, the user wants added to the word gap.
        Because different families have different native space widths, this is
        **recalculated whenever the base font changes** -- the stored em target stays
        put and the resolved multiplier moves.
    ``"multiplier"``
        ``value`` is a factor on the font's own space advance; 1.0 means unchanged.
        This is **kept as-is across a font change**, preserving the user's relative
        intent.

    Research2 classes the family→word-spacing dependency (row P-BF → P-WS) as
    HYP/DIR, enforcement WARN, confidence **Low**, and its change-impact rule R-07 is
    marked *Provisional*: "preserve letter_em (relative); re-derive word_scale only if
    multiplicative". The K2 UI rule implemented here is consistent with that, but it
    is an architectural choice, not an evidence-backed one.
    """

    mode: Literal["target_em", "multiplier"]
    value: float

    def __post_init__(self) -> None:
        if self.mode not in ("target_em", "multiplier"):
            raise ValueError(
                f"WordSpacing.mode must be 'target_em' or 'multiplier', got {self.mode!r}"
            )


@dataclass(frozen=True)
class PresetValues:
    """All six fields, always present (K2) [D A10].

    Mirrors `Configuration` except that ``word`` holds intent (`WordSpacing`) rather
    than the resolved ``word_scale``. Like `Configuration`, it carries **no defaults**:
    the Default preset in `BUILTINS` is the only source of default values.
    """

    base_family: str
    letter_em: float
    word: WordSpacing
    reading_line: float
    ui_line: float
    text_scale: float


@dataclass(frozen=True)
class Preset:
    """A named set of `PresetValues` (K2).

    ``id`` is the stable identity -- fixed strings for built-ins, ``str(uuid4())`` for
    user presets. ``name`` is for display only: it may repeat and may change.
    """

    id: str
    name: str
    values: PresetValues
    kind: Literal["builtin", "user", "custom"]


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
    """Convert an em target to a multiplier: ``1 + target_em / space_width``.

    Returns the raw multiplier; snapping to K1's 0.25 step happens in `resolve`, via
    `round_to_steps`, so the step lives in exactly one place.
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
    return 1.0 + target_em / width


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
    """Wrap the user's current working values as the transient Custom entry.

    Takes explicit values and has no default set: per Research2 C.4, "Custom is not a
    fixed configuration and must not be given default values". Offering it at all is
    backed by the personalisation evidence -- self-set text improved comprehension over
    pre-set text (Krivec et al. 2019); a personalised-parameter procedure outperformed
    standard settings (Lorusso et al. 2024); 58% of 94 children had a stable
    personal-best variation yielding higher comprehension accuracy (Sheppard et al.
    2023); a personalised ranking model raised predicted reading speed by 14-25 WPM
    (Cai et al. 2022). That evidence is about *personalisation working*, not about any
    particular value.
    """
    return Preset(id=CUSTOM_ID, name=name, values=values, kind="custom")


def catalogue(user_presets: tuple[Preset, ...] = ()) -> tuple[Preset, ...]:
    """Built-ins followed by the user's saved presets.

    Enforces the card's "must never: let a user preset overwrite a built-in". A user
    preset that claims a reserved ID, or that declares ``kind="builtin"``, raises
    `BuiltinOverwriteError` instead of shadowing or replacing the real one. Shadowing
    would be the worse failure of the two, because the built-in would still appear in
    the list while resolving to someone else's values.

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
    return BUILTINS + tuple(user_presets)


def is_original(value: object) -> bool:
    """Whether ``value`` is the `ORIGINAL` sentinel rather than a preset or ID.

    Callers short-circuit on this *before* touching `resolve`: Original has no field
    values and must never be turned into a `Configuration`.
    """
    return value == ORIGINAL


# Guard the invariant that the rest of the module depends on. Cheap, and it fails at
# import time rather than in whichever downstream module first trusted it.
assert {p.id for p in BUILTINS} == BUILTIN_IDS, "BUILTINS and BUILTIN_IDS disagree"
assert all(p.kind == "builtin" for p in BUILTINS), "a non-builtin leaked into BUILTINS"
assert set(RANGES), "M1.1 ranges failed to import"
