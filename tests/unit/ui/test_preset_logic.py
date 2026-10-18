"""M2.5 — preset editor logic and preview loading (K1, K2, K7 rules 1-2).

The editor's pure helpers (snapping, K2 edits, preset rows, Custom / save-as) are
tested without GTK: preset_editor.py imports GTK lazily, so this part runs even where
PyGObject is missing. The preview tests (private font map, temp_dir cleanup, the
latest-only worker) need PyGObject + the GTK 4 typelibs but no display, and are
skipped when those are not installed.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

import pytest

from scribesense.config.presets import BUILTINS, PresetResolutionError, resolve
from scribesense.config.settings import is_on_step, positions
from scribesense.contracts.config import RANGES, SHORTLIST, UI_LINE_RECOMMENDED_MAX
from scribesense.contracts.fonts import PreviewFonts
from scribesense.contracts.presets import Preset, PresetValues, WordSpacing
from scribesense.ui import preset_editor as pe

DEFAULT, READING, HIGH = BUILTINS


def _values(**over: object) -> PresetValues:
    base = dict(
        base_family="Noto Sans",
        letter_em=0.12,
        word=WordSpacing("multiplier", 1.5),
        reading_line=1.8,
        ui_line=1.2,
        text_scale=1.0,
    )
    base.update(over)
    return PresetValues(**base)  # type: ignore[arg-type]


def _user(name: str, values: PresetValues | None = None, id_: str | None = None,
          kind: str = "user") -> Preset:
    return Preset(id=id_ or str(uuid.uuid4()), name=name, values=values or _values(),
                  kind=kind)  # type: ignore[arg-type]


# --- controls match RANGES (acceptance) --------------------------------------

def test_controls_are_generated_from_ranges_only() -> None:
    assert list(pe.FIELD_LABELS) == list(RANGES)


def test_slider_digits_show_the_full_step() -> None:
    assert pe.slider_digits("letter_em") == 2
    assert pe.slider_digits("word_scale") == 2  # 1.25 must not show as 1.2
    assert pe.slider_digits("reading_line") == 1
    assert pe.slider_digits("text_scale") == 2


@pytest.mark.parametrize(
    ("field", "raw", "snapped"),
    [("letter_em", 0.135, 0.14), ("word_scale", 1.4, 1.5), ("text_scale", 1.23, 1.25),
     ("reading_line", 1.44, 1.4)],
)
def test_snap_value_puts_raw_slider_values_on_the_k1_lattice(
    field: str, raw: float, snapped: float
) -> None:
    assert pe.snap_value(field, raw) == snapped


@pytest.mark.parametrize("field", list(RANGES))
def test_shown_value_equals_resolved_value_for_every_position(field: str) -> None:
    for position in positions(RANGES[field]):
        values = pe.edit_value(READING.values, field, position)
        assert pe.control_values(values)[field] == position
        assert getattr(resolve(values), field) == position


def test_off_lattice_drag_is_resolved_and_shown_as_the_same_value() -> None:
    values = pe.edit_value(READING.values, "letter_em", 0.135)
    assert values.letter_em == 0.14
    assert pe.control_values(values)["letter_em"] == resolve(values).letter_em == 0.14


def test_control_values_are_resolved_and_on_step() -> None:
    shown = pe.control_values(DEFAULT.values)
    config = resolve(DEFAULT.values)
    for field, rng in RANGES.items():
        assert shown[field] == getattr(config, field)
        assert is_on_step(rng, shown[field])


def test_control_values_fall_back_to_snapped_values_when_unresolvable() -> None:
    shown = pe.control_values(_values(letter_em=0.5))
    assert shown["letter_em"] == RANGES["letter_em"].max  # displayed inside the control
    assert shown["word_scale"] == 1.5


# --- K2 rules -----------------------------------------------------------------

def test_word_spacing_edit_switches_to_multiplier() -> None:
    assert DEFAULT.values.word.mode == "target_em"
    edited = pe.edit_value(DEFAULT.values, "word_scale", 2.1)
    assert edited.word == WordSpacing("multiplier", 2.0)


def test_other_edits_keep_the_target_em_intent() -> None:
    edited = pe.edit_value(DEFAULT.values, "letter_em", 0.2)
    assert edited.word == DEFAULT.values.word
    assert edited.letter_em == 0.2


def test_edit_value_rejects_unknown_fields() -> None:
    with pytest.raises(KeyError):
        pe.edit_value(DEFAULT.values, "base_family", 1.0)


def test_base_font_change_keeps_multiplier_and_target_em_intent() -> None:
    family = SHORTLIST[0]
    multiplier = pe.edit_base_family(HIGH.values, family)
    assert multiplier.base_family == family and multiplier.word == HIGH.values.word
    target = pe.edit_base_family(DEFAULT.values, family)
    assert target.word == DEFAULT.values.word  # recalculated at resolve, not frozen
    assert resolve(target).base_family == family


def test_base_font_must_be_on_the_shortlist() -> None:
    with pytest.raises(ValueError):
        pe.edit_base_family(DEFAULT.values, "Comic Sans MS")


def test_word_label_marks_converted_values() -> None:
    assert pe.word_label(DEFAULT.values).endswith("≈")
    assert not pe.word_label(READING.values).endswith("≈")


# --- slider events that change nothing (no Custom switch, no rebuild) -------------

def test_word_drag_inside_the_current_snap_cell_is_not_an_edit() -> None:
    assert READING.values.word == WordSpacing("multiplier", 1.5)
    for raw in (1.5, 1.52, 1.55, 1.58, 1.6):  # all snap back to 1.5
        assert not pe.slider_edit_changes(READING.values, "word_scale", raw)
    assert pe.slider_edit_changes(READING.values, "word_scale", 1.7)


def test_word_drag_onto_the_shown_value_in_target_em_mode_switches_to_multiplier() -> None:
    shown = pe.control_values(DEFAULT.values)["word_scale"]
    assert DEFAULT.values.word.mode == "target_em"
    assert pe.slider_edit_changes(DEFAULT.values, "word_scale", shown)  # K2 intent switch


def test_other_fields_snapping_back_are_not_edits() -> None:
    assert not pe.slider_edit_changes(READING.values, "letter_em", 0.121)
    assert not pe.slider_edit_changes(READING.values, "reading_line", 1.82)
    assert pe.slider_edit_changes(READING.values, "letter_em", 0.2)


# --- validate() warnings / errors ------------------------------------------------

def test_ui_line_above_recommended_shows_the_warning_but_apply_is_allowed() -> None:
    ev = pe.evaluate(_values(ui_line=1.5))
    assert 1.5 > UI_LINE_RECOMMENDED_MAX
    assert ev.config is not None and not ev.errors
    assert pe.warning_text(ev.warnings) == pe.UI_LINE_WARNING


def test_no_warning_at_or_below_recommended() -> None:
    ev = pe.evaluate(_values(ui_line=1.3))
    assert ev.config is not None
    assert pe.warning_text(ev.warnings) is None


def test_errors_block_apply() -> None:
    ev = pe.evaluate(_values(letter_em=0.5))
    assert ev.config is None
    assert [i.field for i in ev.errors] == ["letter_em"]
    assert "Letter spacing" in pe.error_text(ev.errors)


# --- preset list ------------------------------------------------------------------

def test_preset_rows_order_builtins_user_custom_original() -> None:
    mine = _user("Mine")
    rows = pe.preset_rows([mine])
    assert [r.preset for r in rows[:3]] == list(BUILTINS)
    assert rows[3].preset == mine
    assert [r.kind for r in rows[4:]] == ["custom", "original"]
    assert rows[-1].preset is None  # Original is not a preset (K2)


def test_preset_rows_skip_presets_that_would_shadow_builtins() -> None:
    good = _user("Good")
    rows = pe.preset_rows([
        _user("Evil", id_="default"),
        _user("Custom slot", id_="custom"),
        _user("Fake", kind="builtin"),
        good,
        _user("Dup", id_=good.id),
    ])
    assert [r.preset for r in rows if r.kind == "preset"] == [*BUILTINS, good]


def test_unchanged_preset_is_applied_as_itself() -> None:
    assert pe.preset_to_apply(READING, READING.values) is READING


def test_edited_values_are_applied_as_custom() -> None:
    edited = pe.edit_value(READING.values, "letter_em", 0.2)
    preset = pe.preset_to_apply(READING, edited)
    assert preset.kind == "custom" and preset.values == edited
    assert pe.preset_to_apply(None, READING.values).kind == "custom"


# --- save as ------------------------------------------------------------------------

def test_save_as_builds_a_valid_user_preset() -> None:
    values = pe.edit_value(READING.values, "word_scale", 2.25)
    preset = pe.make_user_preset("  My   reading ", values)
    assert preset.kind == "user" and preset.name == "My reading"
    assert str(uuid.UUID(preset.id)) == preset.id
    assert preset.values == values
    resolve(preset.values)  # postcondition: passes validate()


def test_save_as_refuses_empty_names_and_invalid_values() -> None:
    with pytest.raises(ValueError):
        pe.make_user_preset("   ", READING.values)
    with pytest.raises(PresetResolutionError):
        pe.make_user_preset("Bad", _values(letter_em=0.5))


# --- preview (needs PyGObject + typelibs, no display) -------------------------------

def _preview_module():
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gtk", "4.0")
        gi.require_version("PangoCairo", "1.0")
    except ValueError:
        pytest.skip("GTK 4 / PangoCairo typelibs not installed")
    from scribesense.ui import preview

    return preview


def _tiny_font(path: Path, family: str) -> str:
    """A minimal valid TTF whose family is ``family`` (no system fonts needed)."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder([".notdef", "space", "a"])
    fb.setupCharacterMap({32: "space", 97: "a"})
    pen = TTGlyphPen(None)
    pen.moveTo((100, 0))
    pen.lineTo((100, 500))
    pen.lineTo((400, 500))
    pen.closePath()
    box = pen.glyph()
    fb.setupGlyf({".notdef": box, "space": TTGlyphPen(None).glyph(), "a": box})
    fb.setupHorizontalMetrics({".notdef": (500, 100), "space": (250, 0), "a": (500, 100)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": family, "styleName": "Regular"})
    fb.setupOS2(sTypoAscender=800, usWinAscent=800, usWinDescent=200)
    fb.setupPost()
    fb.save(str(path))
    return str(path)


def _families(font_map) -> set[str]:  # type: ignore[no-untyped-def]
    return {f.get_name() for f in font_map.list_families()}


def test_preview_font_resolves_only_in_the_private_font_map(tmp_path: Path) -> None:
    preview = _preview_module()
    from gi.repository import Pango, PangoCairo

    family = "AccessSans-0123456789ab"
    path = _tiny_font(tmp_path / "AccessSans-0123456789ab-Regular.ttf", family)
    font_map = preview.private_font_map((path,))
    assert font_map is not PangoCairo.FontMap.get_default()
    font = font_map.create_context().load_font(Pango.FontDescription.from_string(f"{family} 12"))
    assert font.describe().get_family() == family  # "preview shows sample in the new font"
    assert family not in _families(PangoCairo.FontMap.get_default())


def test_private_font_map_refuses_unloadable_files(tmp_path: Path) -> None:
    preview = _preview_module()
    from scribesense.contracts.errors import ErrorCode, ScribeSenseError

    bad = tmp_path / "broken.ttf"
    bad.write_bytes(b"not a font")
    with pytest.raises(ScribeSenseError) as exc:
        preview.private_font_map((str(bad),))
    assert exc.value.code is ErrorCode.PREVIEW_UNAVAILABLE
    with pytest.raises(ScribeSenseError):
        preview.private_font_map(())


def _fake_preview(root: Path, name: str) -> PreviewFonts:
    temp = root / name
    temp.mkdir()
    (temp / "f.ttf").write_bytes(b"x")
    return PreviewFonts("AccessSans-x", "AccessSansUI-x", (str(temp / "f.ttf"),),
                        ("Regular",), str(temp))


def test_close_preview_deletes_temp_dir(tmp_path: Path) -> None:
    preview = _preview_module()
    fonts = _fake_preview(tmp_path, "p1")
    preview.close_preview(fonts)
    assert not Path(fonts.temp_dir).exists()
    preview.close_preview(None)  # no-op


def test_error_text_shows_a_code_never_exception_text() -> None:
    preview = _preview_module()
    from scribesense.contracts.errors import ErrorCode, ScribeSenseError

    assert "PREVIEW_UNAVAILABLE" in preview.error_text(ScribeSenseError(ErrorCode.PREVIEW_UNAVAILABLE))
    assert "/home/secret" not in preview.error_text(OSError("/home/secret/file"))


def test_latest_only_builder_builds_newest_and_closes_stale(tmp_path: Path) -> None:
    preview = _preview_module()
    posted: list[tuple] = []
    delivered: list[tuple] = []
    done = threading.Event()
    release_first = threading.Event()
    started_first = threading.Event()
    built: list[str] = []

    def post(fn, *args):  # stands in for GLib.idle_add; run on the "main thread" later
        posted.append((fn, args))
        if len(posted) == 2:
            done.set()

    builder = preview.LatestOnlyBuilder(lambda r, e, c: delivered.append((r, e, c)), post=post)

    def job(name: str, block: bool = False):
        def run() -> PreviewFonts:
            if block:
                started_first.set()
                assert release_first.wait(5)
            built.append(name)
            return _fake_preview(tmp_path, name)
        return run

    builder.submit(job("first", block=True), 1.0)
    assert started_first.wait(5)
    builder.submit(job("second"), 1.1)  # replaced before it starts
    builder.submit(job("third"), 1.2)
    release_first.set()
    assert done.wait(5)

    for fn, args in posted:  # main-thread delivery
        fn(*args)

    assert built == ["first", "third"]  # coalesced: "second" never built
    assert not (tmp_path / "first").exists()  # stale result closed immediately
    assert len(delivered) == 1
    result, error, context = delivered[0]
    assert error is None and context == 1.2 and Path(result.temp_dir).name == "third"


def test_latest_only_builder_cancel_makes_in_flight_result_stale(tmp_path: Path) -> None:
    preview = _preview_module()
    posted: list[tuple] = []
    done = threading.Event()
    delivered: list[object] = []

    def post(fn, *args):
        posted.append((fn, args))
        done.set()

    builder = preview.LatestOnlyBuilder(lambda *a: delivered.append(a), post=post)
    builder.submit(lambda: _fake_preview(tmp_path, "only"), 1.0)
    builder.cancel()  # e.g. widget unrealized / Original selected
    assert done.wait(5)
    for fn, args in posted:
        fn(*args)
    assert delivered == []
    assert not (tmp_path / "only").exists()


def test_latest_only_builder_delivers_errors(tmp_path: Path) -> None:
    preview = _preview_module()
    from scribesense.contracts.errors import ErrorCode, ScribeSenseError

    posted: list[tuple] = []
    done = threading.Event()
    delivered: list[tuple] = []

    def post(fn, *args):
        posted.append((fn, args))
        done.set()

    def failing() -> PreviewFonts:
        raise ScribeSenseError(ErrorCode.PREVIEW_UNAVAILABLE)

    builder = preview.LatestOnlyBuilder(lambda r, e, c: delivered.append((r, e, c)), post=post)
    builder.submit(failing, 1.0)
    assert done.wait(5)
    for fn, args in posted:
        fn(*args)
    (result, error, _ctx), = delivered
    assert result is None and isinstance(error, ScribeSenseError)


def test_latest_only_builder_shutdown_closes_results_not_yet_taken(tmp_path: Path) -> None:
    """App quit: a result posted to a main loop that never runs again is still closed."""
    preview = _preview_module()
    posted: list[tuple] = []
    done = threading.Event()

    def post(fn, *args):
        posted.append((fn, args))
        done.set()

    delivered: list[object] = []
    builder = preview.LatestOnlyBuilder(lambda *a: delivered.append(a), post=post)
    builder.submit(lambda: _fake_preview(tmp_path, "posted"), (1.0, 1.5))
    assert done.wait(5)
    builder.shutdown()
    assert not (tmp_path / "posted").exists()
    for fn, args in posted:  # a late main-loop run must not deliver or double-close
        fn(*args)
    assert delivered == []
    builder.submit(lambda: _fake_preview(tmp_path, "after"), (1.0, 1.5))
    assert not (tmp_path / "after").exists()  # nothing is built after shutdown


def test_latest_only_builder_shutdown_during_a_build_closes_its_result(tmp_path: Path) -> None:
    preview = _preview_module()
    started = threading.Event()
    release = threading.Event()
    posted: list[tuple] = []

    def slow() -> PreviewFonts:
        started.set()
        assert release.wait(5)
        return _fake_preview(tmp_path, "slow")

    builder = preview.LatestOnlyBuilder(lambda *a: None, post=lambda *a: posted.append(a))
    builder.submit(slow, (1.0, 1.5))
    assert started.wait(5)
    threading.Timer(0.05, release.set).start()
    builder.shutdown(timeout=5)  # waits for the worker, which closes the result itself
    assert not (tmp_path / "slow").exists()
    assert posted == []


def _layout_height(preview, path: str, family: str, reading_line: float | None,
                   text_scale: float = 1.0) -> int:
    from gi.repository import Pango, PangoCairo

    ctx = preview.private_font_map((path,)).create_context()
    PangoCairo.context_set_resolution(ctx, 96)
    ctx.set_font_description(Pango.FontDescription.from_string("Sans 12"))
    layout = Pango.Layout.new(ctx)
    layout.set_attributes(preview.sample_attributes(ctx, family, text_scale, reading_line))
    layout.set_text("a a\na a\na a\na a", -1)
    return layout.get_size()[1]


def test_sample_line_height_follows_reading_line(tmp_path: Path) -> None:
    """The preview shows line spacing (GtkLabel/Pango ignore the font's lineGap)."""
    preview = _preview_module()
    family = "AccessSans-line00000000"
    path = _tiny_font(tmp_path / "line.ttf", family)
    h15 = _layout_height(preview, path, family, 1.5)
    h18 = _layout_height(preview, path, family, 1.8)
    assert h18 > h15
    # 4 lines at reading_line x 12 pt @ 96 dpi (16 px): the line box is set exactly.
    from gi.repository import Pango

    assert h15 == 4 * round(1.5 * 16 * Pango.SCALE)
    assert h18 == 4 * round(1.8 * 16 * Pango.SCALE)
    # text_scale scales the line height with the text.
    assert _layout_height(preview, path, family, 1.5, text_scale=2.0) == 2 * h15


def test_font_size_units_converts_points_with_the_context_resolution() -> None:
    preview = _preview_module()
    from gi.repository import Pango, PangoCairo

    ctx = PangoCairo.FontMap.new().create_context()
    PangoCairo.context_set_resolution(ctx, 96)
    ctx.set_font_description(Pango.FontDescription.from_string("Sans 12"))
    assert preview.font_size_units(ctx) == 16 * Pango.SCALE
    desc = Pango.FontDescription.from_string("Sans")
    desc.set_absolute_size(20 * Pango.SCALE)
    ctx.set_font_description(desc)
    assert preview.font_size_units(ctx) == 20 * Pango.SCALE
    attrs = preview.sample_attributes(ctx, "X", 1.0, None)  # no reading_line: no line attr
    kinds = {a.klass.type for a in attrs.get_attributes()}
    assert Pango.AttrType.LINE_HEIGHT not in kinds and Pango.AttrType.ABSOLUTE_LINE_HEIGHT not in kinds
