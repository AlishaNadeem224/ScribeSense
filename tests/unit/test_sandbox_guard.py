"""The unit-level isolation in tests/conftest.py actually blocks what it claims to block."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.paths import run_cmd


def _blocked_error_type() -> type[BaseException]:
    # ProcessLaunchBlocked lives in conftest; match by name to avoid importing conftest.
    return RuntimeError


def test_home_and_xdg_point_into_the_test_dir(sandbox, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    for var in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR",
                "SCRIBESENSE_DATA_DIR", "SCRIBESENSE_FONTS_DIR", "FONTCONFIG_FILE"):
        assert Path(os.environ[var]).is_relative_to(tmp_path), var
    assert os.environ["GSETTINGS_BACKEND"] == "memory"


def test_desktop_session_variables_are_removed() -> None:
    for var in ("DBUS_SESSION_BUS_ADDRESS", "WAYLAND_DISPLAY", "DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE"):
        assert var not in os.environ


@pytest.mark.parametrize("argv", [["gsettings", "list-schemas"], ["hyprctl", "version"], ["fc-match", "sans"],
                                  [sys.executable, "-c", "pass"]])
def test_subprocess_is_blocked(argv: list[str]) -> None:
    with pytest.raises(_blocked_error_type(), match="blocked"):
        subprocess.run(argv, check=False)


def test_run_cmd_is_blocked_in_unit_tests() -> None:
    with pytest.raises(_blocked_error_type(), match="blocked"):
        run_cmd(["gsettings", "get", "org.gnome.desktop.interface", "font-name"], timeout=5)


def test_os_level_launchers_are_blocked() -> None:
    with pytest.raises(_blocked_error_type(), match="blocked"):
        os.system("true")
    with pytest.raises(_blocked_error_type(), match="blocked"):
        os.posix_spawn("/bin/true", ["true"], {})


def test_unix_socket_outside_test_dir_is_blocked() -> None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with pytest.raises(_blocked_error_type(), match="blocked"):
            s.connect("/run/user/1000/bus")
    finally:
        s.close()


@pytest.mark.real_subprocess
def test_real_subprocess_marker_still_refuses_desktop_tools() -> None:
    with pytest.raises(_blocked_error_type(), match="blocked"):
        subprocess.run(["gsettings", "list-schemas"], check=False)


def test_require_sandbox_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("_conftest_under_test", Path(__file__).parents[1] / "conftest.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)  # dataclasses need the module registered
    spec.loader.exec_module(module)
    with pytest.raises(pytest.skip.Exception, match=ErrorCode.SANDBOX_UNAVAILABLE.value):
        module.require_sandbox({})
    module.require_sandbox({"SCRIBESENSE_SANDBOX": "bwrap"})  # inside the runner: allowed


@pytest.mark.integration
def test_integration_marker_never_runs_outside_the_runner() -> None:
    raise AssertionError("integration test executed outside the bubblewrap runner")  # must be skipped


def test_scribesense_error_is_not_a_process_block() -> None:
    assert not issubclass(ScribeSenseError, RuntimeError)
