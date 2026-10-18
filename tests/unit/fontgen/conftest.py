"""O1 test fixtures: tiny synthetic fonts and fakes for the external font tools.

Fonts are generated with fontTools, so no test depends on what is installed on the machine.
FakeRenderer / FakeFontconfig stand in for pango-view / fc-cache / fc-match: unit tests never
launch a process (tests/conftest.py blocks it). They answer from the font files themselves —
the renderer measures real advance widths — so a font that is not really spaced still fails.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

from scribesense.contracts import paths
from scribesense.fontgen.validate import RenderedSize

UPM = 1000
SPACE = 260
DEVANAGARI_KA = 0x0915


def _glyph_name(cp: int) -> str:
    return "space" if cp == 0x20 else f"uni{cp:04X}"


def _advance(cp: int, mono: bool) -> int:
    if cp == 0x20:
        return 600 if mono else SPACE
    return 600 if mono else 480 + (cp % 7) * 20


def build_font(path: Path, family: str = "Fixture Sans", style: str = "Regular", *, mono: bool = False,
               icon: bool = False, cff: bool = False, liga: bool = True, devanagari: bool = True,
               extra_names: dict[int, str] | None = None, os2_version: int | None = None) -> Path:
    """Write a small, valid TTF/OTF with Latin letters, digits, punctuation, é and (optionally) क."""
    if icon:
        cps = [0x20, 0x41] + list(range(0xE000, 0xE040))
    else:
        cps = list(range(0x20, 0x7F)) + [0xE9] + ([DEVANAGARI_KA] if devanagari else [])
    order = [".notdef"] + [_glyph_name(cp) for cp in cps] + (["f_i"] if liga and not icon else [])
    cmap = {cp: _glyph_name(cp) for cp in cps}
    adv = {".notdef": 500, "f_i": 900, **{_glyph_name(cp): _advance(cp, mono) for cp in cps}}

    fb = FontBuilder(UPM, isTTF=not cff)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(cmap)
    if cff:
        charstrings = {}
        for name in order:
            pen = T2CharStringPen(adv[name], None)
            if name != "space":
                _box(pen, adv[name])
            charstrings[name] = pen.getCharString()
        fb.setupCFF(f"{family.replace(' ', '')}-{style}", {"FullName": f"{family} {style}"}, charstrings, {})
    else:
        glyphs = {}
        for name in order:
            pen = TTGlyphPen(None)
            if name != "space":
                _box(pen, adv[name])
            glyphs[name] = pen.glyph()
        fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics({name: (adv[name], 0 if name == "space" else 50) for name in order})
    fb.setupHorizontalHeader(ascent=900, descent=-250)
    fb.setupNameTable({"familyName": family, "styleName": style})
    italic = "Italic" in style
    fb.setupOS2(sTypoAscender=900, sTypoDescender=-250, sTypoLineGap=0, usWinAscent=900, usWinDescent=250,
                fsSelection=0x01 if italic else 0x40, achVendID="TEST")
    fb.setupPost(isFixedPitch=int(mono), italicAngle=-10 if italic else 0)
    if italic:
        fb.updateHead(macStyle=0x02)
    if liga and not icon:
        fb.addOpenTypeFeatures("feature liga { sub uni0066 uni0069 by f_i; } liga;")
    path.parent.mkdir(parents=True, exist_ok=True)
    for name_id, value in (extra_names or {}).items():  # e.g. WWS (21/22) or variations PS prefix (25)
        fb.font["name"].setName(value, name_id, 3, 1, 0x409)
    if os2_version is not None:
        fb.font["OS/2"].version = os2_version
    fb.save(str(path))
    return path


def _box(pen, advance: int) -> None:  # type: ignore[no-untyped-def]
    pen.moveTo((50, 0))
    pen.lineTo((50, 700))
    pen.lineTo((advance - 50, 700))
    pen.lineTo((advance - 50, 0))
    pen.closePath()


@pytest.fixture
def make_font(tmp_path: Path) -> Callable[..., Path]:
    def make(name: str = "FixtureSans-Regular.ttf", **kw) -> Path:  # type: ignore[no-untyped-def]
        return build_font(tmp_path / "src" / name, **kw)
    return make


@pytest.fixture
def family_files(make_font) -> dict[str, Path]:  # type: ignore[no-untyped-def]
    """A two-style base family, as K2's base_font_files() would return it."""
    return {"Regular": make_font("FixtureSans-Regular.ttf"),
            "Italic": make_font("FixtureSans-Italic.ttf", style="Italic")}


def _fonts_by_family(directories: list[Path]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for d in directories:
        for f in sorted(Path(d).glob("*.[ot]tf")):
            with TTFont(f, lazy=True) as font:
                if font["name"].getDebugName(2) == "Regular":
                    found.setdefault(font["name"].getDebugName(1), f)
    return found


class FakeRenderer:
    """M1.6 Renderer without pango-view: resolves families from the private config's <dir>s and
    'renders' by summing advances, so the measurement follows the real font bytes."""

    def __init__(self, missing: tuple[str, ...] = ()) -> None:
        self.missing = missing
        self.resolved: list[str] = []

    def missing_tools(self) -> tuple[str, ...]:
        return self.missing

    def _fonts(self, config_file: Path) -> dict[str, Path]:
        dirs = re.findall(r"<dir>(.*?)</dir>", Path(config_file).read_text())
        return _fonts_by_family([Path(d) for d in dirs])

    def resolve_family(self, family: str, *, config_file: Path) -> tuple[str, str]:
        self.resolved.append(family)
        fonts = self._fonts(config_file)
        hit = fonts.get(family) or next(iter(fonts.values()))  # fontconfig substitutes, never fails
        with TTFont(hit, lazy=True) as font:
            return font["name"].getDebugName(1), str(hit)

    def measure(self, *, family: str, text: str, size_pt: float, config_file: Path, workdir: Path) -> RenderedSize:
        _, path = self.resolve_family(family, config_file=config_file)
        with TTFont(path, lazy=True) as font:
            cmap, hmtx, upm = font.getBestCmap(), font["hmtx"], font["head"].unitsPerEm
            px = size_pt * 96 / 72 / upm
            width = sum(hmtx[cmap[ord(ch)]][0] for ch in text if ord(ch) in cmap) * px
            hhea = font["hhea"]
            return RenderedSize(round(width), round((hhea.ascent - hhea.descent + hhea.lineGap) * px))


class FakeFontconfig:
    """M1.7 FontconfigTool without fc-cache/fc-match: resolves against fonts_dir() only."""

    def __init__(self, *, fail_refresh: bool = False) -> None:
        self.fail_refresh = fail_refresh
        self.refreshed: list[Path] = []

    def missing_tools(self) -> tuple[str, ...]:
        return ()

    def refresh(self, directory: Path) -> str:
        from scribesense.fontgen.cache import FontconfigError

        if self.fail_refresh:
            raise FontconfigError("fc-cache failed")
        self.refreshed.append(directory)
        return ""

    def resolve_family(self, family: str) -> tuple[str, str]:
        fonts = _fonts_by_family(sorted(p for p in paths.fonts_dir().glob("*") if p.is_dir()))
        hit = fonts.get(family)
        return (family, str(hit)) if hit else ("DejaVu Sans", "/usr/share/fonts/dejavu/DejaVuSans.ttf")


@pytest.fixture
def renderer() -> FakeRenderer:
    return FakeRenderer()


@pytest.fixture
def tools() -> FakeFontconfig:
    return FakeFontconfig()
