"""O2 — CLI entry points (M2.11, contract K10).

This module is the process's entry point. It must remain importable without
GTK, because `reset`, `recover`, `uninstall` and `doctor` never load UI code
(A2). The GTK application (and the confirm window) are imported only on the
branch that needs them.

Command lines accepted — exactly K10 (`contracts.cli.COMMANDS`):
  scribesense                                   open / focus the app
  scribesense apply <preset-id>                 -> GTK app (A1)
  scribesense preset next                       -> GTK app (A1)
  scribesense read --selection | --clipboard    -> GTK app (A1)
  scribesense reset                             recovery (A2): revert all, K4 rule 4
  scribesense recover --login                   recovery (A2): login check, step 1 only (A3)
  scribesense uninstall [--delete-data] [--remove-fonts-anyway]   recovery (A2), M3.8
  scribesense doctor                            recovery (A2), M3.7
  scribesense confirm <tx>                      fresh process, never forwarded (A15), M2.3
  scribesense quit                              -> GTK app; the primary instance quits (A1)
  scribesense -h | --help                       usage on stdout, exit 0

Routing rules (A1, A2, A15):
  - a bad command line is a usage error (exit 2) and never loads GTK.
  - `reset`, `recover`, `uninstall`, `doctor` never need the service or GTK and never
    return 4 (K10). They open the store themselves (K9), build one `ApplyLock` shared
    by the `LockPolicy` and the `SqliteJournal` (K4 rule 6), and use the production
    adapters (`adapters.registry`).
  - `confirm <tx>` runs as a fresh process: `ui.confirm.run_confirm(tx)`.
  - everything else goes to the GTK application with the ORIGINAL argv: the first
    launch becomes the single-instance service, later launches are forwarded to it
    by Gio and handled in `ScribeSenseApp.do_command_line` (A1). If the app cannot
    start (GTK / Adw missing, start-up crash), `read` and `preset next` exit 4
    (K10: the service could not be started); the other app commands exit 1.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, cast

from scribesense.contracts.cli import COMMANDS, RECOVERY_COMMANDS, ExitCode
from scribesense.contracts.errors import ErrorCode, ScribeSenseError

if TYPE_CHECKING:  # typing only: never imported at run time here (A2)
    from scribesense.contracts.journal import LockRole
    from scribesense.safety.revert import RevertReport
    from scribesense.store.core import Store

#: Errors that mean "another transaction is active" (K10 exit 3).
_BUSY_CODES: frozenset[ErrorCode] = frozenset({ErrorCode.LOCK_TIMEOUT, ErrorCode.APPLY_IN_PROGRESS})

#: First words that go to the GTK application ("" = open / focus the app).
_APP_COMMANDS: frozenset[str] = frozenset({"", "apply", "preset", "read", "quit"})

#: K10: `read` / `preset next` start the service if it isn't running, and exit 4 only if that fails. Other app
#: commands that cannot start the app exit 1 (K10 asks 4 of these two only).
_SERVICE_STARTERS: frozenset[str] = frozenset({"read", "preset"})


class _UsageError(Exception):
    """A command line K10 does not define. Carries a short, content-free reason."""


def usage() -> str:
    """The K10 command list, one per line."""
    width = max(map(len, COMMANDS))
    lines = ["Usage:"] + [f"  {cmd.ljust(width)}  {who}" for cmd, who in COMMANDS.items()]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- parsing (K10, exact)

def _parse(rest: list[str]) -> tuple[str, dict[str, object]]:
    """`(command, options)` for a K10 command line, or `_UsageError`. "help" is returned for -h/--help."""
    if not rest:
        return "", {}
    command, args = rest[0], rest[1:]
    if command in ("-h", "--help") and not args:
        return "help", {}
    if args in (["-h"], ["--help"]) and (command in RECOVERY_COMMANDS or command in _APP_COMMANDS
                                         or command == "confirm"):
        return "help", {}
    if command == "apply":
        if len(args) != 1 or not args[0] or args[0].startswith("-"):
            raise _UsageError("apply needs exactly one <preset-id>")
        return command, {"preset_id": args[0]}
    if command == "preset":
        if args != ["next"]:
            raise _UsageError("the only preset command is 'preset next'")
        return command, {}
    if command == "read":
        if args not in (["--selection"], ["--clipboard"]):
            raise _UsageError("read needs exactly one of --selection or --clipboard")
        return command, {"source": args[0][2:]}
    if command in ("reset", "doctor", "quit"):
        if args:
            raise _UsageError(f"{command} takes no arguments")
        return command, {}
    if command == "recover":
        if args != ["--login"]:
            raise _UsageError("the only recover command is 'recover --login'")
        return command, {}
    if command == "uninstall":
        allowed = ("--delete-data", "--remove-fonts-anyway")
        if any(a not in allowed for a in args) or len(set(args)) != len(args):
            raise _UsageError("uninstall accepts only --delete-data and --remove-fonts-anyway")
        return command, {"delete_data": "--delete-data" in args,
                         "remove_fonts_anyway": "--remove-fonts-anyway" in args}
    if command == "confirm":
        if len(args) != 1 or not args[0].isascii() or not args[0].isdigit() or int(args[0]) < 1:
            raise _UsageError("confirm needs one transaction number")
        return command, {"tx": int(args[0])}
    raise _UsageError(f"unknown command '{command}'")


# ---------------------------------------------------------------- recovery commands (A2)

def _status(message: str) -> None:
    """LockPolicy.on_status: factual progress on stderr, so stdout stays the report."""
    sys.stderr.write(message + "\n")


def _lock_wait_seconds(store: Store) -> float:
    """K9 setting `lock_wait_seconds` (10–600, default 60): how long recovery waits for another recovery."""
    from scribesense.safety.recovery import DEFAULT_LOCK_WAIT_S

    try:
        value = store.settings.get("lock_wait_seconds")
    except (ScribeSenseError, KeyError):
        return DEFAULT_LOCK_WAIT_S  # an unreadable setting must not stop recovery
    return float(value) if isinstance(value, int) else DEFAULT_LOCK_WAIT_S


def _fail(command: str, exc: ScribeSenseError) -> int:
    """Map a ScribeSenseError to K10: busy -> 3, anything else -> 1. Never 4 for recovery commands."""
    from scribesense.log import log

    log(exc.code, "cli", step=command)
    if exc.code in _BUSY_CODES:
        sys.stderr.write(f"scribesense {command}: another ScribeSense operation is running "
                         f"({exc.code.value}). Nothing was changed; try again in a moment.\n")
        return int(ExitCode.BUSY)
    sys.stderr.write(f"scribesense {command}: failed ({exc.code.value}).\n")
    return int(ExitCode.FAILED)


def _print_revert_report(report: RevertReport) -> None:
    """K10: skipped / failed locations in plain text, then ONE final counts line."""
    if report.skipped:
        print("Left alone (changed by someone else after ScribeSense):")
        for loc in report.skipped:
            print(f"  {loc}")
    if report.failed:
        print("Could not restore (recovery information kept; run `scribesense reset` again):")
        for loc in report.failed:
            print(f"  {loc}")
    print(f"restored {len(report.restored)} · skipped {len(report.skipped)} · failed {len(report.failed)}")


def _run_recovery(command: str, role: LockRole, operation: Callable[..., RevertReport]) -> int:
    """`reset` / `recover --login`: open the existing store in recovery mode and run `operation`.

    No store file means nothing was ever applied: there is nothing to revert (exit 0, nothing created).
    """
    from scribesense.store.core import default_path

    if not default_path().exists():
        if command == "reset":
            print("restored 0 · skipped 0 · failed 0")
        else:
            print("nothing to recover")
        return int(ExitCode.OK)

    from scribesense.adapters import registry
    from scribesense.safety.journal import SqliteJournal
    from scribesense.safety.lock import ApplyLock
    from scribesense.safety.recovery import LockPolicy
    from scribesense.store.core import Store

    try:
        store = Store(default_path(), mode="recovery")
    except ScribeSenseError as exc:
        return _fail(command, exc)
    try:
        lock = ApplyLock()  # ONE lock per process: the policy and the journal share it (K4 rule 6)
        journal = SqliteJournal(store, lock, role=role)
        policy = LockPolicy(lock, role, wait_s=_lock_wait_seconds(store), on_status=_status)
        report = operation(journal, registry.by_name(), policy=policy)
    except ScribeSenseError as exc:
        return _fail(command, exc)
    finally:
        store.close()
    _print_revert_report(report)
    return int(ExitCode.OK if report.ok else ExitCode.FAILED)


def _cmd_reset() -> int:
    from scribesense.safety.recovery import reset

    return _run_recovery("reset", "reset", reset)


def _cmd_recover() -> int:
    from scribesense.safety.recovery import recover_at_login

    return _run_recovery("recover", "recover", recover_at_login)


def _cmd_uninstall(delete_data: bool, remove_fonts_anyway: bool) -> int:
    """M3.8 through O3's `run_uninstall` (prints its own report, returns the K10 exit code).

    An existing store is opened in recovery mode (no migration, never a new file). Without a store see
    `_uninstall_without_store`: nothing is created.
    """
    from scribesense.store.core import default_path

    if not default_path().exists():
        return _uninstall_without_store(delete_data, remove_fonts_anyway)

    from scribesense.adapters import registry
    from scribesense.safety.journal import SqliteJournal
    from scribesense.safety.lock import ApplyLock
    from scribesense.safety.recovery import LockPolicy
    from scribesense.store.core import Store
    from scribesense.uninstall import run_uninstall

    try:
        store = Store(default_path(), mode="recovery")
    except ScribeSenseError as exc:
        return _fail("uninstall", exc)
    try:
        lock = ApplyLock()
        journal = SqliteJournal(store, lock, role="uninstall")
        policy = LockPolicy(lock, "uninstall", wait_s=_lock_wait_seconds(store), on_status=_status)
        return int(run_uninstall(journal, registry.by_name(), store=store, policy=policy,
                                 delete_data=delete_data, remove_fonts_anyway=remove_fonts_anyway))
    except ScribeSenseError as exc:
        return _fail("uninstall", exc)
    finally:
        store.close()  # harmless if the data step already closed it


def _uninstall_without_store(delete_data: bool, remove_fonts_anyway: bool) -> int:
    """`uninstall` when no store exists: nothing was ever applied, so no transaction can be pending (K4).

    Generated fonts and the keybind / login hooks may still exist, so `run_uninstall` still runs, with an
    empty in-memory journal (its reset restores nothing; it proves "nothing pending") and `store=None`. This
    creates no database and no data dir (A2: recovery never creates the store). `--delete-data` has nothing
    to delete and is not passed on (`run_uninstall` rejects it without a store). The apply lock is taken only
    if its file already exists (a store deleted earlier by `--delete-data` leaves the stable lock file,
    K4 rule 1); otherwise no process has ever held it and taking it would create the data dir.
    """
    from scribesense.adapters import registry
    from scribesense.contracts.paths import lock_path
    from scribesense.safety.lock import ApplyLock
    from scribesense.safety.memory_journal import InMemoryJournal
    from scribesense.safety.recovery import DEFAULT_LOCK_WAIT_S, LockPolicy
    from scribesense.uninstall import run_uninstall

    if delete_data:
        print("No ScribeSense database exists: there is no history, preset or setting to delete.")
    policy = None
    if lock_path().exists():
        policy = LockPolicy(ApplyLock(), "uninstall", wait_s=DEFAULT_LOCK_WAIT_S, on_status=_status)
    try:
        return int(run_uninstall(InMemoryJournal(), registry.by_name(), store=None, policy=policy,
                                 remove_fonts_anyway=remove_fonts_anyway))
    except ScribeSenseError as exc:
        return _fail("uninstall", exc)


def _cmd_doctor() -> int:
    from scribesense.doctor import run_doctor  # M3.7 (O3)

    return int(run_doctor())


# ---------------------------------------------------------------- GTK branches (lazy)

def _gtk_missing(exc: BaseException, code: ExitCode = ExitCode.FAILED) -> int:
    sys.stderr.write(
        "scribesense: GTK4 / libadwaita (PyGObject) is required but not "
        f"available: {type(exc).__name__}\n"
        "Install the system packages, then re-run. See `scribesense doctor` for details.\n"
        "Recovery still works without them: `scribesense reset`.\n"
    )
    return int(code)


def _cmd_confirm(tx: int) -> int:
    """A15: a fresh process that never registers with Gio's single-instance forwarding."""
    try:
        from scribesense.ui.confirm import run_confirm  # M2.3

        # GTK is loaded inside run_confirm: a missing PyGObject / typelib surfaces from the call.
        return int(run_confirm(tx))
    except (ImportError, ValueError) as exc:  # ValueError: gi.require_version found no typelib
        return _gtk_missing(exc)


def _cmd_app(command: str, argv: list[str]) -> int:
    """No command, apply, preset, read, quit: the GTK app gets the original argv (A1).

    The app that fails to start is the service that fails to start: `read` / `preset next` then exit 4 (K10).
    """
    failed = ExitCode.SERVICE_UNAVAILABLE if command in _SERVICE_STARTERS else ExitCode.FAILED
    try:
        from scribesense.ui.app import run as run_app
    except (ImportError, ValueError) as exc:
        return _gtk_missing(exc, failed)
    try:
        return int(run_app(argv))
    except (ImportError, ValueError) as exc:  # GTK / Adw typelibs missing at start-up
        return _gtk_missing(exc, failed)
    except Exception as exc:  # noqa: BLE001 — startup failures must not leave a blank window
        sys.stderr.write(f"scribesense: failed to start ({type(exc).__name__}).\n"
                         "Recovery: run `scribesense reset` (works without the app window) "
                         "or press Ctrl+Alt+Shift+Super+Backspace.\n")
        return int(failed)


# ---------------------------------------------------------------- entry point

def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    try:
        command, opts = _parse(args[1:])
    except _UsageError as exc:
        sys.stderr.write(f"scribesense: {exc}\n{usage()}")
        return int(ExitCode.USAGE)

    if command == "help":
        sys.stdout.write(usage())
        return int(ExitCode.OK)
    if command == "reset":
        return _cmd_reset()
    if command == "recover":
        return _cmd_recover()
    if command == "uninstall":
        return _cmd_uninstall(bool(opts["delete_data"]), bool(opts["remove_fonts_anyway"]))
    if command == "doctor":
        return _cmd_doctor()
    if command == "confirm":
        return _cmd_confirm(cast(int, opts["tx"]))
    return _cmd_app(command, args)


if __name__ == "__main__":
    raise SystemExit(main())
