"""O4 — Verify helpers (M4.13): which font a target CHOOSES and which font is actually DRAWN.

Two verification levels (A14):
    chosen_family(target, query)   configuration check — the family fontconfig picks for a pattern query
                                   (host `fc-match`, or `flatpak run --command=fc-match <app-id>` inside a
                                   Flatpak sandbox).
    drawn_family_pango(query)      drawn check — the families Pango actually uses to lay out the fixed
                                   Latin SAMPLE, in a FRESH process (scribesense.adapters.pango_probe):
                                   a long-running process keeps its old fontconfig configuration.
A passing configuration check is never proof that a font is drawn (card "Must never"): adapters whose
target needs a drawn check report failure when that check cannot run.

Lives in adapters/ (not safety/) so adapters may import it (import rule R5).
External commands only through the injectable runner (default contracts.paths.run_cmd, K3 rule 9).

Error mapping (both helpers):
    runner raises ScribeSenseError       -> passed through unchanged (ADAPTER_UNAVAILABLE / _TIMEOUT /
                                            _COMMAND_FAILED stay distinguishable for the caller)
    runner raises OSError (launch refused, ...) -> ScribeSenseError(CHECK_UNAVAILABLE)
    empty / unparsable output            -> ScribeSenseError(CHECK_UNAVAILABLE)
    invalid query or Flatpak app id      -> ScribeSenseError(UNEXPECTED_ADAPTER_ERROR); nothing is run
Nothing here logs: query strings are font names, which never go to the log.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from typing import Any

from scribesense.adapters.pango_probe import MAX_QUERY_LEN, SAMPLE, SAMPLES, valid_query
from scribesense.contracts.adapter import DEFAULT_TIMEOUTS, Target
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.paths import run_cmd

__all__ = [
    "MAX_QUERY_LEN", "PROBE_MODULE", "SAMPLE", "Runner",
    "chosen_family", "drawn_family_pango", "escape_fc_pattern", "flatpak_app_id",
]

#: Same signature as run_cmd; the result needs only a bytes `.stdout`.
Runner = Callable[[list[str], float], Any]

FC_FORMAT = "%{family[0]}"
FLATPAK_ADAPTER = "flatpak"
FLATPAK_PREFIX = "flatpak:"
PROBE_MODULE = "scribesense.adapters.pango_probe"

#: Flatpak application id: reverse-DNS, >= 2 dot-separated elements of [A-Za-z0-9_-], none starting with
#: a digit or '-', at most 255 chars. A leading '-' is impossible, so flatpak can never read it as an option.
_APP_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)+$")
_APP_ID_MAX = 255
#: fontconfig pattern syntax: '-' starts the size, ':' starts properties, ',' separates families,
#: '\' escapes. Each is escaped with a backslash (so an escaped query never starts with '-').
_FC_SPECIAL = re.compile(r"([\\\-:,])")


def escape_fc_pattern(family: str) -> str:
    """Escape a family name for a fontconfig pattern ("sans-serif" -> "sans\\-serif")."""
    return _FC_SPECIAL.sub(r"\\\1", family)


def _check_query(query: object) -> str:
    if not valid_query(query):
        raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR)
    return query  # type: ignore[return-value]


def flatpak_app_id(target: Target) -> str:
    """The validated app id of a Flatpak target ("flatpak:org.gnome.gedit" -> "org.gnome.gedit")."""
    tid = target.target_id
    app_id = tid[len(FLATPAK_PREFIX):] if isinstance(tid, str) and tid.startswith(FLATPAK_PREFIX) else ""
    if len(app_id) > _APP_ID_MAX or not _APP_ID.fullmatch(app_id):
        raise ScribeSenseError(ErrorCode.UNEXPECTED_ADAPTER_ERROR)
    return app_id


def _run(runner: Runner, argv: list[str], timeout: float) -> str:
    try:
        proc = runner(argv, timeout)
    except ScribeSenseError:
        raise
    except OSError:
        raise ScribeSenseError(ErrorCode.CHECK_UNAVAILABLE) from None
    out = getattr(proc, "stdout", None)
    return (out if isinstance(out, bytes) else b"").decode("utf-8", errors="replace").strip()


def chosen_family(target: Target, query: str, *, runner: Runner = run_cmd, timeout: float | None = None) -> str:
    """Configuration check: the family fontconfig CHOOSES for `query` in `target`'s environment.

    Host targets run `fc-match -f %{family[0]} <escaped query>` (DEFAULT_TIMEOUTS["fc-match"]);
    Flatpak targets run fc-match inside the app's sandbox (DEFAULT_TIMEOUTS["flatpak-fc-match"]).
    `timeout` overrides the default (an adapter's card TIMEOUTS).
    """
    pattern = escape_fc_pattern(_check_query(query))
    if target.adapter == FLATPAK_ADAPTER:
        app_id = flatpak_app_id(target)
        argv = ["flatpak", "run", "--command=fc-match", app_id, "-f", FC_FORMAT, pattern]
        deadline = DEFAULT_TIMEOUTS["flatpak-fc-match"] if timeout is None else timeout
    else:
        argv = ["fc-match", "-f", FC_FORMAT, pattern]
        deadline = DEFAULT_TIMEOUTS["fc-match"] if timeout is None else timeout
    family = _run(runner, argv, deadline)
    if not family:
        raise ScribeSenseError(ErrorCode.CHECK_UNAVAILABLE)
    return family


def probe_argv(query: str, sample: str = SAMPLE) -> list[str]:
    """The fresh-process command line of the Pango probe (sample must be a fixed SAMPLES entry)."""
    if sample not in SAMPLES:
        raise ValueError("the drawn check only lays out fixed samples, never user content")
    # -P (Python >= 3.11): do not put the current directory on sys.path, so a stray module in the
    # caller's working directory can never shadow the probe or gi.
    return [sys.executable, "-P", "-m", PROBE_MODULE, _check_query(query), sample]


def parse_probe_output(text: str) -> tuple[str, ...]:
    """Distinct families from the probe's JSON line, in first-use order. Anything else -> CHECK_UNAVAILABLE."""
    try:
        data = json.loads(text)
    except ValueError:
        raise ScribeSenseError(ErrorCode.CHECK_UNAVAILABLE) from None
    families = data.get("families") if isinstance(data, dict) and data.get("v") == 1 else None
    if not isinstance(families, list) or not families or not all(isinstance(f, str) and f for f in families):
        raise ScribeSenseError(ErrorCode.CHECK_UNAVAILABLE)
    return tuple(dict.fromkeys(families))


def drawn_family_pango(query: str, sample: str = SAMPLE, *, runner: Runner = run_cmd,
                       timeout: float | None = None) -> tuple[str, ...]:
    """Drawn check (A14): the families Pango actually uses to lay out the fixed `sample` for `query`.

    Runs `python -P -m scribesense.adapters.pango_probe <query> <sample>` in a fresh process
    (DEFAULT_TIMEOUTS["other"] unless `timeout` is given). Returns the distinct families of the runs in
    first-use order: a single entry means every run was drawn in that family.
    """
    argv = probe_argv(query, sample)
    deadline = DEFAULT_TIMEOUTS["other"] if timeout is None else timeout
    text = _run(runner, argv, deadline)
    if not text:
        raise ScribeSenseError(ErrorCode.CHECK_UNAVAILABLE)
    return parse_probe_output(text)
