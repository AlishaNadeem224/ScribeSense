"""O4 — Is a browser really running? Stale-lock detection for M4.6 (Firefox / Zen) and M4.7 (Chromium / Brave).

Both browser families keep a lock symlink while they run and remove it on a clean exit. After a crash — and, seen on
this project's dev machine 2026-11-22, after a normal quit of the Zen Flatpak — the lock stays behind. Treating any
lock as "running" then blocked Apply ("close the browser") and Revert (the prefs.js key) although nothing ran.
The browsers themselves decide the same way as this module:

    Flatpak browser (path under ~/.var/app/<app-id>/)  running  <=>  <app-id> is listed by `flatpak ps`
                                                       (the lock's PID is from inside the sandbox: meaningless here)
    Firefox family, native   lock -> "<ip>:+<pid>"     stale    <=>  ip is a loopback address AND /proc/<pid> is gone
    Chromium family, native  lock -> "<host>-<pid>"    stale    <=>  host == this machine's hostname AND pid is gone

Anything unclear stays "running" — the safe side (nothing is written while a browser may run): an unreadable link,
a lock written by another machine (a shared home), an unparsable target, `flatpak ps` failing or timing out.
A live PID that was reused by another program also reads as running; that only costs a "close the browser".
"""

from __future__ import annotations

import os
import re
import socket
from pathlib import Path
from typing import Literal

from scribesense.contracts.paths import run_cmd

FLATPAK_PS_TIMEOUT_S = 5.0
_FIREFOX_TARGET = re.compile(r"^(?P<host>[^:]+):\+(?P<pid>[0-9]+)$")
_CHROMIUM_TARGET = re.compile(r"^(?P<host>.+)-(?P<pid>[0-9]+)$")
_APP_ID = re.compile(r"^[A-Za-z0-9_.-]+$")

Kind = Literal["firefox", "chromium"]


def flatpak_app_of(path: Path) -> str | None:
    """The Flatpak app id when `path` lies under ~/.var/app/<app-id>/, else None."""
    parts = path.parts
    for i in range(len(parts) - 2):
        if parts[i] == ".var" and parts[i + 1] == "app" and _APP_ID.match(parts[i + 2]):
            return parts[i + 2]
    return None


def running_flatpak_apps() -> set[str] | None:
    """App ids of running Flatpak instances (`flatpak ps`), or None when that cannot be determined."""
    try:
        out = run_cmd(["flatpak", "ps", "--columns=application"], timeout=FLATPAK_PS_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — unknown means "maybe running": the caller stays on the safe side
        return None
    return {line.strip() for line in out.stdout.decode("utf-8", "replace").splitlines() if line.strip()}


def _pid_alive(pid: int) -> bool:
    return pid > 0 and os.path.exists(f"/proc/{pid}")


def lock_is_live(lock: Path, kind: Kind) -> bool:
    """True when the browser that owns `lock` may be running (see the module docstring)."""
    if not os.path.lexists(lock):
        return False
    app = flatpak_app_of(lock)
    if app is not None:
        apps = running_flatpak_apps()
        return apps is None or app in apps
    try:
        target = os.readlink(lock)
    except OSError:
        return True
    if kind == "firefox":
        match = _FIREFOX_TARGET.match(target)
        if match is None or not match.group("host").startswith("127."):
            return True
    else:
        match = _CHROMIUM_TARGET.match(target)
        if match is None or match.group("host") != socket.gethostname():
            return True
    return _pid_alive(int(match.group("pid")))
