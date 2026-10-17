"""O3 — `scribesense uninstall [--delete-data] [--remove-fonts-anyway]` (M3.8, K10, R2-9).

A RECOVERY command (A2): it never needs the service or the GUI, never imports Gtk / Adw / ui / reader, and never
returns exit code 4. `cli.py` (O2) calls `run_uninstall(...)`; this module only needs the pieces below handed in.

Order (card M3.8):
  1. reset — K4 rule 4, steps 1 + 2 (finish unfinished txs, then restore every baseline; a setting the user changed
     afterwards is SKIPPED, never overwritten). One item failing does not stop the uninstall: it is reported.
  2. remove the generated fonts EXCEPT those still referenced by skipped / unresolved settings, or whose use cannot
     be determined (R2-9) — unless `remove_fonts_anyway`.
  3. remove the keybind file and its include line, and with them the login check (`recover --login`), through
     `safety.keybinds.remove_keybinds` (hand-edited blocks are left alone and reported) — but only when nothing is
     left for recovery: if a revert failed or a transaction is still unfinished, the hooks stay so the login check
     and the recovery keybind can retry.
  4. keep the data — unless "delete all" was asked for AND confirmed. Never delete user data without confirmation.

Nothing here installs anything, edits another owner's files, or touches a desktop on its own: every effect goes
through the injected journal, adapters, font operations and keybind remover.

Lock (K4 rule 6, owner "uninstall"): taken once through `policy` before step 1 and held until the end, so no apply
can start between the reset and the font / keybind / data steps. The journal must share `policy.lock` (the SQLite
journal then uses the held lock for its revert tx and leaves releasing it to this module).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from scribesense.contracts.adapter import Adapter
from scribesense.contracts.cli import ExitCode
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import READING_FAMILY_PREFIX, UI_FAMILY_PREFIX
from scribesense.contracts.journal import Journal
from scribesense.contracts.paths import data_dir
from scribesense.log import log, log_path
from scribesense.safety.keybinds import remove_keybinds
from scribesense.safety.recovery import LockPolicy, acquire_for_recovery
from scribesense.safety.recovery import reset as recovery_reset
from scribesense.safety.revert import RevertReport
from scribesense.store.core import Store

#: R2-9 wording, shown whenever generated fonts were kept.
FONT_FALLBACK_NOTE = "these apps may fall back to another font and their appearance may change"
CONFIRM_WORD = "delete"

KeybindOutcome = Literal["removed", "absent", "left_edited", "failed", "kept_for_recovery"]
DataOutcome = Literal["kept", "deleted", "preferences_only", "not_confirmed"]


class FontOps(Protocol):
    def installed_keys(self) -> list[str]: ...

    def remove(self, key: str) -> bool: ...


class CacheFontOps:
    """The real operations: M1.7's `fontgen.cache`, which deletes only `<fonts_dir()>/<key>/`."""

    def installed_keys(self) -> list[str]:
        from scribesense.fontgen import cache

        return cache.installed_keys()

    def remove(self, key: str) -> bool:
        from scribesense.fontgen import cache

        return cache.remove(key)


@dataclass
class UninstallReport:
    reset: RevertReport = field(default_factory=RevertReport)
    fonts_removed: list[str] = field(default_factory=list)
    fonts_kept_referenced: list[str] = field(default_factory=list)  # a skipped / unresolved setting still uses it
    fonts_kept_unknown: list[str] = field(default_factory=list)  # use could not be determined
    fonts_failed: list[str] = field(default_factory=list)
    keybinds: KeybindOutcome = "absent"
    data: DataOutcome = "kept"
    error: ErrorCode | None = None  # the uninstall stopped before changing anything else


def _terminal_confirm(question: str) -> bool:
    """Ask on the terminal. Without an interactive terminal the answer is NO: nothing is deleted unasked."""
    if not (sys.stdin and sys.stdin.isatty()):
        return False
    sys.stdout.write(f"{question}\nType '{CONFIRM_WORD}' to confirm: ")
    sys.stdout.flush()
    return sys.stdin.readline().strip() == CONFIRM_WORD


def _recovery_pending(journal: Journal, report: UninstallReport) -> bool:
    """Something could not be restored yet: a revert failed, or a transaction is still unfinished."""
    return bool(report.reset.failed) or bool(journal.unfinished())


#: A generated family name inside a planned write ("AccessSans-<key>" / "AccessSansUI-<key>", K7, A19).
_FAMILY_IN_PAYLOAD = re.compile(
    rf"(?:{re.escape(UI_FAMILY_PREFIX)}|{re.escape(READING_FAMILY_PREFIX)})([0-9a-f]{{12}})(?![0-9a-f])")


def _referenced_font_keys(journal: Journal) -> set[str]:
    """Font keys something may still use after the reset, so they are kept for a retry:

    * a location we could not restore that is still managed or unresolved (`managed_font_key`);
    * a planned write of a still-unfinished transaction — e.g. a crashed, unconfirmed apply whose revert failed:
      it never set `managed`, but the setting it wrote may still name its font.
    """
    keys = {r.managed_font_key for r in journal.locations()
            if r.managed_font_key and (r.managed_sha256 is not None or r.unresolved is not None)}
    for tx in journal.unfinished():
        for item in journal.items(tx):
            if item.intent is not None:
                payload = item.intent.payload
                text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
                keys |= set(_FAMILY_IN_PAYLOAD.findall(text))
    return keys


def _remove_fonts(report: UninstallReport, journal: Journal, fonts: FontOps, store: Store | None,
                  remove_anyway: bool) -> None:
    try:
        keys = fonts.installed_keys()
    except Exception:  # noqa: BLE001 — cannot even list them: nothing is removed
        report.fonts_failed.append("(listing)")
        return
    referenced: set[str] = set()
    unknown = False
    if not remove_anyway:
        try:
            referenced = _referenced_font_keys(journal)
        except Exception:  # noqa: BLE001 — "if usage can't be determined, the font is kept" (K7 rule 3 / R2-9)
            unknown = True
    for key in keys:
        if unknown:
            report.fonts_kept_unknown.append(key)
        elif key in referenced:
            report.fonts_kept_referenced.append(key)
        else:
            try:
                if fonts.remove(key):
                    report.fonts_removed.append(key)
                    _mark_removed(store, key)
            except Exception:  # noqa: BLE001 — a font that cannot be removed is reported; the rest goes on
                report.fonts_failed.append(key)


def _mark_removed(store: Store | None, key: str) -> None:
    if store is None:
        return
    try:
        store.fonts.set_status(key, "removed", None)  # the files are gone; the registry row follows
    except (KeyError, ValueError, ScribeSenseError):
        pass  # no row, or it was not 'validated': nothing to record


def _remove_keybinds(report: UninstallReport, remover: Callable[[], bool]) -> None:
    try:
        report.keybinds = "removed" if remover() else "absent"
    except ScribeSenseError as exc:
        report.keybinds = "left_edited" if exc.code is ErrorCode.DRIFT_SKIPPED else "failed"
        log(exc.code, "uninstall", step="keybinds")


def _delete_data(report: UninstallReport, journal: Journal, store: Store, confirm: Callable[[str], bool]) -> None:
    question = ("Delete all ScribeSense data (history, presets, settings, backups)? This removes ScribeSense's "
                "records — it is not secure erasure. Restoring the original settings from ScribeSense will no "
                "longer be possible.")
    if not confirm(question):
        report.data = "not_confirmed"
        return
    token = journal.no_obligations()
    if token is None:
        # Something is still unfinished, unresolved or managed (K9): the recovery records stay.
        store.delete_preferences()
        report.data = "preferences_only"
        log(ErrorCode.RECOVERY_OBLIGATIONS_REMAIN, "uninstall", step="data")
        return
    store.delete_recovery_data(precondition=token)
    store.delete_preferences()
    store.close()
    for name in ("", "-wal", "-shm"):
        Path(str(store.path) + name).unlink(missing_ok=True)
    for path in (log_path(), Path(str(log_path()) + ".1")):
        path.unlink(missing_ok=True)
    try:
        data_dir().rmdir()  # only if empty: the stable lock file is never ours to remove (K4 rule 1)
    except OSError:
        pass
    report.data = "deleted"


def uninstall(
    journal: Journal,
    adapters: Mapping[str, Adapter],
    *,
    store: Store | None = None,
    policy: LockPolicy | None = None,
    delete_data: bool = False,
    remove_fonts_anyway: bool = False,
    confirm: Callable[[str], bool] | None = None,
    fonts: FontOps | None = None,
    keybind_remover: Callable[[], bool] = remove_keybinds,
) -> UninstallReport:
    """Run the uninstall. Never raises for a problem with one item; a problem that stops everything is `.error`."""
    if delete_data and store is None:
        raise ValueError("delete_data needs the store")
    report = UninstallReport()
    if policy is not None:
        try:
            acquire_for_recovery(policy)  # held for the WHOLE uninstall, not only the reset
        except ScribeSenseError as exc:
            report.error = exc.code
            log(exc.code, "uninstall", step="lock")
            return report
    try:
        _run(report, journal, adapters, store=store, delete_data=delete_data,
             remove_fonts_anyway=remove_fonts_anyway, confirm=confirm, fonts=fonts, keybind_remover=keybind_remover)
    finally:
        if policy is not None:
            policy.lock.release()
    return report


def _run(report: UninstallReport, journal: Journal, adapters: Mapping[str, Adapter], *, store: Store | None,
         delete_data: bool, remove_fonts_anyway: bool, confirm: Callable[[str], bool] | None,
         fonts: FontOps | None, keybind_remover: Callable[[], bool]) -> None:
    try:
        report.reset = recovery_reset(journal, adapters)  # the lock is already held (or there is no policy)
    except ScribeSenseError as exc:  # the journal itself failed: change nothing else
        report.error = exc.code
        log(exc.code, "uninstall", step="reset")
        return
    _remove_fonts(report, journal, fonts or CacheFontOps(), store, remove_fonts_anyway)
    if _recovery_pending(journal, report):
        report.keybinds = "kept_for_recovery"  # the login check and recovery keybind must stay to retry
        log(ErrorCode.RECOVERY_OBLIGATIONS_REMAIN, "uninstall", step="keybinds")
    else:
        _remove_keybinds(report, keybind_remover)
    if delete_data and store is not None:
        try:
            _delete_data(report, journal, store, confirm or _terminal_confirm)
        except ScribeSenseError as exc:
            report.error = exc.code
            log(exc.code, "uninstall", step="data")


def exit_code(report: UninstallReport) -> ExitCode:
    """K10: 0 = completed (skipped items are described, not failures) · 1 = something failed · 3 = busy. Never 4 (A2)."""
    if report.error in (ErrorCode.LOCK_TIMEOUT, ErrorCode.APPLY_IN_PROGRESS):
        return ExitCode.BUSY
    if report.error is not None or report.reset.failed or report.fonts_failed or report.keybinds == "failed":
        return ExitCode.FAILED
    return ExitCode.OK


def format_report(report: UninstallReport) -> str:
    lines: list[str] = []
    if report.error is not None:
        lines.append(f"The uninstall stopped: {report.error.value}. Nothing else was changed.")
        if report.error in (ErrorCode.LOCK_TIMEOUT, ErrorCode.APPLY_IN_PROGRESS):
            lines.append("Another ScribeSense operation is running. Try again in a moment.")
        return "\n".join(lines)
    if report.reset.skipped:
        lines.append("Skipped (you changed these after ScribeSense — left exactly as they are):")
        lines += [f"  {loc}" for loc in report.reset.skipped]
    if report.reset.failed:
        lines.append("Failed to restore:")
        lines += [f"  {loc}" for loc in report.reset.failed]
        lines.append("Run `scribesense reset` to try again.")
    kept = report.fonts_kept_referenced + report.fonts_kept_unknown
    if kept:
        why = []
        if report.fonts_kept_referenced:
            why.append("settings you changed after ScribeSense still refer to them")
        if report.fonts_kept_unknown:
            why.append("their use could not be determined")
        lines.append(f"Kept {len(kept)} generated font(s): {'; '.join(why)}. {FONT_FALLBACK_NOTE.capitalize()}.")
        lines.append("Use --remove-fonts-anyway to remove them.")
    if report.fonts_failed:
        lines.append(f"Could not remove {len(report.fonts_failed)} generated font(s); run the uninstall again.")
    lines.append(f"Generated fonts removed: {len(report.fonts_removed)}.")
    lines.append({
        "removed": "Keybinds and the login check were removed.",
        "absent": "No keybinds or login check were installed.",
        "left_edited": "The keybind block was edited by hand, so it was left in place. Remove it yourself.",
        "failed": "The keybinds or login check could not be removed (see the log for the code).",
        "kept_for_recovery": "Keybinds and the login check were kept so recovery can retry what could not be "
                             "restored. Run `scribesense reset`, then uninstall again.",
    }[report.keybinds])
    lines.append({
        "kept": "Your ScribeSense data was kept. Use --delete-data to remove it.",
        "not_confirmed": "Deleting your data was not confirmed, so it was kept.",
        "preferences_only": "Your presets, settings and samples were deleted. The recovery records were kept "
                            "because something is still unresolved.",
        "deleted": "All ScribeSense data was deleted.",
    }[report.data])
    lines.append(f"restored {len(report.reset.restored)} · skipped {len(report.reset.skipped)} · "
                 f"failed {len(report.reset.failed)}")
    return "\n".join(lines)


def run_uninstall(journal: Journal, adapters: Mapping[str, Adapter], **options) -> ExitCode:  # type: ignore[no-untyped-def]
    """What `cli.py` calls: run, print the report, return the K10 exit code."""
    report = uninstall(journal, adapters, **options)
    sys.stdout.write(format_report(report) + "\n")
    return exit_code(report)
