"""O3 — Test isolation (M3.3, unit level). Applies to EVERY test automatically.

What this file guarantees for ordinary (unit) tests:
  1. Environment redirected to a per-test temp dir: HOME, XDG_*, SCRIBESENSE_*, FONTCONFIG_FILE.
     (XDG_RUNTIME_DIR is a short private /tmp/ss-*/run dir: the service socket lives there (K8) and
     Unix socket paths are limited to 108 bytes. The data dir sits beside it, OUTSIDE the temp HOME, so
     tests that fingerprint HOME see only what the code under test wrote there.)
     Desktop session variables (D-Bus, Wayland, X11, Hyprland) are removed; GSETTINGS_BACKEND=memory.
  2. Process launching is BLOCKED (subprocess, os.system, exec*, spawn*, posix_spawn*, fork).
  3. Unix-socket connections outside the test's temp dir are BLOCKED.

A temp dir is defence in depth, NOT the sandbox. Tests that touch real desktop tooling are
`@pytest.mark.integration` and run ONLY inside the bubblewrap runner (M3.3, O3), which sets
SCRIBESENSE_SANDBOX=bwrap. Outside it they are skipped — fail closed, never run unsandboxed. With SCRIBESENSE_REQUIRE_SANDBOX=1
(CI, release runs) a missing sandbox is a test FAILURE instead of a skip, so it cannot hide as "green".

`@pytest.mark.real_subprocess` — only for tests of contracts.paths.run_cmd itself: processes may be
launched, but ONLY the current Python interpreter or files inside the test's temp dir.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from scribesense.contracts.errors import ErrorCode

SANDBOX_ENV = "SCRIBESENSE_SANDBOX"
SANDBOX_VALUE = "bwrap"
REQUIRE_ENV = "SCRIBESENSE_REQUIRE_SANDBOX"

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
        message = f"{ErrorCode.SANDBOX_UNAVAILABLE}: integration tests run only inside the bubblewrap runner (M3.3)"
        if env.get(REQUIRE_ENV) == "1":
            pytest.fail(message, pytrace=False)
        pytest.skip(message)


@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Sandbox:
    if request.node.get_closest_marker("integration"):
        require_sandbox(os.environ)
        # Inside bubblewrap the runner has already set a private HOME / XDG_* / GSETTINGS_BACKEND=keyfile
        # and removed the session variables; do not override that environment.
        home = Path(os.environ["HOME"])
        runtime = Path(os.environ["XDG_RUNTIME_DIR"])
        _clear_dir(home, runtime)  # one HOME per session: start (and end) every test empty (M3.3 postcondition)
        request.addfinalizer(lambda: _clear_dir(home, runtime))
        return Sandbox(root=home, home=home, config_home=Path(os.environ["XDG_CONFIG_HOME"]),
                       data_dir=Path(os.environ["SCRIBESENSE_DATA_DIR"]),
                       fonts_dir=Path(os.environ["SCRIBESENSE_FONTS_DIR"]),
                       runtime_dir=Path(os.environ["XDG_RUNTIME_DIR"]))

    root = tmp_path / "sandbox"
    # service.sock lives in XDG_RUNTIME_DIR (K8). Unix socket paths are limited to 108 bytes and
    # pytest's tmp_path is long, so the runtime dir is a short private dir under /tmp, removed after the test.
    # The data dir is kept beside it, outside the temp HOME: O4's tests compare HOME before/after.
    short = Path(tempfile.mkdtemp(prefix="ss-", dir="/tmp"))
    request.addfinalizer(lambda: shutil.rmtree(short, ignore_errors=True))
    sb = Sandbox(
        root=root,
        home=root / "home",
        config_home=root / "home" / ".config",
        data_dir=short / "data",
        fonts_dir=root / "home" / ".local" / "share" / "fonts" / "scribesense",
        runtime_dir=short / "run",
    )
    for d in (sb.home, sb.config_home, sb.runtime_dir):
        d.mkdir(parents=True, exist_ok=True)
    sb.runtime_dir.chmod(0o700)
    assert len(str(sb.runtime_dir / "scribesense" / "service.sock")) < 108

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

    _block_processes(monkeypatch, allow=bool(request.node.get_closest_marker("real_subprocess")), root=tmp_path)
    _block_foreign_unix_sockets(monkeypatch, roots=(tmp_path, short))
    return sb


def _clear_dir(*dirs: Path) -> None:
    """Empty private sandbox dirs between tests. Only ever under the bwrap tmpfs (/tmp); refuses anything else."""
    for d in dirs:
        if not d.is_absolute() or d.parts[:2] != ("/", "tmp") or len(d.parts) < 3:
            raise RuntimeError(f"refusing to clear {d}: not a private sandbox dir")
        for child in d.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)


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


def _block_foreign_unix_sockets(monkeypatch: pytest.MonkeyPatch, *, roots: tuple[Path, ...]) -> None:
    real_connect = socket.socket.connect

    def guarded_connect(self, address):  # type: ignore[no-untyped-def]
        if self.family == socket.AF_UNIX:
            path = os.fsdecode(address) if isinstance(address, (str, bytes)) else ""
            if path.startswith("\0") or not path or not any(_is_within(Path(path), r) for r in roots):
                raise ProcessLaunchBlocked(f"unix socket outside the test dir blocked: {path!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
