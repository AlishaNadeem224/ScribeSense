"""O4 — Native-messaging host: the K11 bridge between the browser extension and the service (M4.9).

The Chromium-family browser starts this process when the extension calls
`chrome.runtime.connectNative("com.scribesense.host")` and talks to it over stdin/stdout:

    browser --(Chrome framing)--> this host --(one ipc.request per message)--> running service

Chrome framing: a 4-byte length in NATIVE byte order, then that many bytes of UTF-8 JSON.

Rules:
- MAX_MESSAGE_BYTES (K11) applies to the complete encoded JSON message, both ways. A larger request
  from the browser is skipped (the stream stays in sync) and answered with BAD_MESSAGE; a reply that
  would be larger is replaced by BAD_MESSAGE (Chrome's own host->browser limit is 1 MiB too).
- Only the two K11 request types are forwarded: `get_style` and `open_in_reader`. Anything else is
  BAD_MESSAGE and never reaches the service socket (the socket also accepts confirm/reset commands
  that the browser must not be able to send). Only the K11 fields are forwarded; extra keys are dropped.
- `open_in_reader` text longer than MAX_READER_CHARS (A17) -> TOO_LARGE, never truncated.
- Service unreachable -> {"v": 1, "ok": false, "error": "APP_NOT_RUNNING"}.
- Every framed request gets exactly one framed reply, in order (the extension relies on this). The only
  other frames are `style_changed` pushes, which the extension tells apart by their `type`.
- Privacy: message content is never logged — only error codes and the request type (an identifier
  from the allow-list above).

Push (K11 "the service pushes style_changed"): a watcher thread keeps an `ipc.subscribe_style` subscription
open to the service and forwards each revision as {"v": 1, "type": "style_changed", "revision": N}. The first
one arrives as soon as the subscription opens, so a service started after the browser is noticed at the
watcher's next retry (at most WATCH_BACKOFF_MAX_S later) without the extension having to poll.
Service not running, or gone → re-subscribe with backoff (WATCH_BACKOFF_MIN_S … WATCH_BACKOFF_MAX_S). One lock
serialises every frame written to stdout, so a push never lands inside a reply. A push is only a hint; the
extension's `get_style` reply is what counts, and it still re-fetches on every (re)connect.

This module never installs anything. `render_host_manifest()` only RETURNS the JSON the installer
(M3.8) writes into the browser's NativeMessagingHosts directory.
"""

from __future__ import annotations

import json
import re
import struct
import sys
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, BinaryIO

from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.messages import MAX_MESSAGE_BYTES, MAX_READER_CHARS, PROTOCOL_VERSION
from scribesense.log import log
from scribesense.safety import ipc

HOST_NAME = "com.scribesense.host"
HOST_DESCRIPTION = "ScribeSense native messaging host"
FORWARDED_TYPES = frozenset({"get_style", "open_in_reader"})
REQUEST_TIMEOUT_S = 5.0
WATCH_BACKOFF_MIN_S = 1.0
WATCH_BACKOFF_MAX_S = 30.0

Message = dict[str, Any]
Sender = Callable[..., Message]  # ipc.request(message, *, timeout=...) -> reply
Subscriber = Callable[..., Iterable[int]]  # ipc.subscribe_style(*, timeout=...) -> revisions

_LENGTH = struct.Struct("=I")  # native byte order, 4 bytes, no padding
_CHUNK = 65536
_EXTENSION_ID = re.compile(r"[a-p]{32}")


# --- Chrome framing --------------------------------------------------------------------------------

def encode_frame(message: Message) -> bytes:
    """Length prefix + compact UTF-8 JSON. Over MAX_MESSAGE_BYTES -> ScribeSenseError(BAD_MESSAGE)."""
    try:
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):  # not JSON-serialisable, NaN, lone surrogates
        raise ScribeSenseError(ErrorCode.BAD_MESSAGE) from None
    if len(data) > MAX_MESSAGE_BYTES:
        raise ScribeSenseError(ErrorCode.BAD_MESSAGE)
    return _LENGTH.pack(len(data)) + data


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    """Up to `size` bytes; fewer only at end of stream."""
    buf = bytearray()
    while len(buf) < size:
        chunk = stream.read(min(_CHUNK, size - len(buf)))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def _discard(stream: BinaryIO, size: int) -> None:
    remaining = size
    while remaining > 0:
        chunk = stream.read(min(_CHUNK, remaining))
        if not chunk:
            return
        remaining -= len(chunk)


def read_frame(stream: BinaryIO) -> bytes | None:
    """The next message payload. None on a clean end of stream (browser closed the port).

    Oversized message -> its bytes are skipped (framing stays in sync), then BAD_MESSAGE.
    Truncated header or payload -> BAD_MESSAGE (the stream is at its end afterwards).
    """
    header = _read_exact(stream, _LENGTH.size)
    if not header:
        return None
    if len(header) < _LENGTH.size:
        raise ScribeSenseError(ErrorCode.BAD_MESSAGE)
    (length,) = _LENGTH.unpack(header)
    if length > MAX_MESSAGE_BYTES:
        _discard(stream, length)
        raise ScribeSenseError(ErrorCode.BAD_MESSAGE)
    payload = _read_exact(stream, length)
    if len(payload) < length:
        raise ScribeSenseError(ErrorCode.BAD_MESSAGE)
    return payload


def write_frame(stream: BinaryIO, message: Message) -> None:
    """Write one framed reply; a reply that cannot be framed becomes BAD_MESSAGE."""
    try:
        data = encode_frame(message)
    except ScribeSenseError as exc:
        data = encode_frame(ipc.error_reply(exc.code))
    stream.write(data)
    stream.flush()


# --- Requests --------------------------------------------------------------------------------------

def decode_request(payload: bytes) -> Message:
    """Validate a K11 request from the extension; return the message to forward (without "v").

    Raises ScribeSenseError(BAD_MESSAGE) or ScribeSenseError(TOO_LARGE).
    """
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ScribeSenseError(ErrorCode.BAD_MESSAGE) from None
    if not isinstance(message, dict):
        raise ScribeSenseError(ErrorCode.BAD_MESSAGE)
    version = message.get("v")
    if type(version) is not int or version != PROTOCOL_VERSION:  # `True == 1` must not pass
        raise ScribeSenseError(ErrorCode.BAD_MESSAGE)
    kind = message.get("type")
    if kind not in FORWARDED_TYPES:
        raise ScribeSenseError(ErrorCode.BAD_MESSAGE)
    if kind == "get_style":
        return {"type": "get_style"}
    text = message.get("text")
    if not isinstance(text, str):
        raise ScribeSenseError(ErrorCode.BAD_MESSAGE)
    try:
        text.encode("utf-8")  # lone surrogates (valid JSON escapes) cannot reach the socket
    except UnicodeEncodeError:
        raise ScribeSenseError(ErrorCode.BAD_MESSAGE) from None
    if len(text) > MAX_READER_CHARS:  # A17: reject, never truncate
        raise ScribeSenseError(ErrorCode.TOO_LARGE)
    return {"type": "open_in_reader", "text": text}


def handle(payload: bytes, *, send: Sender = ipc.request) -> Message:
    """One request payload -> one reply. Never raises for expected failures."""
    try:
        request = decode_request(payload)
    except ScribeSenseError as exc:
        log(exc.code, "extension", step="decode")
        return ipc.error_reply(exc.code)
    kind = request["type"]
    try:
        return send(request, timeout=REQUEST_TIMEOUT_S)
    except ScribeSenseError as exc:
        code = ErrorCode.APP_NOT_RUNNING if exc.code is ErrorCode.SERVICE_UNREACHABLE else exc.code
    except OSError:  # e.g. permission denied on the socket, not a socket: treat as "no app"
        code = ErrorCode.APP_NOT_RUNNING
    log(code, "extension", step="forward", kind=kind)
    return ipc.error_reply(code)


class FrameWriter:
    """Every frame to the browser goes through here: one lock, so a push never lands inside a reply."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def write(self, message: Message) -> None:
        with self._lock:
            write_frame(self._stream, message)


def watch_style(writer: FrameWriter, stop: threading.Event, *,
                subscribe: Subscriber = ipc.subscribe_style) -> None:
    """Forward the service's style revisions as K11 `style_changed` pushes until `stop` or the browser is gone."""
    delay = WATCH_BACKOFF_MIN_S
    while not stop.is_set():
        try:
            for revision in subscribe(timeout=REQUEST_TIMEOUT_S):
                if stop.is_set():
                    return
                try:
                    writer.write(ipc.style_changed(revision))
                except OSError:  # stdout closed: the browser went away (only this ends the watcher)
                    return
                delay = WATCH_BACKOFF_MIN_S  # connected: the next outage starts from the shortest wait
        except ScribeSenseError as exc:
            if exc.code is not ErrorCode.SERVICE_UNREACHABLE:
                log(exc.code, "extension", step="watch")
        except Exception:  # noqa: BLE001 — a socket error or a bug must never end the pushes; retry later
            log(ErrorCode.SERVICE_UNREACHABLE, "extension", step="watch")
        stop.wait(delay)
        delay = min(delay * 2, WATCH_BACKOFF_MAX_S)


def serve(stdin: BinaryIO, stdout: BinaryIO, *, send: Sender = ipc.request,
          subscribe: Subscriber | None = None) -> int:
    """Bridge until the browser closes the port. Returns the process exit status (0).

    `subscribe` (normally `ipc.subscribe_style`, see `main`) starts the push watcher; None = no pushes.
    """
    writer = FrameWriter(stdout)
    stop = threading.Event()
    if subscribe is not None:
        threading.Thread(target=watch_style, args=(writer, stop), kwargs={"subscribe": subscribe},
                         name="scribesense-style-watch", daemon=True).start()
    try:
        while True:
            try:
                payload = read_frame(stdin)
            except ScribeSenseError as exc:
                log(exc.code, "extension", step="frame")
                try:
                    writer.write(ipc.error_reply(exc.code))
                except OSError:
                    return 0
                continue  # oversized: skipped and in sync; truncated: the next read sees the end
            if payload is None:
                return 0
            reply = handle(payload, send=send)
            try:
                writer.write(reply)
            except OSError:  # browser went away
                return 0
    finally:
        stop.set()


# --- Host manifest (for the installer, M3.8) -------------------------------------------------------

def render_host_manifest(extension_id: str, host_path: str | Path) -> str:
    """The native-messaging HOST manifest JSON. Pure: never writes it anywhere.

    `extension_id` is the pinned ID printed by extension/tools/set_key.py (32 letters a-p);
    `host_path` is the absolute path of the executable the browser starts (Chrome requires absolute
    paths on Linux). Invalid arguments are programming errors -> ValueError.
    """
    if not _EXTENSION_ID.fullmatch(extension_id):
        raise ValueError("extension_id must be 32 characters a-p")
    path = Path(host_path)
    if not path.is_absolute():
        raise ValueError("host_path must be absolute")
    manifest = {
        "name": HOST_NAME,
        "description": HOST_DESCRIPTION,
        "path": str(path),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }
    return json.dumps(manifest, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Entry point the browser starts. Chrome passes the caller origin as argv[1]; it is not needed:
    the host manifest's allowed_origins already limits who may start the host."""
    del argv
    return serve(sys.stdin.buffer, sys.stdout.buffer, subscribe=ipc.subscribe_style)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
