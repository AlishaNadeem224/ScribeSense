"""M2.5 — Presets, adjust and preview screen (K1, K2, K7 rules 1-2).

Pick a preset, adjust values, see the fixed sample paragraph, then press Apply.

- Preset list: built-ins, the user's saved presets (via M3.1, injected as callables),
  Custom (the working values once they differ from a preset), and "Original (revert
  all)". Original is NOT a preset (K2 sentinel): Apply calls ``on_original()``.
- Controls are generated only from K1 ``RANGES``; every slider value is snapped with
  `config.settings.snap`, so the value shown is the value resolved and applied.
- K2 rules: a manual word-spacing edit switches that field to ``multiplier``
  (`with_word_multiplier`); the base-font dropdown (K1 ``SHORTLIST``) uses
  `with_base_family` (target_em recalculated at resolve, multiplier kept). Other edits
  leave the word-spacing intent alone.
- `validate()` warnings are shown (UI line above 1.3×); errors block Apply.
- A failed preview shows a message but Apply stays enabled (card failure rule). The
  preview is built off the main thread by `PreviewWidget`.
- Must never apply anything without the user pressing Apply.

The pure logic (snapping, value edits, list rows, Custom / save-as construction) lives
in module-level functions that do not need GTK, so it is unit-tested without a display
(tests/unit/ui/test_preset_logic.py).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

from scribesense.config.presets import (
    BUILTINS,
    BuiltinOverwriteError,
    PresetResolutionError,
    SpaceWidthUnavailable,
    catalogue,
    custom_preset,
    new_user_preset,
    resolve,
    with_base_family,
    with_word_multiplier,
)
from scribesense.config.settings import decimals_of, snap, validate
from scribesense.contracts.config import (
    RANGES,
    SHORTLIST,
    Configuration,
    Issue,
    IssueCode,
)
from scribesense.contracts.fonts import PreviewFonts
from scribesense.contracts.presets import Preset, PresetValues

# --------------------------------------------------------------------------- #
# Pure logic (no GTK)
# --------------------------------------------------------------------------- #

#: Field labels for the generated controls. Keys must equal RANGES (checked in tests).
FIELD_LABELS: dict[str, str] = {
    "letter_em": "Letter spacing (em)",
    "word_scale": "Word spacing (× space)",
    "reading_line": "Line spacing, reading (×)",
    "ui_line": "Line spacing, menus (×)",
    "text_scale": "Text size (×)",
}

UI_LINE_WARNING = "UI line spacing above 1.3×: menus in some apps may cut off text."
ORIGINAL_MESSAGE = (
    "Original restores every app to how it was before ScribeSense. "
    "There is nothing to preview."
)


def snap_value(field: str, value: float) -> float:
    """Snap a raw control value onto the K1 lattice of ``field`` (K1 rule 1, no clamp)."""
    return snap(RANGES[field], value)


def slider_digits(field: str) -> int:
    """Decimal places a control must show so 1.25 is not displayed as 1.2."""
    return decimals_of(RANGES[field])


def edit_value(values: PresetValues, field: str, raw: float) -> PresetValues:
    """Apply one slider edit to the working values (K2 UI rules).

    ``word_scale`` is a manual word-spacing edit and switches to ``multiplier`` mode;
    every other field is replaced alone, so a letter-spacing edit keeps a target_em
    word intent.
    """
    if field not in RANGES:
        raise KeyError(field)
    value = snap_value(field, raw)
    if field == "word_scale":
        return with_word_multiplier(values, value)
    return replace(values, **{field: value})


def slider_edit_changes(values: PresetValues, field: str, raw: float) -> bool:
    """True when a slider event at ``raw`` really changes the working values.

    A drag that snaps back onto the value already shown is not an edit (no Custom
    switch, no new preview build). For ``word_scale`` the comparison is on the edited
    values, so a drag in ``target_em`` mode that lands on the shown value still counts:
    it switches the intent to ``multiplier`` (K2), while a drag inside the current snap
    cell in ``multiplier`` mode changes nothing.
    """
    edited = edit_value(values, field, raw)
    if edited == values:
        return False
    if field == "word_scale":
        return True
    return getattr(edited, field) != control_values(values)[field]


def edit_base_family(values: PresetValues, family: str) -> PresetValues:
    """Change the base font (K2: multiplier kept, target_em recalculated at resolve)."""
    if family not in SHORTLIST:
        raise ValueError(family)
    return with_base_family(values, family)


def control_values(values: PresetValues) -> dict[str, float]:
    """What each control shows: the RESOLVED value, so shown = applied.

    Falls back to the snapped stored values (word: the multiplier, or the range
    minimum for an unresolvable target) when the values do not resolve.
    """
    try:
        config = resolve(values)
        return {field: float(getattr(config, field)) for field in RANGES}
    except (PresetResolutionError, SpaceWidthUnavailable, ValueError):
        pass
    shown: dict[str, float] = {}
    for field, rng in RANGES.items():
        if field == "word_scale":
            raw = values.word.value if values.word.mode == "multiplier" else rng.min
        else:
            raw = getattr(values, field)
        shown[field] = _finite_snap(field, raw)
    return shown


def _finite_snap(field: str, raw: Any) -> float:
    rng = RANGES[field]
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return rng.min
    if not math.isfinite(value):
        return rng.min
    return min(max(snap_value(field, value), rng.min), rng.max)


@dataclass(frozen=True)
class Evaluation:
    """Result of checking the working values before preview / Apply."""

    config: Configuration | None  # None when the values do not resolve (Apply blocked)
    warnings: tuple[Issue, ...]
    errors: tuple[Issue, ...]


def evaluate(values: PresetValues) -> Evaluation:
    """resolve -> round -> validate (K2). Errors block Apply; warnings are shown."""
    try:
        config = resolve(values)
    except PresetResolutionError as exc:
        return Evaluation(None, (), tuple(exc.issues))
    except (SpaceWidthUnavailable, ValueError):
        return Evaluation(None, (), (Issue("error", "word_scale", IssueCode.NOT_A_NUMBER.value),))
    issues = validate(config)
    return Evaluation(
        config,
        tuple(i for i in issues if i.level == "warning"),
        tuple(i for i in issues if i.level == "error"),
    )


def warning_text(warnings: tuple[Issue, ...] | list[Issue]) -> str | None:
    """The message for `validate()` warnings, or None when there are none."""
    if not warnings:
        return None
    if any(i.code == IssueCode.UI_LINE_ABOVE_RECOMMENDED.value for i in warnings):
        return UI_LINE_WARNING
    return "Some values are above the recommended range."


def error_text(errors: tuple[Issue, ...] | list[Issue]) -> str:
    """The message for values that do not resolve (Apply is blocked)."""
    fields = sorted({FIELD_LABELS.get(i.field, i.field) for i in errors})
    return "These values cannot be applied: " + ", ".join(fields) + "."


@dataclass(frozen=True)
class PresetRow:
    """One entry of the preset dropdown."""

    kind: Literal["preset", "custom", "original"]
    label: str
    preset: Preset | None = None


CUSTOM_LABEL = "Custom"
ORIGINAL_LABEL = "Original (revert all)"


def preset_rows(user_presets: list[Preset] | tuple[Preset, ...]) -> list[PresetRow]:
    """Built-ins, then valid user presets (`catalogue` order), then Custom, then Original.

    A stored user preset that would shadow a built-in or reuse an ID is skipped rather
    than breaking the screen (`catalogue` refuses it).
    """
    accepted: list[Preset] = []
    for preset in user_presets:
        if preset.kind != "user":
            continue
        try:
            catalogue(tuple(accepted) + (preset,))
        except BuiltinOverwriteError:
            continue
        accepted.append(preset)
    rows = [PresetRow("preset", p.name, p) for p in catalogue(tuple(accepted))]
    rows.append(PresetRow("custom", CUSTOM_LABEL))
    rows.append(PresetRow("original", ORIGINAL_LABEL))
    return rows


def preset_to_apply(selected: Preset | None, values: PresetValues) -> Preset:
    """The preset Apply sends: the selected one if unchanged, else Custom(values)."""
    if selected is not None and selected.values == values:
        return selected
    return custom_preset(values)


def make_user_preset(name: str, values: PresetValues) -> Preset:
    """Save-as: a new user preset (uuid4 ID). Postcondition: it passes `validate()`.

    Raises ``ValueError`` for an empty name and `PresetResolutionError` (or
    `SpaceWidthUnavailable`) for values that do not resolve.
    """
    clean = " ".join(name.split())
    if not clean:
        raise ValueError("empty preset name")
    resolve(values)  # raises on any validate() error
    return new_user_preset(clean, values)


def word_label(values: PresetValues) -> str:
    """Word-spacing label; "≈" marks a value converted from an em target (A9)."""
    label = FIELD_LABELS["word_scale"]
    return f"{label} ≈" if values.word.mode == "target_em" else label


# --------------------------------------------------------------------------- #
# GTK page — imported lazily so the logic above stays importable without GTK
# --------------------------------------------------------------------------- #

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk

    from scribesense.ui.preview import PreviewWidget
except (ImportError, ValueError):  # no PyGObject / typelibs: logic tests still run
    Adw = Gtk = None  # type: ignore[assignment]
    PreviewWidget = None  # type: ignore[assignment,misc]

_PageBase: Any = Adw.NavigationPage if Adw is not None else object


class PresetEditorPage(_PageBase):  # type: ignore[misc]
    """The main screen for choosing, adjusting and previewing presets."""

    def __init__(
        self,
        build_preview: Callable[[Configuration], PreviewFonts],
        on_apply: Callable[[Preset], None],
        on_original: Callable[[], None],
        list_presets: Callable[[], list[Preset]] = lambda: [],
        save_preset: Callable[[Preset], None] | None = None,
    ) -> None:
        if Adw is None:
            raise ImportError("PresetEditorPage needs GTK 4 and libadwaita")
        super().__init__(title="Presets")
        self._build_preview = build_preview
        self._on_apply = on_apply
        self._on_original = on_original
        self._list_presets = list_presets
        self._save_preset = save_preset

        self._rows: list[PresetRow] = []
        self._selected: Preset | None = BUILTINS[0]  # the preset the values came from
        self._original = False  # "Original (revert all)" selected
        self._values: PresetValues = BUILTINS[0].values
        self._evaluation: Evaluation = evaluate(self._values)
        self._updating_ui = False  # ignore signals caused by programmatic updates

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        self.header = header  # the window adds its own buttons here

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(24)

        # Preset chooser + save-as
        self.preset_dropdown = Gtk.DropDown()
        self.preset_dropdown.set_hexpand(True)
        self.preset_dropdown.update_property([Gtk.AccessibleProperty.LABEL], ["Preset"])
        self.save_button = Gtk.Button(label="Save as…")
        self.save_button.set_visible(save_preset is not None)
        self.save_button.connect("clicked", self._on_save_clicked)
        preset_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        preset_row.append(self.preset_dropdown)
        preset_row.append(self.save_button)
        outer.append(self._labeled_section("Preset", preset_row))

        # Preview
        self.preview = PreviewWidget()
        outer.append(self._labeled_section("Preview", self.preview))

        # Base font (K1 SHORTLIST)
        self.font_dropdown = Gtk.DropDown.new_from_strings(list(SHORTLIST))
        self.font_dropdown.update_property([Gtk.AccessibleProperty.LABEL], ["Base font"])
        self.font_dropdown.connect("notify::selected", self._on_font_changed)

        # Sliders (generated from RANGES only)
        self.sliders_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.sliders_box.append(self._control_row("Base font", self.font_dropdown)[0])
        self.scales: dict[str, Gtk.Scale] = {}
        self.scale_labels: dict[str, Gtk.Label] = {}
        self._build_sliders()
        outer.append(self._labeled_section("Adjustments", self.sliders_box))

        # validate() warnings / errors
        self.warning_label = Gtk.Label()
        self.warning_label.set_wrap(True)
        self.warning_label.set_xalign(0.0)
        self.warning_label.add_css_class("warning")
        self.warning_label.set_visible(False)
        outer.append(self.warning_label)

        self.message_label = Gtk.Label()
        self.message_label.set_wrap(True)
        self.message_label.set_xalign(0.0)
        self.message_label.set_visible(False)
        outer.append(self.message_label)

        self.apply_button = Gtk.Button(label="Apply")
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", self._on_apply_clicked)
        outer.append(self.apply_button)

        scroller.set_child(outer)
        toolbar.set_content(scroller)
        self.set_child(toolbar)

        self._reload_presets(select=self._selected)
        self.preset_dropdown.connect("notify::selected", self._on_preset_changed)
        self._sync_controls()
        self._refresh()

    # --- layout --------------------------------------------------------------

    def _labeled_section(self, title: str, widget: Gtk.Widget) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        label = Gtk.Label(label=title)
        label.add_css_class("title-4")
        label.set_halign(Gtk.Align.START)
        box.append(label)
        box.append(widget)
        return box

    def _control_row(self, title: str, widget: Gtk.Widget) -> tuple[Gtk.Box, Gtk.Label]:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        label = Gtk.Label(label=title)
        label.set_size_request(200, -1)
        label.set_halign(Gtk.Align.START)
        label.set_xalign(0.0)
        row.append(label)
        widget.set_hexpand(True)
        row.append(widget)
        return row, label

    def _build_sliders(self) -> None:
        for field, rng in RANGES.items():
            scale = Gtk.Scale.new_with_range(
                Gtk.Orientation.HORIZONTAL, rng.min, rng.max, rng.step
            )
            scale.set_digits(slider_digits(field))
            scale.set_draw_value(True)
            scale.update_property([Gtk.AccessibleProperty.LABEL], [FIELD_LABELS[field]])
            scale.connect("value-changed", self._on_slider_changed, field)
            row, label = self._control_row(FIELD_LABELS[field], scale)
            self.sliders_box.append(row)
            self.scales[field] = scale
            self.scale_labels[field] = label

    # --- preset list ---------------------------------------------------------

    def _reload_presets(self, select: Preset | None) -> None:
        try:
            user = list(self._list_presets())
        except Exception:  # a store problem must not break the screen
            user = []
            self._show_message("Saved presets could not be loaded.")
        self._rows = preset_rows(user)
        model = Gtk.StringList()
        for row in self._rows:
            model.append(row.label)
        self._updating_ui = True
        self.preset_dropdown.set_model(model)
        self.preset_dropdown.set_selected(self._row_index(select))
        self._updating_ui = False

    def _row_index(self, preset: Preset | None) -> int:
        for i, row in enumerate(self._rows):
            if self._original and row.kind == "original":
                return i
            if not self._original and preset is not None and row.preset is not None \
                    and row.preset.id == preset.id:
                return i
        return next(i for i, r in enumerate(self._rows) if r.kind == "custom")

    def _select_row_quietly(self) -> None:
        self._updating_ui = True
        self.preset_dropdown.set_selected(self._row_index(self._selected))
        self._updating_ui = False

    # --- signal handlers -----------------------------------------------------

    def _on_preset_changed(self, dropdown: Gtk.DropDown, _pspec: Any) -> None:
        if self._updating_ui:
            return
        idx = dropdown.get_selected()
        if idx >= len(self._rows):
            return
        row = self._rows[idx]
        if row.kind == "original":
            self._original = True
        else:
            self._original = False
            if row.kind == "preset" and row.preset is not None:
                self._selected = row.preset
                self._values = row.preset.values
            else:  # Custom keeps the working values
                self._selected = None
            self._sync_controls()
        self._refresh()

    def _on_slider_changed(self, scale: Gtk.Scale, field: str) -> None:
        if self._updating_ui:
            return
        raw = scale.get_value()
        snapped = snap_value(field, raw)
        if abs(snapped - raw) > 1e-9:  # show the value that will be applied
            self._updating_ui = True
            scale.set_value(snapped)
            self._updating_ui = False
        if not slider_edit_changes(self._values, field, snapped):
            return  # snapped back onto the current value: no Custom switch, no rebuild
        self._values = edit_value(self._values, field, snapped)
        self._after_edit()

    def _on_font_changed(self, dropdown: Gtk.DropDown, _pspec: Any) -> None:
        if self._updating_ui:
            return
        idx = dropdown.get_selected()
        if idx >= len(SHORTLIST) or SHORTLIST[idx] == self._values.base_family:
            return
        self._values = edit_base_family(self._values, SHORTLIST[idx])
        self._after_edit()

    def _after_edit(self) -> None:
        """An edit leaves Original and becomes Custom unless it matches the preset."""
        self._original = False
        if self._selected is not None and self._selected.values != self._values:
            self._selected = None
        self._select_row_quietly()
        self._sync_controls()
        self._refresh()

    def _on_apply_clicked(self, _button: Gtk.Button) -> None:
        if self._original:
            self._on_original()  # K2: Original is a sentinel, never resolved
            return
        if self._evaluation.config is None:
            return  # blocked: values do not resolve
        self._on_apply(preset_to_apply(self._selected, self._values))

    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        if self._save_preset is None:
            return
        entry = Gtk.Entry()
        entry.set_placeholder_text("Preset name")
        entry.set_activates_default(True)
        dialog = Adw.AlertDialog.new("Save preset", "Save the current values as a preset.")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_save_response, entry)
        dialog.present(self)

    def _on_save_response(self, _dialog: Any, response: str, entry: Gtk.Entry) -> None:
        if response != "save" or self._save_preset is None:
            return
        try:
            preset = make_user_preset(entry.get_text(), self._values)
        except ValueError as exc:  # includes PresetResolutionError
            if isinstance(exc, PresetResolutionError):
                self._show_message(error_text(exc.issues))
            else:
                self._show_message("Enter a name to save the preset.")
            return
        except SpaceWidthUnavailable:
            self._show_message("These values cannot be saved.")
            return
        try:
            self._save_preset(preset)
        except Exception:  # store failure: message only, never the name or values
            self._show_message("The preset could not be saved.")
            return
        self._selected = preset
        self._original = False
        self._reload_presets(select=preset)
        self._show_message("Preset saved.")

    # --- view updates --------------------------------------------------------

    def _sync_controls(self) -> None:
        """Move every control to the resolved value of the working values."""
        shown = control_values(self._values)
        self._updating_ui = True
        for field, scale in self.scales.items():
            scale.set_value(shown[field])
        self.scale_labels["word_scale"].set_text(word_label(self._values))
        if self._values.base_family in SHORTLIST:
            self.font_dropdown.set_selected(SHORTLIST.index(self._values.base_family))
        self._updating_ui = False

    def _refresh(self) -> None:
        """Re-check the values, update warnings / Apply, and request a new preview."""
        if self._original:
            self.warning_label.set_visible(False)
            self.preview.show_message(ORIGINAL_MESSAGE)
            self.apply_button.set_label("Revert all")
            self.apply_button.set_sensitive(True)
            return
        self.apply_button.set_label("Apply")
        self._evaluation = evaluation = evaluate(self._values)
        if evaluation.config is None:
            self.warning_label.set_text(error_text(evaluation.errors))
            self.warning_label.set_visible(True)
            self.apply_button.set_sensitive(False)  # K1: errors block
            self.preview.show_message("No preview: the values cannot be applied.")
            return
        text = warning_text(evaluation.warnings)
        self.warning_label.set_text(text or "")
        self.warning_label.set_visible(text is not None)
        # Apply stays possible whatever happens to the preview (card failure rule).
        self.apply_button.set_sensitive(True)
        config = evaluation.config
        build = self._build_preview
        self.preview.request(lambda: build(config), config.text_scale, config.reading_line)

    def _show_message(self, text: str) -> None:
        self.message_label.set_text(text)
        self.message_label.set_visible(True)
