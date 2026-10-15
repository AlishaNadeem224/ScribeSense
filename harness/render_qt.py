"""O3 — fresh-process Qt probe for harness.render (M3.4). `python -m harness.render_qt <family> <sample>`.

Verified 2026-10-15 (PyQt6 from the system Python): patched vs unpatched font distinguished. Runs with QT_QPA_PLATFORM=offscreen and reads the
raw font of each glyph run, i.e. the font Qt actually draws with. Exit 3: PyQt6 missing · 4: failed.
"""

from __future__ import annotations

import json
import os
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 2
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    try:
        from PyQt6.QtGui import QFont, QGuiApplication, QTextLayout
    except ImportError:
        return 3
    try:
        app = QGuiApplication(sys.argv[:1])  # noqa: F841 — must stay alive while laying out
        layout = QTextLayout(argv[1], QFont(argv[0], 12))
        layout.beginLayout()
        line = layout.createLine()
        line.setLineWidth(10_000)
        layout.endLayout()
        families: list[str] = []
        for run in layout.glyphRuns():
            name = run.rawFont().familyName()
            if not name:
                return 4  # fail closed: an unidentified run must not count as the expected family
            if name not in families:
                families.append(name)
    except Exception:  # noqa: BLE001
        return 4
    if not families:
        return 4
    sys.stdout.write(json.dumps({"families": families}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
