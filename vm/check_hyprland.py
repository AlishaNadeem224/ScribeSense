"""O3 — early feasibility check: does Hyprland run in this VM? (M3.5 "verify early", PLAN risk: GPU acceleration)

    python -m vm.check_hyprland        # inside the test VM only; refused anywhere else (vm.guard)

Read-only. It starts nothing and changes nothing: it reports what is installed and what the session offers, and
names the next step. Exit codes: 0 all good · 1 a requirement is missing · 3 refused (not the test VM).
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from vm import guard


@dataclass(frozen=True)
class Item:
    name: str
    ok: bool
    detail: str


def checks(*, which=shutil.which, exists=os.path.exists, env=os.environ) -> list[Item]:  # type: ignore[no-untyped-def]
    dri = [p for p in ("/dev/dri/card0", "/dev/dri/renderD128") if exists(p)]
    return [
        Item("Hyprland binary", which("Hyprland") is not None, "install: sudo dnf install hyprland"),
        Item("hyprctl", which("hyprctl") is not None, "ships with hyprland"),
        Item("GPU device (/dev/dri)", bool(dri),
             "found " + ", ".join(dri) if dri else "none: give the VM a virtual GPU (virtio-gpu with 3D / virgl)"),
        Item("flatpak", which("flatpak") is not None, "needed for the Flatpak scenarios: sudo dnf install flatpak"),
        Item("bubblewrap", which("bwrap") is not None, "sudo dnf install bubblewrap"),
        Item("XDG_RUNTIME_DIR", bool(env.get("XDG_RUNTIME_DIR")), "log in through a real session (not su / ssh without pam)"),
    ]


def main() -> int:
    try:
        guard.require_test_vm()
    except guard.NotATestVm as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return guard.EXIT_REFUSED
    items = checks()
    for it in items:
        print(f"{'OK     ' if it.ok else 'MISSING'} {it.name}: {it.detail}")
    if all(i.ok for i in items):
        print("\nAll present. Next: start Hyprland in the VM and re-run — then record the result in PLAN_v2.md.")
        return 0
    print("\nHyprland cannot be confirmed in this VM yet (see MISSING above). Report it early (card M3.5).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
