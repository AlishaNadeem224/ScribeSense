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


# --- style_changed push (K11) ----------------------------------------------------------------------

import os  # noqa: E402
import select  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from scribesense.contracts.adapter import AppliedItem, PlannedWrite, SnapshotItem, Target, state_sha256  # noqa: E402
from scribesense.contracts.config import Configuration  # noqa: E402
from scribesense.contracts.journal import ActiveConfig  # noqa: E402
from scribesense.contracts.states import TxState  # noqa: E402


def _run_watcher(writer, stop, subscribe, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    """watch_style on a daemon thread with a deadline: a regression fails the test instead of hanging it."""
    t = threading.Thread(target=native_host.watch_style, args=(writer, stop), kwargs={"subscribe": subscribe},
                         daemon=True)
    t.start()
    t.join(timeout)
    stop.set()
    assert not t.is_alive(), "watch_style did not return"


def test_watcher_forwards_revisions_and_resubscribes_after_an_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_host, "WATCH_BACKOFF_MIN_S", 0.01)
    out, stop, calls = io.BytesIO(), threading.Event(), []

    def subscribe(*, timeout: float):  # type: ignore[no-untyped-def]
        calls.append(timeout)
        if len(calls) == 1:
            raise ScribeSenseError(ErrorCode.SERVICE_UNREACHABLE)  # the app is not running yet
        if len(calls) == 2:
            yield 3  # it started: greeting
            yield 4  # a Keep
            raise ScribeSenseError(ErrorCode.SERVICE_UNREACHABLE)  # it quit
        yield 5  # it is back
        stop.set()

    _run_watcher(native_host.FrameWriter(out), stop, subscribe)
    assert replies(out.getvalue()) == [{"v": 1, "type": "style_changed", "revision": n} for n in (3, 4, 5)]
    assert calls == [native_host.REQUEST_TIMEOUT_S] * 3


def test_watcher_stops_when_the_browser_is_gone() -> None:
    class Closed(io.BytesIO):
        def write(self, _b: bytes) -> int:  # type: ignore[override]
            raise BrokenPipeError

    def subscribe(*, timeout: float):  # type: ignore[no-untyped-def]
        while True:
            yield 1

    _run_watcher(native_host.FrameWriter(Closed()), threading.Event(), subscribe)  # returns on its own


def test_a_push_never_lands_inside_a_reply() -> None:
    class Slow(io.BytesIO):
        def write(self, b: bytes) -> int:  # type: ignore[override]
            n = 0
            for i in range(0, len(b), 3):  # dribble bytes so an unlocked writer would interleave
                n += super().write(b[i:i + 3])
                time.sleep(0)
            return n

    out = Slow()
    writer = native_host.FrameWriter(out)
    reply = {"v": 1, "ok": True, "active": True, "revision": 7, "css": "x" * 500}
    threads = [threading.Thread(target=lambda: [writer.write(reply) for _ in range(50)]),
               threading.Thread(target=lambda: [writer.write(ipc.style_changed(i)) for i in range(50)])]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    frames = replies(out.getvalue())
    assert len(frames) == 100 and frames.count(reply) == 50


def test_end_to_end_keep_pushes_style_changed_to_the_browser() -> None:
    """Real SQLite journal -> its on_style_changed hook -> real IPC server -> real host -> a browser frame."""
    from scribesense.safety.journal import SqliteJournal
    from scribesense.safety.lock import ApplyLock
    from scribesense.store import Store, default_path

    store = Store(default_path())
    holder: dict[str, ipc.IpcServer] = {}
    journal = SqliteJournal(store, ApplyLock(),
                            on_style_changed=lambda rev: holder["server"].publish_style_changed(rev))

    def service(message: ipc.Message) -> ipc.Message:
        active = journal.active()
        if active is None:
            return {"v": 1, "ok": True, "active": False, "revision": journal.style_revision()}
        return {"v": 1, "ok": True, "active": True, "revision": active.style_revision, "css": "p{}"}

    stdin_r, stdin_w = os.pipe()
    stdout_r, stdout_w = os.pipe()
    host_in, host_out = os.fdopen(stdin_r, "rb", buffering=0), os.fdopen(stdout_w, "wb", buffering=0)
    browser_in, browser_out = os.fdopen(stdout_r, "rb", buffering=0), os.fdopen(stdin_w, "wb", buffering=0)

    def next_frame() -> dict:
        assert select.select([browser_in], [], [], 5.0)[0], "no frame from the host within 5 s"
        return json.loads(native_host.read_frame(browser_in))

    with ipc.IpcServer(service, style_revision=journal.style_revision) as server:
        holder["server"] = server
        host = threading.Thread(target=native_host.serve, daemon=True, args=(host_in, host_out),
                                kwargs={"subscribe": ipc.subscribe_style})
        host.start()
        try:
            assert next_frame() == {"v": 1, "type": "style_changed", "revision": 0}  # greeting
            browser_out.write(frame({"v": 1, "type": "get_style"}))
            assert next_frame() == {"v": 1, "ok": True, "active": False, "revision": 0}

            t = Target("fake", "t", "Test", False)
            tx = journal.begin("apply", "default")
            item = journal.record_snapshot(tx, SnapshotItem(t, "key", "org.x font-name", True, "'A 11'",
                                                            state_sha256(True, b"'A 11'"), None, "s"), None)
            for state in (TxState.SNAPSHOTTED, TxState.APPLYING):
                journal.set_state(tx, state)
            journal.record_intent(tx, item, PlannedWrite(t, "org.x font-name", "'B 11'", state_sha256(True, b"'B 11'")))
            journal.record_applied(tx, item, AppliedItem(t, "org.x font-name", state_sha256(True, b"'B 11'")))
            journal.set_state(tx, TxState.VERIFYING)
            journal.mark_retained(tx, item)
            journal.set_state(tx, TxState.AWAITING_CONFIRMATION)
            journal.keep(tx, ActiveConfig(Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.0), "abcabcabcabc", "default", 0))

            assert next_frame() == {"v": 1, "type": "style_changed", "revision": 1}  # pushed, no polling
            browser_out.write(frame({"v": 1, "type": "get_style"}))
            assert next_frame() == {"v": 1, "ok": True, "active": True, "revision": 1, "css": "p{}"}
            journal.set_original()
            assert next_frame() == {"v": 1, "type": "style_changed", "revision": 2}
        finally:
            browser_out.close()  # the browser closes the port: the host exits
            host.join(timeout=5)
            for f in (host_in, host_out, browser_in):
                f.close()
    assert not host.is_alive()


class _RecordingStop:
    """A stop event whose wait() records the backoff delays instead of sleeping."""

    def __init__(self, after: int) -> None:
        self.delays: list[float] = []
        self._event = threading.Event()
        self._after = after

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, delay: float) -> bool:
        self.delays.append(delay)
        if len(self.delays) >= self._after:
            self._event.set()
        return self._event.is_set()


def test_watcher_backoff_doubles_to_the_cap_and_restarts_after_a_success() -> None:
    calls = {"n": 0}

    def subscribe(*, timeout: float):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 8:
            yield 1  # one successful subscription in the middle ...
        raise ScribeSenseError(ErrorCode.SERVICE_UNREACHABLE)

    stop = _RecordingStop(after=10)
    native_host.watch_style(native_host.FrameWriter(io.BytesIO()), stop, subscribe=subscribe)  # type: ignore[arg-type]
    assert stop.delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 1.0, 2.0, 4.0]  # ... resets the backoff


@pytest.mark.parametrize("error", [BlockingIOError(11, "backlog full"), PermissionError(13, "denied"), RuntimeError("bug")])
def test_watcher_survives_socket_errors_and_bugs_and_retries(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    monkeypatch.setattr(native_host, "WATCH_BACKOFF_MIN_S", 0.01)
    out, stop, calls = io.BytesIO(), threading.Event(), []

    def subscribe(*, timeout: float):  # type: ignore[no-untyped-def]
        calls.append(1)
        if len(calls) == 1:
            raise error  # not "the browser is gone": the watcher must keep going
        yield 7
        stop.set()

    _run_watcher(native_host.FrameWriter(out), stop, subscribe)
    assert replies(out.getvalue()) == [{"v": 1, "type": "style_changed", "revision": 7}]


def test_main_starts_the_push_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(native_host, "serve", lambda *a, **kw: seen.update(kw) or 0)
    assert native_host.main(["host", "chrome-extension://x/"]) == 0
    assert seen["subscribe"] is ipc.subscribe_style

