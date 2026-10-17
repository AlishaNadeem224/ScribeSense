"""M2.3 — Confirm flow (simplified).

The full M2.3 requires a fresh-process countdown window that talks to the
service over the K4 rule 5 socket and calls journal.keep() or a revert
based on the user's answer. That flow depends on O4's M4.11 (revert) and
on the service-side socket handler for `confirm`, both of which are still
in progress.

This module implements the minimal safe behaviour: the transaction is
committed immediately as soon as it reaches AWAITING_CONFIRMATION. If
journal.keep() fails, the transaction is reverted.

The full countdown will be added as a follow-up once O4's M4.11 lands.
"""

from __future__ import annotations

from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import ActiveConfig, Journal, TxId


def keep_immediately(journal: Journal, tx: TxId, active: ActiveConfig) -> int:
    """Commit the transaction without a countdown window.

    Returns the new style_revision. Raises ScribeSenseError(TX_NOT_PENDING)
    if the transaction is not in AWAITING_CONFIRMATION.
    """
    try:
        return journal.keep(tx, active)
    except ScribeSenseError:
        raise
    except Exception as exc:
        raise ScribeSenseError(ErrorCode.TX_NOT_PENDING) from exc