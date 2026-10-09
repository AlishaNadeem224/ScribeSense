"""K10 — CLI commands (owner O2). FROZEN 2026-10-09.

- reset, recover --login, uninstall, doctor never need the service or GUI (A2) and never return 4.
- read / preset next start the service if it isn't running; 4 only if that fails.
- confirm <tx>: fresh process, bypasses single-instance forwarding; only the pending tx (A15).
- Output is plain readable text; reset ends with one line: restored / skipped / failed counts.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0  # completed (partial coverage / skipped drift described in the output)
    FAILED = 1
    USAGE = 2
    BUSY = 3  # another transaction is active (also LOCK_TIMEOUT)
    SERVICE_UNAVAILABLE = 4  # the service is required and couldn't be reached or started


#: Command -> who uses it.
COMMANDS: dict[str, str] = {
    "scribesense": "opens / focuses the app",
    "scribesense apply <preset-id>": "UI, scripts",
    "scribesense preset next": "keybind Super+Alt+P",
    "scribesense read --selection": "keybind Super+Alt+R",
    "scribesense read --clipboard": "UI",
    "scribesense reset": "recovery keybind Ctrl+Alt+Shift+Super+Backspace",
    "scribesense recover --login": "login check (A3)",
    "scribesense uninstall [--delete-data] [--remove-fonts-anyway]": "user (R2-9)",
    "scribesense doctor": "user, install",
    "scribesense confirm <tx>": "started by the service after apply (A15)",
    "scribesense quit": "user — stops the background service (A1)",
}

#: Commands that must work without the service and without GTK/Adw (A2).
RECOVERY_COMMANDS: frozenset[str] = frozenset({"reset", "recover", "uninstall", "doctor"})
