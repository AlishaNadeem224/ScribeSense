"""M2.9 — Fallback reader window.

Shows captured text in the generated font family. The reader is used for
applications that cannot be restyled (canvas apps, PDFs) and for text that
cannot be verified.

The card's rules:
- Text is held in memory only. Never written to disk, never logged.
- The text is displayed using the active font set (or a fallback if none is active).
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gtk, Pango  # noqa: E402

from scribesense.contracts.fonts import FontSet


class ReaderWindow(Adw.ApplicationWindow):
    """The fallback reader window."""

    def __init__(self, text: str, font_set: FontSet | None, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.set_title("ScribeSense Reader")
        self.set_default_size(600, 700)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)

        # The text view
        self.text_view = Gtk.TextView()
        self.text_view.set_wrap_mode(Gtk.WrapMode.WORD)
        self.text_view.set_editable(False)
        self.text_view.set_cursor_visible(False)
        self.text_view.set_margin_top(24)
        self.text_view.set_margin_bottom(24)
        self.text_view.set_margin_start(24)
        self.text_view.set_margin_end(24)

        buffer = self.text_view.get_buffer()
        buffer.set_text(text)

        # Apply the font if we have one
        if font_set is not None:
            attr_list = Pango.AttrList()
            attr_list.insert(Pango.attr_family_new(font_set.reading_family))
            self.text_view.set_attributes(attr_list)

        scroller.set_child(self.text_view)
        toolbar.set_content(scroller)
        self.set_content(toolbar)