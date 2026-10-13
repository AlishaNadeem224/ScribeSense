"""M2.4 — the application window.

A single Adw.ApplicationWindow containing an Adw.NavigationView. Later cards
push real pages onto the navigation view (M2.5 presets, M2.6 coverage, M2.7
settings, M2.9 reader). For now the window shows one placeholder page so the
shell is runnable and the accessibility checklist has something to check.

The window is never required by the recovery path (A2) and holds no state the
service needs. Closing the window does not quit the application (A1).
"""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402  (after require_version)


class ScribeSenseWindow(Adw.ApplicationWindow):
    """The single top-level window for the application."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

        self.set_title("ScribeSense")
        self.set_default_size(720, 560)

        # NavigationView lets later cards push pages without opening new
        # windows, and gives keyboard back-navigation + screen-reader support
        # for free (both items on the M2.4 accessibility checklist).
        self.nav = Adw.NavigationView()
        self.set_content(self.nav)
        self.nav.push(self._build_placeholder_page())

        # Closing the window must not quit the service (A1). The application
        # object decides that; here we simply allow the default close.
        self.connect("close-request", self._on_close_request)

    def _build_placeholder_page(self) -> Adw.NavigationPage:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(36)
        box.set_margin_bottom(36)
        box.set_margin_start(36)
        box.set_margin_end(36)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)

        title = Gtk.Label(label="ScribeSense")
        title.add_css_class("title-1")
        # Explicit accessible label so screen readers announce it as a heading,
        # not just as another paragraph of text.
        title.update_property([Gtk.AccessibleProperty.LABEL], ["ScribeSense"])

        body = Gtk.Label(
            label=(
                "The application shell is in place. Presets, preview, apply, "
                "and the coverage report arrive in later steps."
            )
        )
        body.set_wrap(True)
        body.set_justify(Gtk.Justification.CENTER)
        body.set_max_width_chars(60)

        box.append(title)
        box.append(body)

        toolbar.set_content(box)
        return Adw.NavigationPage(child=toolbar, title="ScribeSense")

    def _on_close_request(self, *_args: object) -> bool:
        # Return False: let GTK close the window. The application keeps running
        # because ScribeSenseApp holds it (A1). Only the explicit `quit` action
        # stops the service.
        return False