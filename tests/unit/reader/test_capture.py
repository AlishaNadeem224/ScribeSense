"""M2.10 — capture: each way in, empty / unavailable / oversize / binary cases, A17 limits.

The Wayland paths are driven through the real `capture` code with a fake `run_cmd` that behaves like
wl-paste(1) does under K8's run_cmd mapping: `--list-types` prints the offered MIME types or fails
with ADAPTER_COMMAND_FAILED when nothing is offered; `--type T` prints the data for T or fails when T
is not offered. The project sandbox blocks real processes in unit tests, so faking run_cmd is the only
correct way here. The sandbox removes WAYLAND_DISPLAY, so the `fake` fixture names a (fake) Wayland
session; the tests for a missing session remove it again.
"""

from __future__ import annotations

import json
import os
import struct
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.messages import MAX_READER_CHARS
from scribesense.reader import capture

UTF8 = "text/plain;charset=utf-8"


class FakeWlPaste:
    """Stands in for `run_cmd(["wl-paste", ...])`. Records every call (argv, timeout)."""

    def __init__(
        self,
        offers: dict[str, bytes] | None = None,
        *,
        fail_with: BaseException | None = None,
        vanish_before_paste: bool = False,
    ) -> None:
        self.offers = offers  # None = nothing copied / nothing selected
        self.fail_with = fail_with
        self.vanish_before_paste = vanish_before_paste
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv: Sequence[str], timeout: float) -> Any:
        argv = list(argv)
        self.calls.append((argv, timeout))
        assert argv[0] == "wl-paste"
        if self.fail_with is not None:
            raise self.fail_with
        if self.offers is None:
            raise ScribeSenseError(ErrorCode.ADAPTER_COMMAND_FAILED)  # "No selection" exits 1
        if "--list-types" in argv:
            out = "".join(f"{t}\n" for t in self.offers).encode()
            return SimpleNamespace(returncode=0, stdout=out, stderr=b"")
        if self.vanish_before_paste:
            raise ScribeSenseError(ErrorCode.ADAPTER_COMMAND_FAILED)
        wanted = argv[argv.index("--type") + 1]
        if wanted not in self.offers:
            raise ScribeSenseError(ErrorCode.ADAPTER_COMMAND_FAILED)
        return SimpleNamespace(returncode=0, stdout=self.offers[wanted], stderr=b"")

    @property
    def paste_calls(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls if "--list-types" not in argv]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch):
    def install(*args: Any, **kwargs: Any) -> FakeWlPaste:
        wl = FakeWlPaste(*args, **kwargs)
        monkeypatch.setattr(capture, "run_cmd", wl)
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-fake")  # only its presence is checked
        return wl

    return install


# --- normalize -----------------------------------------------------------


def test_normalize_collapses_crlf_and_strips() -> None:
    assert capture.normalize("\r\n  hello\r\nworld  \r\n") == "hello\nworld"


def test_normalize_applies_nfc() -> None:
    assert capture.normalize("é") == "é"


def test_normalize_strips_control_characters() -> None:
    assert capture.normalize("a\x07b\x00c\x1bd") == "abcd"


def test_normalize_keeps_tab_and_newline() -> None:
    assert capture.normalize("a\tb\nc") == "a\tb\nc"


def test_normalize_empty_becomes_empty_string() -> None:
    assert capture.normalize("   \r\n\t ") == ""


# --- argv / timeout ------------------------------------------------------


def test_clipboard_argv_lists_types_then_requests_text_type(fake) -> None:
    wl = fake({UTF8: b"hello", "text/html": b"<b>hello</b>"})
    assert capture.from_clipboard().text == "hello"
    (list_argv, t1), (paste_argv, t2) = wl.calls
    assert list_argv == ["wl-paste", "--list-types"]
    assert paste_argv == ["wl-paste", "--no-newline", "--type", UTF8]
    assert t1 == t2 == capture.WL_PASTE_TIMEOUT_S > 0


def test_selection_argv_uses_primary(fake) -> None:
    wl = fake({UTF8: b"picked"})
    assert capture.from_selection().text == "picked"
    assert all("--primary" in argv for argv, _ in wl.calls)
    assert wl.paste_calls == [["wl-paste", "--primary", "--no-newline", "--type", UTF8]]


@pytest.mark.parametrize(
    ("offers", "expected_type"),
    [
        ({"text/html": b"x", "text/plain": b"x", UTF8: b"x"}, UTF8),
        ({"TARGETS": b"", "UTF8_STRING": b"x", "text/plain": b"x"}, "UTF8_STRING"),
        ({"text/plain": b"x"}, "text/plain"),
        ({"text/plain;charset=UTF-8": b"x"}, "text/plain;charset=UTF-8"),  # case as offered
    ],
)
def test_text_type_preference(fake, offers: dict[str, bytes], expected_type: str) -> None:
    wl = fake(offers)
    assert capture.from_clipboard().text == "x"
    assert wl.paste_calls[0][-1] == expected_type


# --- success -------------------------------------------------------------


def test_from_clipboard_success_is_normalized(fake) -> None:
    fake({UTF8: "  café\r\nline\x1b  ".encode()})
    result = capture.from_clipboard()
    assert result.code is None
    assert result.text == "café\nline"


def test_from_selection_success(fake) -> None:
    fake({UTF8: b"selected words"})
    result = capture.from_selection()
    assert (result.text, result.code) == ("selected words", None)


# --- empty: distinct codes per way in ------------------------------------


def test_empty_selection_is_nothing_selected(fake) -> None:
    fake(None)  # wl-paste: "No selection", exit 1
    result = capture.from_selection()
    assert result.code is ErrorCode.NOTHING_SELECTED
    assert result.text is None


def test_whitespace_selection_is_nothing_selected(fake) -> None:
    fake({UTF8: b" \n\t "})
    assert capture.from_selection().code is ErrorCode.NOTHING_SELECTED


def test_clipboard_empty(fake) -> None:
    fake(None)  # wl-paste: "Nothing is copied", exit 1
    assert capture.from_clipboard().code is ErrorCode.CLIPBOARD_EMPTY


def test_whitespace_clipboard_is_empty(fake) -> None:
    fake({UTF8: b"   "})
    assert capture.from_clipboard().code is ErrorCode.CLIPBOARD_EMPTY


def test_type_vanishes_between_calls_is_empty(fake) -> None:
    fake({UTF8: b"x"}, vanish_before_paste=True)
    assert capture.from_clipboard().code is ErrorCode.CLIPBOARD_EMPTY
    assert capture.from_selection().code is ErrorCode.NOTHING_SELECTED


# --- binary / non-text ---------------------------------------------------

_PNG = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + bytes(range(256))


def test_image_only_clipboard_is_empty_and_never_pasted(fake) -> None:
    wl = fake({"image/png": _PNG})
    assert capture.from_clipboard().code is ErrorCode.CLIPBOARD_EMPTY
    assert wl.paste_calls == []  # the image bytes are never even read


def test_image_only_selection_is_nothing_selected(fake) -> None:
    wl = fake({"image/png": _PNG, "text/uri-list": b"file:///x"})
    assert capture.from_selection().code is ErrorCode.NOTHING_SELECTED
    assert wl.paste_calls == []


def test_invalid_utf8_text_is_rejected_not_replaced(fake) -> None:
    fake({"text/plain": b"\xff\xfe binary" + _PNG})
    result = capture.from_clipboard()
    assert result.code is ErrorCode.CLIPBOARD_EMPTY
    assert result.text is None


# --- tool missing / hung / launch refused --------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        ScribeSenseError(ErrorCode.ADAPTER_UNAVAILABLE),  # wl-clipboard not installed
        ScribeSenseError(ErrorCode.ADAPTER_TIMEOUT),  # wl-paste hung
        PermissionError(13, "Permission denied"),  # no exec bit / a directory on PATH
        OSError(8, "Exec format error"),
    ],
    ids=["missing", "timeout", "permission", "oserror"],
)
@pytest.mark.parametrize("way_in", ["from_clipboard", "from_selection"])
def test_wl_paste_unusable_is_selection_unavailable(fake, failure: BaseException, way_in: str) -> None:
    wl = fake({UTF8: b"text"}, fail_with=failure)
    result = getattr(capture, way_in)()
    assert result.code is ErrorCode.SELECTION_UNAVAILABLE
    assert result.text is None
    assert len(wl.calls) == 1  # no retry, no paste after the tool failed


def test_unavailable_differs_from_empty(fake) -> None:
    fake(None)
    empty = capture.from_clipboard().code
    fake({}, fail_with=ScribeSenseError(ErrorCode.ADAPTER_UNAVAILABLE))
    assert capture.from_clipboard().code is not empty


# --- no Wayland session: unavailable, not empty ------------------------


@pytest.mark.parametrize("way_in", ["from_clipboard", "from_selection"])
def test_no_wayland_session_is_unavailable_without_running_wl_paste(
    fake, monkeypatch: pytest.MonkeyPatch, way_in: str
) -> None:
    wl = fake(None)  # a real wl-paste would print "Failed to connect to a Wayland server" and exit 1
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_SOCKET", raising=False)
    result = getattr(capture, way_in)()
    assert result.code is ErrorCode.SELECTION_UNAVAILABLE
    assert result.text is None
    assert wl.calls == []


def test_empty_wayland_display_counts_as_missing(fake, monkeypatch: pytest.MonkeyPatch) -> None:
    wl = fake({UTF8: b"text"})
    monkeypatch.setenv("WAYLAND_DISPLAY", "")
    monkeypatch.delenv("WAYLAND_SOCKET", raising=False)
    assert capture.from_clipboard().code is ErrorCode.SELECTION_UNAVAILABLE
    assert wl.calls == []


def test_wayland_socket_alone_is_a_session(fake, monkeypatch: pytest.MonkeyPatch) -> None:
    fake({UTF8: b"text"})
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_SOCKET", "3")
    assert capture.from_clipboard().text == "text"


# --- A17: too large is rejected, never truncated -------------------------


def test_exactly_max_chars_accepted_via_selection(fake) -> None:
    fake({UTF8: b"x" * MAX_READER_CHARS})
    result = capture.from_selection()
    assert result.code is None
    assert len(result.text or "") == MAX_READER_CHARS


@pytest.mark.parametrize("way_in", ["from_clipboard", "from_selection"])
def test_one_char_over_is_too_large(fake, way_in: str) -> None:
    fake({UTF8: b"x" * (MAX_READER_CHARS + 1)})
    result = getattr(capture, way_in)()
    assert result.code is ErrorCode.TOO_LARGE
    assert result.text is None  # rejected, not truncated


def test_multibyte_counts_characters_not_bytes(fake) -> None:
    emoji = "\U0001f600"  # 4 bytes in UTF-8
    fake({UTF8: (emoji * MAX_READER_CHARS).encode()})
    assert len(capture.from_clipboard().text or "") == MAX_READER_CHARS
    fake({UTF8: (emoji * (MAX_READER_CHARS + 1)).encode()})
    assert capture.from_clipboard().code is ErrorCode.TOO_LARGE


def test_byte_cap_rejects_before_decoding(fake, monkeypatch: pytest.MonkeyPatch) -> None:
    fake({UTF8: b"\xff" * (capture.MAX_CAPTURE_BYTES + 1)})  # would be "not UTF-8" if decoded
    monkeypatch.setattr(capture, "normalize", _must_not_run)
    assert capture.from_clipboard().code is ErrorCode.TOO_LARGE


def test_counted_on_raw_text_before_normalization(fake, monkeypatch: pytest.MonkeyPatch) -> None:
    # MAX - 5 visible chars + 5 CRLFs = MAX + 5 raw chars: rejected, like the native host does.
    fake({UTF8: ("x" * (MAX_READER_CHARS - 5) + "\r\n" * 5).encode()})
    monkeypatch.setattr(capture, "normalize", _must_not_run)
    assert capture.from_clipboard().code is ErrorCode.TOO_LARGE


def test_decomposed_text_counted_raw() -> None:
    raw = "é" * (MAX_READER_CHARS // 2 + 1)  # MAX + 2 code points, MAX/2 + 1 after NFC
    msg = {"v": 1, "type": "open_in_reader", "text": raw}
    assert capture.from_extension(msg).code is ErrorCode.TOO_LARGE


def _must_not_run(_text: str) -> str:
    raise AssertionError("oversized text must not be normalized")


@pytest.mark.parametrize(
    "text",
    [
        "x" * MAX_READER_CHARS,
        "x" * (MAX_READER_CHARS + 1),
        "x" * (MAX_READER_CHARS - 5) + "\r\n" * 5,
        "é" * (MAX_READER_CHARS // 2 + 1),
        "hi \ud800",
        "\udfff" + "x" * (MAX_READER_CHARS + 1),
        "ok \U0001f600",
    ],
    ids=["max", "max+1", "crlf", "decomposed", "lone-surrogate", "surrogate-and-oversize", "astral"],
)
def test_same_answer_as_native_host(text: str) -> None:
    # TOO_LARGE and BAD_MESSAGE (or acceptance) must match the native host for the same message.
    from scribesense.safety import native_host

    msg = {"v": 1, "type": "open_in_reader", "text": text}
    try:
        native_host.decode_request(json.dumps(msg).encode())
        host_code = None
    except ScribeSenseError as exc:
        host_code = exc.code
    assert host_code in (None, ErrorCode.TOO_LARGE, ErrorCode.BAD_MESSAGE)
    assert capture.from_extension(msg).code is host_code


# --- from_extension ------------------------------------------------------


def test_from_extension_success() -> None:
    result = capture.from_extension({"v": 1, "type": "open_in_reader", "text": "some text"})
    assert (result.text, result.code) == ("some text", None)


def test_from_extension_empty_text() -> None:
    result = capture.from_extension({"v": 1, "type": "open_in_reader", "text": ""})
    assert result.code is ErrorCode.NOTHING_SELECTED


@pytest.mark.parametrize(
    "msg",
    [
        {"v": 99, "type": "open_in_reader", "text": "t"},
        {"v": True, "type": "open_in_reader", "text": "t"},  # True == 1 must not pass
        {"v": 1, "type": "something_else", "text": "t"},
        {"v": 1, "type": "open_in_reader"},
        {"v": 1, "type": "open_in_reader", "text": 42},
        {"v": 1, "type": "open_in_reader", "text": "hi \ud800"},  # lone surrogate (JSON escape)
        {"v": 1, "type": "open_in_reader", "text": "\udc00"},
        ["not", "a", "dict"],
    ],
    ids=["version", "bool-version", "type", "missing-text", "non-string", "lone-high-surrogate",
         "lone-low-surrogate", "not-dict"],
)
def test_from_extension_bad_message(msg: Any) -> None:
    assert capture.from_extension(msg).code is ErrorCode.BAD_MESSAGE


def test_oversize_in_extension() -> None:
    msg = {"v": 1, "type": "open_in_reader", "text": "x" * (MAX_READER_CHARS + 1)}
    result = capture.from_extension(msg)
    assert result.code is ErrorCode.TOO_LARGE
    assert result.text is None


# --- privacy: canary never reaches disk ----------------------------------

CANARY = "CANARY-7f3a-reader-text-never-on-disk"


def _sweep(roots: Sequence[Path], needle: bytes) -> list[str]:
    hits = []
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                path = Path(dirpath) / name
                try:
                    if needle in path.read_bytes():
                        hits.append(name)
                except OSError:
                    continue
    return hits


def test_canary_never_written_to_disk(fake, sandbox) -> None:
    fake({UTF8: f"start {CANARY} end".encode()})
    assert CANARY in (capture.from_clipboard().text or "")
    assert CANARY in (capture.from_selection().text or "")
    assert CANARY in (capture.from_extension({"v": 1, "type": "open_in_reader", "text": CANARY}).text or "")
    roots = [sandbox.root, sandbox.home, sandbox.data_dir, sandbox.config_home, sandbox.runtime_dir]
    assert _sweep([r for r in roots if r.exists()], CANARY.encode()) == []


# --- CaptureResult contract ---------------------------------------------


def test_capture_result_requires_exactly_one_field() -> None:
    with pytest.raises(ValueError):
        capture.CaptureResult(None, None)
    with pytest.raises(ValueError):
        capture.CaptureResult("text", ErrorCode.TOO_LARGE)


def test_capture_result_repr_hides_text() -> None:
    assert "secret" not in repr(capture.CaptureResult("secret document text", None))
