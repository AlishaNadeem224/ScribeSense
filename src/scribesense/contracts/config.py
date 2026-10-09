"""K1 — Configuration (owner O1). FROZEN 2026-10-09.

Types and constants only. `round_to_steps()` and `validate()` are implemented by O1 in
`scribesense.config` (M1.1) and must follow the rules below exactly.

Rules:
1. Values are floats. round_to_steps() makes them canonical:
   round(min + round((v - min) / step) * step, decimals_of(step)).
   It NEVER clamps (out-of-range stays out of range). NaN/±inf pass through unchanged.
2. Call order is always round_to_steps() -> validate().
3. No exact float equality: on-step checks use STEP_TOLERANCE.
4. Issue codes: see IssueCode. NOT_ON_STEP is a guard that never fires in the normal flow.
5. Telling the user a value was rounded is UI feedback, not an Issue.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol


@dataclass(frozen=True)
class Configuration:
    """Always resolved and canonical when used (K2 resolve -> round -> validate)."""

    base_family: str  # one of SHORTLIST
    letter_em: float  # 0.00–0.30, step 0.02
    word_scale: float  # 1.0–3.0 × space width, step 0.25
    reading_line: float  # 1.0–2.0 ×, step 0.1
    ui_line: float  # 1.0–2.0 ×, step 0.1; warning above UI_LINE_RECOMMENDED_MAX
    text_scale: float  # 1.0–2.0, step 0.05


@dataclass(frozen=True)
class Range:
    """No default here — the Default preset (K2) is the only source of defaults."""

    min: float
    max: float
    step: float


SHORTLIST: tuple[str, ...] = ("Carlito", "Atkinson Hyperlegible", "Lexend", "Noto Sans")

#: Single source of truth for UI controls, validation and tests.
RANGES: dict[str, Range] = {
    "letter_em": Range(0.0, 0.30, 0.02),
    "word_scale": Range(1.0, 3.0, 0.25),
    "reading_line": Range(1.0, 2.0, 0.1),
    "ui_line": Range(1.0, 2.0, 0.1),  # A12
    "text_scale": Range(1.0, 2.0, 0.05),  # A11
}

UI_LINE_RECOMMENDED_MAX = 1.3
STEP_TOLERANCE = 1e-9


class IssueCode(StrEnum):
    NON_FINITE = "NON_FINITE"  # error: NaN or ±inf
    OUT_OF_RANGE = "OUT_OF_RANGE"  # error: outside RANGES[field]
    UNKNOWN_FONT = "UNKNOWN_FONT"  # error: base_family not in SHORTLIST
    NOT_ON_STEP = "NOT_ON_STEP"  # error: guard only — value was not rounded first
    UI_LINE_ABOVE_RECOMMENDED = "UI_LINE_ABOVE_RECOMMENDED"  # warning: ui_line > 1.3


@dataclass(frozen=True)
class Issue:
    level: Literal["error", "warning"]
    field: str
    code: str  # an IssueCode value


class ConfigurationApi(Protocol):
    """Implemented by O1 in scribesense.config (M1.1)."""

    def round_to_steps(self, c: Configuration) -> Configuration: ...

    def validate(self, c: Configuration) -> list[Issue]: ...
