"""K5 — Transaction states (shared, defined by O4). FROZEN 2026-10-09 · amended R2 (CONFIRMED removed).

- Terminal: COMPLETED, REVERTED, FAILED_CLEAN (nothing was written).
- REVERT_FAILED is unfinished -> login recovery and reset retry it.
- AWAITING_CONFIRMATION -> COMPLETED happens ONLY via Journal.keep().
- A per-adapter revert after a failed verify doesn't change the tx state. If that revert fails,
  the controller moves the whole tx to REVERTING; it never proceeds to AWAITING_CONFIRMATION.
"""

from __future__ import annotations

from enum import StrEnum


class TxState(StrEnum):
    CREATED = "CREATED"
    SNAPSHOTTED = "SNAPSHOTTED"
    APPLYING = "APPLYING"
    VERIFYING = "VERIFYING"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    REVERTING = "REVERTING"
    COMPLETED = "COMPLETED"
    REVERTED = "REVERTED"
    FAILED_CLEAN = "FAILED_CLEAN"
    REVERT_FAILED = "REVERT_FAILED"


S = TxState

TRANSITIONS: dict[TxState, frozenset[TxState]] = {
    S.CREATED: frozenset({S.SNAPSHOTTED, S.REVERTING, S.FAILED_CLEAN}),
    S.SNAPSHOTTED: frozenset({S.APPLYING, S.FAILED_CLEAN}),
    S.APPLYING: frozenset({S.VERIFYING, S.REVERTING}),
    S.VERIFYING: frozenset({S.AWAITING_CONFIRMATION, S.REVERTING}),
    S.AWAITING_CONFIRMATION: frozenset({S.COMPLETED, S.REVERTING}),
    S.REVERTING: frozenset({S.REVERTED, S.REVERT_FAILED}),
    S.REVERT_FAILED: frozenset({S.REVERTING}),
    S.COMPLETED: frozenset(),
    S.REVERTED: frozenset(),
    S.FAILED_CLEAN: frozenset(),
}

TERMINAL: frozenset[TxState] = frozenset({S.COMPLETED, S.REVERTED, S.FAILED_CLEAN})

#: Every non-terminal state, incl. REVERT_FAILED — what Journal.unfinished() returns.
UNFINISHED: frozenset[TxState] = frozenset(TxState) - TERMINAL


def can_transition(src: TxState, dst: TxState) -> bool:
    return dst in TRANSITIONS[src]
