"""O4 — Test support: the §9 recovery-slice hook (tests/recovery/conftest.py).

TEST SUPPORT ONLY — no production module imports this file.

`make_recovery_system(world, adapters)` wires the in-memory journal (M4.10 double), revert (M4.11) and
recovery (M4.12) to FakeAdapters. Its `apply()` is a REFERENCE apply sequence that follows the K4 rule 2
order and the M2.1 failure rules needed by the scenarios. It is NOT the production controller: when O2's
apply controller (M2.1) exists, `apply()` must call it instead, and the scenarios must still pass.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime

from scribesense.adapters.fake import FakeAdapter, FakeWorld
from scribesense.contracts.adapter import Adapter
from scribesense.contracts.config import Configuration
from scribesense.contracts.fonts import FontSet
from scribesense.contracts.journal import ActiveConfig, ItemId, TxId
from scribesense.contracts.states import TxState
from scribesense.safety.memory_journal import InMemoryJournal
from scribesense.safety.recovery import reset as recovery_reset
from scribesense.safety.revert import Reverter, RevertReport


def fake_fontset(config: Configuration) -> FontSet:
    key = hashlib.sha256(repr(config).encode()).hexdigest()[:12]
    return FontSet(key, f"AccessSans-{key}", f"AccessSansUI-{key}", (), ("Regular",))


class RecoverySystem:
    def __init__(self, world: FakeWorld, adapters: Sequence[FakeAdapter]) -> None:
        self.world = world
        self.journal = InMemoryJournal()
        self._adapter_list: list[Adapter] = list(adapters)
        self.adapters: dict[str, Adapter] = {a.name: a for a in adapters}
        if len(self.adapters) != len(self._adapter_list):
            raise ValueError("adapter names must be unique")

    # ---------------------------------------------------------------- reference apply (K4 rule 2)

    def apply(self, config: Configuration, *, keep: bool) -> TxId:
        j = self.journal
        fonts = fake_fontset(config)
        tx = j.begin("apply", None)
        by_target: dict[tuple[str, str], dict[str, ItemId]] = {}
        targets = [(a, t) for a in self._adapter_list for t in a.detect()]
        for adapter, target in targets:
            ids = by_target.setdefault((adapter.name, target.target_id), {})
            for item in adapter.snapshot(target):
                backup = adapter.read_state(item.location).data if item.kind == "file" and item.existed else None
                ids[item.location] = j.record_snapshot(tx, item, backup)
        j.set_state(tx, TxState.SNAPSHOTTED)
        j.set_state(tx, TxState.APPLYING)
        for adapter, target in targets:
            ids = by_target[(adapter.name, target.target_id)]
            for planned in adapter.plan(target, fonts, config):
                j.record_intent(tx, ids[planned.location], planned)
                j.record_applied(tx, ids[planned.location], adapter.apply(planned))
        j.set_state(tx, TxState.VERIFYING)
        reverter = Reverter(j, self.adapters)
        for adapter, target in targets:
            ids = list(by_target[(adapter.name, target.target_id)].values())
            if adapter.verify(target, fonts).chosen_ok:
                for item_id in ids:
                    j.mark_retained(tx, item_id)
                continue
            # M2.1: verify failed -> revert that adapter only; if that revert fails -> roll back the whole tx (K5)
            if not reverter.revert_items(tx, ids).ok:
                reverter.finish_tx(tx)
                return tx
        j.set_state(tx, TxState.AWAITING_CONFIRMATION)
        if keep:
            j.keep(tx, ActiveConfig(config, fonts.key, None, 0))
        return tx

    # ---------------------------------------------------------------- the rest of the harness interface

    def revert(self, tx: TxId) -> RevertReport:
        return Reverter(self.journal, self.adapters).revert_tx(tx)

    def crash(self) -> None:
        self.journal.simulate_crash()

    def reset(self) -> RevertReport:
        return recovery_reset(self.journal, self.adapters)

    def prune(self, before: datetime, keep_last: int) -> int:
        return self.journal.prune_history(before, keep_last)

    def managed(self, location: str) -> str | None:
        record = self.journal.location(location)
        return record.managed_sha256 if record else None


def make_recovery_system(world: FakeWorld, adapters: Sequence[FakeAdapter]) -> RecoverySystem:
    return RecoverySystem(world, adapters)
