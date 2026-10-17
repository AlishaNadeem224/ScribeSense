"""M2.4 — the application window.

Hosts the PresetEditorPage (M2.5) and routes to the SettingsPage (M2.7).
Adds an optional tray icon (M2.8).
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
from scribesense.ui.tray import TrayIcon  # noqa: E402


class ScribeSenseWindow(Adw.ApplicationWindow):
    """The single top-level window for the application."""

    def __init__(self, app_id: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)

        self.set_title("ScribeSense")
        self.set_default_size(720, 800)

        self.nav = Adw.NavigationView()
        self.set_content(self.nav)

        # Build the editor page
        self.editor = PresetEditorPage(
            build_preview=generator.build_preview,
            on_apply=self._on_apply,
        )
        self._add_settings_button(self.editor)
        self.nav.push(self.editor)

        # M2.8 Optional tray
        self.tray = TrayIcon(
            app_id=app_id,
            on_apply=self._on_tray_apply,
            on_show=self.present,
            on_quit=self._on_tray_quit,
        )

        self.connect("close-request", self._on_close_request)

    def _add_settings_button(self, page: PresetEditorPage) -> None:
        toolbar_view = page.get_child()
        header_bar = toolbar_view.get_top_bar()
        settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        settings_btn.set_tooltip_text("Settings")
        settings_btn.connect("clicked", self._on_settings_clicked)
        header_bar.pack_end(settings_btn)

    def _on_settings_clicked(self, _button: Gtk.Button) -> None:
        def get_opt_in() -> bool: return False
        def set_opt_in(val: bool) -> None: print(f"Opt-in set to {val}")
        def get_keybind(k: str) -> str: return "SUPER ALT, R"
        def set_keybind(k: str, v: str) -> None: print(f"Set {k} to {v}")
        def delete_data() -> None: print("Data deletion requested")
        settings = SettingsPage(get_opt_in, set_opt_in, get_keybind, set_keybind, delete_data)
        self.nav.push(settings)

    def _on_apply(self, preset: Preset) -> None:
        dialog = Adw.AlertDialog.new("Apply Pending", f"Apply for preset '{preset.name}'.")
        dialog.add_response("ok", "OK")
        dialog.present(self)

    def _on_tray_apply(self) -> None:
        # Applies the first built-in preset as a demonstration
        from scribesense.config.presets import BUILTINS
        if BUILTINS:
            self._on_apply(BUILTINS[0])

    def _on_tray_quit(self) -> None:
        self.get_application().quit()

    def _on_close_request(self, *_args: object) -> bool:
        # If we have a tray icon, closing the window hides it (the app stays
        # running in the tray). If not, closing the window quits the app.
        if self.tray._indicator is not None:
            self.set_visible(False)
            return True  # Prevent the window from closing
        return False