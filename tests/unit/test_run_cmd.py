"""K8 run_cmd() outcome mapping. Launches ONLY the current Python interpreter or files in the test's
temp dir (enforced by the `real_subprocess` guard in conftest)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.paths import TERMINATE_GRACE_S, run_cmd

pytestmark = pytest.mark.real_subprocess
PY = sys.executable


def test_success_returns_output() -> None:
    result = run_cmd([PY, "-c", "print('ok')"], timeout=10)
    assert result.returncode == 0 and result.stdout.strip() == b"ok"


def test_nonzero_exit_is_command_failed() -> None:
    with pytest.raises(ScribeSenseError) as exc:
        run_cmd([PY, "-c", "raise SystemExit(3)"], timeout=10)
    assert exc.value.code is ErrorCode.ADAPTER_COMMAND_FAILED


def test_timeout_is_adapter_timeout_and_bounded() -> None:
    start = time.monotonic()
    with pytest.raises(ScribeSenseError) as exc:
        run_cmd([PY, "-c", "import time; time.sleep(30)"], timeout=0.5)
    assert exc.value.code is ErrorCode.ADAPTER_TIMEOUT
    assert time.monotonic() - start < 0.5 + TERMINATE_GRACE_S + 2


def test_timeout_kills_a_process_that_ignores_sigterm() -> None:
    script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    start = time.monotonic()
    with pytest.raises(ScribeSenseError) as exc:
        run_cmd([PY, "-c", script], timeout=0.5)
    assert exc.value.code is ErrorCode.ADAPTER_TIMEOUT
    assert time.monotonic() - start < 0.5 + TERMINATE_GRACE_S + 2


def test_missing_executable_is_adapter_unavailable(tmp_path: Path) -> None:
    with pytest.raises(ScribeSenseError) as exc:
        run_cmd([str(tmp_path / "does-not-exist")], timeout=5)
    assert exc.value.code is ErrorCode.ADAPTER_UNAVAILABLE


def test_permission_denied_is_not_folded_into_unavailable(tmp_path: Path) -> None:
    script = tmp_path / "not-executable.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o644)  # no exec bit
    with pytest.raises(PermissionError):
        run_cmd([str(script)], timeout=5)


def test_directory_as_executable_is_not_folded_into_unavailable(tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        run_cmd([str(tmp_path)], timeout=5)


@pytest.mark.parametrize("argv, timeout", [([], 5.0), ([PY, "-c", "pass"], 0.0), ([PY, "-c", "pass"], -1.0)])
def test_programming_errors_are_value_errors(argv: list[str], timeout: float) -> None:
    with pytest.raises(ValueError):
        run_cmd(argv, timeout=timeout)
