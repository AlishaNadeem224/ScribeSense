"""M3.5 — the full-system tests refuse to run anywhere but the provisioned test VM (checked WITHOUT a VM)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vm import check_hyprland, guard


def test_dev_machine_is_refused() -> None:
    # No marker on this machine, and no flag: the guard must refuse, whether or not this machine is a VM.
    assert guard.refusal_reason() is not None
    with pytest.raises(guard.NotATestVm):
        guard.require_test_vm()


def _marker(tmp_path: Path, text: str = guard.MARKER_TEXT) -> Path:
    p = tmp_path / "marker"
    p.write_text(text)
    return p


def test_marker_and_flag_are_both_required(tmp_path: Path) -> None:
    ok_env = {guard.ENV_FLAG: "1"}
    assert guard.refusal_reason(marker=_marker(tmp_path), env=ok_env) is None
    assert guard.refusal_reason(marker=tmp_path / "missing", env=ok_env)           # no marker
    assert guard.refusal_reason(marker=_marker(tmp_path), env={})                   # no flag
    assert guard.refusal_reason(marker=_marker(tmp_path), env={guard.ENV_FLAG: "0"})
    assert guard.refusal_reason(marker=_marker(tmp_path, "something else"), env=ok_env)  # wrong text


def test_check_refuses_with_exit_3_and_inspects_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    assert check_hyprland.main() == guard.EXIT_REFUSED
    assert "refused" in capsys.readouterr().err


def test_checks_report_each_missing_requirement() -> None:
    items = {i.name: i for i in check_hyprland.checks(which=lambda n: None, exists=lambda p: False, env={})}
    assert items and not any(i.ok for i in items.values())
    assert "virtio-gpu" in items["GPU device (/dev/dri)"].detail
    full = check_hyprland.checks(which=lambda n: f"/usr/bin/{n}", exists=lambda p: True, env={"XDG_RUNTIME_DIR": "/run/user/1"})
    assert all(i.ok for i in full)
