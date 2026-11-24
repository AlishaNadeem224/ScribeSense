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
from scribesense.config.settings import decimals_of, imported_name_ok, snap, validate
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


def edit_base_family(values: PresetValues, family: str,
                     available: list[str] | tuple[str, ...] = SHORTLIST) -> PresetValues:
    """Change the base font (K2: multiplier kept, target_em recalculated at resolve). Only a font that is available
    — the dropdown's `font_choices()`: bundled, or imported (W1) — is accepted."""
    if family not in available:
        raise ValueError(family)
    return with_base_family(values, family)


# --------------------------------------------------------------------------- #
# Imported fonts (W1, 2026-11-24)
# --------------------------------------------------------------------------- #

def font_choices(imported: list[str] | tuple[str, ...]) -> list[str]:
    """The base-font dropdown: the bundled families, then the imported ones (sorted, valid names only). Only fonts
    that are actually available are listed (PLAN_v2 W1.5)."""
    extra = sorted({f for f in imported if f not in SHORTLIST and imported_name_ok(f)}, key=str.casefold)
    return [*SHORTLIST, *extra]


#: importer reasons -> plain words (shown next to the file's name).
IMPORT_REASONS: dict[str, str] = {
    "STYLE_UNSUPPORTED": "only Regular, Bold, Italic and Bold Italic can be used",
    "NO_REGULAR": "the family has no Regular style",
    "DUPLICATE_STYLE": "another file has the same style",
    "BAD_FAMILY_NAME": "the font's family name cannot be used",
    "TOO_MANY_FILES": "too many files at once (at most 64)",
    "FONT_TOO_LARGE": "the file is over 32 MB",
    "FONT_PARSE_TIMEOUT": "the font took too long to check",
    "FONT_UNREADABLE": "not a font ScribeSense can read, or it could not be checked",
    "MISSING_TABLE": "not a complete font file",
    "NO_UPM": "not a complete font file",
    "BASE_IS_MONOSPACE": "monospaced fonts cannot be spaced",
    "BASE_IS_ICON_FONT": "icon fonts cannot be used",
    "NO_LATIN_COVERAGE": "the font has no Latin letters",
    "IMPORTED_FONT_IN_USE": "this family is used by a preset or the current style",
    "STORE": "it could not be saved",
    "COPY": "it could not be copied",
    "STAGING": "the font folder could not be prepared",
    "NOT_INSTALLED": "not found among the installed fonts",
    "FOLDER_UNREADABLE": "the folder could not be read",
}
STYLE_NAMES = {"Regular": "Regular", "Bold": "Bold", "Italic": "Italic", "BoldItalic": "Bold Italic"}


def import_message(report: Any, *, limit: int = 5) -> str:
    """What an import did, in one message: the families added, then each refused file with its reason."""
    parts: list[str] = []
    for family, styles in getattr(report, "imported", {}).items():
        parts.append(f"Imported {family} ({', '.join(STYLE_NAMES.get(s, s) for s in styles)}).")
    refused = list(getattr(report, "refused", []))
    if refused:
        shown = [f"{r.source}: {IMPORT_REASONS.get(r.detail, r.detail)}" for r in refused[:limit]]
        more = f"; and {len(refused) - limit} more" if len(refused) > limit else ""
        parts.append(f"Not imported — {'; '.join(shown)}{more}.")
    return " ".join(parts) or "Nothing was imported."


def _font_error_text(exc: BaseException) -> str:
    code = getattr(getattr(exc, "code", None), "value", None)
    if code == "APPLY_IN_PROGRESS":
        return "Fonts cannot be changed while a change waits for Keep or Revert."
    if code == "IMPORTED_FONT_IN_USE":
        return "This font is used by a saved preset or the current style. Change those first."
    return f"The font could not be changed ({code or 'unexpected error'})."


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
    from gi.repository import Adw, Gio, GLib, Gtk

    from scribesense.ui.preview import PreviewWidget
except (ImportError, ValueError):  # no PyGObject / typelibs: logic tests still run
    Adw = Gio = GLib = Gtk = None  # type: ignore[assignment]
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
        fonts: Any | None = None,
    ) -> None:
        """`fonts` (W1, 2026-11-24): imported-font actions, or None (no service: bundled fonts only). Methods, all
        blocking (the page runs them off the GTK thread): imported() -> names · installed() -> names ·
        import_(kind, what) -> importer.ImportReport · remove(family) -> bool."""
        if Adw is None:
            raise ImportError("PresetEditorPage needs GTK 4 and libadwaita")
        super().__init__(title="Presets")
        self._build_preview = build_preview
        self._on_apply = on_apply
        self._on_original = on_original
        self._list_presets = list_presets
        self._save_preset = save_preset
        self._fonts = fonts
        self._choices: list[str] = font_choices([])

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

        # Preset chooser + save-as + restore original
        self.preset_dropdown = Gtk.DropDown()
        self.preset_dropdown.set_hexpand(True)
        self.preset_dropdown.update_property([Gtk.AccessibleProperty.LABEL], ["Preset"])
        self.save_button = Gtk.Button(label="Save as…")
        self.save_button.set_visible(save_preset is not None)
        self.save_button.connect("clicked", self._on_save_clicked)
        self.restore_button = Gtk.Button(label="Restore original")
        self.restore_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Restore original settings"]
        )
        self.restore_button.connect("clicked", self._on_restore_clicked)
        preset_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        preset_row.append(self.preset_dropdown)
        preset_row.append(self.save_button)
        preset_row.append(self.restore_button)
        outer.append(self._labeled_section("Preset", preset_row))

        # Preview
        self.preview = PreviewWidget()
        outer.append(self._labeled_section("Preview", self.preview))

        # Base font (K1 SHORTLIST)
        self.font_model = Gtk.StringList.new(self._choices)
        self.font_dropdown = Gtk.DropDown(model=self.font_model)
        self.font_dropdown.update_property([Gtk.AccessibleProperty.LABEL], ["Base font"])
        self.font_dropdown.connect("notify::selected", self._on_font_changed)
        self.font_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.font_row.append(self.font_dropdown)
        self.font_menu = self._build_font_menu()
        self.font_row.append(self.font_menu)
        self.font_menu.set_visible(fonts is not None)

        # Sliders (generated from RANGES only)
        self.sliders_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.sliders_box.append(self._control_row("Base font", self.font_row)[0])
        self.font_dropdown.set_hexpand(True)
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
        self.reload_fonts()
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
        if idx >= len(self._choices) or self._choices[idx] == self._values.base_family:
            return
        self._values = edit_base_family(self._values, self._choices[idx], self._choices)
        self._after_edit()
        self._sync_font_menu()

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

    def _on_restore_clicked(self, _button: Gtk.Button) -> None:
        """The always-visible "Restore original" button: same callback as the dropdown."""
        self._on_original()

    # --- imported fonts (W1, 2026-11-24) ---------------------------------------

    def _build_font_menu(self) -> Gtk.MenuButton:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(6)
        self.font_buttons: dict[str, Gtk.Button] = {}
        for key, label, handler in (
            ("files", "Import font files…", self._on_import_files),
            ("folder", "Import a font folder…", self._on_import_folder),
            ("installed", "Use an installed font…", self._on_import_installed),
            ("remove", "Remove this imported font…", self._on_remove_font),
        ):
            button = Gtk.Button(label=label)
            button.add_css_class("flat")
            button.connect("clicked", handler)
            box.append(button)
            self.font_buttons[key] = button
        popover = Gtk.Popover()
        popover.set_child(box)
        menu = Gtk.MenuButton(label="Fonts…", popover=popover)
        menu.update_property([Gtk.AccessibleProperty.LABEL], ["Import or remove fonts"])
        self._font_popover = popover
        return menu

    def _sync_font_menu(self) -> None:
        if hasattr(self, "font_buttons"):
            self.font_buttons["remove"].set_sensitive(self._values.base_family not in SHORTLIST)

    def _fonts_async(self, work: Callable[[], Any], done: Callable[[Any], None]) -> None:
        """Run blocking font work in a thread; `done(result or exception)` on the GTK main loop."""
        import threading

        def runner() -> None:
            try:
                outcome: Any = work()
            except BaseException as exc:  # noqa: BLE001 — handed to `done`
                outcome = exc
            GLib.idle_add(lambda: done(outcome) or False)

        threading.Thread(target=runner, name="scribesense-fonts", daemon=True).start()

    def reload_fonts(self, select: str | None = None) -> None:
        """Re-read the imported families into the dropdown; `select` one (e.g. just imported)."""
        if self._fonts is None:
            return

        def done(result: Any) -> None:
            if isinstance(result, BaseException):
                self._show_message("The imported fonts could not be listed.")
                return
            self._choices = font_choices(list(result))
            self._updating_ui = True
            self.font_model.splice(0, self.font_model.get_n_items(), self._choices)
            self._updating_ui = False
            if select is not None and select in self._choices:
                self._values = edit_base_family(self._values, select, self._choices)
                self._after_edit()
            self._sync_controls()

        self._fonts_async(self._fonts.imported, done)

    def _run_import(self, kind: str, what: Any) -> None:
        self._font_popover.popdown()
        self.font_menu.set_sensitive(False)
        self._show_message("Checking the font… (each file is checked safely before it is used)")

        def done(result: Any) -> None:
            self.font_menu.set_sensitive(True)
            if isinstance(result, BaseException):
                self._show_message(_font_error_text(result))
                return
            self._show_message(import_message(result))
            families = list(getattr(result, "imported", {}))
            self.reload_fonts(select=families[0] if len(families) == 1 else None)

        self._fonts_async(lambda: self._fonts.import_(kind, what), done)

    def _font_dialog(self, title: str) -> Gtk.FileDialog:
        dialog = Gtk.FileDialog(title=title, modal=True)
        only_fonts = Gtk.FileFilter()
        only_fonts.set_name("Fonts (.ttf, .otf)")
        for suffix in ("ttf", "otf", "TTF", "OTF"):
            only_fonts.add_suffix(suffix)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(only_fonts)
        dialog.set_filters(filters)
        dialog.set_default_filter(only_fonts)
        return dialog

    def _on_import_files(self, _button: Gtk.Button) -> None:
        dialog = self._font_dialog("Import font files")

        def chosen(dlg: Gtk.FileDialog, result: Any) -> None:
            try:
                files = dlg.open_multiple_finish(result)
            except GLib.Error:
                return  # cancelled
            paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
            if any(paths):
                self._run_import("files", [p for p in paths if p])

        dialog.open_multiple(self.get_root(), None, chosen)

    def _on_import_folder(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title="Import a font folder", modal=True)

        def chosen(dlg: Gtk.FileDialog, result: Any) -> None:
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            if folder is not None and folder.get_path():
                self._run_import("folder", folder.get_path())

        dialog.select_folder(self.get_root(), None, chosen)

    def _on_import_installed(self, _button: Gtk.Button) -> None:
        self._font_popover.popdown()

        def done(result: Any) -> None:
            if isinstance(result, BaseException) or not result:
                self._show_message("No installed fonts were found.")
                return
            names = list(result)
            picker = Gtk.DropDown.new_from_strings(names)
            picker.set_enable_search(True)
            picker.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
            picker.update_property([Gtk.AccessibleProperty.LABEL], ["Installed font"])
            dialog = Adw.AlertDialog.new("Use an installed font",
                                         "ScribeSense copies the font and checks it before using it.")
            dialog.set_extra_child(picker)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("import", "Use this font")
            dialog.set_response_appearance("import", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("import")
            dialog.set_close_response("cancel")

            def respond(_d: Any, response: str) -> None:
                index = picker.get_selected()
                if response == "import" and index < len(names):
                    self._run_import("installed", names[index])

            dialog.connect("response", respond)
            dialog.present(self)

        self._fonts_async(self._fonts.installed, done)

    def _on_remove_font(self, _button: Gtk.Button) -> None:
        self._font_popover.popdown()
        family = self._values.base_family
        if family in SHORTLIST or self._fonts is None:
            return
        dialog = Adw.AlertDialog.new(f"Remove {family}?",
                                     "Its copy is deleted from ScribeSense. Presets that use it must be changed first.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def respond(_d: Any, response: str) -> None:
            if response != "remove":
                return

            def done(result: Any) -> None:
                if isinstance(result, BaseException):
                    self._show_message(_font_error_text(result))
                    return
                self._show_message(f"{family} was removed.")
                self._values = edit_base_family(self._values, SHORTLIST[-1], SHORTLIST)  # back to a bundled font
                self._after_edit()
                self.reload_fonts()

            self._fonts_async(lambda: self._fonts.remove(family), done)

        dialog.connect("response", respond)
        dialog.present(self)

    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        if self._save_preset is None:
            return
        entry = Gtk.Entry()
        entry.set_placeholder_text("Preset name")
        entry.update_property([Gtk.AccessibleProperty.LABEL], ["Preset name"])  # a placeholder is not a label
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
        if self._values.base_family in self._choices:
            self.font_dropdown.set_selected(self._choices.index(self._values.base_family))
        self._updating_ui = False
        self._sync_font_menu()

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