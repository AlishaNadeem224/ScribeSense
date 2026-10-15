"""O3 — "drawn font" checks per engine, for tests (M3.4, A14).

    drawn_family(engine, sample, family="sans-serif") -> str

Asks `engine` to lay out `sample` as an app would when it requests `family`, and returns the family the
engine ACTUALLY DREW with (not the one it was asked for — that is the configuration check, A14).
If several families were used (fallback runs) they are joined with ", ", so a comparison with a single
expected family fails loudly instead of passing on the first run.

Engines:
    pango    fresh `python3` process using PangoCairo (reuses adapters.pango_probe.drawn_families).
    qt       fresh process, PyQt6 offscreen, QTextLayout glyph runs.           [verified 2026-10-15]
    firefox  headless Firefox, temp profile, --no-remote --new-instance.        [not implemented]
    chromium headless Brave/Chromium, temp user-data-dir.                       [not implemented]

An engine that is missing or not implemented raises EngineUnavailable (a reason, never a value); tests
turn it into pytest.skip via `require_engine()`. It is NEVER reported as a pass (card: "skipped with
reason, not passed").

Rules (card + AGENTS.md):
  - Runs only inside the bubblewrap runner (SCRIBESENSE_SANDBOX=bwrap) — fresh processes inherit its private
    HOME / FONTCONFIG_FILE and cannot reach the user's desktop or browser.
  - Processes start only through run_cmd (K8) with a timeout and are always reaped.
  - `sample` is test text only. Never pass reader/document text.
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Literal

from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.paths import run_cmd

Engine = Literal["pango", "qt", "firefox", "chromium"]
ENGINES: tuple[str, ...] = ("pango", "qt", "firefox", "chromium")
SYSTEM_PYTHON = "/usr/bin/python3"  # PyGObject / PyQt6 come from the system Python (A4)
TIMEOUT_S = 30.0
MAX_SAMPLE_LEN = 500


class EngineUnavailable(Exception):
    """The engine cannot run here; `reason` says why. Not a failure of the code under test."""

    def __init__(self, engine: str, reason: str) -> None:
        super().__init__(f"{engine}: {reason}")
        self.engine = engine
        self.reason = reason


def _in_sandbox() -> bool:
    return os.environ.get("SCRIBESENSE_SANDBOX") == "bwrap"


def _probe(module: str, family: str, sample: str) -> list[str]:
    """Run `python3 -m <module> <family> <sample>` and parse its one JSON line (availability is checked first)."""
    done = run_cmd([SYSTEM_PYTHON, "-m", module, family, sample], TIMEOUT_S)
    try:
        data = json.loads(done.stdout.decode().strip().splitlines()[-1])
        families = data["families"]
    except (ValueError, IndexError, KeyError, TypeError):
        raise ScribeSenseError(ErrorCode.CHECK_UNAVAILABLE) from None
    if not (isinstance(families, list) and families and all(isinstance(f, str) and f for f in families)):
        raise ScribeSenseError(ErrorCode.CHECK_UNAVAILABLE)
    return families


def unavailable_reason(engine: str) -> str | None:
    """Why `engine` cannot run here, or None if it can. Read-only; starts no process of its own."""
    if engine not in ENGINES:
        raise ValueError(f"unknown engine {engine!r}; expected one of {ENGINES}")
    if engine in ("firefox", "chromium"):
        binary = {"firefox": "firefox", "chromium": "brave"}[engine]
        have = shutil.which(binary) is not None or (engine == "chromium" and shutil.which("chromium") is not None)
        return (f"drawn-font check for {engine} is not implemented yet"
                + ("" if have else f" (and {binary} is not installed)"))
    if not _in_sandbox():
        return "drawn-font checks run only inside the bubblewrap runner (M3.3)"
    if engine == "pango":
        try:
            run_cmd([SYSTEM_PYTHON, "-c",
                     "import gi;gi.require_version('Pango','1.0');gi.require_version('PangoCairo','1.0');"
                     "from gi.repository import PangoCairo"], 10.0)
        except ScribeSenseError:
            return "PangoCairo not importable (dnf install gobject-introspection pango)"
    if engine == "qt":
        try:
            run_cmd([SYSTEM_PYTHON, "-c", "import PyQt6.QtGui"], 10.0)
        except ScribeSenseError:
            return "PyQt6 not importable"
    return None


def require_engine(engine: str) -> None:
    """For tests: skip (with the reason) when `engine` cannot run — never pass."""
    import pytest

    reason = unavailable_reason(engine)
    if reason:
        pytest.skip(f"{engine}: {reason}")


def drawn_family(engine: str, sample: str, family: str = "sans-serif") -> str:
    if engine not in ENGINES:
        raise ValueError(f"unknown engine {engine!r}; expected one of {ENGINES}")
    if not sample or len(sample) > MAX_SAMPLE_LEN or not sample.isprintable():
        raise ValueError("sample must be 1..500 printable characters of test text")
    if not family or not family.isprintable():
        raise ValueError("family must be a non-empty printable string")
    reason = unavailable_reason(engine)
    if reason:
        raise EngineUnavailable(engine, reason)
    module = {"pango": "harness.render_pango", "qt": "harness.render_qt"}[engine]
    return ", ".join(_probe(module, family, sample))
