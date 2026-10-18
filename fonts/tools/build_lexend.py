"""O1 — rebuild `fonts/Lexend/` from the official variable font `Lexend[wght].ttf` (Lexend 1.007).

Source: google/fonts `ofl/lexend/` at commit 47d54ca1ebb14fd864695f1d39dc734f39017c33 (git blob
f70999106bfa76d48051b73ad7c888573663879c). Licence: SIL Open Font License 1.1 with Reserved Font Name
"RevReading Lexend" (see OFL.txt) — the static instances are named "Lexend", not the reserved name.
Upstream ships only a variable font (`wght` 100–900) and no italic; the pipeline (M1.4–M1.8) wants one static file
per style (K2, R2-7), and Italic/BoldItalic are optional (R2), so this makes:

    Regular = Lexend[wght].ttf at wght 400
    Bold    = Lexend[wght].ttf at wght 700

Name record 25 (the variations PostScript prefix) is meaningless in a static font and is removed. Timestamps are the
source's, so the bytes are reproducible: running this again must give files whose SHA-256 matches
`fonts/manifest.toml` (tests/unit/fontgen/test_bundled_fonts.py checks the manifest against the files).

    uv run --frozen python fonts/tools/build_lexend.py [fonts/Lexend] [path/to/Lexend[wght].ttf]
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

STYLES = {"Regular": 400, "Bold": 700}


def build(source: Path, wght: int) -> bytes:
    vf = TTFont(source)
    created, modified = vf["head"].created, vf["head"].modified
    static = instancer.instantiateVariableFont(vf, {"wght": wght}, updateFontNames=True)
    static["name"].names = [r for r in static["name"].names if r.nameID != 25]
    static["head"].created, static["head"].modified = created, modified
    static.recalcTimestamp = False
    out = io.BytesIO()
    static.save(out, reorderTables=True)
    return out.getvalue()


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1] if len(argv) > 1 else Path(__file__).resolve().parents[1] / "Lexend")
    source = Path(argv[2] if len(argv) > 2 else "Lexend[wght].ttf")
    out_dir.mkdir(parents=True, exist_ok=True)
    for style, wght in STYLES.items():
        (out_dir / f"Lexend-{style}.ttf").write_bytes(build(source, wght))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
