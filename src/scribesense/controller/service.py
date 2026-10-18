"""O2 — The background service's wiring (M2.4 / M2.1 / M2.3 / K4 rule 5 / K11). No GTK here.

The single-instance app (A1) owns one `Service` for its lifetime. It is the composition root that every other
piece plugs into:

    store      store.Store(default_path())                       — K9, migrated on open
    lock       safety.lock.ApplyLock()                           — K4 rule 1, held by the journal per tx
    server     safety.ipc.IpcServer(handle, style_revision=…)    — K4 rule 5 socket (+ style push, K11)
    journal    safety.journal.SqliteJournal(store, lock, "service", on_style_changed=server.publish_style_changed)
    adapters   adapters.registry.default_adapters()              — K3, fontconfig first
    controller controller.apply_flow.ApplyController(…)          — M2.1

Socket requests the service answers (one JSON object per line, same user only):

    get_style       K11: the CONFIRMED style for the browser extension (M4.8 CSS), or inactive
    open_in_reader  K11: text from the extension -> the app's reader (A17 checked by the native host and here)
    keep / revert   M2.3: from the fresh `scribesense confirm <tx>` process (A15); only the pending tx
    reset           K4 rule 6: `scribesense reset` asks a running service to reset
    pending         M2.3: which tx is awaiting confirmation (the confirm window checks it is still current)

Delete-all (M2.7 / K9 "apply lock -> block applies -> reset -> delete"): `delete_all_data()` holds the service mutex
and the apply lock while it deletes, so no apply can start between the journal's token and the deletion.

Apply (M2.1) leaves the tx AWAITING_CONFIRMATION; `keep()` / `revert()` finish it. The confirm process is started by
the UI (it is a GUI process); if it never answers, `confirm_watchdog()` reverts after the confirmation deadline.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from scribesense.contracts import paths
from scribesense.contracts.adapter import Adapter
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import ActiveConfig, TxId
from scribesense.contracts.messages import MAX_READER_CHARS, PROTOCOL_VERSION
from scribesense.contracts.presets import Preset
from scribesense.contracts.states import TxState
from scribesense.controller.apply_flow import ApplyController, ApplyReport

Message = dict[str, Any]

#: M2.3 / A15: how long the user has to press Keep before the change is reverted.
CONFIRM_SECONDS = 30
#: Extra time the watchdog allows the confirm process to start and answer.
CONFIRM_GRACE_SECONDS = 15


@dataclass(frozen=True)
class _Pending:
    tx: TxId
    config: Configuration
    font_key: str
    preset_id: str | None


class Service:
    """Owns the journal, socket and controller. Build with `Service.open()`; call `close()` on Quit."""

    def __init__(self, *, store: Any, lock: Any, journal: Any, server: Any, adapters: list[Adapter],
                 controller: ApplyController, on_reader_text: Callable[[str], None] | None = None) -> None:
        self.store = store
        self.lock = lock
        self.journal = journal
        self.server = server
        self.adapters = adapters
        self.controller = controller
        self.on_reader_text = on_reader_text
        self._pending: _Pending | None = None
        self._mutex = threading.RLock()  # one apply / keep / revert / reset at a time inside the service

    # ---------------------------------------------------------------- construction

    @classmethod
    def open(cls, *, on_reader_text: Callable[[str], None] | None = None, start_server: bool = True,
             adapters: list[Adapter] | None = None) -> Service:
        from scribesense.adapters.registry import default_adapters
        from scribesense.config import presets, settings
        from scribesense.fontgen import generator
        from scribesense.safety import ipc
        from scribesense.safety.journal import SqliteJournal
        from scribesense.safety.lock import ApplyLock
        from scribesense.store import Store, default_path

        store = Store(default_path())
        lock = ApplyLock()
        holder: dict[str, Service] = {}
        server = ipc.IpcServer(lambda m: holder["service"].handle(m),
                               style_revision=lambda: holder["service"].journal.style_revision())
        journal = SqliteJournal(store, lock, role="service", on_style_changed=server.publish_style_changed)
        adapter_list = default_adapters() if adapters is None else adapters
        controller = ApplyController(
            journal=journal, adapters=adapter_list, generator=generator.build, resolve=presets.resolve,
            round_to_steps=settings.round_to_steps, validate=settings.validate, data_dir=paths.data_dir,
        )
        service = cls(store=store, lock=lock, journal=journal, server=server, adapters=adapter_list,
                      controller=controller, on_reader_text=on_reader_text)
        holder["service"] = service
        service.housekeeping()
        if start_server:
            server.start()
        return service

    def housekeeping(self) -> None:
        """Once at start-up (M3.1/M3.2/K7 rule 3): interrupted builds, retention, unused fonts. Never fatal.

        The font clean-up reads `presets.list_all()`, not `list()`: K7 rule 3's "a saved preset" includes the stored
        `custom` row (K9 D3), which `list()` stops returning (O3's D3 note) — missing it would delete the font of the
        user's own current configuration.
        """
        from scribesense.fontgen import generator
        from scribesense.log import log
        from scribesense.safety.font_references import FontReferenceSource
        from scribesense.store.retention import prune

        for step, run in (
            ("builds", lambda: self.store.recover_interrupted_builds()),
            ("prune", lambda: prune(self.journal, datetime.now(timezone.utc))),
            ("fonts", lambda: generator.remove_unused(references=FontReferenceSource(
                self.journal, self.adapters, presets=self.store.presets.list_all, lock=self.lock))),
        ):
            try:
                run()
            except Exception as exc:  # noqa: BLE001 — housekeeping must never stop the app from starting
                code = exc.code if isinstance(exc, ScribeSenseError) else ErrorCode.STORE_IO_ERROR
                log(code, "service", step=step)

    def close(self) -> None:
        self.server.stop()
        self.store.close()

    # ---------------------------------------------------------------- apply / confirm (M2.1, M2.3)

    def apply(self, preset: Preset) -> ApplyReport:
        """Run one apply. On success the tx waits for Keep/Revert (`pending_tx`)."""
        with self._mutex:
            report = self.controller.apply(preset)
            if report.status == "awaiting_confirmation" and report.tx is not None and report.fonts is not None:
                from scribesense.config import presets

                config = presets.resolve(preset.values)
                self._pending = _Pending(report.tx, config, report.fonts.key, preset.id)
            return report

    def original(self) -> Any:
        """"Original" = revert everything (K2 ORIGINAL, K4 rule 4 step 2) — the same as `scribesense reset`."""
        from scribesense.adapters.registry import by_name
        from scribesense.safety.recovery import reset
        from scribesense.safety.revert import Reverter

        with self._mutex:
            pending, self._pending = self._pending, None
            if pending is not None:
                # The pending apply still holds the journal's lock (K4 rule 1): finish it first, or reset's
                # begin("revert") is refused (APPLY_IN_PROGRESS) and a `reset` / `uninstall` asking us over the socket
                # would read that as "not responding" and take the service over (K4 rule 6).
                Reverter(self.journal, by_name(self.adapters)).revert_tx(pending.tx)
            return reset(self.journal, by_name(self.adapters))

    @property
    def pending_tx(self) -> TxId | None:
        with self._mutex:
            return self._pending.tx if self._pending is not None else None

    def keep(self, tx: TxId) -> int:
        """Keep the pending tx (K4 rule 3, one atomic step). Only the CURRENT pending tx (A15)."""
        with self._mutex:
            pending = self._pending
            if pending is None or pending.tx != tx or self.journal.state(tx) is not TxState.AWAITING_CONFIRMATION:
                raise ScribeSenseError(ErrorCode.TX_NOT_PENDING)
            revision = self.journal.keep(tx, ActiveConfig(pending.config, pending.font_key, pending.preset_id, 0))
            self._pending = None
            return revision

    def revert(self, tx: TxId) -> Any:
        """Revert the pending tx (Revert pressed, window closed, or the confirmation timed out)."""
        from scribesense.adapters.registry import by_name
        from scribesense.safety.revert import Reverter

        with self._mutex:
            pending = self._pending
            if pending is None or pending.tx != tx:
                raise ScribeSenseError(ErrorCode.TX_NOT_PENDING)
            self._pending = None
            return Reverter(self.journal, by_name(self.adapters)).revert_tx(tx)

    def confirm_watchdog(self, tx: TxId) -> bool:
        """Revert `tx` if it is still pending after the confirmation deadline (A15). True if it reverted."""
        with self._mutex:
            if self._pending is None or self._pending.tx != tx:
                return False
        try:
            self.revert(tx)
        except ScribeSenseError:
            return False  # kept or reverted meanwhile
        return True

    # ---------------------------------------------------------------- delete all data (M2.7, K9)

    def delete_all_data(self) -> bool:
        """M2.7 "Delete all ScribeSense data", in K9's order "apply lock -> block applies -> delete".

        Runs under the service mutex AND the apply lock (K4 rule 1), held for the whole deletion, so no apply can start
        between `journal.no_obligations()` and `delete_recovery_data` (O3's M2.7 note 3). Refused with
        `APPLY_IN_PROGRESS` — before anything is deleted — while a change awaits Keep/Revert here or another process
        holds the lock. The reset (R2) is the UI's separate, earlier step (`original()`).

        The service mutex is taken WITHOUT waiting: the caller is the Settings dialog's response handler on the GTK
        main thread, and apply / revert / reset hold the mutex for their whole run (an apply includes the font build).
        Waiting would freeze the window only to refuse afterwards, so a busy mutex is refused at once, the same as a
        busy lock ("refuse while busy").

        Returns True if the recovery records were deleted too, False if they were kept (see `delete_store_data`).
        """
        if not self._mutex.acquire(blocking=False):  # an apply / revert / reset is running on another thread
            raise ScribeSenseError(ErrorCode.APPLY_IN_PROGRESS)
        try:
            if self._pending is not None or self.lock.held:  # our own tx holds the lock until it ends
                raise ScribeSenseError(ErrorCode.APPLY_IN_PROGRESS)
            if not self.lock.try_acquire("service"):
                raise ScribeSenseError(ErrorCode.APPLY_IN_PROGRESS)
            try:
                return delete_store_data(self.store, self.journal)
            finally:
                self.lock.release()
        finally:
            self._mutex.release()

    # ---------------------------------------------------------------- socket (K4 rule 5, K11)

    def handle(self, message: Message) -> Message:
        kind = message.get("type")
        if kind == "get_style":
            return self._get_style()
        if kind == "open_in_reader":
            text = message.get("text")
            if not isinstance(text, str):
                raise ScribeSenseError(ErrorCode.BAD_MESSAGE)
            if len(text) > MAX_READER_CHARS:
                raise ScribeSenseError(ErrorCode.TOO_LARGE)  # A17: reject, never truncate
            if self.on_reader_text is not None:
                self.on_reader_text(text)
            return {"v": PROTOCOL_VERSION, "ok": True}
        if kind in ("keep", "revert"):
            tx = message.get("tx")
            if type(tx) is not int:
                raise ScribeSenseError(ErrorCode.BAD_MESSAGE)
            if kind == "keep":
                return {"v": PROTOCOL_VERSION, "ok": True, "revision": self.keep(TxId(tx))}
            report = self.revert(TxId(tx))
            return {"v": PROTOCOL_VERSION, "ok": True, "failed": len(report.failed)}
        if kind == "pending":
            return {"v": PROTOCOL_VERSION, "ok": True, "tx": self.pending_tx}
        if kind == "reset":
            report = self.original()
            return {"v": PROTOCOL_VERSION, "ok": True, "restored": len(report.restored),
                    "skipped": len(report.skipped), "failed": len(report.failed)}
        raise ScribeSenseError(ErrorCode.BAD_MESSAGE)

    def _get_style(self) -> Message:
        """K11 get_style: the confirmed style only. Inactive when Original, or when its fonts are not installed."""
        from scribesense.adapters.browser_css import browser_css
        from scribesense.fontgen import generator

        revision = self.journal.style_revision()
        active = self.journal.active()
        if active is not None:
            fonts = next((f for f in generator.installed_sets() if f.key == active.font_key), None)
            if fonts is not None:
                return {"v": PROTOCOL_VERSION, "ok": True, "active": True, "revision": revision,
                        "css": browser_css(fonts, active.config)}
        return {"v": PROTOCOL_VERSION, "ok": True, "active": False, "revision": revision}


def delete_store_data(store: Any, journal: Any) -> bool:
    """The store steps of delete-all (K9 API). The caller holds the apply lock — use `Service.delete_all_data()`.

    Preferences (presets, settings, samples) are always deleted. Recovery records are deleted only with the token of
    `journal.no_obligations()`; otherwise they are kept, so `scribesense reset` can still restore the original
    desktop. The store re-checks the token inside its transaction: `RECOVERY_OBLIGATIONS_REMAIN` from it means the
    world changed since the token was issued, and is reported as "recovery records kept" (False), never as an error.
    Returns True if the recovery records were deleted too.
    """
    store.delete_preferences()
    token = journal.no_obligations()
    if token is None:
        return False
    try:
        store.delete_recovery_data(precondition=token)
    except ScribeSenseError as exc:
        if exc.code is not ErrorCode.RECOVERY_OBLIGATIONS_REMAIN:
            raise
        return False
    return True
