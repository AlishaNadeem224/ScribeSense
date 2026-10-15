"""M1.4 patcher, M1.5 naming/variants, M1.6 validation, M1.7 cache, M1.8 generator (K7).

Card acceptance tests, with synthetic fonts and the fakes from conftest.py (no process is started).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

from scribesense.config.settings import round_to_steps
from scribesense.contracts import fonts as k7
from scribesense.contracts import paths
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.fontgen import cache, generator, naming, patcher, validate, variants

CFG = Configuration("Noto Sans", 0.12, 1.5, 1.8, 1.2, 1.0)


def advances(path: Path) -> dict[int, int]:
    with TTFont(path) as font:
        return {cp: font["hmtx"][g][0] for cp, g in font.getBestCmap().items()}


def built(tmp_path: Path, files, config: Configuration = CFG) -> variants.VariantSet:  # type: ignore[no-untyped-def]
    return variants.build_variant_set(config, files=files, out_dir=tmp_path / "out")


# ---- M1.4 patcher ----------------------------------------------------------------------------------

@pytest.mark.parametrize("name,kw", [("a.ttf", {}), ("a.otf", {"cff": True})])
def test_measured_advances_match_the_configuration(make_font, tmp_path: Path, name: str, kw) -> None:  # type: ignore[no-untyped-def]
    src = make_font(name, **kw)
    out = tmp_path / f"out-{name}"
    patcher.patch(src, CFG, 1.8, out, family_name="AccessSans-000000000000")
    before, after = advances(src), advances(out)
    for ch in "aWé":
        assert after[ord(ch)] - before[ord(ch)] == 120  # +0.12 em at 1000 UPM
    assert after[0x20] == round(before[0x20] * 1.5)  # word_scale x space
    for cp in (0x0915, ord("1"), ord(",")):  # Devanagari and Common script untouched
        assert after[cp] == before[cp]
    with TTFont(out) as font:
        tags = {r.FeatureTag for r in font["GSUB"].table.FeatureList.FeatureRecord} if "GSUB" in font else set()
        assert "liga" not in tags
        assert font["name"].getDebugName(1) == "AccessSans-000000000000"
        assert font["hhea"].ascent - font["hhea"].descent + font["hhea"].lineGap == 1800  # 1.8 x UPM


def test_unsupported_font_raises_patch_unsupported_and_writes_nothing(tmp_path: Path) -> None:
    (tmp_path / "bad.ttf").write_bytes(b"not a font")
    out = tmp_path / "out.ttf"
    with pytest.raises(patcher.PatchUnsupported) as exc:
        patcher.patch(tmp_path / "bad.ttf", CFG, 1.5, out)
    assert isinstance(exc.value, ScribeSenseError) and exc.value.code is ErrorCode.PATCH_UNSUPPORTED
    assert str(exc.value) == "PATCH_UNSUPPORTED" and not out.exists()


# ---- M1.5 naming + variants ------------------------------------------------------------------------

def test_same_inputs_same_key_and_bytes(family_files, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    a = variants.build_variant_set(CFG, files=family_files, out_dir=tmp_path / "a")
    b = variants.build_variant_set(CFG, files=family_files, out_dir=tmp_path / "b")
    assert a.key == b.key and naming.is_content_key(a.key)
    assert [p.read_bytes() for p in a.files] == [p.read_bytes() for p in b.files]


def test_key_inputs(family_files) -> None:  # type: ignore[no-untyped-def]
    key = variants.plan(CFG, files=family_files).key
    assert variants.plan(Configuration("Noto Sans", 0.12, 1.50, 1.8, 1.2, 1.0), files=family_files).key == key
    assert variants.plan(Configuration("Noto Sans", 0.12, 1.5, 1.8, 1.2, 2.0), files=family_files).key == key
    for changed in (Configuration("Noto Sans", 0.12, 1.5, 1.8, 1.3, 1.0),   # ui_line alone
                    Configuration("Noto Sans", 0.14, 1.5, 1.8, 1.2, 1.0),
                    Configuration("Noto Sans", 0.12, 1.75, 1.8, 1.2, 1.0),
                    Configuration("Noto Sans", 0.12, 1.5, 1.9, 1.2, 1.0)):
        assert variants.plan(changed, files=family_files).key != key


def test_family_names_follow_k7(family_files, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    vs = built(tmp_path, family_files)
    assert vs.reading_family == k7.READING_FAMILY_PREFIX + vs.key
    assert vs.ui_family == k7.UI_FAMILY_PREFIX + vs.key
    assert vs.styles == ("Regular", "Italic")  # partial set allowed (K7 rule 4)


def test_missing_regular_is_bundled_font_missing(family_files) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(variants.BundledFontMissing) as exc:
        variants.plan(CFG, files={"Italic": family_files["Italic"]})
    assert exc.value.code is ErrorCode.BUNDLED_FONT_MISSING


def test_fc_pattern_escapes_fontconfig_syntax() -> None:
    # Unescaped, fontconfig reads "AccessSansUI-3f9a…" as family "AccessSansUI" at size 3 and
    # substitutes another font: every UI family failed validation before this (2026-10-15).
    assert naming.fc_pattern("AccessSansUI-3f9a1c2b7d10") == r"AccessSansUI\-3f9a1c2b7d10"
    assert naming.fc_pattern(r"a\b:c,d") == r"a\\b\:c\,d"


# ---- M1.6 validation -------------------------------------------------------------------------------

def test_a_correct_font_passes(family_files, tmp_path: Path, renderer) -> None:  # type: ignore[no-untyped-def]
    vs = built(tmp_path, family_files)
    result = validate.validate_font(vs.files, CFG, sources=family_files, renderer=renderer)
    assert result.ok, result.measurements["checks"]
    assert {vs.reading_family, vs.ui_family} <= set(renderer.resolved)


def test_a_deliberately_unpatched_font_fails(family_files, tmp_path: Path, renderer) -> None:  # type: ignore[no-untyped-def]
    vs = built(tmp_path, family_files)
    regular = next(p for p in vs.files_for("reading") if p.name.endswith("-Regular.ttf"))
    unspaced = Configuration("Noto Sans", 0.0, 1.0, 1.8, 1.2, 1.0)
    patcher.patch(family_files["Regular"], unspaced, 1.8, regular, family_name=vs.reading_family)
    result = validate.validate_font(vs.files, CFG, sources=family_files, renderer=renderer)
    assert not result.ok and variants.error_code_for(result.reason_code) == "VALIDATION_FAILED"


def test_missing_tools_never_pass(family_files, tmp_path: Path, renderer) -> None:  # type: ignore[no-untyped-def]
    renderer.missing = ("pango-view",)
    result = validate.validate_font(built(tmp_path, family_files).files, CFG, sources=family_files, renderer=renderer)
    assert (result.ok, result.reason_code) == (False, "VALIDATION_TOOL_MISSING")


def test_pango_renderer_uses_run_cmd_with_a_private_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run_cmd(argv: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"AccessSansUI-3f9a1c2b7d10\n/x/f.ttf\n", b"")

    monkeypatch.setattr(validate, "run_cmd", fake_run_cmd)
    conf = tmp_path / "private.conf"
    r = validate.PangoRenderer(pango_view="/usr/bin/pango-view", fc_match="/usr/bin/fc-match")
    assert r.resolve_family("AccessSansUI-3f9a1c2b7d10", config_file=conf) == ("AccessSansUI-3f9a1c2b7d10", "/x/f.ttf")
    argv = calls[0]
    assert argv[0] == "env" and f"FONTCONFIG_FILE={conf}" in argv
    assert argv[-1] == r"AccessSansUI\-3f9a1c2b7d10"


def test_render_tool_failure_is_render_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def failing(argv: list[str], timeout: float):  # type: ignore[no-untyped-def]
        raise ScribeSenseError(ErrorCode.ADAPTER_COMMAND_FAILED)

    monkeypatch.setattr(validate, "run_cmd", failing)
    r = validate.PangoRenderer(pango_view="/usr/bin/pango-view", fc_match="/usr/bin/fc-match")
    with pytest.raises(validate.RenderFailed):
        r.resolve_family("AccessSans-3f9a1c2b7d10", config_file=tmp_path / "c.conf")


# ---- M1.7 cache --------------------------------------------------------------------------------------

def test_install_lands_in_k8_fonts_dir_and_resolves(family_files, tmp_path: Path, renderer, tools) -> None:  # type: ignore[no-untyped-def]
    vs = built(tmp_path, family_files)
    validation = validate.validate_font(vs.files, CFG, sources=family_files, renderer=renderer)
    result = cache.install_set(vs.key, vs.files, validation=validation, tools=tools)
    assert cache.fonts_dir() == paths.fonts_dir()
    assert result.directory == paths.fonts_dir() / vs.key and result.resolved is True
    assert all(Path(f).parent == result.directory for f in result.files)
    assert tools.resolve_family(vs.ui_family)[1].startswith(str(result.directory))


def test_remove_deletes_only_that_key(family_files, tmp_path: Path, renderer, tools) -> None:  # type: ignore[no-untyped-def]
    keys = []
    for c in (CFG, Configuration("Noto Sans", 0.2, 2.5, 1.6, 1.2, 1.0)):
        vs = variants.build_variant_set(c, files=family_files, out_dir=tmp_path / str(c.letter_em))
        v = validate.validate_font(vs.files, c, sources=family_files, renderer=renderer)
        keys.append(cache.install_set(vs.key, vs.files, validation=v, tools=tools).key)
    own = paths.fonts_dir().parent / "my-font.ttf"
    own.write_bytes(b"user font")
    assert cache.remove(keys[0], tools=tools) is True
    assert cache.installed_keys() == [keys[1]] and own.exists()


def test_install_failure_is_a_scribesense_error(family_files, tmp_path: Path, tools) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(cache.InstallFailed) as exc:
        cache.install_set("not-a-key", [family_files["Regular"]], tools=tools)
    assert exc.value.code is ErrorCode.INSTALL_FAILED and exc.value.cause_code.startswith("INSTALL_")
    assert not any(paths.fonts_dir().rglob("*.ttf"))  # nothing half-installed


def test_fontconfig_tool_escapes_and_uses_run_cmd(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run_cmd(argv: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"AccessSans-3f9a1c2b7d10\n/f.ttf\n", b"")

    monkeypatch.setattr(validate, "run_cmd", fake_run_cmd)
    fc = cache.Fontconfig(fc_cache="/usr/bin/fc-cache", fc_match="/usr/bin/fc-match")
    fc.resolve_family("AccessSans-3f9a1c2b7d10")
    fc.refresh(paths.fonts_dir())
    assert calls[0][-1] == r"AccessSans\-3f9a1c2b7d10"
    assert calls[1][-2:] == ["-f", str(paths.fonts_dir())]  # scoped: never the whole user cache


# ---- M1.8 generator (K7) ---------------------------------------------------------------------------

def test_build_then_cached_with_k7_types(family_files, renderer, tools) -> None:  # type: ignore[no-untyped-def]
    first = generator.build(CFG, files=family_files, renderer=renderer, tools=tools)
    assert isinstance(first, k7.FontBuildResult) and first.status == "built", first
    assert isinstance(first.fonts, k7.FontSet) and first.fonts.styles == ("Regular", "Italic")
    assert all(Path(f).is_relative_to(paths.fonts_dir()) for f in first.fonts.files)
    again = generator.build(CFG, files=family_files, renderer=renderer, tools=tools)
    assert again.status == "cached" and again.fonts == first.fonts
    scaled = Configuration("Noto Sans", 0.12, 1.5, 1.8, 1.2, 1.5)  # text_scale never rebuilds
    assert generator.build(scaled, files=family_files, renderer=renderer, tools=tools).status == "cached"


def test_refusals_carry_error_codes(make_font, renderer, tools) -> None:  # type: ignore[no-untyped-def]
    mono = generator.build(CFG, files={"Regular": make_font("m.ttf", mono=True)}, renderer=renderer, tools=tools)
    assert (mono.status, mono.reason_code, mono.fonts) == ("refused", "BASE_IS_MONOSPACE", None)
    bad = generator.build(Configuration("Noto Sans", 0.5, 1.5, 1.8, 1.2, 1.0), files={}, renderer=renderer)
    assert (bad.status, bad.reason_code) == ("refused", "OUT_OF_RANGE")
    missing = generator.build(CFG, files={}, renderer=renderer)
    assert (missing.status, missing.reason_code) == ("refused", "BUNDLED_FONT_MISSING")


def test_validation_failure_is_validation_failed_with_detail(family_files, renderer, tools) -> None:  # type: ignore[no-untyped-def]
    renderer.missing = ("pango-view",)
    r = generator.build(CFG, files=family_files, renderer=renderer, tools=tools)
    assert (r.status, r.reason_code, r.detail) == ("failed", "VALIDATION_FAILED", "VALIDATION_TOOL_MISSING")
    assert r.reason_code in ErrorCode.__members__ and not paths.fonts_dir().exists()


def test_preview_is_private_and_deleted_on_close(family_files) -> None:  # type: ignore[no-untyped-def]
    with generator.build_preview(CFG, files=family_files) as p:
        assert isinstance(p, k7.PreviewFonts)
        assert p.reading_family == naming.reading_family(variants.plan(CFG, files=family_files).key)
        assert all(Path(f).parent == Path(p.temp_dir) and Path(f).exists() for f in p.files)
        assert len(p.styles) == len(p.files) and set(p.styles) == {"Regular", "Italic"}
        assert not paths.fonts_dir().exists()  # never installed
    assert not os.path.exists(p.temp_dir)


def test_preview_refusal_raises_preview_unavailable(make_font) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(generator.PreviewUnavailable) as exc:
        generator.build_preview(CFG, files={"Regular": make_font("m.ttf", mono=True)})
    assert exc.value.code is ErrorCode.PREVIEW_UNAVAILABLE and exc.value.reason_code == "BASE_IS_MONOSPACE"


def test_remove_unused_keeps_everything_unless_usage_is_known(family_files, renderer, tools) -> None:  # type: ignore[no-untyped-def]
    key = generator.build(CFG, files=family_files, renderer=renderer, tools=tools).fonts.key

    class Refs:
        def __init__(self, keys: frozenset[str], complete: bool = True, active: bool = False) -> None:
            self.keys, self.complete, self.active = keys, complete, active

        def transaction_active(self) -> bool:
            return self.active

        def references(self) -> generator.References:
            return generator.References(self.keys, self.complete)

    assert generator.remove_unused() == []
    assert generator.remove_unused(references=Refs(frozenset(), complete=False), tools=tools) == []
    assert generator.remove_unused(references=Refs(frozenset(), active=True), tools=tools) == []
    assert generator.remove_unused(references=Refs(frozenset({key})), tools=tools) == []
    assert generator.remove_unused(references=Refs(frozenset()), tools=tools) == [key]


@pytest.mark.parametrize("detail,code", [
    ("BASE_IS_MONOSPACE", "BASE_IS_MONOSPACE"), ("OUT_OF_RANGE", "OUT_OF_RANGE"),
    ("VALIDATION_ADVANCE_MISMATCH", "VALIDATION_FAILED"), ("INSTALL_IO_ERROR", "INSTALL_FAILED"),
    ("NO_UPM", "FONT_UNREADABLE"), ("NO_LATIN_COVERAGE", "PATCH_UNSUPPORTED"), ("boom", "GENERATOR_ERROR"),
])
def test_error_code_for(detail: str, code: str) -> None:
    assert variants.error_code_for(detail) == code


def test_build_rounds_its_input(family_files, renderer, tools) -> None:  # type: ignore[no-untyped-def]
    raw = Configuration("Noto Sans", 0.121, 1.5, 1.8, 1.2, 1.0)
    assert round_to_steps(raw) == CFG
    assert generator.build(raw, files=family_files, renderer=renderer, tools=tools).fonts.key == \
        variants.plan(CFG, files=family_files).key
