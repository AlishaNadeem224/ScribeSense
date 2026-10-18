"""M2.4 — the Adw.Application and the single-instance service (A1), with the apply flow (M2.1 -> M2.3 ->
M2.6) and the app side of M2.9 / M2.11.

Two "single-instance" concepts coexist in ScribeSense, on purpose:

  1. The apply lock (safety/lock.py, K4 rule 1) serialises transactions. It is
     held for seconds to a minute and released when the transaction ends.

  2. This application registration serialises the GUI service. It is held for
     the whole session, so launching `scribesense` twice focuses the existing
     window instead of starting a second process.

ARCHITECTURE.md K4 rule 1 is explicit that they are separate concepts.

The app instance IS the service (A1): it opens one controller.service.Service (store, lock, journal,
K4 rule 5 socket, adapters, M2.1 controller) and keeps it until Quit. The Service is opened LAZILY, on the
first activate / command that needs it — not in `do_startup` — so `scribesense quit` with nothing running
quits without migrating the store, pruning the journal, removing unused fonts or opening the socket.
A later launch's argv arrives in `do_command_line` (K10: `apply <id>`, `preset next`,
`read --selection|--clipboard`, `quit`).

M2.11 "exit code 0 only on success": `apply`, `preset next` and `read` keep the forwarded
Gio.ApplicationCommandLine open until the work has finished (`_CommandLineReply`), so the launching
`scribesense` process exits with the real outcome (`apply_exit_code`, `original_exit_code`,
`capture_exit_code`): 0 only for a kept apply / a shown text, 3 for a busy lock, 1 otherwise.

Explicit Quit (A1 "exits safely"): refused (BUSY / a message) while a service call is still running (an apply
writing, an Original); during the confirmation countdown it proceeds and `do_shutdown` REVERTS the
unconfirmed tx (A15) before closing the service.

`scribesense confirm <tx>` is the one command that must never be forwarded to the running instance (A15):
it runs as a fresh process (ui/confirm.py). This app STARTS that process after an apply.

Apply flow (window Apply, `apply <id>`, `preset next`), never on the GTK thread:
  worker 1  service.apply(preset)                     -> ApplyReport (tx AWAITING_CONFIRMATION)
  worker 1  run_cmd([python, -m, scribesense.cli, confirm, tx], timeout=CONFIRM_SECONDS + GRACE)
            (the confirm window answers keep/revert over the socket; its exit status is ignored)
  worker 1  service.confirm_watchdog(tx)              -> reverts if still pending (A15)
  GTK       kept -> push the M2.6 coverage report (with the M2.2 restart notice); otherwise the reason
            in plain words. A forwarded command line gets its exit status now.
The pure helpers below (preset lookup, next-preset cycling, wording, exit codes, reader font choice) have
no GTK state and are unit-tested in tests/unit/ui/test_app_logic.py.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Sequence
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib  # noqa: E402

from scribesense.config import presets as preset_catalogue  # noqa: E402
from scribesense.contracts import paths  # noqa: E402
from scribesense.contracts.cli import ExitCode  # noqa: E402
from scribesense.contracts.config import Configuration  # noqa: E402
from scribesense.contracts.errors import ErrorCode, ScribeSenseError  # noqa: E402
from scribesense.contracts.fonts import FontSet  # noqa: E402
from scribesense.contracts.journal import ActiveConfig  # noqa: E402
from scribesense.contracts.presets import ORIGINAL, Preset  # noqa: E402
from scribesense.contracts.states import TxState  # noqa: E402
from scribesense.controller.service import CONFIRM_GRACE_SECONDS, CONFIRM_SECONDS, Service  # noqa: E402
from scribesense.log import log  # noqa: E402
from scribesense.ui import APP_ID  # noqa: E402
from scribesense.ui.settings_page import error_text, run_async  # noqa: E402
from scribesense.ui.window import ScribeSenseWindow, recovery_text  # noqa: E402

#: The one command that must never be forwarded and never registers with Gio (A15).
_FRESH_PROCESS: frozenset[str] = frozenset({"confirm"})


# ================================================================ pure logic (no GTK state)

class UsageError(Exception):
    """A K10 command with missing or unknown arguments -> exit 2."""


def parse_command(rest: Sequence[str]) -> tuple[str, str | None]:
    """K10 service commands -> (kind, argument). Raises UsageError for anything malformed.

    kinds: "open" (no command), "quit", "confirm", "apply" (preset id), "preset-next",
           "read" ("selection" | "clipboard").
    """
    args = list(rest)
    if not args:
        return "open", None
    command, tail = args[0], args[1:]
    if command == "quit" and not tail:
        return "quit", None
    if command in _FRESH_PROCESS:
        return "confirm", None
    if command == "apply" and len(tail) == 1 and tail[0]:
        return "apply", tail[0]
    if command == "preset" and tail == ["next"]:
        return "preset-next", None
    if command == "read" and tail in (["--selection"], ["--clipboard"]):
        return "read", tail[0][2:]
    raise UsageError(command)


def known_presets(user_presets: Sequence[Preset] = ()) -> list[Preset]:
    """Built-ins, then saved user presets (K9 list order), without duplicates or the Custom row."""
    out: list[Preset] = list(preset_catalogue.BUILTINS)
    seen = {p.id for p in out}
    for preset in user_presets:
        if preset.kind == "user" and preset.id not in seen:
            out.append(preset)
            seen.add(preset.id)
    return out


def find_preset(preset_id: str, presets: Sequence[Preset]) -> Preset | None:
    """`scribesense apply <id>`: the preset with exactly this id, or None."""
    return next((p for p in presets if p.id == preset_id), None)


def next_preset(presets: Sequence[Preset], active_id: str | None) -> Preset | None:
    """`scribesense preset next`: the preset after the active one, wrapping around.

    Nothing active (Original), or an active id that is not in the list (Custom, a deleted preset) ->
    the first preset. Empty list -> None.
    """
    if not presets:
        return None
    for i, preset in enumerate(presets):
        if preset.id == active_id:
            return presets[(i + 1) % len(presets)]
    return presets[0]


#: ApplyReport.reason_code / start-up codes -> plain words.
_REASONS: dict[str, str] = {
    ErrorCode.APPLY_IN_PROGRESS.value: "Another change is still in progress.",
    ErrorCode.LOCK_TIMEOUT.value: "Another ScribeSense task is running. Try again in a minute.",
    ErrorCode.NO_DISK_SPACE.value: "There is not enough free disk space.",
    ErrorCode.NOT_WRITABLE.value: "A settings file could not be written.",
    ErrorCode.BASE_IS_MONOSPACE.value: "The chosen base font is a monospace font, which cannot be used.",
    ErrorCode.BASE_IS_ICON_FONT.value: "The chosen base font is an icon font, which cannot be used.",
    ErrorCode.BUNDLED_FONT_MISSING.value: "A font that ships with ScribeSense is missing.",
    ErrorCode.FONT_UNREADABLE.value: "The base font could not be read.",
    ErrorCode.VALIDATION_FAILED.value: "The generated fonts did not pass their checks.",
    ErrorCode.INSTALL_FAILED.value: "The generated fonts could not be installed.",
    ErrorCode.PATCH_UNSUPPORTED.value: "This font cannot be adjusted.",
    ErrorCode.PLAN_CONFLICT.value: "Two parts of the change wanted the same file.",
    ErrorCode.CHANGED_DURING_APPLY.value: "A setting was changed by another program during the change.",
    ErrorCode.ADAPTER_UNAVAILABLE.value: "A required part of the desktop is not available.",
    ErrorCode.STORE_BUSY.value: "ScribeSense's data is busy. Try again.",
    ErrorCode.STORE_READ_ONLY.value: "ScribeSense's data is read-only.",
    ErrorCode.STORE_CORRUPT.value: "ScribeSense's data is damaged.",
    ErrorCode.STORE_IO_ERROR.value: "ScribeSense's data could not be read or written.",
    ErrorCode.SCHEMA_UNSUPPORTED.value: "ScribeSense's data was written by a newer version.",
    ErrorCode.SERVICE_UNREACHABLE.value: "The ScribeSense background service is not running.",
    ErrorCode.TX_NOT_PENDING.value: "The change was no longer waiting for confirmation.",
    # A3 (M4.10, 2026-10-18): a new Apply is refused until an earlier, unfinished change has been undone.
    ErrorCode.RECOVERY_OBLIGATIONS_REMAIN.value: (
        "An earlier change was not fully undone yet. Run “scribesense reset” (or press "
        "Ctrl+Alt+Shift+Super+Backspace), then try again."
    ),
}


def reason_text(code: str | None) -> str:
    """Plain words for a reason code; unknown codes get a generic sentence. The code is always shown."""
    if not code:
        return "The change did not complete."
    phrase = _REASONS.get(code, "The change could not be completed.")
    return f"{phrase} ({code})"


def report_text(status: str, reason_code: str | None) -> str:
    """The message for an ApplyReport that is not awaiting confirmation."""
    if status == "stopped":
        return f"Nothing was changed. {reason_text(reason_code)}"
    if status == "reverted":
        return f"The change was undone. {reason_text(reason_code)}"
    if status == "revert_failed":
        return ("The change failed and some settings could not be restored. ScribeSense will retry at "
                f"your next login, or run “scribesense reset”. {reason_text(reason_code)}")
    return reason_text(reason_code)


def outcome_text(state: TxState | None) -> str:
    """After the confirm window: what happened to the tx."""
    if state == TxState.COMPLETED:
        return "New settings kept."
    if state == TxState.REVERTED:
        return "Your previous settings are back."
    if state == TxState.REVERT_FAILED:
        return ("Some settings could not be restored. ScribeSense will retry at your next login, or run "
                "“scribesense reset”.")
    return "The change was not confirmed."


def reset_text(report: Any) -> str:
    """Summary of service.original() / reset (RevertReport counts)."""
    restored, skipped, failed = (len(getattr(report, k, ())) for k in ("restored", "skipped", "failed"))
    if failed:
        return (f"Original: restored {restored}, skipped {skipped}, failed {failed}. Failed items are "
                "retried at your next login.")
    if skipped:
        return (f"Original: restored {restored}. {skipped} setting(s) were changed by another program and "
                "were left alone.")
    return f"Original settings restored ({restored})."


#: M2.10 capture codes -> distinct messages (empty and too-large are different, A17).
CAPTURE_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.NOTHING_SELECTED: "Nothing is selected. Select some text first.",
    ErrorCode.SELECTION_UNAVAILABLE: "The selected text could not be read.",
    ErrorCode.CLIPBOARD_EMPTY: "The clipboard has no text. Copy some text first.",
    ErrorCode.TOO_LARGE: "This text is over 200 000 characters. Nothing was shortened: copy a smaller part.",
    ErrorCode.BAD_MESSAGE: "The text could not be read.",
}


def capture_message(code: ErrorCode) -> str:
    return f"{CAPTURE_MESSAGES.get(code, 'The text could not be opened in the reader.')} ({code.value})"


_STARTUP: dict[ErrorCode, str] = {
    ErrorCode.APPLY_IN_PROGRESS: "Another ScribeSense service is already running, so this window cannot "
                                 "apply changes.",
}


def startup_text(code: ErrorCode | None) -> str:
    if code is None:
        return "The ScribeSense background service could not start, so changes cannot be applied."
    phrase = _STARTUP.get(code, "The ScribeSense background service could not start, so changes cannot be "
                                "applied.")
    return f"{phrase} ({code.value})"


_BUSY_REASONS: frozenset[str] = frozenset({ErrorCode.APPLY_IN_PROGRESS.value, ErrorCode.LOCK_TIMEOUT.value})


def _error_exit(exc: BaseException) -> ExitCode:
    code = exc.code.value if isinstance(exc, ScribeSenseError) else None
    return ExitCode.BUSY if code in _BUSY_REASONS else ExitCode.FAILED


def apply_exit_code(outcome: object) -> ExitCode:
    """M2.11 / K10 exit code for `apply <id>` / `preset next`, from the apply flow's outcome.

    `outcome` is what `_apply_worker` returned — (ApplyReport, final TxState | None) — or the exception it
    raised. 0 only when the change was applied AND kept (COMPLETED); 3 when another change held the lock
    (APPLY_IN_PROGRESS / LOCK_TIMEOUT); 1 for stopped, reverted, revert_failed or not confirmed.
    """
    if isinstance(outcome, BaseException):
        return _error_exit(outcome)
    report, state = outcome  # type: ignore[misc]
    if report.status != "awaiting_confirmation":
        return ExitCode.BUSY if report.reason_code in _BUSY_REASONS else ExitCode.FAILED
    return ExitCode.OK if state == TxState.COMPLETED else ExitCode.FAILED


def original_exit_code(outcome: object) -> ExitCode:
    """`apply original` (revert everything): same rule as `scribesense reset` — 0 unless an item failed."""
    if isinstance(outcome, BaseException):
        return _error_exit(outcome)
    return ExitCode.FAILED if getattr(outcome, "failed", None) else ExitCode.OK


def capture_exit_code(outcome: object) -> ExitCode:
    """`read --selection|--clipboard`: 0 only when text was captured and handed to the reader.

    `outcome` is `_capture_worker`'s (code, text, font) or its exception. NOTHING_SELECTED, CLIPBOARD_EMPTY,
    TOO_LARGE, … are failures (1).
    """
    if isinstance(outcome, BaseException):
        return ExitCode.FAILED
    code = outcome[0]  # type: ignore[index]
    return ExitCode.OK if code is None else ExitCode.FAILED


def reader_font(
    active: ActiveConfig | None,
    installed: Callable[[], list[FontSet]],
    default_config: Callable[[], Configuration],
) -> tuple[str | None, Configuration | None]:
    """M2.9: (reading family, configuration) for the reader.

    The ACTIVE (confirmed) configuration's own font set — looked up by its content key, never by list
    order. Only an ALREADY INSTALLED set is used: the reader never builds or installs fonts, because K7
    `build()` installs into the shared fonts folder ("for Apply") and doing that outside a journaled apply
    would add a font family to every app without a record or the apply lock.

    Nothing active (Original) -> the Default preset's configuration [P] (its size and line height) in the
    system font. Active set not installed -> the active configuration, system font.
    """
    if active is None:
        return None, default_config()
    found = next((f for f in installed() if f.key == active.font_key), None)
    return (found.reading_family if found is not None else None), active.config


#: Explicit Quit refused while a change is being written (A1 "exits safely").
QUIT_BUSY_TEXT = ("ScribeSense is still making a change, so it did not quit. Quit again when the change has "
                  "finished.")


def _default_config() -> Configuration:
    return preset_catalogue.resolve(preset_catalogue.builtin(preset_catalogue.DEFAULT_ID).values)


# ================================================================ the application

#: Called once with the final exit code (and an optional message for the launching terminal).
Finished = Callable[[ExitCode, str | None], None]


class _CommandLineReply:
    """Keeps a forwarded `scribesense …` command line open until the work it started has finished (M2.11).

    Gio sends the remote process its exit status when the Gio.ApplicationCommandLine is released (or, on
    GLib >= 2.80, at `done()`). Holding a reference here and setting the status from the work's done-callback
    makes the launching process exit with the REAL outcome instead of 0 at once. `do_command_line` returns a
    provisional FAILED, so a service that dies before answering never reports success.
    """

    def __init__(self, command_line: Gio.ApplicationCommandLine, open_replies: set[_CommandLineReply]) -> None:
        self._command_line: Gio.ApplicationCommandLine | None = command_line
        self._open = open_replies
        open_replies.add(self)

    def __call__(self, code: ExitCode, message: str | None = None) -> None:
        """The deferred answer (GTK thread). Only the first call counts."""
        command_line = self._release()
        if command_line is None:
            return
        ScribeSenseApp._finish(command_line, code, message)
        done = getattr(command_line, "done", None)  # GLib >= 2.80: reply now, not at finalisation
        if callable(done):
            done()

    def now(self, code: ExitCode, message: str | None = None) -> int:
        """The work did not start: answer synchronously (the return value of do_command_line)."""
        command_line = self._release()
        if command_line is None:
            return int(code)
        return ScribeSenseApp._finish(command_line, code, message)

    def _release(self) -> Gio.ApplicationCommandLine | None:
        command_line, self._command_line = self._command_line, None
        self._open.discard(self)
        return command_line


class ScribeSenseApp(Adw.Application):
    """The primary application instance and background service (A1)."""

    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            # A1: deliver a second launch's argv here instead of starting a new process.
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self._window: ScribeSenseWindow | None = None
        self.service: Service | None = None
        self._service_tried = False  # Service.open() is attempted once, lazily (see `_ensure_service`)
        self._startup_problem: str | None = None
        self._busy = False  # an apply / original flow is running (GTK thread only)
        # Set on the GTK thread when a flow starts, cleared by the worker when it leaves the service call
        # (service.apply / service.original). While set, Quit is refused: the store must not close under it.
        self._in_service_call = threading.Event()
        self._open_replies: set[_CommandLineReply] = set()
        self._install_actions()

    # ---------------------------------------------------------------- actions (M2.4 card)

    def _install_actions(self) -> None:
        for name in ("preset-next", "open-reader", "quit"):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", self._on_action, name)
            self.add_action(action)
        apply_action = Gio.SimpleAction.new("apply", GLib.VariantType.new("s"))
        apply_action.connect("activate", lambda _a, param: self._apply_action(param.get_string()))
        self.add_action(apply_action)

    def _apply_action(self, preset_id: str) -> None:
        self._ensure_service()
        result = self.apply_by_id(preset_id)
        if result is not None and result[0] == ExitCode.USAGE:
            self.notify(f"There is no preset with the id “{preset_id}”.", present=True)

    def _on_action(self, _action: Gio.SimpleAction, _param: object, name: str) -> None:
        if name == "quit":
            code, _message = self.request_quit()  # A1: only an explicit Quit stops the service
            if code != ExitCode.OK:
                self.notify(QUIT_BUSY_TEXT, present=True)
            return
        self._ensure_service()
        if name == "preset-next":
            self.preset_next()
        elif name == "open-reader":
            self.read("selection")

    # ---------------------------------------------------------------- lifecycle

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        # A1: the service outlives its window. Without a hold, Gio quits when the last window closes.
        self.hold()
        # The Service is NOT opened here: `scribesense quit` must not migrate, prune or delete fonts.

    def _ensure_service(self) -> Service | None:
        """Open the Service on first use (A1). A start-up error is remembered and shown, never raised."""
        if self.service is None and not self._service_tried:
            self._service_tried = True
            try:
                self.service = Service.open(on_reader_text=self._reader_text_from_service)
            except ScribeSenseError as exc:
                log(exc.code, "ui", step="startup")
                self._startup_problem = startup_text(exc.code)
            except Exception:  # noqa: BLE001 — M2.4: a start-up error shows a message, never a dead app
                self._startup_problem = startup_text(None)
        return self.service

    def request_quit(self) -> tuple[ExitCode, str | None]:
        """Explicit Quit (A1 "exits safely"): refused while a service call is still running.

        During the confirmation countdown the apply has returned, so Quit proceeds and `do_shutdown`
        reverts the unconfirmed tx.
        """
        if self._in_service_call.is_set():
            return ExitCode.BUSY, f"scribesense: {QUIT_BUSY_TEXT}"
        self.quit()
        return ExitCode.OK, None

    def do_shutdown(self) -> None:
        # M2.5 / K7 rule 2: release the live preview's private font files before anything else.
        editor = getattr(getattr(self, "_window", None), "editor", None)
        preview = getattr(editor, "preview", None)
        if preview is not None:
            try:
                preview.shutdown()
            except Exception:  # noqa: BLE001 — the atexit net in ui/preview.py still removes them
                log(ErrorCode.STORE_IO_ERROR, "ui", step="preview_shutdown")
        service, self.service = self.service, None
        if service is not None:
            # A15 / M2.3: a change nobody confirmed is reverted at Quit too — login recovery is for crashes.
            # `pending_tx` takes the service mutex, so a keep / revert / reset still running on the socket
            # thread finishes first.
            try:
                pending = service.pending_tx
                if pending is not None:
                    service.revert(pending)
            except Exception as exc:  # noqa: BLE001 — Quit must still close the store and socket
                code = exc.code if isinstance(exc, ScribeSenseError) else ErrorCode.STORE_IO_ERROR
                log(code, "ui", step="shutdown")  # TX_NOT_PENDING: kept / reverted meanwhile
            try:
                service.close()
            except ScribeSenseError as exc:
                log(exc.code, "ui", step="shutdown")
        Adw.Application.do_shutdown(self)

    def _ensure_window(self) -> ScribeSenseWindow:
        if self._window is None:
            self._ensure_service()
            self._window = ScribeSenseWindow(
                application=self,
                app_id=APP_ID,
                service=self.service,
                on_apply=self.start_apply,
                on_original=self.start_original,
                on_read_clipboard=lambda: self.read("clipboard"),
            )
            if self._startup_problem is not None:
                self._window.show_startup_error(self._startup_problem)
        return self._window

    def do_activate(self) -> None:
        self._ensure_window().present()

    def notify(self, text: str, *, present: bool = False) -> None:
        """Show a message in the window (toast). `present` brings the window up so it is seen."""
        window = self._ensure_window()
        if present and not window.get_visible():
            window.present()
        window.show_toast(text)

    # ---------------------------------------------------------------- command line (A1, K10)

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        """Every launch's argv arrives here (primary instance). Returns the K10 exit code.

        `apply` / `preset next` / `read` that start work return a provisional FAILED; the real code is set
        when the work finishes (`_CommandLineReply`).
        """
        rest = list(command_line.get_arguments())[1:]  # argv[0] is "scribesense"
        try:
            kind, arg = parse_command(rest)
        except UsageError:
            return self._finish(command_line, ExitCode.USAGE, f"scribesense: invalid command: {' '.join(rest)}")

        if kind == "confirm":
            # A15: confirm must never be forwarded; cli.main runs it as a fresh process.
            return self._finish(command_line, ExitCode.FAILED,
                                "scribesense: 'confirm' must run as a fresh process (A15)")
        if kind == "quit":
            return self._finish(command_line, *self.request_quit())  # never opens the Service

        self._ensure_service()
        if kind == "open":
            self.activate()
            return self._finish(command_line, ExitCode.OK)

        reply = _CommandLineReply(command_line, self._open_replies)
        if kind == "read":
            self.read(arg or "selection", on_finished=reply)
            return int(ExitCode.FAILED)  # provisional
        result = (self.apply_by_id(arg or "", on_finished=reply) if kind == "apply"
                  else self.preset_next(on_finished=reply))
        if result is not None:
            return reply.now(*result)
        return int(ExitCode.FAILED)  # provisional

    @staticmethod
    def _finish(command_line: Gio.ApplicationCommandLine, code: ExitCode, message: str | None = None) -> int:
        if message:
            command_line.printerr_literal(message + "\n")  # reaches the launching terminal
        command_line.set_exit_status(int(code))
        return int(code)

    # ---------------------------------------------------------------- presets (K10)

    def _presets(self) -> list[Preset]:
        user: list[Preset] = []
        if self.service is not None:
            try:
                user = list(self.service.store.presets.list())
            except ScribeSenseError as exc:
                log(exc.code, "ui", step="presets")
        return known_presets(user)

    def apply_by_id(self, preset_id: str, on_finished: Finished | None = None) -> tuple[ExitCode, str | None] | None:
        """`scribesense apply <id>` ("original" = revert everything).

        Returns the exit code when nothing was started; None when the flow started (`on_finished` gets the
        final code later).
        """
        if self.service is None:
            self.notify(self._startup_problem or startup_text(None), present=True)
            return ExitCode.SERVICE_UNAVAILABLE, "scribesense: the background service is not running"
        if preset_id == ORIGINAL:
            return self._started(self.start_original(on_finished))
        preset = find_preset(preset_id, self._presets())
        if preset is None:
            return ExitCode.USAGE, f"scribesense: no preset with id '{preset_id}'"
        return self._started(self.start_apply(preset, on_finished))

    def preset_next(self, on_finished: Finished | None = None) -> tuple[ExitCode, str | None] | None:
        """`scribesense preset next`: apply the preset after the active one (return value: see apply_by_id)."""
        if self.service is None:
            self.notify(self._startup_problem or startup_text(None), present=True)
            return ExitCode.SERVICE_UNAVAILABLE, "scribesense: the background service is not running"
        try:
            active = self.service.journal.active()
        except ScribeSenseError as exc:
            log(exc.code, "ui", step="active")
            self.notify(reason_text(exc.code.value), present=True)
            return _error_exit(exc), f"scribesense: {exc.code.value}"
        preset = next_preset(self._presets(), active.source_preset_id if active is not None else None)
        if preset is None:
            return ExitCode.FAILED, "scribesense: no presets"
        return self._started(self.start_apply(preset, on_finished))

    @staticmethod
    def _started(ok: bool) -> tuple[ExitCode, str | None] | None:
        return None if ok else (ExitCode.BUSY, "scribesense: another change is in progress")

    # ---------------------------------------------------------------- apply flow (M2.1 -> M2.3 -> M2.6)

    def start_apply(self, preset: Preset, on_finished: Finished | None = None) -> bool:
        """Start the apply flow off the GTK thread. False if it could not start (busy / no service)."""
        if self.service is None:
            self.notify(self._startup_problem or startup_text(None), present=True)
            return False
        if self._busy:
            self.notify(reason_text(ErrorCode.APPLY_IN_PROGRESS.value), present=True)
            return False
        self._busy = True
        self._in_service_call.set()
        self.notify(f"Applying “{preset.name}”…")
        service, in_call = self.service, self._in_service_call
        run_async(lambda: self._apply_worker(service, preset, in_call),
                  lambda outcome: self._apply_done(outcome, on_finished))
        return True

    @staticmethod
    def _apply_worker(service: Service, preset: Preset,
                      in_call: threading.Event | None = None) -> tuple[Any, TxState | None]:
        """Worker thread: apply, start the fresh confirm process, then the watchdog. Never touches GTK.

        `in_call` is cleared as soon as `service.apply` returns: from then on Quit may proceed (the
        unconfirmed tx is reverted by do_shutdown).
        """
        try:
            report = service.apply(preset)
        finally:
            if in_call is not None:
                in_call.clear()
        if report.status != "awaiting_confirmation" or report.tx is None:
            return report, None
        try:
            paths.run_cmd([sys.executable, "-m", "scribesense.cli", "confirm", str(report.tx)],
                          timeout=CONFIRM_SECONDS + CONFIRM_GRACE_SECONDS)
        except (ScribeSenseError, OSError):
            pass  # its exit status is ignored: the watchdog below decides
        service.confirm_watchdog(report.tx)  # reverts if nobody answered (A15)
        try:
            state: TxState | None = service.journal.state(report.tx)
        except ScribeSenseError as exc:
            log(exc.code, "ui", step="confirm")
            state = None
        return report, state

    def _apply_done(self, outcome: object, on_finished: Finished | None = None) -> None:
        self._busy = False
        self._in_service_call.clear()
        text = self._show_apply_outcome(outcome)
        if on_finished is not None:
            code = apply_exit_code(outcome)
            on_finished(code, None if code == ExitCode.OK else f"scribesense: {text}")

    def _show_apply_outcome(self, outcome: object) -> str:
        """Tell the user what happened (GTK thread); returns the text shown."""
        if isinstance(outcome, BaseException):
            if isinstance(outcome, ScribeSenseError):
                log(outcome.code, "ui", step="apply")
                text = f"Nothing was changed. {reason_text(outcome.code.value)}"
            else:
                text = f"Nothing was changed. {error_text(outcome)}"
            self.notify(text, present=True)
            return text
        report, state = outcome  # type: ignore[misc]
        if report.status != "awaiting_confirmation":
            text = report_text(report.status, report.reason_code)
            self.notify(text, present=True)
            return text
        window = self._ensure_window()
        if state == TxState.COMPLETED:
            window.show_coverage(report.coverage)  # M2.6 (+ M2.2 restart notice), reachable again
        text = outcome_text(state)
        window.show_toast(text)
        if state != TxState.COMPLETED and not window.get_visible():
            window.present()
        return text

    def start_original(self, on_finished: Finished | None = None) -> bool:
        """"Original" = revert everything (service.original()), off the GTK thread."""
        if self.service is None:
            self.notify(f"{self._startup_problem or startup_text(None)} {recovery_text(None)}", present=True)
            return False
        if self._busy:
            self.notify(reason_text(ErrorCode.APPLY_IN_PROGRESS.value), present=True)
            return False
        self._busy = True
        self._in_service_call.set()
        self.notify("Restoring your original settings…")
        service, in_call = self.service, self._in_service_call

        def work() -> Any:
            try:
                return service.original()
            finally:
                in_call.clear()

        run_async(work, lambda outcome: self._original_done(outcome, on_finished))
        return True

    def _original_done(self, outcome: object, on_finished: Finished | None = None) -> None:
        self._busy = False
        self._in_service_call.clear()
        if isinstance(outcome, BaseException):
            if isinstance(outcome, ScribeSenseError):
                log(outcome.code, "ui", step="original")
            text = f"Original did not finish. {error_text(outcome)}"
        else:
            text = reset_text(outcome)
        self.notify(text, present=True)
        if on_finished is not None:
            code = original_exit_code(outcome)
            on_finished(code, None if code == ExitCode.OK else f"scribesense: {text}")

    # ---------------------------------------------------------------- reader (M2.9 / M2.10)

    def read(self, source: str, on_finished: Finished | None = None) -> None:
        """`read --selection|--clipboard` and the window's reader button: capture, then show."""
        run_async(lambda: self._capture_worker(source), lambda outcome: self._capture_done(outcome, on_finished))

    def _capture_worker(self, source: str) -> tuple[ErrorCode | None, str | None, Any]:
        from scribesense.reader import capture

        result = capture.from_selection() if source == "selection" else capture.from_clipboard()
        if result.code is not None:
            return result.code, None, None
        return None, result.text, self._reader_font()

    def _capture_done(self, outcome: object, on_finished: Finished | None = None) -> None:
        text = self._show_capture_outcome(outcome)
        if on_finished is not None:
            code = capture_exit_code(outcome)
            on_finished(code, None if code == ExitCode.OK else f"scribesense: {text}")

    def _show_capture_outcome(self, outcome: object) -> str:
        """Show the reader, or the capture problem (GTK thread). Returns the message (never the text)."""
        if isinstance(outcome, BaseException):
            code = outcome.code if isinstance(outcome, ScribeSenseError) else None
            if code is not None:
                log(code, "reader", step="capture")
            message = capture_message(code) if code else error_text(outcome)
            self.notify(message, present=True)
            return message
        code, text, font = outcome  # type: ignore[misc]
        if code is not None:
            log(code, "reader", step="capture")  # the code only — never the text
            message = capture_message(code)
            self.notify(message, present=True)
            return message
        self._show_reader(text, *font)
        return ""

    def _reader_text_from_service(self, text: str) -> None:
        """K11 open_in_reader (called on the socket thread): resolve the font off the GTK thread."""

        def work() -> None:
            try:
                font = self._reader_font()
            except Exception:  # noqa: BLE001 — the text must still be shown, in the system font
                font = (None, None)
            GLib.idle_add(self._show_reader, text, *font)

        threading.Thread(target=work, name="scribesense-reader", daemon=True).start()

    def _reader_font(self) -> tuple[str | None, Configuration | None]:
        """Worker thread: the active configuration's installed reading family (never builds, see reader_font)."""
        from scribesense.fontgen import generator

        active: ActiveConfig | None = None
        if self.service is not None:
            try:
                active = self.service.journal.active()
            except ScribeSenseError as exc:
                log(exc.code, "reader", step="active")
        try:
            return reader_font(active, generator.installed_sets, _default_config)
        except ScribeSenseError as exc:
            log(exc.code, "reader", step="font")
            return None, active.config if active is not None else None

    def _show_reader(self, text: str, family: str | None, config: Configuration | None) -> bool:
        from scribesense.reader.popup import open_reader

        try:
            open_reader(self, text, family=family, config=config)
        except ValueError:
            open_reader(self, text, family=None, config=None)  # unusable family/config: system font
        return False  # one-shot GLib.idle_add callback


def run(argv: Sequence[str] | None = None) -> int:
    """Entry point used by cli.main. Ensures a GLib main loop exists and runs."""
    app = ScribeSenseApp()
    args = list(argv) if argv is not None else sys.argv
    return int(app.run(args))
