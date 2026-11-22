"""O4 — Facts about the ScribeSense browser extension shared by adapters and safety (M4.7, M4.9; 2026-11-22).

Kept at the adapters level so the Chromium adapter can ask "does the extension style this profile?" without
importing safety/ (import rule R5); `safety.native_host` and `safety.native_host_install` import from here.

    HOST_NAME / HOST_DESCRIPTION   the native-messaging host the extension connects to (K11)
    is_ours(data)                  a host manifest that ScribeSense wrote (name + description match)
    extension_id_from_manifest()   the pinned extension ID from the PUBLIC key in extension/manifest.json
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from pathlib import Path

HOST_NAME = "com.scribesense.host"
HOST_DESCRIPTION = "ScribeSense native messaging host"
#: Development fallback for the extension manifest (packaging moves it; Phase III W2).
DEFAULT_EXTENSION_MANIFEST = Path(__file__).resolve().parents[3] / "extension" / "manifest.json"
_ID_ALPHABET = str.maketrans("0123456789abcdef", "abcdefghijklmnop")


def is_ours(data: bytes) -> bool:
    """A host manifest ScribeSense wrote: a JSON object with our host name AND our description."""
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return (isinstance(manifest, dict) and manifest.get("name") == HOST_NAME
            and manifest.get("description") == HOST_DESCRIPTION)


def extension_id_from_manifest(manifest_path: Path | None = None) -> str:
    """The pinned extension ID for the public key in manifest.json (same rule as extension/tools/set_key.py).
    Default: DEFAULT_EXTENSION_MANIFEST, looked up at call time. ValueError when there is no usable key."""
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
