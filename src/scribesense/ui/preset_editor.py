"""M2.5 — Preset selection, adjustment, and preview.

This page lets the user choose a preset (Default, Reading, High Separation,
or Custom), adjust the spacing values using sliders generated from `RANGES`,
and see a live preview before pressing Apply.

The Apply button is disabled until a valid configuration is selected.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from scribesense.config.presets import (  # type: ignore
    BUILTINS,
    ORIGINAL,
    Preset,
    PresetValues,
    WordSpacing,
    custom_preset,
    is_original,
    resolve,
)
from scribesense.config.settings import RANGES, round_to_steps, validate  # type: ignore
from scribesense.contracts.config import Configuration  # noqa: E402
from scribesense.contracts.fonts import PreviewFonts  # noqa: E402
from scribesense.ui.preview import PreviewWidget  # noqa: E402


class PresetEditorPage(Adw.NavigationPage):
    """The main screen for choosing and previewing presets."""

    def __init__(
        self,
        build_preview: Callable[[Configuration], PreviewFonts],
        on_apply: Callable[[Preset], None],
    ) -> None:
        super().__init__(title="Presets")
        self._build_preview = build_preview
        self._on_apply = on_apply
        self._current_preset: Preset | None = BUILTINS[0] if BUILTINS else None
        self._updating_ui = False  # guard against slider signals during programmatic updates

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        # Main scrollable area
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        outer.set_margin_top(24)
        outer.set_margin_bottom(24)
        outer.set_margin_start(24)
        outer.set_margin_end(24)

        # Preset chooser
        self.preset_dropdown = Gtk.DropDown()
        self._populate_presets()
        self.preset_dropdown.connect("notify::selected", self._on_preset_changed)
        outer.append(self._labeled_section("Preset", self.preset_dropdown))

        # Preview area
        self.preview = PreviewWidget()
        outer.append(self._labeled_section("Preview", self.preview))

        # Sliders (adjustments)
        self.sliders_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.scales: dict[str, Gtk.Scale] = {}
        self._build_sliders()
        outer.append(self._labeled_section("Adjustments", self.sliders_box))

        # Warning label (for ui_line > 1.3)
        self.warning_label = Gtk.Label()
        self.warning_label.add_css_class("error")
        self.warning_label.set_visible(False)
        outer.append(self.warning_label)

        # Apply button
        self.apply_button = Gtk.Button(label="Apply")
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", self._on_apply_clicked)
        outer.append(self.apply_button)

        scroller.set_child(outer)
        toolbar.set_content(scroller)
        self.set_child(toolbar)

        # Trigger initial preview
        self._update_preview()

    def _labeled_section(self, title: str, widget: Gtk.Widget) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        label = Gtk.Label(label=title)
        label.add_css_class("title-4")
        label.set_halign(Gtk.Align.START)
        box.append(label)
        box.append(widget)
        return box

    def _populate_presets(self) -> None:
        names = Gtk.StringList()
        for p in BUILTINS:
            names.append(p.name)
        names.append("Original (Revert all)")
        self.preset_dropdown.set_model(names)
        self.preset_dropdown.set_selected(0)

    def _build_sliders(self) -> None:
        for field, rng in RANGES.items():
            scale = Gtk.Scale.new_with_range(
                Gtk.Orientation.HORIZONTAL, rng.min, rng.max, rng.step
            )
            scale.set_hexpand(True)
            scale.set_draw_value(True)
            scale.connect("value-changed", self._on_slider_changed)

            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            label = Gtk.Label(label=field.replace("_", " ").title())
            label.set_size_request(140, -1)
            label.set_halign(Gtk.Align.START)
            row.append(label)
            row.append(scale)

            self.scales[field] = scale
            self.sliders_box.append(row)

    def _on_preset_changed(self, dropdown: Gtk.DropDown, _pspec: Any) -> None:
        idx = dropdown.get_selected()
        if idx < len(BUILTINS):
            self._current_preset = BUILTINS[idx]
            vals = self._current_preset.values
            
            self._updating_ui = True
            self.scales["letter_em"].set_value(vals.letter_em)
            self.scales["reading_line"].set_value(vals.reading_line)
            self.scales["ui_line"].set_value(vals.ui_line)
            self.scales["text_scale"].set_value(vals.text_scale)
            
            # Resolve the word spacing to get the multiplier
            try:
                config = resolve(vals)
                self.scales["word_scale"].set_value(config.word_scale)
            except Exception:
                self.scales["word_scale"].set_value(1.0)
            self._updating_ui = False
        else:
            # Original is selected (index == len(BUILTINS))
            self._current_preset = None
            self.apply_button.set_sensitive(False)
            self.preview.show_error("Original: no preview available.")
            return
            
        self._update_preview()

    def _on_slider_changed(self, _scale: Gtk.Scale) -> None:
        if self._updating_ui:
            return
        # Any manual adjustment breaks the preset link
        if self._current_preset is not None:
            self._current_preset = None
            self.preset_dropdown.set_selected(Gtk.INVALID_LIST_POSITION)
        self._update_preview()

    def _collect_values(self) -> PresetValues:
        """Build a PresetValues object from the current slider states."""
        base_family = "Noto Sans"
        if self._current_preset is not None:
            base_family = self._current_preset.values.base_family

        return PresetValues(
            base_family=base_family,
            letter_em=self.scales["letter_em"].get_value(),
            word=WordSpacing(mode="multiplier", value=self.scales["word_scale"].get_value()),
            reading_line=self.scales["reading_line"].get_value(),
            ui_line=self.scales["ui_line"].get_value(),
            text_scale=self.scales["text_scale"].get_value(),
        )

    def _update_preview(self) -> None:
        if self._current_preset is None and self.preset_dropdown.get_selected() == len(BUILTINS):
            return  # Original is selected

        try:
            values = self._collect_values()
            config = resolve(values)
            issues = validate(config)
            
            # Show warning if UI line is too high
            warnings = [i for i in issues if i.level == "warning"]
            if warnings:
                self.warning_label.set_text("Warning: UI line height is high. Menus may truncate.")
                self.warning_label.set_visible(True)
            else:
                self.warning_label.set_visible(False)

            # Generate preview font
            preview_fonts = self._build_preview(config)
            self.preview.update_preview(preview_fonts)
            self.apply_button.set_sensitive(True)

        except Exception as e:
            # Fallback to error text
            self.preview.show_error(str(e))
            self.apply_button.set_sensitive(False)

    def _on_apply_clicked(self, _button: Gtk.Button) -> None:
        if self._current_preset is not None:
            self._on_apply(self._current_preset)
        else:
            # If they adjusted sliders, wrap the current state as a Custom preset
            values = self._collect_values()
            custom = custom_preset(values)
            self._on_apply(custom)