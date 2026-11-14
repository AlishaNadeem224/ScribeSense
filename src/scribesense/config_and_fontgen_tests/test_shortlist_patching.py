"""M1.4 -- "Done when: tests pass for all 4 shortlist fonts" (ARCHITECTURE.md M1.4).

O3's M1.4 review (2026-10-18) approved the patcher but could not close its "Done when" condition:
*"The 'Done when: tests pass for all 4 shortlist fonts' condition cannot be met yet: `fonts/` is
empty, so only synthetic fixtures are tested; CFF, hinting and real kerning are not demonstrated."*

All four families are bundled now, so this file asserts the card's acceptance criteria against the
**real** fonts rather than against `tests/unit/fontgen/conftest.py`'s synthetic ones:

    "Measured advance and space width match config; Devanagari glyphs unchanged; no `liga`
     feature; TrueType and CFF fonts handled; variable fonts flattened per weight"

`tests/unit/fontgen/test_bundled_fonts.py` already proves every family *builds* end to end. What
it asserts is the outcome (`status == "built"`), which reaches the spacing criteria only through
M1.6. These tests assert them directly on the patched bytes, so a regression is attributed to M1.4
rather than surfacing as a validation failure two modules away.

What bundling did and did not close
-----------------------------------
**Closed, and newly meaningful:** Carlito is **UPM 2048** where the other three are 1000. Research2
E.3 warns that "any test asserting a fixed delta must be parameterised by unitsPerEm", and E.2.1's
worked example is exactly 0.12 em at 2048 units = +246. Until Carlito was bundled that path existed
only in a fixture; it is now a real font in the shortlist.

**Not closed:** every bundled family is TrueType with static outlines, so the card's "CFF" and
"variable fonts flattened per weight" criteria still rest on the synthetic fonts in
`tests/unit/fontgen/test_fontgen.py` (`test_measured_advances_match_the_configuration[a.otf]` does
cover CFF patching -- it is *real-font* CFF coverage that is missing). The last two tests here are
tripwires: they assert that no bundled family is CFF or variable, so that bundling one later fails
the suite and prompts a re-check instead of silently widening what is claimed.

Hinting and real kerning remain undemonstrated, as O3 noted; both are M1.6 rendering concerns.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

from scribesense.config import bundled, presets
from scribesense.config.rules import latin_glyphs
from scribesense.contracts.config import Configuration, SHORTLIST
from scribesense.fontgen import patcher

SYNTHETIC_FAMILY = "AccessSans-000000000000"

#: unitsPerEm per family. Carlito's 2048 is the reason this is worth pinning: the same
#: `letter_em` produces a different integer delta, which is Research2 E.3's warning.
UNITS_PER_EM = {
    "Carlito": 2048,
    "Atkinson Hyperlegible": 1000,
    "Lexend": 1000,
    "Noto Sans": 1000,
}


def _default_for(family: str) -> Configuration:
    return presets.resolve(
        dataclasses.replace(presets.builtin("default").values, base_family=family)
    )


def _advances(path: Path) -> tuple[dict[str, int], dict[int, str], int, int, int]:
    """``(advance by glyph, cmap, unitsPerEm, ascent, descent)`` for one font file."""
    with TTFont(path, lazy=True) as font:
        hmtx = font["hmtx"]
        return (
            {name: hmtx[name][0] for name in font.getGlyphOrder()},
            dict(font.getBestCmap()),
            font["head"].unitsPerEm,
            font["hhea"].ascent,
            font["hhea"].descent,
        )


@pytest.mark.parametrize("family", SHORTLIST)
def test_units_per_em_is_what_the_delta_is_computed_against(family: str) -> None:
    """Research2 E.3: a fixed delta assertion is only meaningful beside its UPM."""
    path = bundled.base_font_files(family)["Regular"]
    with TTFont(path, lazy=True) as font:
        assert font["head"].unitsPerEm == UNITS_PER_EM[family], family


def test_the_shortlist_covers_both_conventional_upm_values() -> None:
    """The reason Carlito earns its place in this file as well as in the dropdown.

    Research2 E.3 lists 2048 as the TrueType convention and 1000 as the CFF one; a shortlist with
    only one of them cannot catch a delta computed against a hard-coded em.
    """
    assert set(UNITS_PER_EM.values()) == {1000, 2048}


@pytest.mark.parametrize("family", SHORTLIST)
def test_every_shortlist_family_meets_the_m14_acceptance_criteria(
    family: str, tmp_path: Path
) -> None:
    """The card's acceptance test, run on every style of every bundled family."""
    config = _default_for(family)
    line = config.reading_line

    for style, source in sorted(bundled.base_font_files(family).items()):
        out = tmp_path / f"{family}-{style}.ttf"
        patcher.patch(source, config, line, out, family_name=SYNTHETIC_FAMILY)

        before, cmap, upm, ascent, descent = _advances(source)
        after, _after_cmap, after_upm, after_ascent, after_descent = _advances(out)
        where = (family, style)

        assert after_upm == upm, where  # the em must not move, or every delta is meaningless
        assert (after_ascent, after_descent) == (ascent, descent), where

        # "Measured advance ... matches config", for every Latin glyph, at this font's own UPM.
        delta = patcher.letter_delta_units(config.letter_em, upm)
        assert delta == round(config.letter_em * upm) and delta > 0, where
        latin = latin_glyphs(TTFont(source, lazy=True))
        assert latin, where
        for glyph in sorted(latin):
            assert after[glyph] == max(0, before[glyph] + delta), (where, glyph)

        # "... and space width match config".
        space = cmap[patcher.SPACE_CODEPOINT]
        assert after[space] == round(before[space] * config.word_scale), where

        # "Devanagari glyphs unchanged" -- generalised: every advance outside the Latin set and
        # the space glyph is byte-identical. Research2 D.5 makes this a hard invariant, not a
        # Devanagari-specific one, and three of these families carry no Devanagari at all.
        untouched = sorted(set(before) - latin - {space})
        assert untouched, where
        for glyph in untouched:
            assert after[glyph] == before[glyph], (where, glyph)

        with TTFont(out, lazy=True) as font:
            # "no `liga` feature"
            gsub = font.get("GSUB")
            table = getattr(gsub, "table", None)
            feature_list = getattr(table, "FeatureList", None)
            tags = (
                {r.FeatureTag for r in feature_list.FeatureRecord}
                if feature_list is not None
                else set()
            )
            assert not (tags & patcher.LIGATURE_FEATURES), (where, sorted(tags))

            # Line height: the box is the requested ratio, except where the font's own A + D
            # already exceeds it, which `line_gap_units` clamps to a zero gap (M1.4 documents
            # this, and Research2 K-28 holds line height to be the application's anyway).
            hhea = font["hhea"]
            box = hhea.ascent - hhea.descent + hhea.lineGap
            assert box == max(round(line * upm), ascent - descent), where

            assert font["name"].getDebugName(1) == SYNTHETIC_FAMILY, where


@pytest.mark.parametrize("family", SHORTLIST)
def test_patching_a_real_family_is_byte_deterministic(family: str, tmp_path: Path) -> None:
    """M1.5's must-never -- "produce two different outputs for the same inputs" -- on real fonts.

    Synthetic fixtures already cover it, but they do not carry the tables real fonts do (`DSIG`,
    `gasp`, hinting programs), any of which could pick up a timestamp or an iteration order.
    """
    config = _default_for(family)
    source = bundled.base_font_files(family)["Regular"]
    first, second = tmp_path / "first.ttf", tmp_path / "second.ttf"
    for out in (first, second):
        patcher.patch(source, config, config.reading_line, out, family_name=SYNTHETIC_FAMILY)
    assert first.read_bytes() == second.read_bytes(), family


@pytest.mark.parametrize("family", SHORTLIST)
def test_zero_spacing_leaves_every_advance_identical(family: str, tmp_path: Path) -> None:
    """Research2 I.4: "a zero-spacing configuration must build successfully".

    `letter_em` 0.00 with `word_scale` 1.0 is reachable on K1's grid, and must be a no-op on
    advances -- spacing is an option, not an assumption. (Line metrics and the rename still apply:
    those are not spacing.)
    """
    config = dataclasses.replace(_default_for(family), letter_em=0.0, word_scale=1.0)
    source = bundled.base_font_files(family)["Regular"]
    out = tmp_path / "neutral.ttf"
    patcher.patch(source, config, config.reading_line, out, family_name=SYNTHETIC_FAMILY)

    before, _cmap, _upm, _a, _d = _advances(source)
    after, *_ = _advances(out)
    assert after == before, family


# ---- tripwires: what bundling did NOT close ------------------------------------------------


@pytest.mark.parametrize("family", SHORTLIST)
def test_no_bundled_family_is_cff(family: str) -> None:
    """Records that the card's "CFF fonts handled" rests on a synthetic font, not the shortlist.

    If a CFF family is ever bundled this fails, which is the point: the CFF charstring path would
    then be exercised by real data and the M1.4 card's note should be updated to say so.
    """
    with TTFont(bundled.base_font_files(family)["Regular"], lazy=True) as font:
        assert "glyf" in font and not ("CFF " in font or "CFF2" in font), family


@pytest.mark.parametrize("family", SHORTLIST)
def test_no_bundled_family_is_variable(family: str) -> None:
    """Same, for "variable fonts flattened per weight".

    Lexend's *upstream* is variable; `fonts/tools/build_lexend.py` flattens it to static Regular
    and Bold before bundling, so the patcher's `instancer` path never runs for the shortlist.
    """
    with TTFont(bundled.base_font_files(family)["Regular"], lazy=True) as font:
        assert "fvar" not in font, family
