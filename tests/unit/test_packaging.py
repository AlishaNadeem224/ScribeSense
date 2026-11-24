"""W2 Fedora packaging (2026-11-24): the spec, desktop / metainfo files and crx_pointer.py agree with the code, and a
packaged install never asks for root or deletes the package's own files. Static checks only (no rpmbuild here)."""

from __future__ import annotations

import configparser
import importlib.util
import io
import json
import re
import tomllib
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from scribesense import uninstall as un
from scribesense.adapters import extension_host
from scribesense.safety import root_helper as rh
from scribesense.safety import setup
from scribesense.ui import APP_ID
from scribesense.ui.setup_notice import setup_notice

REPO = Path(__file__).resolve().parents[2]
PACKAGING = REPO / "packaging"
SPEC = (PACKAGING / "scribesense.spec").read_text()


def _crx_pointer():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("crx_pointer", PACKAGING / "crx_pointer.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pristine_root_helper():  # type: ignore[no-untyped-def]
    """A fresh copy of root_helper.py: the sandbox redirects the imported one's system paths (tests/conftest.py)."""
    spec = importlib.util.spec_from_file_location("pristine_root_helper",
                                                  REPO / "src" / "scribesense" / "safety" / "root_helper.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _real_crx(tmp_path: Path, version: str = "0.1.2") -> Path:
    """A CRX3 file carrying the REAL manifest key (so its ID is the pinned one)."""
    from tests.unit.test_setup import crx_bytes

    key = json.loads((REPO / "extension" / "manifest.json").read_text())["key"]
    path = tmp_path / "scribesense.crx"
    path.write_bytes(crx_bytes(version, key=key))
    return path


# ---------------------------------------------------------------- the spec agrees with the code

def test_spec_version_matches_pyproject() -> None:
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    assert re.search(rf"^Version:\s+{re.escape(version)}$", SPEC, re.M)


def test_spec_paths_are_the_ones_the_code_uses() -> None:
    rh = _pristine_root_helper()
    assert str(rh.CRX_TARGET) == "/usr/share/scribesense/scribesense.crx"  # %{_datadir}/%{name}/scribesense.crx
    assert str(rh.PACKAGED_MARKER) == "/usr/share/scribesense/packaged"
    assert "%{buildroot}%{_datadir}/%{name}/scribesense.crx" in SPEC
    assert "%{buildroot}%{_datadir}/%{name}/packaged" in SPEC
    for folder in ("chromium", "google-chrome"):  # pointer folders the browsers read (see root_helper docstring)
        assert f"%{{_datadir}}/{folder}/extensions/*.json" in SPEC


def test_brave_also_reads_the_chromium_folder_the_package_fills() -> None:
    """The package's /usr/share/chromium/extensions pointer must count for Brave too (setup's status check)."""
    rh = _pristine_root_helper()
    assert "/usr/share/chromium/extensions" in rh.BROWSER_FOLDERS["brave"][1]
    assert "/usr/share/google-chrome/extensions" in rh.BROWSER_FOLDERS["chrome"][1]


def test_every_shipped_extension_file_exists_and_nothing_private_ships() -> None:
    line = re.search(r"install -p -m 0644 (extension/\S+(?: extension/\S+)*)", SPEC)
    assert line is not None
    shipped = line.group(1).split()
    assert shipped == ["extension/manifest.json", "extension/background.js", "extension/popup.html",
                       "extension/popup.js"]
    assert all((REPO / f).is_file() for f in shipped)


def test_every_bundled_family_has_a_licence_file_for_the_spec_loop() -> None:
    families = [d for d in (REPO / "fonts").iterdir() if d.is_dir() and d.name != "tools"]
    assert {d.name for d in families} == {"Noto Sans", "Atkinson Hyperlegible", "Carlito", "Lexend"}
    assert all((d / "OFL.txt").is_file() and list(d.glob("*.ttf")) for d in families)
    assert (REPO / "fonts" / "manifest.toml").is_file()


def test_spec_requires_what_doctor_checks() -> None:
    requires = set(re.findall(r"^Requires:\s+(\S+)", SPEC, re.M))
    assert {"python3-gobject", "gtk4", "libadwaita", "pango", "gobject-introspection", "fontconfig", "glib2",
            "gsettings-desktop-schemas", "wl-clipboard"} <= requires
    assert "hyprland" not in requires  # COPR-only on Fedora: a weak dependency
    assert re.search(r"^Suggests:\s+hyprland$", SPEC, re.M)
    # review 2026-11-24: the package never runs pkexec, and a weak dep on a polkit agent can pull in a desktop shell
    assert "polkit" not in requires and "PolicyKit-authentication-agent" not in SPEC.split("%description")[0]


def test_every_build_carries_the_packaged_marker_even_without_the_crx() -> None:
    install = SPEC.split("%install")[1].split("%check")[0]
    crx_block = install.split("%if %{with crx}")[1].split("%endif")[0]
    assert "%{_datadir}/%{name}/packaged" in install and "%{_datadir}/%{name}/packaged" not in crx_block


def test_spec_license_is_marked_todo_until_the_team_decides() -> None:
    assert "TODO(license)" in SPEC and re.search(r"^License:\s+LicenseRef-TODO AND OFL-1.1 AND OFL-1.1-RFN$", SPEC, re.M)


# ---------------------------------------------------------------- desktop file and metainfo

def test_desktop_file_matches_the_app_id_and_has_restore_original() -> None:
    path = PACKAGING / f"{APP_ID}.desktop"
    desktop = configparser.ConfigParser(interpolation=None)
    desktop.optionxform = str  # type: ignore[assignment,method-assign]
    desktop.read(path)
    entry = desktop["Desktop Entry"]
    assert entry["Exec"] == "scribesense" and entry["Icon"] == APP_ID and entry["Actions"] == "restore-original;"
    assert desktop["Desktop Action restore-original"]["Exec"] == "scribesense reset"  # K10 recovery command
    assert (PACKAGING / f"{APP_ID}.svg").is_file()


def test_metainfo_id_and_launchable_match() -> None:
    root = ET.parse(PACKAGING / f"{APP_ID}.metainfo.xml").getroot()
    assert root.findtext("id") == APP_ID
    assert root.find("launchable").text == f"{APP_ID}.desktop"  # type: ignore[union-attr]
    assert "Restore original" in ET.tostring(root, encoding="unicode")  # the removal rule is in the description


# ---------------------------------------------------------------- crx_pointer.py (build-time)

def test_crx_pointer_prints_the_pinned_id_and_the_root_helpers_pointer(tmp_path: Path) -> None:
    tool = _crx_pointer()
    crx = _real_crx(tmp_path)
    out = io.StringIO()
    with redirect_stdout(out):
        assert tool.main(["crx_pointer.py", "id", str(crx)]) == 0
    assert out.getvalue().strip() == extension_host.extension_id_from_manifest(REPO / "extension" / "manifest.json")
    out = io.StringIO()
    with redirect_stdout(out):
        assert tool.main(["crx_pointer.py", "pointer", str(crx)]) == 0
    assert json.loads(out.getvalue()) == {"external_crx": "/usr/share/scribesense/scribesense.crx",
                                          "external_version": "0.1.2"}


def test_crx_pointer_refuses_a_bad_file_and_bad_usage(tmp_path: Path) -> None:
    tool = _crx_pointer()
    bad = tmp_path / "bad.crx"
    bad.write_bytes(b"not a crx")
    assert tool.main(["crx_pointer.py", "id", str(bad)]) == 1
    assert tool.main(["crx_pointer.py", "id"]) == 2


# ---------------------------------------------------------------- packaged mode (the package owns the extension)

@pytest.fixture
def packaged_system(sandbox, tmp_path: Path) -> Path:  # type: ignore[no-untyped-def]
    """The RPM's files in the sandbox's system folders: .crx, marker, pointer in /usr/share/chromium/extensions."""
    crx = _real_crx(tmp_path)
    rh.CRX_TARGET.parent.mkdir(parents=True, exist_ok=True)
    rh.CRX_TARGET.write_bytes(crx.read_bytes())
    rh.PACKAGED_MARKER.write_text("packaged\n")
    ext_id, version = rh.crx_info(crx.read_bytes())
    chromium = Path(rh.BROWSER_FOLDERS["brave"][1][2])  # the redirected /usr/share/chromium/extensions
    chromium.mkdir(parents=True, exist_ok=True)
    (chromium / f"{ext_id}.json").write_bytes(rh.pointer(version))
    Path(rh.BROWSER_FOLDERS["brave"][0][0]).mkdir(parents=True, exist_ok=True)  # Brave is installed
    return chromium / f"{ext_id}.json"


def test_root_helper_never_touches_the_package_files(packaged_system: Path) -> None:
    assert rh.main(["root_helper.py", "install", str(rh.CRX_TARGET)]) == 3  # refused outright when packaged
    assert rh.remove(keep_package=True) == []  # what `remove` does as root on a packaged install
    assert packaged_system.exists() and rh.CRX_TARGET.exists()


def _dev_leftover(version: str = "0.1.1") -> Path:
    """A pointer an earlier development root step left in Brave's /opt folder (not a package folder)."""
    ext_id = rh.crx_info(rh.CRX_TARGET.read_bytes())[0]
    folder = Path(rh.BROWSER_FOLDERS["brave"][1][0])
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{ext_id}.json"
    path.write_bytes(rh.pointer(version))
    return path


def test_packaged_remove_cleans_only_development_leftovers(packaged_system: Path) -> None:
    leftover = _dev_leftover()
    assert rh.our_pointers(outside_package=True) == [leftover]
    assert rh.remove(keep_package=True) == [str(leftover)]
    assert not leftover.exists() and packaged_system.exists() and rh.CRX_TARGET.exists()


def test_leftovers_are_offered_for_cleanup_never_cleaned_unasked(packaged_system: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    _dev_leftover()
    monkeypatch.setattr(setup, "polkit_agent_running", lambda: True)
    calls: list[list[str]] = []
    monkeypatch.setattr(setup, "run_cmd", lambda argv, timeout: calls.append(argv))
    report = setup.ensure(background=False)
    assert calls == []  # packaged: no automatic prompt, ever
    notice = setup_notice(report, prompt_due=setup.root_needed(report))
    assert notice is not None and notice.action == "cleanup"
    assert setup.cleanup_stale(report) is True
    assert calls == [["pkexec", "/usr/bin/python3", "-I", str(setup.ROOT_HELPER), "remove"]]


def test_setup_sees_the_package_files_and_never_asks_for_root(packaged_system: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup, "polkit_agent_running", lambda: True)
    report = setup.check()
    assert report.packaged
    assert ("extension", "brave", "ok") in [(i.kind, i.browser, i.status) for i in report.items]
    report.items.append(setup.Item("extension", "chrome", "Chrome", "missing", needs_root=True))
    assert not setup.root_possible(report) and not setup.repair_root(report, user_asked=True)
    assert not setup.cleanup_stale(report)  # nothing left over: nothing to clean
    notice = setup_notice(report, prompt_due=setup.root_needed(report))
    assert notice is not None and notice.button is None and "dnf reinstall" in notice.text


def test_uninstall_leaves_the_package_files_to_dnf(packaged_system: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup, "run_cmd", lambda *a, **k: pytest.fail("no prompt: nothing left over"))
    assert setup.remove_extension() == "packaged"
    assert packaged_system.exists() and rh.CRX_TARGET.exists()
    report = un.UninstallReport()
    un._remove_extension(report, setup.remove_extension)
    assert report.extension == "packaged"
    assert "sudo dnf remove scribesense" in un.format_report(report)


def test_uninstall_on_a_packaged_install_also_cleans_leftovers(packaged_system: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    leftover = _dev_leftover()

    def pkexec_remove(argv: list[str], timeout: float) -> None:
        assert argv[-1] == "remove"
        rh.remove(keep_package=True)  # what root_helper does as root on a packaged install
    monkeypatch.setattr(setup, "run_cmd", pkexec_remove)
    assert setup.remove_extension() == "packaged"
    assert not leftover.exists() and packaged_system.exists() and rh.CRX_TARGET.exists()
