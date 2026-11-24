"""Packaging fix (2026-11-24, O4): the extension's files are searched like O1's bundled fonts — override, installed
data dirs, then the repo — instead of three parents above the module (the Python library folder once installed)."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from scribesense.adapters import extension_host as eh
from scribesense.safety import native_host_install as nhi
from scribesense.safety import setup

REPO_EXTENSION = Path(__file__).resolve().parents[2] / "extension"
OTHER_KEY = base64.b64encode(b"a different public key").decode()  # so the searched copy is distinguishable


def repo_id() -> str:
    return eh.extension_id_from_manifest(REPO_EXTENSION / "manifest.json")


def other_id() -> str:
    return hashlib.sha256(b"a different public key").hexdigest()[:32].translate(str.maketrans("0123456789abcdef",
                                                                                             "abcdefghijklmnop"))


def packaged(root: Path, key: str | None = None) -> Path:
    folder = root / "scribesense" / "extension"
    folder.mkdir(parents=True)
    manifest = json.loads((REPO_EXTENSION / "manifest.json").read_text())
    if key is not None:
        manifest["key"] = key
    (folder / "manifest.json").write_text(json.dumps(manifest))
    return folder


def test_the_sandbox_pins_the_repos_copy() -> None:
    assert eh.extension_dir() == REPO_EXTENSION


def test_an_override_is_the_only_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(eh.EXTENSION_DIR_ENV, str(tmp_path / "empty"))
    assert eh.extension_candidates() == (tmp_path / "empty",)
    assert eh.extension_dir() == tmp_path / "empty"  # never falls through to the real files
    with pytest.raises(OSError):
        eh.extension_id_from_manifest()


def test_the_installed_copy_wins_over_the_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(eh.EXTENSION_DIR_ENV)
    monkeypatch.setenv("XDG_DATA_DIRS", f"{tmp_path / 'none'}:{tmp_path / 'share'}")
    folder = packaged(tmp_path / "share", key=OTHER_KEY)
    assert eh.extension_candidates()[-1] == REPO_EXTENSION
    assert eh.extension_dir() == folder
    assert eh.extension_id_from_manifest() == other_id() != repo_id()  # really read from the installed copy


def test_without_an_installed_copy_the_repo_is_the_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(eh.EXTENSION_DIR_ENV)
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "nothing-here"))
    assert eh.extension_dir() == REPO_EXTENSION


def test_an_installed_package_layout_still_finds_the_installed_copy(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug: once installed, three parents above the module is the library folder, with no extension/ in it."""
    monkeypatch.delenv(eh.EXTENSION_DIR_ENV)
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "share"))
    monkeypatch.setattr(eh, "_repo_extension_dir", lambda: tmp_path / "usr/lib/python3.14/extension")
    folder = packaged(tmp_path / "share")
    assert eh.extension_dir() == folder


def test_the_host_installer_reads_the_searched_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(eh.EXTENSION_DIR_ENV, str(packaged(tmp_path / "share", key=OTHER_KEY)))
    assert nhi.extension_id_from_manifest() == other_id() != repo_id()


def test_root_is_never_handed_a_crx_from_a_searched_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Security (review 2026-11-24): an XDG_DATA_DIRS entry can be user-writable (Flatpak exports in ~/.local/share).
    The .crx root pre-installs for every user comes only from CRX_TARGET or the checkout, never from the search."""
    monkeypatch.delenv(eh.EXTENSION_DIR_ENV)
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "user-writable"))
    planted = packaged(tmp_path / "user-writable") / "dist" / "scribesense.crx"
    planted.parent.mkdir()
    planted.write_bytes(b"attacker crx")
    checkout = tmp_path / "checkout" / "extension"
    monkeypatch.setattr(setup, "_repo_extension_dir", lambda: checkout)
    monkeypatch.setattr(setup.root_helper, "crx_info", lambda data: ("id", "9.9.9"))
    assert eh.extension_dir() == planted.parent.parent  # the manifest search does find the planted copy…
    assert not setup.root_helper.CRX_TARGET.exists()
    assert setup.find_crx() == (None, None)  # …but its .crx is never offered to root
    (checkout / "dist").mkdir(parents=True)
    (checkout / "dist" / "scribesense.crx").write_bytes(b"dev build")
    assert setup.find_crx() == (checkout / "dist" / "scribesense.crx", "9.9.9")


def test_relative_data_dirs_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """XDG: relative entries are invalid — never resolved against the current folder (extension AND fonts)."""
    from scribesense.config import bundled

    monkeypatch.delenv(eh.EXTENSION_DIR_ENV)
    monkeypatch.delenv(bundled.BUNDLED_FONTS_ENV)
    monkeypatch.setenv("XDG_DATA_DIRS", f"share:{tmp_path / 'abs'}")
    monkeypatch.chdir(tmp_path)
    packaged(tmp_path / "share", key=OTHER_KEY)
    assert eh.extension_candidates() == (tmp_path / "abs/scribesense/extension", REPO_EXTENSION)
    assert eh.extension_dir() == REPO_EXTENSION
    assert all(c.is_absolute() for c in bundled.candidate_roots())
