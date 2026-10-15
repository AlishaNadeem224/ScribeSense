"""O3 — "never run on the development machine" guard for the full-system tests (M3.5).

Why not just detect a VM: the development machine can itself be a VM (this one is QEMU). `systemd-detect-virt` therefore
proves nothing. A full-system run needs TWO explicit facts, both created only by the VM provisioning step
(`vm/README.md`), never by the tests:

    1. the marker file   /etc/scribesense-test-vm          (contents: MARKER_TEXT)
    2. the environment   SCRIBESENSE_FULL_SYSTEM_TESTS=1   (set by the VM runner, not by a developer shell by habit)

Anything else -> refuse (exit code 3, same "fail closed" code as the sandbox runner). Nothing is written, no
setting is changed, no process is started by this module.
"""

from __future__ import annotations

import os
from pathlib import Path

MARKER_PATH = Path("/etc/scribesense-test-vm")
MARKER_TEXT = "scribesense disposable test vm — reset from snapshot after every run\n"
ENV_FLAG = "SCRIBESENSE_FULL_SYSTEM_TESTS"
EXIT_REFUSED = 3


class NotATestVm(RuntimeError):
    """This machine is not the disposable test VM; the caller must stop."""


def refusal_reason(*, marker: Path = MARKER_PATH, env: dict[str, str] | os._Environ[str] = os.environ) -> str | None:
    """None if this is the provisioned test VM and the run was requested; otherwise why not."""
    try:
        text = marker.read_text()
    except OSError:
        return f"{marker} is missing: this is not a provisioned test VM"
    if text != MARKER_TEXT:
        return f"{marker} does not carry the expected text"
    if env.get(ENV_FLAG) != "1":
        return f"{ENV_FLAG}=1 is not set"
    return None


def require_test_vm(**kwargs) -> None:  # type: ignore[no-untyped-def]
    reason = refusal_reason(**kwargs)
    if reason:
        raise NotATestVm(reason)
