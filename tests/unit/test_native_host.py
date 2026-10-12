"""M4.9 / K11 — native-messaging host bridge (framing, limits, forwarding, host manifest)."""

from __future__ import annotations

import io
import json
import struct

import pytest

from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.messages import MAX_MESSAGE_BYTES, MAX_READER_CHARS
from scribesense.log import log_path
from scribesense.safety import ipc, native_host

EXT_ID = "abcdefghijklmnopabcdefghijklmnop"


@pytest.fixture(autouse=True)
def short_runtime_dir(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """Socket paths are limited to 108 bytes; tmp_path is too long. Same approach as test_ipc.py."""
    runtime = sandbox.data_dir.parent / "run"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))


def frame(obj: object) -> bytes:
    data = json.dumps(obj).encode() if not isinstance(obj, bytes) else obj
    return struct.pack("=I", len(data)) + data


def replies(raw: bytes) -> list[dict]:
    out, stream = [], io.BytesIO(raw)
    while (payload := native_host.read_frame(stream)) is not None:
        out.append(json.loads(payload))
    return out


def run_host(stdin: bytes, send=None) -> list[dict]:  # type: ignore[no-untyped-def]
    out = io.BytesIO()
    kwargs = {} if send is None else {"send": send}
    assert native_host.serve(io.BytesIO(stdin), out, **kwargs) == 0
    return replies(out.getvalue())


class FakeService:
    def __init__(self, reply: dict | None = None, exc: BaseException | None = None) -> None:
        self.reply = reply if reply is not None else {"v": 1, "ok": True}
        self.exc = exc
        self.calls: list[tuple[dict, float]] = []

    def __call__(self, message: dict, *, timeout: float) -> dict:
        self.calls.append((message, timeout))
        if self.exc is not None:
            raise self.exc
        return self.reply


# --- framing ---------------------------------------------------------------------------------------

def test_frame_round_trip_uses_native_order_length() -> None:
    msg = {"v": 1, "ok": True, "active": True, "revision": 7, "css": "x { a: b }", "t": "é✓"}
    data = native_host.encode_frame(msg)
    body = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode()
    assert data[:4] == struct.pack("=I", len(body))
    assert data[4:] == body
    assert native_host.read_frame(io.BytesIO(data)) == body


def test_clean_end_of_stream_is_none() -> None:
    assert native_host.read_frame(io.BytesIO(b"")) is None


@pytest.mark.parametrize("raw", [b"\x05\x00", struct.pack("=I", 10) + b"abc"])
def test_truncated_frames_are_bad_message(raw: bytes) -> None:
    with pytest.raises(ScribeSenseError) as exc:
        native_host.read_frame(io.BytesIO(raw))
    assert exc.value.code is ErrorCode.BAD_MESSAGE


def test_oversized_request_is_bad_message_and_stream_stays_in_sync() -> None:
    service = FakeService({"v": 1, "ok": True, "active": False, "revision": 3})
    big = b"x" * (MAX_MESSAGE_BYTES + 1)
    out = run_host(frame(big) + frame({"v": 1, "type": "get_style"}), send=service)
    assert out == [{"v": 1, "ok": False, "error": "BAD_MESSAGE"}, {"v": 1, "ok": True, "active": False, "revision": 3}]
    assert len(service.calls) == 1


def test_message_of_exactly_the_limit_is_accepted_by_the_framing() -> None:
    body = b"y" * MAX_MESSAGE_BYTES
    assert native_host.read_frame(io.BytesIO(frame(body))) == body


def test_encode_refuses_oversized_reply() -> None:
    with pytest.raises(ScribeSenseError) as exc:
        native_host.encode_frame({"v": 1, "css": "x" * MAX_MESSAGE_BYTES})
    assert exc.value.code is ErrorCode.BAD_MESSAGE


def test_oversized_service_reply_becomes_bad_message() -> None:
    service = FakeService({"v": 1, "ok": True, "active": True, "revision": 1, "css": "x" * MAX_MESSAGE_BYTES})
    assert run_host(frame({"v": 1, "type": "get_style"}), send=service) == [
        {"v": 1, "ok": False, "error": "BAD_MESSAGE"}]


# --- request validation ----------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    b"not json",
    b"\xff\xfe",
    b"[1, 2]",
    b'{"type": "get_style"}',
    b'{"v": 2, "type": "get_style"}',
    b'{"v": true, "type": "get_style"}',
    b'{"v": 1, "type": "reset"}',
    b'{"v": 1, "type": "keep", "tx": 1}',
    b'{"v": 1}',
    b'{"v": 1, "type": "open_in_reader"}',
    b'{"v": 1, "type": "open_in_reader", "text": 5}',
    b'{"v": 1, "type": "open_in_reader", "text": "\\ud800"}',
])
def test_bad_requests_get_bad_message_and_never_reach_the_service(payload: bytes) -> None:
    service = FakeService()
    assert run_host(frame(payload), send=service) == [{"v": 1, "ok": False, "error": "BAD_MESSAGE"}]
    assert service.calls == []


def test_only_k11_fields_are_forwarded() -> None:
    service = FakeService()
    run_host(frame({"v": 1, "type": "get_style", "extra": "x"})
             + frame({"v": 1, "type": "open_in_reader", "text": "hello", "url": "x"}), send=service)
    assert [m for m, _ in service.calls] == [{"type": "get_style"}, {"type": "open_in_reader", "text": "hello"}]
    assert all(t == native_host.REQUEST_TIMEOUT_S for _, t in service.calls)


def test_reader_text_limit_is_enforced_without_truncation() -> None:
    service = FakeService()
    ok = "a" * MAX_READER_CHARS
    too_long = "a" * (MAX_READER_CHARS + 1)
    out = run_host(frame({"v": 1, "type": "open_in_reader", "text": ok})
                   + frame({"v": 1, "type": "open_in_reader", "text": too_long}), send=service)
    assert out == [{"v": 1, "ok": True}, {"v": 1, "ok": False, "error": "TOO_LARGE"}]
    assert [len(m["text"]) for m, _ in service.calls] == [MAX_READER_CHARS]


# --- forwarding ------------------------------------------------------------------------------------

def test_unreachable_service_is_app_not_running() -> None:
    """Real ipc.request against an empty runtime dir: nobody is listening."""
    assert run_host(frame({"v": 1, "type": "get_style"})) == [{"v": 1, "ok": False, "error": "APP_NOT_RUNNING"}]


@pytest.mark.parametrize("exc", [ScribeSenseError(ErrorCode.SERVICE_UNREACHABLE), PermissionError()])
def test_service_failures_map_to_app_not_running(exc: BaseException) -> None:
    out = run_host(frame({"v": 1, "type": "get_style"}), send=FakeService(exc=exc))
    assert out == [{"v": 1, "ok": False, "error": "APP_NOT_RUNNING"}]


def test_bad_service_reply_is_bad_message() -> None:
    out = run_host(frame({"v": 1, "type": "get_style"}), send=FakeService(exc=ScribeSenseError(ErrorCode.BAD_MESSAGE)))
    assert out == [{"v": 1, "ok": False, "error": "BAD_MESSAGE"}]


def test_round_trip_against_a_real_ipc_server() -> None:
    seen: list[dict] = []
    revision = {"n": 6}

    def service(message: ipc.Message) -> ipc.Message:
        seen.append(message)
        if message["type"] == "get_style":
            revision["n"] += 1
            return {"v": 1, "ok": True, "active": True, "revision": revision["n"], "css": ":root { line-height: 1.5 }"}
        return {"v": 1, "ok": True}

    with ipc.IpcServer(service):
        out = run_host(frame({"v": 1, "type": "get_style"})
                       + frame({"v": 1, "type": "open_in_reader", "text": "Zwölf Boxkämpfer"})
                       + frame({"v": 1, "type": "get_style"}))
    assert out == [
        {"v": 1, "ok": True, "active": True, "revision": 7, "css": ":root { line-height: 1.5 }"},
        {"v": 1, "ok": True},
        {"v": 1, "ok": True, "active": True, "revision": 8, "css": ":root { line-height: 1.5 }"},
    ]
    assert seen == [{"v": 1, "type": "get_style"},
                    {"v": 1, "type": "open_in_reader", "text": "Zwölf Boxkämpfer"},
                    {"v": 1, "type": "get_style"}]


def test_message_content_is_never_logged() -> None:
    secret = "MySecretDocumentSentence"
    run_host(frame({"v": 1, "type": "open_in_reader", "text": secret})          # APP_NOT_RUNNING
             + frame({"v": 1, "type": "open_in_reader", "text": secret * 20000})  # TOO_LARGE
             + frame(b'{"v": 1, "type": "' + secret.encode() + b'"}'))          # BAD_MESSAGE
    logged = log_path().read_text()
    assert "APP_NOT_RUNNING" in logged and "TOO_LARGE" in logged and "BAD_MESSAGE" in logged
    assert secret not in logged


# --- host manifest ---------------------------------------------------------------------------------

def test_host_manifest_content() -> None:
    text = native_host.render_host_manifest(EXT_ID, "/opt/scribesense/bin/scribesense-native-host")
    assert json.loads(text) == {
        "name": "com.scribesense.host",
        "description": native_host.HOST_DESCRIPTION,
        "path": "/opt/scribesense/bin/scribesense-native-host",
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{EXT_ID}/"],
    }
    assert native_host.HOST_NAME == "com.scribesense.host"


@pytest.mark.parametrize("ext_id", ["", "ABCDEFGHIJKLMNOPABCDEFGHIJKLMNOP", "abcdefghijklmnopabcdefghijklmnoq",
                                    "abcdefghijklmnop", "abcdefghijklmnopabcdefghijklmnop/",
                                    "abcdefghijklmnopabcdefghijklmnop\n"])
def test_host_manifest_rejects_bad_extension_ids(ext_id: str) -> None:
    with pytest.raises(ValueError):
        native_host.render_host_manifest(ext_id, "/usr/bin/x")


def test_host_manifest_requires_an_absolute_path() -> None:
    with pytest.raises(ValueError):
        native_host.render_host_manifest(EXT_ID, "bin/host")
