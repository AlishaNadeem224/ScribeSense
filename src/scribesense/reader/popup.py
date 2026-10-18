"""M2.9 — Fallback reader window.

Shows captured text in the reading font and line height [D F7]. The reader is used for applications
that cannot be restyled (canvas apps, PDFs) and for text that cannot be verified.

The card's rules:
- Text is held in memory only: never written to disk, never logged, never put in the window title.
  The buffer is cleared when the window closes (the session ends).
- The text is shown in the reading family of the ACTIVE configuration ("AccessSans-<key>", K7) at
  `config.reading_line`. With no family/configuration it is shown in the default system font, still
  readable.
- No extra spacing CSS: letter and word spacing are built into the generated font. Only font-family and
  line-height are set.

Text size [decision 2026-10-18, pending O2 card-owner sign-off]: the reader does NOT set font-size from
`config.text_scale`. The card asks for "the reading font and line height" only, and the GTK adapter
(M4.3) already applies `text_scale` desktop-wide as `text-scaling-factor`, which this GTK 4 app
inherits. Scaling the TextView again would show the text at text_scale squared (1.5 -> 2.25x). If the GTK
adapter is not applied, the reader stays at the system size, like any other app that ignores the
setting (A11: documented, not promised).

Styling uses a private Gtk.CssProvider attached to the reader's TextView only (Gtk.TextView has no
`set_attributes`). The CSS text comes from `reader_css()`, a pure function that needs no display.

API used by the app (M2.11): `open_reader(application, text, family=..., config=...)`.
"""

from __future__ import annotations

import math
import re

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from scribesense.contracts.config import RANGES, Configuration  # noqa: E402
from scribesense.contracts.fonts import FontSet  # noqa: E402

#: Family names we put into CSS: K7 names ("AccessSans-<key>") and the K1 shortlist names
#: ("Atkinson Hyperlegible", "Noto Sans", ...). No quotes, backslashes, semicolons or braces can get in,
#: so the quoted CSS string needs no escaping. Always used with `fullmatch` (a `$` anchor would let a
#: trailing newline through, and GTK then silently drops the whole declaration block).
_FAMILY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")

#: The reader window's title. Never derived from the text.
READER_TITLE = "ScribeSense Reader"


def _checked_family(family: str) -> str:
    if not isinstance(family, str) or not _FAMILY_RE.fullmatch(family):
        raise ValueError("unsupported font family name")
    return family


def _checked_value(config: Configuration, field: str) -> float:
    """A K1 field that must be a finite number inside RANGES (Configuration is canonical when used)."""
    value = getattr(config, field)
    allowed = RANGES[field]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not (allowed.min <= value <= allowed.max)
    ):
        raise ValueError(f"{field} is not a valid configuration value")
    return float(value)


def reader_css(family: str | None, config: Configuration | None) -> str:
    """CSS for the reader's TextView. Pure: no GTK, no display.

    - family -> `font-family: "<family>";` (validated against `_FAMILY_RE`, else ValueError).
    - config -> `line-height: <reading_line>;` (unitless factor, same as the extension's CSS).
    - Both None -> "" (the default system font is used).
    Never emits font-size (the desktop's text-scaling-factor already applies text_scale; see the module
    docstring) and never letter-spacing or word-spacing (M2.9: the spacing is in the font).
    """
    declarations: list[str] = []
    if family is not None:
        declarations.append(f'font-family: "{_checked_family(family)}";')
    if config is not None:
        line = _checked_value(config, "reading_line")
        declarations.append(f"line-height: {line:.1f};")
    if not declarations:
        return ""
    return "textview {\n" + "".join(f"  {d}\n" for d in declarations) + "}\n"


def _load_css(provider: Gtk.CssProvider, css: str) -> None:
    if hasattr(provider, "load_from_string"):  # GTK >= 4.12
        provider.load_from_string(css)
    else:
        provider.load_from_data(css, -1)


class ReaderWindow(Adw.ApplicationWindow):
    """The fallback reader window. Use `open_reader()`; constructing it directly does not present it.

    `font_set` is accepted for older callers: when `family` is None its `reading_family` is used.
    """

    def __init__(
        self,
        text: str,
        font_set: FontSet | None = None,
        *,
        family: str | None = None,
        config: Configuration | None = None,
        **kwargs: object,
    ) -> None:
        if family is None and font_set is not None:
            family = font_set.reading_family
        css = reader_css(family, config)  # validate before building any widget
        super().__init__(**kwargs)
        self.set_title(READER_TITLE)
        self.set_default_size(600, 700)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)

        self.text_view = Gtk.TextView()
        self.text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.text_view.set_editable(False)
        self.text_view.set_cursor_visible(False)
        self.text_view.set_margin_top(24)
        self.text_view.set_margin_bottom(24)
        self.text_view.set_margin_start(24)
        self.text_view.set_margin_end(24)

        buffer = self.text_view.get_buffer()
        buffer.set_text(text)

        if css:
            # Private provider on this TextView only: the rest of the app keeps its own fonts.
            self._css_provider = Gtk.CssProvider()
            _load_css(self._css_provider, css)
            self.text_view.get_style_context().add_provider(
                self._css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        if config is not None:
            # The same line height again as a whole-buffer tag: a TextView lays out lines from its
            # text attributes, so this makes the reading line height hold whichever way GTK honours it.
            tag = buffer.create_tag("reading-line", line_height=config.reading_line)
            buffer.apply_tag(tag, buffer.get_start_iter(), buffer.get_end_iter())

        scroller.set_child(self.text_view)
        toolbar.set_content(scroller)
        self.set_content(toolbar)
        self.connect("close-request", self._on_close_request)

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        """The session ends: drop the text from memory. Returning False lets the window close."""
        self.text_view.get_buffer().set_text("")
        return False


def open_reader(
    application: Gtk.Application | None,
    text: str,
    *,
    family: str | None,
    config: Configuration | None,
) -> Gtk.Window:
    """Create, present and return a reader window showing `text` (M2.9).

    `family` is the reading family of the active configuration ("AccessSans-<key>") and `config` that
    configuration; None for either falls back to the system font / line height. The text size always
    follows the desktop's text-scaling-factor (set from text_scale by the GTK adapter).
    Raises ValueError for an unsupported family name or a non-canonical configuration value.
    """
    window = ReaderWindow(text, family=family, config=config, application=application)
    window.present()
    return window
