"""M4.8 — browser CSS generator (pure)."""

from __future__ import annotations

import re

import pytest

from scribesense.adapters import browser_css as bc
from scribesense.adapters.browser_css import CODE_SELECTORS, ICON_SELECTORS, browser_css
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import FontSet

FONTS = FontSet("abc123", "AccessSans-abc123", "AccessSansUI-abc123", (), ("Regular",))
CFG = Configuration("Noto Sans", 0.12, 1.5, 1.8, 1.2, 1.0)


def declarations(css: str) -> list[tuple[str, str]]:
    body = css[css.index("{") + 1: css.rindex("}")]
    decls = [d.strip() for d in body.split(";") if d.strip()]
    return [(d.split(":", 1)[0].strip(), d.split(":", 1)[1].strip()) for d in decls]


def test_sets_only_font_family_and_line_height_both_important() -> None:
    css = browser_css(FONTS, CFG)
    assert declarations(css) == [
        ("font-family", '"AccessSans-abc123", sans-serif !important'),
        ("line-height", "1.8 !important"),
    ]


def test_uses_the_reading_family_never_the_ui_family() -> None:
    css = browser_css(FONTS, CFG)
    assert "AccessSans-abc123" in css and "AccessSansUI" not in css


@pytest.mark.parametrize("forbidden", ["letter-spacing", "word-spacing", "@font-face", "@import", "url(",
                                       "file:", "http:", "https:"])
def test_never_spacing_or_any_url(forbidden: str) -> None:
    assert forbidden not in browser_css(FONTS, CFG).lower()


@pytest.mark.parametrize("selector", ["code", "pre", "kbd", "samp", ".material-icons",
                                      '[class^="material-symbols"]', ".fa", '[class*=" fa-"]', "svg"])
def test_code_and_icons_are_excluded_with_their_descendants(selector: str) -> None:
    css = browser_css(FONTS, CFG)
    excluded = css[css.index(":not(") + 5: css.index(") {")]
    items = [x.strip() for x in excluded.split(",\n")]
    assert selector in items
    if not selector.endswith(" *"):
        assert f"{selector} *" in items


def test_every_listed_selector_is_excluded() -> None:
    css = browser_css(FONTS, CFG)
    for sel in CODE_SELECTORS + ICON_SELECTORS:
        assert sel in css


def test_output_is_deterministic_and_structurally_balanced() -> None:
    a, b = browser_css(FONTS, CFG), browser_css(FONTS, CFG)
    assert a == b
    stripped = re.sub(r'"[^"]*"', '""', re.sub(r"/\*.*?\*/", "", a, flags=re.S))
    assert stripped.count("{") == stripped.count("}") == 1
    assert stripped.count("(") == stripped.count(")")
    assert stripped.count("[") == stripped.count("]")


@pytest.mark.parametrize("value, text", [(1.5, "1.5"), (2.0, "2"), (1.0, "1"), (1.7000000000000002, "1.7")])
def test_line_height_formatting(value: float, text: str) -> None:
    assert bc.format_line_height(value) == text


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0.0, -1.0])
def test_bad_line_height_is_refused(value: float) -> None:
    with pytest.raises(ScribeSenseError) as exc:
        bc.format_line_height(value)
    assert exc.value.code is ErrorCode.UNEXPECTED_ADAPTER_ERROR


@pytest.mark.parametrize("family", ["Comic Sans", "AccessSansUI-abc", 'AccessSans-x"; } body { color: red',
                                    "AccessSans-" + "a" * 70, "AccessSans-a b"])
def test_only_our_reading_family_can_be_emitted(family: str) -> None:
    with pytest.raises(ScribeSenseError) as exc:
        browser_css(FontSet("k", family, "AccessSansUI-k", (), ()), CFG)
    assert exc.value.code is ErrorCode.UNEXPECTED_ADAPTER_ERROR


def test_forbidden_content_check_is_enforced_not_asserted(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a future edit slipped spacing into the selector lists, generation must fail (not just warn)."""
    monkeypatch.setattr(bc, "CODE_SELECTORS", CODE_SELECTORS + ('[style*="letter-spacing"]',))
    with pytest.raises(ScribeSenseError):
        browser_css(FONTS, CFG)
