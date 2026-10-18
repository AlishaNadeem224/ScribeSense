"""O1 — rebuild `fonts/Noto Sans/` from Fedora's `google-noto-sans-vf-fonts` (Noto Sans 2.015).

Licence: SIL Open Font License 1.1, no Reserved Font Name (see OFL.txt) — static instances may keep the name.
The upstream ships Noto Sans as two variable fonts (upright and italic, `wght` 400–700); the pipeline (M1.4–M1.8)
wants one static file per style (K2, R2-7), so this makes them with fontTools' instancer:

    Regular    = NotoSans[wght].ttf         at wght 400
    Bold       = NotoSans[wght].ttf         at wght 700
    Italic     = NotoSans-Italic[wght].ttf  at wght 400
    BoldItalic = NotoSans-Italic[wght].ttf  at wght 700

Name record 25 (the variations PostScript prefix) is meaningless in a static font and is removed. Timestamps are the
source's, so the bytes are reproducible: running this again must give files whose SHA-256 matches
`fonts/manifest.toml` (tests/unit/fontgen/test_bundled_fonts.py checks the manifest against the files).

    uv run --frozen python fonts/tools/build_noto_sans.py ["fonts/Noto Sans"] [/usr/share/fonts/google-noto-vf]
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

STYLES = {
    "Regular": ("NotoSans[wght].ttf", 400),
    "Bold": ("NotoSans[wght].ttf", 700),
    "Italic": ("NotoSans-Italic[wght].ttf", 400),
    "BoldItalic": ("NotoSans-Italic[wght].ttf", 700),
}


def build(src_dir: Path, style: str) -> bytes:
    name, wght = STYLES[style]
    vf = TTFont(src_dir / name)
    created, modified = vf["head"].created, vf["head"].modified
    static = instancer.instantiateVariableFont(vf, {"wght": wght}, updateFontNames=True)
    static["name"].names = [r for r in static["name"].names if r.nameID != 25]
    static["head"].created, static["head"].modified = created, modified
    static.recalcTimestamp = False
    out = io.BytesIO()
    static.save(out, reorderTables=True)
    return out.getvalue()


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1] if len(argv) > 1 else Path(__file__).resolve().parents[1] / "Noto Sans")
    src_dir = Path(argv[2] if len(argv) > 2 else "/usr/share/fonts/google-noto-vf")
    out_dir.mkdir(parents=True, exist_ok=True)
    for style in STYLES:
        (out_dir / f"NotoSans-{style}.ttf").write_bytes(build(src_dir, style))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
