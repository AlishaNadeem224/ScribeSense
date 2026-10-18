"""M2.11 / K10 — command-line parsing and routing in cli.main (A1, A2, A15).

No GTK and no sockets: the GTK app (`scribesense.ui.app`) and the confirm window
(`scribesense.ui.confirm`) are replaced by fake modules in sys.modules, so these
tests only prove WHICH branch cli.main takes and WHAT it hands over. The
recovery commands themselves are exercised end to end in tests/unit/test_cli_commands.py.
"""

from __future__ import annotations

import sys
import types

import pytest

from scribesense import cli
from scribesense.contracts.cli import COMMANDS, ExitCode
from scribesense.safety import ipc
from scribesense.safety.keybinds import LOGIN_ARGS


class FakeApp:
    """Stands in for scribesense.ui.app: records the argv handed to run()."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, result: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.result = result
        module = types.ModuleType("scribesense.ui.app")
        module.run = self.run  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "scribesense.ui.app", module)

    def run(self, argv: list[str]) -> int:
        self.calls.append(list(argv))
        return self.result


class FakeConfirm:
    """Stands in for scribesense.ui.confirm (M2.3): records run_confirm(tx)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[int] = []
        module = types.ModuleType("scribesense.ui.confirm")
        module.run_confirm = self.run_confirm  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "scribesense.ui.confirm", module)

    def run_confirm(self, tx: int) -> int:
        self.calls.append(tx)
        return int(ExitCode.OK)


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FakeApp:
    return FakeApp(monkeypatch)


@pytest.fixture
def no_socket(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Fails the test if anything talks to the K4 socket (quit / apply / read must not)."""
    sent: list[object] = []
    monkeypatch.setattr(ipc, "request", lambda message, **_kw: sent.append(message) or {"ok": True})
    return sent


# ---------------------------------------------------------------- the GTK app branch (A1)

@pytest.mark.parametrize("argv", [
    ["scribesense"],
    ["scribesense", "apply", "builtin.default"],
    ["scribesense", "preset", "next"],
    ["scribesense", "read", "--selection"],
    ["scribesense", "read", "--clipboard"],
    ["scribesense", "quit"],
])
def test_app_commands_reach_the_gtk_app_with_the_original_argv(
    argv: list[str], app: FakeApp, no_socket: list[object],
) -> None:
    # A1: the first launch becomes the service; Gio forwards later launches to its do_command_line.
    assert cli.main(argv) == int(ExitCode.OK)
    assert app.calls == [argv]
    assert no_socket == []  # quit in particular is NOT sent to the K4 socket (nothing serves it)


def test_the_app_exit_code_is_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeApp(monkeypatch, result=int(ExitCode.SERVICE_UNAVAILABLE))
    assert cli.main(["scribesense", "preset", "next"]) == int(ExitCode.SERVICE_UNAVAILABLE)


def test_main_reads_sys_argv_when_no_argv_is_given(monkeypatch: pytest.MonkeyPatch, app: FakeApp) -> None:
    monkeypatch.setattr(sys, "argv", ["scribesense", "read", "--clipboard"])
    assert cli.main() == int(ExitCode.OK)
    assert app.calls == [["scribesense", "read", "--clipboard"]]


#: K10: read / preset next start the service, "4 only if that fails"; the other app commands exit 1.
APP_START_FAILURE = [
    (["scribesense", "read", "--selection"], ExitCode.SERVICE_UNAVAILABLE),  # keybind Super+Alt+R
    (["scribesense", "read", "--clipboard"], ExitCode.SERVICE_UNAVAILABLE),
    (["scribesense", "preset", "next"], ExitCode.SERVICE_UNAVAILABLE),  # keybind Super+Alt+P
    (["scribesense", "apply", "builtin.default"], ExitCode.FAILED),
    (["scribesense"], ExitCode.FAILED),
    (["scribesense", "quit"], ExitCode.FAILED),
]


@pytest.mark.parametrize("argv,expected", APP_START_FAILURE)
def test_gtk_missing_is_a_clear_failure(
    argv: list[str], expected: ExitCode, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setitem(sys.modules, "scribesense.ui.app", None)  # import fails like a missing PyGObject
    assert cli.main(argv) == int(expected)
    err = capsys.readouterr().err
    assert "GTK4" in err and "scribesense reset" in err


@pytest.mark.parametrize("argv,expected", APP_START_FAILURE)
def test_an_app_startup_crash_is_reported_with_the_recovery_hint(
    argv: list[str], expected: ExitCode, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    module = types.ModuleType("scribesense.ui.app")

    def boom(_argv: list[str]) -> int:
        raise RuntimeError("display gone")

    module.run = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "scribesense.ui.app", module)
    assert cli.main(argv) == int(expected)
    err = capsys.readouterr().err
    assert "scribesense reset" in err and "display gone" not in err  # no free-form exception text


# ---------------------------------------------------------------- confirm (A15)

def test_confirm_runs_its_own_window_and_never_the_app(monkeypatch: pytest.MonkeyPatch, app: FakeApp) -> None:
    confirm = FakeConfirm(monkeypatch)
    assert cli.main(["scribesense", "confirm", "42"]) == int(ExitCode.OK)
    assert confirm.calls == [42] and app.calls == []  # fresh process: no Gio single-instance forwarding


@pytest.mark.parametrize("tx", ["abc", "0", "-3", "1.5", "", "٣"])
def test_confirm_with_a_bad_tx_is_a_usage_error(
    tx: str, monkeypatch: pytest.MonkeyPatch, app: FakeApp,
) -> None:
    confirm = FakeConfirm(monkeypatch)
    assert cli.main(["scribesense", "confirm", tx]) == int(ExitCode.USAGE)
    assert confirm.calls == [] and app.calls == []


def test_confirm_without_gtk_is_a_clear_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setitem(sys.modules, "scribesense.ui.confirm", None)
    assert cli.main(["scribesense", "confirm", "7"]) == int(ExitCode.FAILED)
    assert "GTK4" in capsys.readouterr().err


@pytest.mark.parametrize("error", [ImportError("gi"), ValueError("Namespace Gtk not available")])
def test_gtk_missing_inside_the_window_code_is_a_clear_failure(
    error: Exception, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    # ui.confirm / ui.app load gi lazily, so a missing typelib is raised by the call, not the import.
    def missing(*_a: object) -> int:
        raise error

    for name, attr in (("scribesense.ui.confirm", "run_confirm"), ("scribesense.ui.app", "run")):
        module = types.ModuleType(name)
        setattr(module, attr, missing)
        monkeypatch.setitem(sys.modules, name, module)
    assert cli.main(["scribesense", "confirm", "7"]) == int(ExitCode.FAILED)
    # K10: read cannot start the service -> 4; apply is not one of K10's service-starting commands -> 1.
    assert cli.main(["scribesense", "read", "--selection"]) == int(ExitCode.SERVICE_UNAVAILABLE)
    assert cli.main(["scribesense", "apply", "builtin.default"]) == int(ExitCode.FAILED)
    err = capsys.readouterr().err
    assert err.count("GTK4") == 3


# ---------------------------------------------------------------- recovery routing (A2)

@pytest.mark.parametrize("argv,handler,expected", [
    (["scribesense", "reset"], "_cmd_reset", ()),
    (["scribesense", *LOGIN_ARGS], "_cmd_recover", ()),  # the exact line the login hook runs (A3)
    (["scribesense", "doctor"], "_cmd_doctor", ()),
    (["scribesense", "uninstall"], "_cmd_uninstall", (False, False)),
    (["scribesense", "uninstall", "--delete-data"], "_cmd_uninstall", (True, False)),
    (["scribesense", "uninstall", "--remove-fonts-anyway"], "_cmd_uninstall", (False, True)),
    (["scribesense", "uninstall", "--remove-fonts-anyway", "--delete-data"], "_cmd_uninstall", (True, True)),
])
def test_recovery_commands_route_to_their_handler_not_the_app(
    argv: list[str], handler: str, expected: tuple[bool, ...],
    monkeypatch: pytest.MonkeyPatch, app: FakeApp,
) -> None:
    seen: list[tuple[bool, ...]] = []
    monkeypatch.setattr(cli, handler, lambda *a: seen.append(a) or int(ExitCode.OK))
    assert cli.main(argv) == int(ExitCode.OK)
    assert seen == [expected] and app.calls == []


def test_doctor_returns_run_doctors_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    import scribesense.doctor

    monkeypatch.setattr(scribesense.doctor, "run_doctor", lambda: ExitCode.FAILED)
    assert cli.main(["scribesense", "doctor"]) == int(ExitCode.FAILED)


# ---------------------------------------------------------------- usage (K10: exit 2, no GTK)

@pytest.mark.parametrize("rest", [
    ["definitely-not-a-command"],
    ["apply"],
    ["apply", "a", "b"],
    ["apply", "--force"],
    ["preset"],
    ["preset", "previous"],
    ["read"],
    ["read", "--selection", "--clipboard"],
    ["read", "--file"],
    ["reset", "--extra"],
    ["recover"],
    ["recover", "--now"],
    ["uninstall", "--everything"],
    ["uninstall", "--delete-data", "--delete-data"],
    ["doctor", "x"],
    ["confirm"],
    ["confirm", "1", "2"],
    ["quit", "now"],
    ["--version"],
])
def test_bad_command_lines_are_usage_errors_without_gtk(
    rest: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setitem(sys.modules, "scribesense.ui.app", None)  # any attempt to load the app would fail
    monkeypatch.setitem(sys.modules, "scribesense.ui.confirm", None)
    assert cli.main(["scribesense", *rest]) == int(ExitCode.USAGE)
    err = capsys.readouterr().err
    assert err.startswith("scribesense: ") and "Usage:" in err


def test_unknown_command_names_it(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["scribesense", "frobnicate"]) == int(ExitCode.USAGE)
    assert "unknown command 'frobnicate'" in capsys.readouterr().err


@pytest.mark.parametrize("rest", [["--help"], ["-h"], ["reset", "--help"], ["apply", "-h"]])
def test_help_prints_every_k10_command_and_exits_0(
    rest: list[str], app: FakeApp, capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["scribesense", *rest]) == int(ExitCode.OK)
    out = capsys.readouterr().out
    assert all(command in out for command in COMMANDS)
    assert app.calls == []


def test_every_k10_command_line_is_accepted(monkeypatch: pytest.MonkeyPatch, app: FakeApp) -> None:
    # K10 is the contract: each listed form (placeholders filled in) must parse.
    filled = {"<preset-id>": "builtin.default", "<tx>": "5"}
    for form in COMMANDS:
        words = form.split()[1:]
        if "[--delete-data]" in words:
            words = ["uninstall", "--delete-data", "--remove-fonts-anyway"]
        words = [filled.get(w, w) for w in words]
        assert cli._parse(words)[0] == (words[0] if words else ""), form
