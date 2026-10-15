"""O3 — tiny valid TrueType fonts for render checks (M3.4). Pure Python (fontTools), no external tools."""

from __future__ import annotations

import re
from pathlib import Path


def make_test_font(path: Path, family: str) -> None:
    """A TrueType font named `family` with box glyphs for printable ASCII."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    def box() -> object:
        pen = TTGlyphPen(None)
        pen.moveTo((50, 0))
        pen.lineTo((50, 700))
        pen.lineTo((550, 700))
        pen.lineTo((550, 0))
        pen.closePath()
        return pen.glyph()

    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder([".notdef", "space", "box"])
    fb.setupCharacterMap({0x20: "space", **{cp: "box" for cp in range(0x21, 0x7F)}})
    fb.setupGlyf({".notdef": box(), "space": TTGlyphPen(None).glyph(), "box": box()})
    fb.setupHorizontalMetrics({".notdef": (600, 50), "space": (300, 0), "box": (600, 50)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    ps = re.sub(r"[^A-Za-z0-9-]", "", family) + "-Regular"
    fb.setupNameTable({"familyName": family, "styleName": "Regular", "uniqueFontIdentifier": ps,
                       "fullName": f"{family} Regular", "psName": ps})
    fb.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    fb.setupPost()
    path.parent.mkdir(parents=True, exist_ok=True)
    fb.save(str(path))
