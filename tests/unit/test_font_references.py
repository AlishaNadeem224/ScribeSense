"""K7 rule 3 — which generated fonts are still in use (safety/font_references.py), and remove_unused() on top of it.

Fake desktop (FakeAdapter writes "family=AccessSans-<key>" files / keys), both journals, no real fonts or processes.
"""

from __future__ import annotations

import pytest

from scribesense.adapters.fake import FakeAdapter, FakeWorld
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode
from scribesense.contracts.presets import Preset, PresetValues, WordSpacing
from scribesense.fontgen import generator
from scribesense.safety.font_references import FontReferenceSource, bundled_font_key, font_keys_in
from scribesense.safety.lock import ApplyLock
from scribesense.safety.testing import fake_fontset, make_recovery_system

FILE = "/fake/home/.config/fontconfig/conf.d/99-scribesense.conf"
KEY = "org.gnome.desktop.interface font-name"
A = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.0)
A_KEY = fake_fontset(A).key
PRESET_KEY = "aaaaaaaaaaaa"
LIVE_KEY = "bbbbbbbbbbbb"
STRAY = "0123456789ab"
PRESET = Preset("reading", "Reading", PresetValues("Noto Sans", 0.12, WordSpacing("multiplier", 1.5), 1.8, 1.2, 1.0),
                "builtin")


@pytest.fixture(params=["memory", "sqlite"])
def system(request):  # type: ignore[no-untyped-def]
    world = FakeWorld()
    world.set_file(FILE, None)
    world.set_key(KEY, "'Fira Sans 11'")
    return make_recovery_system(world, [FakeAdapter(world, {"t": [FILE, KEY]})], journal=request.param)


def source(system, *, presets=(), preset_key=lambda _c: None, **kw) -> FontReferenceSource:  # type: ignore[no-untyped-def]
    return FontReferenceSource(system.journal, list(system.adapters.values()), presets=lambda: list(presets),
                               resolve=lambda _values: A, preset_font_key=preset_key, **kw)


def test_font_keys_are_found_only_in_generated_family_names() -> None:
    text = b"AccessSansUI-0123456789ab 11 | AccessSans-fedcba987654 | AccessSans-abcdefabcdef1 | Noto Sans"
    assert font_keys_in(text) == {"0123456789ab", "fedcba987654"}  # 13 hex digits is not a key
    assert font_keys_in(None) == set() and font_keys_in("'Fira Sans 11'") == set()


def test_a_confirmed_apply_is_referenced_by_active_managed_and_live(system) -> None:  # type: ignore[no-untyped-def]
    system.apply(A, keep=True)
    src = source(system)
    assert not src.transaction_active()
    refs = src.references()
    assert refs.complete and refs.keys == {A_KEY}
    # each source on its own finds it too
    assert src._active()[0] == {A_KEY} and src._locations()[0] == {A_KEY} and src._live_settings()[0] == {A_KEY}


def test_a_live_setting_alone_keeps_its_font(system) -> None:  # type: ignore[no-untyped-def]
    # e.g. the user copied our family name into a setting by hand: no journal record names it
    system.world.set_key(KEY, f"'AccessSansUI-{LIVE_KEY} 11'")
    refs = source(system).references()
    assert refs.complete and refs.keys == {LIVE_KEY}


def test_saved_presets_keep_their_fonts(system) -> None:  # type: ignore[no-untyped-def]
    refs = source(system, presets=[PRESET], preset_key=lambda _c: PRESET_KEY).references()
    assert refs.complete and refs.keys == {PRESET_KEY}


def test_an_unfinished_transaction_blocks_the_sweep_and_its_planned_fonts_are_referenced(system) -> None:  # type: ignore[no-untyped-def]
    system.apply(A, keep=False)
    system.crash()  # unconfirmed apply left unfinished
    src = source(system)
    assert src.transaction_active()
    assert A_KEY in src._unfinished()[0]


def test_another_process_holding_the_lock_blocks_the_sweep(system) -> None:  # type: ignore[no-untyped-def]
    other = ApplyLock()
    assert other.try_acquire("reset")
    try:
        assert source(system, lock=ApplyLock()).transaction_active()
    finally:
        other.release()


def test_an_unreadable_source_makes_the_answer_incomplete_but_the_others_still_count(system) -> None:  # type: ignore[no-untyped-def]
    system.apply(A, keep=True)
    broken = FakeAdapter(system.world, {"x": ["/fake/x.conf"]}, name="broken", fail_on={"detect": ErrorCode.ADAPTER_TIMEOUT})
    src = FontReferenceSource(system.journal, [*system.adapters.values(), broken], presets=lambda: [],
                              resolve=lambda _v: A, preset_font_key=lambda _c: None)
    refs = src.references()
    assert not refs.complete and A_KEY in refs.keys

    def boom(_c: Configuration) -> str:
        raise ValueError("preset does not resolve")

    assert not source(system, presets=[PRESET], preset_key=boom).references().complete


def test_bundled_font_key_is_none_while_no_base_font_is_bundled(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from scribesense.config import bundled

    monkeypatch.setattr(bundled, "FONTS_ROOT", tmp_path / "fonts")  # empty: build() would refuse
    assert bundled_font_key(A) is None


# ---------------------------------------------------------------- remove_unused() on top of it

@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """generator's view of the font directory: these keys are installed; remove() deletes from the list."""
    keys = [A_KEY, PRESET_KEY, LIVE_KEY, STRAY]
    monkeypatch.setattr(generator.install_cache, "installed_keys", lambda: list(keys))
    monkeypatch.setattr(generator.install_cache, "remove", lambda key, tools=None: keys.remove(key) is None)
    return keys


def test_remove_unused_removes_only_what_no_source_references(system, installed: list[str]) -> None:  # type: ignore[no-untyped-def]
    system.apply(A, keep=True)
    system.world.set_key(KEY, f"'AccessSansUI-{LIVE_KEY} 11'")  # a user edit after Keep: still in use
    src = source(system, presets=[PRESET], preset_key=lambda _c: PRESET_KEY)
    assert generator.remove_unused(references=src) == [STRAY]
    assert installed == [A_KEY, PRESET_KEY, LIVE_KEY]


def test_remove_unused_removes_nothing_when_usage_cannot_be_determined(system, installed: list[str]) -> None:  # type: ignore[no-untyped-def]
    broken = FakeAdapter(system.world, {"x": ["/fake/x.conf"]}, name="broken", fail_on={"read_state": ErrorCode.ADAPTER_TIMEOUT})
    src = FontReferenceSource(system.journal, [broken], presets=lambda: [], resolve=lambda _v: A,
                              preset_font_key=lambda _c: None)
    assert generator.remove_unused(references=src) == [] and len(installed) == 4


def test_remove_unused_waits_while_a_transaction_is_open(system, installed: list[str]) -> None:  # type: ignore[no-untyped-def]
    system.journal.begin("apply", None)
    assert generator.remove_unused(references=source(system)) == [] and len(installed) == 4
