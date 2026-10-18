"""M2.9 — reader styling (reader_css), the open_reader API shape, and the card's acceptance test.

The ordinary tests never create or show a window: the project sandbox has no display. `reader_css` is
pure and is checked as text, then parsed by a Gtk.CssProvider (which needs no display) to prove GTK
accepts it. The acceptance test (canary text never on disk after a reader session) needs a real window
and is `@pytest.mark.gui`: excluded by default, run locally with `pytest -m gui` on a desktop session.
Skipped when PyGObject or the GTK 4 / libadwaita typelibs are missing.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
try:  # PyGObject alone is not enough: popup needs the GTK 4 + libadwaita typelibs
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError:
    pytest.skip("GTK 4 / libadwaita typelibs not installed", allow_module_level=True)

from gi.repository import Gtk  # noqa: E402

from scribesense.contracts.config import Configuration  # noqa: E402
from scribesense.reader import popup  # noqa: E402

FAMILY = "AccessSans-3f9a1c2b7d10"
CONFIG = Configuration(
    base_family="Atkinson Hyperlegible",
    letter_em=0.06,
    word_scale=1.5,
    reading_line=1.5,
    ui_line=1.2,
    text_scale=1.25,
)


def _parse_errors(css: str) -> list[str]:
    provider = Gtk.CssProvider()
    errors: list[str] = []
    provider.connect("parsing-error", lambda _p, _section, error: errors.append(error.message))
    popup._load_css(provider, css)
    return errors


def test_no_family_no_config_uses_system_font() -> None:
    assert popup.reader_css(None, None) == ""


def test_family_and_config() -> None:
    css = popup.reader_css(FAMILY, CONFIG)
    assert f'font-family: "{FAMILY}";' in css
    assert "line-height: 1.5;" in css
    assert css.startswith("textview {")


def test_family_only_and_config_only() -> None:
    assert "line-height" not in popup.reader_css(FAMILY, None)
    css = popup.reader_css(None, CONFIG)
    assert "font-family" not in css and "line-height: 1.5;" in css


@pytest.mark.parametrize("scale", [1.0, 1.05, 1.5, 2.0])
def test_text_scale_is_not_applied_again(scale: float) -> None:
    # The GTK adapter already sets text-scaling-factor = text_scale and this app inherits it; a
    # font-size here would scale twice (1.5 -> 2.25x). The reader CSS is the same for every scale.
    css = popup.reader_css(FAMILY, replace(CONFIG, text_scale=scale))
    assert "font-size" not in css
    assert css == popup.reader_css(FAMILY, CONFIG)


def test_reading_line_steps_render_exactly() -> None:
    assert "line-height: 1.0;" in popup.reader_css(None, replace(CONFIG, reading_line=1.0))
    assert "line-height: 2.0;" in popup.reader_css(None, replace(CONFIG, reading_line=2.0))


def test_never_adds_spacing() -> None:
    css = popup.reader_css(FAMILY, replace(CONFIG, letter_em=0.3, word_scale=3.0))
    assert "letter-spacing" not in css and "word-spacing" not in css


@pytest.mark.parametrize("family", ["Atkinson Hyperlegible", "Noto Sans", "Carlito", FAMILY])
def test_shortlist_and_generated_names_accepted(family: str) -> None:
    assert f'"{family}"' in popup.reader_css(family, None)


@pytest.mark.parametrize(
    "family",
    [
        "",
        'Evil"; } window { background: red; } textview { font-family: "x',
        "a;b",
        "a{b}",
        "line\nbreak",
        FAMILY + "\n",  # a `$` anchor would accept this; GTK then drops the whole block silently
        "AccessSans-abc\r",
        "back\\slash",
        " leading-space",
        "x" * 65,
        42,
    ],
    ids=["empty", "injection", "semicolon", "braces", "newline", "trailing-newline", "trailing-cr",
         "backslash", "space", "long", "int"],
)
def test_bad_family_rejected(family: object) -> None:
    with pytest.raises(ValueError):
        popup.reader_css(family, None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"reading_line": float("nan")},
        {"reading_line": float("inf")},
        {"reading_line": 0.0},
        {"reading_line": 9.0},
        {"reading_line": True},
        {"reading_line": "1.5"},
    ],
)
def test_bad_config_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        popup.reader_css(None, replace(CONFIG, **changes))


@pytest.mark.parametrize(
    ("family", "config"), [(FAMILY, CONFIG), (None, CONFIG), (FAMILY, None), ("Noto Sans", CONFIG)]
)
def test_gtk_parses_the_css(family: str | None, config: Configuration | None) -> None:
    assert _parse_errors(popup.reader_css(family, config)) == []


def test_open_reader_signature() -> None:
    sig = inspect.signature(popup.open_reader)
    assert list(sig.parameters) == ["application", "text", "family", "config"]
    assert sig.parameters["family"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["config"].kind is inspect.Parameter.KEYWORD_ONLY


def test_reader_has_no_set_attributes_call() -> None:
    # Regression for the crash: Gtk.TextView has no set_attributes in GTK 4.
    assert ".set_attributes(" not in inspect.getsource(popup)


def test_title_is_fixed() -> None:
    assert popup.READER_TITLE == "ScribeSense Reader"


# --- M2.9 acceptance: canary text never on disk after a session (gui, local only) ---------------

CANARY = "CANARY-41c7-reader-window-text-never-on-disk"


def _sweep(roots: Sequence[Path], needle: bytes) -> list[str]:
    hits = []
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                try:
                    if needle in (Path(dirpath) / name).read_bytes():
                        hits.append(name)
                except OSError:
                    continue
    return hits


def _iterate_main_context(rounds: int = 50) -> None:
    from gi.repository import GLib

    context = GLib.MainContext.default()
    for _ in range(rounds):
        while context.pending():
            context.iteration(False)


@pytest.mark.gui
def test_canary_never_on_disk_after_reader_session(sandbox) -> None:
    """Store + logs + data dir sweep after a full reader session (M2.9 acceptance test).

    GTK connects to the display when PyGObject imports Gtk (at collection, before the sandbox fixture
    strips the session variables), so this runs only where a display existed at that point. All files
    the session could write land in the sandbox's HOME / XDG / SCRIBESENSE_* dirs, which are swept.
    """
    from gi.repository import Adw, Gdk

    from scribesense.contracts import paths
    from scribesense.contracts.errors import ErrorCode
    from scribesense.log import log, log_path
    from scribesense.store import Store, default_path

    if Gdk.Display.get_default() is None:
        pytest.skip("no GTK display")
    Adw.init()

    store = Store(default_path())  # a real store file exists to be swept
    assert log(ErrorCode.NOTHING_SELECTED, "reader", step="gui_test")  # and a real log file
    try:
        window = popup.open_reader(None, f"start {CANARY} end", family=FAMILY, config=CONFIG)
        _iterate_main_context()
        buffer = window.text_view.get_buffer()
        shown = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        assert CANARY in shown
        assert CANARY not in (window.get_title() or "")
        window.close()
        _iterate_main_context()
        assert buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) == ""
    finally:
        store.close()

    for path in (default_path(), log_path()):
        assert path.exists()
    roots = [sandbox.root, sandbox.home, sandbox.data_dir, sandbox.config_home, sandbox.runtime_dir,
             paths.data_dir(), paths.logs_dir()]
    assert _sweep([r for r in roots if r.exists()], CANARY.encode()) == []
