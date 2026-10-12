"""O4 — Pango drawn-font probe (M4.13, A14). Run as a FRESH process: `python -m scribesense.adapters.pango_probe`.

    python -m scribesense.adapters.pango_probe <query> <sample>

Lays out `sample` with a font description whose family is `query` and prints which font families Pango
actually used for the runs, as ONE JSON line on stdout:

    {"v": 1, "families": ["AccessSans-abc123"]}

Why a separate process: a long-running process keeps the fontconfig configuration it loaded at start-up
and would never see rules written by an apply. A fresh interpreter loads the current configuration.

Rules:
  - Only fixed samples (SAMPLES) are accepted — never user/document text (§4 text privacy).
  - Imports only Pango / PangoCairo (GObject introspection) lazily inside main(); never Gtk/Adw, never
    scribesense.safety / ui (A2, import rules R5/R6). PangoCairo's default font map gives a context
    without any drawing surface.
  - Any failure (gi missing, Pango missing, no font, bad arguments) -> nothing on stdout, nothing on
    stderr, exit code != 0. The caller (verify.drawn_family_pango) sees run_cmd's ADAPTER_COMMAND_FAILED.
    No traceback is ever printed (it could echo the arguments).
"""

from __future__ import annotations

import json
import sys

#: The fixed Latin sample laid out by the drawn check. Never user content.
SAMPLE = "The quick brown fox jumps over the lazy dog 0123456789"
#: Every sample the probe accepts (closed list).
SAMPLES: tuple[str, ...] = (SAMPLE,)
#: Longest family query accepted.
MAX_QUERY_LEN = 200
#: Point size used for the layout (any size works; fontconfig matching does not depend on it here).
PROBE_SIZE_PT = 12

EXIT_OK = 0
EXIT_BAD_ARGS = 2
EXIT_NO_PANGO = 3
EXIT_FAILED = 4


def valid_query(query: object) -> bool:
    """A family query: str, 1..MAX_QUERY_LEN chars, printable (no control chars), not only blanks."""
    return (
        isinstance(query, str)
        and 0 < len(query) <= MAX_QUERY_LEN
        and query.isprintable()
        and bool(query.strip())
    )


def _load_pango():  # type: ignore[no-untyped-def]
    import gi

    gi.require_version("Pango", "1.0")
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import Pango, PangoCairo

    return Pango, PangoCairo


def drawn_families(query: str, sample: str) -> list[str]:
    """Distinct families Pango used for the runs of `sample`, in first-use order. Raises on any failure."""
    Pango, PangoCairo = _load_pango()
    context = PangoCairo.FontMap.get_default().create_context()
    layout = Pango.Layout.new(context)
    desc = Pango.FontDescription()
    # set_family, not from_string: a family ending in a style word or a number ("Foo Bold", "Foo 2")
    # must not be parsed as style / size.
    desc.set_family(query)
    desc.set_size(PROBE_SIZE_PT * Pango.SCALE)
    layout.set_font_description(desc)
    layout.set_text(sample, -1)

    families: list[str] = []
    it = layout.get_iter()
    while True:
        run = it.get_run()
        if run is not None:  # None marks the end of a line
            family = run.item.analysis.font.describe().get_family()
            if not isinstance(family, str) or not family:
                # Fail closed: a run from an unidentified font must not count as the reading family.
                raise ValueError("run without a family name")
            if family not in families:
                families.append(family)
        if not it.next_run():
            break
    return families


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or not valid_query(args[0]) or args[1] not in SAMPLES:
        return EXIT_BAD_ARGS
    query, sample = args
    try:
        _load_pango()
    except Exception:  # noqa: BLE001 — gi / Pango missing: report by exit code only
        return EXIT_NO_PANGO
    try:
        families = drawn_families(query, sample)
    except Exception:  # noqa: BLE001 — never print a traceback (it could echo the arguments)
        return EXIT_FAILED
    if not families:
        return EXIT_FAILED
    sys.stdout.write(json.dumps({"v": 1, "families": families}) + "\n")
    sys.stdout.flush()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
