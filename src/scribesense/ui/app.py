"""M2.4 — the Adw.Application and the single-instance service.

Two "single-instance" concepts coexist in ScribeSense, on purpose:

  1. The apply lock (safety/lock.py, K4 rule 1) serialises transactions. It is
     held for seconds to a minute and released when the transaction ends.

  2. This application registration serialises the GUI service. It is held for
     the whole session, so launching `scribesense` twice focuses the existing
     window instead of starting a second process.

ARCHITECTURE.md K4 rule 1 is explicit that they are separate concepts.

`scribesense confirm <tx>` is the one command that must never be forwarded to
the running instance (A15): it must run as a fresh process so a bug in the
service cannot fake a confirmation.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio  # noqa: E402

from scribesense.contracts.cli import ExitCode  # noqa: E402
from scribesense.ui import APP_ID  # noqa: E402
from scribesense.ui.window import ScribeSenseWindow  # noqa: E402


#: Commands the service runs itself. This instance IS the service (A1): Gio
#: delivers a later launch's argv here, so nothing is forwarded over the K4
#: rule 5 socket — that socket is for `confirm`, `reset` and the native host.
#: command -> (action, card that implements it)
_SERVICE_COMMANDS: dict[str, tuple[str, str]] = {
    "apply": ("apply", "M2.11"),
    "preset": ("preset-next", "M2.11"),
    "read": ("open-reader", "M2.9"),
}

#: The one command that must never be forwarded and never registers with Gio.
_FRESH_PROCESS: frozenset[str] = frozenset({"confirm"})


class ScribeSenseApp(Adw.Application):
    """The primary application instance. A second launch reaches
    `do_command_line`, where arguments are forwarded to the existing window
    (focus) or handed to the service.
    """

    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            # A1: deliver a second launch's argv here instead of starting a new
            # process.
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self._window: ScribeSenseWindow | None = None
        self._install_actions()

    # ---------------------------------------------------------------- actions

    def _install_actions(self) -> None:
        # `apply`, `preset-next`, `open-reader` are the three actions named in
        # M2.4's card. Their real bodies arrive with M2.1, M2.11 and M2.9.
        for name in ("apply", "preset-next", "open-reader", "quit"):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", self._on_action, name)
            self.add_action(action)

    def _on_action(self, _action: Gio.SimpleAction, _param: object, name: str) -> None:
        if name == "quit":
            # A1: only an explicit Quit stops the service.
            self.quit()
            return
        # Not implemented yet (M2.9, M2.11); report rather than silently do nothing.
        sys.stderr.write(f"scribesense: '{name}' is not implemented yet\n")

    # ---------------------------------------------------------- app lifecycle

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        # A1: the service outlives its window. Without a hold, Gio quits the
        # application when the last window closes; only Quit stops it.
        self.hold()

    def do_activate(self) -> None:
        if self._window is None:
            self._window = ScribeSenseWindow(application=self)
        self._window.present()

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        """A second launch's argv arrives here (A1).

        We never create a second window. The window is presented, and the
        requested command is either handled locally, forwarded to the service,
        or refused if it is `confirm`.
        """
        argv = list(command_line.get_arguments())
        rest = argv[1:]  # argv[0] is "scribesense"

        if not rest:
            self.activate()
            command_line.set_exit_status(int(ExitCode.OK))
            return int(ExitCode.OK)

        command = rest[0]

        # A15: `confirm` must never be forwarded. cli.main already refuses to
        # route it here; refuse defensively if it reaches us anyway.
        if command in _FRESH_PROCESS:
            sys.stderr.write(
                "scribesense: 'confirm' must run as a fresh process (A15)\n"
            )
            command_line.set_exit_status(int(ExitCode.FAILED))
            return int(ExitCode.FAILED)

        if command == "quit":
            # A1: `scribesense quit` stops the background service.
            self.quit()
            command_line.set_exit_status(int(ExitCode.OK))
            return int(ExitCode.OK)

        if command in _SERVICE_COMMANDS:
            status = self._run_service_command(command, rest[1:])
            command_line.set_exit_status(int(status))
            return int(status)

        sys.stderr.write(f"scribesense: unknown command '{command}'\n")
        command_line.set_exit_status(int(ExitCode.USAGE))
        return int(ExitCode.USAGE)

    def _run_service_command(self, command: str, args: Sequence[str]) -> ExitCode:
        """Run a K10 service command in this (the service) process."""
        _action, card = _SERVICE_COMMANDS[command]
        # The bodies arrive with their cards; until then, refuse clearly.
        sys.stderr.write(f"scribesense: '{command}' is not implemented yet ({card})\n")
        return ExitCode.FAILED

def run(argv: Sequence[str] | None = None) -> int:
    """Entry point used by cli.main. Ensures a GLib main loop exists and runs."""
    app = ScribeSenseApp()
    args = list(argv) if argv is not None else sys.argv
    return int(app.run(args))