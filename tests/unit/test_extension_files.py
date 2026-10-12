"""M4.9 — static checks of the browser extension files and extension/tools/set_key.py.

The JavaScript behaviour itself is tested with node: node --test 'extension/test/*.test.mjs'.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from scribesense.contracts.messages import EXTENSION_ERRORS, MAX_MESSAGE_BYTES, MAX_READER_CHARS
from scribesense.safety import native_host

REPO = Path(__file__).resolve().parents[2]
EXT = REPO / "extension"
MANIFEST = EXT / "manifest.json"
TEXT_SUFFIXES = {".js", ".mjs", ".json", ".html", ".css", ".py", ".md"}


def _files(*, exclude: tuple[str, ...] = ()) -> list[Path]:
    out = []
    for path in sorted(EXT.rglob("*")):
        rel = path.relative_to(EXT)
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES or "__pycache__" in rel.parts:
            continue
        if rel.parts and rel.parts[0] in exclude:
            continue
        out.append(path)
    return out


def _load_set_key():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("scribesense_set_key", EXT / "tools" / "set_key.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no __pycache__ inside extension/
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


# --- manifest ----------------------------------------------------------------------------------------

def test_manifest_is_mv3_with_exactly_the_agreed_permissions() -> None:
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["manifest_version"] == 3
    assert manifest["name"] == "ScribeSense"
    assert re.fullmatch(r"\d+(\.\d+){0,3}", manifest["version"])
    assert sorted(manifest["permissions"]) == sorted(["nativeMessaging", "scripting", "contextMenus", "storage", "tabs"])
    assert manifest["host_permissions"] == ["<all_urls>"]
    assert manifest["background"] == {"service_worker": "background.js", "type": "module"}
    assert manifest["action"]["default_popup"] == "popup.html"
    for name in ("background.js", "popup.html", "popup.js"):
        assert (EXT / name).is_file(), name


def test_manifest_has_no_key_yet_and_nothing_that_widens_access() -> None:
    manifest = json.loads(MANIFEST.read_text())
    # O4 inserts the PUBLIC key with extension/tools/set_key.py (decision 2026-10-12); until then: none.
    assert "key" not in manifest
    for field in ("content_scripts", "web_accessible_resources", "externally_connectable",
                  "content_security_policy", "update_url", "optional_permissions", "optional_host_permissions"):
        assert field not in manifest, field


# --- privacy / network -------------------------------------------------------------------------------

def test_no_remote_urls_in_extension_files() -> None:
    # extension/test/ holds node tests with made-up page URLs for fake tabs; it is not loaded by the browser.
    pattern = re.compile(r"\b(?:https?|wss?|ftp)://|@import\b|\burl\s*\(", re.I)
    files = _files(exclude=("test",))
    assert {p.name for p in files} >= {"manifest.json", "background.js", "popup.html", "popup.js", "set_key.py"}
    found = [f"{p.relative_to(EXT)}: {m.group(0)}" for p in files for m in pattern.finditer(p.read_text())]
    assert found == []


@pytest.mark.parametrize("pattern", [
    r"\beval\s*\(", r"\bnew\s+Function\b", r"\bFunction\s*\(", r"\bfetch\s*\(", r"\bXMLHttpRequest\b",
    r"\bWebSocket\b", r"\bEventSource\b", r"\bsendBeacon\b", r"\bimportScripts\b", r"\bWebTransport\b",
])
def test_no_dynamic_code_or_network_apis_in_javascript(pattern: str) -> None:
    js = [p for p in _files() if p.suffix in {".js", ".mjs", ".html"}]
    hits = [str(p.relative_to(EXT)) for p in js if re.search(pattern, p.read_text())]
    assert hits == []


def test_popup_has_no_inline_or_remote_scripts() -> None:
    html = (EXT / "popup.html").read_text()
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, re.S | re.I)
    assert scripts, "popup loads popup.js"
    for attrs, body in scripts:
        assert body.strip() == "", "MV3 forbids inline scripts"
        assert re.search(r'src="popup\.js"', attrs)
    assert not re.search(r"\son[a-z]+\s*=", html, re.I), "no inline event handlers"


def test_extension_never_builds_css_itself() -> None:
    source = (EXT / "background.js").read_text()
    for declaration in ("font-family", "line-height", "letter-spacing", "word-spacing", "@font-face"):
        assert declaration not in source


# --- constants shared with Python ----------------------------------------------------------------------

def _js_const(name: str) -> str:
    match = re.search(rf"export const {name} = (.+?);", (EXT / "background.js").read_text())
    assert match, name
    return match.group(1)


def test_javascript_constants_match_the_contract() -> None:
    assert json.loads(_js_const("HOST_NAME")) == native_host.HOST_NAME == "com.scribesense.host"
    assert int(_js_const("MAX_READER_CHARS").split("//")[0]) == MAX_READER_CHARS
    assert _js_const("MAX_MESSAGE_BYTES").split("//")[0].strip() == "1024 * 1024" and MAX_MESSAGE_BYTES == 1024 * 1024
    codes = json.loads(re.search(r"Object\.freeze\((\[.*?\])\)", _js_const("ERROR_CODES")).group(1))
    assert set(codes) == {c.value for c in EXTENSION_ERRORS}


# --- set_key.py ----------------------------------------------------------------------------------------

def _der(tag: int, content: bytes) -> bytes:
    n = len(content)
    if n < 0x80:
        return bytes([tag, n]) + content
    size = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(size)]) + size + content


RSA_ALG = _der(0x30, _der(0x06, bytes.fromhex("2a864886f70d010101")) + _der(0x05, b""))
MODULUS = b"\x00" + bytes((i * 37 + 11) % 256 for i in range(256))  # made-up, not a real key
RSA_PUB = _der(0x30, _der(0x02, MODULUS) + _der(0x02, b"\x01\x00\x01"))
SPKI = _der(0x30, RSA_ALG + _der(0x03, b"\x00" + RSA_PUB))
PKCS8_PRIVATE = _der(0x30, _der(0x02, b"\x00") + RSA_ALG + _der(0x04, RSA_PUB))


def _pem(label: str, der: bytes) -> bytes:
    body = base64.encodebytes(der).decode().replace("\n", "")
    lines = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN {label}-----\n{lines}\n-----END {label}-----\n".encode()


def _expected_id(der: bytes) -> str:
    digest = hashlib.sha256(der).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def test_set_key_writes_public_key_and_prints_the_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    set_key = _load_set_key()
    manifest = tmp_path / "manifest.json"
    original = json.loads(MANIFEST.read_text())
    manifest.write_text(MANIFEST.read_text())
    manifest.chmod(0o644)
    pub = tmp_path / "pub.pem"
    pub.write_bytes(_pem("PUBLIC KEY", SPKI))

    assert set_key.main([str(pub), "--manifest", str(manifest)]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed == _expected_id(SPKI)
    assert re.fullmatch(r"[a-p]{32}", printed)
    updated = json.loads(manifest.read_text())
    assert base64.b64decode(updated.pop("key")) == SPKI
    assert updated == original, "every other field kept"
    assert list(json.loads(manifest.read_text()))[: len(original)] == list(original), "field order kept"
    assert manifest.stat().st_mode & 0o777 == 0o644
    assert native_host.render_host_manifest(printed, "/usr/bin/x")  # the ID is accepted by the host manifest


def test_set_key_accepts_der_and_id_only_leaves_manifest_alone(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    set_key = _load_set_key()
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"manifest_version": 3}\n')
    pub = tmp_path / "pub.der"
    pub.write_bytes(SPKI)
    assert set_key.main([str(pub), "--manifest", str(manifest), "--id-only"]) == 0
    assert capsys.readouterr().out.strip() == _expected_id(SPKI)
    assert manifest.read_text() == '{"manifest_version": 3}\n'


def test_set_key_writes_through_a_symlinked_manifest(tmp_path: Path) -> None:
    set_key = _load_set_key()
    real = tmp_path / "real.json"
    real.write_text('{"manifest_version": 3}\n')
    link = tmp_path / "manifest.json"
    link.symlink_to(real)
    set_key.write_manifest_key(link, SPKI)
    assert link.is_symlink()
    assert "key" in json.loads(real.read_text())


@pytest.mark.parametrize("content", [
    _pem("PRIVATE KEY", PKCS8_PRIVATE),
    _pem("RSA PRIVATE KEY", PKCS8_PRIVATE),
    _pem("ENCRYPTED PRIVATE KEY", PKCS8_PRIVATE),
    _pem("OPENSSH PRIVATE KEY", PKCS8_PRIVATE),
    _pem("PUBLIC KEY", SPKI) + _pem("PRIVATE KEY", PKCS8_PRIVATE),  # bundle with both
    PKCS8_PRIVATE,  # raw DER private key
])
def test_set_key_refuses_private_keys_loudly(tmp_path: Path, capsys: pytest.CaptureFixture[str], content: bytes) -> None:
    set_key = _load_set_key()
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"manifest_version": 3}\n')
    key = tmp_path / "key"
    key.write_bytes(content)
    assert set_key.main([str(key), "--manifest", str(manifest)]) == 2
    err = capsys.readouterr()
    assert "PRIVATE" in err.err and err.out == ""
    assert manifest.read_text() == '{"manifest_version": 3}\n'


@pytest.mark.parametrize("content", [
    b"", b"hello", _pem("CERTIFICATE", SPKI), _pem("RSA PUBLIC KEY", RSA_PUB), SPKI + b"\x00",
    _pem("PUBLIC KEY", SPKI).replace(b"M", b"!", 1),
])
def test_set_key_rejects_things_that_are_not_a_public_key(tmp_path: Path, content: bytes) -> None:
    set_key = _load_set_key()
    key = tmp_path / "key"
    key.write_bytes(content)
    assert set_key.main([str(key), "--id-only"]) == 2
