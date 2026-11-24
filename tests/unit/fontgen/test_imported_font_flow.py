"""W1 end to end (amendment 2026-11-24): import a font through the service, build the real spaced font from the
stored copy, Apply -> Keep -> Original, and the in-use rules around it. Real store, journal, controller, resolve,
validate and patcher; fake adapters, renderer and fontconfig (unit sandbox). The import check starts only
`sys.executable` (real_subprocess)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

from scribesense.adapters.fake import FakeAdapter, FakeWorld
from scribesense.config import bundled
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.paths import user_fonts_dir
from scribesense.contracts.presets import Preset, PresetValues, WordSpacing
from scribesense.controller.service import Service
from scribesense.fontgen import generator

pytestmark = pytest.mark.real_subprocess

FILE = "/fake/fc.conf"


@pytest.fixture(autouse=True)
def short_runtime_dir(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    runtime = sandbox.data_dir.parent / "run"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))


def make_family(tmp_path: Path, style: str) -> Path:
    f = TTFont(bundled.base_font_files("Noto Sans")[style])
    for record in list(f["name"].names):
        if record.nameID in (1, 16):
            f["name"].setName("Test Sans", record.nameID, record.platformID, record.platEncID, record.langID)
    f["name"].removeNames(nameID=21)
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = tmp_path / f"TestSans-{style}.ttf"
    f.save(out)
    return out


def preset(family: str) -> Preset:
    return Preset("w1-test", "Imported", PresetValues(family, 0.12, WordSpacing("target_em", 0.16), 1.5, 1.2, 1.0),
                  "user")


def test_import_apply_keep_original_with_an_imported_font(tmp_path: Path, renderer, tools) -> None:  # type: ignore[no-untyped-def]
    world = FakeWorld()
    world.set_file(FILE, b"orig")
    svc = Service.open(adapters=[FakeAdapter(world, {"fc": [FILE]}, name="fontconfig")])
    svc.controller._generator = lambda c: generator.build(c, renderer=renderer, tools=tools)  # the real patcher
    try:
        report = svc.import_fonts("files", [make_family(tmp_path, "Regular"), make_family(tmp_path, "Bold")])
        assert report.imported == {"Test Sans": ["Regular", "Bold"]} and svc.imported_families() == ["Test Sans"]

        applied = svc.apply(preset("Test Sans"))  # resolve (target_em -> imported space width), validate, build
        assert applied.status == "awaiting_confirmation", applied.reason_code
        built = applied.fonts
        assert built is not None and built.reading_family.startswith("AccessSans-")
        assert all(Path(f).exists() for f in built.files) and world.state(FILE).data != b"orig"
        with pytest.raises(ScribeSenseError) as exc:  # never while the confirm window is open
            svc.import_fonts("files", [make_family(tmp_path / "again", "Regular")])
        assert exc.value.code is ErrorCode.APPLY_IN_PROGRESS

        svc.keep(applied.tx)
        with pytest.raises(ScribeSenseError) as exc:  # the confirmed style uses it
            svc.delete_imported_family("Test Sans")
        assert exc.value.code is ErrorCode.IMPORTED_FONT_IN_USE

        assert svc.original().failed == [] and world.state(FILE).data == b"orig"
        assert svc.delete_imported_family("Test Sans") is True
        assert not any(user_fonts_dir().glob("*/*.ttf")) and svc.imported_families() == []
        stopped = svc.apply(preset("Test Sans"))  # deleted: neither bundled nor imported any more
        assert stopped.tx is None and stopped.status != "awaiting_confirmation"
    finally:
        svc.close()


def test_closing_the_service_forgets_imported_names(tmp_path: Path) -> None:
    world = FakeWorld()
    world.set_file(FILE, b"orig")
    svc = Service.open(adapters=[FakeAdapter(world, {"fc": [FILE]}, name="fontconfig")])
    svc.import_fonts("files", [make_family(tmp_path, "Regular")])
    assert set(bundled.base_font_files("Test Sans")) == {"Regular"}
    svc.close()
    with pytest.raises(ScribeSenseError) as exc:
        bundled.base_font_files("Test Sans")
    assert exc.value.code is ErrorCode.BUNDLED_FONT_MISSING
