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


def test_confirm_is_fresh_process_and_not_forwarded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A15: confirm must never go through the GTK app.
    code = cli.main(["scribesense", "confirm", "42"])
    assert code == int(ExitCode.FAILED)
    err = capsys.readouterr().err
    assert "confirm" in err
    assert "M2.3" in err


@pytest.mark.parametrize("command", ["reset", "recover", "uninstall", "doctor"])
def test_recovery_commands_do_not_load_ui(
    command: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A2: recovery commands must not import GTK.
    monkeypatch.setitem(sys.modules, "scribesense.ui.app", None)

    code = cli.main(["scribesense", command])
    assert code == int(ExitCode.FAILED)
    assert command in capsys.readouterr().err


def test_unknown_command_falls_through_to_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("gi")

    captured: dict[str, list[str]] = {}

    from scribesense.ui import app as ui_app  # type: ignore[import-not-found]

    def _fake_run(argv: list[str]) -> int:
        captured["argv"] = argv
        return int(ExitCode.USAGE)

    monkeypatch.setattr(ui_app, "run", _fake_run)

    code = cli.main(["scribesense", "definitely-not-a-command"])
    assert code == int(ExitCode.USAGE)
    assert captured["argv"] == ["scribesense", "definitely-not-a-command"]


def test_no_arguments_falls_through_to_app(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("gi")

    called: dict[str, list[str]] = {}

    from scribesense.ui import app as ui_app  # type: ignore[import-not-found]

    def _fake_run(argv: list[str]) -> int:
        called["argv"] = argv
        return int(ExitCode.OK)

    monkeypatch.setattr(ui_app, "run", _fake_run)

    code = cli.main(["scribesense"])
    assert code == int(ExitCode.OK)
    assert called["argv"] == ["scribesense"]