"""O4 — Test support: the §9 recovery-slice hook (tests/recovery/conftest.py).

TEST SUPPORT ONLY — no production module imports this file.

`make_recovery_system(world, adapters)` wires the in-memory journal (M4.10 double), revert (M4.11) and
recovery (M4.12) to FakeAdapters. Its `apply()` runs O2's real apply controller (M2.1) — the §9 scenarios
are part of that card's acceptance — with K1/K2/K7 stubbed out (fonts are not what these scenarios test).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from datetime import datetime

from scribesense.adapters.fake import FakeAdapter, FakeWorld
from scribesense.contracts.adapter import Adapter
from scribesense.contracts.config import Configuration
from scribesense.contracts.fonts import FontBuildResult, FontSet
from scribesense.contracts.journal import ActiveConfig, TxId
from scribesense.contracts.presets import Preset, PresetValues, WordSpacing
from scribesense.controller.apply_flow import ApplyController
from scribesense.safety.journal import SqliteJournal
from scribesense.safety.lock import ApplyLock
from scribesense.safety.memory_journal import InMemoryJournal
from scribesense.safety.recovery import reset as recovery_reset
from scribesense.safety.revert import Reverter, RevertReport


def fake_fontset(config: Configuration) -> FontSet:
    key = hashlib.sha256(repr(config).encode()).hexdigest()[:12]
    return FontSet(key, f"AccessSans-{key}", f"AccessSansUI-{key}", (), ("Regular",))


class RecoverySystem:
    def __init__(self, world: FakeWorld, adapters: Sequence[FakeAdapter],
                 open_journal: Callable[[], InMemoryJournal | SqliteJournal] = InMemoryJournal) -> None:
        self.world = world
        self._open_journal = open_journal
        self.journal = open_journal()
        self._adapter_list: list[Adapter] = list(adapters)
        self.adapters: dict[str, Adapter] = {a.name: a for a in adapters}
        if len(self.adapters) != len(self._adapter_list):
            raise ValueError("adapter names must be unique")

    # ---------------------------------------------------------------- reference apply (K4 rule 2)

    def apply(self, config: Configuration, *, keep: bool) -> TxId:
        fonts = fake_fontset(config)
        controller = ApplyController(
            journal=self.journal,
            adapters=self._adapter_list,
            generator=lambda _c: FontBuildResult("built", fonts, None),
            resolve=lambda _values: config,
            round_to_steps=lambda c: c,
            validate=lambda _c: [],
            disk_space_min_bytes=0,
        )
        values = PresetValues(config.base_family, config.letter_em, WordSpacing("multiplier", config.word_scale),
                              config.reading_line, config.ui_line, config.text_scale)
        report = controller.apply(Preset("default", "Default", values, "builtin"))
        if report.tx is None:
            raise AssertionError(f"apply stopped before a tx: {report.reason_code}")
        if keep and report.status == "awaiting_confirmation":
            self.journal.keep(report.tx, ActiveConfig(config, fonts.key, None, 0))
        return report.tx

    # ---------------------------------------------------------------- the rest of the harness interface

    def revert(self, tx: TxId) -> RevertReport:
        return Reverter(self.journal, self.adapters).revert_tx(tx)

    def crash(self) -> None:
        if isinstance(self.journal, InMemoryJournal):
            self.journal.simulate_crash()
            return
        # The process died: the OS drops the flock and the DB connection; a new process opens both again.
        self.journal._lock.release()
        self.journal._t._s.close()
        self.journal = self._open_journal()

    def reset(self) -> RevertReport:
        return recovery_reset(self.journal, self.adapters)

    def prune(self, before: datetime, keep_last: int) -> int:
        return self.journal.prune_history(before, keep_last)

    def managed(self, location: str) -> str | None:
        record = self.journal.location(location)
        return record.managed_sha256 if record else None


def sqlite_journal() -> SqliteJournal:
    """The real M4.10 journal on a store at `default_path()` (inside the test's data dir in the sandbox)."""
    from scribesense.store import Store, default_path

    return SqliteJournal(Store(default_path()), ApplyLock())


def make_recovery_system(world: FakeWorld, adapters: Sequence[FakeAdapter], *,
                         journal: str = "memory") -> RecoverySystem:
    """journal="memory" (M4.10 double) or "sqlite" (the real journal on the store)."""
    return RecoverySystem(world, adapters, {"memory": InMemoryJournal, "sqlite": sqlite_journal}[journal])
