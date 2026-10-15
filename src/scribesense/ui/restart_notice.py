"""M2.2 — Restart notice.

Tells the user which of the running applications must be restarted for the
applied change to take effect. This module is a filter and a label; it does
NOT restart anything, close anything, or open a subprocess. That is the
M2.2 card's "Must never" rule, enforced by the absence of any such code.

ScribeSense never closes or restarts user applications. The user is asked,
and the application is left to them. This module is the "asking" part.
"""

from __future__ import annotations

from collections.abc import Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk  # noqa: E402  (after require_version)

from scribesense.contracts.adapter import Target
from scribesense.contracts.coverage import CoverageEntry, CoverageStatus


def restart_list(entries: Sequence[CoverageEntry]) -> list[Target]:
    """Return the running targets that need a restart.

    A target qualifies if it is marked RESTART_NEEDED and is running. A
    RESTART_NEEDED target that is not running is dropped, because restarting
    is not required — the next launch will pick up the change.

    K3's `Target.running` is currently a plain bool, so there is no third
    "unknown" case. If O4 later changes it to Optional[bool] (the M2.2 card's
    "unknown running state" case), extend this function to include targets
    whose `running` is None with the "may need restart" wording.
    """
    seen: set[str] = set()
    result: list[Target] = []
    for entry in entries:
        if entry.status is not CoverageStatus.RESTART_NEEDED:
            continue
        if not entry.target.running:
            continue
        if entry.target.target_id in seen:
            continue
        seen.add(entry.target.target_id)
        result.append(entry.target)
    return result


def _names(targets: Sequence[Target]) -> str:
    """Comma-joined display names, Oxford-style for three or more."""
    names = [t.display_name for t in targets]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def restart_message(targets: Sequence[Target]) -> str:
    """One-line message for the banner. Empty string if no targets."""
    if not targets:
        return ""
    noun = "application" if len(targets) == 1 else "applications"
    return f"Restart {noun} to see the change: {_names(targets)}"


def build_restart_banner(targets: Sequence[Target]) -> Gtk.Widget | None:
    """A warning-style banner listing the applications to restart, or None.

    Returning None when there is nothing to say keeps the coverage report
    clean: the banner only appears when it has something to say.
    """
    message = restart_message(targets)
    if not message:
        return None

    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    box.add_css_class("card")
    box.set_margin_start(18)
    box.set_margin_end(18)
    box.set_margin_bottom(12)
    box.set_margin_top(0)

    icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
    icon.set_valign(Gtk.Align.START)
    icon.set_margin_top(12)
    icon.set_margin_bottom(12)
    icon.set_margin_start(14)

    label = Gtk.Label(label=message)
    label.set_wrap(True)
    label.set_xalign(0.0)
    label.set_halign(Gtk.Align.START)
    label.set_hexpand(True)
    label.set_margin_top(12)
    label.set_margin_bottom(12)
    label.set_margin_end(14)
    # Screen readers: the banner is a single announcement, not a stream of
    # words. Explicit accessible label keeps that intact.
    label.update_property([Gtk.AccessibleProperty.LABEL], [message])

    box.append(icon)
    box.append(label)
    return box