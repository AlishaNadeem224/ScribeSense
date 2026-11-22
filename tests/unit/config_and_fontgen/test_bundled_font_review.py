"""M1.2 -- O1's review of the bundled base fonts (PLAN_v2 W7; K2 rule 2).

K2 rule 2 puts one duty on O1 personally: *"Before bundling, O1 verifies each licence permits
redistribution and modification."* Carlito, Atkinson Hyperlegible and Lexend were bundled by O4
on O1's behalf (2026-10-18) and marked "for O1 to review". These tests are that review, written
down so it stays true rather than being a one-off check in a terminal.

What `tests/unit/fontgen/test_bundled_fonts.py` already covers is not repeated here: manifest
SHA-256 values, licence-file presence, per-family eligibility, that every family builds. This file
adds the three things that review found uncovered **on the real bundled fonts**:

1. The manifest's ``reserved_font_name`` field was dead documentation -- no test read it. The
   existing check looks for the *family* name (``family.split()[0]``), which happens to cover
   today's four, but would miss a font whose RFN differs from its family name (a plausible future
   import: "Source Sans 3" reserves "Source").
2. The existing primary-name check uses ``(1, 4, 6, 16, 17)`` and so **omits record 3**
   (``uniqueID``) -- which is the one record every bundled source demonstrably carries the family
   name in (``'1.104;tyPL;Carlito-Bold'``). The patcher rewrites it correctly and M1.6 would refuse
   a leak, but nothing pinned it for the real fonts.
3. K7 rule 4's ``[V] check each bundled family's actual files`` -- Lexend's missing italic.

Verdict recorded in PLAN_v2 (2026-11-14): **approved**. Nothing here failed; the tests exist so a
future font swap cannot quietly break what was checked.
"""

from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

from scribesense.config import bundled, presets
from scribesense.config.rules import is_eligible
from scribesense.contracts.config import Configuration, SHORTLIST
from scribesense.fontgen import patcher, variants

MANIFEST = tomllib.loads(
    (bundled.fonts_root() / bundled.MANIFEST_NAME).read_text(encoding="utf-8")
)

IDENTIFYING_NAME_IDS = (1, 3, 4, 6, 16, 17, 21, 22, 25)
"""Every name record that can identify a family, matching M1.6's `_IDENTIFYING_NAME_IDS`.

Deliberately wider than `test_bundled_fonts.PRIMARY_NAME_IDS`. Record 3 is the live case: the
patcher *rewrites* it (it is not in M1.4's `_NAME_IDS_DROPPED`), and it is the only record in
which all four bundled sources actually carry their family name. Records 16/17/21/22/25 are
absent from every bundled source today, so for this shortlist the drop-list is inert and record 3
is doing the work.
"""

#: The styles each family ships. Lexend's upstream has no italic (K7 rule 4 [V], R2 partial styles).
EXPECTED_STYLES = {
    "Carlito": {"Regular", "Bold", "Italic", "BoldItalic"},
    "Atkinson Hyperlegible": {"Regular", "Bold", "Italic", "BoldItalic"},
    "Lexend": {"Regular", "Bold"},
    "Noto Sans": {"Regular", "Bold", "Italic", "BoldItalic"},
}

#: Measured from the bundled Regular faces, and the `word_scale` the Default preset's
#: "+0.16 em" target resolves to on each. Recorded in ARCHITECTURE.md M1.2.
SPACE_WIDTH_EM = {
    "Carlito": 0.226,
    "Atkinson Hyperlegible": 0.280,
    "Lexend": 0.280,
    "Noto Sans": 0.260,
}


def _default_for(family: str) -> Configuration:
    """The Default preset resolved against one family -- how every preset reaches a build."""
    values = dataclasses.replace(presets.builtin("default").values, base_family=family)
    return presets.resolve(values)


def _squash(text: str) -> str:
    return "".join(text.split()).casefold()


def _identifying_names(path: Path) -> dict[int, str]:
    with TTFont(path, lazy=True) as font:
        table = font["name"]
        return {
            nid: value
            for nid in IDENTIFYING_NAME_IDS
            if (value := table.getDebugName(nid))
        }


# ---- K2 rule 2: the licence duty --------------------------------------------------------


@pytest.mark.parametrize("family", SHORTLIST)
def test_the_licence_permits_modification_and_redistribution(family: str) -> None:
    """K2 rule 2's actual requirement, checked against the licence text, not the SPDX id.

    "OFL-1.1" in a manifest is a claim; the grant is in the file. The OFL's permission clause
    names both verbs this project needs, because ScribeSense ships a *modified* copy.
    """
    entry = MANIFEST[family]
    assert entry["licence"] == "OFL-1.1", family
    text = (bundled.fonts_root() / entry["licence_file"]).read_text(
        encoding="utf-8", errors="replace"
    )
    assert "SIL Open Font License, Version 1.1" in text
    grant = text.casefold()
    assert "modify" in grant and "redistribute" in grant, family


@pytest.mark.parametrize("family", SHORTLIST)
def test_the_declared_reserved_font_name_never_reaches_a_generated_font(
    family: str, tmp_path: Path
) -> None:
    """OFL clause 3, enforced from the manifest's own declaration.

    Two of the four reserve a name: Carlito reserves "Carlito", Lexend reserves
    "RevReading Lexend". A Reserved Font Name may not appear in a modified version's name, which
    is exactly what M1.4's rename exists for. Reading the declaration rather than guessing from
    the family name is the point -- it makes the manifest field load-bearing instead of a comment,
    so a future font whose RFN differs from its family cannot slip past.
    """
    reserved = MANIFEST[family]["reserved_font_name"]
    if reserved is False:
        pytest.skip(f"{family} declares no Reserved Font Name")

    config = _default_for(family)
    needle = _squash(reserved)
    for path in bundled.base_font_files(family).values():
        out = tmp_path / f"rfn-{path.name}"
        patcher.patch(
            path, config, config.reading_line, out, family_name="AccessSans-000000000000"
        )
        for nid, value in _identifying_names(out).items():
            assert needle not in _squash(value), (family, path.name, nid, value)


# ---- the record-3 gap -------------------------------------------------------------------


@pytest.mark.parametrize("family", SHORTLIST)
def test_record_3_is_where_the_source_family_actually_hides(family: str) -> None:
    """Documents *why* the wider record set matters, by asserting the premise.

    If a future upstream stops putting the family in ``uniqueID``, this test fails and the
    reasoning above can be revisited -- rather than the wider check quietly becoming
    decoration nobody can justify.
    """
    for style, path in bundled.base_font_files(family).items():
        unique_id = _identifying_names(path).get(3)
        assert unique_id, (family, style, "no uniqueID record")
        assert _squash(family.split()[0]) in _squash(unique_id), (family, style, unique_id)


@pytest.mark.parametrize("family", SHORTLIST)
def test_no_identifying_record_of_a_generated_font_names_the_source(
    family: str, tmp_path: Path
) -> None:
    """M1.4's must-never -- "keep the original family name" -- across all nine records.

    `tests/unit/fontgen/test_bundled_fonts.py` checks (1, 4, 6, 16, 17) on the real fonts and
    `test_patcher_drops_every_record_that_could_name_the_source_family` checks the full set on a
    synthetic one. Neither checks the full set on the fonts that actually ship.
    """
    config = _default_for(family)
    needle = _squash(family.split()[0])
    for style, path in bundled.base_font_files(family).items():
        out = tmp_path / f"name-{path.name}"
        patcher.patch(
            path, config, config.reading_line, out, family_name="AccessSans-000000000000"
        )
        records = _identifying_names(out)
        assert records.get(1) == "AccessSans-000000000000", (family, style)
        for nid, value in records.items():
            assert needle not in _squash(value), (family, style, nid, value)


# ---- K7 rule 4: partial styles ------------------------------------------------------------


@pytest.mark.parametrize("family", SHORTLIST)
def test_the_manifest_records_exactly_the_styles_the_pipeline_finds(family: str) -> None:
    found = bundled.base_font_files(family)
    assert set(found) == EXPECTED_STYLES[family], family
    assert set(MANIFEST[family]["files"]) == set(found), family
    assert bundled.REQUIRED_STYLE in found, family


def test_lexend_ships_without_italic_and_that_is_not_an_error() -> None:
    """K7 rule 4's ``[V] check each bundled family's actual files``, for the one that differs.

    Lexend upstream ships a variable font with no italic axis, so the bundled set is Regular +
    Bold. R2 allows partial styles; what must not happen is a refusal, or a synthesised italic.
    """
    files = bundled.base_font_files("Lexend")
    assert set(files) == {"Regular", "Bold"}
    assert "Italic" not in files and "BoldItalic" not in files
    config = _default_for("Lexend")
    assert variants.plan(config).styles == ("Regular", "Bold")


@pytest.mark.parametrize("family", SHORTLIST)
def test_every_bundled_face_is_eligible(family: str) -> None:
    """M1.3 is the gate M1.4 depends on; a bundled font that fails it could never build."""
    for style, path in bundled.base_font_files(family).items():
        verdict = is_eligible(path)
        assert verdict.ok, (family, style, verdict.reason_code)
        # ITALIC_ONLY on an italic face of a complete family is noise, not a defect -- it is
        # per-face by construction. Recorded as a finding in docs/research2-conformance-O1.md 9.6.
        assert set(verdict.warnings) <= {"ITALIC_ONLY"}, (family, style, verdict.warnings)


# ---- what the measured metrics mean for the presets ----------------------------------------


@pytest.mark.parametrize("family", SHORTLIST)
def test_the_space_width_each_family_resolves_with_is_the_measured_one(family: str) -> None:
    """`resolve()` divides by this, so a font swap silently moves every preset's word spacing.

    The SHA-256 test catches the file changing; this catches the *consequence* being different
    from what ARCHITECTURE.md M1.2 records.
    """
    assert presets.space_width_source(family) == "measured", family
    assert bundled.space_width_em(family) == pytest.approx(SPACE_WIDTH_EM[family], abs=5e-4)


# The per-family Default resolution and its WCAG standing now live in
# `test_wcag_text_spacing.py`, which owns the W6.5 decision that changed them.
