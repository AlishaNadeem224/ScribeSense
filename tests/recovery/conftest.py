"""§9 recovery slice — test harness interface.

The scenarios in this folder drive the journal + revert + recovery code through one in-memory
FakeAdapter world. Current wiring (2026-10-12): scribesense.safety.testing uses the IN-MEMORY journal
(M4.10 double), the real revert (M4.11) and recovery core (M4.12), and a REFERENCE apply sequence.
When the SQLite journal (after K9) and O2's apply controller (M2.1) exist, the hook must switch to them
and these scenarios must still pass. If the hook module is missing, every scenario skips as BLOCKED —
they never pass vacuously.

O4 provides the hook (test-support code, not production behaviour):

    scribesense/safety/testing.py
        def make_recovery_system(world: FakeWorld, adapters: list[FakeAdapter]) -> RecoverySystem

RecoverySystem — what the scenarios call (backed by the real modules, store in the test's data_dir):

    apply(config: Configuration, *, keep: bool) -> TxId
        Full apply sequence (K4 rule 2 order) up to AWAITING_CONFIRMATION; keep=True then calls the
        real Keep path (Journal.keep()).
    revert(tx: TxId) -> None
        User pressed Revert / confirmation timed out: revert that tx.
    crash() -> None
        Simulate process death: drop all in-memory objects, keep only durable data (store + world),
        release the lock as the OS would. The next call starts from a fresh process state.
    reset() -> ResetReport          # .restored: list[str] · .skipped: list[str] · .failed: list[str]
        Exactly what `scribesense reset` does (K4 rule 4, steps 1 + 2).
    prune(before: datetime, keep_last: int) -> int
        Journal.prune_history().
    managed(location: str) -> str | None
        LocationRecord.managed_sha256 for that location.
"""

from __future__ import annotations

import importlib

import pytest

HOOK_MODULE = "scribesense.safety.testing"
HOOK_FUNCTION = "make_recovery_system"
BLOCKED = ("BLOCKED: recovery slice needs the journal (M4.10), revert (M4.11) and recovery (M4.12); "
           "the journal waits for O3's K9 sign-off. Provide scribesense.safety.testing.make_recovery_system.")


@pytest.fixture
def make_system():  # type: ignore[no-untyped-def]
    try:
        module = importlib.import_module(HOOK_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name == HOOK_MODULE:
            pytest.skip(BLOCKED)
        raise
    return getattr(module, HOOK_FUNCTION)
