"""M2.10 — capture normalization, limits, and failure codes.

The two Wayland paths (`from_clipboard`, `from_selection`) are exercised with
a stubbed `run_cmd`. The project sandbox blocks real subprocess launches in
unit tests, so stubbing is not optional here — it is the only correct way.

`from_extension` and `normalize` need no stubs.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest

from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.reader import capture


# --- helper: a stub run_cmd that returns a fake CompletedProcess -----------

def _stub_run_cmd(stdout: bytes, returncode: int = 0):
    def _run(argv: Sequence[str], timeout: float) -> Any:
        # We do not assert argv here; the specific test does that if it matters.
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=b"")
    return _run


def _stub_run_cmd_raises(exc: BaseException):
    def _run(argv: Sequence[str], timeout: float) -> Any:
        raise exc
    return _run


# --- normalize -----------------------------------------------------------

def test_normalize_collapses_crlf_and_strips() -> None:
    assert capture.normalize("\r\n  hello\r\nworld  \r\n") == "hello\nworld"


def test_normalize_applies_nfc() -> None:
    # 'e' followed by a combining acute accent should become 'é' after NFC.
    assert capture.normalize("e\u0301") == "\u00e9"


def test_normalize_strips_control_characters() -> None:
    # BEL (0x07), NUL (0x00), and a typical ANSI escape (\x1b).
    assert capture.normalize("a\x07b\x00c\x1bd") == "abcd"


def test_normalize_keeps_tab_and_newline() -> None:
    assert capture.normalize("a\tb\nc") == "a\tb\nc"


def test_normalize_empty_becomes_empty_string() -> None:
    assert capture.normalize("   \r\n\t ") == ""


# --- from_clipboard ------------------------------------------------------

def test_from_clipboard_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture, "run_cmd", _stub_run_cmd(b"hello world"))
    result = capture.from_clipboard()
    assert result.code is None
    assert result.text == "hello world"


def test_from_clipboard_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture, "run_cmd", _stub_run_cmd(b""))
    result = capture.from_clipboard()
    assert result.code is ErrorCode.CLIPBOARD_EMPTY
    assert result.text is None


def test_from_clipboard_command_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture, "run_cmd", _stub_run_cmd(b"", returncode=1))
    result = capture.from_clipboard()
    assert result.code is ErrorCode.CLIPBOARD_EMPTY


def test_from_clipboard_missing_program(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture, "run_cmd", _stub_run_cmd_raises(ScribeSenseError(ErrorCode.ADAPTER_COMMAND_FAILED)))
    result = capture.from_clipboard()
    assert result.code is ErrorCode.CLIPBOARD_EMPTY


# --- from_selection ------------------------------------------------------

def test_from_selection_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture, "run_cmd", _stub_run_cmd(b"selected words"))
    result = capture.from_selection()
    assert result.code is None
    assert result.text == "selected words"


def test_from_selection_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture, "run_cmd", _stub_run_cmd(b""))
    result = capture.from_selection()
    assert result.code is ErrorCode.SELECTION_UNAVAILABLE


def test_from_selection_command_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture, "run_cmd", _stub_run_cmd(b"", returncode=1))
    result = capture.from_selection()
    assert result.code is ErrorCode.SELECTION_UNAVAILABLE


# --- from_extension ------------------------------------------------------

def test_from_extension_success() -> None:
    msg = {"v": 1, "type": "open_in_reader", "text": "some text"}
    result = capture.from_extension(msg)
    assert result.code is None
    assert result.text == "some text"


def test_from_extension_empty_text() -> None:
    msg = {"v": 1, "type": "open_in_reader", "text": ""}
    result = capture.from_extension(msg)
    assert result.code is ErrorCode.NOTHING_SELECTED


def test_from_extension_wrong_version() -> None:
    msg = {"v": 99, "type": "open_in_reader", "text": "text"}
    result = capture.from_extension(msg)
    assert result.code is ErrorCode.BAD_MESSAGE


def test_from_extension_wrong_type() -> None:
    msg = {"v": 1, "type": "something_else", "text": "text"}
    result = capture.from_extension(msg)
    assert result.code is ErrorCode.BAD_MESSAGE


def test_from_extension_missing_text() -> None:
    msg = {"v": 1, "type": "open_in_reader"}
    result = capture.from_extension(msg)
    assert result.code is ErrorCode.BAD_MESSAGE


def test_from_extension_non_string_text() -> None:
    msg = {"v": 1, "type": "open_in_reader", "text": 42}
    result = capture.from_extension(msg)
    assert result.code is ErrorCode.BAD_MESSAGE


# --- oversize ------------------------------------------------------------

def test_oversize_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    big = ("x" * (capture.MAX_CAPTURE_CHARS + 1)).encode()
    monkeypatch.setattr(capture, "run_cmd", _stub_run_cmd(big))
    result = capture.from_clipboard()
    assert result.code is ErrorCode.TOO_LARGE
    assert result.text is None


def test_oversize_in_extension() -> None:
    big = "x" * (capture.MAX_CAPTURE_CHARS + 1)
    msg = {"v": 1, "type": "open_in_reader", "text": big}
    result = capture.from_extension(msg)
    assert result.code is ErrorCode.TOO_LARGE


# --- CaptureResult contract ---------------------------------------------

def test_capture_result_requires_exactly_one_field() -> None:
    with pytest.raises(ValueError):
        capture.CaptureResult(None, None)
    with pytest.raises(ValueError):
        capture.CaptureResult("text", ErrorCode.TOO_LARGE)


def test_capture_result_repr_hides_text() -> None:
    # The repr is used only in debugging; it must not leak captured text.
    r = capture.CaptureResult("secret document text", None)
    assert "secret" not in repr(r)