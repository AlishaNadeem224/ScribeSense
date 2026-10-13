"""K6 — coverage mapping, owned by O2.

The controller maps situations to statuses (ARCHITECTURE.md §5 K6); adapters
never decide coverage (K3 rule 5). This module exists so the mapping is one
place, tested in isolation, and easy to change if O4 adds a new situation.

Mapping (from K6):

    must-be-closed target is running         -> PENDING
    per-target adapter error or timeout      -> NOT_COVERED + that code
    unexpected exception                     -> NOT_COVERED + UNEXPECTED_ADAPTER_ERROR
    fontconfig fails                         -> not a coverage result; whole apply stops
"""

from __future__ import annotations

from scribesense.contracts.adapter import Target
from scribesense.contracts.coverage import CoverageEntry, CoverageStatus
from scribesense.contracts.errors import ErrorCode, ScribeSenseError


def covered(target: Target) -> CoverageEntry:
    return CoverageEntry(target=target, status=CoverageStatus.COVERED, detail_code="OK")


def pending(target: Target) -> CoverageEntry:
    """Target must be closed and was not."""
    return CoverageEntry(
        target=target, status=CoverageStatus.PENDING, detail_code=ErrorCode.BROWSER_RUNNING
    )


def not_covered(target: Target, code: ErrorCode | str) -> CoverageEntry:
    """Per-target failure. `code` is an ErrorCode value or a short identifier."""
    return CoverageEntry(target=target, status=CoverageStatus.NOT_COVERED, detail_code=code)


def not_covered_from_exception(target: Target, exc: BaseException) -> CoverageEntry:
    """A ScribeSenseError keeps its code; anything else becomes UNEXPECTED_ADAPTER_ERROR.

    Raw exception text is never surfaced or logged (K8). We only look at
    `ScribeSenseError.code`; other exception types are treated as generic.
    """
    if isinstance(exc, ScribeSenseError):
        return not_covered(target, exc.code)
    return not_covered(target, ErrorCode.UNEXPECTED_ADAPTER_ERROR)


def restart_needed(target: Target) -> CoverageEntry:
    """Apply succeeded but the app must be restarted to see the change."""
    return CoverageEntry(
        target=target, status=CoverageStatus.RESTART_NEEDED, detail_code="RESTART_NEEDED"
    )