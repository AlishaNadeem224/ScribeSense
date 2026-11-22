"""M1.3 -- out-of-process eligibility probe. Run as a FRESH process:

    python -m scribesense.config.fontprobe <path>

Prints the verdict as ONE JSON line on stdout::

    {"v": 1, "ok": true, "reason_code": null, "warnings": ["PARTIAL_LATIN_COVERAGE"]}

Why a separate process
----------------------
`rules.MAX_FONT_BYTES` is a size cap, not the parse budget Research2 D.2/D.6 actually ask
for: font parsers crash and are a documented attack surface (Liang et al. 2017; Endignoux
et al. 2016), corrupt and adversarial font files are a known input class (Park et al. 2026;
Kim et al. 2026), and malicious web fonts are demonstrably feasible (Mueller et al. 2019).
A small but pathological file -- a decompression bomb, or declared counts that force
quadratic work -- passes a size cap and then consumes unbounded time, or takes the whole
process down, inside a parser no one here wrote.

A wall-clock budget needs something that can be *killed*, which a function call is not. So
the parse happens in a child that `run_cmd` will SIGTERM then SIGKILL at the deadline
(K3 rule 9). Whatever the child does -- hang, abort, segfault, exhaust memory -- the caller
gets a refusal with a reason code and the application keeps running.

This matters most for **user-imported fonts** (PLAN_v2 Phase III, W1.3: "Parse imported
files in a separate process with a timeout"), because the generated font is later loaded by
every application's font engine on the machine. The bundled families are pinned by SHA-256
in `fonts/manifest.toml` and do not need it, which is why `rules.is_eligible` leaves
isolation off by default -- a Python interpreter per font file is real cost on a path M1.5
walks once per style per build.

Rules this module follows (the `adapters/pango_probe` pattern)
--------------------------------------------------------------
* Imports only the standard library and `scribesense.config.rules` -- never ui, store,
  adapters, safety or controller (import rule R4), and never `gi`.
* One JSON line on stdout and nothing else. **No traceback is ever printed**: a traceback
  can echo the path it was given, and §4's logging rule keeps file paths out of logs.
* Any failure -> empty stdout and a non-zero exit, which the caller maps to
  `FONT_UNREADABLE`. A refusal the probe reaches *deliberately* (a monospace font, say) is
  exit 0 with ``"ok": false``: that is a verdict, not a failure.
* `sys.executable` is the interpreter -- the same assumption `adapters/pango_probe` makes.
  It is wrong only in a frozen or embedded build, which this project does not produce.
"""

from __future__ import annotations

import json
import sys

PROTOCOL_VERSION = 1

EXIT_OK = 0
EXIT_BAD_ARGS = 2
EXIT_FAILED = 4

MAX_PATH_LEN = 4096
"""Longest path the probe accepts. Bounded so a malformed argv cannot be unbounded work."""


def valid_path_arg(value: object) -> bool:
    """A path argument: a non-blank string within `MAX_PATH_LEN`, with no NUL."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_PATH_LEN
        and value.strip() != ""
        and "\x00" not in value
    )


def main(argv: list[str]) -> int:
    """``argv`` is ``sys.argv[1:]``. Returns the process exit code."""
    if len(argv) != 1 or not valid_path_arg(argv[0]):
        return EXIT_BAD_ARGS

    try:
        # Imported here, not at module scope: an import failure must be an exit code, not a
        # traceback on stderr, and the cost belongs to the run rather than to `-m` start-up.
        from scribesense.config.rules import verdict

        result = verdict(argv[0])
        line = json.dumps(
            {
                "v": PROTOCOL_VERSION,
                "ok": result.ok,
                "reason_code": result.reason_code,
                "warnings": list(result.warnings),
            },
            separators=(",", ":"),
        )
    except BaseException:  # noqa: BLE001 -- see the module docstring: never a traceback
        # Deliberately BaseException: a parser that exhausts memory raises MemoryError, and
        # a child that dies loudly is worse than one that reports a clean failure. Anything
        # the interpreter cannot survive at all is handled by the caller's timeout instead.
        return EXIT_FAILED

    sys.stdout.write(line + "\n")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover -- exercised through run_cmd
    raise SystemExit(main(sys.argv[1:]))
