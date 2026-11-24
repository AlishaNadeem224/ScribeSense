"""O3, K9 addition — built_in_presets / preset_adjustment_limit tables (draft amendment to K1/K2,
pending O1 sign-off: docs/K1_K2_preset_adjustment_draft.md). Verifies the 7 seeded presets, the 5
confirmed adjustment-limit rows, NULL handling, idempotent seeding, and the per-preset word-spacing
warning exception (Phase 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scribesense.store import Store

PRESET_ROWS = {
    "neutral-baseline": ("Neutral Baseline", "platform_sans", 0.00, 0.00, 1.40, 1.40, 1.00),
    "wcag-text-spacing": ("WCAG Text Spacing", "eligible_sans", 0.12, 0.16, 1.50, 1.40, 1.00),
    "high-separation-crowding": ("High Separation", "eligible_sans", 0.18, 0.24, 1.60, 1.50, 1.00),
    "word-spacing-first": ("Word Spacing First", "eligible_sans", 0.00, 0.27, 1.40, 1.40, 1.00),
    "low-vision-magnification": ("Low Vision Magnification", "eligible_sans", 0.12, 0.16, 1.50, 1.40, 1.60),
    "older-adult-comfort": ("Visual Comfort Ceiling", "eligible_sans", 0.00, 0.04, 1.20, 1.20, 1.00),
    "read-along-support": ("Read-Along Support", "eligible_sans", 0.12, 0.16, 1.50, 1.40, 1.00),
}

LIMIT_ROWS = {
    "letter_em": (0.00, 0.25, 0.01, None, 0.18),
    "word_spacing_em": (0.00, 0.32, 0.01, None, 0.27),
    "reading_line": (1.20, 2.00, 0.05, 1.50, 1.75),
    "ui_line": (1.20, 1.80, 0.05, None, None),
    "text_scale": (0.50, 2.00, 0.05, None, 1.60),
}


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "db" / "scribesense.db")


# ---------------------------------------------------------------- built_in_presets

def test_all_seven_presets_are_seeded_with_exact_values(store: Store) -> None:
    rows = store.built_in_presets.list_all()
    assert {r.preset_id for r in rows} == set(PRESET_ROWS)
    for r in rows:
        name, base_family_rule, letter_em, word_spacing_em, reading_line, ui_line, text_scale = PRESET_ROWS[r.preset_id]
        assert (r.name, r.base_family_rule, r.letter_em, r.word_spacing_em, r.reading_line, r.ui_line, r.text_scale) == (
            name, base_family_rule, letter_em, word_spacing_em, reading_line, ui_line, text_scale)


def test_get_returns_a_single_preset_or_none(store: Store) -> None:
    p = store.built_in_presets.get("high-separation-crowding")
    assert p is not None and (p.letter_em, p.word_spacing_em) == (0.18, 0.24)
    assert store.built_in_presets.get("not-a-preset") is None


def test_preset_id_is_the_primary_key(store: Store) -> None:
    with pytest.raises(Exception):  # sqlite3.IntegrityError, mapped by the store's own guard elsewhere
        store.execute(
            "INSERT INTO built_in_presets (preset_id, name, base_family_rule, letter_em, word_spacing_em, "
            "reading_line, ui_line, text_scale) VALUES ('neutral-baseline','x','platform_sans',0,0,1.4,1.4,1.0)")


def test_reopening_does_not_duplicate_preset_rows(tmp_path: Path) -> None:
    db = tmp_path / "db" / "scribesense.db"
    Store(db).close()
    store = Store(db)
    assert store.fetchone("SELECT COUNT(*) FROM built_in_presets")[0] == 7


# ---------------------------------------------------------------- preset_adjustment_limit

def test_all_five_confirmed_limit_rows_are_seeded_with_exact_values(store: Store) -> None:
    rows = store.preset_adjustment_limit.list_all()
    assert {r.field_name for r in rows} == set(LIMIT_ROWS)
    for r in rows:
        min_allowed, max_allowed, step, warn_below, warn_above = LIMIT_ROWS[r.field_name]
        assert (r.min_allowed, r.max_allowed, r.step, r.warn_below, r.warn_above) == (
            min_allowed, max_allowed, step, warn_below, warn_above)


def test_the_two_unresolved_fields_are_not_present(store: Store) -> None:
    """The task claims 8 adjustable fields; only 5 are verified anywhere in this codebase. Nothing is
    invented for the other 2 — asserting the table stays at exactly 5 rows."""
    assert store.fetchone("SELECT COUNT(*) FROM preset_adjustment_limit")[0] == 5


def test_null_warn_thresholds_round_trip_as_none(store: Store) -> None:
    ui_line = store.preset_adjustment_limit.get("ui_line")
    assert ui_line is not None and ui_line.warn_below is None and ui_line.warn_above is None
    letter_em = store.preset_adjustment_limit.get("letter_em")
    assert letter_em is not None and letter_em.warn_below is None and letter_em.warn_above == 0.18


def test_why_that_stop_matches_the_task_text_exactly(store: Store) -> None:
    row = store.preset_adjustment_limit.get("text_scale")
    assert row is not None
    assert row.why_that_stop == "1.6x is where horizontal scrolling and overlap begin — the display-breaking point."


def test_field_name_is_the_primary_key(store: Store) -> None:
    with pytest.raises(Exception):
        store.execute(
            "INSERT INTO preset_adjustment_limit (field_name, min_allowed, max_allowed, step, why_that_stop) "
            "VALUES ('letter_em', 0, 1, 0.1, 'dup')")


def test_reopening_does_not_duplicate_limit_rows(tmp_path: Path) -> None:
    db = tmp_path / "db" / "scribesense.db"
    Store(db).close()
    store = Store(db)
    assert store.fetchone("SELECT COUNT(*) FROM preset_adjustment_limit")[0] == 5


# ---------------------------------------------------------------- per-preset word-spacing warning (Phase 3)

@pytest.mark.parametrize("preset_id,expected", [
    ("neutral-baseline", 0.00),
    ("wcag-text-spacing", 0.16),
    ("high-separation-crowding", 0.24),
    ("word-spacing-first", 0.27),
    ("low-vision-magnification", 0.16),
    ("older-adult-comfort", 0.04),
    ("read-along-support", 0.16),
])
def test_word_spacing_warn_below_is_the_selected_presets_original_value(store: Store, preset_id: str, expected: float) -> None:
    assert store.preset_adjustment_limit.word_spacing_warn_below(preset_id) == expected


def test_word_spacing_warn_below_is_none_for_an_unknown_preset(store: Store) -> None:
    assert store.preset_adjustment_limit.word_spacing_warn_below("not-a-preset") is None


def test_word_spacing_warn_below_static_row_is_null(store: Store) -> None:
    """The static table never carries a single word_spacing_em lower threshold (it's per-preset)."""
    row = store.preset_adjustment_limit.get("word_spacing_em")
    assert row is not None and row.warn_below is None and row.warn_above == 0.27
