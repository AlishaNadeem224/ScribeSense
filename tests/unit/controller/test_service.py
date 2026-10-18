"""The service wiring (controller/service.py): real store + SQLite journal + IPC socket + controller, fake adapters.

Fonts are faked (no base font is bundled yet); everything else is the production code path.
"""

from __future__ import annotations

import pytest

from scribesense.adapters.fake import FakeAdapter, FakeWorld
from scribesense.config.presets import BUILTINS
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import FontBuildResult
from scribesense.contracts.states import TxState
from scribesense.controller.service import Service
from scribesense.safety import ipc
from scribesense.safety.testing import fake_fontset

FILE = "/fake/fc.conf"


@pytest.fixture(autouse=True)
def short_runtime_dir(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    runtime = sandbox.data_dir.parent / "run"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))


@pytest.fixture
def world() -> FakeWorld:
    w = FakeWorld()
    w.set_file(FILE, b"orig")
    return w


@pytest.fixture
def service(world: FakeWorld):  # type: ignore[no-untyped-def]
    svc = Service.open(adapters=[FakeAdapter(world, {"fc": [FILE]}, name="fontconfig")])
    svc.controller._generator = lambda c: FontBuildResult("built", fake_fontset(c), None)
    yield svc
    svc.close()


def test_apply_then_keep_over_the_socket_pushes_and_serves_the_confirmed_style(service, world) -> None:  # type: ignore[no-untyped-def]
    pushes = ipc.subscribe_style(timeout=2)
    assert next(pushes) == 0
    report = service.apply(BUILTINS[1])
    assert report.status == "awaiting_confirmation" and service.pending_tx == report.tx
    assert world.state(FILE).data != b"orig"
    assert ipc.request({"type": "pending"}, timeout=2) == {"v": 1, "ok": True, "tx": report.tx}
    assert ipc.request({"type": "get_style"}, timeout=2)["active"] is False  # not confirmed yet (K11)
    assert ipc.request({"type": "keep", "tx": report.tx}, timeout=2) == {"v": 1, "ok": True, "revision": 1}
    assert next(pushes) == 1  # K11 push after Keep
    assert service.journal.state(report.tx) is TxState.COMPLETED and service.pending_tx is None
    pushes.close()


def test_revert_restores_and_a_stale_tx_is_refused(service, world) -> None:  # type: ignore[no-untyped-def]
    report = service.apply(BUILTINS[2])
    reply = ipc.request({"type": "revert", "tx": report.tx}, timeout=2)
    assert reply == {"v": 1, "ok": True, "failed": 0} and world.state(FILE).data == b"orig"
    assert ipc.request({"type": "keep", "tx": report.tx}, timeout=2) == {"v": 1, "ok": False, "error": "TX_NOT_PENDING"}


def test_reset_over_the_socket_returns_everything_to_original(service, world) -> None:  # type: ignore[no-untyped-def]
    service.keep(service.apply(BUILTINS[1]).tx)
    assert ipc.request({"type": "reset"}, timeout=5) == {"v": 1, "ok": True, "restored": 1, "skipped": 0, "failed": 0}
    assert world.state(FILE).data == b"orig" and service.journal.active() is None


def test_confirmation_watchdog_reverts_an_unanswered_apply(service, world) -> None:  # type: ignore[no-untyped-def]
    report = service.apply(BUILTINS[1])
    assert service.confirm_watchdog(report.tx) is True and world.state(FILE).data == b"orig"
    kept = service.apply(BUILTINS[1])
    service.keep(kept.tx)
    assert service.confirm_watchdog(kept.tx) is False  # already kept: the watchdog leaves it alone


def test_reader_text_reaches_the_app_and_a17_is_enforced(service) -> None:  # type: ignore[no-untyped-def]
    got: list[str] = []
    service.on_reader_text = got.append
    assert ipc.request({"type": "open_in_reader", "text": "Zwölf"}, timeout=2) == {"v": 1, "ok": True}
    assert got == ["Zwölf"]
    big = ipc.request({"type": "open_in_reader", "text": "x" * 200_001}, timeout=2)
    assert big == {"v": 1, "ok": False, "error": "TOO_LARGE"} and got == ["Zwölf"]


def test_unknown_requests_are_bad_message(service) -> None:  # type: ignore[no-untyped-def]
    assert ipc.request({"type": "confirm_all"}, timeout=2)["error"] == "BAD_MESSAGE"
    assert ipc.request({"type": "keep", "tx": "1"}, timeout=2)["error"] == "BAD_MESSAGE"


def test_keep_without_a_pending_tx_is_refused(service) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ScribeSenseError) as exc:
        service.keep(1)
    assert exc.value.code is ErrorCode.TX_NOT_PENDING
