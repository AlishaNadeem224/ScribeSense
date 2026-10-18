"""O4 — The production adapter list (K3): what the service, `reset`, `recover --login` and `uninstall` use.

fontconfig comes first (K3 §7: it is applied first and its failure stops the apply). Each adapter is built with its
production defaults (real `run_cmd`, real install roots). Building one never touches the desktop: adapters only act
when the controller or recovery calls them.
"""

from __future__ import annotations

from scribesense.contracts.adapter import Adapter


def default_adapters() -> list[Adapter]:
    from scribesense.adapters.chromium import ChromiumAdapter
    from scribesense.adapters.firefox import FirefoxAdapter
    from scribesense.adapters.flatpak import FlatpakAdapter
    from scribesense.adapters.fontconfig import FontconfigAdapter
    from scribesense.adapters.gtk import GtkAdapter
    from scribesense.adapters.qt import QtAdapter

    return [FontconfigAdapter(), GtkAdapter(), QtAdapter(), FlatpakAdapter(), FirefoxAdapter(), ChromiumAdapter()]


def by_name(adapters: list[Adapter] | None = None) -> dict[str, Adapter]:
    """`{adapter.name: adapter}` — the mapping recovery (M4.11/M4.12) and uninstall (M3.8) take."""
    return {a.name: a for a in (default_adapters() if adapters is None else adapters)}
