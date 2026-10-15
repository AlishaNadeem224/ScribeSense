"""O3 — fresh-process Pango probe for harness.render (M3.4). `python -m harness.render_pango <family> <sample>`.

Prints one JSON line {"families": [...]}. Exit 3: PangoCairo not importable · 4: layout failed.
A fresh process matters: a long-lived one keeps the fontconfig configuration it loaded at start-up.
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 2
    try:
        from scribesense.adapters.pango_probe import drawn_families
    except Exception:  # noqa: BLE001
        return 3
    try:
        families = drawn_families(argv[0], argv[1])
    except ImportError:
        return 3
    except Exception:  # noqa: BLE001
        return 4
    sys.stdout.write(json.dumps({"families": families}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
