"""O2 — The setup banner (Phase III, 2026-11-24; built by O4 for O2 with the user's go-ahead). No GTK here.

`safety/setup.py` repairs per-user items silently and asks ONCE per extension version for the root step (one polkit
password dialog). When that did not happen — declined, failed, or no polkit agent to ask — the window shows one banner
saying what is missing and offering the one action that fixes it:

    extension missing, already asked   "…not installed in Brave and Chrome…"     [Install…]  -> one password prompt
    extension missing, no polkit agent "…no password prompt can be shown…"       [Check again]
    no signed extension package        "…package is missing from this installation"  (no button)

Nothing to say -> None (the banner stays hidden). A pending automatic prompt shows nothing, so the user is never asked
twice at once. Flatpak browsers are not listed (they cannot use the extension's host; see setup.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Action = Literal["install", "check"]


@dataclass(frozen=True)
class SetupNotice:
    text: str
    button: str | None = None  # None: no button
    action: Action | None = None


def _names(items: list[Any]) -> str:
    names = sorted({i.display for i in items})
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def setup_notice(report: Any, *, prompt_due: bool) -> SetupNotice | None:
    """`report`: a safety.setup.SetupReport. `prompt_due`: setup.root_needed(report) — the automatic prompt is still
    to come (then the banner waits for it)."""
    missing = [i for i in getattr(report, "items", []) if i.kind == "extension" and i.status in ("missing", "outdated")]
    unavailable = [i for i in getattr(report, "items", []) if i.kind == "extension" and i.status == "unavailable"]
    if missing:
        if prompt_due:
            return None
        where = _names(missing)
        if any(i.kind == "agent" and i.status == "missing" for i in report.items):
            return SetupNotice(f"The ScribeSense browser extension is not installed in {where}, and no password "
                               "prompt can be shown (no polkit agent is running). Start one, e.g. hyprpolkitagent, "
                               "then check again.", "Check again", "check")
        return SetupNotice(f"The ScribeSense browser extension is not installed in {where}. Without it, "
                           f"{'this browser is' if len(missing) == 1 else 'these browsers are'} styled only when "
                           "closed.", "Install…", "install")
    if unavailable:
        return SetupNotice("The ScribeSense browser extension package is missing from this installation, so "
                           f"{_names(unavailable)} cannot be styled while open.")
    return None
