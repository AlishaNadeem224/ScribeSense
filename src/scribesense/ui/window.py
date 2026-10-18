"""M2.4 — the application window.

Layout:
    Adw.Banner          start-up problem + recovery info (hidden unless the service failed to start)
    Adw.ToastOverlay    short messages (apply results, capture errors, settings outcomes)
      Adw.NavigationView
        PresetEditorPage (M2.5)   header: Reader · Last apply · Settings
        CoveragePage (M2.6)       pushed after a kept apply, or from "Last apply"
        SettingsPage (M2.7)

A1: closing the window HIDES it (`set_hide_on_close(True)`); the app holds itself, so the service keeps
running and a later launch presents this same window. This does not depend on the tray (M2.8), which
is unavailable in GTK 4 (see ui/tray.py).

The window owns no apply logic: Apply / Original / "read clipboard" are callables injected by the app
(ui/app.py), which runs them off the GTK thread. Store access goes through the app's Service
(`service.store` — K9 SettingsRepo / PresetRepo / CoverageRepo); without a service (start-up failure)
the window still opens and shows the recovery information, with the K9 defaults.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from scribesense.config.presets import BUILTINS  # noqa: E402
from scribesense.contracts.coverage import CoverageEntry  # noqa: E402
from scribesense.contracts.errors import ErrorCode, ScribeSenseError  # noqa: E402
from scribesense.contracts.presets import Preset  # noqa: E402
from scribesense.contracts.store import SETTINGS  # noqa: E402
from scribesense.fontgen import generator  # noqa: E402
from scribesense.log import log  # noqa: E402
from scribesense.ui import APP_ID  # noqa: E402
from scribesense.ui.coverage_report import build_coverage_page  # noqa: E402
from scribesense.ui.preset_editor import PresetEditorPage  # noqa: E402
from scribesense.ui.settings_page import (  # noqa: E402
    RESET_COMMAND,
    SettingsPage,
    delete_all_data,
    error_text,
    install_stored_keybinds,
)


def reset_keybind_text(service: Any | None) -> str:
    """The emergency-reset keybind as stored (K9 `keybind_reset`), or the K9 default."""
    if service is not None:
        try:
            return str(service.store.settings.get("keybind_reset"))
        except ScribeSenseError as exc:
            log(exc.code, "ui", setting="keybind_reset")
    return str(SETTINGS["keybind_reset"][1])


def recovery_text(service: Any | None) -> str:
    """M2.4 failure case: recovery info shown with every start-up error."""
    return (f"To restore your original desktop, press {reset_keybind_text(service)} "
            f"or run “{RESET_COMMAND}”.")


def _find_header(widget: Gtk.Widget | None) -> Adw.HeaderBar | None:
    """The first Adw.HeaderBar inside `widget` (depth first). Adw.ToolbarView has no getter for it."""
    while widget is not None:
        if isinstance(widget, Adw.HeaderBar):
            return widget
        found = _find_header(widget.get_first_child())
        if found is not None:
            return found
        widget = widget.get_next_sibling()
    return None


class ScribeSenseWindow(Adw.ApplicationWindow):
    """The single top-level window (M2.4)."""

    def __init__(
        self,
        *,
        service: Any | None = None,
        on_apply: Callable[[Preset], None] | None = None,
        on_original: Callable[[], None] | None = None,
        on_read_clipboard: Callable[[], None] | None = None,
        app_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        """All arguments are keyword-only and optional, so `ScribeSenseWindow(application=app)` works.

        service            the app's controller.service.Service, or None if it failed to start
        on_apply           start the apply flow for a preset (app: worker thread + confirm + report)
        on_original        start "Original" (revert everything)
        on_read_clipboard  capture the clipboard and open the reader
        app_id             kept for callers of the earlier signature; defaults to ui.APP_ID
        """
        super().__init__(**kwargs)
        self.app_id = app_id or APP_ID
        self._service = service
        self._on_apply_cb = on_apply
        self._on_original_cb = on_original
        self._on_read_clipboard_cb = on_read_clipboard
        self._last_coverage: tuple[CoverageEntry, ...] | None = None

        self.set_title("ScribeSense")
        self.set_default_size(720, 800)
        # A1: closing hides the window; only Quit stops the service. Never depends on the tray.
        self.set_hide_on_close(True)

        self.banner = Adw.Banner()
        self.banner.set_revealed(False)
        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.banner)
        self.toasts.set_vexpand(True)
        box.append(self.toasts)
        self.set_content(box)

        self.editor = PresetEditorPage(
            build_preview=generator.build_preview,
            on_apply=self._on_apply,
            on_original=self._on_original,
            list_presets=self._list_presets,
            save_preset=self._save_preset,
        )
        self.nav.push(self.editor)
        self._add_header_buttons(box)

    # ---------------------------------------------------------------- header

    def _add_header_buttons(self, box: Gtk.Box) -> None:
        buttons: list[tuple[str, str, str, Callable[[Gtk.Button], None]]] = [
            ("start", "accessories-text-editor-symbolic", "Open the clipboard text in the reader",
             self._on_reader_clicked),
            ("end", "emblem-system-symbolic", "Settings", self._on_settings_clicked),
            ("end", "view-list-bullet-symbolic", "Last apply: which apps were covered",
             self._on_last_apply_clicked),
        ]
        header = getattr(self.editor, "header", None) or _find_header(self.editor.get_child())
        bar: Any = header
        if bar is None:  # editor without a header bar: keep the actions reachable at the bottom
            bar = Gtk.ActionBar()
            box.append(bar)
        for side, icon, tooltip, handler in buttons:
            button = Gtk.Button(icon_name=icon)
            button.set_tooltip_text(tooltip)
            button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])  # screen-reader label
            button.connect("clicked", handler)
            (bar.pack_start if side == "start" else bar.pack_end)(button)

    # ---------------------------------------------------------------- messages

    def show_toast(self, text: str) -> None:
        toast = Adw.Toast.new(text)
        toast.set_timeout(6)
        self.toasts.add_toast(toast)

    def show_startup_error(self, text: str) -> None:
        """M2.4: a start-up error shows a message and the recovery info — never a blank window."""
        self.banner.set_title(f"{text} {recovery_text(self._service)}")
        self.banner.set_revealed(True)

    # ---------------------------------------------------------------- presets (M2.5 via M3.1)

    def _list_presets(self) -> list[Preset]:
        if self._service is None:
            return list(BUILTINS)
        try:
            return list(self._service.store.presets.list())
        except ScribeSenseError as exc:
            log(exc.code, "ui", step="presets")
            self.show_toast(f"Your saved presets could not be loaded. {error_text(exc)}")
            return list(BUILTINS)

    def _save_preset(self, preset: Preset) -> None:
        if self._service is None:
            raise ScribeSenseError(ErrorCode.SERVICE_UNREACHABLE)
        self._service.store.presets.save(preset)

    def _on_apply(self, preset: Preset) -> None:
        if self._on_apply_cb is None:
            self.show_toast("Applying is not available right now.")
            return
        self._on_apply_cb(preset)

    def _on_original(self, *_args: object) -> None:
        if self._on_original_cb is None:
            self.show_toast(f"Original is not available right now. Run “{RESET_COMMAND}” instead.")
            return
        self._on_original_cb()

    # ---------------------------------------------------------------- reader (M2.9 / M2.10)

    def _on_reader_clicked(self, _button: Gtk.Button) -> None:
        if self._on_read_clipboard_cb is None:
            self.show_toast("The reader is not available right now.")
            return
        self._on_read_clipboard_cb()

    # ---------------------------------------------------------------- coverage (M2.6)

    def show_coverage(self, entries: Sequence[CoverageEntry]) -> None:
        """Push the M2.6 report (it includes the M2.2 restart notice). Replaces an older report page."""
        self._last_coverage = tuple(entries)
        self.nav.pop_to_page(self.editor)
        self.nav.push(build_coverage_page(self._last_coverage))

    def _on_last_apply_clicked(self, _button: Gtk.Button) -> None:
        entries: Sequence[CoverageEntry] | None = self._last_coverage
        if entries is None and self._service is not None:
            try:
                entries = self._service.store.coverage.last_completed_apply()
            except ScribeSenseError as exc:
                log(exc.code, "ui", step="coverage")
                self.show_toast(f"The last report could not be loaded. {error_text(exc)}")
                return
        if not entries:
            self.show_toast("No change has been applied and kept yet.")
            return
        self.show_coverage(entries)

    # ---------------------------------------------------------------- settings (M2.7)

    def _on_settings_clicked(self, _button: Gtk.Button) -> None:
        service = self._service

        def get_setting(key: str) -> object:
            if service is None:
                return SETTINGS[key][1]
            return service.store.settings.get(key)

        def set_setting(key: str, value: object) -> None:
            if service is None:
                raise ScribeSenseError(ErrorCode.SERVICE_UNREACHABLE)
            service.store.settings.set(key, value)

        def reset_desktop() -> Any:
            if service is None:
                raise ScribeSenseError(ErrorCode.SERVICE_UNREACHABLE)
            return service.original()

        def delete_all() -> bool:
            if service is None:
                raise ScribeSenseError(ErrorCode.SERVICE_UNREACHABLE)
            return delete_all_data(service.store, service.journal)

        def has_obligations() -> bool:
            if service is None:
                raise ScribeSenseError(ErrorCode.SERVICE_UNREACHABLE)
            return service.journal.no_obligations() is None

        self.nav.pop_to_page(self.editor)
        self.nav.push(SettingsPage(
            get_setting=get_setting,
            set_setting=set_setting,
            apply_keybinds=install_stored_keybinds,
            reset_desktop=reset_desktop,
            delete_all=delete_all,
            notify=self.show_toast,
            has_obligations=has_obligations,
        ))
