"""O3 — `python -m harness.run_integration [pytest args]` runs the integration tests in bubblewrap (M3.3).

Fails closed: if bubblewrap is missing or cannot start, nothing runs and the exit code is 3.
Exit codes: pytest's own (0 = passed, 1 = tests failed, 5 = none collected …) · 3 = SANDBOX_UNAVAILABLE.
Default selection is `-m integration` over tests/ (integration tests also live beside their module's unit tests); extra arguments replace the default paths.
"""

from __future__ import annotations

import site
import sys
import sysconfig
from pathlib import Path

from harness import sandbox
from harness.inside import EXIT_MARKER
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.paths import run_cmd

REPO = Path(__file__).resolve().parents[1]
SANDBOX_PYTHON = "/usr/bin/python3"
TIMEOUT_S = 900.0
EXIT_SANDBOX_UNAVAILABLE = 3


def site_packages() -> tuple[Path, ...]:
    """Packages the sandboxed tests import: this environment's (fontTools, pytest). The system Python's own
    (PyGObject) is already visible through the read-only /usr mount."""
    found = {Path(p) for p in (sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"])}
    found |= {Path(p) for p in site.getsitepackages()}
    system = Path("/usr")
    return tuple(sorted(p for p in found if p.is_dir() and not p.is_relative_to(system)))


def parse_exit(stdout: str) -> int | None:
    """The pytest exit code the sandboxed process reported, or None if it never got that far."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(EXIT_MARKER):
            try:
                return int(line[len(EXIT_MARKER):])
            except ValueError:
                return None
    return None


def main(argv: list[str]) -> int:
    try:
        sandbox.check_available(repo=REPO)
    except ScribeSenseError as exc:
        print(f"{exc.code.value}: bubblewrap is missing or cannot create the sandbox; nothing was run.\n"
              "Fedora: sudo dnf install bubblewrap", file=sys.stderr)
        return EXIT_SANDBOX_UNAVAILABLE
    exe = sandbox.bwrap_path()
    assert exe is not None
    pytest_args = argv or ["tests"]
    command = [SANDBOX_PYTHON, "-m", "harness.inside", "-m", "integration", "-ra", *pytest_args]
    try:
        done = run_cmd(sandbox.build_argv(command, repo=REPO, site_packages=site_packages(), bwrap=exe),
                       timeout=TIMEOUT_S)
    except ScribeSenseError as exc:
        print(f"{exc.code.value}: the sandbox did not complete; treating it as {ErrorCode.SANDBOX_UNAVAILABLE.value}",
              file=sys.stderr)
        return EXIT_SANDBOX_UNAVAILABLE
    out = done.stdout.decode(errors="replace")
    sys.stdout.write(out)
    sys.stderr.write(done.stderr.decode(errors="replace"))
    code = parse_exit(out)
    return EXIT_SANDBOX_UNAVAILABLE if code is None else code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
