"""M3.1 — repositories: round trips, read-only built-ins, settings, font registry, samples (K9 tests 1–6, 12)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scribesense.config.presets import BUILTINS, resolve
from scribesense.contracts.adapter import Target
from scribesense.contracts.config import Configuration
from scribesense.contracts.coverage import CoverageEntry, CoverageStatus
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.presets import Preset, PresetValues, WordSpacing
from scribesense.contracts.store import SETTINGS, FontRegistryEntry
from scribesense.store import JournalTables, Store
from scribesense.store import serialize as ser

CANARY = "CANARY-reader-text-7f3a"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "db" / "scribesense.db")
    s.clock = lambda: NOW
    return s


def values(**kw) -> PresetValues:  # type: ignore[no-untyped-def]
    base = dict(base_family="Noto Sans", letter_em=0.12, word=WordSpacing("target_em", 0.16), reading_line=1.5,
                ui_line=1.2, text_scale=1.0)
    return PresetValues(**{**base, **kw})


def user_preset(pid: str = "11111111-1111-4111-8111-111111111111", name: str = "Mine", **kw) -> Preset:  # type: ignore[no-untyped-def]
    return Preset(pid, name, values(**kw), "user")


def config(**kw) -> Configuration:  # type: ignore[no-untyped-def]
    base = dict(base_family="Noto Sans", letter_em=0.12, word_scale=1.75, reading_line=1.5, ui_line=1.2, text_scale=1.0)
    return Configuration(**{**base, **kw})


def entry(key: str = "abc123", status: str = "validated", cfg: Configuration | None = None, **kw) -> FontRegistryEntry:  # type: ignore[no-untyped-def]
    base = dict(key=key, config=cfg or config(), reading_family=f"AccessSans-{key}", ui_family=f"AccessSansUI-{key}",
                styles=("Regular", "Bold"), files=("/f/a.ttf", "/f/b.ttf"), file_sha256=("aa", "bb"), status=status,
                reason_code=None, created_at=NOW, last_used_at=NOW)
    return FontRegistryEntry(**{**base, **kw})


# ---------------------------------------------------------------- serialization (D9)

def test_numbers_are_fixed_decimal_strings_and_round_trip_exactly() -> None:
    c = config(letter_em=0.12, word_scale=1.5, reading_line=1.8, ui_line=1.2, text_scale=1.05)
    text = ser.config_to_json(c)
    obj = json.loads(text)
    assert obj["v"] == 1
    assert (obj["letter_em"], obj["word_scale"], obj["reading_line"], obj["text_scale"]) == ("0.12", "1.50", "1.8", "1.05")
    assert ser.config_from_json(text) == c


def test_equal_values_have_equal_text() -> None:
    assert ser.config_to_json(config(word_scale=1.5)) == ser.config_to_json(config(word_scale=1.50))
    assert ser.config_to_json(config(letter_em=0.1 + 0.02)) == ser.config_to_json(config(letter_em=0.12))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), None, "0.12", True])
def test_values_that_are_not_real_numbers_cannot_be_stored(bad: object) -> None:
    with pytest.raises(ValueError):
        ser.config_to_json(config(letter_em=bad))  # type: ignore[arg-type]


@pytest.mark.parametrize("text", ["", "not json", "[]", '{"v":2}', '{"v":1}',
                                  json.dumps({"v": 1, "base_family": "Noto Sans", "letter_em": "x"})])
def test_corrupt_stored_json_is_reported_as_store_corrupt(text: str) -> None:
    with pytest.raises(ScribeSenseError) as err:
        ser.config_from_json(text)
    assert err.value.code is ErrorCode.STORE_CORRUPT


def test_timestamps_are_utc_and_must_carry_a_zone() -> None:
    plus2 = datetime(2026, 10, 15, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    assert ser.ts_to_text(plus2) == "2026-10-15T12:00:00+00:00"
    assert ser.ts_from_text("2026-10-15T14:00:00+02:00") == NOW
    with pytest.raises(ValueError):
        ser.ts_to_text(datetime(2026, 10, 15))
    with pytest.raises(ScribeSenseError):
        ser.ts_from_text("2026-10-15T12:00:00")


# ---------------------------------------------------------------- presets (tests 1, 2)

def test_builtins_come_first_and_are_not_stored(store: Store) -> None:
    store.presets.save(user_preset(name="Zed"))
    store.presets.save(user_preset("22222222-2222-4222-8222-222222222222", name="alpha"))
    listed = store.presets.list()
    assert [p.id for p in listed[:3]] == ["default", "reading", "high-separation"]
    assert [p.name for p in listed[3:]] == ["alpha", "Zed"]
    assert store.fetchone("SELECT COUNT(*) FROM presets WHERE kind NOT IN ('user','custom')")[0] == 0
    assert store.presets.get("reading") == BUILTINS[1]


def test_user_preset_round_trips_with_its_word_mode(store: Store) -> None:
    p = user_preset(word=WordSpacing("target_em", 0.044), text_scale=1.25)
    store.presets.save(p)
    assert store.presets.get(p.id) == p
    p2 = replace(p, name="Renamed", values=values(word=WordSpacing("multiplier", 2.5)))
    store.presets.save(p2)  # same id -> update
    assert store.presets.get(p.id) == p2
    assert store.fetchone("SELECT COUNT(*) FROM presets")[0] == 1


def test_the_custom_row_is_stored_once_with_the_reserved_id(store: Store) -> None:
    custom = Preset("custom", "Custom", values(), "custom")
    store.presets.save(custom)
    store.presets.save(replace(custom, values=values(letter_em=0.2)))
    assert store.presets.get("custom").values.letter_em == 0.2  # type: ignore[union-attr]
    assert store.fetchone("SELECT COUNT(*) FROM presets")[0] == 1
    store.presets.save(user_preset(name="AAA"))
    assert [p.id for p in store.presets.list()][3] == "custom"  # right after the built-ins
    assert store.presets.delete("custom") is True and store.presets.get("custom") is None


def test_builtins_cannot_be_saved_or_deleted(store: Store) -> None:
    for preset in (BUILTINS[0], replace(BUILTINS[0], kind="user"), Preset("reading", "x", values(), "user")):
        with pytest.raises(ScribeSenseError) as err:
            store.presets.save(preset)
        assert err.value.code is ErrorCode.PRESET_READ_ONLY
    for pid in ("default", "reading", "high-separation"):
        with pytest.raises(ScribeSenseError) as err:
            store.presets.delete(pid)
        assert err.value.code is ErrorCode.PRESET_READ_ONLY
    assert store.fetchone("SELECT COUNT(*) FROM presets")[0] == 0


def test_original_is_never_a_preset_and_reserved_ids_are_refused(store: Store) -> None:
    assert store.presets.get("original") is None
    assert store.presets.delete("original") is False
    for bad in (Preset("original", "x", values(), "user"), Preset("custom", "x", values(), "user"),
                Preset("not-custom", "x", values(), "custom"), Preset("abc", "x", values(), "weird"),  # type: ignore[arg-type]
                Preset("abc", "", values(), "user")):
        with pytest.raises(ValueError):
            store.presets.save(bad)
    assert store.fetchone("SELECT COUNT(*) FROM presets")[0] == 0


def test_delete_reports_whether_anything_was_removed(store: Store) -> None:
    p = user_preset()
    store.presets.save(p)
    assert store.presets.delete(p.id) is True
    assert store.presets.delete(p.id) is False


def test_a_corrupt_preset_row_is_reported_not_returned(store: Store) -> None:
    store.presets.save(user_preset())
    store.execute("UPDATE presets SET data = '{\"v\":1}'")
    with pytest.raises(ScribeSenseError) as err:
        store.presets.list()
    assert err.value.code is ErrorCode.STORE_CORRUPT


# ---------------------------------------------------------------- settings (test 3)

def test_settings_default_and_round_trip(store: Store) -> None:
    for name, (_t, default) in SETTINGS.items():
        assert store.settings.get(name) == default
    store.settings.set("keybind_reader", "SUPER ALT, X")
    store.settings.set("lock_wait_seconds", 600)
    store.settings.set("recommend_opt_in", True)
    assert store.settings.get("keybind_reader") == "SUPER ALT, X"
    assert store.settings.get("lock_wait_seconds") == 600
    assert store.settings.get("recommend_opt_in") is True


def test_settings_reject_unknown_names_wrong_types_and_out_of_range(store: Store) -> None:
    with pytest.raises(KeyError):
        store.settings.get("nope")
    with pytest.raises(KeyError):
        store.settings.set("nope", 1)
    with pytest.raises(TypeError):
        store.settings.set("lock_wait_seconds", True)  # True is not an int
    with pytest.raises(TypeError):
        store.settings.set("lock_wait_seconds", 60.0)
    with pytest.raises(TypeError):
        store.settings.set("recommend_opt_in", 1)
    with pytest.raises(TypeError):
        store.settings.set("keybind_reader", 5)
    for out in (9, 601, -1):
        with pytest.raises(ValueError):
            store.settings.set("lock_wait_seconds", out)
    assert store.settings.get("lock_wait_seconds") == 60  # none of the rejected values was kept


def test_a_stored_setting_that_breaks_its_rule_is_corrupt(store: Store) -> None:
    store.execute("INSERT INTO settings VALUES ('lock_wait_seconds', '5')")
    with pytest.raises(ScribeSenseError) as err:
        store.settings.get("lock_wait_seconds")
    assert err.value.code is ErrorCode.STORE_CORRUPT


# ---------------------------------------------------------------- font registry (tests 1, 4, 5, 6)

def test_font_entry_round_trips_with_utc_timestamps(store: Store) -> None:
    e = entry()
    store.fonts.create(e)
    assert store.fonts.get("abc123") == e
    assert store.fonts.get("zzz") is None
    assert store.fonts.keys() == ["abc123"]


def test_same_key_different_files_is_an_identity_conflict(store: Store) -> None:
    store.fonts.create(entry())
    for other in (entry(files=("/f/other.ttf", "/f/b.ttf")), entry(file_sha256=("aa", "XX")),
                  entry(styles=("Regular",)), entry(cfg=config(reading_line=1.6)),
                  entry(cfg=config(word_scale=2.0)), entry(reading_family="AccessSans-other")):
        with pytest.raises(ScribeSenseError) as err:
            store.fonts.create(other)
        assert err.value.code is ErrorCode.FONT_IDENTITY_CONFLICT
    assert store.fonts.get("abc123") == entry()  # untouched


def test_identity_ignores_text_scale_because_the_key_does(store: Store) -> None:  # decision D8
    store.fonts.create(entry(cfg=config(text_scale=1.0)))
    store.fonts.create(entry(cfg=config(text_scale=1.5)))  # the same font set: no conflict, nothing changes
    assert store.fonts.get("abc123").config.text_scale == 1.0  # type: ignore[union-attr]
    assert store.fonts.keys() == ["abc123"]


def test_create_validates_its_input(store: Store) -> None:
    with pytest.raises(ValueError):
        store.fonts.create(entry(status="weird"))
    with pytest.raises(ValueError):
        store.fonts.create(entry(file_sha256=("aa",)))
    with pytest.raises(ValueError):
        store.fonts.create(entry(created_at=datetime(2026, 1, 1)))  # naive timestamp


def test_font_status_flow(store: Store) -> None:
    store.fonts.create(entry(status="building"))
    store.fonts.set_status("abc123", "failed", "VALIDATION_FAILED")
    assert store.fonts.get("abc123").reason_code == "VALIDATION_FAILED"  # type: ignore[union-attr]
    store.fonts.set_status("abc123", "building", None)  # retry
    store.fonts.set_status("abc123", "validated", None)
    for bad in ("building", "failed", "validated"):
        with pytest.raises(ValueError):
            store.fonts.set_status("abc123", bad, None)
    store.fonts.set_status("abc123", "removed", None)
    with pytest.raises(ValueError):
        store.fonts.set_status("abc123", "validated", None)  # removed is final
    with pytest.raises(KeyError):
        store.fonts.set_status("missing", "failed", None)


def test_keys_can_be_filtered_by_status_and_touch_updates_last_used(store: Store) -> None:
    store.fonts.create(entry("a", "building"))
    store.fonts.create(entry("b", "validated"))
    assert store.fonts.keys("validated") == ["b"] and store.fonts.keys("building") == ["a"]
    later = NOW + timedelta(hours=1)
    store.clock = lambda: later
    store.fonts.touch("b")
    assert store.fonts.get("b").last_used_at == later  # type: ignore[union-attr]
    with pytest.raises(KeyError):
        store.fonts.touch("missing")


def test_a_build_left_in_progress_becomes_failed_interrupted(store: Store) -> None:
    store.fonts.create(entry("a", "building"))
    store.fonts.create(entry("b", "validated"))
    assert store.recover_interrupted_builds() == 1
    a = store.fonts.get("a")
    assert (a.status, a.reason_code) == ("failed", "INTERRUPTED_BUILD")  # type: ignore[union-attr]
    assert store.fonts.get("b").status == "validated"  # type: ignore[union-attr]
    assert store.recover_interrupted_builds() == 0


def test_opening_the_store_does_not_fail_a_build_that_is_running(tmp_path: Path) -> None:
    path = tmp_path / "db" / "s.db"
    Store(path).fonts.create(entry("a", "building"))
    assert Store(path).fonts.get("a").status == "building"  # a CLI process must not fail the service's build


def _font_for(store: Store, key: str = "abc123", **kw) -> FontRegistryEntry:  # type: ignore[no-untyped-def]
    e = entry(key, **kw)
    store.fonts.create(e)
    return e


def test_unused_font_can_be_marked_removed_and_marking_twice_is_harmless(store: Store) -> None:
    _font_for(store, cfg=config(letter_em=0.06))
    store.fonts.mark_removed("abc123")
    assert store.fonts.get("abc123").status == "removed"  # type: ignore[union-attr]
    store.fonts.mark_removed("abc123")
    with pytest.raises(KeyError):
        store.fonts.mark_removed("missing")


def test_font_in_use_by_the_active_configuration(store: Store) -> None:
    _font_for(store, cfg=config(letter_em=0.06))
    store.execute("UPDATE active_config SET config = ?, font_key = 'abc123'", (ser.config_to_json(config()),))
    with pytest.raises(ScribeSenseError) as err:
        store.fonts.mark_removed("abc123")
    assert err.value.code is ErrorCode.FONT_IN_USE


def test_font_in_use_by_a_managed_location(store: Store) -> None:
    _font_for(store, cfg=config(letter_em=0.06))
    store.execute("INSERT INTO location_records (location,target_adapter,target_id,target_display_name,kind,existed,"
                  "old_sha256,managed_sha256,managed_font_key) VALUES ('/x','a','t','d','file',1,'h','m','abc123')")
    with pytest.raises(ScribeSenseError) as err:
        store.fonts.mark_removed("abc123")
    assert err.value.code is ErrorCode.FONT_IN_USE


def test_font_in_use_by_a_builtin_or_a_saved_preset(store: Store) -> None:
    default_cfg = resolve(BUILTINS[0].values)
    _font_for(store, "built", cfg=default_cfg)
    with pytest.raises(ScribeSenseError) as err:
        store.fonts.mark_removed("built")  # the Default preset resolves to exactly this font
    assert err.value.code is ErrorCode.FONT_IN_USE
    mine = user_preset(letter_em=0.08, word=WordSpacing("multiplier", 2.0))
    _font_for(store, "mine", cfg=resolve(mine.values))
    store.fonts.mark_removed("mine")  # before the preset is saved: free to go
    _font_for(store, "mine2", cfg=resolve(mine.values), reading_family="AccessSans-mine2")
    store.presets.save(mine)
    with pytest.raises(ScribeSenseError):
        store.fonts.mark_removed("mine2")
    store.presets.delete(mine.id)
    store.fonts.mark_removed("mine2")  # the preset is gone: allowed


def test_font_in_use_by_an_unfinished_transaction(store: Store) -> None:
    from scribesense.contracts.adapter import PlannedWrite, SnapshotItem
    from scribesense.contracts.states import TxState

    _font_for(store, cfg=config(letter_em=0.06))
    j = JournalTables(store)
    tx = j.create_tx("apply", None)
    target = Target("fontconfig", "fontconfig", "fontconfig", False)
    item = j.add_item(tx, SnapshotItem(target, "file", "/c/rule.conf", False, None, "h", None, None), None)
    j.set_intent(tx, item, PlannedWrite(target, "/c/rule.conf", b"<alias>AccessSansUI-abc123</alias>", "x"))
    with pytest.raises(ScribeSenseError) as err:
        store.fonts.mark_removed("abc123")
    assert err.value.code is ErrorCode.FONT_IN_USE
    j.set_tx_state(tx, TxState.FAILED_CLEAN)  # finished: no longer a reason to keep the font
    store.fonts.mark_removed("abc123")


# ---------------------------------------------------------------- coverage is read-only

def test_coverage_repo_has_no_write_methods_and_reads_the_newest_completed_apply(store: Store) -> None:
    assert not [n for n in dir(store.coverage) if not n.startswith("_") and n != "last_completed_apply"]
    assert store.coverage.last_completed_apply() == []
    j = JournalTables(store)
    t = Target("gtk", "gtk:desktop", "GTK apps", True)
    tx = j.create_tx("apply", None)
    j.add_coverage(tx, CoverageEntry(t, CoverageStatus.COVERED, "OK"))
    assert store.coverage.last_completed_apply() == []  # not completed yet
    store.execute("UPDATE transactions SET state = 'COMPLETED'")
    [got] = store.coverage.last_completed_apply()
    assert got == CoverageEntry(replace(t, running=False), CoverageStatus.COVERED, "OK")  # `running` is not stored


# ---------------------------------------------------------------- samples and privacy (test 12)

def test_samples_need_the_opt_in_and_it_is_re_read_on_every_call(store: Store) -> None:
    with pytest.raises(ScribeSenseError) as err:
        store.samples.add(config())
    assert err.value.code is ErrorCode.SAMPLES_DISABLED
    store.settings.set("recommend_opt_in", True)
    store.samples.add(config())
    store.settings.set("recommend_opt_in", False)
    with pytest.raises(ScribeSenseError):
        store.samples.add(config())


def test_opting_out_deletes_existing_samples(store: Store) -> None:
    store.settings.set("recommend_opt_in", True)
    store.samples.add(config())
    store.samples.add(config(letter_em=0.2))
    assert store.fetchone("SELECT COUNT(*) FROM recommendation_samples")[0] == 2
    store.settings.set("recommend_opt_in", False)
    assert store.fetchone("SELECT COUNT(*) FROM recommendation_samples")[0] == 0


def test_delete_all_samples(store: Store) -> None:
    store.settings.set("recommend_opt_in", True)
    store.samples.add(config())
    store.samples.delete_all()
    assert store.fetchone("SELECT COUNT(*) FROM recommendation_samples")[0] == 0


def _every_text_cell(store: Store) -> list[str]:
    cells: list[str] = []
    for (table,) in store.fetchall("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        for row in store.fetchall(f"SELECT * FROM {table}"):
            cells += [v if isinstance(v, str) else bytes(v).decode("latin-1") for v in tuple(row)
                      if isinstance(v, (str, bytes))]
    return cells


def test_no_reader_text_ends_up_in_any_table_or_in_the_file(store: Store, tmp_path: Path) -> None:
    # The canary is "reader text": the store has no API that accepts it, so it must appear nowhere after normal use.
    store.settings.set("recommend_opt_in", True)
    store.samples.add(config())
    store.presets.save(user_preset())
    store.fonts.create(entry())
    j = JournalTables(store)
    tx = j.create_tx("apply", "default")
    from scribesense.contracts.adapter import SnapshotItem

    t = Target("gtk", "gtk:desktop", "GTK apps", True)
    j.add_item(tx, SnapshotItem(t, "key", "org.gnome.desktop.interface font-name", True, "'Sans 11'", "h", None, "s"), None)
    cells = _every_text_cell(store)
    assert cells and not any(CANARY in c for c in cells)
    store.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert CANARY.encode() not in (tmp_path / "db" / "scribesense.db").read_bytes()


def test_the_sweep_itself_would_find_a_canary(store: Store, tmp_path: Path) -> None:
    store.execute("INSERT INTO settings VALUES ('keybind_reader', ?)", (json.dumps(CANARY),))
    assert any(CANARY in c for c in _every_text_cell(store))  # the check above is not blind
    store.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert CANARY.encode() in (tmp_path / "db" / "scribesense.db").read_bytes()
