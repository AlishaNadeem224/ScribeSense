"""M2.2 — restart_list filter and message.

Pure-Python tests. No GTK import at module level except via importorskip;
the parts that need GTK (build_restart_banner) are only exercised when
PyGObject is available. That keeps this file runnable in the CI sandbox
where PyGObject is not installed (the whole file is skipped there).
"""

from __future__ import annotations

import pytest

pytest.importorskip("gi")

from scribesense.contracts.adapter import Target  # noqa: E402
from scribesense.contracts.coverage import CoverageEntry, CoverageStatus  # noqa: E402
from scribesense.contracts.errors import ErrorCode  # noqa: E402
from scribesense.ui import restart_notice as rn  # noqa: E402


def _t(name: str, running: bool) -> Target:
    return Target(adapter="test", target_id=name, display_name=name, running=running)


def _e(
    target: Target, status: CoverageStatus, code: ErrorCode | str = "OK"
) -> CoverageEntry:
    return CoverageEntry(target=target, status=status, detail_code=code)


# --- restart_list -------------------------------------------------------------

def test_restart_list_empty_when_nothing_needs_restart() -> None:
    entries = [
        _e(_t("System fonts", False), CoverageStatus.COVERED),
        _e(_t("GTK", False), CoverageStatus.COVERED),
    ]
    assert rn.restart_list(entries) == []


def test_restart_list_returns_only_running_restart_needed() -> None:
    entries = [
        _e(_t("System fonts", False), CoverageStatus.COVERED),
        _e(_t("Zen", True), CoverageStatus.RESTART_NEEDED),
        _e(_t("LibreOffice", True), CoverageStatus.RESTART_NEEDED),
        _e(_t("Qt", False), CoverageStatus.NOT_COVERED, ErrorCode.QT6CT_NOT_IN_SESSION),
        _e(_t("Brave", True), CoverageStatus.PENDING, ErrorCode.BROWSER_RUNNING),
    ]
    result = rn.restart_list(entries)
    names = [t.display_name for t in result]
    assert names == ["Zen", "LibreOffice"]


def test_restart_list_drops_not_running_restart_needed() -> None:
    # A RESTART_NEEDED target that is not running should not be listed:
    # the next launch picks up the change anyway.
    entries = [_e(_t("Not running", False), CoverageStatus.RESTART_NEEDED)]
    assert rn.restart_list(entries) == []


def test_restart_list_deduplicates() -> None:
    t = _t("Zen", True)
    entries = [
        _e(t, CoverageStatus.RESTART_NEEDED),
        _e(t, CoverageStatus.RESTART_NEEDED),
    ]
    assert len(rn.restart_list(entries)) == 1


# --- restart_message ----------------------------------------------------------

def test_message_empty_for_no_targets() -> None:
    assert rn.restart_message([]) == ""


def test_message_single_target() -> None:
    msg = rn.restart_message([_t("Zen", True)])
    assert msg == "Restart application to see the change: Zen"


def test_message_two_targets_uses_and() -> None:
    msg = rn.restart_message([_t("Zen", True), _t("LibreOffice", True)])
    assert "Zen and LibreOffice" in msg
    assert "applications" in msg


def test_message_three_targets_uses_oxford_comma() -> None:
    msg = rn.restart_message([_t("A", True), _t("B", True), _t("C", True)])
    assert "A, B, and C" in msg


# --- banner -------------------------------------------------------------------

def test_banner_is_none_when_nothing_to_restart() -> None:
    assert rn.build_restart_banner([]) is None


def test_banner_builds_for_non_empty_list() -> None:
    widget = rn.build_restart_banner([_t("Zen", True)])
    assert widget is not None