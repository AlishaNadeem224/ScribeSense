"""O3 — bubblewrap sandbox for integration tests (M3.3, integration level).

`build_argv()` is pure (no process, no I/O) so it is unit-tested. `check_available()` fails closed:
if bubblewrap is missing or cannot create the sandbox, integration tests do NOT run (SANDBOX_UNAVAILABLE).

What the sandbox gives a test (card M3.3, R2):
  - private /tmp, private HOME and XDG_RUNTIME_DIR (tmpfs); the real home is not mounted at all
  - no host D-Bus / Wayland / X11 / Hyprland / service sockets (the whole /run tree is private)
  - --unshare-all (incl. network), --clearenv, no inherited descriptors, --die-with-parent
  - only /usr, /etc/fonts, /etc/ld.so.cache, /etc/passwd-less minimum mounted read-only
  - repo and the venv's site-packages mounted read-only; the root itself is remounted read-only
  - GSETTINGS_BACKEND=keyfile, private fontconfig, SCRIBESENSE_SANDBOX=bwrap
"""

from __future__ import annotations

import shutil
from pathlib import Path

from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.paths import run_cmd

SANDBOX_HOME = "/tmp/home"
SANDBOX_RUNTIME = "/tmp/run"
WORK = "/work"
SITE = "/site"

#: Read-only host paths a test may see. Not the home directory, not /run, not /var.
RO_SYSTEM_PATHS = ("/usr", "/etc/fonts", "/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d",
                   "/etc/alternatives", "/etc/localtime")
#: Library symlinks live at the root on merged-/usr systems (Fedora).
ROOT_SYMLINKS = ("/bin", "/sbin", "/lib", "/lib64")


def bwrap_path() -> str | None:
    return shutil.which("bwrap")


def build_argv(command: list[str], *, repo: Path, site_packages: tuple[Path, ...] = (),
               bwrap: str = "bwrap") -> list[str]:
    """The bwrap command line that runs `command` inside the sandbox. Pure function."""
    if not command:
        raise ValueError("command must not be empty")
    argv = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv"]
    for p in ROOT_SYMLINKS:
        if Path(p).is_symlink():
            argv += ["--symlink", str(Path(p).readlink()), p]
        elif Path(p).exists():
            argv += ["--ro-bind", p, p]
    for p in RO_SYSTEM_PATHS:
        if Path(p).exists():
            argv += ["--ro-bind", p, p]
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run",
             "--dir", SANDBOX_HOME, "--dir", SANDBOX_RUNTIME, "--chmod", "0700", SANDBOX_RUNTIME,
             "--ro-bind", str(repo), WORK]
    pythonpath = [f"{WORK}/src", WORK]
    for i, sp in enumerate(site_packages):
        argv += ["--ro-bind", str(sp), f"{SITE}/{i}"]
        pythonpath.append(f"{SITE}/{i}")
    argv += ["--remount-ro", "/"]  # the sandbox root is read-only; only /tmp and /run (tmpfs) stay writable
    env = {
        "PATH": "/usr/bin",
        "HOME": SANDBOX_HOME,
        "XDG_CONFIG_HOME": f"{SANDBOX_HOME}/.config",
        "XDG_DATA_HOME": f"{SANDBOX_HOME}/.local/share",
        "XDG_CACHE_HOME": f"{SANDBOX_HOME}/.cache",
        "XDG_STATE_HOME": f"{SANDBOX_HOME}/.local/state",
        "XDG_RUNTIME_DIR": SANDBOX_RUNTIME,
        "SCRIBESENSE_DATA_DIR": f"{SANDBOX_HOME}/.local/share/scribesense",
        "SCRIBESENSE_FONTS_DIR": f"{SANDBOX_HOME}/.local/share/fonts/scribesense",
        "FONTCONFIG_FILE": f"{SANDBOX_HOME}/fonts.conf",  # private; a test writes it (includes /etc/fonts/fonts.conf)
        "GSETTINGS_BACKEND": "keyfile",
        "PYTHONPATH": ":".join(pythonpath),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "C.UTF-8",
        "SCRIBESENSE_SANDBOX": "bwrap",
    }
    for key, value in env.items():
        argv += ["--setenv", key, value]
    return argv + ["--chdir", WORK, "--"] + command


def check_available(*, repo: Path) -> None:
    """Fail closed. Starts a trivial sandbox; any problem -> SANDBOX_UNAVAILABLE."""
    exe = bwrap_path()
    if exe is None:
        raise ScribeSenseError(ErrorCode.SANDBOX_UNAVAILABLE)
    try:
        run_cmd(build_argv(["/usr/bin/true"], repo=repo, bwrap=exe), timeout=15)
    except (ScribeSenseError, OSError):
        raise ScribeSenseError(ErrorCode.SANDBOX_UNAVAILABLE) from None
