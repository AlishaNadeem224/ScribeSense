"""M2.11 acceptance — each recovery command end to end (K10, A2, A3, K4 rules 4 and 6).

Runs cli.main against the REAL store and SQLite journal inside the unit-test sandbox (tests/conftest.py
points the data dir, runtime dir and config home at tmp_path) and against FakeAdapters over a FakeWorld
in place of the production adapters (`adapters.registry.default_adapters` is monkeypatched). No real
desktop, no process, no socket.

"Import test proves recovery commands load no UI modules", in two parts:
  * at run time, every recovery command runs with GTK / Adw / scribesense.ui / scribesense.reader removed from
    sys.modules and an import blocker installed; the test fails if anything tries to load them. One uninstall
    runs with the REAL production adapters, freshly imported under the blocker (`registry.default_adapters`
    imports all six adapter modules inside the function, and the other tests patch it away);
  * statically, everything the recovery branches of cli.py import — function-level imports included, so all
    six production adapters are reached through `adapters.registry` — never reaches GUI code
    (tests/unit/test_import_rules.py's scanner; a module already loaded at collection time is invisible to the
    run-time blocker, and `gi.require_version("Gtk", ...)` is not an import at all).
"""

from __future__ import annotations

import ast
import importlib.abc
import importlib.util
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from scribesense import cli
from scribesense import uninstall as un
from scribesense.adapters import registry
from scribesense.adapters.fake import FakeAdapter, FakeWorld
from scribesense.contracts.cli import ExitCode
from scribesense.contracts.config import Configuration
from scribesense.contracts.journal import TxId
from scribesense.contracts.paths import data_dir, lock_path
from scribesense.contracts.states import TxState
from scribesense.safety.journal import SqliteJournal
from scribesense.safety.lock import ApplyLock
from scribesense.safety.testing import fake_fontset, make_recovery_system
from scribesense.store import Store
from scribesense.store.core import default_path

FILE = "/fake/home/.config/fontconfig/conf.d/99-scribesense.conf"
KEY = "org.gnome.desktop.interface font-name"
OTHER = "/fake/home/.config/qt6ct/qt6ct.conf"
A = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.0)
GUI_PREFIXES = ("gi.repository.Gtk", "gi.repository.Adw", "scribesense.ui", "scribesense.reader")


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def world() -> FakeWorld:
    w = FakeWorld()
    w.set_file(FILE, None)
    w.set_key(KEY, "'Fira Sans 11'")
    w.set_file(OTHER, b"[Fonts]\ngeneral=Fira Sans\n")
    return w


@pytest.fixture
def adapter(world: FakeWorld, monkeypatch: pytest.MonkeyPatch) -> FakeAdapter:
    """The production adapter list, replaced by one FakeAdapter (registry.by_name() then maps it)."""
    fake = FakeAdapter(world, {"t": [FILE, KEY, OTHER]})
    monkeypatch.setattr(registry, "default_adapters", lambda: [fake])
    return fake


class _GuiBlocker(importlib.abc.MetaPathFinder):
    def __init__(self) -> None:
        self.attempts: list[str] = []

    def find_spec(self, name: str, path: object = None, target: object = None) -> None:
        if name.startswith(GUI_PREFIXES):
            self.attempts.append(name)
            raise ImportError(f"A2: {name} must not be imported by a recovery command")
        return None


@pytest.fixture(autouse=True)
def no_gui(monkeypatch: pytest.MonkeyPatch) -> Iterator[_GuiBlocker]:
    """A2, checked transitively at run time: no GUI module is loaded while a recovery command runs."""
    for name in [n for n in sys.modules if n.startswith(GUI_PREFIXES)]:
        monkeypatch.delitem(sys.modules, name)  # restored at teardown
    blocker = _GuiBlocker()
    monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])
    yield blocker
    assert blocker.attempts == []
    assert not [n for n in sys.modules if n.startswith(GUI_PREFIXES)]


def _applied(world: FakeWorld, adapter: FakeAdapter, *, keep: bool) -> TxId:
    """An apply by a service process (real SQLite journal), which then exits: lock dropped, store closed."""
    system = make_recovery_system(world, [adapter], journal="sqlite")
    tx = system.apply(A, keep=keep)
    system.journal._lock.release()  # the process is gone: the OS drops its flock ...
    system.journal._t._s.close()  # ... and its database connection
    return tx


def _journal() -> tuple[SqliteJournal, Store]:
    store = Store(default_path(), mode="recovery")
    return SqliteJournal(store, ApplyLock(), role="reset"), store


def _last_line(out: str) -> str:
    return out.strip().splitlines()[-1]


# ---------------------------------------------------------------- reset

def test_reset_after_an_apply_restores_everything_and_prints_the_summary(
    world: FakeWorld, adapter: FakeAdapter, capsys: pytest.CaptureFixture[str],
) -> None:
    original = world.snapshot_all()
    _applied(world, adapter, keep=True)
    assert world.snapshot_all() != original
    assert cli.main(["scribesense", "reset"]) == int(ExitCode.OK)
    assert world.snapshot_all() == original  # byte-identical
    assert _last_line(capsys.readouterr().out) == "restored 3 · skipped 0 · failed 0"  # K10: ONE counts line
    journal, store = _journal()
    try:
        assert journal.active() is None and journal.unfinished() == []  # "Original"
    finally:
        store.close()
    assert not ApplyLock().is_locked()  # released


def test_reset_with_no_store_exits_0_and_creates_nothing(
    adapter: FakeAdapter, capsys: pytest.CaptureFixture[str],
) -> None:
    assert not default_path().exists()  # nothing was ever applied
    assert cli.main(["scribesense", "reset"]) == int(ExitCode.OK)
    assert capsys.readouterr().out.strip() == "restored 0 · skipped 0 · failed 0"
    assert not default_path().exists()
    assert adapter.calls == []


def test_reset_leaves_a_user_change_alone_and_says_so(
    world: FakeWorld, adapter: FakeAdapter, capsys: pytest.CaptureFixture[str],
) -> None:
    _applied(world, adapter, keep=True)
    world.external_edit(OTHER, b"[Fonts]\ngeneral=User Choice\n")  # the user changed it afterwards
    assert cli.main(["scribesense", "reset"]) == int(ExitCode.OK)  # skipped is not a failure (K10)
    out = capsys.readouterr().out
    assert OTHER in out and _last_line(out) == "restored 2 · skipped 1 · failed 0"
    assert world.state(OTHER).data == b"[Fonts]\ngeneral=User Choice\n"  # never overwritten


def test_reset_exits_1_when_a_location_cannot_be_restored(
    world: FakeWorld, adapter: FakeAdapter, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _applied(world, adapter, keep=True)
    monkeypatch.setattr(registry, "default_adapters", lambda: [])  # the adapter is gone: nothing can revert
    assert cli.main(["scribesense", "reset"]) == int(ExitCode.FAILED)  # exit 0 only on success (M2.11)
    out = capsys.readouterr().out
    assert "run `scribesense reset` again" in out and _last_line(out) == "restored 0 · skipped 0 · failed 3"


def test_reset_while_another_recovery_holds_the_lock_exits_3_and_changes_nothing(
    world: FakeWorld, adapter: FakeAdapter, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _applied(world, adapter, keep=True)
    changed = world.snapshot_all()
    monkeypatch.setattr(cli, "_lock_wait_seconds", lambda _store: 0.2)  # K9 lock_wait_seconds, shortened
    other = ApplyLock()  # another `scribesense uninstall` is running: never interrupted (K4 rule 6)
    assert other.try_acquire("uninstall")
    try:
        assert cli.main(["scribesense", "reset"]) == int(ExitCode.BUSY)
    finally:
        other.release()
    assert world.snapshot_all() == changed
    err = capsys.readouterr().err
    assert "Waiting for uninstall" in err and "LOCK_TIMEOUT" in err


def test_reset_on_an_unreadable_store_exits_1_with_the_code(
    adapter: FakeAdapter, capsys: pytest.CaptureFixture[str],
) -> None:
    default_path().parent.mkdir(parents=True, exist_ok=True)
    default_path().write_bytes(b"this is not a database")
    code = cli.main(["scribesense", "reset"])
    assert code == int(ExitCode.FAILED)
    err = capsys.readouterr().err
    assert err.startswith("scribesense reset: failed (")
    assert any(code in err for code in ("STORE_CORRUPT", "STORE_IO_ERROR", "SCHEMA_UNSUPPORTED"))


# ---------------------------------------------------------------- recover --login (A3)

def test_recover_login_reverts_an_unconfirmed_apply(
    world: FakeWorld, adapter: FakeAdapter, capsys: pytest.CaptureFixture[str],
) -> None:
    original = world.snapshot_all()
    tx = _applied(world, adapter, keep=False)  # crashed while awaiting confirmation
    journal, store = _journal()
    try:
        assert journal.unfinished() == [tx]
    finally:
        store.close()
    assert cli.main(["scribesense", "recover", "--login"]) == int(ExitCode.OK)
    assert world.snapshot_all() == original
    assert _last_line(capsys.readouterr().out) == "restored 3 · skipped 0 · failed 0"
    journal, store = _journal()
    try:
        assert journal.unfinished() == []
        assert journal.state(tx) is TxState.REVERTED
    finally:
        store.close()


def test_recover_login_never_touches_a_confirmed_configuration(
    world: FakeWorld, adapter: FakeAdapter,
) -> None:
    _applied(world, adapter, keep=True)
    kept = world.snapshot_all()
    assert cli.main(["scribesense", "recover", "--login"]) == int(ExitCode.OK)
    assert world.snapshot_all() == kept  # A3: step 1 only
    journal, store = _journal()
    try:
        assert journal.active() is not None and journal.active().font_key == fake_fontset(A).key
    finally:
        store.close()


def test_recover_login_with_no_store_exits_0(adapter: FakeAdapter, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["scribesense", "recover", "--login"]) == int(ExitCode.OK)
    assert "nothing to recover" in capsys.readouterr().out
    assert not default_path().exists()


def test_recover_login_busy_exits_3_never_4(
    world: FakeWorld, adapter: FakeAdapter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _applied(world, adapter, keep=False)
    monkeypatch.setattr(cli, "_lock_wait_seconds", lambda _store: 0.2)
    other = ApplyLock()
    assert other.try_acquire("reset")
    try:
        assert cli.main(["scribesense", "recover", "--login"]) == int(ExitCode.BUSY)
    finally:
        other.release()


# ---------------------------------------------------------------- uninstall (M3.8 through run_uninstall)

class _Spy:
    """Wraps O3's real `uninstall()` with fake fonts and keybinds; records what cli handed over."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[dict[str, Any]] = []
        self.keybind_calls = 0
        monkeypatch.setattr(un, "run_uninstall", self)

    def _keybinds(self) -> bool:
        self.keybind_calls += 1
        return True

    def __call__(self, journal: SqliteJournal, adapters: dict[str, Any], **options: Any) -> ExitCode:
        self.calls.append({"journal": journal, "adapters": adapters, **options})
        report = un.uninstall(journal, adapters, fonts=_NoFonts(), keybind_remover=self._keybinds, **options)
        sys.stdout.write(un.format_report(report) + "\n")
        return un.exit_code(report)


class _NoFonts:
    def installed_keys(self) -> list[str]:
        return []

    def remove(self, key: str) -> bool:
        return False


def test_uninstall_after_an_apply_restores_everything(
    world: FakeWorld, adapter: FakeAdapter, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = world.snapshot_all()
    _applied(world, adapter, keep=True)
    spy = _Spy(monkeypatch)
    assert cli.main(["scribesense", "uninstall"]) == int(ExitCode.OK)
    assert world.snapshot_all() == original
    call = spy.calls[0]
    assert call["adapters"] == {"fake": adapter}
    assert call["policy"].lock is call["journal"]._lock  # one lock per process (K4 rule 6)
    assert call["policy"].role == "uninstall" and call["store"] is not None
    assert call["delete_data"] is False and call["remove_fonts_anyway"] is False
    assert spy.keybind_calls == 1
    assert _last_line(capsys.readouterr().out) == "restored 3 · skipped 0 · failed 0"
    assert not ApplyLock().is_locked()


@pytest.mark.parametrize("flags,delete_data,remove_anyway", [
    (["--delete-data"], True, False),
    (["--remove-fonts-anyway"], False, True),
    (["--delete-data", "--remove-fonts-anyway"], True, True),
])
def test_uninstall_flags_reach_run_uninstall(
    flags: list[str], delete_data: bool, remove_anyway: bool,
    world: FakeWorld, adapter: FakeAdapter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _applied(world, adapter, keep=True)
    spy = _Spy(monkeypatch)
    assert cli.main(["scribesense", "uninstall", *flags]) == int(ExitCode.OK)  # no terminal: data kept
    assert spy.calls[0]["delete_data"] is delete_data
    assert spy.calls[0]["remove_fonts_anyway"] is remove_anyway
    assert default_path().exists()  # deleting data needs a typed confirmation, never given here


def test_uninstall_with_no_store_still_runs_and_creates_nothing(
    adapter: FakeAdapter, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    assert not data_dir().exists()  # nothing was ever applied
    spy = _Spy(monkeypatch)
    assert cli.main(["scribesense", "uninstall"]) == int(ExitCode.OK)
    assert spy.keybind_calls == 1  # fonts / keybinds are still removed
    assert spy.calls[0]["store"] is None and spy.calls[0]["policy"] is None
    assert _last_line(capsys.readouterr().out) == "restored 0 · skipped 0 · failed 0"
    assert not default_path().exists()  # A2: recovery never creates the store ...
    assert not data_dir().exists()  # ... nor the data dir or the lock file
    assert adapter.calls == []


def test_uninstall_delete_data_with_no_store_says_there_is_nothing_to_delete(
    adapter: FakeAdapter, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    spy = _Spy(monkeypatch)
    assert cli.main(["scribesense", "uninstall", "--delete-data"]) == int(ExitCode.OK)
    assert spy.calls[0]["store"] is None and "delete_data" not in spy.calls[0]  # run_uninstall would reject it
    out = capsys.readouterr().out
    assert "No ScribeSense database exists" in out
    assert _last_line(out) == "restored 0 · skipped 0 · failed 0"
    assert not data_dir().exists()


def test_uninstall_with_no_store_still_respects_a_held_lock(
    adapter: FakeAdapter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # --delete-data removed the store earlier; the stable lock file stays (K4 rule 1) and another recovery holds it.
    spy = _Spy(monkeypatch)
    monkeypatch.setattr("scribesense.safety.recovery.DEFAULT_LOCK_WAIT_S", 0.2)
    other = ApplyLock()
    assert other.try_acquire("reset")
    try:
        assert lock_path().exists() and not default_path().exists()
        assert cli.main(["scribesense", "uninstall"]) == int(ExitCode.BUSY)
    finally:
        other.release()
    assert spy.keybind_calls == 0 and not default_path().exists()


def test_uninstall_busy_exits_3(
    world: FakeWorld, adapter: FakeAdapter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _applied(world, adapter, keep=True)
    changed = world.snapshot_all()
    spy = _Spy(monkeypatch)
    monkeypatch.setattr(cli, "_lock_wait_seconds", lambda _store: 0.2)
    other = ApplyLock()
    assert other.try_acquire("reset")
    try:
        assert cli.main(["scribesense", "uninstall"]) == int(ExitCode.BUSY)
    finally:
        other.release()
    assert world.snapshot_all() == changed and spy.keybind_calls == 0


# ---------------------------------------------------------------- the production adapters, without GUI (A2)

PRODUCTION_ADAPTERS = {"fontconfig", "gtk", "qt", "flatpak", "firefox", "chromium"}


def test_uninstall_loads_the_real_production_adapters_without_gui(
    monkeypatch: pytest.MonkeyPatch, no_gui: _GuiBlocker,
) -> None:
    # No `adapter` fixture: registry.default_adapters is NOT patched. The adapter modules are dropped from
    # sys.modules (restored at teardown) so they are imported again here, with the GUI blocker in place.
    for name in [n for n in sys.modules if n == "scribesense.adapters" or n.startswith("scribesense.adapters.")]:
        monkeypatch.delitem(sys.modules, name)
    spy = _Spy(monkeypatch)  # fake fonts / keybinds; no store, so the reset touches no adapter
    assert cli.main(["scribesense", "uninstall"]) == int(ExitCode.OK)
    assert set(spy.calls[0]["adapters"]) == PRODUCTION_ADAPTERS
    assert no_gui.attempts == []


def _import_rules() -> ModuleType:
    """tests/unit/test_import_rules.py's scanner (O3), loaded by path: tests/unit is not a package."""
    path = Path(__file__).with_name("test_import_rules.py")
    spec = importlib.util.spec_from_file_location("_scribesense_import_rules", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up while being defined
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]
    return module


#: cli.py functions that load the GTK app / confirm window on purpose (A1, A15). Every other function serves
#: the recovery commands or usage, and must stay GUI-free.
GUI_BRANCHES = {"_cmd_app", "_cmd_confirm"}


def _recovery_roots(src: Path) -> tuple[str, ...]:
    """Every module the non-GUI functions of cli.py import (inside the function: they load lazily)."""
    tree = ast.parse((src / "scribesense" / "cli.py").read_text())
    roots: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name not in GUI_BRANCHES:
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom) and sub.level == 0 and sub.module:
                    roots.add(sub.module)
                    roots.update(f"{sub.module}.{a.name}" for a in sub.names)
                elif isinstance(sub, ast.Import):
                    roots.update(a.name for a in sub.names)
    return tuple(sorted(r for r in roots if r.startswith("scribesense")))


def _recovery_gui_reach(src: Path) -> dict[str, set[str]]:
    rules = _import_rules()
    modules = rules.scan(src)
    roots = tuple(r for r in _recovery_roots(src) if r in modules)
    return {root: bad for root, bad in rules.gui_reachable(modules, roots, ()).items() if bad}


def test_recovery_branches_of_cli_reach_every_production_adapter_and_no_gui() -> None:
    src = Path(cli.__file__).resolve().parents[1]
    roots = _recovery_roots(src)
    assert "scribesense.adapters.registry" in roots and "scribesense.uninstall" in roots
    rules = _import_rules()
    modules = rules.scan(src)
    # registry imports the adapters inside default_adapters(): the scanner's all_imports must still see them
    reached = modules["scribesense.adapters.registry"].all_imports
    assert {f"scribesense.adapters.{name}" for name in PRODUCTION_ADAPTERS} <= reached
    assert _recovery_gui_reach(src) == {}


@pytest.mark.parametrize("adapter_module", sorted(PRODUCTION_ADAPTERS))
def test_the_static_check_catches_gtk_in_any_production_adapter(adapter_module: str, tmp_path: Path) -> None:
    src = Path(cli.__file__).resolve().parents[1]
    shutil.copytree(src / "scribesense", tmp_path / "scribesense",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    target = tmp_path / "scribesense" / "adapters" / f"{adapter_module}.py"
    target.write_text('import gi\ngi.require_version("Gtk", "4.0")\n'
                      "from gi.repository import Gtk\n" + target.read_text())
    bad = _recovery_gui_reach(tmp_path)
    assert "scribesense.adapters.registry" in bad, bad
    assert any("gi.repository.Gtk" in b for b in bad["scribesense.adapters.registry"])


# ---------------------------------------------------------------- doctor and usage, still without GUI

def test_doctor_runs_without_gui(monkeypatch: pytest.MonkeyPatch) -> None:
    import scribesense.doctor

    monkeypatch.setattr(scribesense.doctor, "run_doctor", lambda: ExitCode.OK)
    assert cli.main(["scribesense", "doctor"]) == int(ExitCode.OK)


@pytest.mark.parametrize("rest", [["reset", "now"], ["recover"], ["uninstall", "--all"], ["nope"]])
def test_recovery_usage_errors_exit_2_without_gui(rest: list[str], adapter: FakeAdapter) -> None:
    assert cli.main(["scribesense", *rest]) == int(ExitCode.USAGE)
    assert adapter.calls == []
