"""Build-time helper for the RPM (W2, 2026-11-24): the extension ID and the browsers' pointer file for a signed .crx.

    python3 -I packaging/crx_pointer.py id <scribesense.crx>        prints the extension ID (32 letters a-p)
    python3 -I packaging/crx_pointer.py pointer <scribesense.crx>   prints the <id>.json content the browsers read

It loads `src/scribesense/safety/root_helper.py` by path (standard library only), so the package uses exactly the same
CRX3 validation and pointer format as the development root step — two implementations would be two ways to get it
wrong. Never run as root and never installed: the spec calls it from the build tree.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "src" / "scribesense" / "safety" / "root_helper.py"


def _root_helper():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("scribesense_root_helper", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("id", "pointer"):
        print("usage: crx_pointer.py id|pointer <scribesense.crx>", file=sys.stderr)
        return 2
    helper = _root_helper()
    try:
        ext_id, version = helper.crx_info(Path(argv[2]).read_bytes())
    except (OSError, helper.HelperError) as exc:
        print(f"crx_pointer: {exc}", file=sys.stderr)
        return 1
    if argv[1] == "id":
        print(ext_id)
    else:
        sys.stdout.write(helper.pointer(version).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
