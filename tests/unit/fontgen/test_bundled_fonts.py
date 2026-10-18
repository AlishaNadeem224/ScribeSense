"""K2 bundled base fonts: the files in fonts/ match fonts/manifest.toml, are eligible, and build (M1.2–M1.8).

Uses the REAL bundled files (no fixtures). Rendering and fontconfig are faked here (conftest.py); the same build with
the real pango-view / fc-cache / fc-match runs in the bubblewrap runner (test_fontgen_integration.py).
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

import pytest

from scribesense.config import bundled, presets
from scribesense.config.rules import is_eligible
from scribesense.contracts import fonts as k7
from scribesense.fontgen import generator, variants

FONTS = bundled.FONTS_ROOT
MANIFEST = tomllib.loads((FONTS / "manifest.toml").read_text())


@pytest.mark.parametrize("family", sorted(MANIFEST))
def test_manifest_matches_the_bundled_files(family: str) -> None:
    entry = MANIFEST[family]
    assert entry["licence"] == "OFL-1.1" and (FONTS / entry["licence_file"]).is_file()
    for style, spec in entry["files"].items():
        path = FONTS / spec["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == spec["sha256"], f"{family} {style} changed"
    assert set(entry["files"]) == set(bundled.base_font_files(family))  # what the pipeline finds = what is recorded


def test_noto_sans_is_bundled_with_four_eligible_styles() -> None:
    files = bundled.base_font_files("Noto Sans")
    assert set(files) == {"Regular", "Bold", "Italic", "BoldItalic"}
    for style, path in files.items():
        verdict = is_eligible(path)
        assert verdict.ok, (style, verdict)
    assert "SIL Open Font License, Version 1.1" in (FONTS / "Noto Sans" / "OFL.txt").read_text()


def test_space_width_is_measured_so_default_resolves_as_k2_intends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(presets, "MEASURED_SPACE_WIDTH_EM", {})
    assert presets.default_space_width_em("Noto Sans") == pytest.approx(0.26)
    assert presets.space_width_source("Noto Sans") == "measured"
    # +0.16 em target / 0.26 em space -> 1.615 -> 1.5 on the 0.25 grid (the fallback 0.25 em gave 1.75)
    assert presets.resolve(presets.builtin("default").values).word_scale == 1.5


@pytest.mark.parametrize("preset_id", ["default", "reading", "high-separation"])
def test_every_builtin_plans_from_the_bundled_files(preset_id: str) -> None:
    config = presets.resolve(presets.builtin(preset_id).values)
    plan = variants.plan(config)  # no files= override: the real bundled tree
    assert set(plan.sources) == {"Regular", "Bold", "Italic", "BoldItalic"}


def test_the_default_preset_builds_from_the_real_fonts(renderer, tools) -> None:  # type: ignore[no-untyped-def]
    config = presets.resolve(presets.builtin("default").values)
    result = generator.build(config, renderer=renderer, tools=tools)
    assert result.status == "built", (result.reason_code, getattr(result, "detail", None))
    assert isinstance(result.fonts, k7.FontSet)
    assert result.fonts.styles == ("Regular", "Bold", "Italic", "BoldItalic")
    assert all(Path(f).exists() for f in result.fonts.files)
