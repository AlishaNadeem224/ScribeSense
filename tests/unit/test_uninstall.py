"""M3.8 — `scribesense uninstall`: reset, fonts kept or removed (R2-9), keybinds and login check, data only on
explicit confirmation. Runs against fake desktops, a fake font directory and a fake keybind remover — no real
desktop, no process (unit-test rules). The VM test ("install → apply → uninstall → byte-identical") waits for M3.5."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from harness.table_journal import TableJournal
from scribesense.adapters.fake import FakeAdapter, FakeWorld
from scribesense.contracts.cli import ExitCode
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.presets import Preset, PresetValues, WordSpacing
from scribesense.log import log_path
from scribesense.safety.memory_journal import InMemoryJournal
from scribesense.safety.testing import RecoverySystem, fake_fontset
from scribesense.store import Store
from scribesense import uninstall as un

FILE = "/fake/home/.config/fontconfig/conf.d/99-scribesense.conf"
KEY = "org.gnome.desktop.interface font-name"
OTHER = "/fake/home/.config/qt6ct/qt6ct.conf"
A = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.0)
A_FONT = fake_fontset(A).key
STRAY = "0123456789ab"  # a generated font nothing references


class FakeFonts:
    def __init__(self, *keys: str, fail: tuple[str, ...] = ()) -> None:
        self.keys = list(keys)
        self.fail = fail
        self.removed: list[str] = []

    def installed_keys(self) -> list[str]:
        return sorted(self.keys)

    def remove(self, key: str) -> bool:
        if key in self.fail:
            raise OSError("cannot remove")
        self.keys.remove(key)
        self.removed.append(key)
        return True


class FakeKeybinds:
    def __init__(self, result: bool | ScribeSenseError = True) -> None:
        self.result = result
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        if isinstance(self.result, ScribeSenseError):
            raise self.result
        return self.result


@pytest.fixture
def world() -> FakeWorld:
    w = FakeWorld()
    w.set_file(FILE, None)
    w.set_key(KEY, "'Fira Sans 11'")
    w.set_file(OTHER, b"[Fonts]\ngeneral=Fira Sans\n")
    return w


@pytest.fixture
def system(world: FakeWorld) -> RecoverySystem:
    s = RecoverySystem(world, [FakeAdapter(world, {"t": [FILE, KEY, OTHER]})])
    s.apply(A, keep=True)
    return s


def run(system: RecoverySystem, fonts: FakeFonts | None = None, keybinds: FakeKeybinds | None = None,
        **kw: Any) -> un.UninstallReport:
    return un.uninstall(system.journal, system.adapters, fonts=fonts or FakeFonts(A_FONT, STRAY),
                        keybind_remover=keybinds or FakeKeybinds(), **kw)


# ---------------------------------------------------------------- the acceptance scenario (fake desktop)

def test_install_apply_uninstall_leaves_every_file_as_it_was(world: FakeWorld) -> None:
    original = world.snapshot_all()
    s = RecoverySystem(world, [FakeAdapter(world, {"t": [FILE, KEY, OTHER]})])
    s.apply(A, keep=True)
    assert world.snapshot_all() != original  # something was really changed
    fonts, keybinds = FakeFonts(A_FONT, STRAY), FakeKeybinds()
    report = run(s, fonts, keybinds)
    assert world.snapshot_all() == original  # byte-identical
    assert report.reset.skipped == [] and report.reset.failed == [] and len(report.reset.restored) == 3
    assert s.journal.active() is None  # "Original" again
    assert sorted(report.fonts_removed) == sorted([A_FONT, STRAY]) and fonts.keys == []
    assert keybinds.calls == 1 and report.keybinds == "removed"
    assert un.exit_code(report) is ExitCode.OK and report.data == "kept" and report.error is None


def test_uninstall_without_any_apply_is_harmless(world: FakeWorld) -> None:
    original = world.snapshot_all()
    s = RecoverySystem(world, [FakeAdapter(world, {"t": [FILE, KEY, OTHER]})])
    report = run(s, FakeFonts(), FakeKeybinds(False))
    assert world.snapshot_all() == original
    assert report.keybinds == "absent" and un.exit_code(report) is ExitCode.OK
    assert un.format_report(report).splitlines()[-1] == "restored 0 · skipped 0 · failed 0"


# ---------------------------------------------------------------- user changes are never overwritten (R2-9)

def test_a_setting_changed_after_apply_is_skipped_and_its_font_is_kept(system: RecoverySystem, world: FakeWorld) -> None:
    world.external_edit(KEY, "'My Own Font 12'")  # the user changed a managed setting afterwards
    fonts = FakeFonts(A_FONT, STRAY)
    report = run(system, fonts)
    assert KEY in report.reset.skipped
    assert system.adapters["fake"].read_state(KEY).data == b"'My Own Font 12'"  # left exactly as the user set it
    assert report.fonts_kept_referenced == [A_FONT]  # a skipped setting may still name ScribeSense's font
    assert report.fonts_removed == [STRAY] and fonts.keys == [A_FONT]
    assert un.exit_code(report) is ExitCode.OK  # skipped is described, not a failure
    text = un.format_report(report)
    assert KEY in text and un.FONT_FALLBACK_NOTE in text.lower() and "--remove-fonts-anyway" in text
    assert text.splitlines()[-1] == "restored 2 · skipped 1 · failed 0"


def test_remove_fonts_anyway_removes_even_a_font_a_skipped_setting_uses(system: RecoverySystem, world: FakeWorld) -> None:
    world.external_edit(KEY, "'My Own Font 12'")
    fonts = FakeFonts(A_FONT, STRAY)
    report = run(system, fonts, remove_fonts_anyway=True)
    assert sorted(report.fonts_removed) == sorted([A_FONT, STRAY]) and fonts.keys == []
    assert report.fonts_kept_referenced == [] and KEY in report.reset.skipped  # the setting is still left alone


def test_fonts_are_kept_when_their_use_cannot_be_determined(system: RecoverySystem) -> None:
    fonts = FakeFonts(A_FONT, STRAY)
    with patch.object(un, "_referenced_font_keys", side_effect=ScribeSenseError(ErrorCode.STORE_IO_ERROR)):
        report = run(system, fonts)
    assert sorted(report.fonts_kept_unknown) == sorted([A_FONT, STRAY]) and fonts.removed == []
    assert "could not be determined" in un.format_report(report)
    forced = run(system, fonts, remove_fonts_anyway=True)  # the override does not need to know
    assert sorted(forced.fonts_removed) == sorted([A_FONT, STRAY])


# ---------------------------------------------------------------- one thing failing does not stop the rest

def test_a_failed_revert_is_reported_and_the_uninstall_continues(world: FakeWorld) -> None:
    s = RecoverySystem(world, [FakeAdapter(world, {"t": [FILE, KEY, OTHER]})])
    s.apply(A, keep=True)
    failing = FakeAdapter(world, {"t": [FILE, KEY, OTHER]}, fail_on={"revert": ErrorCode.ADAPTER_COMMAND_FAILED})
    s.adapters = {failing.name: failing}
    keybinds = FakeKeybinds()
    report = run(s, FakeFonts(A_FONT, STRAY), keybinds)
    assert report.reset.failed  # every revert failed ...
    assert report.fonts_removed == [STRAY] and report.fonts_kept_referenced == [A_FONT]  # ... the rest went on
    # ... but the login check and recovery keybind stay, so the failed items are retried (O4 review, 2026-10-16)
    assert keybinds.calls == 0 and report.keybinds == "kept_for_recovery"
    assert un.exit_code(report) is ExitCode.FAILED
    assert "scribesense reset" in un.format_report(report)  # offers `reset`


def test_a_crashed_unconfirmed_apply_keeps_its_font_and_the_login_check(world: FakeWorld) -> None:
    # The apply crashed before Keep, so `managed` was never set; its revert then fails. Its font may still be in use.
    s = RecoverySystem(world, [FakeAdapter(world, {"t": [FILE, KEY, OTHER]})])
    s.apply(A, keep=False)
    s.crash()
    failing = FakeAdapter(world, {"t": [FILE, KEY, OTHER]}, fail_on={"revert": ErrorCode.ADAPTER_COMMAND_FAILED})
    s.adapters = {failing.name: failing}
    fonts, keybinds = FakeFonts(A_FONT, STRAY), FakeKeybinds()
    report = run(s, fonts, keybinds)
    assert s.journal.unfinished()  # still to be recovered
    assert report.fonts_kept_referenced == [A_FONT]  # named by the unfinished apply's planned write (R2-9)
    assert report.fonts_removed == [STRAY]  # nothing references this one
    assert keybinds.calls == 0 and report.keybinds == "kept_for_recovery"


@pytest.mark.parametrize("journal_kind", ["memory", "sqlite"])
def test_the_lock_is_held_for_the_whole_uninstall(world: FakeWorld, journal_kind: str) -> None:
    from scribesense.safety.lock import ApplyLock
    from scribesense.safety.recovery import LockPolicy
    from scribesense.safety.testing import make_recovery_system

    s = make_recovery_system(world, [FakeAdapter(world, {"t": [FILE, KEY, OTHER]})], journal=journal_kind)
    s.apply(A, keep=True)
    lock = s.journal._lock if journal_kind == "sqlite" else ApplyLock()  # the journal shares the policy's lock
    free_during: list[bool] = []

    class Probe(FakeFonts):
        def remove(self, key: str) -> bool:
            other = ApplyLock()  # another process trying to start an apply
            free_during.append(other.try_acquire("service"))
            other.release()
            return super().remove(key)

    report = run(s, Probe(A_FONT, STRAY), policy=LockPolicy(lock, "uninstall"))
    assert report.error is None and free_during == [False, False]  # nobody could start an apply meanwhile
    assert not lock.held and not ApplyLock().is_locked()  # released at the end


def test_a_font_that_cannot_be_removed_is_reported_and_the_rest_goes_on(system: RecoverySystem) -> None:
    fonts = FakeFonts(A_FONT, STRAY, fail=(STRAY,))
    keybinds = FakeKeybinds()
    report = run(system, fonts, keybinds)
    assert report.fonts_failed == [STRAY] and report.fonts_removed == [A_FONT]
    assert keybinds.calls == 1 and un.exit_code(report) is ExitCode.FAILED


@pytest.mark.parametrize("raised,outcome,code", [
    (ScribeSenseError(ErrorCode.DRIFT_SKIPPED), "left_edited", ExitCode.OK),  # told to the user, not a failure
    (ScribeSenseError(ErrorCode.ADAPTER_COMMAND_FAILED), "failed", ExitCode.FAILED),
])
def test_keybind_removal_outcomes(system: RecoverySystem, raised: ScribeSenseError, outcome: str, code: ExitCode) -> None:
    report = run(system, keybinds=FakeKeybinds(raised))
    assert report.keybinds == outcome and un.exit_code(report) is code
    assert "hand" in un.format_report(report) or outcome == "failed"


def test_a_busy_journal_changes_nothing_and_exits_busy(world: FakeWorld) -> None:
    s = RecoverySystem(world, [FakeAdapter(world, {"t": [FILE, KEY, OTHER]})])
    s.journal.begin("apply", "default")  # an apply is running: the in-memory lock is held
    original = world.snapshot_all()
    fonts, keybinds = FakeFonts(A_FONT), FakeKeybinds()
    report = run(s, fonts, keybinds)
    assert report.error is ErrorCode.APPLY_IN_PROGRESS and un.exit_code(report) is ExitCode.BUSY
    assert fonts.removed == [] and keybinds.calls == 0 and world.snapshot_all() == original
    assert "Nothing else was changed" in un.format_report(report)


def test_an_unexpected_journal_error_stops_before_anything_else_is_touched(system: RecoverySystem) -> None:
    fonts, keybinds = FakeFonts(A_FONT), FakeKeybinds()
    with patch.object(un, "recovery_reset", side_effect=ScribeSenseError(ErrorCode.STORE_CORRUPT)):
        report = run(system, fonts, keybinds)
    assert report.error is ErrorCode.STORE_CORRUPT and un.exit_code(report) is ExitCode.FAILED
    assert fonts.removed == [] and keybinds.calls == 0  # the recovery hooks stay until the originals are back


# ---------------------------------------------------------------- data is deleted only when asked AND confirmed

@pytest.fixture
def real_store(tmp_path: Path, world: FakeWorld) -> tuple[Store, RecoverySystem]:
    store = Store(tmp_path / "data" / "scribesense.db")
    s = RecoverySystem(world, [FakeAdapter(world, {"t": [FILE, KEY, OTHER]})])
    s.journal = TableJournal(store, store.clock)  # type: ignore[assignment]
    s.apply(A, keep=True)
    values = PresetValues("Noto Sans", 0.12, WordSpacing("multiplier", 1.5), 1.5, 1.2, 1.0)
    store.presets.save(Preset("11111111-1111-4111-8111-111111111111", "Mine", values, "user"))
    return store, s


def test_data_is_kept_by_default(real_store: tuple[Store, RecoverySystem]) -> None:
    store, s = real_store
    report = run(s, store=store)
    assert report.data == "kept" and store.fetchone("SELECT COUNT(*) FROM presets")[0] == 1
    assert store.fetchone("SELECT COUNT(*) FROM transactions")[0] >= 1


def test_deleting_data_without_the_store_is_a_programming_error(system: RecoverySystem) -> None:
    with pytest.raises(ValueError):
        run(system, delete_data=True)


def test_delete_data_asks_first_and_a_no_keeps_everything(real_store: tuple[Store, RecoverySystem]) -> None:
    store, s = real_store
    asked: list[str] = []
    report = run(s, store=store, delete_data=True, confirm=lambda q: asked.append(q) or False)
    assert report.data == "not_confirmed" and store.path.exists()
    assert store.fetchone("SELECT COUNT(*) FROM presets")[0] == 1
    assert "not secure erasure" in asked[0] and "no longer be possible" in asked[0]


def test_without_a_terminal_the_default_answer_is_no(real_store: tuple[Store, RecoverySystem]) -> None:
    store, s = real_store
    report = run(s, store=store, delete_data=True)  # pytest has no interactive stdin
    assert report.data == "not_confirmed" and store.path.exists()


def test_confirmed_delete_removes_the_records_and_the_files(real_store: tuple[Store, RecoverySystem]) -> None:
    store, s = real_store
    log_path().parent.mkdir(parents=True, exist_ok=True)
    log_path().write_text("{}\n")
    report = run(s, store=store, delete_data=True, confirm=lambda q: True)
    assert report.data == "deleted" and un.exit_code(report) is ExitCode.OK
    assert not store.path.exists() and not Path(str(store.path) + "-wal").exists()
    assert not log_path().exists()
    assert "All ScribeSense data was deleted." in un.format_report(report)


def test_when_something_is_unresolved_only_the_preferences_go(real_store: tuple[Store, RecoverySystem], world: FakeWorld) -> None:
    store, s = real_store
    world.external_edit(KEY, "'My Own Font 12'")  # skipped -> unresolved: recovery records must stay
    report = run(s, store=store, delete_data=True, confirm=lambda q: True)
    assert report.data == "preferences_only" and KEY in report.reset.skipped
    assert store.path.exists() and store.fetchone("SELECT COUNT(*) FROM presets")[0] == 0
    assert store.fetchone("SELECT COUNT(*) FROM location_records WHERE unresolved IS NOT NULL")[0] == 1
    assert "recovery records were kept" in un.format_report(report)


def test_the_registry_row_follows_a_removed_font(real_store: tuple[Store, RecoverySystem]) -> None:
    from datetime import datetime, timezone
    from scribesense.contracts.store import FontRegistryEntry

    store, s = real_store
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    store.fonts.create(FontRegistryEntry(STRAY, A, f"AccessSans-{STRAY}", f"AccessSansUI-{STRAY}", ("Regular",),
                                         ("/f/a.ttf",), ("aa",), "validated", None, now, now))
    run(s, FakeFonts(A_FONT, STRAY), store=store)
    assert store.fonts.get(STRAY).status == "removed"  # type: ignore[union-attr]


# ---------------------------------------------------------------- report and exit code

def test_exit_codes_follow_k10() -> None:
    ok = un.UninstallReport()
    assert un.exit_code(ok) is ExitCode.OK
    assert un.exit_code(un.UninstallReport(error=ErrorCode.LOCK_TIMEOUT)) is ExitCode.BUSY
    assert un.exit_code(un.UninstallReport(error=ErrorCode.APPLY_IN_PROGRESS)) is ExitCode.BUSY
    assert un.exit_code(un.UninstallReport(error=ErrorCode.STORE_BUSY)) is ExitCode.FAILED
    assert un.exit_code(un.UninstallReport(fonts_failed=["x"])) is ExitCode.FAILED
    assert ExitCode.SERVICE_UNAVAILABLE not in {un.exit_code(r) for r in (
        ok, un.UninstallReport(error=ErrorCode.SERVICE_UNREACHABLE))}  # never 4 (A2)


def test_run_uninstall_prints_the_report_and_returns_the_code(system: RecoverySystem, capsys: pytest.CaptureFixture[str]) -> None:
    code = un.run_uninstall(system.journal, system.adapters, fonts=FakeFonts(A_FONT), keybind_remover=FakeKeybinds())
    out = capsys.readouterr().out
    assert code is ExitCode.OK and out.rstrip().splitlines()[-1] == "restored 3 · skipped 0 · failed 0"
    assert "Keybinds and the login check were removed." in out


def test_the_log_gets_codes_only(system: RecoverySystem, world: FakeWorld) -> None:
    world.external_edit(KEY, "'My Own Font 12'")
    run(system, keybinds=FakeKeybinds(ScribeSenseError(ErrorCode.DRIFT_SKIPPED)))
    text = log_path().read_text()
    assert "DRIFT_SKIPPED" in text and "My Own Font" not in text and "/fake/home" not in text
