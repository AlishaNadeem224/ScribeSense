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
"""

from __future__ import annotations

from typing import Literal, TypedDict

from scribesense.contracts.errors import ErrorCode

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 1024 * 1024  # 1 MiB, complete encoded JSON message (also the IPC socket limit)
MAX_READER_CHARS = 200_000  # A17 — reject, never truncate

#: Errors the extension can receive.
EXTENSION_ERRORS: frozenset[ErrorCode] = frozenset(
    {ErrorCode.APP_NOT_RUNNING, ErrorCode.HOST_MISSING, ErrorCode.TOO_LARGE, ErrorCode.BAD_MESSAGE}
)


class OpenInReader(TypedDict):
    v: int
    type: Literal["open_in_reader"]
    text: str


class GetStyle(TypedDict):
    v: int
    type: Literal["get_style"]


class StyleActive(TypedDict):
    v: int
    ok: Literal[True]
    active: Literal[True]
    revision: int
    css: str


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
