"""K2 — Preset (owner O1). FROZEN 2026-10-09.

A preset stores the user's INTENT; resolve() turns it into one concrete Configuration (K1).
Only the resolved Configuration is used for building fonts — no second authoritative value.

Rules:
- Built-in IDs are fixed (BUILTIN_IDS); user presets use str(uuid4()). Names are display only.
- Changing the base font keeps `multiplier` word spacing and recalculates `target_em`.
  A manual word-spacing edit switches that field to `multiplier` mode.
- Base fonts are BUNDLED in the repo (fonts/ + manifest.toml), never taken from the system.
- Built-in values (BUILTINS) live in scribesense.config (M1.2) and must match the K2 table:
    default          Noto Sans · 0.12 em · target ≈ +0.16 em · 1.5× · UI 1.2× · 1.0
    reading          Noto Sans · 0.12 em · 1.5×              · 1.8× · UI 1.2× · 1.0
    high-separation  Noto Sans · 0.20 em · 2.5×              · 1.6× · UI 1.2× · 1.0
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from scribesense.contracts.config import Configuration


@dataclass(frozen=True)
class WordSpacing:
    mode: Literal["target_em", "multiplier"]  # target_em: recalc when base font changes
    value: float  # em added (target_em) or × space width (multiplier)


@dataclass(frozen=True)
class PresetValues:
    """All six fields, always present (A10)."""

    base_family: str
    letter_em: float
    word: WordSpacing
    reading_line: float
    ui_line: float
    text_scale: float


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    values: PresetValues
    kind: Literal["builtin", "user", "custom"]


BUILTIN_IDS: tuple[str, ...] = ("default", "reading", "high-separation")

#: Sentinel: revert everything. Not a preset.
ORIGINAL = "original"


class PresetApi(Protocol):
    """Implemented by O1 in scribesense.config (M1.2)."""

    BUILTINS: tuple[Preset, ...]

    def resolve(self, values: PresetValues) -> Configuration:
        """target_em -> word_scale = 1 + value / space_width_em(Regular face of base_family),
        rounded to the 0.25 step, then round_to_steps() + validate()."""
        ...

    def base_font_files(self, family: str) -> dict[str, Path]:
        """style -> bundled file. Raises ScribeSenseError(BUNDLED_FONT_MISSING)."""
        ...
