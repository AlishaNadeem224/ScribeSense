"""O4 — The only code ScribeSense runs as root (automatic setup, PLAN_v2 2026-11-22). Standalone on purpose.

It pre-installs the signed ScribeSense extension into every Chromium-family browser present, through each browser's
documented "external extensions" folder (Linux only: a `<extension-id>.json` naming a local .crx installs it without
the web store or developer mode), and removes it again:

    install <path to scribesense.crx>   copy it to CRX_TARGET; write <id>.json for every browser that is installed
    remove                              delete our <id>.json files (only ours) and CRX_TARGET

Security rules (root must never act on free input):
- Two commands, one argument at most. No paths are taken from the caller except the .crx to copy, which is READ and
  validated (CRX3 magic, size limit, a zip with a manifest.json whose public key gives the extension ID and whose
  version is a plain dotted number). Everything written goes to the fixed paths below, mode 0644, owned by root.
- Imports only the standard library, never the rest of ScribeSense; run it as `pkexec /usr/bin/python3 -I <file>`,
  so no user site-packages or environment reach root. A packaged install ships this file root-owned (W2); a
  development checkout runs it from the repository, which the user can edit — the prompt says so.
- remove() deletes a <id>.json only when it points at CRX_TARGET (never a file another program put there).

Folders (verified: Brave; documented by Google: Chrome; to verify: Chromium) — a browser counts as installed when one
of its install markers exists (BROWSER_FOLDERS):
    brave     /opt/brave.com/brave                     -> /opt/brave.com/brave/extensions, /usr/share/brave/extensions
    chrome    /opt/google/chrome                       -> /opt/google/chrome/extensions, /usr/share/google-chrome/extensions
    chromium  /usr/lib64/chromium-browser, /usr/lib/chromium  -> /usr/share/chromium/extensions
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

CRX_TARGET = Path("/usr/share/scribesense/scribesense.crx")
MAX_CRX_BYTES = 5 * 1024 * 1024
#: browser key -> (install markers, extension folders). Paths are absolute; `root` re-bases them for tests.
BROWSER_FOLDERS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "brave": (("/opt/brave.com/brave",), ("/opt/brave.com/brave/extensions", "/usr/share/brave/extensions")),
    "chrome": (("/opt/google/chrome",), ("/opt/google/chrome/extensions", "/usr/share/google-chrome/extensions")),
    "chromium": (("/usr/lib64/chromium-browser", "/usr/lib/chromium"), ("/usr/share/chromium/extensions",)),
}
_VERSION = re.compile(r"^[0-9]{1,5}(\.[0-9]{1,5}){0,3}$")
_ID_ALPHABET = str.maketrans("0123456789abcdef", "abcdefghijklmnop")


class HelperError(ValueError):
    """Refused input; the message never contains file content."""


def _at(root: Path, absolute: str | Path) -> Path:
    return root / str(absolute).lstrip("/")


def crx_info(data: bytes) -> tuple[str, str]:
    """(extension id, version) of a CRX3 file; HelperError for anything else."""
    if len(data) > MAX_CRX_BYTES:
        raise HelperError("the .crx is too large")
    if len(data) < 12 or data[:4] != b"Cr24":
        raise HelperError("not a .crx file")
    version, header_len = struct.unpack("<II", data[4:12])
    if version != 3 or 12 + header_len > len(data):
        raise HelperError("not a CRX3 file")
    try:
        with zipfile.ZipFile(io.BytesIO(data[12 + header_len:])) as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, ValueError):
        raise HelperError("the .crx has no readable manifest.json") from None
    key, ext_version = manifest.get("key"), manifest.get("version")
    if not isinstance(key, str) or not isinstance(ext_version, str) or not _VERSION.match(ext_version):
        raise HelperError("the .crx manifest has no pinned key or no plain version")
    import base64
    import binascii
    try:
        der = base64.b64decode(key, validate=True)
    except binascii.Error:
        raise HelperError("the .crx manifest key is not base64") from None
    return hashlib.sha256(der).hexdigest()[:32].translate(_ID_ALPHABET), ext_version


def installed_browsers(root: Path = Path("/")) -> list[str]:
    return [k for k, (markers, _) in BROWSER_FOLDERS.items() if any(_at(root, m).is_dir() for m in markers)]


def pointer(version: str) -> bytes:
    return (json.dumps({"external_crx": str(CRX_TARGET), "external_version": version}) + "\n").encode()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".scribesense.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def install(crx: Path, root: Path = Path("/")) -> list[str]:
    """Copy the validated .crx to CRX_TARGET and point every installed browser at it. Returns the browsers done."""
    data = crx.read_bytes()
    ext_id, version = crx_info(data)
    _write(_at(root, CRX_TARGET), data)
    done = []
    for browser in installed_browsers(root):
        for folder in BROWSER_FOLDERS[browser][1]:
            _write(_at(root, folder) / f"{ext_id}.json", pointer(version))
        done.append(browser)
    return done


def remove(root: Path = Path("/")) -> list[str]:
    """Delete our pointer files (only those naming CRX_TARGET) and CRX_TARGET. Returns the files removed."""
    removed = []
    for _, folders in BROWSER_FOLDERS.values():
        for folder in folders:
            directory = _at(root, folder)
            try:
                names = sorted(os.listdir(directory))
            except OSError:
                continue
            for name in names:
                if not re.fullmatch(r"[a-p]{32}\.json", name):
                    continue
                path = directory / name
                try:
                    content = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(content, dict) and content.get("external_crx") == str(CRX_TARGET):
                    path.unlink()
                    removed.append(str(path))
    target = _at(root, CRX_TARGET)
    if target.exists():
        target.unlink()
        removed.append(str(target))
    return removed


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "install":
        command = "install"
    elif len(argv) == 2 and argv[1] == "remove":
        command = "remove"
    else:
        print("usage: root_helper.py install <scribesense.crx> | remove", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("root_helper: must run as root (through pkexec)", file=sys.stderr)
        return 2
    try:
        done = install(Path(argv[2])) if command == "install" else remove()
    except (HelperError, OSError) as exc:
        print(f"root_helper: {exc}", file=sys.stderr)
        return 1
    print(" ".join(done) if done else "nothing to do")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
