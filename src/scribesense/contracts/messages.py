"""K11 — Extension <-> app messages (owner O4 with O2). FROZEN 2026-10-09 · amended R2.

Chromium family only. Path: extension -> native-messaging host -> service socket (K4 rule 5).

Rules:
- The app sends the CSS (M4.8 is the only source); the extension never builds CSS itself.
- Revisions order everything, inactive replies included; the extension ignores older revisions.
- Persistent connection: the service pushes style_changed; the extension then fetches get_style.
- On connect / reconnect: always fetch the full confirmed state.
- Disconnected: keep the last styling, show "disconnected". Local Disable works without the app.
- Bridge: native host translates Chrome framing (32-bit native-order length prefix + UTF-8 JSON)
  <-> newline-delimited JSON on the socket. MAX_MESSAGE_BYTES applies to the complete encoded message.
- Pinned extension ID: same public `key` in every manifest.json; no private key in the repo.
- Web fonts (amendment 2026-11-22, O4 + O2): an active style names its font key and styles; the extension
  fetches each style's Latin web-font copy with get_font and registers it in the page as a web font, because
  browsers may hide installed fonts from websites (Brave Shields). Only the ACTIVE style's fonts are served,
  one style per message, at most MAX_WEBFONT_BYTES of font data; the CSS itself stays free of url() / @font-face.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from scribesense.contracts.errors import ErrorCode

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 1024 * 1024  # 1 MiB, complete encoded JSON message (also the IPC socket limit)
MAX_READER_CHARS = 200_000  # A17 — reject, never truncate
MAX_WEBFONT_BYTES = 256 * 1024  # one style's web-font copy, before base64 (amendment 2026-11-22)
WEBFONT_STYLES = ("Regular", "Bold", "Italic", "BoldItalic")

#: Errors the extension can receive.
EXTENSION_ERRORS: frozenset[ErrorCode] = frozenset(
    {ErrorCode.APP_NOT_RUNNING, ErrorCode.HOST_MISSING, ErrorCode.TOO_LARGE, ErrorCode.BAD_MESSAGE,
     ErrorCode.FONT_UNAVAILABLE}
)


class OpenInReader(TypedDict):
    v: int
    type: Literal["open_in_reader"]
    text: str


class GetStyle(TypedDict):
    v: int
    type: Literal["get_style"]


class GetFont(TypedDict):  # amendment 2026-11-22
    v: int
    type: Literal["get_font"]
    key: str  # StyleActive.font_key
    style: str  # one of StyleActive.styles


class StyleActive(TypedDict):
    v: int
    ok: Literal[True]
    active: Literal[True]
    revision: int
    css: str
    font_key: str  # amendment 2026-11-22: the generated font set the css names
    styles: list[str]  # amendment 2026-11-22: styles get_font can serve for that key


class FontReply(TypedDict):  # amendment 2026-11-22
    v: int
    ok: Literal[True]
    key: str
    style: str
    family: str  # the reading family the css names, e.g. "AccessSans-<key>"
    data: str  # base64 of the web-font copy (Latin only), at most MAX_WEBFONT_BYTES before encoding


class StyleInactive(TypedDict):  # Original — remove styling
    v: int
    ok: Literal[True]
    active: Literal[False]
    revision: int


class ErrorReply(TypedDict):
    v: int
    ok: Literal[False]
    error: str  # an ErrorCode value from EXTENSION_ERRORS


class StyleChanged(TypedDict):  # pushed by the service over the open connection
    v: int
    type: Literal["style_changed"]
    revision: int
