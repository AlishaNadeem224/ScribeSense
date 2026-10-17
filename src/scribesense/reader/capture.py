"""M2.10 — Text capture for the fallback reader.

Four ways to bring text into ScribeSense:

  1. `from_clipboard()`   — the Wayland clipboard (`wl-paste --no-newline --clipboard`).
  2. `from_selection()`   — the Wayland primary selection (`wl-paste --no-newline --primary`).
  3. `from_extension(msg)` — a message from the browser extension (K11).
  4. (paste)              — handled by the reader window itself; not here.

The module is GTK-free and testable without a display. It calls `run_cmd`
(K8) to invoke `wl-paste`, which means subprocess launching goes through the
shared timeout-guarded helper rather than calling subprocess directly.

Text is never logged, written, or included in an error code. It lives only in
the returned `CaptureResult` and is discarded when the caller releases it.
"""

from __future__ import annotations

import unicodedata

from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.messages import PROTOCOL_VERSION
from scribesense.contracts.paths import run_cmd

#: A17: hard limit for reader text. Beyond this, we refuse rather than truncate.
MAX_CAPTURE_CHARS = 200_000

#: `wl-paste` should be fast; if it hangs, we give up and return the "empty" code.
_WL_PASTE_TIMEOUT_S = 5.0


class CaptureResult:
    """The result of a capture attempt. At most one of `text` or `code` is set."""

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
        return f"CaptureResult(text=<{len(self.text)} chars>)"


def normalize(text: str) -> str:
    """Normalize captured text for display.

    - NFC normalizes composed characters (e.g. an 'e' + combining acute becomes
      a single 'é'). This matches the K2 preset rules and the fonts' expectations.
    - Every C0 control character except '\\n', '\\t' and '\\r' is removed. That
      strips terminal escape sequences and the odd NUL that some apps put on the
      clipboard.
    - '\\r\\n' is collapsed to '\\n'.
    - Leading and trailing whitespace is stripped.
    """
    # Step 1: collapse CRLF so that the rest of the pipeline sees only LF.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Step 2: strip control characters except \n and \t.
    text = "".join(
        ch for ch in text if ch in ("\n", "\t") or not unicodedata.category(ch) == "Cc"
    )
    # Step 3: NFC normalization.
    text = unicodedata.normalize("NFC", text)
    # Step 4: trim leading/trailing whitespace.
    return text.strip()


def _finish(text: str, empty_code: ErrorCode) -> CaptureResult:
    """Common tail of every capture path: normalize, check size, check empty."""
    normalized = normalize(text)
    if not normalized:
        return CaptureResult(None, empty_code)
    if len(normalized) > MAX_CAPTURE_CHARS:
        return CaptureResult(None, ErrorCode.TOO_LARGE)
    return CaptureResult(normalized, None)


def _read_wl_paste(target: str) -> str | None:
    """Read a Wayland clipboard/selection via wl-paste. None on any failure.

    The only permitted way to start a process is `run_cmd` (K8 rule 9). If
    `wl-paste` is not installed, or returns non-zero, or times out, this
    function returns None and the caller maps that to the appropriate "empty"
    code. The stderr text is never surfaced: it could contain clipboard
    content on some compositors.
    """
    try:
        result = run_cmd(["wl-paste", "--no-newline", target], timeout=_WL_PASTE_TIMEOUT_S)
    except ScribeSenseError:
        return None
    except FileNotFoundError:
        # `run_cmd` may raise this if wl-paste is not installed; either way,
        # treat it as "no capture available".
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def from_clipboard() -> CaptureResult:
    """Read the Wayland clipboard.

    On failure, returns CLIPBOARD_EMPTY. The card's "empty selection" rule
    applies to the primary selection, not to the clipboard: if the user pressed
    'open what I copied' and the clipboard is empty, that is an empty clipboard.
    """
    raw = _read_wl_paste("--clipboard")
    if raw is None:
        return CaptureResult(None, ErrorCode.CLIPBOARD_EMPTY)
    return _finish(raw, empty_code=ErrorCode.CLIPBOARD_EMPTY)


def from_selection() -> CaptureResult:
    """Read the Wayland primary selection.

    On failure, returns SELECTION_UNAVAILABLE. A17 says an empty selection
    should say "nothing selected" — on Hyprland, "nothing is selected" and
    "wl-paste failed" are indistinguishable from Python's side, so they share
    the same error code and the UI turns that into "nothing selected".
    """
    raw = _read_wl_paste("--primary")
    if raw is None:
        return CaptureResult(None, ErrorCode.SELECTION_UNAVAILABLE)
    return _finish(raw, empty_code=ErrorCode.SELECTION_UNAVAILABLE)


def from_extension(message: dict[str, object]) -> CaptureResult:
    """Handle a K11 'open_in_reader' message from the browser extension.

    `message` is the parsed JSON dict, e.g.
        {"v": 1, "type": "open_in_reader", "text": "..."}

    The message shape is validated defensively: wrong protocol version, wrong
    type, or a missing/non-string `text` field all produce BAD_MESSAGE.
    """
    if not isinstance(message, dict):
        return CaptureResult(None, ErrorCode.BAD_MESSAGE)
    if message.get("v") != PROTOCOL_VERSION:
        return CaptureResult(None, ErrorCode.BAD_MESSAGE)
    if message.get("type") != "open_in_reader":
        return CaptureResult(None, ErrorCode.BAD_MESSAGE)
    text = message.get("text")
    if not isinstance(text, str):
        return CaptureResult(None, ErrorCode.BAD_MESSAGE)
    return _finish(text, empty_code=ErrorCode.NOTHING_SELECTED)