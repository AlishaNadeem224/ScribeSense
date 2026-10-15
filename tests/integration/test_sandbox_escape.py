"""M3.3 escape tests — run ONLY inside the bubblewrap runner (skipped otherwise; failed when
SCRIBESENSE_REQUIRE_SANDBOX=1, so a missing sandbox cannot look green).

Each test checks the sandbox itself (mount table, namespaces, descriptors), not only that one call
fails — a connect() to a path that does not exist would "fail" even in a leaky sandbox.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _mounts() -> dict[str, str]:
    """mount point -> filesystem type, from this process's own mount table."""
    table: dict[str, str] = {}
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ")
        table[left.split()[4]] = right.split()[0]
    return table


def test_marker_and_private_environment() -> None:
    assert os.environ["SCRIBESENSE_SANDBOX"] == "bwrap"
    assert os.environ["GSETTINGS_BACKEND"] == "keyfile"
    for var in ("DBUS_SESSION_BUS_ADDRESS", "WAYLAND_DISPLAY", "DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE"):
        assert var not in os.environ, var


def test_real_home_is_not_visible() -> None:
    assert not Path("/home").exists()
    assert os.environ["HOME"].startswith("/tmp/")
    assert not any(m == "/home" or m.startswith("/home/") for m in _mounts())


def test_private_dirs_are_memory_backed() -> None:
    mounts = _mounts()
    assert mounts["/tmp"] == "tmpfs" and mounts["/run"] == "tmpfs"


def test_write_to_host_paths_is_blocked() -> None:
    for target in ("/usr/scribesense-escape", "/etc/scribesense-escape", "/etc/fonts/scribesense-escape",
                   "/work/scribesense-escape", "/work/src/scribesense-escape"):
        with pytest.raises(OSError):
            Path(target).write_text("x")


def test_system_and_repo_mounts_are_read_only() -> None:
    for target in ("/usr", "/work"):
        assert os.statvfs(target).f_flag & os.ST_RDONLY, target


def test_writes_to_home_stay_in_the_private_tmpfs() -> None:
    target = Path(os.environ["HOME"]) / ".config" / "scribesense-escape" / "probe"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x")
    assert target.read_text() == "x"
    assert _mounts()["/tmp"] == "tmpfs"  # HOME lives under /tmp, so this write never reaches the host


def test_no_host_session_sockets() -> None:
    assert not Path("/run/user").exists() and not Path("/run/dbus").exists()
    assert list(Path("/run").iterdir()) == []
    for path in ("/run/user/1000/bus", "/run/dbus/system_bus_socket", "/tmp/.X11-unix/X0",
                 os.environ["XDG_RUNTIME_DIR"] + "/hypr/x/.socket.sock"):
        s = socket.socket(socket.AF_UNIX)
        try:
            with pytest.raises(OSError):
                s.connect(path)
        finally:
            s.close()


def test_network_namespace_has_only_loopback() -> None:
    interfaces = [line.split(":")[0].strip() for line in Path("/proc/net/dev").read_text().splitlines()[2:]]
    assert interfaces == ["lo"]
    s = socket.socket(socket.AF_INET)
    s.settimeout(2)
    try:
        with pytest.raises(OSError):
            s.connect(("93.184.216.34", 80))
    finally:
        s.close()


def test_no_inherited_host_descriptors() -> None:
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue  # the listing's own descriptor
        assert not target.startswith("socket:"), (fd, target)
        assert not target.startswith("/home/"), (fd, target)


def test_user_namespace_is_private() -> None:
    assert os.getpid() < 100  # own PID namespace (--unshare-all); host PIDs are far larger
