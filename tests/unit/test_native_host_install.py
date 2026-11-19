"""M4.9 / Phase III W3 — the native-messaging host installer (safety/native_host_install.py), in the unit sandbox.

Every browser folder lives under the sandbox HOME; no real browser profile is touched and no process is started.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import stat
from pathlib import Path

import pytest

from scribesense.adapters.chromium import BROWSERS, user_data_dir
from scribesense.safety import native_host_install as nhi
from scribesense.safety.native_host import HOST_NAME, render_host_manifest

EXT_ID = "abcdefghijklmnopabcdefghijklmnop"
HOST = Path("/usr/libexec/scribesense/native-host")
REPO = Path(__file__).resolve().parents[2]


def browser(key: str):  # type: ignore[no-untyped-def]
    return next(b for b in BROWSERS if b.key == key)


def make_browser(key: str) -> Path:
    d = user_data_dir(browser(key))
    d.mkdir(parents=True)
    return d


def manifest_of(key: str) -> Path:
    return user_data_dir(browser(key)) / "NativeMessagingHosts" / f"{HOST_NAME}.json"


def outcomes(report: nhi.HostReport) -> list[tuple[str, str]]:
    return [(t.browser, o) for t, o in report.outcomes]


def test_only_installed_browsers_are_targets_and_flatpak_ones_are_unsupported(sandbox) -> None:  # type: ignore[no-untyped-def]
    assert nhi.host_targets() == []
    make_browser("brave")
    make_browser("brave-flatpak")
    targets = nhi.host_targets()
    assert [(t.browser, t.manifest) for t in targets] == [("brave", manifest_of("brave")), ("brave-flatpak", None)]
    assert outcomes(nhi.install(EXT_ID, HOST)) == [("brave", "written"), ("brave-flatpak", "unsupported")]


def test_install_writes_exactly_the_rendered_manifest(sandbox) -> None:  # type: ignore[no-untyped-def]
    make_browser("brave")
    make_browser("chromium")
    report = nhi.install(EXT_ID, HOST)
    assert outcomes(report) == [("brave", "written"), ("chromium", "written")]
    for key in ("brave", "chromium"):
        path = manifest_of(key)
        assert path.read_text() == render_host_manifest(EXT_ID, HOST)
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700  # created by us
        data = json.loads(path.read_text())
        assert data["allowed_origins"] == [f"chrome-extension://{EXT_ID}/"] and data["path"] == str(HOST)


def test_reinstall_is_unchanged_and_a_new_host_path_updates_our_file(sandbox) -> None:  # type: ignore[no-untyped-def]
    make_browser("chrome")
    nhi.install(EXT_ID, HOST)
    assert outcomes(nhi.install(EXT_ID, HOST)) == [("chrome", "unchanged")]
    other = Path("/opt/scribesense/host")
    assert outcomes(nhi.install(EXT_ID, other)) == [("chrome", "replaced")]
    assert json.loads(manifest_of("chrome").read_text())["path"] == str(other)


@pytest.mark.parametrize("foreign", [
    b'{"name": "com.scribesense.host", "description": "someone else", "path": "/x", "type": "stdio"}',
    b"not json at all",
])
def test_a_foreign_file_is_never_overwritten_or_deleted(sandbox, foreign: bytes) -> None:  # type: ignore[no-untyped-def]
    make_browser("brave")
    path = manifest_of("brave")
    path.parent.mkdir(parents=True)
    path.write_bytes(foreign)
    assert outcomes(nhi.install(EXT_ID, HOST)) == [("brave", "left_foreign")]
    assert outcomes(nhi.remove()) == [("brave", "left_foreign")]
    assert path.read_bytes() == foreign
    assert outcomes(nhi.status()) == [("brave", "left_foreign")]


def test_remove_deletes_only_our_file_and_leaves_the_directory(sandbox) -> None:  # type: ignore[no-untyped-def]
    make_browser("brave")
    make_browser("chromium")
    nhi.install(EXT_ID, HOST)
    manifest_of("chromium").unlink()  # gone already
    assert outcomes(nhi.remove()) == [("brave", "removed"), ("chromium", "absent")]
    assert not manifest_of("brave").exists() and manifest_of("brave").parent.is_dir()
    assert outcomes(nhi.status()) == [("brave", "absent"), ("chromium", "absent")]


def test_a_write_failure_is_reported_per_browser_and_the_rest_continue(sandbox) -> None:  # type: ignore[no-untyped-def]
    make_browser("brave")
    make_browser("chromium")
    blocked = manifest_of("brave").parent
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_bytes(b"")  # a FILE where the directory should be
    report = nhi.install(EXT_ID, HOST)
    assert outcomes(report) == [("brave", "failed"), ("chromium", "written")] and report.failed


def test_bad_arguments_are_refused_before_anything_is_written(sandbox) -> None:  # type: ignore[no-untyped-def]
    make_browser("brave")
    with pytest.raises(ValueError):
        nhi.install("not-an-id", HOST)
    with pytest.raises(ValueError):
        nhi.install(EXT_ID, "relative/host")
    assert not manifest_of("brave").exists()


def _set_key_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("set_key", REPO / "extension" / "tools" / "set_key.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_the_extension_id_matches_set_key_for_the_same_public_key(tmp_path: Path) -> None:
    der = bytes.fromhex("30820122300d06092a864886f70d01010105000382010f00") + bytes(range(256)) + b"\x02\x03\x01\x00\x01"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"name": "ScribeSense", "key": base64.b64encode(der).decode()}))
    expected = hashlib.sha256(der).hexdigest()[:32].translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))
    assert nhi.extension_id_from_manifest(manifest) == expected == _set_key_module().extension_id(der)


@pytest.mark.parametrize("content", [{"name": "ScribeSense"}, {"key": ""}, {"key": "***"}])
def test_a_manifest_without_a_usable_key_is_refused(tmp_path: Path, content: dict) -> None:  # type: ignore[type-arg]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(content))
    with pytest.raises(ValueError):
        nhi.extension_id_from_manifest(manifest)


def test_the_development_launcher_is_private_and_starts_the_host_module(sandbox) -> None:  # type: ignore[no-untyped-def]
    path = nhi.write_launcher("/opt/py env/bin/python3")
    assert path == nhi.launcher_path() and path.is_absolute()
    text = path.read_text()
    assert text.startswith("#!/bin/sh\n")
    assert "exec '/opt/py env/bin/python3' -m scribesense.safety.native_host \"$@\"" in text  # quoted path
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    with pytest.raises(ValueError):
        nhi.write_launcher("python3")


def test_cli_install_status_remove(sandbox, capsys) -> None:  # type: ignore[no-untyped-def]
    make_browser("brave")
    assert nhi.main(["install", "--extension-id", EXT_ID, "--host", str(HOST)]) == 0
    assert "Brave: host installed" in capsys.readouterr().out
    assert nhi.main(["status"]) == 0 and "Brave: host already installed" in capsys.readouterr().out
    assert nhi.main(["remove"]) == 0 and "Brave: host removed" in capsys.readouterr().out


def test_cli_install_without_a_key_explains_what_to_do(sandbox, capsys, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"name": "ScribeSense"}))
    monkeypatch.setattr(nhi, "DEFAULT_EXTENSION_MANIFEST", manifest)
    assert nhi.main(["install"]) == 2
    assert "set_key.py" in capsys.readouterr().err
