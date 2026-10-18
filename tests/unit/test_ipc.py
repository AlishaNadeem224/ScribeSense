"""K4 rule 5 — local IPC socket."""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from scribesense.contracts import paths
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.messages import MAX_MESSAGE_BYTES
from scribesense.safety import ipc


@pytest.fixture(autouse=True)
def short_runtime_dir(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """Socket paths are limited to 108 bytes; tmp_path is too long. Use the fixture's short dir."""
    runtime = sandbox.data_dir.parent / "run"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))


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


# --- style subscription (K11 push; K4 rule 5 addition 2026-10-18) -----------------------------------

def _next(stream, timeout: float = 3.0):  # type: ignore[no-untyped-def]
    """_next(stream) with a deadline: a broken push must fail the test, never hang it."""
    box: list = []

    def run() -> None:
        try:
            box.append(("ok", next(stream)))
        except BaseException as exc:  # noqa: BLE001 — re-raised below
            box.append(("err", exc))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    assert box, f"no message within {timeout} s"
    kind, value = box[0]
    if kind == "err":
        raise value
    return value


def _until(predicate, timeout: float = 2.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_subscription_greets_with_the_current_revision_then_delivers_every_push_in_order() -> None:
    revision = {"n": 4}
    with ipc.IpcServer(echo, style_revision=lambda: revision["n"]) as server:
        stream = ipc.subscribe_style(timeout=2)
        assert _next(stream) == 4  # the greeting: the current confirmed revision
        assert _until(lambda: server.subscriber_count == 1)
        for n in (5, 6, 7):  # back-to-back: several lines may arrive in one recv()
            assert server.publish_style_changed(n) == 1
        assert [_next(stream), _next(stream), _next(stream)] == [5, 6, 7]
        stream.close()


def test_requests_are_still_served_while_subscriptions_are_open() -> None:
    with ipc.IpcServer(echo, style_revision=lambda: 1) as server:
        streams = [ipc.subscribe_style(timeout=2) for _ in range(3)]
        assert [_next(s) for s in streams] == [1, 1, 1]
        assert ipc.request({"type": "ping"}, timeout=2) == {"v": 1, "ok": True, "type": "ping"}
        assert server.publish_style_changed(2) == 3
        assert [_next(s) for s in streams] == [2, 2, 2]
        for s in streams:
            s.close()


def test_a_server_without_style_revision_offers_no_subscription_and_never_asks_the_handler() -> None:
    calls: list[ipc.Message] = []

    def recording(message: ipc.Message) -> ipc.Message:
        calls.append(message)
        return echo(message)

    with ipc.IpcServer(recording):
        with pytest.raises(ScribeSenseError) as exc:
            _next(ipc.subscribe_style(timeout=2))
    assert exc.value.code is ErrorCode.BAD_MESSAGE
    assert calls == []  # subscribe_style is the server's own message, never the handler's


def test_no_service_means_service_unreachable_for_subscribers_too() -> None:
    with pytest.raises(ScribeSenseError) as exc:
        _next(ipc.subscribe_style(timeout=1))
    assert exc.value.code is ErrorCode.SERVICE_UNREACHABLE


def test_stopping_the_service_ends_every_subscription() -> None:
    server = ipc.IpcServer(echo, style_revision=lambda: 1)
    server.start()
    stream = ipc.subscribe_style(timeout=2)
    assert _next(stream) == 1
    server.stop()
    with pytest.raises(ScribeSenseError) as exc:
        _next(stream)
    assert exc.value.code is ErrorCode.SERVICE_UNREACHABLE


def test_gone_subscribers_are_dropped_and_the_cap_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ipc, "MAX_SUBSCRIBERS", 2)
    with ipc.IpcServer(echo, style_revision=lambda: 1) as server:
        a, b = ipc.subscribe_style(timeout=2), ipc.subscribe_style(timeout=2)
        assert _next(a) == 1 and _next(b) == 1
        with pytest.raises(ScribeSenseError) as exc:  # a third one is over the cap
            _next(ipc.subscribe_style(timeout=2))
        assert exc.value.code is ErrorCode.BAD_MESSAGE
        a.close()  # this browser's host went away
        c = ipc.subscribe_style(timeout=2)  # the closed one is pruned, so there is room again
        assert _next(c) == 1
        assert _until(lambda: server.publish_style_changed(2) == 2)
        b.close()
        c.close()


def test_a_subscriber_that_cannot_be_written_is_dropped_and_publish_never_raises() -> None:
    class Broken:
        def send(self, _data: bytes, _flags: int = 0) -> int:
            raise BrokenPipeError

        def close(self) -> None:
            pass

    with ipc.IpcServer(echo, style_revision=lambda: 1) as server:
        server._subscribers.append(Broken())  # type: ignore[arg-type]
        assert server.publish_style_changed(9) == 0 and server.subscriber_count == 0
        assert server.publish_style_changed(True) == 0  # type: ignore[arg-type]  # not a revision: nothing sent


def test_style_changed_messages_are_validated() -> None:
    assert ipc.style_changed(3) == {"v": 1, "type": "style_changed", "revision": 3}
    for bad in (True, 3.0, "3", None):
        with pytest.raises(ScribeSenseError):
            ipc.style_changed(bad)  # type: ignore[arg-type]
    for msg in ({"v": 1, "type": "style_changed", "revision": True}, {"v": 1, "ok": True}):
        with pytest.raises(ScribeSenseError):
            ipc._revision_of(msg)


def test_a_subscriber_that_does_not_read_is_dropped_without_ever_blocking_publish() -> None:
    with ipc.IpcServer(echo, style_revision=lambda: 1) as server:
        stuck = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stuck.connect(str(paths.socket_path()))
        stuck.sendall(b'{"v":1,"type":"subscribe_style"}\n')  # ... and never reads again
        assert _until(lambda: server.subscriber_count == 1)
        started, n, slowest = time.monotonic(), 2, 0.0
        while server.subscriber_count and n < 100_000:  # its buffer fills after a few dozen lines
            t0 = time.monotonic()
            server.publish_style_changed(n)
            slowest, n = max(slowest, time.monotonic() - t0), n + 1
        assert server.subscriber_count == 0  # dropped once a line no longer fit
        assert slowest < 0.2 and time.monotonic() - started < 5  # a Keep is never held up
        done = threading.Event()
        threading.Thread(target=lambda: (server.stop(), done.set()), daemon=True).start()
        assert done.wait(3)  # and stop() does not hang on it either
        stuck.close()


def test_a_subscribe_that_arrives_while_stopping_is_refused_and_closed() -> None:
    server = ipc.IpcServer(echo, style_revision=lambda: 7)
    server.start()
    late = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    late.connect(str(paths.socket_path()))  # the IPC thread now waits for this client's first line
    server.stop()  # gives up joining that thread after 2 s and clears the subscribers
    late.settimeout(3)
    late.sendall(b'{"v":1,"type":"subscribe_style"}\n')
    assert late.recv(100) == b""  # refused and closed (EOF), not registered with a stopped server
    assert server.subscriber_count == 0
    late.close()


def test_line_reader_limits() -> None:
    a, b = socket.socketpair()
    with a, b:
        b.sendall(b'{"v":1,"type":"style_changed","revision":1}\n{"v":1')
        b.close()
        reader = ipc._LineReader(a)
        assert reader.next_line() == b'{"v":1,"type":"style_changed","revision":1}'
        with pytest.raises(ScribeSenseError):  # EOF in the middle of a line
            reader.next_line()
    a, b = socket.socketpair()
    with a, b:
        b.close()
        assert ipc._LineReader(a).next_line() is None  # clean EOF at a line boundary
    a, b = socket.socketpair()
    with a, b:
        sender = threading.Thread(target=lambda: b.sendall(b"x" * (MAX_MESSAGE_BYTES + 10)), daemon=True)
        sender.start()
        with pytest.raises(ScribeSenseError) as exc:
            ipc._LineReader(a).next_line()
        assert exc.value.code is ErrorCode.BAD_MESSAGE
        a.close()
        sender.join(3)


def test_every_socket_error_while_subscribing_is_service_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self, _addr):  # type: ignore[no-untyped-def]
        raise BlockingIOError(11, "listen backlog full")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    with pytest.raises(ScribeSenseError) as exc:
        next(ipc.subscribe_style(timeout=1))
    assert exc.value.code is ErrorCode.SERVICE_UNREACHABLE


def test_a_subscriber_that_sends_anything_and_closes_frees_its_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ipc, "MAX_SUBSCRIBERS", 1)
    with ipc.IpcServer(echo, style_revision=lambda: 1) as server:
        rogue = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        rogue.connect(str(paths.socket_path()))
        rogue.sendall(b'{"v":1,"type":"subscribe_style"}\n')
        assert _until(lambda: server.subscriber_count == 1)
        rogue.sendall(b"x")  # a subscriber must never send after subscribing ...
        rogue.close()
        stream = ipc.subscribe_style(timeout=2)
        assert _next(stream) == 1  # ... so its slot is freed for the next real subscriber
        stream.close()


def test_socket_creation_errors_are_service_unreachable_too(monkeypatch: pytest.MonkeyPatch) -> None:
    def emfile(*_a, **_k):  # type: ignore[no-untyped-def]
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(ipc.socket, "socket", emfile)
    with pytest.raises(ScribeSenseError) as exc:
        next(ipc.subscribe_style(timeout=1))
    assert exc.value.code is ErrorCode.SERVICE_UNREACHABLE

