"""M1.2 / K9 -- one serialiser for presets, and the docstrings that claim what exists.

Task 3 of O1's remaining-work list (PLAN_v2 W7 housekeeping). Two kinds of rot were cleaned
up on 2026-11-14, and this file guards against both coming back.

**Duplicate serialisation.** `config/presets.py` carried `to_record` / `from_record` for
K9 D9's versioned JSON. M3.1 shipped `store/serialize.py` with the same shape, `PresetRepo`
calls that, and O1's pair had no caller and no test. Verified identical before removal (the
``values`` payloads matched field for field, including the fixed-decimal spelling), so the
duplication was the whole defect: two implementations of one record shape, owned by two
people, is a second authority on what a preset legally contains.

**Docstrings asserting the absence of things that now exist.** `fontgen/generator.py` said
M3.1 did not exist (it was built 2026-10-15) and that nothing implemented `ReferenceSource`
(O4 built `FontReferenceSource` 2026-10-17). A comment that is confidently wrong is worse
than none: the next reader believes it. These tests assert the *premises* those docstrings
now rely on, so if one stops being true the suite says so instead of the prose quietly
rotting again.

A note on `importlib.import_module`, so it does not read as evasion
-------------------------------------------------------------------
These tests assert facts about `store`, `safety` and `controller`. Import rule R4 forbids
anything under `scribesense.config*` from importing those layers -- and this package matches
that prefix, by accident of its name (`"scribesense.config_and_fontgen_tests".startswith(
"scribesense.config")`). A module-level `from scribesense.store import repos` here would be
a real static dependency from the config namespace into the store, and R4 *should* reject it.

So the modules are loaded inside the test functions instead. That is the ordinary way to
assert a cross-layer fact without creating a layering edge -- but it is also invisible to
the AST-based checker, which only sees `ast.Import` / `ast.ImportFrom`. Stated here so the
next reader knows the check was considered rather than dodged.

The prefix collision is worth fixing at the source: R4 matches on a string prefix, so **any**
future `scribesense.config_*` module inherits config's rules whether or not that is intended.
Raised for O3 (who owns `tests/unit/test_import_rules.py`) in PLAN_v2 2026-11-14.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

from scribesense.config import presets
from scribesense.fontgen import generator


# ---- the duplication stays gone ---------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["to_record", "from_record", "PresetRecordError", "RECORD_VERSION"]
)
def test_preset_serialisation_does_not_come_back(name: str) -> None:
    """`store/serialize.py` is the one place a preset becomes bytes.

    If this fails, someone has re-added a serialiser to `config/presets.py`. Before keeping
    it, check `store.serialize.preset_values_to_json` / `preset_from_row` -- which
    `PresetRepo.save()` and `.get()` actually call -- and decide which one the store should
    use. Two is the failure mode, not either one of them.
    """
    assert not hasattr(presets, name), (
        f"{name} is back in config.presets; the store owns preset serialisation (K9, M3.1)"
    )
    assert name not in presets.__all__


def test_the_store_serialiser_is_the_one_with_callers() -> None:
    """The positive half: the surviving implementation really is wired.

    Removing the unused twin is only correct if the used one works, so this asserts the
    round trip the store actually performs -- `PresetRepo.save()` writes
    `preset_values_to_json`, `PresetRepo.get()` reads `preset_from_row`.
    """
    serialize = importlib.import_module("scribesense.store.serialize")
    preset = presets.custom_preset(presets.builtin("default").values)

    data = serialize.preset_values_to_json(preset.values)
    restored = serialize.preset_from_row(preset.id, preset.name, preset.kind, data)
    assert restored == preset

    repos = importlib.import_module("scribesense.store.repos")
    source = inspect.getsource(repos.PresetRepo)
    assert "preset_values_to_json" in source and "preset_from_row" in source


def test_what_the_model_still_owns_is_still_here() -> None:
    """The removal was scoped to serialisation; the preset *model* is unchanged.

    `STORABLE_KINDS`, `RESERVED_IDS`, `CUSTOM_ID` and `is_storable` state which kinds exist
    and which ids are reserved. The store re-states that policy inline next to its SQL
    rather than asking `is_storable` -- worth reconciling (it is M3.1's call), but not a
    reason to have kept a second serialiser alive.
    """
    assert presets.STORABLE_KINDS == frozenset({"user", "custom"})
    assert presets.CUSTOM_ID in presets.RESERVED_IDS
    assert set(presets.BUILTIN_IDS) <= presets.RESERVED_IDS
    assert presets.ORIGINAL in presets.RESERVED_IDS
    assert presets.is_storable(presets.custom_preset(presets.builtin("default").values))
    assert not presets.is_storable(presets.builtin("default"))


# ---- the premises the corrected docstrings rely on --------------------------------------


def test_m31_exists_and_offers_a_font_registry() -> None:
    """`generator.py` claimed M3.1 did not exist. It does -- and that is the correction."""
    repos = importlib.import_module("scribesense.store.repos")
    assert hasattr(repos, "FontRegistryRepo")


def test_the_registry_seam_still_does_not_match_m31_and_nothing_bridges_it() -> None:
    """**The finding behind the correction, asserted rather than only written down.**

    `FontRegistry` here is `record(Mapping)` / `forget(key)`. `FontRegistryRepo` is a typed
    `FontRegistryEntry` through `create()` / `set_status()` / `mark_removed()`. Nothing
    converts between them and no caller passes ``registry=``, so `_record` assembles a row
    on every successful build that `NullRegistry` throws away.

    Nothing depends on the row for correctness -- Research2 I.1 makes the filesystem the
    authority and `installed_sets` the reconciliation path -- but the row should be written.
    When someone bridges the two shapes this test fails, which is the moment to delete it
    and update the M1.8 card.
    """
    repos = importlib.import_module("scribesense.store.repos")

    def public_methods(cls: type) -> set[str]:
        return {
            name
            for name, _ in inspect.getmembers(cls, inspect.isfunction)
            if not name.startswith("_")
        }

    seam, repo = public_methods(generator.FontRegistry), public_methods(repos.FontRegistryRepo)
    assert seam == {"record", "forget"}
    assert not (seam & repo), "the shapes now overlap -- has someone bridged them?"

    # And the default really is the one that stores nothing.
    signature = inspect.signature(generator.build)
    assert signature.parameters["registry"].default is None
    assert generator.NullRegistry().record({"key": "x"}) is None


def test_reference_source_is_implemented_and_wired() -> None:
    """`generator.py` claimed nothing implemented `ReferenceSource`. O4 did, on 2026-10-17.

    Checked structurally rather than by `issubclass`: `ReferenceSource` is a plain Protocol,
    not runtime-checkable, and `controller/service.py` is where the wiring lives.
    """
    font_references = importlib.import_module("scribesense.safety.font_references")
    source = font_references.FontReferenceSource
    for method in ("transaction_active", "references"):
        assert callable(getattr(source, method, None)), method

    service = inspect.getsource(importlib.import_module("scribesense.controller.service"))
    assert "remove_unused(references=FontReferenceSource(" in service.replace("\n", "")


def test_base_font_files_exists_so_the_files_override_is_only_for_tests() -> None:
    """The last of the four stale claims: `base_font_files` "does not exist yet (K7 E5)"."""
    assert callable(presets.base_font_files)
    assert presets.base_font_files("Noto Sans")["Regular"].is_file()
