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
    brave     /opt/brave.com/brave                     -> /opt/brave.com/brave/extensions, /usr/share/brave/extensions,
                                                          /usr/share/chromium/extensions
    chrome    /opt/google/chrome                       -> /opt/google/chrome/extensions, /usr/share/google-chrome/extensions
    chromium  /usr/lib64/chromium-browser, /usr/lib/chromium  -> /usr/share/chromium/extensions
Brave reads `<install dir>/extensions` and Chromium's standalone folder `/usr/share/chromium/extensions` (it is a
non-Google Chromium build): the Brave binary contains that path and no `/usr/share/brave/extensions` (checked with
`strings` on Brave 1.x, 2026-11-24). `/usr/share/brave/extensions` is kept so existing installs are still cleaned up.

Packaged installs (W2, 2026-11-24): the RPM ships the .crx at CRX_TARGET and the pointer files in PACKAGE_FOLDERS
(/usr/share/chromium/extensions, /usr/share/google-chrome/extensions) as its OWN files, plus PACKAGED_MARKER. Then
`install` refuses (exit 3): root never rewrites a file rpm owns. `remove` still runs but leaves CRX_TARGET and
PACKAGE_FOLDERS alone: it only cleans OUR pointers left in the other folders by an earlier development setup (they
could name an older version and nothing else would ever remove them; review 2026-11-24).
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
#: Shipped by the RPM (W2): the package manager owns the extension files (see the module docstring).
PACKAGED_MARKER = Path("/usr/share/scribesense/packaged")
#: The pointer folders the RPM ships its own files in (never touched by this helper on a packaged install).
PACKAGE_FOLDERS: tuple[str, ...] = ("/usr/share/chromium/extensions", "/usr/share/google-chrome/extensions")
MAX_CRX_BYTES = 5 * 1024 * 1024
#: browser key -> (install markers, extension folders). Paths are absolute; `root` re-bases them for tests.
BROWSER_FOLDERS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "brave": (("/opt/brave.com/brave",), ("/opt/brave.com/brave/extensions", "/usr/share/brave/extensions",
                                          "/usr/share/chromium/extensions")),
    "chrome": (("/opt/google/chrome",), ("/opt/google/chrome/extensions", "/usr/share/google-chrome/extensions")),
    "chromium": (("/usr/lib64/chromium-browser", "/usr/lib/chromium"), ("/usr/share/chromium/extensions",)),
}
_VERSION = re.compile(r"^[0-9]{1,5}(\.[0-9]{1,5}){0,3}$")
_ID_ALPHABET = str.maketrans("0123456789abcdef", "abcdefghijklmnop")


class HelperError(ValueError):
    """Refused input; the message never contains file content."""


def _at(root: Path, absolute: str | Path) -> Path:
    return root / str(absolute).lstrip("/")


def _proto_fields(data: bytes) -> list[tuple[int, bytes]]:
    """(field number, bytes) of every length-delimited field of a protobuf message; varints are skipped. Only what
    a CRX3 header needs; HelperError for anything malformed or another wire type."""
    def varint(pos: int) -> tuple[int, int]:
        value = shift = 0
        while True:
            if pos >= len(data) or shift > 63:
                raise HelperError("malformed CRX3 header")
            byte = data[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value, pos
            shift += 7

    fields, pos = [], 0
    while pos < len(data):
        key, pos = varint(pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            _, pos = varint(pos)
        elif wire == 2:
            length, pos = varint(pos)
            if pos + length > len(data):
                raise HelperError("malformed CRX3 header")
            fields.append((number, data[pos:pos + length]))
            pos += length
        else:
            raise HelperError("malformed CRX3 header")
    return fields


def _header_id(header: bytes) -> str:
    """The extension ID the CRX3 header itself declares (SignedData.crx_id, field 10000 -> 1), required to match the
    sha256 of one of its signing keys (AsymmetricKeyProof.public_key in fields 2 / 3). The signatures themselves are
    checked by the browsers (no RSA / ECDSA in the standard library); this check stops a .crx signed with a different
    key than the pinned one from being shipped or installed under the pinned ID (review 2026-11-24)."""
    fields = _proto_fields(header)
    signed = [value for number, value in fields if number == 10000]
    crx_ids = [value for number, value in _proto_fields(signed[0]) if number == 1] if len(signed) == 1 else []
    if len(crx_ids) != 1 or len(crx_ids[0]) != 16:
        raise HelperError("the CRX3 header has no extension id")
    keys = [key for number, proof in fields if number in (2, 3)
            for n, key in _proto_fields(proof) if n == 1]
    if not any(hashlib.sha256(key).digest()[:16] == crx_ids[0] for key in keys):
        raise HelperError("the CRX3 header's id matches none of its signing keys")
    return crx_ids[0].hex().translate(_ID_ALPHABET)


def crx_info(data: bytes) -> tuple[str, str]:
    """(extension id, version) of a CRX3 file; HelperError for anything else. The ID from the header (signed data +
    signing key) must equal the ID from the manifest's pinned `key`."""
    if len(data) > MAX_CRX_BYTES:
        raise HelperError("the .crx is too large")
    if len(data) < 12 or data[:4] != b"Cr24":
        raise HelperError("not a .crx file")
    version, header_len = struct.unpack("<II", data[4:12])
    if version != 3 or 12 + header_len > len(data):
        raise HelperError("not a CRX3 file")
    header_id = _header_id(data[12:12 + header_len])
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
    manifest_id = hashlib.sha256(der).hexdigest()[:32].translate(_ID_ALPHABET)
    if manifest_id != header_id:
        raise HelperError("the .crx is signed with a different key than its manifest pins")
    return manifest_id, ext_version


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


def our_pointers(root: Path = Path("/"), *, outside_package: bool = False) -> list[Path]:
    """Our pointer files (a [a-p]{32}.json naming CRX_TARGET) in every browser folder; with `outside_package`, only
    those NOT in PACKAGE_FOLDERS (leftovers of a development setup on a packaged install)."""
    found: list[Path] = []
    folders = sorted({f for _, fs in BROWSER_FOLDERS.values() for f in fs})
    for folder in folders:
        if outside_package and folder in PACKAGE_FOLDERS:
            continue
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
                found.append(path)
    return found


def remove(root: Path = Path("/"), *, keep_package: bool = False) -> list[str]:
    """Delete our pointer files (only those naming CRX_TARGET) and CRX_TARGET. With `keep_package` (a packaged
    install), CRX_TARGET and PACKAGE_FOLDERS are left to the package manager. Returns the files removed."""
    removed = []
    for path in our_pointers(root, outside_package=keep_package):
        path.unlink()
        removed.append(str(path))
    if keep_package:
        return removed
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
    packaged = PACKAGED_MARKER.exists()
    if packaged and command == "install":
        print("root_helper: the scribesense package manages the extension; nothing to do", file=sys.stderr)
        return 3
    if os.geteuid() != 0:
        print("root_helper: must run as root (through pkexec)", file=sys.stderr)
        return 2
    try:
        done = install(Path(argv[2])) if command == "install" else remove(keep_package=packaged)
    except (HelperError, OSError) as exc:
        print(f"root_helper: {exc}", file=sys.stderr)
        return 1
    print(" ".join(done) if done else "nothing to do")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
