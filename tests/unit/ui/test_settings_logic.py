"""M2.7 — the Settings screen's GTK-free logic (ui/settings_logic.py, O3's M2.7 note 4).

Runs where PyGObject / GTK are not installed: nothing here imports `gi`, and the first two tests prove that
settings_logic never does (statically over everything it loads, and by importing it with `gi` blocked).
External effects are faked: the store, the journal and the Hyprland keybind writer.
"""

from __future__ import annotations

import ast
import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.journal import NoRecoveryObligations
from scribesense.contracts.store import SETTINGS
from scribesense.safety import keybinds as kb
from scribesense.ui import settings_logic as sl

SRC = Path(__file__).resolve().parents[3] / "src"


# ---------------------------------------------------------------- no gi

def _module_level_imports(name: str) -> tuple[set[str], set[str]]:
    """(imports executed when `name` is imported, every import anywhere in the file)."""
    path = SRC.joinpath(*name.split("."))
    path = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
    tree = ast.parse(path.read_text())
    top: set[str] = set()
    every: set[str] = set()
    for node in ast.walk(tree):
        found: list[str] = []
        if isinstance(node, ast.Import):
            found = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        every.update(found)
        if node in tree.body:
            top.update(found)
    return top, every


def test_settings_logic_never_reaches_gi() -> None:
    _top, own = _module_level_imports("scribesense.ui.settings_logic")
    assert not {i for i in own if i == "gi" or i.startswith("gi.")}
    seen: set[str] = set()
    stack = ["scribesense.ui.settings_logic"]
    reached_gi: set[str] = set()
    while stack:
        name = stack.pop()
        if name in seen or not name.startswith("scribesense"):
            continue
        path = SRC.joinpath(*name.split("."))
        if not (path.is_dir() or path.with_suffix(".py").exists()):
            continue  # `from pkg import name` where name is not a module
        seen.add(name)
        top, _every = _module_level_imports(name)
        reached_gi |= {f"{name}: {i}" for i in top if i == "gi" or i.startswith("gi.")}
        stack.extend(top)
        stack.extend(".".join(name.split(".")[:k]) for k in range(1, name.count(".") + 1))  # parent packages
    assert reached_gi == set()
    assert "scribesense.controller.service" in seen and "scribesense.safety.keybinds" in seen  # the walk ran


def test_settings_logic_imports_with_gi_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    import scribesense.ui

    monkeypatch.setitem(sys.modules, "gi", None)  # `import gi` -> ImportError
    monkeypatch.delitem(sys.modules, "scribesense.ui.settings_logic")
    monkeypatch.setattr(scribesense.ui, "settings_logic", sl)  # restored afterwards (the import rebinds it)
    fresh = importlib.import_module("scribesense.ui.settings_logic")
    assert fresh.validate_keybinds is not sl.validate_keybinds  # really re-executed


# ---------------------------------------------------------------- keybinds

def test_validate_keybinds_normalizes_valid_input() -> None:
    normalized, errors = sl.validate_keybinds({
        "keybind_reader": "super+alt, R", "keybind_preset_next": "ALT SUPER, P",
        "keybind_reset": "CTRL ALT SHIFT SUPER, BackSpace"})
    assert errors == {}
    assert normalized == {"keybind_reader": "SUPER ALT, R", "keybind_preset_next": "SUPER ALT, P",
                          "keybind_reset": "SUPER CTRL ALT SHIFT, BackSpace"}


@pytest.mark.parametrize("reset_text", ["", "   ", "CTRL ALT SHIFT SUPER BackSpace", "SUPER, a b",
                                        "SUPER, R; exec rm", "NOPE, R"])
def test_validate_keybinds_refuses_invalid_or_empty(reset_text: str) -> None:
    normalized, errors = sl.validate_keybinds({
        "keybind_reader": "SUPER ALT, R", "keybind_preset_next": "SUPER ALT, P", "keybind_reset": reset_text})
    assert normalized == {} and set(errors) == {"keybind_reset"}


def test_validate_keybinds_missing_key_is_an_error() -> None:
    normalized, errors = sl.validate_keybinds({"keybind_reader": "SUPER ALT, R"})
    assert normalized == {} and set(errors) == {"keybind_preset_next", "keybind_reset"}


def test_validate_keybinds_refuses_two_actions_on_one_combo() -> None:
    normalized, errors = sl.validate_keybinds({
        "keybind_reader": "SUPER ALT, R", "keybind_preset_next": "alt super, r",
        "keybind_reset": "CTRL ALT SHIFT SUPER, BackSpace"})
    assert normalized == {} and set(errors) == {"keybind_preset_next"}
    assert "Open selection in reader" in errors["keybind_preset_next"]


def test_describe_conflict_names_our_action_their_keys_and_what_they_do() -> None:
    ours = kb.parse_keybind("SUPER ALT, R", "reader")
    text = sl.describe_conflict(kb.Conflict(ours, kb.LiveBind(72, "R", "exec", "", "Launch terminal")))
    assert text == "“Open selection in reader” uses SUPER ALT, R, which Hyprland already uses for: Launch terminal."
    no_description = sl.describe_conflict(kb.Conflict(ours, kb.LiveBind(72, "R", "killactive", "")))
    assert no_description.endswith("for: killactive.")
    nothing = sl.describe_conflict(kb.Conflict(ours, kb.LiveBind(72, "R", "", "")))
    assert nothing.endswith("for: another action.")


def _saved(key: str) -> object:
    return SETTINGS[key][1]


def test_stored_binds_reads_every_action_and_refuses_a_bad_value() -> None:
    assert [b.action for b in sl.stored_binds(_saved)] == list(kb.ACTIONS)
    with pytest.raises(ValueError):
        sl.stored_binds(lambda _k: "garbage")


def test_install_keybinds_refuses_and_shows_the_conflicting_bind() -> None:
    def find(binds: Any) -> list[kb.Conflict]:
        return [kb.Conflict(binds[0], kb.LiveBind(72, "R", "exec", "", "Launch terminal"))]

    def install(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("must not install on conflict")

    text = sl.install_stored_keybinds(_saved, install=install, find_conflicts=find, command="/usr/bin/scribesense")
    assert text.startswith("Not applied.") and "SUPER ALT, R" in text and "Launch terminal" in text


@pytest.mark.parametrize("reload, expected", [("reloaded", "applied to Hyprland"), ("skipped", "next login")])
def test_install_keybinds_success(reload: str, expected: str) -> None:
    got: list[Any] = []

    def install(binds: Any, command: str) -> Any:
        got.append(([b.action for b in binds], command))
        return SimpleNamespace(reload=reload)

    text = sl.install_stored_keybinds(_saved, install=install, find_conflicts=lambda b: [],
                                      command="/usr/bin/scribesense")
    assert expected in text
    assert got == [(["reader", "preset_next", "reset"], "/usr/bin/scribesense")]


@pytest.mark.parametrize("code", [ErrorCode.KEYBIND_CONFLICT, ErrorCode.CHECK_UNAVAILABLE,
                                  ErrorCode.ADAPTER_UNAVAILABLE, ErrorCode.DRIFT_SKIPPED, ErrorCode.NOT_WRITABLE])
def test_install_keybinds_reports_errors_as_messages(code: ErrorCode) -> None:
    def install(*_a: Any, **_k: Any) -> Any:
        raise ScribeSenseError(code)

    text = sl.install_stored_keybinds(_saved, install=install, find_conflicts=lambda b: [],
                                      command="/usr/bin/scribesense")
    assert "not applied" in text.lower()
    if code is not ErrorCode.KEYBIND_CONFLICT:
        assert code.value in text


def test_install_keybinds_conflict_check_failure_is_a_message() -> None:
    def find(_binds: Any) -> list[kb.Conflict]:
        raise ScribeSenseError(ErrorCode.CHECK_UNAVAILABLE)

    text = sl.install_stored_keybinds(_saved, find_conflicts=find, command="/usr/bin/scribesense")
    assert "CHECK_UNAVAILABLE" in text


def test_install_keybinds_value_error_from_the_writer_is_a_message() -> None:
    def install(*_a: Any, **_k: Any) -> Any:
        raise ValueError("bad command path")

    text = sl.install_stored_keybinds(_saved, install=install, find_conflicts=lambda b: [], command="/x")
    assert "not valid" in text and "bad command path" not in text


def test_install_keybinds_without_command_or_with_bad_saved_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sl.shutil, "which", lambda _n: None)
    assert "not found" in sl.install_stored_keybinds(_saved)
    text = sl.install_stored_keybinds(lambda k: "garbage", command="/usr/bin/scribesense")
    assert "not valid" in text


# ---------------------------------------------------------------- texts

def test_error_text_shows_the_code_never_exception_text() -> None:
    assert sl.error_text(ScribeSenseError(ErrorCode.STORE_BUSY)) == "Something went wrong (STORE_BUSY)."
    assert "secret" not in sl.error_text(RuntimeError("/home/user/secret.txt"))


def test_delete_dialog_texts_match_what_happens() -> None:
    assert "reset" in sl.DELETE_FIRST_BODY.lower()
    assert sl.RESET_COMMAND == "scribesense reset"
    assert sl.RESET_COMMAND in sl.DELETE_SKIP_BODY  # kept records -> reset still possible
    assert "original settings" in sl.DELETE_FINAL_BODY
    partial = sl.delete_partial_body(["gtk:font-name"])
    assert "gtk:font-name" in partial and "kept" in partial and sl.RESET_COMMAND in partial
    assert "not fully back" in partial
    many = sl.delete_partial_body([f"loc{i}" for i in range(8)])
    assert "loc4" in many and "loc5" not in many and "3 more" in many
    assert "could not be confirmed" in sl.delete_partial_body([])
    assert sl.RESET_COMMAND in sl.delete_result_text(False) and "kept" in sl.delete_result_text(False)
    assert sl.delete_result_text(True) == "All ScribeSense data was deleted."


# ---------------------------------------------------------------- delete_all_data (store steps)

@dataclass
class _FakeStore:
    refuse: ErrorCode | None = None
    calls: list[Any] = field(default_factory=list)

    def delete_preferences(self) -> None:
        self.calls.append("prefs")

    def delete_recovery_data(self, *, precondition: Any) -> None:
        self.calls.append(("recovery", precondition))
        if self.refuse is not None:
            raise ScribeSenseError(self.refuse)


def test_delete_all_data_deletes_recovery_records_only_with_the_token() -> None:
    store, token = _FakeStore(), NoRecoveryObligations()
    assert sl.delete_all_data(store, SimpleNamespace(no_obligations=lambda: token)) is True
    assert store.calls == ["prefs", ("recovery", token)]
    store = _FakeStore()
    assert sl.delete_all_data(store, SimpleNamespace(no_obligations=lambda: None)) is False
    assert store.calls == ["prefs"]  # recovery records kept while something is left to restore


def test_delete_all_data_store_refusing_the_token_means_records_kept() -> None:
    """O3's M2.7 note 3: RECOVERY_OBLIGATIONS_REMAIN is "recovery kept", never a generic error."""
    store = _FakeStore(refuse=ErrorCode.RECOVERY_OBLIGATIONS_REMAIN)
    assert sl.delete_all_data(store, SimpleNamespace(no_obligations=NoRecoveryObligations)) is False


def test_delete_all_data_other_store_errors_propagate() -> None:
    store = _FakeStore(refuse=ErrorCode.STORE_IO_ERROR)
    with pytest.raises(ScribeSenseError) as exc:
        sl.delete_all_data(store, SimpleNamespace(no_obligations=NoRecoveryObligations))
    assert exc.value.code is ErrorCode.STORE_IO_ERROR


def test_window_deletes_through_the_service_under_its_lock() -> None:
    """O3's M2.7 note 3: ui/window.py calls `service.delete_all_data()` (mutex + apply lock), never the bare store
    steps. Checked on the source, so it runs without GTK."""
    tree = ast.parse((SRC / "scribesense" / "ui" / "window.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    through_service = [c for c in calls if isinstance(c.func, ast.Attribute) and c.func.attr == "delete_all_data"
                       and isinstance(c.func.value, ast.Name) and c.func.value.id == "service" and not c.args]
    bare = [c for c in calls if isinstance(c.func, ast.Name) and c.func.id in ("delete_all_data", "delete_store_data")]
    assert len(through_service) == 1 and bare == []
