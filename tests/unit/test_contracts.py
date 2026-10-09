"""Contract sanity checks (K1, K3, K5, K7, K8, K10, K11) — types and constants only."""

from __future__ import annotations

import dataclasses
import math

import pytest

from scribesense.contracts import paths
from scribesense.contracts.adapter import DEFAULT_TIMEOUTS, state_sha256
from scribesense.contracts.cli import ExitCode
from scribesense.contracts.config import RANGES, SHORTLIST, Configuration, IssueCode
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import PreviewFonts
from scribesense.contracts.messages import MAX_MESSAGE_BYTES, MAX_READER_CHARS, PROTOCOL_VERSION
from scribesense.contracts.states import TERMINAL, TRANSITIONS, UNFINISHED, TxState, can_transition


def test_error_codes_are_unique_identifiers() -> None:
    values = [c.value for c in ErrorCode]
    assert len(values) == len(set(values)) == 56
    assert all(c.name == c.value and c.value.isupper() for c in ErrorCode)


def test_issue_codes_are_not_error_codes() -> None:
    assert {c.value for c in IssueCode}.isdisjoint({c.value for c in ErrorCode})


def test_scribesense_error_carries_only_the_code() -> None:
    err = ScribeSenseError(ErrorCode.BACKUP_MISSING)
    assert err.code is ErrorCode.BACKUP_MISSING
    assert str(err) == "BACKUP_MISSING"


def test_ranges_cover_every_numeric_field() -> None:
    numeric = {f.name for f in dataclasses.fields(Configuration)} - {"base_family"}
    assert set(RANGES) == numeric
    for r in RANGES.values():
        assert r.min < r.max and r.step > 0
        steps = (r.max - r.min) / r.step
        assert math.isclose(steps, round(steps), abs_tol=1e-9)  # max lies on a step


def test_shortlist_is_fixed() -> None:
    assert SHORTLIST == ("Carlito", "Atkinson Hyperlegible", "Lexend", "Noto Sans")


def test_state_hash_distinguishes_absent_and_empty() -> None:
    absent = state_sha256(False, None)
    empty = state_sha256(True, b"")
    assert absent != empty
    assert state_sha256(False, b"ignored") == absent
    assert state_sha256(True, b"x") != empty


def test_default_timeouts_match_k3_rule_9() -> None:
    assert DEFAULT_TIMEOUTS == {
        "gsettings": 5.0, "dconf": 5.0, "fc-match": 10.0,
        "flatpak-fc-match": 20.0, "fc-cache": 60.0, "other": 10.0,
    }


def test_k5_state_machine() -> None:
    S = TxState
    assert len(TxState) == 10 and "CONFIRMED" not in TxState.__members__
    assert TERMINAL == {S.COMPLETED, S.REVERTED, S.FAILED_CLEAN}
    assert S.REVERT_FAILED in UNFINISHED and S.AWAITING_CONFIRMATION in UNFINISHED
    assert can_transition(S.AWAITING_CONFIRMATION, S.COMPLETED)
    assert can_transition(S.REVERT_FAILED, S.REVERTING)
    assert can_transition(S.CREATED, S.REVERTING)  # revert / uninstall tx
    assert not can_transition(S.APPLYING, S.COMPLETED)
    assert all(not TRANSITIONS[t] for t in TERMINAL)


def test_paths_follow_sandbox_overrides(sandbox) -> None:  # type: ignore[no-untyped-def]
    assert paths.home() == sandbox.home
    assert paths.config_home() == sandbox.config_home
    assert paths.data_dir() == sandbox.data_dir
    assert paths.fonts_dir() == sandbox.fonts_dir
    assert paths.logs_dir() == sandbox.data_dir / "logs"
    assert paths.lock_path() == sandbox.data_dir / "apply.lock"
    assert paths.socket_path() == sandbox.data_dir / "service.sock"


def test_paths_defaults_without_overrides(sandbox, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for var in ("SCRIBESENSE_DATA_DIR", "SCRIBESENSE_FONTS_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var)
    assert paths.data_dir() == sandbox.home / ".local/share/scribesense"
    assert paths.fonts_dir() == sandbox.home / ".local/share/fonts/scribesense"
    assert paths.config_home() == sandbox.home / ".config"


def test_exit_codes() -> None:
    assert [int(c) for c in ExitCode] == [0, 1, 2, 3, 4]


def test_k11_limits() -> None:
    assert PROTOCOL_VERSION == 1
    assert MAX_MESSAGE_BYTES == 1024 * 1024
    assert MAX_READER_CHARS == 200_000


def test_preview_fonts_declaration() -> None:
    assert [f.name for f in dataclasses.fields(PreviewFonts)] == [
        "reading_family", "ui_family", "files", "styles", "temp_dir",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        PreviewFonts("a", "b", (), (), "/tmp/x").temp_dir = "y"  # type: ignore[misc]
