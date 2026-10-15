"""
O3 — `scribesense doctor`: names missing dependencies with install commands (M3.7, K10).

Rules (A2 AGENTS.md 7):
    - NEVER import Gtk, Adw, scribesense.ui or scribesense.reader.
    - Distinguish REQUIRED vs OPTIONAL dependencies.
    - Never install anything itself (read-only inspection).
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from dataclasses import dataclass
from typing import Literal

from scribesense.contracts.cli import ExitCode

Category = Literal["required", "optional"]


@dataclass(frozen=True)
class CheckResult:
    name: str
    category: Category
    present: bool
    details: str
    install_hint: str | None = None


def check_python_version() -> CheckResult:
    present = sys.version_info >= (3, 11)
    details = f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    hint = "Upgrade Python to >= 3.11 (sudo dnf install python3.11 or later)" if not present else None
    return CheckResult("python", "required", present, details, hint)

def check_pygobject() -> CheckResult:
    present = importlib.util.find_spec("gi") is not None
    details = "PyGObject (gi) installed" if present else "PyGObject (gi) missing"
    hint = "sudo dnf install python3-gobject" if not present else None
    return CheckResult("pygobject", "required", present, details, hint)

def check_fonttools() -> CheckResult:
    present = importlib.util.find_spec("fontTools") is not None
    details = "fontTools installed" if present else "fontTools missing"
    hint = "uv sync ; or: pip install fonttools" if not present else None
    return CheckResult("fonttools", "required", present, details, hint)

def check_pango_view() -> CheckResult:
    present = shutil.which("pango-view") is not None
    details = "pango-view found" if present else "pango-view missing"
    hint = "sudo dnf install pango" if not present else None
    return CheckResult("pango-view", "required", present, details, hint)

def _pangocairo_importable() -> bool:
    """The Pango drawn-font probe (M4.13) needs PangoCairo and the cairo typelib through GObject introspection."""
    try:
        import gi

        gi.require_version("Pango", "1.0")
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import PangoCairo  # noqa: F401
        return True
    except Exception:
        return False

def check_pangocairo() -> CheckResult:
    if _pangocairo_importable():
        return CheckResult("pangocairo", "required", True, "PangoCairo (with cairo typelib) importable")
    return CheckResult("pangocairo", "required", False, "PangoCairo / cairo typelib not importable (drawn-font check cannot run)",
                       "sudo dnf install gobject-introspection pango")

def check_fontconfig() -> CheckResult:
    missing = [t for t in ("fc-cache", "fc-match") if shutil.which(t) is None]
    if missing:
        return CheckResult("fontconfig", "required", False, f"{', '.join(missing)} missing", "sudo dnf install fontconfig")
    return CheckResult("fontconfig", "required", True, "fc-cache and fc-match found")

def _interface_schema_installed() -> bool:
    try:
        from gi.repository import Gio
        source = Gio.SettingsSchemaSource.get_default()
        return source is not None and source.lookup("org.gnome.desktop.interface", False) is not None
    except Exception:
        return False

def check_glib_schemas() -> CheckResult:
    if shutil.which("gsettings") is None:
        return CheckResult("gsettings", "required", False, "gsettings missing", "sudo dnf install glib2")
    if not _interface_schema_installed():
        return CheckResult("gsettings-desktop-schemas", "required", False, "org.gnome.desktop.interface schema missing", "sudo dnf install gsettings-desktop-schemas")
    return CheckResult("gsettings", "required", True, "gsettings and org.gnome.desktop.interface found")

def check_qt6ct() -> CheckResult:
    present = shutil.which("qt6ct") is not None
    details = "qt6ct found" if present else "qt6ct not found (Qt apps will not be styled by name)"
    hint = "sudo dnf install qt6ct" if not present else None
    return CheckResult("qt6ct", "optional", present, details, hint)

def check_flatpak() -> CheckResult:
    present = shutil.which("flatpak") is not None
    details = "flatpak found" if present else "flatpak not found (Flatpak overrides disabled)"
    hint = "sudo dnf install flatpak" if not present else None
    return CheckResult("flatpak", "optional", present, details, hint)

def check_hyprctl() -> CheckResult:
    present = shutil.which("hyprctl") is not None
    details = "hyprctl found" if present else "hyprctl not found (not in a running Hyprland session)"
    hint = "Hyprland session needed for live desktop reload" if not present else None
    return CheckResult("hyprctl", "optional", present, details, hint)

def check_lua() -> CheckResult:
    present = shutil.which("lua") is not None or shutil.which("luajit") is not None
    details = "Lua interpreter found" if present else "Lua interpreter not found (Hyprland Lua config syntax checks skipped)"
    hint = "sudo dnf install lua" if not present else None
    return CheckResult("lua", "optional", present, details, hint)

def run_checks() -> list[CheckResult]:
    return [
        check_python_version(),
        check_pygobject(),
        check_fonttools(),
        check_pango_view(),
        check_fontconfig(),
        check_pangocairo(),
        check_glib_schemas(),
        check_qt6ct(),
        check_flatpak(),
        check_hyprctl(),
        check_lua(),
    ]

def run_doctor() -> ExitCode:
    """Run diagnostics and print formatted report to stdout."""
    checks = run_checks()
    required = [c for c in checks if c.category == "required"]
    optional = [c for c in checks if c.category == "optional"]

    all_req_passed = all(c.present for c in required)

    print("=== ScribeSense Doctor ===")
    print("\n[Required Components]")
    for c in required:
        status = "OK" if c.present else "MISSING"
        print(f" {status:9} {c.name}: {c.details}")
        if not c.present and c.install_hint:
            print(f"    Fix: {c.install_hint}")

    print("\n[Optional Components]")
    for c in optional:
        status = "OK" if c.present else "OPTIONAL"
        print(f" {status:9} {c.name}: {c.details}")
        if not c.present and c.install_hint:
            print(f"    Fix: {c.install_hint}")

    if all_req_passed:
        print("\nResult: All required components are present.")
        return ExitCode.OK
    else:
        print("\nResult: Some required components are missing. Run the fix commands above.")
        return ExitCode.FAILED
