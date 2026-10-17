"""M2.7 — Settings screen.

Provides the UI for:
  - "Help improve recommendations" opt-in (default off)
  - Hyprland keybind adjustments
  - "Delete all ScribeSense data" (requires confirmation)
  - Recovery information (the reset keybind)

This screen does not implement the storage or the keybind writing itself;
it delegates to the injected dependencies (StoreSettings, KeybindWriter).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402


class SettingsPage(Adw.NavigationPage):
    """The application settings screen."""

    def __init__(
        self,
        get_opt_in: Callable[[], bool],
        set_opt_in: Callable[[bool], None],
        get_keybind: Callable[[str], str],
        set_keybind: Callable[[str, str], None],
        delete_data_fn: Callable[[], None],
    ) -> None:
        super().__init__(title="Settings")
        self._get_opt_in = get_opt_in
        self._set_opt_in = set_opt_in
        self._get_keybind = get_keybind
        self._set_keybind = set_keybind
        self._delete_data_fn = delete_data_fn

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        page_box = Adw.PreferencesPage()
        
        # 1. Recommendations (Opt-in)
        rec_group = Adw.PreferencesGroup(
            title="Recommendations",
            description="Help improve ScribeSense by anonymously sharing your preset adjustments. "
                        "Text content is never recorded."
        )
        self.opt_in_row = Adw.ActionRow(title="Help improve recommendations")
        self.opt_in_switch = Gtk.Switch()
        self.opt_in_switch.set_active(self._get_opt_in())
        self.opt_in_switch.set_valign(Gtk.Align.CENTER)
        self.opt_in_switch.connect("notify::active", self._on_opt_in_toggled)
        self.opt_in_row.add_suffix(self.opt_in_switch)
        self.opt_in_row.set_activatable_widget(self.opt_in_switch)
        rec_group.add(self.opt_in_row)
        page_box.add(rec_group)

        # 2. Keybinds
        kb_group = Adw.PreferencesGroup(
            title="Keybinds",
            description="Adjust the shortcuts ScribeSense uses. Requires a Hyprland session restart to take effect."
        )
        self.kb_rows: dict[str, Adw.EntryRow] = {}
        for action, display_name in (
            ("keybind_reader", "Open selection in reader"),
            ("keybind_preset_next", "Switch to next preset"),
            ("keybind_reset", "Emergency reset"),
        ):
            row = Adw.EntryRow(title=display_name)
            row.set_text(self._get_keybind(action))
            row.connect("changed", self._on_keybind_changed, action)
            self.kb_rows[action] = row
            kb_group.add(row)
        page_box.add(kb_group)

        # 3. Recovery & Data
        data_group = Adw.PreferencesGroup(title="Data & Recovery")
        
        # Recovery info (non-editable)
        reset_row = Adw.ActionRow(title="Emergency reset keybind")
        reset_lbl = Gtk.Label(label=self._get_keybind("keybind_reset"))
        reset_lbl.add_css_class("dim-label")
        reset_row.add_suffix(reset_lbl)
        data_group.add(reset_row)

        # Delete all data button
        self.delete_row = Adw.ActionRow(title="Delete all ScribeSense data")
        self.delete_row.set_subtitle("Removes presets, settings, and recovery records.")
        delete_btn = Gtk.Button(label="Delete...")
        delete_btn.add_css_class("destructive-action")
        delete_btn.set_valign(Gtk.Align.CENTER)
        delete_btn.connect("clicked", self._on_delete_clicked)
        self.delete_row.add_suffix(delete_btn)
        data_group.add(self.delete_row)
        
        page_box.add(data_group)

        scroller.set_child(page_box)
        toolbar.set_content(scroller)
        self.set_child(toolbar)

    def _on_opt_in_toggled(self, switch: Gtk.Switch, _pspec: Any) -> None:
        self._set_opt_in(switch.get_active())

    def _on_keybind_changed(self, row: Adw.EntryRow, action: str) -> None:
        # In a full implementation, we would validate the keybind syntax here
        # and show an error if it conflicts. For M2.7, we pass it straight to O4.
        self._set_keybind(action, row.get_text())

    def _on_delete_clicked(self, _button: Gtk.Button) -> None:
        dialog = Adw.AlertDialog.new(
            "Delete all data?",
            "This will permanently delete all ScribeSense presets, settings, and recovery records. "
            "It will first offer to reset your desktop settings to their original state."
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete everything")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_delete_confirmed)
        dialog.present(self.get_root())

    def _on_delete_confirmed(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response == "delete":
            try:
                self._delete_data_fn()
                # Show a toast or success message
                print("Data deleted successfully.")
            except Exception as e:
                print(f"Failed to delete data: {e}")