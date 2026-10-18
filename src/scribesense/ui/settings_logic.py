"""M2.7 — the Settings screen's logic, without GTK (O3's M2.7 note 4: testable where GTK is not installed).

Everything here is pure or works through injected callables; ui/settings_page.py builds the widgets on top and
re-exports these names, so `from scribesense.ui.settings_page import validate_keybinds` keeps working.

  validate_keybinds / describe_conflict / stored_binds / install_stored_keybinds   keybinds (M4.14 parser + writer)
  error_text                                                                       code-only failure text (privacy)
  delete_result_text / delete_partial_body / DELETE_* texts                        what the delete dialogs say
  delete_all_data                                                                  the store steps of delete-all

This module never imports `gi` (checked by tests/unit/ui/test_settings_logic.py).
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.controller.service import delete_store_data
from scribesense.log import log
from scribesense.safety import keybinds as kb

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


def error_text(exc: BaseException) -> str:
    """User-facing text for a failure: the error code only, never raw exception text (privacy)."""
    if isinstance(exc, ScribeSenseError):
        return f"Something went wrong ({exc.code.value})."
    return "Something unexpected went wrong."


# ---------------------------------------------------------------- keybinds

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


# ---------------------------------------------------------------- delete all data

#: The store steps of delete-all (controller/service.py `delete_store_data`), kept under this name for existing
#: callers. It does NOT take the apply lock: the window calls `Service.delete_all_data()`, which holds the service
#: mutex and the lock around it (O3's M2.7 note 3). True if the recovery records were deleted too.
delete_all_data = delete_store_data


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
