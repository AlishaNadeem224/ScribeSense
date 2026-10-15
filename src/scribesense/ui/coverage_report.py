"""M2.6 — Coverage report page (with the M2.2 restart notice).

Renders a sequence of CoverageEntry as a libadwaita page: one row per target,
with a status label and a short human-readable detail. When a running target
needs a restart, a warning banner is shown above the list (M2.2).

The page is built by a function that takes the entries as an argument — no
store access, no network, no apply. This makes it trivial to test and easy to
reuse for both the live report (after apply) and the historical report (from
M3.1's store, once it exists).

The card's "must never" rule is enforced here: the word "Covered" appears only
for entries whose status is exactly CoverageStatus.COVERED. There is no
fallback that assumes success.
"""

from __future__ import annotations

from collections.abc import Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gtk, Pango  # noqa: E402  (after require_version)

from scribesense.contracts.coverage import CoverageEntry, CoverageStatus
from scribesense.contracts.errors import ErrorCode
from scribesense.ui.restart_notice import build_restart_banner, restart_list


#: Status -> (short label, CSS class for colour). Kept as a module constant so
#: the mapping is exhaustive and auditable; if K6 adds a status, the tests in
#: tests/unit/ui/test_coverage_report.py surface it.
_STATUS_LABELS: dict[CoverageStatus, tuple[str, str]] = {
    CoverageStatus.COVERED: ("Covered", "success"),
    CoverageStatus.NOT_COVERED: ("Not covered", "error"),
    CoverageStatus.PENDING: ("Pending — app is open", "warning"),
    CoverageStatus.RESTART_NEEDED: ("Restart needed", "warning"),
}

#: Friendly phrases for the codes a user is most likely to see. Anything not in
#: this map falls back to a generic phrase, and the raw code is appended.
_DETAIL_PHRASES: dict[str, str] = {
    "OK": "",  # covered — no detail to show
    ErrorCode.BROWSER_RUNNING.value: (
        "The browser must be closed before the change can be applied."
    ),
    ErrorCode.ADAPTER_TIMEOUT.value: "The application did not respond in time.",
    ErrorCode.ADAPTER_COMMAND_FAILED.value: "A required system command failed.",
    ErrorCode.ADAPTER_VERIFY_FAILED.value: (
        "The change could not be verified in this application."
    ),
    ErrorCode.ADAPTER_UNAVAILABLE.value: (
        "The application is not available on this system."
    ),
    ErrorCode.UNEXPECTED_ADAPTER_ERROR.value: "An unexpected problem occurred.",
    ErrorCode.CHECK_UNAVAILABLE.value: "The verification step is not available here.",
    ErrorCode.SCHEMA_MISSING.value: (
        "This application's settings schema is not installed."
    ),
    ErrorCode.QT6CT_NOT_IN_SESSION.value: (
        "Qt applications are not configured to use qt6ct."
    ),
    ErrorCode.PREFERENCES_UNREADABLE.value: (
        "The browser's settings file could not be read."
    ),
    ErrorCode.PROFILE_NOT_FOUND.value: "No profile was found for this browser.",
}


def _label_for(status: CoverageStatus) -> tuple[str, str]:
    """(label, css_class) for a status. Unknown status -> a defensive fallback."""
    return _STATUS_LABELS.get(status, ("Unknown status", "warning"))


def _detail_for(code: ErrorCode | str) -> str:
    """Human-readable detail for a coverage entry.

    Known codes -> friendly phrase. Unknown -> generic phrase plus the raw code
    in parentheses, per the card's failure-case rule.
    """
    raw = code.value if isinstance(code, ErrorCode) else str(code)
    phrase = _DETAIL_PHRASES.get(raw)
    if phrase is not None:
        return phrase
    return f"This application could not be covered. ({raw})"


def _summarise(entries: Sequence[CoverageEntry]) -> str:
    """One-line summary: how many of how many are covered."""
    total = len(entries)
    if total == 0:
        return "No applications were checked."
    covered = sum(1 for e in entries if e.status is CoverageStatus.COVERED)
    if covered == total:
        return f"All {total} applications covered."
    return f"{covered} of {total} applications covered."


def build_coverage_page(entries: Sequence[CoverageEntry]) -> Adw.NavigationPage:
    """Build the coverage report navigation page.

    Layout, top to bottom:
      1. Adw.HeaderBar       — title + back button (from the navigation view)
      2. Summary label       — "N of M applications covered."
      3. Restart banner      — M2.2, only if some running app needs a restart
      4. List of rows        — one Gtk.ListBoxRow per CoverageEntry
      All of it inside a ScrolledWindow so large lists stay usable.
    """
    # --- 1. Header bar -------------------------------------------------------
    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    toolbar.add_top_bar(header)

    # The vertical container that holds summary + banner + list.
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

    # --- 2. Summary ----------------------------------------------------------
    summary = Gtk.Label(label=_summarise(entries))
    summary.add_css_class("title-3")
    summary.set_margin_top(18)
    summary.set_margin_bottom(12)
    summary.set_margin_start(18)
    summary.set_margin_end(18)
    summary.set_halign(Gtk.Align.START)
    summary.set_xalign(0.0)
    summary.update_property([Gtk.AccessibleProperty.LABEL], [_summarise(entries)])
    outer.append(summary)

    # --- 3. M2.2 restart banner ---------------------------------------------
    # build_restart_banner returns None when there is nothing to restart, in
    # which case we simply do not append anything.
    banner = build_restart_banner(restart_list(entries))
    if banner is not None:
        outer.append(banner)

    # --- 4. One row per CoverageEntry ---------------------------------------
    if entries:
        list_box = Gtk.ListBox()
        list_box.add_css_class("boxed-list")
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        list_box.set_margin_start(18)
        list_box.set_margin_end(18)
        list_box.set_margin_bottom(18)
        for entry in entries:
            list_box.append(_row_for(entry))
        outer.append(list_box)

    # Wrap the whole content in a scroll container so tall lists and large
    # text sizes do not clip anything.
    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroller.set_vexpand(True)
    scroller.set_child(outer)

    toolbar.set_content(scroller)
    return Adw.NavigationPage(child=toolbar, title="Coverage")


def _row_for(entry: CoverageEntry) -> Gtk.ListBoxRow:
    """One row: display name on the left, status pill on the right, detail below."""
    row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    row_box.set_margin_top(12)
    row_box.set_margin_bottom(12)
    row_box.set_margin_start(14)
    row_box.set_margin_end(14)

    top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

    name = Gtk.Label(label=entry.target.display_name)
    name.set_halign(Gtk.Align.START)
    name.set_hexpand(True)
    name.set_xalign(0.0)
    name.set_ellipsize(Pango.EllipsizeMode.END)
    label_text, css_class = _label_for(entry.status)
    # The accessible label combines both pieces of information so a screen
    # reader hears "Covered — Brave" or "Not covered — Qt (menu)" as one unit.
    name.update_property(
        [Gtk.AccessibleProperty.LABEL],
        [f"{label_text} — {entry.target.display_name}"],
    )

    status = Gtk.Label(label=label_text)
    status.add_css_class("pill")
    status.add_css_class(css_class)
    status.set_halign(Gtk.Align.END)
    status.set_valign(Gtk.Align.START)

    top.append(name)
    top.append(status)
    row_box.append(top)

    detail_text = _detail_for(entry.detail_code)
    if detail_text:
        detail = Gtk.Label(label=detail_text)
        detail.add_css_class("dim-label")
        detail.set_halign(Gtk.Align.START)
        detail.set_xalign(0.0)
        detail.set_wrap(True)
        row_box.append(detail)

    return Gtk.ListBoxRow(child=row_box)