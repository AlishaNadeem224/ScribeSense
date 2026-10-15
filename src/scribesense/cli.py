"""O2 — CLI entry points (M2.11, contract K10).

This module is the process's entry point. It must remain importable without
GTK, because `reset`, `recover`, `uninstall` and `doctor` never load UI code
(A2). The GTK application is imported only on the branch that needs it.

Routing rules (A1, A15):
  - `confirm <tx>` never forwards. It runs as a fresh process.
  - `reset`, `recover`, `uninstall`, `doctor` never need the service (A2).
  - an unknown command is a usage error (exit 2) without loading GTK.
  - everything else goes to the GTK application: the first launch becomes the
    single-instance service, later launches are forwarded to it by Gio (A1).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from scribesense.contracts.cli import ExitCode, RECOVERY_COMMANDS

#: Commands that must run as a fresh process and never register with Gio.
_FRESH: frozenset[str] = frozenset({"confirm"})

#: First word of every K10 command ("" = open / focus the app).
_KNOWN: frozenset[str] = frozenset({"", "apply", "preset", "read", "quit"}) | _FRESH | RECOVERY_COMMANDS

#: Why a recovery command cannot run yet (it needs the SQLite journal, K9 / M3.1).
_NEEDS_STORE = "needs the journal store (M3.1), which is not built yet"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    rest = args[1:]
    command = rest[0] if rest else ""

    # A15 — `confirm` runs as a fresh process, bypassing single-instance
    # forwarding entirely. It must not import the GTK app at this stage.
    if command in _FRESH:
        # M2.3 will implement the confirm window. Until then, refuse clearly.
        sys.stderr.write(
            "scribesense: 'confirm' is not implemented yet (arrives with M2.3)\n"
        )
        return int(ExitCode.FAILED)

    if command not in _KNOWN:
        sys.stderr.write(
            f"scribesense: unknown command '{command}'\n"
            "Commands: apply <preset-id> · preset next · read --selection|--clipboard · reset · "
            "recover --login · uninstall · doctor · confirm <tx> · quit\n"
        )
        return int(ExitCode.USAGE)

    # A2 — recovery commands must not load UI code.
    if command == "doctor":
        try:
            from scribesense.doctor import run_doctor  # M3.7 (O3)
        except ImportError:
            sys.stderr.write("scribesense: 'doctor' is not implemented yet (M3.7)\n")
            return int(ExitCode.FAILED)
        return int(run_doctor())
    if command in RECOVERY_COMMANDS:
        # M4.11/M4.12 exist (safety.recovery) but run on the real journal only.
        sys.stderr.write(f"scribesense: '{command}' {_NEEDS_STORE}\n")
        return int(ExitCode.FAILED)

    # Everything else goes through the GTK application. Import lazily so the
    # branches above never pull GTK in.
    try:
        from scribesense.ui.app import run as run_app
    except ImportError as exc:
        sys.stderr.write(
            "scribesense: GTK4 / libadwaita (PyGObject) is required but not "
            f"available: {exc}\n"
            "Install the system packages, then re-run. "
            "See `scribesense doctor` for details.\n"
        )
        return int(ExitCode.FAILED)

    try:
        return int(run_app(args))
    except Exception as exc:  # startup failures must not leave a blank window
        sys.stderr.write(f"scribesense: failed to start: {exc}\n")
        sys.stderr.write(
            "Recovery: run `scribesense reset` (works without the app window) "
            "or press Ctrl+Alt+Shift+Super+Backspace.\n"
        )
        return int(ExitCode.FAILED)


if __name__ == "__main__":
    raise SystemExit(main())
