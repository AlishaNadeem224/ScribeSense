"""M2.3 — the confirm window: `scribesense confirm <tx>` (A15, window side).

The service applies a preset, leaves the transaction AWAITING_CONFIRMATION and starts this module as a
FRESH process (`python -m scribesense.cli confirm <tx>`). Running in a new process proves the new fonts
load in an application started after the change, and a bug in the service cannot fake the answer.

Flow:
  1. Ask the service which tx is pending (K4 rule 5 socket, `{"type": "pending"}`). Service unreachable,
     or a different tx pending -> show a message and exit 1 (A15: only the pending tx may be confirmed).
  2. Show a sample paragraph, a CONFIRM_SECONDS countdown and Keep / Revert buttons.
  3. Keep -> `{"type": "keep", "tx": tx}`. Revert, closing the window or the countdown reaching 0 ->
     `{"type": "revert", "tx": tx}`. Exactly one answer is ever sent.
  4. Exit 0 when the service accepted the answer, 1 otherwise.

Liveness (M2.3 deliverables, W5.1 — built 2026-11-24 by O4 for O2 with the user's go-ahead):
  * the `pending` reply names the reading family; the window draws its sample in it and checks, in THIS fresh
    process, that the family really resolves (`family_matches`). If it does not, it reverts at once and says so;
  * only then it sends `{"type": "confirm_ready", "tx"}` — the 30 s countdown starts at readiness, not at process
    start;
  * its event loop sends `{"type": "heartbeat", "tx"}` every HEARTBEAT_SECONDS (never tied to user input, so
    inactivity is never a hang).
The service supervises all three (`Service.supervise_confirmation`): not ready in time, heartbeat lost, or no answer
after the countdown -> it reverts with CONFIRM_STARTUP_TIMEOUT / CONFIRM_HEARTBEAT_LOST / CONFIRM_TIMEOUT. If this
process crashes, the same supervision reverts; login recovery (A3) covers a crashed service.

Process isolation: this window uses its OWN application id (APP_ID + ".Confirm") with NON_UNIQUE, so it
never registers as, or forwards to, the running ScribeSense service (A15 "must never be forwarded").

The decision logic (`ConfirmSession`, `answer_message`, `pending_matches`, `deliver`) is GTK-free and
unit-tested in tests/unit/ui/test_app_logic.py; GTK is imported only inside `run_confirm`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from scribesense.contracts.cli import ExitCode
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.controller.service import CONFIRM_SECONDS, HEARTBEAT_SECONDS

Message = dict[str, Any]
Request = Callable[..., Message]
Event = Literal["keep", "revert", "close", "timeout"]

#: Socket deadline for each request. Keep is one journal commit; a revert runs the adapters' restores,
#: which can take several seconds. The watchdog still reverts if this deadline is missed.
PENDING_TIMEOUT_S = 5.0
ANSWER_TIMEOUT_S = 30.0

#: Shown in the window so the user can judge readability in the new font.
SAMPLE_TEXT = (
    "Can you read this comfortably? The quick brown fox jumps over the lazy dog. "
    "Letters, words and lines should look clear and evenly spaced."
)

#: event -> the answer it sends. Everything except an explicit Keep reverts (A15: never keep by default).
_ANSWERS: dict[str, str] = {"keep": "keep", "revert": "revert", "close": "revert", "timeout": "revert"}


def answer_message(event: str, tx: int) -> Message:
    """The socket message for a window event. Unknown events revert (fail safe)."""
    return {"type": _ANSWERS.get(event, "revert"), "tx": int(tx)}


def pending_matches(reply: Message, tx: int) -> bool:
    """True if the service's `pending` reply names exactly this tx (A15: only the pending tx)."""
    return reply.get("ok") is True and type(reply.get("tx")) is int and reply["tx"] == int(tx)


def countdown_text(seconds_left: int) -> str:
    return f"Keep ({max(0, int(seconds_left))})"


def fetch_pending(tx: int, request: Request) -> tuple[ErrorCode | None, str]:
    """Step 1: (None, reading family) if `tx` is the pending tx; otherwise (the code to show, "")."""
    try:
        reply = request({"type": "pending"}, timeout=PENDING_TIMEOUT_S)
    except ScribeSenseError as exc:
        return exc.code, ""
    if not pending_matches(reply, tx):
        return ErrorCode.TX_NOT_PENDING, ""
    family = reply.get("family")
    return None, family if isinstance(family, str) else ""


def check_pending(tx: int, request: Request) -> ErrorCode | None:
    """Step 1. None if `tx` is the pending tx; otherwise the code to show (and exit 1)."""
    return fetch_pending(tx, request)[0]


def family_matches(requested: str, resolved: str) -> bool:
    """The sample's font really is the new reading family (M2.3: "window checks its sample resolves to the intended
    family"). An unknown family ("": an older service) is not checked; a fallback font never matches."""
    return requested == "" or resolved.casefold() == requested.casefold()


def ready_message(tx: int) -> Message:
    return {"type": "confirm_ready", "tx": int(tx)}


def heartbeat_message(tx: int) -> Message:
    return {"type": "heartbeat", "tx": int(tx)}


def ready_seconds(reply: Message) -> int:
    """The countdown the service granted at readiness (falls back to CONFIRM_SECONDS)."""
    seconds = reply.get("seconds")
    return seconds if type(seconds) is int and 1 <= seconds <= 300 else CONFIRM_SECONDS


def deliver(message: Message, request: Request) -> tuple[ExitCode, ErrorCode | None]:
    """Send one answer. (OK, None) when the service accepted it; (FAILED, code) otherwise."""
    try:
        reply = request(message, timeout=ANSWER_TIMEOUT_S)
    except ScribeSenseError as exc:
        return ExitCode.FAILED, exc.code
    if reply.get("ok") is True:
        return ExitCode.OK, None
    try:
        code = ErrorCode(reply.get("error"))
    except ValueError:
        code = ErrorCode.BAD_MESSAGE
    return ExitCode.FAILED, code


class ConfirmSession:
    """The countdown and the one-answer rule, without GTK.

    `event(name)` returns the message to send for the FIRST decisive event and None afterwards, so a
    window close after Keep (or a timer tick after Revert) can never send a second answer.
    `tick()` counts one second down and returns the "timeout" message when the countdown reaches 0.
    """

    def __init__(self, tx: int, seconds: int = CONFIRM_SECONDS) -> None:
        self.tx = int(tx)
        self.seconds_left = int(seconds)
        self.answered: str | None = None

    def event(self, name: str) -> Message | None:
        if self.answered is not None:
            return None
        message = answer_message(name, self.tx)
        self.answered = message["type"]
        return message

    def tick(self) -> Message | None:
        if self.answered is not None:
            return None
        self.seconds_left -= 1
        if self.seconds_left <= 0:
            return self.event("timeout")
        return None


#: Plain words for the codes this window can meet.
_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.SERVICE_UNREACHABLE: "ScribeSense is not running, so there is nothing to confirm.",
    ErrorCode.TX_NOT_PENDING: "This change is no longer waiting for confirmation.",
    ErrorCode.BAD_MESSAGE: "ScribeSense sent an unexpected reply.",
    ErrorCode.ADAPTER_VERIFY_FAILED: ("The new font could not be loaded in a newly started application, so your "
                                      "previous settings are coming back."),
}


def message_for(code: ErrorCode) -> str:
    return f"{_MESSAGES.get(code, 'The answer could not be delivered.')} ({code.value})"


def run_confirm(tx: int, *, request: Request | None = None) -> int:
    """`scribesense confirm <tx>`: show the window, deliver one answer, return the K10 exit code."""
    import threading

    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Pango", "1.0")
    from gi.repository import Adw, Gio, GLib, Gtk, Pango

    from scribesense.log import log
    from scribesense.ui import APP_ID

    if request is None:
        from scribesense.safety.ipc import request as ipc_request

        request = ipc_request

    session = ConfirmSession(tx)
    result: dict[str, int] = {"exit": int(ExitCode.FAILED)}
    problem, family = fetch_pending(tx, request)
    if problem is not None:
        log(problem, "confirm", step="pending")

    app = Adw.Application(
        application_id=APP_ID + ".Confirm",
        flags=Gio.ApplicationFlags.NON_UNIQUE,  # A15: never forwarded to, or registered as, the service
    )

    def build(application: Adw.Application) -> None:
        window = Adw.ApplicationWindow(application=application, title="Keep these settings?")
        window.set_default_size(520, 300)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(24)
        heading = Gtk.Label(label="Keep these reading settings?", wrap=True, xalign=0.0)
        heading.add_css_class("title-2")
        box.append(heading)
        status = Gtk.Label(wrap=True, xalign=0.0)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, halign=Gtk.Align.END)

        def finish_later() -> None:
            GLib.idle_add(window.destroy)

        if problem is not None:
            status.set_label(message_for(problem))
            box.append(status)
            close = Gtk.Button(label="Close")
            close.connect("clicked", lambda _b: window.destroy())
            buttons.append(close)
            box.append(buttons)
            window.set_content(box)
            window.present()
            return

        sample = Gtk.Label(label=SAMPLE_TEXT, wrap=True, xalign=0.0)
        if family:  # draw the sample in the NEW reading font (M2.3)
            attrs = Pango.AttrList()
            attrs.insert(Pango.attr_family_new(family))
            sample.set_attributes(attrs)
        box.append(sample)
        status.set_label(
            f"If you do nothing, your previous settings come back in {CONFIRM_SECONDS} seconds."
        )
        box.append(status)
        revert_btn = Gtk.Button(label="Revert")
        keep_btn = Gtk.Button(label=countdown_text(session.seconds_left))
        keep_btn.add_css_class("suggested-action")
        # Accessibility (audit 2026-11-24): the visible Keep label changes every second; a fixed accessible name
        # stops a screen reader from re-reading the focused button each tick. The deadline is in the description.
        keep_btn.update_property([Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
                                 ["Keep these reading settings", status.get_label()])
        revert_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Revert to the previous settings"])
        buttons.append(revert_btn)
        buttons.append(keep_btn)
        box.append(buttons)

        def send(message: Message | None) -> None:
            if message is None:
                return
            keep_btn.set_sensitive(False)
            revert_btn.set_sensitive(False)
            status.set_label("Sending your answer…")

            def work() -> None:
                code, err = deliver(message, request)
                result["exit"] = int(code)
                if err is not None:
                    log(err, "confirm", step=message["type"])
                GLib.idle_add(done, err)

            threading.Thread(target=work, name="scribesense-confirm", daemon=True).start()

        def done(err: ErrorCode | None) -> bool:
            if err is None:
                finish_later()
            else:
                status.set_label(message_for(err))
                close = Gtk.Button(label="Close")
                close.connect("clicked", lambda _b: window.destroy())
                buttons.append(close)
            return False

        def on_tick() -> bool:
            message = session.tick()
            if session.answered is None:
                keep_btn.set_label(countdown_text(session.seconds_left))
                return True
            send(message)
            return False

        def on_close(_w: object) -> bool:
            message = session.event("close")
            if message is None:
                return False  # already answered: let it close
            send(message)
            return True  # stay open until the revert is delivered, then close

        def on_beat() -> bool:
            """The event loop is alive: tell the service (in a thread, so a slow socket never blocks the UI)."""
            if session.answered is not None:
                return False

            def work() -> None:
                try:
                    request(heartbeat_message(tx), timeout=PENDING_TIMEOUT_S)
                except ScribeSenseError:
                    pass  # the service supervises: a lost heartbeat reverts there

            threading.Thread(target=work, name="scribesense-heartbeat", daemon=True).start()
            return True

        def resolved_family() -> str:
            font = sample.get_pango_context().load_font(Pango.FontDescription.from_string(f"{family} 16"))
            if font is None:
                return ""
            return font.describe().get_family() or ""

        def start_when_ready() -> bool:
            """Sample + controls are shown: check the font, then signal readiness and start the countdown."""
            if not family_matches(family, resolved_family()):
                log(ErrorCode.ADAPTER_VERIFY_FAILED, "confirm", step="sample")
                status.set_label(message_for(ErrorCode.ADAPTER_VERIFY_FAILED))
                send(session.event("revert"))
                return False

            def work() -> None:
                try:
                    reply = request(ready_message(tx), timeout=PENDING_TIMEOUT_S)
                except ScribeSenseError as exc:
                    reply = {"ok": False, "error": exc.code.value}
                GLib.idle_add(on_ready, reply)

            threading.Thread(target=work, name="scribesense-ready", daemon=True).start()
            return False

        def on_ready(reply: Message) -> bool:
            if reply.get("ok") is not True:
                try:
                    code = ErrorCode(reply.get("error"))
                except ValueError:
                    code = ErrorCode.BAD_MESSAGE
                log(code, "confirm", step="ready")
                session.answered = "revert"  # the service will not take an answer any more
                done(code)
                return False
            session.seconds_left = ready_seconds(reply)  # the 30 s count starts now (M2.3)
            keep_btn.set_label(countdown_text(session.seconds_left))
            keep_btn.set_sensitive(True)
            revert_btn.set_sensitive(True)
            keep_btn.grab_focus()
            GLib.timeout_add_seconds(1, on_tick)
            GLib.timeout_add_seconds(HEARTBEAT_SECONDS, on_beat)
            return False

        keep_btn.connect("clicked", lambda _b: send(session.event("keep")))
        revert_btn.connect("clicked", lambda _b: send(session.event("revert")))
        # Escape = Revert, the safe answer (audit 2026-11-24): works before readiness too.
        escape = Gtk.ShortcutController()
        escape.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("Escape"),
                                             Gtk.CallbackAction.new(lambda *_a: send(session.event("revert")) or True)))
        window.add_controller(escape)
        window.connect("close-request", on_close)
        window.set_content(box)
        keep_btn.set_sensitive(False)  # enabled at readiness, together with the countdown
        revert_btn.set_sensitive(False)
        window.present()
        GLib.idle_add(start_when_ready)

    app.connect("activate", build)
    app.run([])
    return result["exit"]
