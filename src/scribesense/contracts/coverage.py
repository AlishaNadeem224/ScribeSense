"""K6 — Coverage status (shared, defined by O2). FROZEN 2026-10-09.

Mapping (owned by O2, in the controller):
- target must be closed and is running        -> PENDING
- per-target adapter error or timeout         -> NOT_COVERED + that code
- unexpected exception                        -> NOT_COVERED + UNEXPECTED_ADAPTER_ERROR (raw text never shown/logged)
- fontconfig fails (incl. timeout)            -> not a coverage result: the whole apply stops and reverts
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from scribesense.contracts.adapter import Target
from scribesense.contracts.errors import ErrorCode


class CoverageStatus(StrEnum):
    COVERED = "covered"
    NOT_COVERED = "not_covered"  # app keeps its font; text can go to the reader
    PENDING = "pending"  # app must be closed and wasn't
    RESTART_NEEDED = "restart_needed"


@dataclass(frozen=True)
class CoverageEntry:
    target: Target
    status: CoverageStatus
    detail_code: ErrorCode | str
