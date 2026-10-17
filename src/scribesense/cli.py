"""O2 — CLI entry points (M2.11, contract K10).

This module is the process's entry point. It must remain importable without
GTK, because `reset`, `recover`, `uninstall`, and `doctor` never load UI code
(A2). The GTK application is imported only on the branch that actually needs
it.

Routing rules (A1, A15):
  - `confirm <tx>` never forwards. It runs as a fresh process.
  - `reset`, `recover`, `uninstall`, `doctor` never need the service (A2).
  - everything else forwards to the running service, starting it if needed.

M2.11 wires these commands to the actual functions implemented by O3 and O4.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from scribesense.contracts.cli import ExitCode, RECOVERY_COMMANDS

#: Commands that must run as a fresh process and never register with Gio.
_FRESH: frozenset[str] = frozenset({"confirm"})


def _cmd_reset(args: argparse.Namespace) -> int:
    from scribesense.safety.recovery import reset
    try:
        report = reset()
        print(f"Restored: {report.restored}, Skipped: {report.skipped}, Failed: {report.failed}")
        return int(ExitCode.OK if report.failed == 0 else ExitCode.FAILED)
    except Exception as e:
        print(f"Reset failed: {e}", file=sys.stderr)
        return int(ExitCode.FAILED)


def _cmd_recover(args: argparse.Namespace) -> int:
    from scribesense.safety.recovery import recover_at_login
    try:
        recover_at_login()
        return int(ExitCode.OK)
    except Exception as e:
        print(f"Recovery failed: {e}", file=sys.stderr)
        return int(ExitCode.FAILED)


def _cmd_uninstall(args: argparse.Namespace) -> int:
    from scribesense.store.uninstall import uninstall
    try:
        # Pass flags if provided
        uninstall(delete_data=getattr(args, "delete_data", False), remove_fonts=getattr(args, "remove_fonts_anyway", False))
        return int(ExitCode.OK)
    except Exception as e:
        print(f"Uninstall failed: {e}", file=sys.stderr)
        return int(ExitCode.FAILED)


def _cmd_doctor(args: argparse.Namespace) -> int:
    from scribesense.doctor import run_doctor
    try:
        return int(run_doctor())
    except Exception as e:
        print(f"Doctor failed: {e}", file=sys.stderr)
        return int(ExitCode.FAILED)


def _cmd_apply(args: argparse.Namespace) -> int:
    # For now, this forwards to the running service (via app logic)
    pass


def _cmd_quit(args: argparse.Namespace) -> int:
    from scribesense.safety.ipc import request
    try:
        reply = request({"v": 1, "type": "quit"}, timeout=2.0)
        return int(ExitCode.OK if reply.get("ok") else ExitCode.FAILED)
    except Exception:
        return int(ExitCode.SERVICE_UNAVAILABLE)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scribesense", add_help=False)
    subparsers = parser.add_subparsers(dest="command")

    # Recovery commands (no UI, no service)
    subparsers.add_parser("reset")
    subparsers.add_parser("recover")
    subparsers.add_parser("doctor")
    p_uninstall = subparsers.add_parser("uninstall")
    p_uninstall.add_argument("--delete-data", action="store_true")
    p_uninstall.add_argument("--remove-fonts-anyway", action="store_true")

    # Service commands (start UI if needed)
    subparsers.add_parser("apply")
    subparsers.add_parser("preset")
    subparsers.add_parser("read")
    subparsers.add_parser("confirm")
    subparsers.add_parser("quit")

    args = parser.parse_args(argv)

    if not args.command:
        # Default action: launch UI
        try:
            from scribesense.ui.app import run as run_app
            return int(run_app())
        except ImportError:
            print("scribesense: GTK4 / libadwaita missing", file=sys.stderr)
            return int(ExitCode.FAILED)

    # A15: confirm runs as a fresh process.
    if args.command in _FRESH:
        print("scribesense: 'confirm' is not implemented yet (arrives with M2.3)", file=sys.stderr)
        return int(ExitCode.FAILED)

    # A2: recovery commands do not load UI.
    if args.command in RECOVERY_COMMANDS:
        if args.command == "reset":
            return _cmd_reset(args)
        if args.command == "recover":
            return _cmd_recover(args)
        if args.command == "uninstall":
            return _cmd_uninstall(args)
        if args.command == "doctor":
            return _cmd_doctor(args)

    # Everything else goes through the GTK app.
    if args.command == "quit":
        return _cmd_quit(args)

    # All other commands (apply, preset, read) are handled by the UI app.
    try:
        from scribesense.ui.app import run as run_app
        return int(run_app(sys.argv))
    except ImportError:
        print("scribesense: GTK4 / libadwaita missing", file=sys.stderr)
        return int(ExitCode.FAILED)
    except Exception as exc:
        print(f"scribesense: failed to start: {exc}", file=sys.stderr)
        return int(ExitCode.FAILED)


if __name__ == "__main__":
    raise SystemExit(main())