"""O4 — Facts about the ScribeSense browser extension shared by adapters and safety (M4.7, M4.9; 2026-11-22).

Kept at the adapters level so the Chromium adapter can ask "does the extension style this profile?" without
importing safety/ (import rule R5); `safety.native_host` and `safety.native_host_install` import from here.

    HOST_NAME / HOST_DESCRIPTION   the native-messaging host the extension connects to (K11)
    is_ours(data)                  a host manifest that ScribeSense wrote (name + description match)
    extension_id_from_manifest()   the pinned extension ID from the PUBLIC key in extension/manifest.json
    extension_dir()                where the extension's files are (2026-11-24, packaging fix — see below)

Finding the extension's files (fixed 2026-11-24, O4; the same bug and the same fix as O1's fonts, W2.1): this file is
`<repo>/src/scribesense/adapters/extension_host.py` in a checkout, but `<site-packages>/scribesense/adapters/…` once
installed, where three parents no longer reach any `extension/` folder. Search order, re-read on every call:
  1. $SCRIBESENSE_EXTENSION_DIR — the ONLY candidate when set (a test pointing at an empty folder must not fall
     through to the real files; the sandboxes set it to the repo's copy).
  2. each ABSOLUTE $XDG_DATA_DIRS entry (default /usr/local/share:/usr/share) + /scribesense/extension — the packaged
     copy. Used only for the manifest's public key (the extension ID in the user's own host manifests); the .crx that
     root pre-installs is never taken from this search (safety/setup.py DEV_CRX).
  3. <repo>/extension — the development fallback.
A candidate counts only if it holds manifest.json; none does -> the last candidate (callers then fail as before:
OSError / ValueError, reported by setup and doctor).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path

HOST_NAME = "com.scribesense.host"
HOST_DESCRIPTION = "ScribeSense native messaging host"
EXTENSION_DIR_ENV = "SCRIBESENSE_EXTENSION_DIR"
_PACKAGE_DATA_SUBDIR = ("scribesense", "extension")
_DEFAULT_DATA_DIRS = os.pathsep.join(("/usr/local/share", "/usr/share"))  # XDG's default when unset or empty
MANIFEST_NAME = "manifest.json"
_ID_ALPHABET = str.maketrans("0123456789abcdef", "abcdefghijklmnop")


def is_ours(data: bytes) -> bool:
    """A host manifest ScribeSense wrote: a JSON object with our host name AND our description."""
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return (isinstance(manifest, dict) and manifest.get("name") == HOST_NAME
            and manifest.get("description") == HOST_DESCRIPTION)


def _repo_extension_dir() -> Path:
    """<repo>/extension in a checkout (src/scribesense/adapters/ -> three parents up). Development fallback only."""
    return Path(__file__).resolve().parents[3] / "extension"


def extension_candidates() -> tuple[Path, ...]:
    """Every place the extension's files may be, best first (see the module docstring). Public so `doctor` can
    report the search path."""
    override = os.environ.get(EXTENSION_DIR_ENV)
    if override:
        return (Path(override),)
    data_dirs = os.environ.get("XDG_DATA_DIRS") or _DEFAULT_DATA_DIRS
    # XDG: relative entries are invalid and ignored — never resolve a data dir against the current folder.
    found = [Path(part).joinpath(*_PACKAGE_DATA_SUBDIR) for part in data_dirs.split(os.pathsep)
             if part and os.path.isabs(part)]
    found.append(_repo_extension_dir())
    return tuple(found)


def extension_dir() -> Path:
    """The first candidate holding manifest.json, else the last candidate. Never raises."""
    candidates = extension_candidates()
    for candidate in candidates:
        if (candidate / MANIFEST_NAME).is_file():
            return candidate
    return candidates[-1]


def extension_manifest() -> Path:
    return extension_dir() / MANIFEST_NAME


def extension_id_from_manifest(manifest_path: Path | None = None) -> str:
    """The pinned extension ID for the public key in manifest.json (same rule as extension/tools/set_key.py).
    Default: `extension_manifest()`, looked up at call time. ValueError when there is no usable key."""
    path = extension_manifest() if manifest_path is None else manifest_path
    manifest = json.loads(path.read_text(encoding="utf-8"))
    key = manifest.get("key") if isinstance(manifest, dict) else None
    if not isinstance(key, str) or not key:
        raise ValueError("extension/manifest.json has no \"key\": run extension/tools/set_key.py first")
    try:
        der = base64.b64decode(key, validate=True)
    except binascii.Error:
        raise ValueError("the \"key\" in extension/manifest.json is not valid base64") from None
    return hashlib.sha256(der).hexdigest()[:32].translate(_ID_ALPHABET)
