"""WCAG 2.1 SC 1.4.12 Text Spacing — the W6.5 automated check (PLAN_v2 Phase III, W6.5).

W6.5 asks for "WCAG 2.1 SC 1.4.12 check of the presets (letter >= 0.12 em, word >= 0.16 em,
line >= 1.5x) as an automated test", and names one question O1 had to settle first:

    "O1 to define the comparison against WCAG (space-glyph growth only, or the whole gap as
     CSS letter- + word-spacing would produce) and, if needed, round target_em up instead of
     to the nearest step."

Both are settled here. The reasoning is in the tests, because that is where it has to survive.

Decision 1 — word spacing is **space-glyph growth only**
---------------------------------------------------------
`test_the_letter_delta_is_letter_spacing_not_word_spacing` is the evidence. Measured on
Noto Sans at the Default preset: "catdog" grows by 6 x L and "cat dog" grows by 6 x L + W, so
the width attributable to the **word break** is exactly W, the space glyph's growth. The letter
delta appears once per letter whether or not a space follows it; counting it toward word spacing
charges one adjustment to two criteria, and SC 1.4.12 requires "all of the following" to be set
**together**.

The generous reading is not even generous in the right direction. In CSS, `letter-spacing`
applies after *every* character including the space, so letter-spacing 0.12 + word-spacing 0.16
gives a gap of `space + 0.16 + 2 x 0.12`. ScribeSense gives `space + W + 1 x 0.12`, because the
space glyph is Unicode script Common and so excluded from `latin_glyphs` -- one L short of the
CSS the criterion is written against. Counting L toward the word floor would hide that, rather
than make up for it. (The shortfall itself is a separate finding:
`docs/research2-conformance-O1.md` 9.4.)

Decision 2 — `target_em` rounds **up**
---------------------------------------
A `target_em` means "add at least this much". Rounding to the nearest 0.25 `word_scale` step
delivered less than asked whenever the quotient fell in the lower half of a step, which is what
made the Default preset add 0.130 em while documenting itself as meeting 0.16. `settings.snap_up`
now does the conversion. Default clears the floor on all four families; nothing else moved,
because nothing else uses `target_em`.

What this file does NOT claim
-----------------------------
Passing is conformance with an **accessibility requirement**, not evidence of reading benefit.
Research2 is explicit that WCAG numbers are "accessibility requirements about reachability, not
proofs of benefit", and that letter-spacing effects are positive, null and negative depending on
population and task. A preset meeting these floors is a preset that meets these floors.
"""

from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

from scribesense.config import bundled, presets, settings
from scribesense.contracts.config import RANGES, SHORTLIST
from scribesense.fontgen import patcher

#: SC 1.4.12, as multiples of the font size. Paragraph spacing (>= 2x) is a layout property no
#: font can carry, so it is the rendering layer's and is deliberately absent here.
LETTER_FLOOR = 0.12
WORD_FLOOR = 0.16
LINE_FLOOR = 1.5

BUILTIN_IDS = ("default", "reading", "high-separation")


def _resolved(preset_id: str, family: str):  # type: ignore[no-untyped-def]
    values = dataclasses.replace(presets.builtin(preset_id).values, base_family=family)
    return presets.resolve(values)


def _word_em(config, family: str) -> float:  # type: ignore[no-untyped-def]
    """What the word-spacing mechanism adds to a word gap, in em. See Decision 1."""
    return (config.word_scale - 1.0) * bundled.space_width_em(family)


# ---- the measurement that settles Decision 1 --------------------------------------------


def test_the_letter_delta_is_letter_spacing_not_word_spacing() -> None:
    """Attribute the width growth, rather than arguing about the gap.

    If the letter delta were word spacing, removing the space would not change how much of it
    there is. It does not: both strings gain 6 x L for their six letters, and only the one with
    a space gains W as well.
    """
    config = _resolved("default", "Noto Sans")
    source = bundled.base_font_files("Noto Sans")["Regular"]
    out = Path(tempfile.mkdtemp()) / "patched.ttf"
    patcher.patch(source, config, config.reading_line, out, family_name="AccessSans-000000000000")

    with TTFont(source, lazy=True) as before, TTFont(out, lazy=True) as after:
        upm = before["head"].unitsPerEm
        cmap = before.getBestCmap()

        def width(font: TTFont, text: str) -> float:
            return sum(font["hmtx"][cmap[ord(c)]][0] for c in text) / upm

        letter_delta = patcher.letter_delta_units(config.letter_em, upm) / upm
        word_added = _word_em(config, "Noto Sans")

        no_space = width(after, "catdog") - width(before, "catdog")
        with_space = width(after, "cat dog") - width(before, "cat dog")

        assert no_space == pytest.approx(6 * letter_delta, abs=1e-3)
        assert with_space == pytest.approx(6 * letter_delta + word_added, abs=1e-3)
        # The word break accounts for W and nothing more.
        assert with_space - no_space == pytest.approx(word_added, abs=1e-3)


def test_scribesense_gives_one_letter_delta_less_gap_than_css_would() -> None:
    """Why the generous reading would hide a shortfall instead of excusing one.

    CSS applies `letter-spacing` after every character, the space included. ScribeSense cannot:
    the space glyph is script Common, so `latin_glyphs` excludes it and it receives no tracking.
    At identical nominal settings the ScribeSense gap is therefore exactly one L narrower.
    """
    config = _resolved("default", "Noto Sans")
    space_em = bundled.space_width_em("Noto Sans")
    letter_delta = config.letter_em
    word_added = _word_em(config, "Noto Sans")

    css_gap = space_em + WORD_FLOOR + 2 * letter_delta
    scribesense_gap = space_em + word_added + 1 * letter_delta
    assert scribesense_gap < css_gap

    source = bundled.base_font_files("Noto Sans")["Regular"]
    with TTFont(source, lazy=True) as font:
        from scribesense.config.rules import latin_glyphs

        assert font.getBestCmap()[0x20] not in latin_glyphs(font), "the space gets no tracking"


# ---- what SC 1.4.12 actually requires: reachability --------------------------------------


@pytest.mark.parametrize("family", SHORTLIST)
def test_every_floor_is_reachable_on_the_k1_grid(family: str) -> None:
    """The conformance claim proper.

    SC 1.4.12 is about what the user can *set*: "no loss of content or functionality occurs by
    setting all of the following". That is a property of K1's ranges, not of any preset, and it
    is the thing ScribeSense can honestly claim to conform to.
    """
    assert RANGES["letter_em"].max >= LETTER_FLOOR, family
    assert RANGES["reading_line"].max >= LINE_FLOOR, family
    reachable_word = (RANGES["word_scale"].max - 1.0) * bundled.space_width_em(family)
    assert reachable_word >= WORD_FLOOR, (family, reachable_word)


@pytest.mark.parametrize("family", SHORTLIST)
def test_every_floor_sits_on_a_step_or_below_one(family: str) -> None:
    """Reachable is not enough if the grid steps over the value.

    `letter_em` 0.12 is on the 0.02 grid exactly. `word_scale` is a factor, so the floor lands
    between steps and `snap_up` is what guarantees a preset can land on or above it -- which is
    the whole of Decision 2, asserted rather than assumed.
    """
    assert settings.is_on_step(RANGES["letter_em"], LETTER_FLOOR), "0.12 is on the 0.02 grid"
    assert settings.is_on_step(RANGES["reading_line"], LINE_FLOOR), "1.5 is on the 0.1 grid"

    space_em = bundled.space_width_em(family)
    exact = 1.0 + WORD_FLOOR / space_em
    snapped = settings.snap_up(RANGES["word_scale"], exact)
    assert snapped >= exact
    assert (snapped - 1.0) * space_em >= WORD_FLOOR, family


# ---- the presets, which is what W6.5 asked for -------------------------------------------


@pytest.mark.parametrize("family", SHORTLIST)
def test_the_default_preset_meets_all_three_floors(family: str) -> None:
    """Default is the preset that *claims* WCAG anchoring, so it is the one held to it.

    K2's table gives it "0.12 em / target ~= +0.16 em / 1.5x" -- the three SC 1.4.12 numbers.
    Before Decision 2 it missed the word floor on three of the four families.
    """
    config = _resolved("default", family)
    assert config.letter_em >= LETTER_FLOOR, family
    assert config.reading_line >= LINE_FLOOR, family
    assert _word_em(config, family) >= WORD_FLOOR, (family, _word_em(config, family))


@pytest.mark.parametrize("family", SHORTLIST)
def test_high_separation_meets_all_three_floors(family: str) -> None:
    """It does not claim them, but it clears them comfortably on every family."""
    config = _resolved("high-separation", family)
    assert config.letter_em >= LETTER_FLOOR
    assert config.reading_line >= LINE_FLOOR
    assert _word_em(config, family) >= WORD_FLOOR, family


@pytest.mark.parametrize("family", SHORTLIST)
def test_reading_meets_the_letter_and_line_floors_but_not_the_word_floor(family: str) -> None:
    """**A finding, pinned as current behaviour. For the team, not for O1 alone.**

    Reading's word spacing is K2's `multiplier` 1.5 -- a relative intent, not a target -- so
    Decision 2 does not touch it. On every bundled family it adds 0.113-0.140 em, below the
    0.16 em floor.

    It is not a broken promise: K2's table gives Reading "0.12 em / 1.5x / 1.8x" and claims no
    WCAG anchor on the word field. But after Decision 2 the result is odd enough to raise --
    **the Default preset now provides more word spacing than Reading does**, on all four
    families, which is not what the names suggest. Research2's own Reading preset used +0.16 em.

    Changing it means changing K2's frozen built-in table, which is the team's call and not
    O1's alone; the M1.2 card's acceptance test is "values match the K2 table". Asserted as-is
    so that a decision to change it is visible here.
    """
    config = _resolved("reading", family)
    assert config.letter_em >= LETTER_FLOOR, family
    assert config.reading_line >= LINE_FLOOR, family

    reading_word = _word_em(config, family)
    assert reading_word < WORD_FLOOR, (family, reading_word)
    assert reading_word < _word_em(_resolved("default", family), family), (
        f"{family}: Reading adds less word spacing than Default"
    )


# ---- Decision 2, isolated -----------------------------------------------------------------


def test_a_target_em_is_a_floor_and_is_never_rounded_below_it() -> None:
    """`snap_up` against `snap`, on the case that motivated it.

    1 + 0.16/0.26 = 1.615. Nearest gives 1.50 and adds 0.130 em; up gives 1.75 and adds
    0.195 em. The second is more than asked for, which a floor permits; the first is less,
    which it does not.
    """
    grid = RANGES["word_scale"]
    exact = 1.0 + WORD_FLOOR / 0.26
    assert settings.snap(grid, exact) == 1.5 and (1.5 - 1.0) * 0.26 < WORD_FLOOR
    assert settings.snap_up(grid, exact) == 1.75 and (1.75 - 1.0) * 0.26 >= WORD_FLOOR


def test_snap_up_leaves_a_value_already_on_the_grid_alone() -> None:
    """Float noise must not push an exact value a whole step up."""
    grid = RANGES["word_scale"]
    for position in settings.positions(grid):
        assert settings.snap_up(grid, position) == position, position


def test_snap_up_does_not_clamp() -> None:
    """K1 rule 1 holds for the new helper too: out of range stays out, and `validate` reports it.

    A target too large for the font's space must be refused, not quietly reduced to the maximum
    -- the user would otherwise be told they got what they asked for.
    """
    grid = RANGES["word_scale"]
    assert settings.snap_up(grid, 3.1) > grid.max
    config = dataclasses.replace(
        _resolved("default", "Noto Sans"), word_scale=settings.snap_up(grid, 3.1)
    )
    assert [i.code for i in settings.validate(config) if i.field == "word_scale"] == [
        settings.OUT_OF_RANGE
    ]


@pytest.mark.parametrize("preset_id", BUILTIN_IDS)
@pytest.mark.parametrize("family", SHORTLIST)
def test_every_builtin_still_resolves_and_validates_after_the_change(
    preset_id: str, family: str
) -> None:
    """Decision 2 must not have pushed any built-in off the grid or out of range."""
    config = _resolved(preset_id, family)
    assert [i for i in settings.validate(config) if i.level == "error"] == []
    assert settings.round_to_steps(config) == config
