"""M2.8 — System tray (optional). Unavailable in this GTK 4 build.

Why it is unavailable:
  The only Python tray bindings on the target systems are AppIndicator3 / AyatanaAppIndicator3. Both
  are built on GTK 3 (their menus are Gtk.Menu objects), and a process cannot load GTK 3 and GTK 4
  together: once the app has loaded Gtk 4.0, `gi.require_version("AppIndicator3", "0.1")` fails, and
  Gtk.Menu / Gtk.MenuItem do not exist in GTK 4 at all. A GTK 4 tray would need its own
  StatusNotifierItem + com.canonical.dbusmenu implementation over Gio D-Bus, which is not built.

So, per the card ("No tray host -> feature hidden, no error"; "Must never: be required for any core
function"), the tray is simply reported as unavailable:

  * this module never imports gi, AppIndicator3 or GTK — importing it has no side effects;
  * `is_available()` returns False and `UNAVAILABLE_REASON` says why;
  * `TrayIcon` is an inert placeholder with the planned interface, so callers can be written once.

The window does not depend on the tray: closing it always hides it (A1, `set_hide_on_close(True)` in
ui/window.py), and every tray action (apply, open, quit) is also reachable from the window, the CLI
(K10) and the keybinds.
"""

from __future__ import annotations

from collections.abc import Callable

#: Shown in Settings / documentation when someone asks where the tray icon is.
UNAVAILABLE_REASON = (
    "The tray icon is not available: the system tray library (AppIndicator) only works with GTK 3, "
    "and ScribeSense uses GTK 4. Use the window, the keybinds or the scribesense command instead."
)


def is_available() -> bool:
    """M2.8 availability check. Always False in a GTK 4 process (see the module docstring)."""
    return False


class TrayIcon:
    """Inert placeholder for the M2.8 tray. `available` is always False; every method is a no-op.

    The constructor takes the planned callbacks (apply the current preset, open the window, quit) so a
    future StatusNotifierItem implementation can replace this class without changing its callers.
    """

    available = False

    def __init__(
        self,
        app_id: str,
        on_apply: Callable[[], None],
        on_show: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self.app_id = app_id
        self._callbacks = (on_apply, on_show, on_quit)

    def set_tooltip(self, text: str) -> None:
        """No-op: there is no tray icon to label."""

    def stop(self) -> None:
        """No-op: there is no tray icon to hide."""
