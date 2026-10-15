"""M3.3 — the bubblewrap command line is built correctly (pure; no process is started)."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness import sandbox
from scribesense.contracts.errors import ErrorCode, ScribeSenseError

REPO = Path("/srv/repo")


def _argv() -> list[str]:
    return sandbox.build_argv(["pytest", "-m", "integration"], repo=REPO, site_packages=(Path("/srv/venv/sp"),))


def _binds(argv: list[str], flag: str) -> list[tuple[str, str]]:
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag]


def test_isolation_flags_present() -> None:
    argv = _argv()
    for flag in ("--unshare-all", "--die-with-parent", "--new-session", "--clearenv"):
        assert flag in argv
    assert "--share-net" not in argv


def test_real_home_and_session_sockets_are_not_mounted() -> None:
    argv = _argv()
    mounted = [s for s, _ in _binds(argv, "--ro-bind") + _binds(argv, "--bind")]
    assert not any(m == "/home" or m.startswith("/home/") or m.startswith("/run") or m.startswith("/var")
                   for m in mounted)
    assert ("--tmpfs" in argv) and argv[argv.index("--tmpfs", argv.index("--tmpfs") + 1) + 1] == "/run"


def test_nothing_is_writable_from_the_host() -> None:
    assert "--bind" not in _argv() and "--bind-try" not in _argv()


def test_environment_is_private_and_marked() -> None:
    argv = _argv()
    env = {argv[i + 1]: argv[i + 2] for i, a in enumerate(argv) if a == "--setenv"}
    assert env["SCRIBESENSE_SANDBOX"] == "bwrap" and env["GSETTINGS_BACKEND"] == "keyfile"
    assert env["HOME"].startswith("/tmp/") and env["XDG_RUNTIME_DIR"] == "/tmp/run"
    assert not {"DBUS_SESSION_BUS_ADDRESS", "WAYLAND_DISPLAY", "DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE"} & env.keys()
    assert len(env["XDG_RUNTIME_DIR"] + "/scribesense/service.sock") < 108


def test_command_comes_last_after_separator() -> None:
    argv = _argv()
    assert argv[-3:] == ["pytest", "-m", "integration"] and argv[-4] == "--"


def test_empty_command_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        sandbox.build_argv([], repo=REPO)


def test_missing_bwrap_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "bwrap_path", lambda: None)
    with pytest.raises(ScribeSenseError) as exc:
        sandbox.check_available(repo=REPO)
    assert exc.value.code is ErrorCode.SANDBOX_UNAVAILABLE


def test_exit_code_is_read_from_the_last_marker_line() -> None:
    from harness import run_integration

    assert run_integration.parse_exit("....\n3 passed\n\nSCRIBESENSE_EXIT=0\n") == 0
    assert run_integration.parse_exit("x\nSCRIBESENSE_EXIT=1\n") == 1
    assert run_integration.parse_exit("crashed before the marker") is None
    assert run_integration.parse_exit("SCRIBESENSE_EXIT=oops") is None


def test_runner_refuses_to_run_without_bwrap(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from harness import run_integration

    monkeypatch.setattr(sandbox, "bwrap_path", lambda: None)
    assert run_integration.main([]) == run_integration.EXIT_SANDBOX_UNAVAILABLE
    assert "SANDBOX_UNAVAILABLE" in capsys.readouterr().err


def test_runner_treats_a_dead_sandbox_as_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from harness import run_integration

    monkeypatch.setattr(sandbox, "check_available", lambda *, repo: None)
    monkeypatch.setattr(sandbox, "bwrap_path", lambda: "/usr/bin/bwrap")

    def failing(argv: list[str], timeout: float):  # type: ignore[no-untyped-def]
        raise ScribeSenseError(ErrorCode.ADAPTER_COMMAND_FAILED)

    monkeypatch.setattr(run_integration, "run_cmd", failing)
    assert run_integration.main([]) == run_integration.EXIT_SANDBOX_UNAVAILABLE
