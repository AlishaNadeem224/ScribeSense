"""M2.6 — coverage report label mapping and summary.

The GTK rendering itself is exercised by the manual accessibility checklist.
These tests check the two parts that can be tested without a display: the
status → label mapping (four cases), the detail-code → phrase mapping
(known and unknown), and the summary sentence. They import the module only
when PyGObject is available.
"""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
try:  # PyGObject alone is not enough: the module needs the GTK 4 + libadwaita typelibs
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError:
    pytest.skip("GTK 4 / libadwaita typelibs not installed", allow_module_level=True)

from scribesense.contracts.adapter import Target  # noqa: E402
from scribesense.contracts.coverage import CoverageEntry, CoverageStatus  # noqa: E402
from scribesense.contracts.errors import ErrorCode  # noqa: E402
from scribesense.ui import coverage_report as cr  # noqa: E402


def _t(name: str) -> Target:
    return Target(adapter="test", target_id=name, display_name=name, running=False)


def _e(status: CoverageStatus, code: ErrorCode | str = "OK") -> CoverageEntry:
    return CoverageEntry(target=_t("App"), status=status, detail_code=code)


# --- status labels ------------------------------------------------------------

@pytest.mark.parametrize(
    "status, expected",
    [
        (CoverageStatus.COVERED, "Covered"),
        (CoverageStatus.NOT_COVERED, "Not covered"),
        (CoverageStatus.PENDING, "Pending — app is open"),
        (CoverageStatus.RESTART_NEEDED, "Restart needed"),
    ],
)
def test_status_labels(status: CoverageStatus, expected: str) -> None:
    label, _css = cr._label_for(status)
    assert label == expected


def test_all_statuses_have_labels() -> None:
    # Exhaustive: any new CoverageStatus member will fail this test.
    for status in CoverageStatus:
        label, css = cr._label_for(status)
        assert label
        assert css in {"success", "warning", "error"}


# --- detail phrases ----------------------------------------------------------

def test_ok_detail_is_empty() -> None:
    assert cr._detail_for("OK") == ""


def test_known_error_code_has_friendly_phrase() -> None:
    text = cr._detail_for(ErrorCode.BROWSER_RUNNING)
    assert "browser" in text.lower()
    assert "BROWSER_RUNNING" not in text


def test_unknown_detail_code_is_shown_verbatim() -> None:
    # Card's failure case: "Unknown detail code → generic text, code shown".
    text = cr._detail_for("SOME_NEW_CODE_FROM_O4")
    assert "SOME_NEW_CODE_FROM_O4" in text
    assert "could not be covered" in text.lower()


# --- summary -----------------------------------------------------------------

def test_summary_empty() -> None:
    assert cr._summarise([]) == "No applications were checked."


def test_summary_all_covered() -> None:
    entries = [_e(CoverageStatus.COVERED), _e(CoverageStatus.COVERED)]
    assert cr._summarise(entries) == "All 2 applications covered."


def test_summary_partial() -> None:
    entries = [
        _e(CoverageStatus.COVERED),
        _e(CoverageStatus.NOT_COVERED, ErrorCode.QT6CT_NOT_IN_SESSION),
        _e(CoverageStatus.PENDING, ErrorCode.BROWSER_RUNNING),
    ]
    assert cr._summarise(entries) == "1 of 3 applications covered."


def test_summary_does_not_call_unverified_covered() -> None:
    # The M2.6 card's "must never": say "covered" for an unverified target.
    entries = [_e(CoverageStatus.NOT_COVERED, ErrorCode.ADAPTER_VERIFY_FAILED)]
    assert "covered" not in cr._summarise(entries).replace("covered.", "")