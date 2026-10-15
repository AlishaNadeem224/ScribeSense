"""M1.1 settings, M1.2 presets + bundled fonts, M1.3 eligibility — card acceptance tests."""

from __future__ import annotations

import math
import shutil
import uuid
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

from scribesense.config import bundled, presets, rules, settings
from scribesense.contracts import config as k1
from scribesense.contracts import presets as k2
from scribesense.contracts.config import Configuration, Issue
from scribesense.contracts.errors import ErrorCode, ScribeSenseError


def cfg(**kw) -> Configuration:  # type: ignore[no-untyped-def]
    base = dict(base_family="Noto Sans", letter_em=0.12, word_scale=1.5, reading_line=1.5, ui_line=1.2,
                text_scale=1.0)
    return Configuration(**{**base, **kw})


# ---- M1.1 ----------------------------------------------------------------------------------------

def test_vocabulary_is_the_k1_contract_not_a_copy() -> None:
    assert settings.Configuration is k1.Configuration and settings.Issue is k1.Issue
    assert settings.RANGES is k1.RANGES and settings.SHORTLIST is k1.SHORTLIST


@pytest.mark.parametrize("field", list(k1.RANGES))
def test_every_position_is_canonical_and_valid(field: str) -> None:
    r = k1.RANGES[field]
    pos = settings.positions(r)
    assert pos[0] == r.min and pos[-1] == r.max and len(pos) == settings.count(r)
    for v in pos:
        c = cfg(**{field: v})
        assert settings.round_to_steps(c) == c
        assert not [i for i in settings.validate(c) if i.level == "error"]


def test_rounding_follows_k1_rule_1_and_is_idempotent() -> None:
    c = settings.round_to_steps(cfg(letter_em=0.131, word_scale=1.6, text_scale=1.0749))
    assert (c.letter_em, c.word_scale, c.text_scale) == (0.14, 1.5, 1.05)
    assert settings.round_to_steps(c) == c
    # K1 writes Python's round(): an exact tie goes to the even step.
    assert settings.snap(k1.RANGES["letter_em"], 0.01) == 0.0
    assert settings.snap(k1.RANGES["letter_em"], 0.03) == 0.04
    assert math.copysign(1, settings.snap(k1.RANGES["letter_em"], -0.001)) == 1.0  # never -0.0


def test_out_of_range_is_never_clamped() -> None:
    c = settings.round_to_steps(cfg(letter_em=0.5, word_scale=0.5))
    assert (c.letter_em, c.word_scale) == (0.5, 0.5)
    codes = {(i.field, i.code) for i in settings.validate(c)}
    assert {("letter_em", "OUT_OF_RANGE"), ("word_scale", "OUT_OF_RANGE")} <= codes


def test_ui_line_above_recommended_is_a_warning_not_an_error() -> None:
    assert settings.validate(cfg(ui_line=1.4)) == [Issue("warning", "ui_line", "UI_LINE_ABOVE_RECOMMENDED")]
    assert settings.validate(cfg(ui_line=1.3)) == []


def test_bad_values_are_reported_not_coerced() -> None:
    c = cfg(base_family="Comic Sans", letter_em="0.1", word_scale=True, reading_line=float("nan"), ui_line=None)
    r = settings.round_to_steps(c)  # passes non-numbers and NaN through unchanged
    assert (r.letter_em, r.word_scale, r.ui_line) == ("0.1", True, None) and math.isnan(r.reading_line)
    assert settings.validate(c) == [
        Issue("error", "base_family", "UNKNOWN_FONT"),
        Issue("error", "letter_em", "NOT_A_NUMBER"),
        Issue("error", "word_scale", "NOT_A_NUMBER"),
        Issue("error", "reading_line", "NON_FINITE"),
        Issue("error", "ui_line", "NOT_A_NUMBER"),
    ]
    assert settings.validate(cfg(letter_em=0.13)) == [Issue("error", "letter_em", "NOT_ON_STEP")]
    assert {settings.NOT_A_NUMBER, settings.NON_FINITE} <= {c.value for c in k1.IssueCode}


# ---- M1.2 ----------------------------------------------------------------------------------------

K2_TABLE = {  # the K2 contract table, verbatim
    "default": ("Noto Sans", 0.12, k2.WordSpacing("target_em", 0.16), 1.5, 1.2, 1.0),
    "reading": ("Noto Sans", 0.12, k2.WordSpacing("multiplier", 1.5), 1.8, 1.2, 1.0),
    "high-separation": ("Noto Sans", 0.20, k2.WordSpacing("multiplier", 2.5), 1.6, 1.2, 1.0),
}


def test_builtins_match_the_k2_table_and_types() -> None:
    assert tuple(p.id for p in presets.BUILTINS) == k2.BUILTIN_IDS
    for p in presets.BUILTINS:
        assert isinstance(p, k2.Preset) and isinstance(p.values, k2.PresetValues) and p.kind == "builtin"
        assert k2.PresetValues(*K2_TABLE[p.id]) == p.values


def test_builtins_resolve_and_validate() -> None:
    for p in presets.BUILTINS:
        c = presets.resolve(p.values, space_width_em=lambda _f: 0.26)
        assert not [i for i in settings.validate(c) if i.level == "error"]
    # Default: 1 + 0.16 / 0.26 = 1.615 -> 1.5 on the 0.25 grid.
    assert presets.resolve(presets.BUILTINS[0].values, space_width_em=lambda _f: 0.26).word_scale == 1.5


def test_target_em_is_recalculated_on_font_change_multiplier_is_kept() -> None:
    widths = {"Noto Sans": 0.26, "Lexend": 0.16}
    target = presets.with_base_family(presets.builtin("default").values, "Lexend")
    assert presets.resolve(target, space_width_em=widths.__getitem__).word_scale == 2.0  # 1 + 0.16/0.16
    kept = presets.with_base_family(presets.builtin("reading").values, "Lexend")
    assert presets.resolve(kept, space_width_em=widths.__getitem__).word_scale == 1.5
    edited = presets.with_word_multiplier(target, 1.75)
    assert edited.word == k2.WordSpacing("multiplier", 1.75)


def test_original_is_not_a_preset_and_ids_are_protected() -> None:
    assert presets.ORIGINAL == k2.ORIGINAL and presets.is_original(k2.ORIGINAL)
    assert k2.ORIGINAL not in {p.id for p in presets.catalogue()}
    user = presets.new_user_preset("Mine", presets.builtin("reading").values)
    assert user.kind == "user" and uuid.UUID(user.id).version == 4
    assert presets.catalogue((user,))[-1] is user
    for bad_id in (*k2.BUILTIN_IDS, presets.CUSTOM_ID, k2.ORIGINAL):
        with pytest.raises(presets.BuiltinOverwriteError):
            presets.catalogue((k2.Preset(bad_id, "x", user.values, "user"),))
    assert presets.custom_preset(user.values).kind == "custom"


def test_unknown_word_mode_is_rejected() -> None:
    values = k2.PresetValues("Noto Sans", 0.12, k2.WordSpacing("em", 0.1), 1.5, 1.2, 1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        presets.resolve(values)


def test_base_font_files_and_space_width_come_from_the_bundled_tree(tmp_path: Path, make_font,  # type: ignore[no-untyped-def]
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bundled, "FONTS_ROOT", tmp_path / "fonts")
    monkeypatch.setattr(presets, "MEASURED_SPACE_WIDTH_EM", {})
    with pytest.raises(ScribeSenseError) as exc:
        presets.base_font_files("Lexend")
    assert exc.value.code is ErrorCode.BUNDLED_FONT_MISSING and str(exc.value) == "BUNDLED_FONT_MISSING"
    assert presets.space_width_source("Lexend") == "fallback"
    assert presets.default_space_width_em("Lexend") == presets.REFERENCE_SPACE_WIDTH_EM

    family = tmp_path / "fonts" / "Lexend"
    family.mkdir(parents=True)
    shutil.copy(make_font(), family / "Lexend-Regular.ttf")
    shutil.copy(make_font("b.ttf"), family / "Lexend-Bold.ttf")
    assert set(presets.base_font_files("Lexend")) == {"Regular", "Bold"}
    assert presets.default_space_width_em("Lexend") == pytest.approx(0.26)  # fixture space 260/1000
    assert presets.space_width_source("Lexend") == "measured"


def test_fonts_root_is_the_repository_fonts_dir() -> None:
    assert bundled.FONTS_ROOT == Path(__file__).resolve().parents[3] / "fonts"


# ---- M1.3 ----------------------------------------------------------------------------------------

def test_eligibility(make_font, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    assert rules.is_eligible(make_font()).ok
    assert rules.is_eligible(make_font("c.otf", cff=True)).ok
    assert rules.is_eligible(make_font("m.ttf", mono=True)).reason_code == "BASE_IS_MONOSPACE"
    assert rules.is_eligible(make_font("i.ttf", icon=True)).reason_code == "BASE_IS_ICON_FONT"
    (tmp_path / "bad.ttf").write_bytes(b"not a font")
    assert rules.is_eligible(tmp_path / "bad.ttf").reason_code == "FONT_UNREADABLE"
    assert rules.is_eligible(tmp_path / "missing.ttf").reason_code == "FONT_UNREADABLE"


def test_only_latin_letters_are_in_the_spacing_set(make_font) -> None:  # type: ignore[no-untyped-def]
    with TTFont(make_font()) as font:
        glyphs = rules.latin_glyphs(font)
    assert {"uni0061", "uni0057", "uni00E9"} <= glyphs
    assert not {"uni0915", "space", "uni0031", "uni002C"} & glyphs  # Devanagari, Common script
