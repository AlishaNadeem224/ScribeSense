"""M2.7 — Settings screen.

  - "Help improve recommendations" opt-in — K9 setting `recommend_opt_in`, default OFF (card "must never").
  - Keybinds — K9 settings `keybind_reader`, `keybind_preset_next`, `keybind_reset`. Edits are validated
    with M4.14's parser and saved only when the user presses "Save keybinds" (never per keystroke).
    Writing them into Hyprland is a separate, explicit "Apply to Hyprland" button (M4.14
    `install_keybinds`); a live conflict is refused and the conflicting bind is shown (card failure case).
  - Recovery info — the emergency-reset keybind as stored (default "CTRL ALT SHIFT SUPER, BackSpace")
    and the `scribesense reset` command.
  - "Delete all ScribeSense data" — first offers a reset (R2), then asks for an explicit confirmation,
    then deletes through the store API (see `delete_all_data`). The dialog text says exactly that: when
    the reset left settings alone (skipped) or something is still left to restore, the final dialog says
    the recovery records are kept (`delete_partial_body`).

The page owns no storage: every effect goes through injected callables (wired to the service's Store
in ui/window.py), so the logic below is testable with fakes. The pure helpers (`validate_keybinds`,
`describe_conflict`, `install_stored_keybinds`, `delete_all_data`) need no display.

`run_async` (worker thread + result back on the GTK main loop) lives here because this is the lowest
UI module that needs it; ui/window.py and ui/app.py import it from here.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from scribesense.contracts.errors import ErrorCode, ScribeSenseError  # noqa: E402
from scribesense.contracts.store import SETTINGS  # noqa: E402
from scribesense.log import log  # noqa: E402
from scribesense.safety import keybinds as kb  # noqa: E402

T = TypeVar("T")

#: settings key -> label shown in the keybind group, in display order.
KEYBIND_ROWS: tuple[tuple[str, str], ...] = (
    ("keybind_reader", "Open selection in reader"),
    ("keybind_preset_next", "Switch to next preset"),
    ("keybind_reset", "Emergency reset"),
)
#: settings key -> M4.14 action name.
_ACTION_OF: dict[str, str] = {key: action for action, key in kb.SETTING_KEYS.items()}
_LABEL_OF: dict[str, str] = dict(KEYBIND_ROWS)

RESET_COMMAND = "scribesense reset"


# ---------------------------------------------------------------- background work

def run_async(work: Callable[[], T], done: Callable[[T | BaseException], None]) -> None:
    """Run `work()` in a worker thread; call `done(result or exception)` on the GTK main loop.

    Keeps slow work (applies, resets, hyprctl calls) off the GTK thread so the window never freezes.
    """

    def runner() -> None:
        try:
            outcome: T | BaseException = work()
        except BaseException as exc:  # noqa: BLE001 — handed to `done`, which reports it
            outcome = exc

        def deliver() -> bool:
            done(outcome)
            return False

        GLib.idle_add(deliver)

    threading.Thread(target=runner, name="scribesense-worker", daemon=True).start()


def error_text(exc: BaseException) -> str:
    """User-facing text for a failure: the error code only, never raw exception text (privacy)."""
    if isinstance(exc, ScribeSenseError):
        return f"Something went wrong ({exc.code.value})."
    return "Something unexpected went wrong."


# ---------------------------------------------------------------- pure logic

def validate_keybinds(entries: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Validate the three keybind texts together.

    Returns (normalized, errors): `normalized` maps every settings key to its canonical text
    ("SUPER ALT, R") when everything is valid; `errors` maps a settings key to a message otherwise.
    Refused: unparsable text (M4.14 `parse_keybind`), an empty value (the reset bind must always exist),
    and two ScribeSense actions sharing one key combination.
    """
    normalized: dict[str, str] = {}
    errors: dict[str, str] = {}
    parsed: dict[str, kb.Keybind] = {}
    for key, _label in KEYBIND_ROWS:
        text = (entries.get(key) or "").strip()
        if not text:
            errors[key] = "A keybind is required (for example: SUPER ALT, R)."
            continue
        try:
            bind = kb.parse_keybind(text, _ACTION_OF[key])
        except ValueError:
            errors[key] = "Not a valid keybind. Use modifiers, a comma, then a key: SUPER ALT, R"
            continue
        parsed[key] = bind
        normalized[key] = kb.format_keybind(bind)
    seen: dict[tuple[int, str], str] = {}
    for key, bind in parsed.items():
        other = seen.get(bind.combo())
        if other is not None:
            errors[key] = f"Same keys as “{_LABEL_OF[other]}”."
        else:
            seen[bind.combo()] = key
    return ({} if errors else normalized), errors


def describe_conflict(conflict: kb.Conflict) -> str:
    """Card failure case: show the conflicting bind (shown on screen only, never logged)."""
    theirs = conflict.theirs
    combo = f"{' '.join(theirs.mods)}, {theirs.key}"
    what = theirs.description or theirs.dispatcher or "another action"
    ours = _LABEL_OF.get(kb.SETTING_KEYS.get(conflict.ours.action, ""), conflict.ours.action)
    return f"“{ours}” uses {combo}, which Hyprland already uses for: {what}."


def stored_binds(get_setting: Callable[[str], object]) -> list[kb.Keybind]:
    """The stored keybinds as M4.14 Keybind objects (ValueError if a stored value is invalid)."""
    return [kb.parse_keybind(str(get_setting(kb.SETTING_KEYS[a])), a) for a in kb.ACTIONS]


def install_stored_keybinds(
    get_setting: Callable[[str], object],
    *,
    install: Callable[..., Any] = kb.install_keybinds,
    find_conflicts: Callable[[Sequence[kb.Keybind]], list[kb.Conflict]] = kb.conflicts,
    command: str | None = None,
) -> str:
    """The "Apply to Hyprland" action: write the STORED binds through M4.14. Returns the user message.

    Conflicts are checked first so the user sees which bind collides (install_keybinds itself only
    raises KEYBIND_CONFLICT). Every outcome is a message; nothing is raised to the UI.
    """
    exe = command if command is not None else shutil.which("scribesense")
    if not exe:
        return "The scribesense command was not found on your PATH, so no keybinds were written."
    try:
        binds = stored_binds(get_setting)
    except ValueError:
        return "A saved keybind is not valid. Fix it and press Save keybinds first."
    try:
        found = find_conflicts(binds)
        if found:
            return "Not applied. " + " ".join(describe_conflict(c) for c in found)
        status = install(binds, exe)
    except ScribeSenseError as exc:
        log(exc.code, "settings", step="keybinds")
        return _INSTALL_MESSAGES.get(exc.code, f"Keybinds were not applied ({exc.code.value}).")
    except ValueError:
        return "Keybinds were not applied: the keybinds or the command path are not valid."
    if getattr(status, "reload", None) == "skipped":
        return "Keybinds saved to your Hyprland config. They take effect at your next login."
    return "Keybinds applied to Hyprland."


_INSTALL_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.KEYBIND_CONFLICT: "Not applied: a keybind is already used in Hyprland.",
    ErrorCode.CHECK_UNAVAILABLE: (
        "Not applied: ScribeSense could not check Hyprland for conflicting keybinds (is Hyprland "
        "running?). (CHECK_UNAVAILABLE)"
    ),
    ErrorCode.ADAPTER_UNAVAILABLE: "Not applied: no Hyprland configuration was found. (ADAPTER_UNAVAILABLE)",
    ErrorCode.DRIFT_SKIPPED: (
        "Not applied: the ScribeSense block in your Hyprland config was edited by hand, so it was left "
        "alone. (DRIFT_SKIPPED)"
    ),
}


def delete_all_data(store: Any, journal: Any) -> bool:
    """Delete through the K9 store API. Returns True if the recovery records were deleted too.

    Preferences (presets, settings, samples) are always deleted. Recovery records are deleted only when
    the journal confirms nothing is left to restore (`journal.no_obligations()` gives the token);
    otherwise they are kept, so `scribesense reset` can still restore the original desktop.
    """
    store.delete_preferences()
    token = journal.no_obligations()
    if token is None:
        return False
    store.delete_recovery_data(precondition=token)
    return True


def delete_result_text(recovery_deleted: bool) -> str:
    if recovery_deleted:
        return "All ScribeSense data was deleted."
    return (
        "Your presets and settings were deleted. The records needed to restore your original desktop "
        f"were kept, because not every ScribeSense change is undone. Run “{RESET_COMMAND}” to "
        "restore it."
    )


#: Dialog texts. They describe exactly what `delete_all_data` does (review finding: text must match).
DELETE_FIRST_HEADING = "Delete all ScribeSense data?"
DELETE_FIRST_BODY = (
    "First, ScribeSense can reset your desktop to its original settings. "
    "Then it deletes your saved presets and settings, and the records of past changes."
)
DELETE_SKIP_HEADING = "Delete without resetting?"
DELETE_SKIP_BODY = (
    "Your desktop keeps its current ScribeSense styling. Your presets and settings will be deleted. "
    "If ScribeSense changes are still active, the records needed to restore your original desktop "
    f"are kept so “{RESET_COMMAND}” can still undo them. If nothing is left to restore, those "
    "records are deleted now as well."
)
DELETE_FINAL_HEADING = "Delete everything?"
DELETE_FINAL_BODY = (
    "Your desktop is back to its original settings. This permanently deletes your saved presets, "
    "settings and the records of past changes."
)
DELETE_PARTIAL_HEADING = "Delete presets and settings?"
#: Most left-alone settings named in the partial dialog; the rest are counted.
_MAX_NAMED = 5


def delete_partial_body(skipped: Sequence[str]) -> str:
    """The final dialog when the reset left settings alone (changed by another program, DRIFT_SKIPPED).

    Those locations stay recorded as unresolved, so `journal.no_obligations()` is None and
    `delete_all_data` keeps the recovery records: the text says exactly that.
    """
    names = ", ".join(skipped[:_MAX_NAMED])
    if len(skipped) > _MAX_NAMED:
        names += f" and {len(skipped) - _MAX_NAMED} more"
    left = (f"{len(skipped)} setting(s) were changed by another program and were left alone: {names}. "
            if skipped else "Some ScribeSense changes could not be confirmed as undone. ")
    return (
        left + "Your desktop is not fully back to its original settings. This permanently deletes your "
        "saved presets and settings. The records needed to restore your original desktop are kept, so "
        f"“{RESET_COMMAND}” can still restore them."
    )


# ---------------------------------------------------------------- the page

class SettingsPage(Adw.NavigationPage):
    """The application settings screen (M2.7)."""

    def __init__(
        self,
        *,
        get_setting: Callable[[str], object],
        set_setting: Callable[[str, object], None],
        apply_keybinds: Callable[[Callable[[str], object]], str],
        reset_desktop: Callable[[], Any],
        delete_all: Callable[[], bool],
        notify: Callable[[str], None],
        has_obligations: Callable[[], bool] | None = None,
    ) -> None:
        """
        get_setting / set_setting  K9 SettingsRepo get / set (fall back to SETTINGS defaults on errors).
        apply_keybinds             `install_stored_keybinds`-shaped: given a getter for the SAVED binds, writes
                                   them to Hyprland (runs in a worker thread); returns the message to show.
        reset_desktop              the "Original" reset (service.original()); run off the GTK thread.
        delete_all                 `delete_all_data(store, journal)`; True if recovery records went too.
        notify                     shows a short message (the window's toast).
        has_obligations            True while something is left to restore (`journal.no_obligations()` is None),
                                   i.e. `delete_all` would keep the recovery records; checked after the reset.
        """
        super().__init__(title="Settings")
        self._get = get_setting
        self._set = set_setting
        self._apply_keybinds = apply_keybinds
        self._reset_desktop = reset_desktop
        self._delete_all = delete_all
        self._notify = notify
        self._has_obligations = has_obligations
        self._busy = False

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        page_box = Adw.PreferencesPage()

        # 1. Recommendations (opt-in, default off).
        rec_group = Adw.PreferencesGroup(
            title="Recommendations",
            description="Share your preset adjustments to help improve recommendations. Off by default. "
                        "Text you read is never recorded.",
        )
        self.opt_in_row = Adw.SwitchRow(title="Help improve recommendations")
        self.opt_in_row.set_active(bool(self._read("recommend_opt_in")))
        self.opt_in_row.connect("notify::active", self._on_opt_in_toggled)
        rec_group.add(self.opt_in_row)
        page_box.add(rec_group)

        # 2. Keybinds: edit, then Save (validated); Apply to Hyprland is a separate explicit action.
        kb_group = Adw.PreferencesGroup(
            title="Keybinds",
            description="Format: modifiers, a comma, then a key — for example SUPER ALT, R.",
        )
        self.kb_rows: dict[str, Adw.EntryRow] = {}
        for key, label in KEYBIND_ROWS:
            row = Adw.EntryRow(title=label)
            row.set_text(str(self._read(key)))
            row.connect("changed", self._on_keybind_edited)
            self.kb_rows[key] = row
            kb_group.add(row)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, halign=Gtk.Align.END)
        buttons.set_margin_top(12)
        self.save_kb_btn = Gtk.Button(label="Save keybinds")
        self.save_kb_btn.connect("clicked", self._on_save_keybinds)
        self.apply_kb_btn = Gtk.Button(label="Apply to Hyprland")
        self.apply_kb_btn.set_tooltip_text("Write the saved keybinds into your Hyprland configuration")
        self.apply_kb_btn.connect("clicked", self._on_apply_keybinds)
        buttons.append(self.save_kb_btn)
        buttons.append(self.apply_kb_btn)
        kb_group.add(buttons)
        page_box.add(kb_group)

        # 3. Recovery & data.
        data_group = Adw.PreferencesGroup(
            title="Data & Recovery",
            description=f"If your desktop becomes hard to read, press the emergency reset keybind or run "
                        f"“{RESET_COMMAND}” in a terminal.",
        )
        self.reset_row = Adw.ActionRow(title="Emergency reset keybind")
        self.reset_label = Gtk.Label(label=str(self._read("keybind_reset")))
        self.reset_label.add_css_class("dim-label")
        self.reset_label.set_selectable(True)
        self.reset_row.add_suffix(self.reset_label)
        data_group.add(self.reset_row)

        self.delete_row = Adw.ActionRow(title="Delete all ScribeSense data")
        self.delete_row.set_subtitle("Offers a reset first, then asks before deleting anything.")
        self.delete_btn = Gtk.Button(label="Delete…")
        self.delete_btn.add_css_class("destructive-action")
        self.delete_btn.set_valign(Gtk.Align.CENTER)
        self.delete_btn.connect("clicked", self._on_delete_clicked)
        self.delete_row.add_suffix(self.delete_btn)
        data_group.add(self.delete_row)
        page_box.add(data_group)

        toolbar.set_content(page_box)
        self.set_child(toolbar)

    # ---- settings access

    def _read(self, key: str) -> object:
        """A stored setting; the K9 default if the store cannot be read (shown, never a wrong value)."""
        try:
            return self._get(key)
        except ScribeSenseError as exc:
            log(exc.code, "settings", setting=key)
            return SETTINGS[key][1]

    # ---- opt-in

    def _on_opt_in_toggled(self, row: Adw.SwitchRow, _pspec: Any) -> None:
        value = bool(row.get_active())
        try:
            self._set("recommend_opt_in", value)
        except ScribeSenseError as exc:
            log(exc.code, "settings", setting="recommend_opt_in")
            self._notify(error_text(exc))
            row.handler_block_by_func(self._on_opt_in_toggled)
            row.set_active(not value)
            row.handler_unblock_by_func(self._on_opt_in_toggled)

    # ---- keybinds

    def _on_keybind_edited(self, row: Adw.EntryRow) -> None:
        row.remove_css_class("error")  # validation happens on Save, not per keystroke

    def _on_save_keybinds(self, _button: Gtk.Button) -> None:
        normalized, errors = validate_keybinds({k: r.get_text() for k, r in self.kb_rows.items()})
        for key, row in self.kb_rows.items():
            if key in errors:
                row.add_css_class("error")
            else:
                row.remove_css_class("error")
        if errors:
            first = next(k for k, _l in KEYBIND_ROWS if k in errors)
            self._notify(f"{_LABEL_OF[first]}: {errors[first]}")
            return
        try:
            for key, text in normalized.items():
                if str(self._read(key)) != text:
                    self._set(key, text)
        except ScribeSenseError as exc:
            log(exc.code, "settings", step="keybinds")
            self._notify(error_text(exc))
            return
        for key, text in normalized.items():
            self.kb_rows[key].set_text(text)
        self.reset_label.set_label(normalized["keybind_reset"])
        self._notify("Keybinds saved. Press “Apply to Hyprland” to use them.")

    def _on_apply_keybinds(self, button: Gtk.Button) -> None:
        button.set_sensitive(False)
        # Read the SAVED values here, on the GTK thread; only the Hyprland work runs in the worker.
        saved = {kb.SETTING_KEYS[a]: self._read(kb.SETTING_KEYS[a]) for a in kb.ACTIONS}

        def done(outcome: object) -> None:
            button.set_sensitive(True)
            self._notify(outcome if isinstance(outcome, str) else error_text(outcome))  # type: ignore[arg-type]

        run_async(lambda: self._apply_keybinds(saved.__getitem__), done)

    # ---- delete all (R2: offer reset first, then confirm)

    def _dialog(self, heading: str, body: str, responses: Sequence[tuple[str, str, Any]],
                on_response: Callable[[str], None]) -> Adw.AlertDialog:
        dialog = Adw.AlertDialog.new(heading, body)
        for rid, label, appearance in responses:
            dialog.add_response(rid, label)
            if appearance is not None:
                dialog.set_response_appearance(rid, appearance)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, rid: on_response(rid))
        dialog.present(self.get_root())
        return dialog

    def _on_delete_clicked(self, _button: Gtk.Button) -> None:
        if self._busy:
            return
        self._dialog(
            DELETE_FIRST_HEADING, DELETE_FIRST_BODY,
            (("cancel", "Cancel", None),
             ("skip", "Delete Without Resetting", Adw.ResponseAppearance.DESTRUCTIVE),
             ("reset", "Reset First", Adw.ResponseAppearance.SUGGESTED)),
            self.on_first_response,
        )

    def on_first_response(self, response: str) -> None:
        """Step 1: reset first (R2), skip with a warning, or cancel (nothing is deleted)."""
        if response == "reset":
            self._start_reset()
        elif response == "skip":
            self._dialog(
                DELETE_SKIP_HEADING, DELETE_SKIP_BODY,
                (("cancel", "Cancel", None), ("delete", "Delete", Adw.ResponseAppearance.DESTRUCTIVE)),
                self.on_final_response,
            )

    def _start_reset(self) -> None:
        self._busy = True
        self.delete_btn.set_sensitive(False)
        self._notify("Resetting your desktop…")
        run_async(self._reset_desktop, self.on_reset_done)

    def on_reset_done(self, outcome: object) -> None:
        """After the reset: a failure stops; a partial reset gets a dialog that says the records are kept."""
        self._busy = False
        self.delete_btn.set_sensitive(True)
        if isinstance(outcome, BaseException):
            self._notify(f"The reset did not finish, so nothing was deleted. {error_text(outcome)}")
            return
        failed = getattr(outcome, "failed", None)
        if failed:
            self._notify(f"{len(failed)} setting(s) could not be restored, so nothing was deleted. "
                         f"Try “{RESET_COMMAND}” again later.")
            return
        skipped = list(getattr(outcome, "skipped", None) or ())
        if skipped or self._obligations_left():
            self._dialog(
                DELETE_PARTIAL_HEADING, delete_partial_body(skipped),
                (("cancel", "Cancel", None),
                 ("delete", "Delete Presets and Settings", Adw.ResponseAppearance.DESTRUCTIVE)),
                self.on_final_response,
            )
            return
        self._dialog(
            DELETE_FINAL_HEADING, DELETE_FINAL_BODY,
            (("cancel", "Cancel", None), ("delete", "Delete Everything", Adw.ResponseAppearance.DESTRUCTIVE)),
            self.on_final_response,
        )

    def _obligations_left(self) -> bool:
        """True if the recovery records would be kept; an unreadable store counts as "kept" (safe text)."""
        if self._has_obligations is None:
            return False
        try:
            return bool(self._has_obligations())
        except ScribeSenseError as exc:
            log(exc.code, "settings", step="obligations")
            return True

    def on_final_response(self, response: str) -> None:
        """Step 2: only an explicit Delete deletes."""
        if response != "delete":
            return
        try:
            recovery_deleted = self._delete_all()
        except ScribeSenseError as exc:
            log(exc.code, "settings", step="delete")
            self._notify(f"The deletion did not finish. {error_text(exc)}")
            return
        self._notify(delete_result_text(bool(recovery_deleted)))
        for key, row in self.kb_rows.items():  # settings are back to their defaults
            row.set_text(str(self._read(key)))
        self.reset_label.set_label(str(self._read("keybind_reset")))
        self.opt_in_row.handler_block_by_func(self._on_opt_in_toggled)
        self.opt_in_row.set_active(bool(self._read("recommend_opt_in")))
        self.opt_in_row.handler_unblock_by_func(self._on_opt_in_toggled)
