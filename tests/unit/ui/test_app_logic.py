"""M2.3 / M2.4 / M2.7 / M2.8 / M2.11 (app side) — the logic behind the app, window, settings, confirm and tray.

No test opens a window or needs a display. External effects are faked: the service, the store, the
socket (`request`), `run_cmd` and the Hyprland keybind writer. Tests that import GTK modules (app,
window, settings page) skip when PyGObject or the GTK 4 / libadwaita typelibs are missing; the confirm
decision table and the tray check are GTK-free and always run.
"""

from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from scribesense.contracts.cli import ExitCode
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.store import SETTINGS


def _require_gtk() -> None:
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
    except ValueError:
        pytest.skip("GTK 4 / libadwaita typelibs not installed")


@pytest.fixture
def app_mod() -> Any:
    _require_gtk()
    from scribesense.ui import app

    return app


@pytest.fixture
def settings_mod() -> Any:
    _require_gtk()
    from scribesense.ui import settings_page

    return settings_page


# ================================================================ M2.8 tray (GTK-free)

def test_tray_is_unavailable_and_never_loads_appindicator() -> None:
    from scribesense.ui import tray

    assert tray.is_available() is False
    assert "GTK 3" in tray.UNAVAILABLE_REASON
    assert not any("AppIndicator" in name for name in sys.modules)
    icon = tray.TrayIcon("x", on_apply=lambda: None, on_show=lambda: None, on_quit=lambda: None)
    assert icon.available is False
    icon.set_tooltip("t")
    icon.stop()


def test_tray_source_imports_no_gtk() -> None:
    from scribesense.ui import tray

    source = inspect.getsource(tray)
    assert "import gi" not in source and "AppIndicator3 as" not in source


# ================================================================ M2.3 confirm decision table (GTK-free)

@pytest.mark.parametrize("event, sent", [("keep", "keep"), ("revert", "revert"), ("close", "revert"),
                                         ("timeout", "revert"), ("anything-else", "revert")])
def test_confirm_answer_for_each_event(event: str, sent: str) -> None:
    from scribesense.ui.confirm import answer_message

    assert answer_message(event, 42) == {"type": sent, "tx": 42}


def test_confirm_sends_exactly_one_answer() -> None:
    from scribesense.ui.confirm import ConfirmSession

    s = ConfirmSession(7, seconds=3)
    assert s.event("keep") == {"type": "keep", "tx": 7}
    assert s.event("close") is None  # closing after Keep never sends a revert
    assert s.tick() is None


def test_confirm_countdown_reaching_zero_reverts() -> None:
    from scribesense.ui.confirm import ConfirmSession

    s = ConfirmSession(7, seconds=3)
    assert s.tick() is None and s.tick() is None
    assert s.tick() == {"type": "revert", "tx": 7}
    assert s.answered == "revert"
    assert s.event("keep") is None  # a late Keep after the timeout is never sent


def test_confirm_default_countdown_is_the_service_constant() -> None:
    from scribesense.controller.service import CONFIRM_SECONDS
    from scribesense.ui.confirm import ConfirmSession

    assert ConfirmSession(1).seconds_left == CONFIRM_SECONDS == 30


def _request(reply: dict[str, Any] | None = None, error: ErrorCode | None = None) -> Any:
    sent: list[dict[str, Any]] = []

    def request(message: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        assert timeout > 0
        sent.append(message)
        if error is not None:
            raise ScribeSenseError(error)
        return reply or {}

    request.sent = sent  # type: ignore[attr-defined]
    return request


def test_confirm_checks_the_pending_tx() -> None:
    from scribesense.ui.confirm import check_pending

    assert check_pending(5, _request({"v": 1, "ok": True, "tx": 5})) is None
    assert check_pending(5, _request({"v": 1, "ok": True, "tx": 6})) is ErrorCode.TX_NOT_PENDING
    assert check_pending(5, _request({"v": 1, "ok": True, "tx": None})) is ErrorCode.TX_NOT_PENDING
    assert check_pending(5, _request({"v": 1, "ok": True, "tx": True})) is ErrorCode.TX_NOT_PENDING
    assert check_pending(5, _request(error=ErrorCode.SERVICE_UNREACHABLE)) is ErrorCode.SERVICE_UNREACHABLE
    req = _request({"v": 1, "ok": True, "tx": 5})
    check_pending(5, req)
    assert req.sent == [{"type": "pending"}]


def test_confirm_delivery_exit_codes() -> None:
    from scribesense.ui.confirm import deliver

    assert deliver({"type": "keep", "tx": 1}, _request({"v": 1, "ok": True})) == (ExitCode.OK, None)
    assert deliver({"type": "keep", "tx": 1}, _request({"v": 1, "ok": False, "error": "TX_NOT_PENDING"})) \
        == (ExitCode.FAILED, ErrorCode.TX_NOT_PENDING)
    assert deliver({"type": "keep", "tx": 1}, _request({"v": 1, "ok": False, "error": "nonsense"})) \
        == (ExitCode.FAILED, ErrorCode.BAD_MESSAGE)
    assert deliver({"type": "revert", "tx": 1}, _request(error=ErrorCode.SERVICE_UNREACHABLE)) \
        == (ExitCode.FAILED, ErrorCode.SERVICE_UNREACHABLE)


def test_confirm_uses_its_own_non_unique_application() -> None:
    from scribesense.ui import confirm

    source = inspect.getsource(confirm.run_confirm)
    assert 'APP_ID + ".Confirm"' in source and "NON_UNIQUE" in source


# ================================================================ M2.11 command parsing (app side)

@pytest.mark.parametrize("rest, expected", [
    ([], ("open", None)),
    (["quit"], ("quit", None)),
    (["confirm", "4"], ("confirm", None)),
    (["apply", "reading"], ("apply", "reading")),
    (["preset", "next"], ("preset-next", None)),
    (["read", "--selection"], ("read", "selection")),
    (["read", "--clipboard"], ("read", "clipboard")),
])
def test_parse_command(app_mod: Any, rest: list[str], expected: tuple[str, str | None]) -> None:
    assert app_mod.parse_command(rest) == expected


@pytest.mark.parametrize("rest", [["apply"], ["apply", "a", "b"], ["preset"], ["preset", "prev"],
                                  ["read"], ["read", "--both"], ["quit", "now"], ["bogus"]])
def test_parse_command_usage_errors(app_mod: Any, rest: list[str]) -> None:
    with pytest.raises(app_mod.UsageError):
        app_mod.parse_command(rest)


# ================================================================ preset lookup / next

def _user(pid: str, name: str = "Mine") -> Any:
    from scribesense.config.presets import BUILTINS
    from scribesense.contracts.presets import Preset

    return Preset(id=pid, name=name, values=BUILTINS[0].values, kind="user")


def test_known_presets_builtins_then_users_without_custom(app_mod: Any) -> None:
    from scribesense.config.presets import BUILTINS, custom_preset

    stored = [*BUILTINS, custom_preset(BUILTINS[0].values), _user("u1"), _user("u1")]
    ids = [p.id for p in app_mod.known_presets(stored)]
    assert ids == ["default", "reading", "high-separation", "u1"]


def test_find_preset_for_apply_id(app_mod: Any) -> None:
    presets = app_mod.known_presets([_user("u1")])
    assert app_mod.find_preset("reading", presets).id == "reading"
    assert app_mod.find_preset("u1", presets).id == "u1"
    assert app_mod.find_preset("nope", presets) is None


def test_next_preset_cycles_and_wraps(app_mod: Any) -> None:
    presets = app_mod.known_presets([_user("u1")])
    nxt = app_mod.next_preset
    assert nxt(presets, "default").id == "reading"
    assert nxt(presets, "high-separation").id == "u1"
    assert nxt(presets, "u1").id == "default"  # wraps
    assert nxt(presets, None).id == "default"  # Original active -> first
    assert nxt(presets, "custom").id == "default"  # unknown -> first
    assert nxt([], "default") is None


# ================================================================ wording

def test_a_refused_apply_after_a_crash_tells_the_user_how_to_recover(app_mod: Any) -> None:
    text = app_mod.reason_text("RECOVERY_OBLIGATIONS_REMAIN")  # A3: journal refuses a new apply (M4.10)
    assert "scribesense reset" in text and "Backspace" in text and "(RECOVERY_OBLIGATIONS_REMAIN)" in text


def test_reason_text_known_unknown_and_missing(app_mod: Any) -> None:
    assert "disk space" in app_mod.reason_text("NO_DISK_SPACE") and "(NO_DISK_SPACE)" in app_mod.reason_text(
        "NO_DISK_SPACE")
    unknown = app_mod.reason_text("SOMETHING_NEW")
    assert "(SOMETHING_NEW)" in unknown
    assert app_mod.reason_text(None)


def test_report_text_per_status(app_mod: Any) -> None:
    assert app_mod.report_text("stopped", "NO_DISK_SPACE").startswith("Nothing was changed.")
    assert app_mod.report_text("reverted", "ADAPTER_UNAVAILABLE").startswith("The change was undone.")
    assert "scribesense reset" in app_mod.report_text("revert_failed", "REVERT_ITEM_FAILED")


def test_outcome_text_per_state(app_mod: Any) -> None:
    from scribesense.contracts.states import TxState

    assert app_mod.outcome_text(TxState.COMPLETED) == "New settings kept."
    assert "previous settings" in app_mod.outcome_text(TxState.REVERTED)
    assert "next login" in app_mod.outcome_text(TxState.REVERT_FAILED)
    assert app_mod.outcome_text(None)


def test_capture_messages_are_distinct_and_show_the_code(app_mod: Any) -> None:
    codes = [ErrorCode.NOTHING_SELECTED, ErrorCode.SELECTION_UNAVAILABLE, ErrorCode.CLIPBOARD_EMPTY,
             ErrorCode.TOO_LARGE]
    texts = [app_mod.capture_message(c) for c in codes]
    assert len(set(texts)) == len(texts)  # M2.10: empty and oversize give distinct messages
    for code, text in zip(codes, texts):
        assert f"({code.value})" in text
    assert "200 000" in app_mod.capture_message(ErrorCode.TOO_LARGE)


def test_startup_text_names_the_code(app_mod: Any) -> None:
    assert "already running" in app_mod.startup_text(ErrorCode.APPLY_IN_PROGRESS)
    assert "(STORE_CORRUPT)" in app_mod.startup_text(ErrorCode.STORE_CORRUPT)
    assert app_mod.startup_text(None)


def test_reset_text_counts(app_mod: Any) -> None:
    ok = SimpleNamespace(restored=["a", "b"], skipped=[], failed=[])
    assert "(2)" in app_mod.reset_text(ok)
    assert "failed 1" in app_mod.reset_text(SimpleNamespace(restored=[], skipped=[], failed=["x"]))


# ================================================================ M2.9 reader font choice

def _fontset(key: str) -> Any:
    from scribesense.contracts.fonts import FontSet

    return FontSet(key=key, reading_family=f"AccessSans-{key}", ui_family=f"AccessSansUI-{key}", files=(),
                   styles=("Regular",))


def _active(key: str) -> Any:
    from scribesense.config.presets import BUILTINS, resolve
    from scribesense.contracts.journal import ActiveConfig

    return ActiveConfig(resolve(BUILTINS[1].values), key, "reading", 3)


def test_reader_font_uses_the_active_key_not_list_order(app_mod: Any) -> None:
    active = _active("bbb")
    family, config = app_mod.reader_font(active, lambda: [_fontset("aaa"), _fontset("bbb"), _fontset("zzz")],
                                         lambda: None)
    assert family == "AccessSans-bbb" and config == active.config


def test_reader_font_never_builds_or_installs(app_mod: Any) -> None:
    """Nothing active -> the Default configuration in the system font; no generator.build (K7: Apply only)."""
    from scribesense.config.presets import BUILTINS, resolve

    assert "build" not in inspect.signature(app_mod.reader_font).parameters
    default = resolve(BUILTINS[0].values)

    def installed() -> list[Any]:
        raise AssertionError("nothing active: no font set is looked up")

    assert app_mod.reader_font(None, installed, lambda: default) == (None, default)


def test_reader_font_active_set_not_installed_keeps_the_config(app_mod: Any) -> None:
    active = _active("gone")
    assert app_mod.reader_font(active, lambda: [_fontset("zzz")], lambda: None) == (None, active.config)


def test_app_reader_font_does_not_pass_generator_build(app_mod: Any) -> None:
    source = inspect.getsource(app_mod.ScribeSenseApp._reader_font)
    assert "generator.build" not in source


def test_default_reader_config_is_the_default_preset(app_mod: Any) -> None:
    from scribesense.config.presets import BUILTINS, resolve

    assert app_mod._default_config() == resolve(BUILTINS[0].values)


# ================================================================ apply flow worker (no GTK calls)

@dataclass
class _FakeService:
    report: Any
    final_state: Any = None
    calls: list[tuple[str, Any]] = field(default_factory=list)

    def apply(self, preset: Any) -> Any:
        self.calls.append(("apply", preset.id))
        return self.report

    def confirm_watchdog(self, tx: int) -> bool:
        self.calls.append(("watchdog", tx))
        return False

    @property
    def journal(self) -> Any:
        return SimpleNamespace(state=lambda tx: self.final_state)


def _report(status: str, tx: int | None = 9, reason: str | None = None) -> Any:
    return SimpleNamespace(status=status, tx=tx, coverage=(), reason_code=reason)


def test_apply_worker_starts_fresh_confirm_process_then_watchdog(app_mod: Any, monkeypatch: Any) -> None:
    from scribesense.config.presets import BUILTINS
    from scribesense.contracts import paths
    from scribesense.contracts.states import TxState
    from scribesense.controller.service import CONFIRM_GRACE_SECONDS, CONFIRM_SECONDS

    launched: list[tuple[list[str], float]] = []

    def fake_run_cmd(argv: list[str], timeout: float) -> Any:
        launched.append((argv, timeout))
        raise ScribeSenseError(ErrorCode.ADAPTER_COMMAND_FAILED)  # exit status is ignored

    monkeypatch.setattr(paths, "run_cmd", fake_run_cmd)
    service = _FakeService(_report("awaiting_confirmation"), TxState.COMPLETED)
    report, state = app_mod.ScribeSenseApp._apply_worker(service, BUILTINS[1])
    assert launched == [([sys.executable, "-m", "scribesense.cli", "confirm", "9"],
                         CONFIRM_SECONDS + CONFIRM_GRACE_SECONDS)]
    assert service.calls == [("apply", "reading"), ("watchdog", 9)]
    assert state == TxState.COMPLETED and report.tx == 9


def test_apply_worker_clears_the_service_call_flag_when_apply_returns(app_mod: Any, monkeypatch: Any) -> None:
    import threading

    from scribesense.config.presets import BUILTINS
    from scribesense.contracts import paths

    in_call = threading.Event()
    in_call.set()
    seen: list[bool] = []

    def fake_run_cmd(argv: list[str], timeout: float) -> Any:
        seen.append(in_call.is_set())  # during the confirmation countdown Quit may proceed
        return None

    monkeypatch.setattr(paths, "run_cmd", fake_run_cmd)
    app_mod.ScribeSenseApp._apply_worker(_FakeService(_report("awaiting_confirmation")), BUILTINS[1], in_call)
    assert seen == [False]

    class Boom(_FakeService):
        def apply(self, preset: Any) -> Any:
            raise ScribeSenseError(ErrorCode.NO_DISK_SPACE)

    in_call.set()
    with pytest.raises(ScribeSenseError):
        app_mod.ScribeSenseApp._apply_worker(Boom(None), BUILTINS[1], in_call)
    assert not in_call.is_set()


def test_apply_worker_stopped_report_starts_nothing(app_mod: Any, monkeypatch: Any) -> None:
    from scribesense.config.presets import BUILTINS
    from scribesense.contracts import paths

    def fake_run_cmd(argv: list[str], timeout: float) -> Any:
        raise AssertionError("no confirm process for a stopped apply")

    monkeypatch.setattr(paths, "run_cmd", fake_run_cmd)
    service = _FakeService(_report("stopped", tx=None, reason="NO_DISK_SPACE"))
    report, state = app_mod.ScribeSenseApp._apply_worker(service, BUILTINS[0])
    assert state is None and report.reason_code == "NO_DISK_SPACE"
    assert service.calls == [("apply", "default")]


# ================================================================ M2.11 exit codes (0 only on success)

def test_apply_exit_code_mapping(app_mod: Any) -> None:
    from scribesense.contracts.states import TxState

    code = app_mod.apply_exit_code
    assert code((_report("awaiting_confirmation"), TxState.COMPLETED)) == ExitCode.OK
    assert code((_report("awaiting_confirmation"), TxState.REVERTED)) == ExitCode.FAILED
    assert code((_report("awaiting_confirmation"), TxState.REVERT_FAILED)) == ExitCode.FAILED
    assert code((_report("awaiting_confirmation"), None)) == ExitCode.FAILED
    assert code((_report("stopped", None, "NO_DISK_SPACE"), None)) == ExitCode.FAILED
    assert code((_report("reverted", 9, "CHANGED_DURING_APPLY"), None)) == ExitCode.FAILED
    assert code((_report("revert_failed", 9, "NOT_WRITABLE"), None)) == ExitCode.FAILED
    assert code((_report("stopped", None, "LOCK_TIMEOUT"), None)) == ExitCode.BUSY
    assert code((_report("stopped", None, "APPLY_IN_PROGRESS"), None)) == ExitCode.BUSY
    assert code(ScribeSenseError(ErrorCode.LOCK_TIMEOUT)) == ExitCode.BUSY
    assert code(ScribeSenseError(ErrorCode.STORE_IO_ERROR)) == ExitCode.FAILED
    assert code(RuntimeError("x")) == ExitCode.FAILED


def test_original_and_capture_exit_codes(app_mod: Any) -> None:
    assert app_mod.original_exit_code(SimpleNamespace(restored=["a"], skipped=["b"], failed=[])) == ExitCode.OK
    assert app_mod.original_exit_code(SimpleNamespace(restored=[], skipped=[], failed=["x"])) == ExitCode.FAILED
    assert app_mod.original_exit_code(ScribeSenseError(ErrorCode.APPLY_IN_PROGRESS)) == ExitCode.BUSY
    assert app_mod.capture_exit_code((None, "text", (None, None))) == ExitCode.OK
    assert app_mod.capture_exit_code((ErrorCode.NOTHING_SELECTED, None, None)) == ExitCode.FAILED
    assert app_mod.capture_exit_code((ErrorCode.TOO_LARGE, None, None)) == ExitCode.FAILED
    assert app_mod.capture_exit_code(ScribeSenseError(ErrorCode.SELECTION_UNAVAILABLE)) == ExitCode.FAILED


class _FakeCommandLine:
    def __init__(self, *argv: str) -> None:
        self.argv = ["scribesense", *argv]
        self.status: int | None = None
        self.err: list[str] = []
        self.done_calls = 0

    def get_arguments(self) -> list[str]:
        return list(self.argv)

    def set_exit_status(self, status: int) -> None:
        self.status = status

    def printerr_literal(self, text: str) -> None:
        self.err.append(text)

    def done(self) -> None:
        self.done_calls += 1


class _FakeApp:
    """Stands in for ScribeSenseApp's state so do_command_line / quit / done-callbacks run without GTK."""

    def __init__(self, app_mod: Any, *, started: bool = True) -> None:
        import threading

        self.app_mod = app_mod
        self.started = started
        self.calls: list[Any] = []
        self.finishers: list[Any] = []
        self._open_replies: set[Any] = set()
        self._in_service_call = threading.Event()
        self._busy = False
        self._finish = app_mod.ScribeSenseApp._finish

    def request_quit(self) -> Any:
        return self.app_mod.ScribeSenseApp.request_quit(self)

    def quit(self) -> None:
        self.calls.append("quit")

    def _ensure_service(self) -> None:
        self.calls.append("service")

    def activate(self) -> None:
        self.calls.append("activate")

    def read(self, source: str, on_finished: Any = None) -> None:
        self.calls.append(("read", source))
        self.finishers.append(on_finished)

    def apply_by_id(self, preset_id: str, on_finished: Any = None) -> Any:
        self.calls.append(("apply", preset_id))
        if not self.started:
            return ExitCode.BUSY, "scribesense: another change is in progress"
        self.finishers.append(on_finished)
        return None

    def preset_next(self, on_finished: Any = None) -> Any:
        return self.apply_by_id("<next>", on_finished)


def _command(app_mod: Any, fake: _FakeApp, *argv: str) -> tuple[int, _FakeCommandLine]:
    command_line = _FakeCommandLine(*argv)
    return app_mod.ScribeSenseApp.do_command_line(fake, command_line), command_line


def test_quit_never_opens_the_service(app_mod: Any) -> None:
    fake = _FakeApp(app_mod)
    code, command_line = _command(app_mod, fake, "quit")
    assert code == ExitCode.OK == command_line.status and fake.calls == ["quit"]


def test_quit_is_refused_while_a_service_call_runs(app_mod: Any) -> None:
    fake = _FakeApp(app_mod)
    fake._in_service_call.set()
    code, command_line = _command(app_mod, fake, "quit")
    assert code == ExitCode.BUSY == command_line.status and "quit" not in fake.calls
    assert command_line.err and "did not quit" in command_line.err[0]


@pytest.mark.parametrize("argv, final", [
    (("apply", "reading"), ExitCode.OK), (("apply", "reading"), ExitCode.FAILED),
    (("preset", "next"), ExitCode.BUSY), (("read", "--selection"), ExitCode.FAILED),
    (("read", "--clipboard"), ExitCode.OK)])
def test_work_commands_answer_with_the_real_outcome(app_mod: Any, argv: tuple[str, ...], final: ExitCode) -> None:
    fake = _FakeApp(app_mod)
    code, command_line = _command(app_mod, fake, *argv)
    assert code == ExitCode.FAILED  # provisional: never 0 before the work has run
    assert fake.calls[0] == "service" and len(fake._open_replies) == 1  # reference held until the outcome
    finish = fake.finishers[-1]
    finish(final, None if final == ExitCode.OK else "scribesense: why")
    assert command_line.status == int(final) and command_line.done_calls == 1
    assert fake._open_replies == set()
    finish(ExitCode.OK)  # a second answer is ignored
    assert command_line.status == int(final) and command_line.done_calls == 1


def test_work_command_that_cannot_start_answers_at_once(app_mod: Any) -> None:
    fake = _FakeApp(app_mod, started=False)
    code, command_line = _command(app_mod, fake, "apply", "reading")
    assert code == ExitCode.BUSY == command_line.status and fake._open_replies == set()
    assert command_line.done_calls == 0  # synchronous: Gio replies with the returned status


def test_usage_and_confirm_do_not_open_the_service(app_mod: Any) -> None:
    fake = _FakeApp(app_mod)
    assert _command(app_mod, fake, "apply")[0] == ExitCode.USAGE
    assert _command(app_mod, fake, "confirm", "3")[0] == ExitCode.FAILED
    assert fake.calls == []


def test_apply_done_reports_the_exit_code(app_mod: Any) -> None:
    from scribesense.contracts.states import TxState

    fake = _FakeApp(app_mod)
    fake._busy = True
    fake._in_service_call.set()
    fake._show_apply_outcome = lambda outcome: "The change was not confirmed."
    got: list[Any] = []
    app_mod.ScribeSenseApp._apply_done(fake, (_report("awaiting_confirmation"), TxState.REVERTED),
                                       lambda code, msg: got.append((code, msg)))
    assert got == [(ExitCode.FAILED, "scribesense: The change was not confirmed.")]
    assert fake._busy is False and not fake._in_service_call.is_set()
    app_mod.ScribeSenseApp._apply_done(fake, (_report("awaiting_confirmation"), TxState.COMPLETED),
                                       lambda code, msg: got.append((code, msg)))
    assert got[-1] == (ExitCode.OK, None)


def test_capture_done_reports_the_exit_code(app_mod: Any) -> None:
    fake = _FakeApp(app_mod)
    fake._show_capture_outcome = lambda outcome: "Nothing is selected. (NOTHING_SELECTED)"
    got: list[Any] = []
    app_mod.ScribeSenseApp._capture_done(fake, (ErrorCode.NOTHING_SELECTED, None, None),
                                         lambda code, msg: got.append((code, msg)))
    assert got == [(ExitCode.FAILED, "scribesense: Nothing is selected. (NOTHING_SELECTED)")]


class _ShutdownService:
    def __init__(self, pending: Any, revert_error: ErrorCode | None = None) -> None:
        self.pending_tx = pending
        self.revert_error = revert_error
        self.calls: list[Any] = []

    def revert(self, tx: Any) -> None:
        self.calls.append(("revert", tx))
        if self.revert_error is not None:
            raise ScribeSenseError(self.revert_error)

    def close(self) -> None:
        self.calls.append("close")


@pytest.mark.parametrize("pending, error, expected", [
    (7, None, [("revert", 7), "close"]),
    (7, ErrorCode.TX_NOT_PENDING, [("revert", 7), "close"]),
    (None, None, ["close"])])
def test_shutdown_reverts_an_unconfirmed_change_then_closes(app_mod: Any, monkeypatch: Any, pending: Any,
                                                            error: Any, expected: list[Any]) -> None:
    chained: list[Any] = []
    monkeypatch.setattr(app_mod, "Adw", SimpleNamespace(Application=SimpleNamespace(
        do_shutdown=lambda self: chained.append(self))))
    service = _ShutdownService(pending, error)
    fake = SimpleNamespace(service=service)
    app_mod.ScribeSenseApp.do_shutdown(fake)
    assert service.calls == expected and fake.service is None and chained == [fake]


def test_startup_does_not_open_the_service(app_mod: Any) -> None:
    assert "Service.open" not in inspect.getsource(app_mod.ScribeSenseApp.do_startup)


def test_ensure_service_opens_once_and_remembers_a_failure(app_mod: Any, monkeypatch: Any) -> None:
    opened: list[int] = []

    def fail(**_k: Any) -> Any:
        opened.append(1)
        raise ScribeSenseError(ErrorCode.STORE_CORRUPT)

    monkeypatch.setattr(app_mod.Service, "open", staticmethod(fail))
    fake = SimpleNamespace(service=None, _service_tried=False, _startup_problem=None,
                           _reader_text_from_service=print)
    assert app_mod.ScribeSenseApp._ensure_service(fake) is None
    assert app_mod.ScribeSenseApp._ensure_service(fake) is None
    assert opened == [1] and "STORE_CORRUPT" in fake._startup_problem


# ================================================================ M2.4 window

def test_window_accepts_the_apps_construction_call() -> None:
    _require_gtk()
    from scribesense.ui.window import ScribeSenseWindow

    sig = inspect.signature(ScribeSenseWindow.__init__)
    sig.bind(object(), application=object())  # ScribeSenseWindow(application=app): no required positional
    sig.bind(object(), application=object(), app_id="x", service=None, on_apply=print, on_original=print,
             on_read_clipboard=print)


def test_window_hides_on_close_independent_of_tray() -> None:
    _require_gtk()
    from scribesense.ui import window

    source = inspect.getsource(window.ScribeSenseWindow.__init__)
    assert "self.set_hide_on_close(True)" in source
    assert "tray" not in source.lower().replace("the tray", "")  # no tray dependency in construction


def test_recovery_keybind_is_the_reset_bind(settings_mod: Any) -> None:
    from scribesense.ui.window import recovery_text, reset_keybind_text

    assert reset_keybind_text(None) == "CTRL ALT SHIFT SUPER, BackSpace" == SETTINGS["keybind_reset"][1]
    stored = SimpleNamespace(store=SimpleNamespace(settings=SimpleNamespace(
        get=lambda k: {"keybind_reset": "SUPER SHIFT, Escape", "keybind_reader": "SUPER ALT, R"}[k])))
    assert reset_keybind_text(stored) == "SUPER SHIFT, Escape"

    def broken(_k: str) -> object:
        raise ScribeSenseError(ErrorCode.STORE_CORRUPT)

    assert reset_keybind_text(SimpleNamespace(store=SimpleNamespace(settings=SimpleNamespace(get=broken)))) \
        == "CTRL ALT SHIFT SUPER, BackSpace"
    assert "CTRL ALT SHIFT SUPER, BackSpace" in recovery_text(None) and "scribesense reset" in recovery_text(None)


# ================================================================ M2.7 settings

def test_opt_in_is_off_by_default() -> None:
    assert SETTINGS["recommend_opt_in"] == (bool, False)


def test_validate_keybinds_normalizes_valid_input(settings_mod: Any) -> None:
    normalized, errors = settings_mod.validate_keybinds({
        "keybind_reader": "super+alt, R", "keybind_preset_next": "ALT SUPER, P",
        "keybind_reset": "CTRL ALT SHIFT SUPER, BackSpace"})
    assert errors == {}
    assert normalized == {"keybind_reader": "SUPER ALT, R", "keybind_preset_next": "SUPER ALT, P",
                          "keybind_reset": "SUPER CTRL ALT SHIFT, BackSpace"}


@pytest.mark.parametrize("reset_text", ["", "   ", "CTRL ALT SHIFT SUPER BackSpace", "SUPER, a b",
                                        "SUPER, R; exec rm", "NOPE, R"])
def test_validate_keybinds_refuses_invalid_or_empty(settings_mod: Any, reset_text: str) -> None:
    normalized, errors = settings_mod.validate_keybinds({
        "keybind_reader": "SUPER ALT, R", "keybind_preset_next": "SUPER ALT, P", "keybind_reset": reset_text})
    assert normalized == {} and set(errors) == {"keybind_reset"}


def test_validate_keybinds_refuses_two_actions_on_one_combo(settings_mod: Any) -> None:
    normalized, errors = settings_mod.validate_keybinds({
        "keybind_reader": "SUPER ALT, R", "keybind_preset_next": "alt super, r",
        "keybind_reset": "CTRL ALT SHIFT SUPER, BackSpace"})
    assert normalized == {} and set(errors) == {"keybind_preset_next"}
    assert "Open selection in reader" in errors["keybind_preset_next"]


def _saved(key: str) -> object:
    return SETTINGS[key][1]


def test_install_keybinds_refuses_and_shows_the_conflicting_bind(settings_mod: Any) -> None:
    from scribesense.safety import keybinds as kb

    def find(binds: Any) -> list[kb.Conflict]:
        return [kb.Conflict(binds[0], kb.LiveBind(72, "R", "exec", "", "Launch terminal"))]

    def install(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("must not install on conflict")

    text = settings_mod.install_stored_keybinds(_saved, install=install, find_conflicts=find,
                                                command="/usr/bin/scribesense")
    assert text.startswith("Not applied.") and "SUPER ALT, R" in text and "Launch terminal" in text


@pytest.mark.parametrize("reload, expected", [("reloaded", "applied to Hyprland"), ("skipped", "next login")])
def test_install_keybinds_success(settings_mod: Any, reload: str, expected: str) -> None:
    got: list[Any] = []

    def install(binds: Any, command: str) -> Any:
        got.append(([b.action for b in binds], command))
        return SimpleNamespace(reload=reload)

    text = settings_mod.install_stored_keybinds(_saved, install=install, find_conflicts=lambda b: [],
                                                command="/usr/bin/scribesense")
    assert expected in text
    assert got == [(["reader", "preset_next", "reset"], "/usr/bin/scribesense")]


@pytest.mark.parametrize("code", [ErrorCode.KEYBIND_CONFLICT, ErrorCode.CHECK_UNAVAILABLE,
                                  ErrorCode.DRIFT_SKIPPED, ErrorCode.NOT_WRITABLE])
def test_install_keybinds_reports_errors(settings_mod: Any, code: ErrorCode) -> None:
    def install(*_a: Any, **_k: Any) -> Any:
        raise ScribeSenseError(code)

    text = settings_mod.install_stored_keybinds(_saved, install=install, find_conflicts=lambda b: [],
                                                command="/usr/bin/scribesense")
    assert "not applied" in text.lower()
    if code is not ErrorCode.KEYBIND_CONFLICT:
        assert code.value in text


def test_install_keybinds_without_command_or_with_bad_saved_value(settings_mod: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(settings_mod.shutil, "which", lambda _n: None)
    assert "not found" in settings_mod.install_stored_keybinds(_saved)
    text = settings_mod.install_stored_keybinds(lambda k: "garbage", command="/usr/bin/scribesense")
    assert "not valid" in text


@dataclass
class _FakeStore:
    calls: list[Any] = field(default_factory=list)

    def delete_preferences(self) -> None:
        self.calls.append("prefs")

    def delete_recovery_data(self, *, precondition: Any) -> None:
        self.calls.append(("recovery", precondition))


def test_delete_all_data_uses_the_store_api(settings_mod: Any) -> None:
    from scribesense.contracts.journal import NoRecoveryObligations

    store, token = _FakeStore(), NoRecoveryObligations()
    assert settings_mod.delete_all_data(store, SimpleNamespace(no_obligations=lambda: token)) is True
    assert store.calls == ["prefs", ("recovery", token)]
    store = _FakeStore()
    assert settings_mod.delete_all_data(store, SimpleNamespace(no_obligations=lambda: None)) is False
    assert store.calls == ["prefs"]  # recovery records kept while something is left to restore


def test_delete_dialog_texts_match_what_happens(settings_mod: Any) -> None:
    assert "reset" in settings_mod.DELETE_FIRST_BODY.lower()
    assert "scribesense reset" in settings_mod.DELETE_SKIP_BODY  # kept records -> reset still possible
    assert "once nothing is left" not in settings_mod.DELETE_SKIP_BODY  # no promise of a later deletion
    partial = settings_mod.delete_partial_body(["gtk:font-name"])
    assert "gtk:font-name" in partial and "kept" in partial and "scribesense reset" in partial
    assert "not fully back" in partial
    many = settings_mod.delete_partial_body([f"loc{i}" for i in range(8)])
    assert "loc4" in many and "loc5" not in many and "3 more" in many
    assert "scribesense reset" in settings_mod.delete_result_text(False)
    assert settings_mod.delete_result_text(True) == "All ScribeSense data was deleted."


class _FakePage:
    """Stands in for SettingsPage's widgets so the delete-flow methods run without a display."""

    def __init__(self) -> None:
        self.dialogs: list[str] = []
        self.notes: list[str] = []
        self.deleted = 0
        self.reset_started = 0
        self._busy = False
        self.delete_btn = SimpleNamespace(set_sensitive=lambda v: None)
        self.kb_rows: dict[str, Any] = {}
        self.reset_label = SimpleNamespace(set_label=lambda t: None)
        self.opt_in_row = SimpleNamespace(handler_block_by_func=lambda f: None, set_active=lambda v: None,
                                          handler_unblock_by_func=lambda f: None)
        self._on_opt_in_toggled = None
        self.on_final_response = lambda response: None  # the dialogs' callback (not invoked by the fake)
        self._has_obligations: Any = None
        self.bodies: list[str] = []

    def _dialog(self, heading: str, body: str, _responses: Any, _cb: Any) -> None:
        self.dialogs.append(heading)
        self.bodies.append(body)

    def _obligations_left(self) -> bool:
        from scribesense.ui.settings_page import SettingsPage

        return SettingsPage._obligations_left(self)  # type: ignore[arg-type]

    def _notify(self, text: str) -> None:
        self.notes.append(text)

    def _delete_all(self) -> bool:
        self.deleted += 1
        return True

    def _start_reset(self) -> None:
        self.reset_started += 1

    def _read(self, key: str) -> object:
        return SETTINGS[key][1]


def test_delete_flow_offers_reset_first_and_needs_confirmation(settings_mod: Any) -> None:
    page_cls = settings_mod.SettingsPage
    page = _FakePage()
    page_cls.on_first_response(page, "cancel")
    assert page.dialogs == [] and page.deleted == 0 and page.reset_started == 0
    page_cls.on_first_response(page, "reset")
    assert page.reset_started == 1 and page.deleted == 0
    page_cls.on_first_response(page, "skip")
    assert page.dialogs == [settings_mod.DELETE_SKIP_HEADING] and page.deleted == 0
    page_cls.on_final_response(page, "cancel")
    assert page.deleted == 0
    page_cls.on_final_response(page, "delete")
    assert page.deleted == 1 and page.notes[-1] == "All ScribeSense data was deleted."


def test_delete_flow_stops_when_the_reset_fails(settings_mod: Any) -> None:
    page_cls = settings_mod.SettingsPage
    page = _FakePage()
    page_cls.on_reset_done(page, ScribeSenseError(ErrorCode.LOCK_TIMEOUT))
    assert page.dialogs == [] and "LOCK_TIMEOUT" in page.notes[-1]
    page_cls.on_reset_done(page, SimpleNamespace(restored=[], skipped=[], failed=["gtk:font"]))
    assert page.dialogs == [] and page.deleted == 0
    page_cls.on_reset_done(page, SimpleNamespace(restored=["a"], skipped=[], failed=[]))
    assert page.dialogs == [settings_mod.DELETE_FINAL_HEADING] and page.deleted == 0


def test_delete_flow_after_a_partial_reset_says_the_records_are_kept(settings_mod: Any) -> None:
    page_cls = settings_mod.SettingsPage
    page = _FakePage()
    page_cls.on_reset_done(page, SimpleNamespace(restored=["a"], skipped=["x"], failed=[]))
    assert page.dialogs == [settings_mod.DELETE_PARTIAL_HEADING] and page.deleted == 0
    assert "x" in page.bodies[-1] and "kept" in page.bodies[-1]
    assert settings_mod.DELETE_FINAL_BODY not in page.bodies


@pytest.mark.parametrize("left, heading", [(True, "DELETE_PARTIAL_HEADING"), (False, "DELETE_FINAL_HEADING")])
def test_delete_flow_checks_the_journal_after_a_clean_reset(settings_mod: Any, left: bool, heading: str) -> None:
    page = _FakePage()
    page._has_obligations = lambda: left
    settings_mod.SettingsPage.on_reset_done(page, SimpleNamespace(restored=["a"], skipped=[], failed=[]))
    assert page.dialogs == [getattr(settings_mod, heading)]


def test_delete_flow_unreadable_journal_counts_as_records_kept(settings_mod: Any) -> None:
    def broken() -> bool:
        raise ScribeSenseError(ErrorCode.STORE_BUSY)

    page = _FakePage()
    page._has_obligations = broken
    settings_mod.SettingsPage.on_reset_done(page, SimpleNamespace(restored=["a"], skipped=[], failed=[]))
    assert page.dialogs == [settings_mod.DELETE_PARTIAL_HEADING]


def test_error_text_never_contains_exception_text(settings_mod: Any) -> None:
    assert settings_mod.error_text(ScribeSenseError(ErrorCode.STORE_BUSY)) == "Something went wrong (STORE_BUSY)."
    assert "secret" not in settings_mod.error_text(RuntimeError("/home/user/secret.txt"))
