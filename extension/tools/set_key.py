#!/usr/bin/env python3
"""Pin the ScribeSense extension ID: put a PUBLIC key into extension/manifest.json (M4.9, K11).

    python3 extension/tools/set_key.py /path/outside/the/repo/scribesense-extension.pub.pem

Reads a SubjectPublicKeyInfo public key (PEM "BEGIN PUBLIC KEY", or raw DER), writes its base64 DER
into manifest.json as "key", and prints the extension ID Chromium derives from it: the first 32 hex
digits of sha256(DER), with 0-f mapped to a-p. That ID goes into the native-messaging host manifest
(allowed_origins).

Private keys are REFUSED: this tool never reads, stores or needs the private key. See README.md here
for how to create the key pair outside the repository. Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "manifest.json"

_PEM_BLOCK = re.compile(rb"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.S)
_PRIVATE_MARKERS = (b"PRIVATE KEY", b"PRIVATE-KEY", b"PuTTY-User-Key-File")
_TAG_SEQUENCE = 0x30
_TAG_BIT_STRING = 0x03
_TAG_INTEGER = 0x02
_ID_ALPHABET = str.maketrans("0123456789abcdef", "abcdefghijklmnop")


class KeyFileError(ValueError):
    """A key file this tool will not use; the message says why (no key material in it)."""


def _der_element(data: bytes, pos: int) -> tuple[int, int, int]:
    """(tag, content_start, content_end) of the DER element at `pos`."""
    if pos + 2 > len(data):
        raise KeyFileError("not a DER public key (truncated)")
    tag = data[pos]
    length = data[pos + 1]
    start = pos + 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4 or start + count > len(data):
            raise KeyFileError("not a DER public key (bad length)")
        length = int.from_bytes(data[start:start + count], "big")
        start += count
    end = start + length
    if end > len(data):
        raise KeyFileError("not a DER public key (truncated)")
    return tag, start, end


def check_public_key_der(der: bytes) -> None:
    """Accept only SubjectPublicKeyInfo: SEQUENCE { SEQUENCE algorithm, BIT STRING key }.

    A PKCS#8 / PKCS#1 private key starts its SEQUENCE with an INTEGER (version) and is refused loudly.
    """
    tag, start, end = _der_element(der, 0)
    if tag != _TAG_SEQUENCE or end != len(der):
        raise KeyFileError("not a DER SubjectPublicKeyInfo public key")
    first_tag, _, first_end = _der_element(der, start)
    if first_tag == _TAG_INTEGER:
        raise KeyFileError("this looks like a PRIVATE key. Refusing. Give the PUBLIC key (openssl pkey -pubout)")
    if first_tag != _TAG_SEQUENCE:
        raise KeyFileError("not a DER SubjectPublicKeyInfo public key")
    second_tag, _, second_end = _der_element(der, first_end)
    if second_tag != _TAG_BIT_STRING or second_end != end:
        raise KeyFileError("not a DER SubjectPublicKeyInfo public key")


def load_public_key(raw: bytes) -> bytes:
    """PEM or DER file content -> SubjectPublicKeyInfo DER bytes. Refuses private keys."""
    if any(marker in raw for marker in _PRIVATE_MARKERS):
        raise KeyFileError("this file contains a PRIVATE KEY. Refusing. Never put the private key in the repo; "
                        "give the PUBLIC key file instead (see extension/tools/README.md)")
    if b"-----BEGIN" in raw:
        blocks = _PEM_BLOCK.findall(raw)
        if len(blocks) != 1:
            raise KeyFileError("expected exactly one PEM block")
        label, body = blocks[0]
        if label == b"RSA PUBLIC KEY":
            raise KeyFileError("PKCS#1 'RSA PUBLIC KEY' is not accepted; convert with: "
                            "openssl rsa -RSAPublicKey_in -in <file> -pubout -out <new file>  (gives 'BEGIN PUBLIC KEY')")
        if label != b"PUBLIC KEY":
            raise KeyFileError("expected a 'BEGIN PUBLIC KEY' PEM block")
        try:
            der = base64.b64decode(b"".join(body.split()), validate=True)
        except binascii.Error:
            raise KeyFileError("PEM body is not valid base64") from None
    else:
        der = raw
    check_public_key_der(der)
    return der


def extension_id(der: bytes) -> str:
    """Chromium extension ID for a SubjectPublicKeyInfo DER public key."""
    return hashlib.sha256(der).hexdigest()[:32].translate(_ID_ALPHABET)


def write_manifest_key(manifest_path: Path, der: bytes) -> None:
    """Set "key" in manifest.json, keeping every other field and its order. Atomic replace."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise KeyFileError("manifest.json is not a JSON object")
    manifest["key"] = base64.b64encode(der).decode("ascii")
    data = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    target = Path(os.path.realpath(manifest_path))
    mode = target.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(prefix=".manifest.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a PUBLIC key into the extension manifest and print the ID.")
    parser.add_argument("public_key", type=Path, help="public key file (PEM 'BEGIN PUBLIC KEY' or DER)")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="manifest.json to update")
    parser.add_argument("--id-only", action="store_true", help="only print the ID; do not change manifest.json")
    args = parser.parse_args(argv)
    try:
        der = load_public_key(args.public_key.read_bytes())
        if not args.id_only:
            write_manifest_key(args.manifest, der)
    except KeyFileError as exc:
        print(f"set_key: ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"set_key: ERROR: cannot read or write a file ({exc.strerror})", file=sys.stderr)
        return 1
    print(extension_id(der))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
