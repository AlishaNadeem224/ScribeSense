"""M2.4 — routing decisions in cli.main.

These tests must not import GTK unless PyGObject is available, and must not
open sockets: the project's unit-test sandbox (tests/conftest.py) blocks
foreign Unix-socket connections and blocks process launches. So the tests
only exercise branch selection in cli.main; the runtime forwarding path is
verified manually on a real session.
"""

from __future__ import annotations

import sys

import pytest

from scribesense import cli
from scribesense.contracts.cli import ExitCode


def _require_gtk() -> None:
    """Skip unless the GTK app can load: PyGObject plus the GTK 4 + libadwaita typelibs."""
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
    except ValueError:
        pytest.skip("GTK 4 / libadwaita typelibs not installed")


def test_confirm_is_fresh_process_and_not_forwarded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A15: confirm must never go through the GTK app.
    code = cli.main(["scribesense", "confirm", "42"])
    assert code == int(ExitCode.FAILED)
    err = capsys.readouterr().err
    assert "confirm" in err
    assert "M2.3" in err


@pytest.mark.parametrize("command", ["reset", "recover", "uninstall"])
def test_recovery_commands_do_not_load_ui(
    command: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A2: recovery commands must not import GTK.
    monkeypatch.setitem(sys.modules, "scribesense.ui.app", None)

    code = cli.main(["scribesense", command])
    assert code == int(ExitCode.FAILED)
    err = capsys.readouterr().err
    assert command in err and "M3.1" in err  # says what it is waiting for


def test_doctor_runs_without_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    # A2 + M3.7: doctor runs O3's checks and never imports GTK.
    import scribesense.doctor

    monkeypatch.setitem(sys.modules, "scribesense.ui.app", None)
    monkeypatch.setattr(scribesense.doctor, "run_doctor", lambda: ExitCode.OK, raising=False)
    assert cli.main(["scribesense", "doctor"]) == int(ExitCode.OK)


def test_unknown_command_is_a_usage_error_without_ui(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setitem(sys.modules, "scribesense.ui.app", None)
    assert cli.main(["scribesense", "definitely-not-a-command"]) == int(ExitCode.USAGE)
    assert "unknown command" in capsys.readouterr().err


class _CommandLine:
    def __init__(self, argv: list[str]) -> None:
        self.argv, self.status = argv, None

    def get_arguments(self) -> list[str]:
        return self.argv

    def set_exit_status(self, status: int) -> None:
        self.status = status


def test_the_service_runs_commands_itself_and_quit_quits() -> None:
    # A1: this instance IS the service; later launches arrive in do_command_line.
    _require_gtk()
    from scribesense.ui import app as ui_app  # type: ignore[import-not-found]

    app = ui_app.ScribeSenseApp()
    quit_calls: list[bool] = []
    app.quit = lambda: quit_calls.append(True)  # type: ignore[method-assign]
    assert app.do_command_line(_CommandLine(["scribesense", "quit"])) == int(ExitCode.OK)
    assert quit_calls == [True]
    # apply is answered here (not forwarded to a socket nobody serves); its body is M2.11.
    assert app.do_command_line(_CommandLine(["scribesense", "apply", "default"])) == int(ExitCode.FAILED)
    assert app.do_command_line(_CommandLine(["scribesense", "confirm", "1"])) == int(ExitCode.FAILED)


def test_no_arguments_falls_through_to_app(monkeypatch: pytest.MonkeyPatch) -> None:
    _require_gtk()

    called: dict[str, list[str]] = {}

    from scribesense.ui import app as ui_app  # type: ignore[import-not-found]

    def _fake_run(argv: list[str]) -> int:
        called["argv"] = argv
        return int(ExitCode.OK)

    monkeypatch.setattr(ui_app, "run", _fake_run)

    code = cli.main(["scribesense"])
    assert code == int(ExitCode.OK)
    assert called["argv"] == ["scribesense"]