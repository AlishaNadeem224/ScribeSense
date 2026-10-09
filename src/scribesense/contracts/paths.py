"""K8 — Paths and the external-command helper (owner O3).

The only place paths are defined. No module may hard-code ~ or /home/... .
Every function honours environment overrides so the test sandbox (M3.3) can redirect them:
HOME, XDG_CONFIG_HOME, SCRIBESENSE_DATA_DIR, SCRIBESENSE_FONTS_DIR (and FONTCONFIG_FILE for fontconfig).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from scribesense.contracts.errors import ErrorCode, ScribeSenseError

#: K3 rule 9 — grace period between SIGTERM and SIGKILL when a command times out.
TERMINATE_GRACE_S = 2.0


def home() -> Path:
    """$HOME."""
    return Path(os.environ.get("HOME") or Path.home())


def data_dir() -> Path:
    """$SCRIBESENSE_DATA_DIR, else ~/.local/share/scribesense (store, backups, lock, socket)."""
    override = os.environ.get("SCRIBESENSE_DATA_DIR")
    return Path(override) if override else home() / ".local" / "share" / "scribesense"


def fonts_dir() -> Path:
    """$SCRIBESENSE_FONTS_DIR, else ~/.local/share/fonts/scribesense (generated fonts, one dir per key)."""
    override = os.environ.get("SCRIBESENSE_FONTS_DIR")
    return Path(override) if override else home() / ".local" / "share" / "fonts" / "scribesense"


def config_home() -> Path:
    """$XDG_CONFIG_HOME, else $HOME/.config."""
    override = os.environ.get("XDG_CONFIG_HOME")
    return Path(override) if override else home() / ".config"


def logs_dir() -> Path:
    """data_dir()/logs."""
    return data_dir() / "logs"


def lock_path() -> Path:
    """data_dir()/apply.lock — stable file, never unlinked or replaced; owner record inside (K4 rule 1)."""
    return data_dir() / "apply.lock"


def socket_path() -> Path:
    """data_dir()/service.sock — directory 0700, socket 0600 (K4 rule 5)."""
    return data_dir() / "service.sock"


def run_cmd(argv: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
    """The ONLY way to start an external command (K3 rule 9). Never call subprocess directly.

    - No shell; stdin closed; stdout/stderr captured as bytes; runs in the caller's process group.

    Outcome mapping (approved 2026-10-09) — each case stays distinguishable:
        executable not found (FileNotFoundError at launch) -> ScribeSenseError(ADAPTER_UNAVAILABLE)
        launch refused (PermissionError, e.g. no exec bit
          or argv[0] is a directory)                       -> PermissionError propagates unchanged
        any other launch OSError                           -> propagates unchanged
        non-zero exit                                      -> ScribeSenseError(ADAPTER_COMMAND_FAILED)
        timeout: SIGTERM -> TERMINATE_GRACE_S -> SIGKILL
          -> reap                                          -> ScribeSenseError(ADAPTER_TIMEOUT)
        empty argv / timeout <= 0                          -> ValueError (programming error)
    Only a missing executable maps to ADAPTER_UNAVAILABLE; other launch errors are NOT folded into it.
    There is no working-directory parameter, so an invalid cwd cannot occur here.
    Callers pick `timeout` from their card's TIMEOUTS (defaults: contracts.adapter.DEFAULT_TIMEOUTS).
    """
    if not argv:
        raise ValueError("argv must not be empty")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
    except FileNotFoundError:
        raise ScribeSenseError(ErrorCode.ADAPTER_UNAVAILABLE) from None
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.communicate(timeout=TERMINATE_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        raise ScribeSenseError(ErrorCode.ADAPTER_TIMEOUT) from None
    if proc.returncode != 0:
        raise ScribeSenseError(ErrorCode.ADAPTER_COMMAND_FAILED)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)
