"""M2.4 — the application window.

Now hosts the PresetEditorPage (M2.5) instead of a placeholder.
"""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

# Import O1's actual implementations
from scribesense.config.presets import resolve  # type: ignore
from scribesense.fontgen import generator  # type: ignore

from scribesense.contracts.presets import Preset  # noqa: E402
from scribesense.ui.preset_editor import PresetEditorPage  # noqa: E402


class ScribeSenseWindow(Adw.ApplicationWindow):
    """The single top-level window for the application."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

        self.set_title("ScribeSense")
        self.set_default_size(720, 800)

        self.nav = Adw.NavigationView()
        self.set_content(self.nav)

        # Build the editor page using O1's real fontgen.generator.build_preview
        editor = PresetEditorPage(
            build_preview=generator.build_preview,
            on_apply=self._on_apply,
        )
        self.nav.push(editor)

        self.connect("close-request", self._on_close_request)

    def _on_apply(self, preset: Preset) -> None:
        """Called when the user presses Apply."""
        # This is where M2.1's controller would be invoked.
        dialog = Adw.AlertDialog.new(
            "Apply Pending",
            f"M2.1 apply controller will be wired here for preset '{preset.name}'."
        )
        dialog.add_response("ok", "OK")
        dialog.present(self)

    def _on_close_request(self, *_args: object) -> bool:
        return False