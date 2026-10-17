"""M2.4 — the application window.

Hosts the PresetEditorPage (M2.5) and routes to the SettingsPage (M2.7).
"""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from scribesense.config.presets import resolve  # type: ignore
from scribesense.fontgen import generator  # type: ignore
from scribesense.contracts.presets import Preset  # noqa: E402
from scribesense.ui.preset_editor import PresetEditorPage  # noqa: E402
from scribesense.ui.settings_page import SettingsPage  # noqa: E402


class ScribeSenseWindow(Adw.ApplicationWindow):
    """The single top-level window for the application."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

        self.set_title("ScribeSense")
        self.set_default_size(720, 800)

        self.nav = Adw.NavigationView()
        self.set_content(self.nav)

        # Build the editor page
        editor = PresetEditorPage(
            build_preview=generator.build_preview,
            on_apply=self._on_apply,
        )
        # Add a settings button to the editor's header
        self._add_settings_button(editor)
        
        self.nav.push(editor)

        self.connect("close-request", self._on_close_request)

    def _add_settings_button(self, page: PresetEditorPage) -> None:
        # Find the ToolbarView inside the editor page to add a top bar button
        toolbar_view = page.get_child()
        header_bar = toolbar_view.get_top_bar()
        
        settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        settings_btn.set_tooltip_text("Settings")
        settings_btn.connect("clicked", self._on_settings_clicked)
        header_bar.pack_end(settings_btn)

    def _on_settings_clicked(self, _button: Gtk.Button) -> None:
        # In a real app, these callables would use O3's SettingsRepo and O4's keybind writer.
        # For now, we provide mock implementations so the UI works.
        def get_opt_in() -> bool: return False
        def set_opt_in(val: bool) -> None: print(f"Opt-in set to {val}")
        def get_keybind(k: str) -> str: return "SUPER ALT, R"
        def set_keybind(k: str, v: str) -> None: print(f"Set {k} to {v}")
        def delete_data() -> None: print("Data deletion requested")

        settings = SettingsPage(
            get_opt_in=get_opt_in,
            set_opt_in=set_opt_in,
            get_keybind=get_keybind,
            set_keybind=set_keybind,
            delete_data_fn=delete_data,
        )
        self.nav.push(settings)

    def _on_apply(self, preset: Preset) -> None:
        dialog = Adw.AlertDialog.new(
            "Apply Pending",
            f"M2.1 apply controller will be wired here for preset '{preset.name}'."
        )
        dialog.add_response("ok", "OK")
        dialog.present(self)

    def _on_close_request(self, *_args: object) -> bool:
        return False