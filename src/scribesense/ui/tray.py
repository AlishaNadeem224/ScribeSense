"""M2.8 — System tray icon (Optional).

Provides a tray icon using AppIndicator (StatusNotifierItem) for Hyprland/Waybar.
If the desktop environment does not support tray icons, this module fails
silently and the app continues to work without it (M2.8 "Must never: Be
required for any core function").
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import gi

# AppIndicator is not always available. We fail gracefully if it is missing.
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ValueError, ImportError):
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as AppIndicator  # type: ignore
    except (ValueError, ImportError):
        AppIndicator = None

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402


class TrayIcon:
    """A system tray icon for ScribeSense."""

    def __init__(
        self,
        app_id: str,
        on_apply: Callable[[], None],
        on_show: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._indicator = None
        if AppIndicator is None:
            return  # Tray not supported on this system.

        self._indicator = AppIndicator.Indicator.new(
            f"{app_id}.tray",
            "preferences-desktop-font",  # Fallback icon name
            AppIndicator.IndicatorCategory.APPLICATION_STATUS,
        )
        self._indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)

        menu = Gtk.Menu()
        
        item_apply = Gtk.MenuItem(label="Apply Current Preset")
        item_apply.connect("activate", lambda _w: on_apply())
        menu.append(item_apply)
        
        item_show = Gtk.MenuItem(label="Open ScribeSense")
        item_show.connect("activate", lambda _w: on_show())
        menu.append(item_show)
        
        menu.append(Gtk.SeparatorMenuItem())
        
        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", lambda _w: on_quit())
        menu.append(item_quit)
        
        menu.show_all()
        self._indicator.set_menu(menu)

    def set_tooltip(self, text: str) -> None:
        if self._indicator is not None:
            self._indicator.set_title(text)

    def stop(self) -> None:
        if self._indicator is not None:
            self._indicator.set_status(AppIndicator.IndicatorStatus.PASSIVE)