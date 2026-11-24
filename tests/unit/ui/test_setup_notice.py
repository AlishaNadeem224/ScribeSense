"""ui/setup_notice.py (Phase III, 2026-11-24): what the setup banner says, and its one action. No GTK."""

from __future__ import annotations

from scribesense.safety.setup import Item, SetupReport
from scribesense.ui.setup_notice import setup_notice


def report(*items: Item) -> SetupReport:
    return SetupReport(items=list(items))


BRAVE_MISSING = Item("extension", "brave", "Brave", "missing", needs_root=True)
CHROME_MISSING = Item("extension", "chrome", "Google Chrome", "outdated", needs_root=True)
AGENT_MISSING = Item("agent", "", "password prompt (polkit agent)", "missing")


def test_nothing_missing_shows_nothing() -> None:
    assert setup_notice(report(Item("extension", "brave", "Brave", "ok", needs_root=True)), prompt_due=False) is None
    assert setup_notice(report(), prompt_due=False) is None


def test_a_declined_install_offers_the_install_button_naming_the_browsers() -> None:
    notice = setup_notice(report(BRAVE_MISSING, CHROME_MISSING), prompt_due=False)
    assert notice is not None and notice.action == "install" and notice.button == "Install…"
    assert "Brave and Google Chrome" in notice.text and "these browsers are" in notice.text


def test_while_the_automatic_prompt_is_due_nothing_is_shown() -> None:
    assert setup_notice(report(BRAVE_MISSING), prompt_due=True) is None  # never two prompts at once


def test_no_polkit_agent_says_so_and_offers_check_again() -> None:
    notice = setup_notice(report(BRAVE_MISSING, AGENT_MISSING), prompt_due=False)
    assert notice is not None and notice.action == "check" and "polkit agent" in notice.text


def test_no_extension_package_is_information_only() -> None:
    notice = setup_notice(report(Item("extension", "brave", "Brave", "unavailable", needs_root=True)),
                          prompt_due=False)
    assert notice is not None and notice.button is None and notice.action is None
