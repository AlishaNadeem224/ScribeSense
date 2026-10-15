"""O3 — runs INSIDE the bubblewrap sandbox: pytest in-process, result reported on the last line.

`run_cmd` (K8) drops the output of a failing command, which would hide the pytest report exactly when
tests fail. So this module always exits 0 and prints `SCRIBESENSE_EXIT=<pytest exit code>` last; the
runner (harness/run_integration.py) reads it. A non-zero exit therefore always means bwrap itself failed.
"""

from __future__ import annotations

import os
import sys

EXIT_MARKER = "SCRIBESENSE_EXIT="


def main(argv: list[str]) -> int:
    import pytest

    if os.environ.get("SCRIBESENSE_SANDBOX") != "bwrap":
        print("harness.inside must run inside the bubblewrap sandbox", file=sys.stderr)
        return 2
    # The repo is mounted read-only: no pytest cache, no bytecode.
    code = int(pytest.main(["-p", "no:cacheprovider", *argv]))
    sys.stdout.flush()
    print(f"\n{EXIT_MARKER}{code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
