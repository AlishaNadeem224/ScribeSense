"""adapters/browser_lock.py — stale browser locks (2026-11-22). No process is started: flatpak ps is replaced."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from scribesense.adapters import browser_lock as bl


def lock(tmp_path: Path, target: str, name: str = "lock") -> Path:
    path = tmp_path / name
    os.symlink(target, path)
    return path


def test_no_lock_is_not_running(tmp_path: Path) -> None:
    assert bl.lock_is_live(tmp_path / "lock", "firefox") is False


def test_firefox_live_and_stale(tmp_path: Path) -> None:
    assert bl.lock_is_live(lock(tmp_path, f"127.0.0.1:+{os.getpid()}"), "firefox") is True
    assert bl.lock_is_live(lock(tmp_path, "127.0.1.1:+999999999", "l2"), "firefox") is False


def test_chromium_live_stale_and_other_host(tmp_path: Path) -> None:
    host = socket.gethostname()
    assert bl.lock_is_live(lock(tmp_path, f"{host}-{os.getpid()}", "a"), "chromium") is True
    assert bl.lock_is_live(lock(tmp_path, f"{host}-999999999", "b"), "chromium") is False
    assert bl.lock_is_live(lock(tmp_path, "another-machine-999999999", "c"), "chromium") is True


def test_flatpak_app_ids_are_found_in_paths() -> None:
    assert bl.flatpak_app_of(Path("/h/u/.var/app/app.zen_browser.zen/.zen/p/lock")) == "app.zen_browser.zen"
    assert bl.flatpak_app_of(Path("/h/u/.var/app/com.brave.Browser/config/BraveSoftware/x/SingletonLock")) == "com.brave.Browser"
    assert bl.flatpak_app_of(Path("/h/u/.mozilla/firefox/p/lock")) is None


def test_flatpak_lock_follows_flatpak_ps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / ".var" / "app" / "com.brave.Browser" / "config"
    root.mkdir(parents=True)
    path = lock(root, "fedora-2", "SingletonLock")
    monkeypatch.setattr(bl, "running_flatpak_apps", lambda: {"com.brave.Browser"})
    assert bl.lock_is_live(path, "chromium") is True
    monkeypatch.setattr(bl, "running_flatpak_apps", lambda: set())
    assert bl.lock_is_live(path, "chromium") is False
    monkeypatch.setattr(bl, "running_flatpak_apps", lambda: None)
    assert bl.lock_is_live(path, "chromium") is True


def test_flatpak_ps_failure_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise OSError("no flatpak")
    monkeypatch.setattr(bl, "run_cmd", boom)
    assert bl.running_flatpak_apps() is None
