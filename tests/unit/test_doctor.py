"""M3.7 Doctor — acceptance: each missing dependency is reported (in the sandbox, host-independent)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from scribesense import doctor
from scribesense.contracts.cli import ExitCode

OPTIONAL_TOOLS = ("qt6ct", "flatpak", "hyprctl", "lua")


def _which_without(*missing: str):  # type: ignore[no-untyped-def]
    return lambda name: None if name in missing else f"/usr/bin/{name}"


def _by_name(checks):  # type: ignore[no-untyped-def]
    return {c.name: c for c in checks}


def test_everything_present_returns_ok() -> None:
    with patch("scribesense.doctor.shutil.which", _which_without()), \
         patch("scribesense.doctor.importlib.util.find_spec", return_value=object()), \
         patch("scribesense.doctor._interface_schema_installed", return_value=True), \
         patch("scribesense.doctor._pangocairo_importable", return_value=True):
        assert doctor.run_doctor() == ExitCode.OK


@pytest.mark.parametrize("tool,check", [("pango-view", "pango-view"), ("gsettings", "gsettings"),
                                        ("fc-cache", "fontconfig"), ("fc-match", "fontconfig")])
def test_missing_required_tool_is_reported_with_a_dnf_hint(tool: str, check: str) -> None:
    with patch("scribesense.doctor.shutil.which", _which_without(tool)), \
         patch("scribesense.doctor._interface_schema_installed", return_value=True):
        result = _by_name(doctor.run_checks())[check]
    assert result.category == "required" and not result.present
    assert result.install_hint and result.install_hint.startswith("sudo dnf install ")


@pytest.mark.parametrize("module,check", [("gi", "pygobject"), ("fontTools", "fonttools")])
def test_missing_required_python_module_is_reported(module: str, check: str) -> None:
    real = doctor.importlib.util.find_spec
    with patch("scribesense.doctor.importlib.util.find_spec",
               lambda name, *a: None if name == module else real(name, *a)):
        result = _by_name(doctor.run_checks())[check]
    assert result.category == "required" and not result.present and result.install_hint


def test_missing_pangocairo_is_reported() -> None:
    with patch("scribesense.doctor._pangocairo_importable", return_value=False):
        result = _by_name(doctor.run_checks())["pangocairo"]
    assert result.category == "required" and not result.present
    assert "gobject-introspection" in (result.install_hint or "")


def test_missing_schema_is_reported() -> None:
    with patch("scribesense.doctor.shutil.which", _which_without()), \
         patch("scribesense.doctor._interface_schema_installed", return_value=False):
        result = _by_name(doctor.run_checks())["gsettings-desktop-schemas"]
    assert result.category == "required" and not result.present
    assert "gsettings-desktop-schemas" in (result.install_hint or "")


@pytest.mark.parametrize("tool", OPTIONAL_TOOLS)
def test_missing_optional_tool_does_not_fail_the_run(tool: str) -> None:
    missing = (tool, "luajit") if tool == "lua" else (tool,)
    with patch("scribesense.doctor.shutil.which", _which_without(*missing)), \
         patch("scribesense.doctor.importlib.util.find_spec", return_value=object()), \
         patch("scribesense.doctor._interface_schema_installed", return_value=True), \
         patch("scribesense.doctor._pangocairo_importable", return_value=True):
        result = _by_name(doctor.run_checks())[tool]
        code = doctor.run_doctor()
    assert result.category == "optional" and not result.present
    assert code == ExitCode.OK


def test_missing_required_returns_failed_and_prints_fix(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("scribesense.doctor.shutil.which", return_value=None):
        assert doctor.run_doctor() == ExitCode.FAILED
    out = capsys.readouterr().out
    assert "Fix: sudo dnf install" in out


def test_doctor_installs_nothing() -> None:
    # Unit tests block process launch; doctor must stay read-only and never need one.
    with patch("scribesense.doctor.shutil.which", return_value=None):
        doctor.run_doctor()
