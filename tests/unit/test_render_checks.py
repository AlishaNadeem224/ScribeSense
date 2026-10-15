"""M3.4 Render checks — unit level (no process is started; engines are never really run here)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from harness import render
from scribesense.contracts.errors import ErrorCode, ScribeSenseError


def test_unknown_engine_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        render.drawn_family("lynx", "hello")
    with pytest.raises(ValueError):
        render.unavailable_reason("lynx")


@pytest.mark.parametrize("sample", ["", "x" * 501, "bad\x00text", "tab\there"])
def test_bad_sample_is_rejected_before_anything_runs(sample: str) -> None:
    with patch("harness.render.run_cmd") as run:
        with pytest.raises(ValueError):
            render.drawn_family("pango", sample)
    run.assert_not_called()


@pytest.mark.parametrize("engine", ["firefox", "chromium"])
def test_unimplemented_browser_engines_report_a_reason_never_a_value(engine: str) -> None:
    with patch("harness.render.run_cmd") as run, pytest.raises(render.EngineUnavailable) as err:
        render.drawn_family(engine, "hello")
    assert "not implemented" in err.value.reason
    run.assert_not_called()  # nothing is ever sent to a browser


@pytest.mark.parametrize("engine", ["pango", "qt"])
def test_outside_the_sandbox_engines_are_unavailable(engine: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCRIBESENSE_SANDBOX", raising=False)
    with patch("harness.render.run_cmd") as run, pytest.raises(render.EngineUnavailable) as err:
        render.drawn_family(engine, "hello")
    assert "bubblewrap" in err.value.reason
    run.assert_not_called()


def test_require_engine_skips_with_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCRIBESENSE_SANDBOX", raising=False)
    with pytest.raises(pytest.skip.Exception) as err:
        render.require_engine("firefox")
    assert "firefox" in str(err.value)


def _in_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRIBESENSE_SANDBOX", "bwrap")


def test_single_family_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    _in_sandbox(monkeypatch)
    out = SimpleNamespace(stdout=json.dumps({"families": ["AccessSans-abc"]}).encode() + b"\n")
    with patch("harness.render.run_cmd", return_value=out) as run:
        assert render.drawn_family("pango", "Hello", "sans-serif") == "AccessSans-abc"
    argv = run.call_args_list[-1].args[0]
    assert argv[1:] == ["-m", "harness.render_pango", "sans-serif", "Hello"]  # no shell, family/sample as args


def test_fallback_runs_are_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    _in_sandbox(monkeypatch)
    out = SimpleNamespace(stdout=json.dumps({"families": ["AccessSans-abc", "DejaVu Sans"]}).encode())
    with patch("harness.render.run_cmd", return_value=out):
        assert render.drawn_family("pango", "Hello") == "AccessSans-abc, DejaVu Sans"


@pytest.mark.parametrize("stdout", [b"", b"not json", b'{"families": []}', b'{"families": [""]}', b'{"x": 1}',
                                    b'{"families": "A"}'])
def test_garbage_probe_output_is_a_check_failure(stdout: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    _in_sandbox(monkeypatch)
    with patch("harness.render.run_cmd", return_value=SimpleNamespace(stdout=stdout)):
        with pytest.raises(ScribeSenseError) as err:
            render.drawn_family("pango", "Hello")
    assert err.value.code is ErrorCode.CHECK_UNAVAILABLE


def test_engine_missing_is_unavailable_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _in_sandbox(monkeypatch)
    with patch("harness.render.run_cmd", side_effect=ScribeSenseError(ErrorCode.ADAPTER_COMMAND_FAILED)):
        assert "PangoCairo" in (render.unavailable_reason("pango") or "")
        with pytest.raises(render.EngineUnavailable):
            render.drawn_family("pango", "Hello")
