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
from scribesense.contracts.errors import ErrorCode, ScribeSenseError  # noqa: E402
from scribesense.contracts.messages import PROTOCOL_VERSION  # noqa: E402
from scribesense.safety import ipc  # noqa: E402
from scribesense.ui import APP_ID  # noqa: E402
from scribesense.ui.window import ScribeSenseWindow  # noqa: E402


#: Commands forwarded to a running service over the K4 rule 5 socket. The
#: service side of these arrives with M2.11 / M4.12; until then, a forward to
#: a non-listening service correctly returns SERVICE_UNAVAILABLE.
_FORWARD_TO_SERVICE: frozenset[str] = frozenset({"apply", "preset", "read"})

#: Commands handled locally by the app (focus the window).
_LOCAL: frozenset[str] = frozenset({"", "quit"})

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
        # Not implemented yet; report rather than silently do nothing.
        sys.stderr.write(f"scribesense: '{name}' is not implemented yet\n")

    # ---------------------------------------------------------- app lifecycle

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

        if command in _LOCAL:
            self.activate()
            command_line.set_exit_status(int(ExitCode.OK))
            return int(ExitCode.OK)

        if command in _FORWARD_TO_SERVICE:
            status = self._forward(command, rest[1:])
            command_line.set_exit_status(int(status))
            return int(status)

        sys.stderr.write(f"scribesense: unknown command '{command}'\n")
        command_line.set_exit_status(int(ExitCode.USAGE))
        return int(ExitCode.USAGE)

    def _forward(self, command: str, args: Sequence[str]) -> ExitCode:
        """Send one request to the running service over the K4 rule 5 socket."""
        message = {
            "v": PROTOCOL_VERSION,
            "type": command,
            "args": list(args),
        }
        try:
            reply = ipc.request(message, timeout=5.0)
        except ScribeSenseError as exc:
            # SERVICE_UNREACHABLE is the expected case when nothing listens.
            sys.stderr.write(f"scribesense: service unavailable ({exc.code.value})\n")
            return ExitCode.SERVICE_UNAVAILABLE
        except OSError as exc:
            # Defensive: ipc.request already converts these, but be explicit.
            sys.stderr.write(f"scribesense: service unavailable ({exc!s})\n")
            return ExitCode.SERVICE_UNAVAILABLE

        if not reply.get("ok", False):
            sys.stderr.write(f"scribesense: {reply.get('error', 'command failed')}\n")
            return ExitCode.FAILED
        return ExitCode.OK


def run(argv: Sequence[str] | None = None) -> int:
    """Entry point used by cli.main. Ensures a GLib main loop exists and runs."""
    app = ScribeSenseApp()
    args = list(argv) if argv is not None else sys.argv
    return int(app.run(args))