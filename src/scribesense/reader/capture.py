"""M2.10 — Text capture for the fallback reader.

Four ways to bring text into ScribeSense:

  1. `from_clipboard()`    — the Wayland clipboard (`wl-paste --no-newline --type <text type>`).
  2. `from_selection()`    — the Wayland primary selection (same, plus `--primary`).
  3. `from_extension(msg)` — a message from the browser extension (K11).
  4. (paste)               — handled by the reader window itself; not here.

The module is GTK-free and testable without a display. Every `wl-paste` call goes through
`run_cmd` (K8) with a timeout; subprocess is never used directly.

How a Wayland read works (wl-paste(1)):
  - `wl-paste --list-types` prints the MIME types the current owner offers, one per line. It exits
    non-zero when nothing is offered at all ("No selection" / "Nothing is copied").
  - We pick the first offered type from `TEXT_TYPES` (UTF-8 text only) and ask for exactly that type
    with `--type`. If no text type is offered (an image, a file list without text, binary data), we
    never paste: the result is the "empty" code, so binary bytes can never appear as garbage text.
  - `--no-newline` stops wl-paste from appending its own newline.

Outcome mapping (run_cmd's K8 mapping -> capture code):
  no Wayland session in the environment (neither
    WAYLAND_DISPLAY nor WAYLAND_SOCKET set)               -> SELECTION_UNAVAILABLE, wl-paste not run
  wl-paste exits non-zero (nothing offered, type vanished) -> empty code
  no text type offered / bytes are not valid UTF-8         -> empty code
  wl-paste missing (ADAPTER_UNAVAILABLE), hung
    (ADAPTER_TIMEOUT), launch refused (PermissionError /
    other OSError)                                        -> SELECTION_UNAVAILABLE
  more than MAX_CAPTURE_BYTES, or more than
    MAX_READER_CHARS characters                           -> TOO_LARGE (A17: reject, never truncate)

Known limit: run_cmd reports every non-zero exit as ADAPTER_COMMAND_FAILED and stderr is never read
(it can echo clipboard content), so a wl-paste that exits 1 because it cannot reach a compositor that the
environment does name (a stale WAYLAND_DISPLAY), or because the compositor lacks primary-selection
support, still shows as the empty code. The environment check above removes the common case (capture
called from a non-graphical session); the rest cannot be told apart without parsing stderr.

The empty code is NOTHING_SELECTED for the primary selection (the card's example) and CLIPBOARD_EMPTY
for the clipboard. "Cannot read at all" is SELECTION_UNAVAILABLE for BOTH ways in: in Wayland terms the
clipboard is itself a selection (wl_data_device.selection), and the K8 reader codes have no separate
"clipboard unavailable" code. It stays distinct from "empty", so the UI can say "install wl-clipboard"
instead of "the clipboard is empty". A dedicated CLIPBOARD_UNAVAILABLE would need an add-only K8 code.

A17 limits: the character limit is `contracts.messages.MAX_READER_CHARS` (one source of truth, shared
with the native-messaging host) and is counted on the raw decoded text, before normalization, exactly as
`safety.native_host` counts it. The byte cap is checked before decoding so a huge clipboard is never
decoded or normalized.

Text is never logged, written, or included in an error code. It lives only in the returned
`CaptureResult` and is discarded when the caller releases it.
"""

from __future__ import annotations

import os
import unicodedata

from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.messages import MAX_READER_CHARS, PROTOCOL_VERSION
from scribesense.contracts.paths import run_cmd

#: A17 transport limit for wl-paste output: 4 bytes is the longest UTF-8 encoding of one character,
#: plus a little slack (a BOM, stray trailing bytes). Above this the text cannot fit MAX_READER_CHARS.
MAX_CAPTURE_BYTES = MAX_READER_CHARS * 4 + 64

#: Per wl-paste call. A capture makes at most two calls (list types, then paste).
WL_PASTE_TIMEOUT_S = 5.0

#: Offered MIME types we accept, in order of preference. All of them carry UTF-8 text: GTK, Qt and
#: browsers offer `text/plain;charset=utf-8`; XWayland apps offer `UTF8_STRING`. Plain `text/plain`
#: has no declared charset, so it is accepted only if it decodes as strict UTF-8. Anything else
#: (`image/*`, `text/html` markup, `text/uri-list`, legacy `STRING`) is not reader text.
TEXT_TYPES: tuple[str, ...] = ("text/plain;charset=utf-8", "UTF8_STRING", "text/plain")


class CaptureResult:
    """The result of a capture attempt. Exactly one of `text` or `code` is set."""

    __slots__ = ("text", "code")

    def __init__(self, text: str | None, code: ErrorCode | None) -> None:
        if (text is None) == (code is None):
            raise ValueError("CaptureResult needs exactly one of text or code")
        self.text = text
        self.code = code

    def __repr__(self) -> str:  # noqa: D401 — used only in debugging, never logged
        if self.code is not None:
            return f"CaptureResult(code={self.code.value})"
        # Do not include the text itself: even the repr could end up in a traceback.
        return f"CaptureResult(text=<{len(self.text or '')} chars>)"


def normalize(text: str) -> str:
    """Normalize captured text for display.

    - '\\r\\n' and lone '\\r' become '\\n'.
    - Every control character (Unicode category Cc) except '\\n' and '\\t' is removed. That
      strips terminal escape sequences and the odd NUL that some apps put on the clipboard.
    - NFC normalizes composed characters (e.g. an 'e' + combining acute becomes a single 'é').
      This matches the K2 preset rules and the fonts' expectations.
    - Leading and trailing whitespace is stripped.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in ("\n", "\t") or unicodedata.category(ch) != "Cc")
    text = unicodedata.normalize("NFC", text)
    return text.strip()


def _finish(text: str, empty_code: ErrorCode) -> CaptureResult:
    """Common tail of every capture path: size check on the raw text (A17), then normalize, then empty.

    The size is counted before normalization, as the native host does, so the same text gets the same
    answer whichever way it came in, and oversized text is never normalized.
    """
    if len(text) > MAX_READER_CHARS:
        return CaptureResult(None, ErrorCode.TOO_LARGE)
    normalized = normalize(text)
    if not normalized:
        return CaptureResult(None, empty_code)
    return CaptureResult(normalized, None)


def _wl_paste(args: list[str]) -> bytes | ErrorCode:
    """Run `wl-paste <args>` through run_cmd. Returns stdout, or the run_cmd outcome as a code.

    ADAPTER_COMMAND_FAILED means wl-paste ran and said "nothing to give" (non-zero exit). Every other
    code (ADAPTER_UNAVAILABLE, ADAPTER_TIMEOUT; a launch OSError such as PermissionError is reported as
    ADAPTER_UNAVAILABLE) means wl-paste could not be used at all. stderr is never surfaced: on some
    compositors it can echo clipboard content.
    """
    try:
        result = run_cmd(["wl-paste", *args], timeout=WL_PASTE_TIMEOUT_S)
    except ScribeSenseError as exc:
        return exc.code
    except OSError:  # run_cmd lets PermissionError and other launch errors through unchanged (K8)
        return ErrorCode.ADAPTER_UNAVAILABLE
    return result.stdout


def _pick_text_type(listing: bytes) -> str | None:
    """The first offered type from TEXT_TYPES (compared case-insensitively, spaces ignored), or None."""
    offered: dict[str, str] = {}
    for line in listing.decode("utf-8", errors="replace").splitlines():
        name = line.strip()
        if name:
            offered.setdefault(name.replace(" ", "").lower(), name)
    for wanted in TEXT_TYPES:
        match = offered.get(wanted.lower())
        if match is not None:
            return match
    return None


def _wayland_session() -> bool:
    """True when the environment names a Wayland connection wl-paste can use (libwayland reads
    WAYLAND_SOCKET first, then WAYLAND_DISPLAY). Only presence is checked: the value is never used."""
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("WAYLAND_SOCKET"))


def _read_wayland(target: list[str], empty_code: ErrorCode) -> CaptureResult:
    """Read one Wayland selection (`[]` = clipboard, `["--primary"]` = primary) as UTF-8 text."""
    if not _wayland_session():  # wl-paste would fail to connect and exit 1, which reads as "empty"
        return CaptureResult(None, ErrorCode.SELECTION_UNAVAILABLE)
    listing = _wl_paste([*target, "--list-types"])
    if isinstance(listing, ErrorCode):
        return CaptureResult(None, _failure_code(listing, empty_code))
    mime = _pick_text_type(listing)
    if mime is None:  # nothing textual on offer: never paste binary
        return CaptureResult(None, empty_code)
    raw = _wl_paste([*target, "--no-newline", "--type", mime])
    if isinstance(raw, ErrorCode):  # e.g. the owner changed between the two calls
        return CaptureResult(None, _failure_code(raw, empty_code))
    if len(raw) > MAX_CAPTURE_BYTES:  # A17: checked before decoding
        return CaptureResult(None, ErrorCode.TOO_LARGE)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:  # not text after all; never show replacement-character garbage
        return CaptureResult(None, empty_code)
    return _finish(text, empty_code)


def _failure_code(code: ErrorCode, empty_code: ErrorCode) -> ErrorCode:
    """Map a run_cmd outcome to a capture code (see the module docstring)."""
    if code is ErrorCode.ADAPTER_COMMAND_FAILED:
        return empty_code
    return ErrorCode.SELECTION_UNAVAILABLE


def from_clipboard() -> CaptureResult:
    """Read the Wayland clipboard.

    Empty, or nothing textual on it -> CLIPBOARD_EMPTY. wl-paste unusable -> SELECTION_UNAVAILABLE.
    Too large -> TOO_LARGE.
    """
    return _read_wayland([], ErrorCode.CLIPBOARD_EMPTY)


def from_selection() -> CaptureResult:
    """Read the Wayland primary selection.

    Nothing selected (or nothing textual) -> NOTHING_SELECTED; it never falls back to anything else
    (M2.10). wl-paste unusable -> SELECTION_UNAVAILABLE. Too large -> TOO_LARGE.
    """
    return _read_wayland(["--primary"], ErrorCode.NOTHING_SELECTED)


def from_extension(message: dict[str, object]) -> CaptureResult:
    """Handle a K11 'open_in_reader' message from the browser extension.

    `message` is the parsed JSON dict, e.g. {"v": 1, "type": "open_in_reader", "text": "..."}.

    Validated like `safety.native_host`: wrong protocol version (an int, so `True` does not pass),
    wrong type, a missing/non-string `text` field, or text that is not encodable as UTF-8 (a lone
    surrogate, e.g. from a JSON "\\ud800" escape; GTK cannot display it) -> BAD_MESSAGE.
    Empty text -> NOTHING_SELECTED.
    """
    if not isinstance(message, dict):
        return CaptureResult(None, ErrorCode.BAD_MESSAGE)
    version = message.get("v")
    if type(version) is not int or version != PROTOCOL_VERSION:
        return CaptureResult(None, ErrorCode.BAD_MESSAGE)
    if message.get("type") != "open_in_reader":
        return CaptureResult(None, ErrorCode.BAD_MESSAGE)
    text = message.get("text")
    if not isinstance(text, str):
        return CaptureResult(None, ErrorCode.BAD_MESSAGE)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:  # same guard as native_host.decode_request
        return CaptureResult(None, ErrorCode.BAD_MESSAGE)
    return _finish(text, empty_code=ErrorCode.NOTHING_SELECTED)
