"""K4 rule 5 — local IPC socket."""

from __future__ import annotations

import os
import socket
import time

import pytest

from scribesense.contracts import paths
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.messages import MAX_MESSAGE_BYTES
from scribesense.safety import ipc


def echo(message: ipc.Message) -> ipc.Message:
    return {"v": 1, "ok": True, "type": message["type"]}


def test_round_trip_and_permissions() -> None:
    with ipc.IpcServer(echo) as server:
        assert server.path == paths.socket_path()
        assert oct(server.path.stat().st_mode & 0o777) == "0o600"
        assert oct(server.path.parent.stat().st_mode & 0o777) == "0o700"
        assert ipc.request({"type": "ping"}, timeout=2) == {"v": 1, "ok": True, "type": "ping"}
    assert not paths.socket_path().exists(), "socket removed on stop"


def test_no_service_is_service_unreachable() -> None:
    with pytest.raises(ScribeSenseError) as exc:
        ipc.request({"type": "ping"}, timeout=1)
    assert exc.value.code is ErrorCode.SERVICE_UNREACHABLE


def test_hung_handler_is_service_unreachable_within_the_deadline() -> None:
    def slow(_m: ipc.Message) -> ipc.Message:
        time.sleep(3)
        return {"v": 1, "ok": True}

    with ipc.IpcServer(slow):
        start = time.monotonic()
        with pytest.raises(ScribeSenseError) as exc:
            ipc.request({"type": "reset"}, timeout=0.5)
        assert exc.value.code is ErrorCode.SERVICE_UNREACHABLE
        assert time.monotonic() - start < 2


def _raw(payload: bytes) -> bytes:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3)
    s.connect(str(paths.socket_path()))
    s.sendall(payload)
    s.shutdown(socket.SHUT_WR)
    data = s.recv(65536)
    s.close()
    return data


@pytest.mark.parametrize("payload", [b"not json\n", b'{"type": "x"}\n', b"[1,2]\n", b'{"v": 2, "type": "x"}\n'])
def test_malformed_requests_get_bad_message(payload: bytes) -> None:
    with ipc.IpcServer(echo):
        assert ipc.decode(_raw(payload).rstrip(b"\n")) == {"v": 1, "ok": False, "error": "BAD_MESSAGE"}


def test_oversized_request_gets_bad_message() -> None:
    with ipc.IpcServer(echo):
        reply = _raw(b"x" * (MAX_MESSAGE_BYTES + 10) + b"\n")
        assert ipc.decode(reply.rstrip(b"\n"))["error"] == "BAD_MESSAGE"


def test_encode_refuses_oversized_messages() -> None:
    with pytest.raises(ScribeSenseError) as exc:
        ipc.encode({"v": 1, "text": "x" * MAX_MESSAGE_BYTES})
    assert exc.value.code is ErrorCode.BAD_MESSAGE


def test_other_users_are_dropped_without_reply() -> None:
    with ipc.IpcServer(echo, allowed_uid=os.getuid() + 1):
        with pytest.raises(ScribeSenseError) as exc:
            ipc.request({"type": "ping"}, timeout=2)
        assert exc.value.code is ErrorCode.SERVICE_UNREACHABLE


def test_handler_errors_become_error_replies() -> None:
    def failing(_m: ipc.Message) -> ipc.Message:
        raise ScribeSenseError(ErrorCode.TX_NOT_PENDING)

    with ipc.IpcServer(failing):
        assert ipc.request({"type": "keep", "tx": 1}, timeout=2) == {"v": 1, "ok": False, "error": "TX_NOT_PENDING"}


def test_stale_socket_is_replaced_but_a_live_one_is_not() -> None:
    path = paths.socket_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()  # file left behind, nobody listening
    with ipc.IpcServer(echo):
        assert ipc.request({"type": "ping"}, timeout=2)["ok"] is True
        with pytest.raises(ScribeSenseError) as exc:
            ipc.IpcServer(echo).start()  # a second service must not steal a live socket
        assert exc.value.code is ErrorCode.APPLY_IN_PROGRESS
