"""O3 — Test isolation (M3.3, unit level). Applies to EVERY test automatically.

What this file guarantees for ordinary (unit) tests:
  1. Environment redirected to a per-test temp dir: HOME, XDG_*, SCRIBESENSE_*, FONTCONFIG_FILE.
     Desktop session variables (D-Bus, Wayland, X11, Hyprland) are removed; GSETTINGS_BACKEND=memory.
  2. Process launching is BLOCKED (subprocess, os.system, exec*, spawn*, posix_spawn*, fork).
  3. Unix-socket connections outside the test's temp dir are BLOCKED.

A temp dir is defence in depth, NOT the sandbox. Tests that touch real desktop tooling are
`@pytest.mark.integration` and run ONLY inside the bubblewrap runner (M3.3, O3), which sets
SCRIBESENSE_SANDBOX=bwrap. Outside it they are skipped — fail closed, never run unsandboxed.

`@pytest.mark.real_subprocess` — only for tests of contracts.paths.run_cmd itself: processes may be
launched, but ONLY the current Python interpreter or files inside the test's temp dir.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from scribesense.contracts.errors import ErrorCode

SANDBOX_ENV = "SCRIBESENSE_SANDBOX"
SANDBOX_VALUE = "bwrap"

_SESSION_VARS = (
    "DBUS_SESSION_BUS_ADDRESS",
    "WAYLAND_DISPLAY",
    "DISPLAY",
    "HYPRLAND_INSTANCE_SIGNATURE",
    "XDG_SESSION_TYPE",
    "XDG_CURRENT_DESKTOP",
    "QT_QPA_PLATFORMTHEME",
)


class ProcessLaunchBlocked(RuntimeError):
    """Raised when a unit test tries to start a real process."""


@dataclass(frozen=True)
class Sandbox:
    root: Path
    home: Path
    config_home: Path
    data_dir: Path
    fonts_dir: Path
    runtime_dir: Path


def require_sandbox(env: dict[str, str] | os._Environ[str]) -> None:
    """Fail closed: integration tests run only inside the bubblewrap runner."""
    if env.get(SANDBOX_ENV) != SANDBOX_VALUE:
        pytest.skip(f"{ErrorCode.SANDBOX_UNAVAILABLE}: integration tests run only inside the bubblewrap runner (M3.3)")


@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Sandbox:
    if request.node.get_closest_marker("integration"):
        require_sandbox(os.environ)

    root = tmp_path / "sandbox"
    sb = Sandbox(
        root=root,
        home=root / "home",
        config_home=root / "home" / ".config",
        data_dir=root / "home" / ".local" / "share" / "scribesense",
        fonts_dir=root / "home" / ".local" / "share" / "fonts" / "scribesense",
        runtime_dir=root / "run",
    )
    for d in (sb.home, sb.config_home, sb.runtime_dir):
        d.mkdir(parents=True, exist_ok=True)
    sb.runtime_dir.chmod(0o700)

    monkeypatch.setenv("HOME", str(sb.home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(sb.config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(sb.home / ".local" / "share"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(sb.home / ".cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(sb.home / ".local" / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(sb.runtime_dir))
    monkeypatch.setenv("SCRIBESENSE_DATA_DIR", str(sb.data_dir))
    monkeypatch.setenv("SCRIBESENSE_FONTS_DIR", str(sb.fonts_dir))
    monkeypatch.setenv("FONTCONFIG_FILE", str(root / "fonts.conf"))
    monkeypatch.setenv("GSETTINGS_BACKEND", "memory")
    for var in _SESSION_VARS:
        monkeypatch.delenv(var, raising=False)

    if not request.node.get_closest_marker("integration"):
        _block_processes(monkeypatch, allow=bool(request.node.get_closest_marker("real_subprocess")), root=tmp_path)
        _block_foreign_unix_sockets(monkeypatch, root=tmp_path)
    return sb


def _block_processes(monkeypatch: pytest.MonkeyPatch, *, allow: bool, root: Path) -> None:
    real_popen_init = subprocess.Popen.__init__

    def guarded_popen_init(self, args, *a, **kw):  # type: ignore[no-untyped-def]
        argv0 = args if isinstance(args, (str, bytes, os.PathLike)) else args[0]
        argv0 = os.fsdecode(argv0)
        if allow and kw.get("shell") is not True:
            exe = Path(argv0)
            if exe == Path(sys.executable) or (exe.is_absolute() and _is_within(exe, root)):
                return real_popen_init(self, args, *a, **kw)
        raise ProcessLaunchBlocked(f"process launch blocked in unit tests: {argv0!r}")

    def blocked(*_a, **_kw):  # type: ignore[no-untyped-def]
        raise ProcessLaunchBlocked("process launch blocked in unit tests")

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_popen_init)
    monkeypatch.setattr(os, "system", blocked)
    monkeypatch.setattr(os, "fork", blocked)
    names = ["execv", "execve", "execl", "execle", "execlp", "execlpe", "execvp", "execvpe",
             "spawnv", "spawnve", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnvp", "spawnvpe",
             "forkpty"]
    if not allow:
        # In real_subprocess mode, Popen may launch through os.posix_spawn (Python ≥ 3.13); the
        # Popen guard above has already checked the executable, so posix_spawn stays usable there.
        names += ["posix_spawn", "posix_spawnp"]
    for name in names:
        if hasattr(os, name):
            monkeypatch.setattr(os, name, blocked)


def _block_foreign_unix_sockets(monkeypatch: pytest.MonkeyPatch, *, root: Path) -> None:
    real_connect = socket.socket.connect

    def guarded_connect(self, address):  # type: ignore[no-untyped-def]
        if self.family == socket.AF_UNIX:
            path = os.fsdecode(address) if isinstance(address, (str, bytes)) else ""
            if path.startswith("\0") or not path or not _is_within(Path(path), root):
                raise ProcessLaunchBlocked(f"unix socket outside the test dir blocked: {path!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
