"""Automatic setup and repair (safety/setup.py) and the root helper (safety/root_helper.py), 2026-11-22.

Unit sandbox only: the "system" folders (/opt, /usr/share) are re-pointed into tmp_path, the .crx is built here, and
pkexec is replaced — no password prompt, no real browser folder, no real system file is ever touched.
"""

from __future__ import annotations

import base64
import io
import json
import os
import stat
import struct
import zipfile
from pathlib import Path

import pytest

from scribesense.adapters.chromium import BROWSERS, user_data_dir
from scribesense.adapters.fake import FakeAdapter, FakeWorld
from scribesense.config.presets import BUILTINS
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import FontBuildResult
from scribesense.controller.service import Service
from scribesense.safety import native_host_install as nhi
from scribesense.safety import root_helper as rh
from scribesense.safety import setup
from scribesense.safety.testing import fake_fontset

KEY_DER = bytes.fromhex("30820122300d06092a864886f70d01010105000382010f00") + bytes(range(256)) + b"\x02\x03\x01\x00\x01"
KEY_B64 = base64.b64encode(KEY_DER).decode()


def crx_bytes(version: str = "0.1.2", key: str | None = KEY_B64) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        manifest = {"manifest_version": 3, "name": "ScribeSense", "version": version}
        if key is not None:
            manifest["key"] = key
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("background.js", "// test")
    header = b"\x12\x00signed-header-not-checked-here"
    return b"Cr24" + struct.pack("<II", 3, len(header)) + header + buf.getvalue()


EXT_ID = rh.crx_info(crx_bytes())[0]


@pytest.fixture
def system(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake system root: Brave installed, Chrome not; CRX_TARGET inside it."""
    root = tmp_path / "sys"
    (root / "opt/brave.com/brave").mkdir(parents=True)
    folders = {
        "brave": ((str(root / "opt/brave.com/brave"),), (str(root / "opt/brave.com/brave/extensions"),
                                                          str(root / "usr/share/brave/extensions"))),
        "chrome": ((str(root / "opt/google/chrome"),), (str(root / "opt/google/chrome/extensions"),)),
    }
    monkeypatch.setattr(rh, "BROWSER_FOLDERS", folders)
    monkeypatch.setattr(rh, "CRX_TARGET", root / "usr/share/scribesense/scribesense.crx")
    monkeypatch.setattr(setup, "DEV_CRX", tmp_path / "dist" / "scribesense.crx")
    monkeypatch.setattr(setup, "extension_id_from_manifest", lambda: EXT_ID)
    monkeypatch.setattr(nhi, "extension_id_from_manifest", lambda manifest_path=None: EXT_ID)
    monkeypatch.setattr(setup, "polkit_agent_running", lambda: True)
    return root


def dev_crx(version: str = "0.1.2") -> Path:
    setup.DEV_CRX.parent.mkdir(parents=True, exist_ok=True)
    setup.DEV_CRX.write_bytes(crx_bytes(version))
    return setup.DEV_CRX


def brave_user_dir() -> Path:
    d = user_data_dir(next(b for b in BROWSERS if b.key == "brave"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def items(report: setup.SetupReport) -> list[tuple[str, str, str]]:
    return [(i.kind, i.browser, i.status) for i in report.items]


# ---------------------------------------------------------------- root helper

def test_crx_info_reads_id_and_version() -> None:
    assert rh.crx_info(crx_bytes("1.2.3")) == (EXT_ID, "1.2.3")
    assert len(EXT_ID) == 32 and set(EXT_ID) <= set("abcdefghijklmnop")


@pytest.mark.parametrize("data", [
    b"PK\x03\x04 a zip, not a crx", b"Cr24" + struct.pack("<II", 2, 0), crx_bytes(key=None), crx_bytes(version="1.0 beta"),
    b"Cr24" + struct.pack("<II", 3, 999999),
])
def test_crx_info_refuses_anything_else(data: bytes) -> None:
    with pytest.raises(rh.HelperError):
        rh.crx_info(data)


def test_install_copies_the_crx_and_points_only_installed_browsers(system: Path, tmp_path: Path) -> None:
    src = tmp_path / "in.crx"
    src.write_bytes(crx_bytes("0.1.2"))
    assert rh.install(src) == ["brave"]
    assert rh.CRX_TARGET.read_bytes() == src.read_bytes()
    for folder in rh.BROWSER_FOLDERS["brave"][1]:
        pointer = Path(folder) / f"{EXT_ID}.json"
        assert json.loads(pointer.read_text()) == {"external_crx": str(rh.CRX_TARGET), "external_version": "0.1.2"}
        assert stat.S_IMODE(pointer.stat().st_mode) == 0o644
    assert not Path(rh.BROWSER_FOLDERS["chrome"][1][0]).exists()  # Chrome is not installed here


def test_remove_deletes_only_our_pointers_and_the_crx(system: Path, tmp_path: Path) -> None:
    src = tmp_path / "in.crx"
    src.write_bytes(crx_bytes())
    rh.install(src)
    folder = Path(rh.BROWSER_FOLDERS["brave"][1][0])
    foreign = folder / ("a" * 32 + ".json")
    foreign.write_text(json.dumps({"external_crx": "/opt/someone/else.crx", "external_version": "1"}))
    removed = rh.remove()
    assert str(rh.CRX_TARGET) in removed and not rh.CRX_TARGET.exists()
    assert not (folder / f"{EXT_ID}.json").exists()
    assert foreign.exists()  # another program's file is never touched


def test_main_refuses_bad_usage_and_non_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert rh.main(["root_helper.py"]) == 2
    assert rh.main(["root_helper.py", "install"]) == 2
    assert rh.main(["root_helper.py", "rm", "-rf", "/"]) == 2
    if os.geteuid() != 0:
        assert rh.main(["root_helper.py", "remove"]) == 2
        assert "must run as root" in capsys.readouterr().err


# ---------------------------------------------------------------- check and per-user repair

def test_check_finds_a_browser_missing_its_host_and_extension(sandbox, system: Path) -> None:  # type: ignore[no-untyped-def]
    brave_user_dir()
    dev_crx()
    report = setup.check()
    assert ("host", "brave", "missing") in items(report)
    assert ("extension", "brave", "missing") in items(report)
    assert ("agent", "", "ok") in items(report)
    assert report.crx == setup.DEV_CRX and report.crx_version == "0.1.2"


def test_repair_user_installs_the_host_silently_and_check_then_says_ok(sandbox, system: Path) -> None:  # type: ignore[no-untyped-def]
    brave_user_dir()
    assert setup.repair_user(setup.check()) == ["brave"]
    assert ("host", "brave", "ok") in items(setup.check())


def test_a_launcher_whose_python_is_gone_counts_as_missing_and_is_rewritten(sandbox, system: Path) -> None:  # type: ignore[no-untyped-def]
    brave_user_dir()
    setup.repair_user(setup.check())
    nhi.launcher_path().write_text("#!/bin/sh\nexec '/gone/venv/bin/python3' -m scribesense.safety.native_host \"$@\"\n")
    assert ("host", "brave", "missing") in items(setup.check())  # e.g. after the venv was rebuilt elsewhere
    setup.repair_user(setup.check())
    assert ("host", "brave", "ok") in items(setup.check())


def test_extension_status_ok_and_outdated(sandbox, system: Path, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    brave_user_dir()
    old = tmp_path / "old.crx"
    old.write_bytes(crx_bytes("0.1.1"))
    rh.install(old)
    dev_crx("0.1.1")
    rh.CRX_TARGET.unlink()  # the installed copy is gone; the dev build is the same version
    assert ("extension", "brave", "missing") in items(setup.check())
    rh.install(old)
    assert ("extension", "brave", "ok") in items(setup.check())
    rh.CRX_TARGET.write_bytes(crx_bytes("0.1.2"))  # a newer build is available -> the pointers are outdated
    assert ("extension", "brave", "outdated") in items(setup.check())


def test_flatpak_browsers_are_unsupported_and_no_crx_means_unavailable(sandbox, system: Path) -> None:  # type: ignore[no-untyped-def]
    flatpak = next(b for b in BROWSERS if b.key == "brave-flatpak")
    user_data_dir(flatpak).mkdir(parents=True)
    brave_user_dir()
    report = setup.check()
    assert ("host", "brave-flatpak", "unsupported") in items(report)
    assert ("extension", "brave", "unavailable") in items(report)  # no signed .crx built yet
    assert not setup.root_needed(report)


def test_firefox_only_machines_need_nothing(sandbox, system: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(rh, "BROWSER_FOLDERS", {})
    assert setup.check().items == []


# ---------------------------------------------------------------- the one root prompt

def test_repair_root_runs_one_pkexec_with_fixed_arguments(sandbox, system: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    brave_user_dir()
    crx = dev_crx()
    calls: list[list[str]] = []
    monkeypatch.setattr(setup, "run_cmd", lambda argv, timeout: calls.append(argv))
    assert setup.repair_root(setup.check()) is True
    assert calls == [["pkexec", "/usr/bin/python3", "-I", str(setup.ROOT_HELPER), "install", str(crx)]]


def test_a_declined_prompt_is_not_repeated_until_a_new_version(sandbox, system: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    brave_user_dir()
    dev_crx("0.1.2")
    calls: list[list[str]] = []

    def cancelled(argv: list[str], timeout: float) -> None:
        calls.append(argv)
        raise ScribeSenseError(ErrorCode.ADAPTER_COMMAND_FAILED)  # pkexec: dismissed / not authorised
    monkeypatch.setattr(setup, "run_cmd", cancelled)
    assert setup.repair_root(setup.check()) is False
    assert setup.repair_root(setup.check()) is False
    assert len(calls) == 1  # asked once
    dev_crx("0.1.3")
    setup.repair_root(setup.check())
    assert len(calls) == 2  # a new version asks again


def test_no_polkit_agent_means_no_prompt_and_the_agent_is_reported(sandbox, system: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    brave_user_dir()
    dev_crx()
    monkeypatch.setattr(setup, "polkit_agent_running", lambda: False)
    monkeypatch.setattr(setup, "run_cmd", lambda *a, **k: pytest.fail("no prompt without an agent"))
    report = setup.check()
    assert ("agent", "", "missing") in items(report)
    assert setup.repair_root(report) is False


def test_ensure_never_raises(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(setup, "check", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert setup.ensure(background=False).items == []


# ---------------------------------------------------------------- the service hook

@pytest.fixture(autouse=True)
def short_runtime_dir(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    runtime = sandbox.data_dir.parent / "run"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))


def test_the_service_runs_setup_on_open_and_after_every_keep_and_survives_errors(sandbox) -> None:  # type: ignore[no-untyped-def]
    calls: list[int] = []

    def flaky() -> None:
        calls.append(1)
        raise RuntimeError("setup broke")
    world = FakeWorld()
    world.set_file("/fake/fc.conf", b"orig")
    svc = Service.open(adapters=[FakeAdapter(world, {"fc": ["/fake/fc.conf"]}, name="fontconfig")], setup=flaky)
    try:
        assert calls == [1]
        svc.controller._generator = lambda c: FontBuildResult("built", fake_fontset(c), None)
        svc.keep(svc.apply(BUILTINS[0]).tx)
        assert calls == [1, 1]  # a font change re-checks the browsers
    finally:
        svc.close()


def test_without_a_setup_hook_the_service_never_runs_it(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(setup, "ensure", lambda **k: pytest.fail("tests must never run the real setup"))
    world = FakeWorld()
    world.set_file("/fake/fc.conf", b"orig")
    Service.open(adapters=[FakeAdapter(world, {"fc": ["/fake/fc.conf"]}, name="fontconfig")]).close()



def test_a_leftover_settings_folder_of_an_uninstalled_browser_is_ignored(sandbox, system: Path) -> None:  # type: ignore[no-untyped-def]
    chromium = next(b for b in BROWSERS if b.key == "chromium")
    user_data_dir(chromium).mkdir(parents=True)  # e.g. ~/.config/chromium left behind; Chromium not installed
    assert [i for i in setup.check().items if i.browser == "chromium"] == []


def test_after_a_successful_prompt_it_never_asks_again_for_that_version(sandbox, system: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    brave_user_dir()
    dev_crx("0.1.2")
    calls: list[list[str]] = []
    monkeypatch.setattr(setup, "run_cmd", lambda argv, timeout: calls.append(argv))  # "succeeds" but writes nothing
    assert setup.repair_root(setup.check()) is True
    assert setup.repair_root(setup.check()) is False  # still "missing" (here), but this version was asked: no loop
    assert len(calls) == 1
