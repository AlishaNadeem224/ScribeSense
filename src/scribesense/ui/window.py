"""M2.4 — the application window.

A single Adw.ApplicationWindow containing an Adw.NavigationView. Later cards
push real pages onto the navigation view (M2.5 presets, M2.6 coverage, M2.7
settings, M2.9 reader). The placeholder page now links to the coverage
report (M2.6) with sample data, so the report is reachable during
development.

The window is never required by the recovery path (A2) and holds no state the
service needs. Closing the window does not quit the application (A1).
"""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402  (after require_version)

from scribesense.contracts.adapter import Target  # noqa: E402
from scribesense.contracts.coverage import CoverageEntry, CoverageStatus  # noqa: E402
from scribesense.contracts.errors import ErrorCode  # noqa: E402
from scribesense.ui.coverage_report import build_coverage_page  # noqa: E402


def _sample_coverage() -> list[CoverageEntry]:
    """Sample coverage entries so the M2.6 page is reachable before M2.1 is
    wired to the confirm flow. Removed when the real report source lands.
    """
    return [
        CoverageEntry(
            target=Target("fontconfig", "system", "System fonts", False),
            status=CoverageStatus.COVERED,
            detail_code="OK",
        ),
        CoverageEntry(
            target=Target("gtk", "gtk", "GTK applications", False),
            status=CoverageStatus.COVERED,
            detail_code="OK",
        ),
        CoverageEntry(
            target=Target("chromium", "brave:Default", "Brave", True),
            status=CoverageStatus.PENDING,
            detail_code=ErrorCode.BROWSER_RUNNING,
        ),
        CoverageEntry(
            target=Target("qt", "qt6ct", "Qt (via qt6ct)", False),
            status=CoverageStatus.NOT_COVERED,
            detail_code=ErrorCode.QT6CT_NOT_IN_SESSION,
        ),
        CoverageEntry(
            target=Target("firefox", "zen:default", "Zen", True),
            status=CoverageStatus.RESTART_NEEDED,
            detail_code="RESTART_NEEDED",
        ),
    ]


class ScribeSenseWindow(Adw.ApplicationWindow):
    """The single top-level window for the application."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

        self.set_title("ScribeSense")
        self.set_default_size(720, 560)

        self.nav = Adw.NavigationView()
        self.set_content(self.nav)
        self.nav.push(self._build_placeholder_page())

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
        title.update_property([Gtk.AccessibleProperty.LABEL], ["ScribeSense"])

        body = Gtk.Label(
            label=(
                "The application shell is in place. Presets, preview, apply, "
                "and the full coverage report arrive in later steps."
            )
        )
        body.set_wrap(True)
        body.set_justify(Gtk.Justification.CENTER)
        body.set_max_width_chars(60)

        coverage_button = Gtk.Button(label="View coverage report")
        coverage_button.add_css_class("suggested-action")
        coverage_button.connect("clicked", self._on_coverage_clicked)

        box.append(title)
        box.append(body)
        box.append(coverage_button)

        toolbar.set_content(box)
        return Adw.NavigationPage(child=toolbar, title="ScribeSense")

    def _on_coverage_clicked(self, _button: Gtk.Button) -> None:
        # M2.6: for now, this pushes a sample report. The real source is
        # ApplyReport.coverage once M2.1 is wired to the confirm flow, or
        # store.last_completed_apply() once M3.1 lands.
        self.nav.push(build_coverage_page(_sample_coverage()))

    def _on_close_request(self, *_args: object) -> bool:
        return False