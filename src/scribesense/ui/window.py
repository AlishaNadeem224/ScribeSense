"""M2.4 — the application window."""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from scribesense.config.presets import resolve  # type: ignore
from scribesense.fontgen import generator  # type: ignore
from scribesense.contracts.presets import Preset  # noqa: E402
from scribesense.reader.capture import from_clipboard  # noqa: E402
from scribesense.reader.popup import ReaderWindow  # noqa: E402
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
        self._add_header_buttons(self.editor)
        self.nav.push(self.editor)

        self.tray = TrayIcon(
            app_id=app_id,
            on_apply=self._on_tray_apply,
            on_show=self.present,
            on_quit=self._on_tray_quit,
        )

        self.connect("close-request", self._on_close_request)

    def _add_header_buttons(self, page: PresetEditorPage) -> None:
        toolbar_view = page.get_child()
        header_bar = toolbar_view.get_top_bar()
        
        # Settings button
        settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        settings_btn.set_tooltip_text("Settings")
        settings_btn.connect("clicked", self._on_settings_clicked)
        header_bar.pack_end(settings_btn)

        # Reader button (M2.9)
        reader_btn = Gtk.Button(icon_name="utilities-terminal-symbolic")
        reader_btn.set_tooltip_text("Open reader from clipboard")
        reader_btn.connect("clicked", self._on_reader_clicked)
        header_bar.pack_start(reader_btn)

    def _on_reader_clicked(self, _button: Gtk.Button) -> None:
        result = from_clipboard()
        if result.code is not None:
            print(f"Capture failed: {result.code.value}")
            return
        
        # Find the most recently installed font
        installed = generator.installed_sets()
        font_set = installed[-1] if installed else None
        
        reader = ReaderWindow(
            text=result.text,
            font_set=font_set,
            application=self.get_application(),
        )
        reader.present()

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
        from scribesense.config.presets import BUILTINS
        if BUILTINS:
            self._on_apply(BUILTINS[0])

    def _on_tray_quit(self) -> None:
        self.get_application().quit()

    def _on_close_request(self, *_args: object) -> bool:
        if self.tray._indicator is not None:
            self.set_visible(False)
            return True
        return False