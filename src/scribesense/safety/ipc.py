"""O4 — Local IPC (K4 rule 5): Unix socket at contracts.paths.socket_path(), stdlib only, no GTK.

Used by `confirm`, `reset` and the native-messaging host to talk to a RUNNING service. When no service
holds the lock, recovery never needs this socket (A2).

Rules:
- Same user only: directory 0700, socket 0600, and the peer UID (SO_PEERCRED) must match; other users'
  connections are closed without a reply.
- One JSON object per line; a complete encoded message (incl. newline) is at most MAX_MESSAGE_BYTES.
  Anything else -> {"v": 1, "ok": false, "error": "BAD_MESSAGE"}.
- Requests are {"v": 1, "type": "<name>", ...}. The handler returns the reply dict.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scribesense.contracts import paths
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.messages import MAX_MESSAGE_BYTES, PROTOCOL_VERSION

Message = dict[str, Any]
Handler = Callable[[Message], Message]

_CRED = struct.Struct("3i")  # struct ucred: pid, uid, gid
_ACCEPT_POLL_S = 0.2
_SERVER_READ_TIMEOUT_S = 5.0


def encode(message: Message) -> bytes:
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    if len(data) > MAX_MESSAGE_BYTES:
        raise ScribeSenseError(ErrorCode.BAD_MESSAGE)
    return data


def decode(line: bytes) -> Message:
    try:
        message = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        raise ScribeSenseError(ErrorCode.BAD_MESSAGE) from None
    if not isinstance(message, dict) or message.get("v") != PROTOCOL_VERSION:
        raise ScribeSenseError(ErrorCode.BAD_MESSAGE)
    return message


def error_reply(code: ErrorCode) -> Message:
    return {"v": PROTOCOL_VERSION, "ok": False, "error": code.value}


def _read_line(conn: socket.socket) -> bytes | None:
    """One newline-terminated line, at most MAX_MESSAGE_BYTES. None on EOF before any byte."""
    buf = bytearray()
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            if not buf:
                return None
            raise ScribeSenseError(ErrorCode.BAD_MESSAGE)  # no terminating newline
        buf += chunk
        end = buf.find(b"\n")
        if end != -1:
            if end + 1 > MAX_MESSAGE_BYTES:
                raise ScribeSenseError(ErrorCode.BAD_MESSAGE)
            return bytes(buf[:end])
        if len(buf) >= MAX_MESSAGE_BYTES:
            raise ScribeSenseError(ErrorCode.BAD_MESSAGE)


def peer_uid(conn: socket.socket) -> int:
    _pid, uid, _gid = _CRED.unpack(conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _CRED.size))
    return uid


class IpcServer:
    """Serves one request per connection on a background thread."""

    def __init__(self, handler: Handler, path: Path | None = None, *, allowed_uid: int | None = None) -> None:
        self.handler = handler
        self.path = path if path is not None else paths.socket_path()
        self.allowed_uid = os.getuid() if allowed_uid is None else allowed_uid
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        if self.path.exists() or self.path.is_symlink():
            if _someone_listening(self.path):
                raise ScribeSenseError(ErrorCode.APPLY_IN_PROGRESS)  # another service owns the socket
            self.path.unlink()  # stale socket from a crashed service
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)
        try:
            sock.bind(str(self.path))
        finally:
            os.umask(old_umask)
        os.chmod(self.path, 0o600)
        sock.listen(8)
        sock.settimeout(_ACCEPT_POLL_S)
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="scribesense-ipc", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> IpcServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            with conn:
                self._handle(conn)

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(_SERVER_READ_TIMEOUT_S)
        try:
            if peer_uid(conn) != self.allowed_uid:
                return  # other users: dropped, no reply
            line = _read_line(conn)
            if line is None:
                return
            try:
                reply = self.handler(decode(line))
            except ScribeSenseError as exc:
                reply = error_reply(exc.code)
            conn.sendall(encode(reply))
        except ScribeSenseError as exc:
            try:
                conn.sendall(encode(error_reply(exc.code)))
            except OSError:
                pass
        except OSError:
            pass


def _someone_listening(path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def request(message: Message, *, timeout: float, path: Path | None = None) -> Message:
    """Send one request to the running service and return its reply.

    No service / no reply in time / connection dropped -> ScribeSenseError(SERVICE_UNREACHABLE).
    Malformed or oversized reply -> ScribeSenseError(BAD_MESSAGE).
    """
    target = path if path is not None else paths.socket_path()
    data = encode({"v": PROTOCOL_VERSION, **message})
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(target))
        sock.sendall(data)
        line = _read_line(sock)
    except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError, BrokenPipeError,
            TimeoutError, socket.timeout):
        raise ScribeSenseError(ErrorCode.SERVICE_UNREACHABLE) from None
    finally:
        sock.close()
    if line is None:
        raise ScribeSenseError(ErrorCode.SERVICE_UNREACHABLE)
    return decode(line)
