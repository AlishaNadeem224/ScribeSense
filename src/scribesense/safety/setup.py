"""O4 — Automatic setup and repair for browsers (PLAN_v2 "Automatic setup and repair", 2026-11-22).

The user never opens a terminal. On every service start and after every Keep, `ensure()`:
  1. CHECKS this machine (discovery, nothing hard-coded to one machine): every installed Chromium-family browser
     (M4.7's list, plus the system-wide install markers of root_helper.BROWSER_FOLDERS), whether our native-messaging
     host is installed for it and its launcher still works, and whether the signed extension is pre-installed for it.
  2. REPAIRS per-user items silently: host manifests + the development launcher (safety/native_host_install.py).
  3. ASKS ONCE for the root item: `pkexec /usr/bin/python3 -I <root_helper.py> install <scribesense.crx>` — polkit's
     own graphical password dialog. Asked AT MOST ONCE per .crx version (accepted or declined), so the user is
     never asked on every font change; a newer .crx asks again. No polkit agent -> not asked, reported instead.

Firefox / Zen need nothing here (their stylesheet is written by Apply). Flatpak Chromium browsers are reported
`unsupported` (a host outside the sandbox cannot be started from inside it).

The signed .crx is a release artifact made by the maintainer with the private key (never in the repo): looked for at
`root_helper.CRX_TARGET` (already installed / packaged) and the development fallback `extension/dist/scribesense.crx`.

Development note: in a repository checkout the root helper is a file the user can edit; a packaged install ships it
root-owned and does the root step at install time (W2), so packaged users are never prompted.

    python -m scribesense.safety.setup check | repair       (setup tool for development; not part of K10)
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from scribesense.adapters.chromium import BROWSERS, user_data_dir
from scribesense.adapters.extension_host import extension_id_from_manifest
from scribesense.adapters.fontconfig import write_durable
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.paths import data_dir, run_cmd
from scribesense.log import log
from scribesense.safety import native_host_install as nhi
from scribesense.safety import root_helper

REPO = Path(__file__).resolve().parents[3]
DEV_CRX = REPO / "extension" / "dist" / "scribesense.crx"
ROOT_HELPER = Path(root_helper.__file__).resolve()
SYSTEM_PYTHON = "/usr/bin/python3"
PKEXEC_TIMEOUT_S = 300.0
STATE_FILE = "setup-state.json"
#: Process names of polkit authentication agents (what shows the password dialog under a Wayland compositor).
POLKIT_AGENTS = ("hyprpolkitagent", "polkit-gnome-authentication-agent-1", "polkit-kde-authentication-agent-1",
                 "polkit-mate-authentication-agent-1", "lxqt-policykit-agent", "lxpolkit", "xfce-polkit",
                 "soteria", "polkit-agent-helper")

Status = Literal["ok", "missing", "outdated", "unsupported", "unavailable"]


@dataclass(frozen=True)
class Item:
    kind: Literal["host", "extension", "agent"]
    browser: str  # M4.7 key, or "" for the agent
    display: str
    status: Status
    needs_root: bool = False


@dataclass
class SetupReport:
    items: list[Item] = field(default_factory=list)
    crx: Path | None = None
    crx_version: str | None = None

    def missing(self, kind: str, *, root: bool | None = None) -> list[Item]:
        return [i for i in self.items if i.kind == kind and i.status in ("missing", "outdated")
                and (root is None or i.needs_root == root)]


# ---------------------------------------------------------------- discovery

def find_crx() -> tuple[Path | None, str | None]:
    """The signed extension to pre-install, and its version (installed copy first, then the development build)."""
    for path in (root_helper.CRX_TARGET, DEV_CRX):
        try:
            _, version = root_helper.crx_info(path.read_bytes())
        except (OSError, root_helper.HelperError):
            continue
        return path, version
    return None, None


def chromium_browsers() -> list[tuple[str, str, bool]]:
    """(key, display, is_flatpak) for every Chromium-family browser INSTALLED: native ones by their system install
    marker (a leftover ~/.config folder of an uninstalled browser does not count), Flatpak ones by their data dir."""
    found = []
    system = set(root_helper.installed_browsers())
    for browser in BROWSERS:
        flatpak = browser.flatpak_app is not None
        if (flatpak and user_data_dir(browser).is_dir()) or (not flatpak and browser.key in system):
            found.append((browser.key, browser.display, flatpak))
    return found


def _host_ok(key: str) -> bool:
    browser = next(b for b in BROWSERS if b.key == key)
    path = user_data_dir(browser) / nhi.HOSTS_DIR / nhi.MANIFEST_FILE
    try:
        data = path.read_bytes()
        host = json.loads(data.decode("utf-8")).get("path", "")
    except (OSError, ValueError, AttributeError):
        return False
    return nhi.is_ours(data) and _launcher_ok(Path(host))


def _launcher_ok(host: Path) -> bool:
    """The host executable exists and, for our development launcher, the Python it starts still exists."""
    if not host.is_absolute() or not os.access(host, os.X_OK):
        return False
    if host != nhi.launcher_path():
        return True
    match = re.search(r"^exec '?([^' ]+)'? -m scribesense\.safety\.native_host", host.read_text(), re.M)
    return bool(match) and os.access(match.group(1), os.X_OK)


def _extension_status(key: str, ext_id: str, version: str) -> Status:
    folders = root_helper.BROWSER_FOLDERS.get(key)
    if folders is None:
        return "unsupported"
    seen = None
    for folder in folders[1]:
        try:
            pointer = json.loads((Path(folder) / f"{ext_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(pointer, dict) and pointer.get("external_crx") == str(root_helper.CRX_TARGET):
            seen = pointer.get("external_version")
    if seen is None or not root_helper.CRX_TARGET.is_file():
        return "missing"
    return "ok" if seen == version else "outdated"


def polkit_agent_running() -> bool:
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            name = Path(f"/proc/{pid}/comm").read_text().strip()
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0].decode("utf-8", "replace")
        except OSError:
            continue
        if any(name == agent[:15] or os.path.basename(cmd) == agent for agent in POLKIT_AGENTS):  # comm: 15 chars
            return True
    return False


def check() -> SetupReport:
    """Read-only: what is missing on this machine."""
    report = SetupReport()
    report.crx, report.crx_version = find_crx()
    try:
        ext_id: str | None = extension_id_from_manifest()
    except (OSError, ValueError):
        ext_id = None
    for key, display, flatpak in chromium_browsers():
        if flatpak:
            report.items.append(Item("host", key, display, "unsupported"))
            continue
        report.items.append(Item("host", key, display, "ok" if _host_ok(key) else "missing"))
        if ext_id is None or report.crx_version is None:
            report.items.append(Item("extension", key, display, "unavailable", needs_root=True))
        else:
            report.items.append(Item("extension", key, display, _extension_status(key, ext_id, report.crx_version),
                                     needs_root=True))
    if report.missing("extension", root=True):
        report.items.append(Item("agent", "", "password prompt (polkit agent)",
                                 "ok" if polkit_agent_running() else "missing"))
    return report


# ---------------------------------------------------------------- repair

def repair_user(report: SetupReport) -> list[str]:
    """Install / refresh our host manifests (and the development launcher) where missing. Never prompts."""
    if not report.missing("host"):
        return []
    try:
        extension_id = extension_id_from_manifest()
        host = nhi.write_launcher()
    except (OSError, ValueError, ScribeSenseError) as exc:
        code = exc.code if isinstance(exc, ScribeSenseError) else ErrorCode.NOT_WRITABLE
        log(code, "setup", step="host")
        return []
    result = nhi.install(extension_id, host)
    return [t.browser for t, outcome in result.outcomes if outcome in ("written", "replaced")]


def _state_path() -> Path:
    return data_dir() / STATE_FILE


def _asked_version() -> str | None:
    try:
        value = json.loads(_state_path().read_text()).get("root_asked_for")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def _remember_asked(version: str | None, outcome: str) -> None:
    path = _state_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_durable(path, (json.dumps({"root_asked_for": version, "outcome": outcome}) + "\n").encode(), 0o600)


def root_needed(report: SetupReport) -> bool:
    """A root repair is due: something is missing, a .crx exists, an agent can ask, and this .crx version was not
    asked for yet (accepted or declined — never a loop)."""
    return (bool(report.missing("extension", root=True)) and report.crx is not None
            and not report.missing("agent") and _asked_version() != report.crx_version)


def repair_root(report: SetupReport) -> bool:
    """One polkit prompt to pre-install the extension for every installed browser. True when it ran."""
    if not root_needed(report):
        return False
    argv = ["pkexec", SYSTEM_PYTHON, "-I", str(ROOT_HELPER), "install", str(report.crx)]
    try:
        run_cmd(argv, timeout=PKEXEC_TIMEOUT_S)
    except (ScribeSenseError, OSError) as exc:  # cancelled, not authorised, no agent, timeout
        code = exc.code if isinstance(exc, ScribeSenseError) else ErrorCode.ADAPTER_COMMAND_FAILED
        log(code, "setup", step="root")
        _remember_asked(report.crx_version, "declined")
        return False
    _remember_asked(report.crx_version, "done")
    return True


def extension_preinstalled() -> bool:
    """Is any of OUR pre-install files on this system (the .crx or a pointer naming it)?"""
    if root_helper.CRX_TARGET.exists():
        return True
    for _, folders in root_helper.BROWSER_FOLDERS.values():
        for folder in folders:
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for name in names:
                if not re.fullmatch(r"[a-p]{32}\.json", name):
                    continue
                try:
                    pointer = json.loads((Path(folder) / name).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(pointer, dict) and pointer.get("external_crx") == str(root_helper.CRX_TARGET):
                    return True
    return False


def remove_extension() -> Literal["removed", "absent", "failed"]:
    """Uninstall: remove the pre-installed extension from every browser (one password prompt, root_helper remove).
    Each browser then drops the extension at its next start. Nothing installed -> no prompt."""
    if not extension_preinstalled():
        return "absent"
    argv = ["pkexec", SYSTEM_PYTHON, "-I", str(ROOT_HELPER), "remove"]
    try:
        run_cmd(argv, timeout=PKEXEC_TIMEOUT_S)
    except (ScribeSenseError, OSError) as exc:  # cancelled, not authorised, no agent / terminal, timeout
        code = exc.code if isinstance(exc, ScribeSenseError) else ErrorCode.ADAPTER_COMMAND_FAILED
        log(code, "setup", step="remove")
        return "failed"
    return "removed" if not extension_preinstalled() else "failed"


def ensure(*, ask_root: bool = True, background: bool = True) -> SetupReport:
    """Check, repair per-user items, and (in a background thread by default) ask once for the root item.
    Never raises: setup problems are logged and reported, they never break the app."""
    try:
        report = check()
        repair_user(report)
        if ask_root and root_needed(report):
            if background:
                threading.Thread(target=repair_root, args=(report,), name="scribesense-setup", daemon=True).start()
            else:
                repair_root(report)
        return report
    except Exception:  # noqa: BLE001 — setup must never break the app
        log(ErrorCode.UNEXPECTED_ADAPTER_ERROR, "setup", step="ensure")
        return SetupReport()


# ---------------------------------------------------------------- command line (setup tool)

_TEXT = {"ok": "ok", "missing": "missing", "outdated": "outdated", "unsupported": "not supported (Flatpak)",
         "unavailable": "extension not built yet (no signed .crx / no key)"}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args not in (["check"], ["repair"]):
        print("usage: python -m scribesense.safety.setup check | repair", file=sys.stderr)
        return 2
    report = check() if args == ["check"] else ensure(background=False)
    if args == ["repair"]:
        report = check()
    print(f"signed extension: {report.crx} ({report.crx_version})" if report.crx else "signed extension: not found")
    for item in report.items:
        print(f"{item.display:28s} {item.kind:9s} {_TEXT[item.status]}{'  (needs your password)' if item.needs_root and item.status in ('missing', 'outdated') else ''}")
    if not report.items:
        print("No Chromium-family browser found (Firefox / Zen need nothing here).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
