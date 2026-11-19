"""O4 — Native-messaging host installer (M4.9, Phase III W3, 2026-11-19).

Chromium-family browsers find the host that `chrome.runtime.connectNative("com.scribesense.host")` starts through a
HOST MANIFEST file, `<user-data dir>/NativeMessagingHosts/com.scribesense.host.json` (per user). This module writes
that file (`native_host.render_host_manifest`) for every installed browser and removes it again.

Targets: the NATIVE browsers of M4.7 (`adapters.chromium.BROWSERS` without a Flatpak app id) whose user-data dir
exists — Brave, Google Chrome, Chromium. Flatpak browsers are reported `unsupported`: a host outside the sandbox
cannot be started from inside it.

Ownership (like M4.14's own keybind file): the manifest file is ScribeSense's alone — its name is our host name.
  install  absent → written · ours (name + description match) → replaced if different, else unchanged ·
           anything else at that path (not JSON, another name) → left_foreign, never overwritten.
  remove   ours → removed · absent → absent · foreign → left_foreign, never deleted.
Because the file holds nothing of the user's, it is not journaled (K4): removal restores "absent" exactly. The
NativeMessagingHosts directory is created when missing (0700) and left on removal (like M4.2's conf.d).

Host executable: the browser needs an ABSOLUTE path to an executable. Packaging (W2) ships one; for a development
install `write_launcher()` puts a two-line shell launcher in `data_dir()/native-host/`, starting the current Python
with `-m scribesense.safety.native_host`.

Extension ID: pinned by the PUBLIC key in extension/manifest.json (`extension/tools/set_key.py`);
`extension_id_from_manifest()` derives the same ID Chromium does (sha256 of the key's DER, first 32 hex digits
mapped 0-f → a-p). No key in the manifest → ValueError: the key is set by a human, never by this module.

Never run against the real desktop from tests (AGENTS.md §5): every path comes from K8 (`config_home()`, `home()`,
`data_dir()`), which the test sandbox redirects. Logging carries codes and identifiers only, never paths.

    python -m scribesense.safety.native_host_install install [--extension-id ID] [--host PATH]
    python -m scribesense.safety.native_host_install remove
    python -m scribesense.safety.native_host_install status
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from scribesense.adapters.chromium import BROWSERS, user_data_dir
from scribesense.adapters.fontconfig import delete_durable, write_durable
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.paths import data_dir
from scribesense.log import log
from scribesense.safety.native_host import HOST_DESCRIPTION, HOST_NAME, render_host_manifest

HOSTS_DIR = "NativeMessagingHosts"
MANIFEST_FILE = f"{HOST_NAME}.json"
MANIFEST_MODE = 0o644  # the browser reads it
HOSTS_DIR_MODE = 0o700
LAUNCHER_DIR = "native-host"
LAUNCHER_NAME = "scribesense-native-host"
LAUNCHER_MODE = 0o700
#: Development fallback for the extension manifest (packaging moves it; Phase III W2).
DEFAULT_EXTENSION_MANIFEST = Path(__file__).resolve().parents[3] / "extension" / "manifest.json"
_ID_ALPHABET = str.maketrans("0123456789abcdef", "abcdefghijklmnop")

Outcome = Literal["written", "replaced", "unchanged", "removed", "absent", "left_foreign", "unsupported", "failed"]


@dataclass(frozen=True)
class HostTarget:
    browser: str  # M4.7 browser key (identifier, may be logged)
    display: str  # shown to the user, never logged
    manifest: Path | None  # None: unsupported (Flatpak)


@dataclass
class HostReport:
    outcomes: list[tuple[HostTarget, Outcome]] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(o == "failed" for _, o in self.outcomes)

    def count(self, *outcomes: Outcome) -> int:
        return sum(1 for _, o in self.outcomes if o in outcomes)


# ---------------------------------------------------------------- targets and ownership

def host_targets() -> list[HostTarget]:
    """One target per installed Chromium-family browser (user-data dir exists), in M4.7's order."""
    found = []
    for browser in BROWSERS:
        if not user_data_dir(browser).is_dir():
            continue
        manifest = None if browser.flatpak_app else user_data_dir(browser) / HOSTS_DIR / MANIFEST_FILE
        found.append(HostTarget(browser.key, browser.display, manifest))
    return found


def is_ours(data: bytes) -> bool:
    """A host manifest this module wrote: JSON object with our host name AND our description."""
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return (isinstance(manifest, dict) and manifest.get("name") == HOST_NAME
            and manifest.get("description") == HOST_DESCRIPTION)


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------- extension ID and launcher

def extension_id_from_manifest(manifest_path: Path | None = None) -> str:
    """The pinned extension ID for the public key in the extension's manifest.json (same rule as set_key.py).
    Default: DEFAULT_EXTENSION_MANIFEST, looked up at call time."""
    path = DEFAULT_EXTENSION_MANIFEST if manifest_path is None else manifest_path
    manifest = json.loads(path.read_text(encoding="utf-8"))
    key = manifest.get("key") if isinstance(manifest, dict) else None
    if not isinstance(key, str) or not key:
        raise ValueError("extension/manifest.json has no \"key\": run extension/tools/set_key.py first")
    try:
        der = base64.b64decode(key, validate=True)
    except binascii.Error:
        raise ValueError("the \"key\" in extension/manifest.json is not valid base64") from None
    return hashlib.sha256(der).hexdigest()[:32].translate(_ID_ALPHABET)


def launcher_path() -> Path:
    return data_dir() / LAUNCHER_DIR / LAUNCHER_NAME


def launcher_script(python: str) -> bytes:
    return (f"#!/bin/sh\n# ScribeSense native-messaging host (development launcher)\n"
            f"exec {shlex.quote(python)} -m scribesense.safety.native_host \"$@\"\n").encode("utf-8")


def write_launcher(python: str = sys.executable) -> Path:
    """Write the development launcher (0700) and return its absolute path."""
    if not os.path.isabs(python):
        raise ValueError("python must be an absolute path")
    path = launcher_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_durable(path, launcher_script(python), LAUNCHER_MODE)
    return path


# ---------------------------------------------------------------- install / remove

def install(extension_id: str, host_path: str | Path, targets: list[HostTarget] | None = None) -> HostReport:
    """Write (or update) our host manifest for every supported browser. One failure does not stop the rest."""
    payload = render_host_manifest(extension_id, host_path).encode("utf-8")  # ValueError for bad arguments
    report = HostReport()
    for target in host_targets() if targets is None else targets:
        if target.manifest is None:
            report.outcomes.append((target, "unsupported"))
            continue
        try:
            current = _read(target.manifest)
            if current is not None and not is_ours(current):
                outcome: Outcome = "left_foreign"
            elif current == payload:
                outcome = "unchanged"
            else:
                target.manifest.parent.mkdir(mode=HOSTS_DIR_MODE, parents=True, exist_ok=True)
                write_durable(target.manifest, payload, MANIFEST_MODE)
                outcome = "written" if current is None else "replaced"
        except (OSError, ScribeSenseError) as exc:
            code = exc.code if isinstance(exc, ScribeSenseError) else ErrorCode.NOT_WRITABLE
            log(code, "native_host", step="install", target=target.browser)
            outcome = "failed"
        report.outcomes.append((target, outcome))
    return report


def remove(targets: list[HostTarget] | None = None) -> HostReport:
    """Delete our host manifest wherever it is ours; anything else is left alone and reported."""
    report = HostReport()
    for target in host_targets() if targets is None else targets:
        if target.manifest is None:
            continue
        try:
            current = _read(target.manifest)
            if current is None:
                outcome: Outcome = "absent"
            elif not is_ours(current):
                outcome = "left_foreign"
            else:
                delete_durable(target.manifest)
                outcome = "removed"
        except (OSError, ScribeSenseError) as exc:
            code = exc.code if isinstance(exc, ScribeSenseError) else ErrorCode.NOT_WRITABLE
            log(code, "native_host", step="remove", target=target.browser)
            outcome = "failed"
        report.outcomes.append((target, outcome))
    return report


def status(targets: list[HostTarget] | None = None) -> HostReport:
    """Read-only: is our host manifest installed for each browser?"""
    report = HostReport()
    for target in host_targets() if targets is None else targets:
        if target.manifest is None:
            report.outcomes.append((target, "unsupported"))
            continue
        current = _read(target.manifest)
        outcome: Outcome = "absent" if current is None else ("unchanged" if is_ours(current) else "left_foreign")
        report.outcomes.append((target, outcome))
    return report


# ---------------------------------------------------------------- command line (a setup tool, not part of K10)

_TEXT: dict[str, str] = {
    "written": "host installed", "replaced": "host updated", "unchanged": "host already installed",
    "removed": "host removed", "absent": "no host installed", "unsupported": "not supported (Flatpak browser)",
    "left_foreign": "another program's file is in the way — left untouched", "failed": "failed (see the log)",
}


def _print(report: HostReport) -> None:
    if not report.outcomes:
        print("No Chromium-family browser (Brave, Chrome, Chromium) was found.")
    for target, outcome in report.outcomes:
        print(f"{target.display}: {_TEXT[outcome]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scribesense.safety.native_host_install",
                                     description="Install or remove the ScribeSense native-messaging host.")
    sub = parser.add_subparsers(dest="command", required=True)
    inst = sub.add_parser("install")
    inst.add_argument("--extension-id", help="32 letters a-p (default: from extension/manifest.json)")
    inst.add_argument("--host", help="absolute path of the host executable (default: a development launcher)")
    sub.add_parser("remove")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    if args.command == "remove":
        report = remove()
    elif args.command == "status":
        report = status()
    else:
        try:
            extension_id = args.extension_id or extension_id_from_manifest()
            host = Path(args.host) if args.host else write_launcher()
            report = install(extension_id, host)
        except (ValueError, OSError) as exc:
            print(f"native_host_install: {exc}", file=sys.stderr)
            return 2
    _print(report)
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
