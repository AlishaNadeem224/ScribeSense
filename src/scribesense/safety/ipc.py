"""O4 — Local IPC (K4 rule 5): Unix socket at contracts.paths.socket_path(), stdlib only, no GTK.

Used by `confirm`, `reset` and the native-messaging host to talk to a RUNNING service. When no service
holds the lock, recovery never needs this socket (A2).

Rules:
- Same user only: directory 0700, socket 0600, and the peer UID (SO_PEERCRED) must match; other users'
  connections are closed without a reply.
- One JSON object per line; a complete encoded message (incl. newline) is at most MAX_MESSAGE_BYTES.
  Anything else -> {"v": 1, "ok": false, "error": "BAD_MESSAGE"}.
- Requests are {"v": 1, "type": "<name>", ...}. The handler returns the reply dict.

Style subscription (K11 "the service pushes style_changed"; K4 rule 5 addition, O4 2026-10-18):
- {"v": 1, "type": "subscribe_style"} is answered by the SERVER itself, never by the handler: the connection
  stays open, the first line is {"v": 1, "type": "style_changed", "revision": <current>} and every later
  confirmed-style change sends one more such line (`publish_style_changed`). Only a server constructed with
  `style_revision=` accepts it (others reply BAD_MESSAGE). At most MAX_SUBSCRIBERS at once. A push never waits
  for a subscriber: one that is gone or cannot take the whole line at once is dropped, and re-subscribes on its
  own (its greeting then carries the current revision, so nothing is lost). The native-messaging host (M4.9) is
  the subscriber.
- A push is a notification, never the source of truth: the extension then asks `get_style` (K11).
"""

from __future__ import annotations

import json
import os
import socket
import struct
import select
import threading
from collections.abc import Callable, Iterator
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

SUBSCRIBE_STYLE = "subscribe_style"
MAX_SUBSCRIBERS = 32  # one native host per running Chromium-family browser
_SUBSCRIBER_SEND_TIMEOUT_S = 1.0  # greeting only; pushes use a non-blocking send (publish_style_changed)


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


def style_changed(revision: int) -> Message:
    """The K11 StyleChanged notification."""
    if type(revision) is not int:
        raise ScribeSenseError(ErrorCode.BAD_MESSAGE)
    return {"v": PROTOCOL_VERSION, "type": "style_changed", "revision": revision}


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
    """Serves one request per connection on a background thread, plus style subscriptions (see module doc).

    `style_revision` returns the confirmed style revision (the journal's `style_revision`); without it the
    server does not offer subscriptions.
    """

    def __init__(self, handler: Handler, path: Path | None = None, *, allowed_uid: int | None = None,
                 style_revision: Callable[[], int] | None = None) -> None:
        self.handler = handler
        self.path = path if path is not None else paths.socket_path()
        self.allowed_uid = os.getuid() if allowed_uid is None else allowed_uid
        self.style_revision = style_revision
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._subscribers: list[socket.socket] = []
        # One lock for every write to a subscriber and for the subscriber list: a greeting and a publish can
        # never interleave on one socket, and a revision change between reading the greeting's revision and
        # registering the subscriber cannot be missed.
        self._subs_lock = threading.Lock()

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
        with self._subs_lock:
            for conn in self._subscribers:
                conn.close()
            self._subscribers.clear()
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
            if not self._handle(conn):
                conn.close()

    def _handle(self, conn: socket.socket) -> bool:
        """Serve one connection. True = it became a subscriber and stays open."""
        conn.settimeout(_SERVER_READ_TIMEOUT_S)
        try:
            if peer_uid(conn) != self.allowed_uid:
                return False  # other users: dropped, no reply
            line = _read_line(conn)
            if line is None:
                return False
            try:
                message = decode(line)
                if message.get("type") == SUBSCRIBE_STYLE:
                    return self._subscribe(conn)
                reply = self.handler(message)
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
        return False

    # ---------------------------------------------------------------- style subscriptions

    def _subscribe(self, conn: socket.socket) -> bool:
        if self.style_revision is None:
            raise ScribeSenseError(ErrorCode.BAD_MESSAGE)  # this server offers no subscriptions
        with self._subs_lock:
            if self._stop.is_set():
                return False  # stopping: stop() already cleared the list; _serve closes conn, the host retries
            self._prune_closed()
            if len(self._subscribers) >= MAX_SUBSCRIBERS:
                raise ScribeSenseError(ErrorCode.BAD_MESSAGE)
            try:
                greeting = encode(style_changed(self.style_revision()))
            except ScribeSenseError:
                raise  # answered with its code by _handle
            except Exception:  # noqa: BLE001 — the revision cannot be read: no subscription, the host retries
                return False
            conn.settimeout(_SUBSCRIBER_SEND_TIMEOUT_S)
            conn.sendall(greeting)
            # From now on every write is a push and must never wait: a socket with a timeout makes Python poll
            # for writability first (MSG_DONTWAIT alone does not stop that), so make it truly non-blocking.
            conn.setblocking(False)
            self._subscribers.append(conn)
        return True

    def _prune_closed(self) -> None:
        """Drop subscribers that hung up. A subscriber never sends after subscribing, so a readable one has either
        closed (EOF) or broken the protocol: both are dropped without reading. Caller holds the lock."""
        alive = []
        for conn in self._subscribers:
            try:
                readable, _, _ = select.select([conn], [], [], 0)
                if readable:
                    raise OSError("peer closed or sent data")
            except (OSError, ValueError):
                conn.close()
                continue
            alive.append(conn)
        self._subscribers[:] = alive

    def publish_style_changed(self, revision: int) -> int:
        """Tell every subscriber the confirmed style changed. Never raises, never blocks; returns how many were reached.

        Called by the service after Keep / Original (the journal's `on_style_changed`), so it must not delay a
        Keep: each line goes out in ONE non-blocking send. A subscriber that is gone, or whose buffer cannot take
        the whole line right now, is dropped — a partial line makes its reader fail, it re-subscribes and its
        greeting carries the current revision. The lock is held only for these microsecond sends.
        """
        try:
            data = encode(style_changed(revision))
        except ScribeSenseError:
            return 0
        with self._subs_lock:
            alive = []
            for conn in self._subscribers:
                try:
                    if conn.send(data, socket.MSG_DONTWAIT) != len(data):
                        raise BlockingIOError  # only part of the line fit: drop, never wait
                except OSError:  # EAGAIN/BlockingIOError, EPIPE, ECONNRESET, ...
                    conn.close()
                    continue
                alive.append(conn)
            self._subscribers[:] = alive
            return len(alive)

    @property
    def subscriber_count(self) -> int:
        with self._subs_lock:
            return len(self._subscribers)


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


class _LineReader:
    """Newline-delimited messages from a stream socket, keeping bytes that arrive after a newline."""

    def __init__(self, conn: socket.socket) -> None:
        self._conn = conn
        self._buf = bytearray()

    def next_line(self) -> bytes | None:
        """The next line; None on EOF at a line boundary. Over MAX_MESSAGE_BYTES or EOF mid-line -> BAD_MESSAGE."""
        while True:
            end = self._buf.find(b"\n")
            if end != -1:
                if end + 1 > MAX_MESSAGE_BYTES:
                    raise ScribeSenseError(ErrorCode.BAD_MESSAGE)
                line = bytes(self._buf[:end])
                del self._buf[:end + 1]
                return line
            if len(self._buf) >= MAX_MESSAGE_BYTES:
                raise ScribeSenseError(ErrorCode.BAD_MESSAGE)
            chunk = self._conn.recv(65536)
            if not chunk:
                if self._buf:
                    raise ScribeSenseError(ErrorCode.BAD_MESSAGE)
                return None
            self._buf += chunk


def subscribe_style(*, timeout: float, path: Path | None = None) -> Iterator[int]:
    """Subscribe to confirmed-style changes; yields revisions, the first one being the current revision.

    `timeout` bounds connecting and the first reply only; afterwards the call waits for pushes as long as
    the service keeps the connection open. No service, or the service went away -> SERVICE_UNREACHABLE.
    A reply that is not a K11 style_changed (e.g. the service offers no subscriptions) -> BAD_MESSAGE.
    """
    target = path if path is not None else paths.socket_path()
    sock: socket.socket | None = None
    try:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # EMFILE/ENFILE can happen here
            sock.settimeout(timeout)
            sock.connect(str(target))
            sock.sendall(encode({"v": PROTOCOL_VERSION, "type": SUBSCRIBE_STYLE}))
            reader = _LineReader(sock)
            first = reader.next_line()
        except OSError:  # missing, refused, backlog full (EAGAIN), EACCES, EMFILE, timeout, ...: retry later
            raise ScribeSenseError(ErrorCode.SERVICE_UNREACHABLE) from None
        if first is None:
            raise ScribeSenseError(ErrorCode.SERVICE_UNREACHABLE)
        yield _revision_of(decode(first))  # (the yields are outside the try: a consumer's error stays its own)
        sock.settimeout(None)
        while True:
            try:
                line = reader.next_line()
            except OSError:
                raise ScribeSenseError(ErrorCode.SERVICE_UNREACHABLE) from None
            if line is None:
                raise ScribeSenseError(ErrorCode.SERVICE_UNREACHABLE)  # the service closed it (e.g. quit)
            yield _revision_of(decode(line))
    finally:
        if sock is not None:
            sock.close()


def _revision_of(message: Message) -> int:
    revision = message.get("revision")
    if message.get("type") != "style_changed" or type(revision) is not int:
        raise ScribeSenseError(ErrorCode.BAD_MESSAGE)
    return revision
