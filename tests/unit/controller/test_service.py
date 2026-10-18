"""The service wiring (controller/service.py): real store + SQLite journal + IPC socket + controller, fake adapters.

Fonts are faked (no base font is bundled yet); everything else is the production code path.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from scribesense.adapters.fake import FakeAdapter, FakeWorld
from scribesense.config.presets import BUILTINS, custom_preset, new_user_preset
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import FontBuildResult
from scribesense.contracts.journal import NoRecoveryObligations
from scribesense.contracts.presets import PresetValues, WordSpacing
from scribesense.contracts.states import TxState
from scribesense.controller.service import Service
from scribesense.safety import ipc
from scribesense.safety.lock import ApplyLock
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


def test_reset_during_the_confirmation_countdown_reverts_the_pending_apply_first(service, world) -> None:  # type: ignore[no-untyped-def]
    """A `reset` / `uninstall` asking the service while a change awaits Keep must get a real reset, not an error
    (an error made recovery take the live app over, K4 rule 6)."""
    report = service.apply(BUILTINS[1])
    assert service.pending_tx == report.tx
    reply = ipc.request({"type": "reset"}, timeout=5)
    assert reply["ok"] is True and reply["failed"] == 0
    assert world.state(FILE).data == b"orig" and service.pending_tx is None
    assert service.journal.unfinished() == []



# ---------------------------------------------------------------- start-up font clean-up (K7 rule 3, K9 D3)

CUSTOM_KEY = "cccccccccccc"
BUILTIN_KEY = "bbbbbbbbbbbb"
STRAY_KEY = "0123456789ab"
CUSTOM_VALUES = PresetValues("Noto Sans", 0.28, WordSpacing("multiplier", 2.0), 1.4, 1.2, 1.0)


def test_startup_clean_up_keeps_the_font_of_the_stored_custom_row(world, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """O3's D3 note: the clean-up must read `presets.list_all()`. `list()` stops returning `custom` (D3), simulated
    here, so a service still reading `list()` would delete the font of the user's own current configuration."""
    from scribesense.fontgen import generator, variants
    from scribesense.store import Store, default_path
    from scribesense.store.repos import PresetRepo

    store = Store(default_path())
    store.presets.save(custom_preset(CUSTOM_VALUES))
    store.close()

    installed = [CUSTOM_KEY, BUILTIN_KEY, STRAY_KEY]
    monkeypatch.setattr(generator.install_cache, "installed_keys", lambda: list(installed))
    monkeypatch.setattr(generator.install_cache, "remove", lambda key, tools=None: installed.remove(key) is None)
    # bundled_font_key() -> variants.plan(config).key: the custom values get their own key, built-ins share one
    monkeypatch.setattr(variants, "plan", lambda config, **_kw: SimpleNamespace(
        key=CUSTOM_KEY if config.letter_em == CUSTOM_VALUES.letter_em else BUILTIN_KEY))
    all_rows = PresetRepo.list_all
    monkeypatch.setattr(PresetRepo, "list", lambda self: [p for p in all_rows(self) if p.kind != "custom"])

    svc = Service.open(adapters=[FakeAdapter(world, {"fc": [FILE]}, name="fontconfig")], start_server=False)
    try:
        assert installed == [CUSTOM_KEY, BUILTIN_KEY]  # only the stray font went
    finally:
        svc.close()


# ---------------------------------------------------------------- delete all data (M2.7 / K9, O3's note 3)

def _save_preferences(svc: Service) -> None:
    svc.store.presets.save(new_user_preset("Mine", CUSTOM_VALUES))
    svc.store.settings.set("recommend_opt_in", True)


def _preferences_deleted(svc: Service) -> bool:
    return (all(p.kind == "builtin" for p in svc.store.presets.list_all())
            and svc.store.settings.get("recommend_opt_in") is False)


def test_delete_all_data_with_nothing_left_to_restore_deletes_everything_and_releases_the_lock(service, world) -> None:  # type: ignore[no-untyped-def]
    service.keep(service.apply(BUILTINS[1]).tx)
    service.original()
    _save_preferences(service)
    assert service.delete_all_data() is True
    assert _preferences_deleted(service) and service.journal.locations() == []
    assert not service.lock.held and not ApplyLock().is_locked()  # released: the next apply can run
    service.keep(service.apply(BUILTINS[1]).tx)


def test_delete_all_data_keeps_the_recovery_records_while_a_change_is_active(service, world) -> None:  # type: ignore[no-untyped-def]
    service.keep(service.apply(BUILTINS[1]).tx)
    _save_preferences(service)
    assert service.delete_all_data() is False
    assert _preferences_deleted(service)
    assert service.journal.active() is not None and service.journal.no_obligations() is None
    assert service.original().restored and world.state(FILE).data == b"orig"  # reset still works
    assert not service.lock.held


def test_delete_all_data_store_refusal_of_the_token_means_records_kept_not_an_error(service, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The store re-checks the token in its transaction; RECOVERY_OBLIGATIONS_REMAIN -> "kept" (False)."""
    service.keep(service.apply(BUILTINS[1]).tx)
    _save_preferences(service)
    monkeypatch.setattr(service.journal, "no_obligations", lambda: NoRecoveryObligations())  # a stale token
    assert service.delete_all_data() is False
    assert _preferences_deleted(service) and service.journal.active() is not None
    assert not service.lock.held


def test_delete_all_data_is_refused_while_a_change_awaits_confirmation(service) -> None:  # type: ignore[no-untyped-def]
    report = service.apply(BUILTINS[1])
    _save_preferences(service)
    with pytest.raises(ScribeSenseError) as exc:
        service.delete_all_data()
    assert exc.value.code is ErrorCode.APPLY_IN_PROGRESS
    assert not _preferences_deleted(service)  # refused before anything was deleted
    assert service.pending_tx == report.tx and service.lock.held  # the pending apply keeps its lock


def test_delete_all_data_is_refused_while_another_process_holds_the_apply_lock(service) -> None:  # type: ignore[no-untyped-def]
    _save_preferences(service)
    other = ApplyLock()
    assert other.try_acquire("reset")
    try:
        with pytest.raises(ScribeSenseError) as exc:
            service.delete_all_data()
        assert exc.value.code is ErrorCode.APPLY_IN_PROGRESS
        assert not _preferences_deleted(service) and not service.lock.held
    finally:
        other.release()


def test_delete_all_data_holds_the_lock_during_the_deletion_and_releases_it_on_error(service, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: list[bool] = []

    def failing_delete() -> None:
        seen.append(service.lock.held)
        raise ScribeSenseError(ErrorCode.STORE_IO_ERROR)

    monkeypatch.setattr(service.store, "delete_preferences", failing_delete)
    with pytest.raises(ScribeSenseError) as exc:
        service.delete_all_data()
    assert exc.value.code is ErrorCode.STORE_IO_ERROR
    assert seen == [True] and not service.lock.held


def test_delete_all_data_is_refused_at_once_while_another_thread_holds_the_service_mutex(service, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """An apply / revert / reset on a worker thread holds the mutex; delete (GTK main thread) must not wait for it.

    Without the mutex check the deletion would run beside the apply (and race it for the non-re-entrant ApplyLock);
    with a blocking mutex the caller would freeze until the worker finished.
    """
    _save_preferences(service)
    lock_calls: list[str] = []
    real_try_acquire = service.lock.try_acquire
    monkeypatch.setattr(service.lock, "try_acquire", lambda owner: lock_calls.append(owner) or real_try_acquire(owner))
    held, release = threading.Event(), threading.Event()

    def busy_worker() -> None:
        with service._mutex:
            held.set()
            release.wait(10)

    worker = threading.Thread(target=busy_worker, daemon=True)
    worker.start()
    outcome: list[object] = []
    try:
        assert held.wait(5)
        caller = threading.Thread(target=lambda: outcome.append(_catch(service.delete_all_data)), daemon=True)
        caller.start()
        caller.join(2)
        assert not caller.is_alive(), "delete_all_data() waited on the busy service mutex"
    finally:
        release.set()
        worker.join(5)
    assert len(outcome) == 1 and isinstance(outcome[0], ScribeSenseError)
    assert outcome[0].code is ErrorCode.APPLY_IN_PROGRESS
    assert lock_calls == [] and not _preferences_deleted(service)  # nothing touched while the mutex was busy
    assert service.delete_all_data() is True  # mutex released by the refusal path: the next delete runs
    assert lock_calls == ["service"] and not service.lock.held


def _catch(call):  # type: ignore[no-untyped-def]
    try:
        return call()
    except ScribeSenseError as exc:
        return exc
