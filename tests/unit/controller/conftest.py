"""Shared stubs and fixtures for the M2.1 tests.

K1's round_to_steps/validate, K2's resolve, and K7's build do not exist yet
(M1.1, M1.2, M1.8 are O1's cards). The stubs below match the frozen
signatures exactly; when O1 lands the real implementations, the wiring in
controller/__init__.py changes, and these tests keep working unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path

import pytest

from scribesense.adapters.fake import FakeAdapter, FakeWorld  # noqa: F401  (re-exported for tests)
from scribesense.contracts.config import Configuration, Issue, IssueCode, RANGES
from scribesense.contracts.fonts import FontBuildResult, FontSet
from scribesense.contracts.presets import Preset, PresetValues, WordSpacing
from scribesense.safety.memory_journal import InMemoryJournal


# --- K1 stubs -----------------------------------------------------------------

def _round_to_steps(c: Configuration) -> Configuration:
    def step(v: float, r) -> float:
        if not math.isfinite(v):
            return v
        return round(r.min + round((v - r.min) / r.step) * r.step, 6)

    return Configuration(
        base_family=c.base_family,
        letter_em=step(c.letter_em, RANGES["letter_em"]),
        word_scale=step(c.word_scale, RANGES["word_scale"]),
        reading_line=step(c.reading_line, RANGES["reading_line"]),
        ui_line=step(c.ui_line, RANGES["ui_line"]),
        text_scale=step(c.text_scale, RANGES["text_scale"]),
    )


def _validate(c: Configuration) -> list[Issue]:
    issues: list[Issue] = []
    if c.base_family not in ("Carlito", "Atkinson Hyperlegible", "Lexend", "Noto Sans"):
        issues.append(Issue("error", "base_family", IssueCode.UNKNOWN_FONT))
    for field, value in (
        ("letter_em", c.letter_em),
        ("word_scale", c.word_scale),
        ("reading_line", c.reading_line),
        ("ui_line", c.ui_line),
        ("text_scale", c.text_scale),
    ):
        r = RANGES[field]
        if value < r.min or value > r.max:
            issues.append(Issue("error", field, IssueCode.OUT_OF_RANGE))
    if c.ui_line > 1.3:
        issues.append(Issue("warning", "ui_line", IssueCode.UI_LINE_ABOVE_RECOMMENDED))
    return issues


# --- K2 stub ------------------------------------------------------------------

def _resolve(values: PresetValues) -> Configuration:
    if values.word.mode == "multiplier":
        word_scale = values.word.value
    else:
        word_scale = 1.0 + values.word.value * 5.0
    return Configuration(
        base_family=values.base_family,
        letter_em=values.letter_em,
        word_scale=word_scale,
        reading_line=values.reading_line,
        ui_line=values.ui_line,
        text_scale=values.text_scale,
    )


# --- fixtures -----------------------------------------------------------------

@pytest.fixture
def journal() -> InMemoryJournal:
    return InMemoryJournal()


@pytest.fixture
def world() -> FakeWorld:
    return FakeWorld()


@pytest.fixture
def make_fonts() -> Callable[[str], FontSet]:
    def _make(key: str = "abc123") -> FontSet:
        return FontSet(
            key=key,
            reading_family=f"AccessSans-{key}",
            ui_family=f"AccessSansUI-{key}",
            files=(f"/tmp/{key}.ttf",),
            styles=("Regular",),
        )

    return _make


@pytest.fixture
def good_build(make_fonts: Callable[[str], FontSet]) -> Callable[[Configuration], FontBuildResult]:
    def _build(config: Configuration) -> FontBuildResult:
        return FontBuildResult("built", make_fonts(), None)

    return _build


@pytest.fixture
def bad_build() -> Callable[[Configuration], FontBuildResult]:
    def _build(config: Configuration) -> FontBuildResult:
        return FontBuildResult("failed", None, "VALIDATION_FAILED")

    return _build


@pytest.fixture
def preset() -> Preset:
    return Preset(
        id="default",
        name="Default",
        values=PresetValues(
            base_family="Noto Sans",
            letter_em=0.12,
            word=WordSpacing("multiplier", 1.5),
            reading_line=1.5,
            ui_line=1.2,
            text_scale=1.0,
        ),
        kind="builtin",
    )


@pytest.fixture
def resolve_fn() -> Callable[[PresetValues], Configuration]:
    return _resolve


@pytest.fixture
def round_fn() -> Callable[[Configuration], Configuration]:
    return _round_to_steps


@pytest.fixture
def validate_fn() -> Callable[[Configuration], list[Issue]]:
    return _validate


@pytest.fixture
def data_dir(tmp_path: Path) -> Callable[[], Path]:
    d = tmp_path / "scribesense-data"
    d.mkdir()
    return lambda: d