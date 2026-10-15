"""O2 — User Experience (M2.4 onwards).

This package holds the GTK4 / libadwaita application, its windows, and its
screens. Importing this package must not import GTK at module level: the
recovery path (reset, recover, uninstall, doctor) never loads UI code, and
tests/unit/test_import_rules.py enforces that.
"""

from __future__ import annotations

__all__ = ["APP_ID", "ScribeSenseApp", "ScribeSenseWindow"]

#: Fixed application ID for the D-Bus single-instance registration and, later,
#: for desktop integration (the native-messaging host's allowed_origins and any
#: .desktop file must agree with this name). Changing it splits the
#: single-instance behaviour on users' machines.
APP_ID = "io.github.scribesense.ScribeSense"


def __getattr__(name: str):
    # Lazy import so `import scribesense.ui` never pulls in GTK.
    if name == "ScribeSenseApp":
        from scribesense.ui.app import ScribeSenseApp
        return ScribeSenseApp
    if name == "ScribeSenseWindow":
        from scribesense.ui.window import ScribeSenseWindow
        return ScribeSenseWindow
    raise AttributeError(name)