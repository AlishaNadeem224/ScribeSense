"""M1.8 / K7 -- O1's review of O4's 2026-10-15 integration, and the `PreviewFonts` confirmation.

Tasks 2.1 and 2.2 of O1's remaining work. ARCHITECTURE.md's M1.8 note records nine changes O4 made
to O1's modules while integrating them, ending "**O1 to review**"; K7's note adds
"``build_preview()`` keeps the provisional `PreviewFonts` shape exactly (**O1 confirms it**)".

Every claim in that note was checked. All nine hold. What this file adds is the subset that was
**verified by hand but not pinned by a test**, so the integration cannot silently come undone:

* `tests/unit/fontgen/test_config.py::test_vocabulary_is_the_k1_contract_not_a_copy` covers **K1
  only** -- K2's and K7's types were never asserted to be the contract's own objects.
* `test_refusals_carry_error_codes` checks `FontBuildResult.reason_code`; nothing checked the
  *errors contract* itself -- that every raised exception's message is the bare code with no path.
* The note's two "behaviour to know" items (Latin-only tracking; a line height below the font's
  natural height cannot be reduced) were documentation only.

Verdict recorded in PLAN_v2 (2026-11-14): integration **approved**; `PreviewFonts` **fields
confirmed**, with one semantic finding on `styles` raised for a K7 decision -- see
`test_preview_styles_is_parallel_to_files_not_a_set`.
"""

from __future__ import annotations

import dataclasses
import inspect
import os
import tempfile
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

from scribesense.config import bundled, presets, settings
from scribesense.config.rules import latin_glyphs
from scribesense.contracts import config as k1
from scribesense.contracts import fonts as k7
from scribesense.contracts import presets as k2
from scribesense.contracts.errors import ScribeSenseError
from scribesense.fontgen import cache, generator, naming, patcher, validate, variants


@pytest.fixture(autouse=True)
def _temp_files_in_tmp_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every tempfile.mkstemp / mkdtemp in this module lands in pytest's tmp_path, which pytest removes
    (added 2026-11-22: these calls used to leave files in the system /tmp)."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


FAMILY = "AccessSans-000000000000"


def _default() -> settings.Configuration:
    return presets.resolve(presets.builtin("default").values)


# ---- claim 3: the contracts are used, not copied ----------------------------------------


def test_k2_and_k7_types_are_the_contract_objects_not_copies() -> None:
    """Extends the K1-only check to the two contracts it left out.

    A copied dataclass is the failure this guards: it would type-check, pass its own tests, and
    then fail an `isinstance` somewhere in O2's UI for reasons nobody could see.
    """
    assert presets.WordSpacing is k2.WordSpacing
    assert presets.PresetValues is k2.PresetValues
    assert presets.Preset is k2.Preset
    assert presets.BUILTIN_IDS is k2.BUILTIN_IDS
    assert presets.ORIGINAL is k2.ORIGINAL
    assert generator.FontSet is k7.FontSet
    # K1, for completeness beside the others rather than in a second file.
    assert settings.Configuration is k1.Configuration
    assert settings.Range is k1.Range and settings.RANGES is k1.RANGES


def test_the_generator_result_types_extend_k7_without_changing_it() -> None:
    """M1.8 adds fields; it may not remove or reorder K7's.

    K7's three `FontBuildResult` fields are positional for every caller outside O1, so an inserted
    field would silently shift `reason_code` into `fonts`.
    """
    assert issubclass(generator.FontBuildResult, k7.FontBuildResult)
    assert issubclass(generator.PreviewFonts, k7.PreviewFonts)

    k7_fields = [f.name for f in dataclasses.fields(k7.FontBuildResult)]
    mine = [f.name for f in dataclasses.fields(generator.FontBuildResult)]
    assert mine[: len(k7_fields)] == k7_fields
    assert mine[len(k7_fields):] == ["warnings", "detail"]
    for extra in dataclasses.fields(generator.FontBuildResult)[len(k7_fields):]:
        assert extra.default is not dataclasses.MISSING, extra.name


# ---- claim 7: the errors contract -------------------------------------------------------


def _raise_bundled_font_missing() -> None:
    bundled.base_font_files("Not A Bundled Family")


def _raise_patch_unsupported() -> None:
    handle, name = tempfile.mkstemp(suffix=".ttf")
    os.close(handle)
    Path(name).write_bytes(b"not a font")
    patcher.patch(name, _default(), 1.5, Path(tempfile.mkdtemp()) / "out.ttf")


def _raise_install_failed() -> None:
    cache.install("0123456789ab", [])


def _raise_remove_refused() -> None:
    cache.remove("../../etc/passwd")


def _raise_preview_unavailable() -> None:
    generator.build_preview(settings.Configuration("Nope", 0.12, 1.5, 1.5, 1.2, 1.0))


@pytest.mark.parametrize(
    "name,trigger",
    [
        ("BundledFontMissing", _raise_bundled_font_missing),
        ("PatchUnsupported", _raise_patch_unsupported),
        ("InstallFailed", _raise_install_failed),
        ("RemoveRefused", _raise_remove_refused),
        ("PreviewUnavailable", _raise_preview_unavailable),
    ],
)
def test_every_refusal_exception_carries_a_bare_code_and_no_path(name: str, trigger) -> None:  # type: ignore[no-untyped-def]
    """The errors contract, asserted on the exceptions rather than on `FontBuildResult`.

    O1's modules handle paths constantly, so the risk is a path reaching a log through an
    exception message. The contract's answer is that ``str(exc)`` is the code and nothing else;
    the diagnosis lives on ``.detail``, which is for the developer, not the log.
    """
    with pytest.raises(ScribeSenseError) as exc:
        trigger()
    message = str(exc.value)
    assert message == exc.value.code.value, name
    assert "/" not in message and "\\" not in message, (name, message)
    # The diagnosis still exists -- it is just not in the message.
    assert getattr(exc.value, "detail", None) or getattr(exc.value, "reason_code", None), name


def test_caller_errors_stay_valueerror_and_are_not_dressed_as_font_failures() -> None:
    """The deliberate split the integration preserved, and the reason it matters.

    `patch()` raises ``ValueError`` for a caller error and `PatchUnsupported` for a font problem.
    Folding the first into the second would let a bad configuration masquerade as an unpatchable
    font -- M1.8 would report `refused`/`PATCH_UNSUPPORTED` and the real fault would be invisible.

    So not every exception in O1's area is a `ScribeSenseError`, and that is correct, not an
    oversight: `PresetResolutionError`, `SpaceWidthUnavailable` and `BuiltinOverwriteError`
    are all programming errors in the caller.
    """
    unrounded = dataclasses.replace(_default(), letter_em=0.131)  # off K1's 0.02 grid
    source = bundled.base_font_files("Noto Sans")["Regular"]
    out = Path(tempfile.mkdtemp()) / "out.ttf"

    with pytest.raises(ValueError) as exc:
        patcher.patch(source, unrounded, 1.5, out)
    assert not isinstance(exc.value, ScribeSenseError)
    assert not out.exists(), "a caller error must still write nothing"

    for kind in (presets.PresetResolutionError, presets.SpaceWidthUnavailable,
                 presets.BuiltinOverwriteError):
        assert not issubclass(kind, ScribeSenseError), kind.__name__


# ---- claim 8: K2's function has one home ------------------------------------------------


def test_base_font_files_is_one_function_reachable_from_all_three_modules() -> None:
    """K2 places the interface in `scribesense.config`; `variants` re-exports it for M1.5.

    Identity, not equality: two wrappers would each need the `fonts_root` fix, and only one
    would get it.
    """
    assert presets.base_font_files is bundled.base_font_files
    assert variants.base_font_files is bundled.base_font_files


# ---- claim 2: the fontconfig escaping bug -----------------------------------------------


def test_every_generated_family_would_be_misread_unescaped_and_is_escaped() -> None:
    """The bug that made every Linux build fail, pinned at its cause rather than its symptom.

    In fontconfig's pattern syntax ``-`` separates family from size, so `AccessSansUI-3f9a...`
    parses as family ``AccessSansUI`` at size 3 and fontconfig silently substitutes. Every
    generated family contains a ``-`` by construction (the key suffix), so this is not an edge
    case -- it is every build, which is why M1.6 refused every set until it was fixed.
    """
    key = "3f9a1c2b7d10"
    for family in (naming.reading_family(key), naming.ui_family(key)):
        assert "-" in family, "the key suffix is what makes escaping mandatory"
        assert naming.fc_pattern(family) == family.replace("-", r"\-")

    # Both modules that hand a family to fontconfig go through it.
    for module in (validate, cache):
        assert "fc_pattern(" in inspect.getsource(module), module.__name__


# ---- claim 10: the two behaviours the note flags ----------------------------------------


def test_digits_and_punctuation_are_not_tracked() -> None:
    """The note's first "behaviour to know", on a real font, with its consequence named.

    `latin_glyphs()` derives membership from the Unicode *script* property, and digits and ASCII
    punctuation are script Common, not Latin. So they keep their original advance while letters
    grow. The visible result is that "Chapter 12." is tracked after every letter and not after
    ``1``, ``2`` or ``.``.

    Recorded as a finding (docs/research2-conformance-O1.md 9.4), because WCAG 1.4.12's letter
    spacing is a property of the text run, not of letters. Asserted here as current behaviour so
    that widening the set is a visible decision rather than a silent drift.
    """
    config = _default()
    source = bundled.base_font_files("Noto Sans")["Regular"]
    out = Path(tempfile.mkdtemp()) / "out.ttf"
    patcher.patch(source, config, config.reading_line, out, family_name=FAMILY)

    with TTFont(source, lazy=True) as before, TTFont(out, lazy=True) as after:
        cmap = before.getBestCmap()
        latin = latin_glyphs(before)
        delta = patcher.letter_delta_units(config.letter_em, before["head"].unitsPerEm)

        for char in "aZé":
            glyph = cmap[ord(char)]
            assert glyph in latin, char
            assert after["hmtx"][glyph][0] == before["hmtx"][glyph][0] + delta, char

        for char in "0123456789.,;:!?()":
            glyph = cmap[ord(char)]
            assert glyph not in latin, char
            assert after["hmtx"][glyph][0] == before["hmtx"][glyph][0], char


def test_a_line_height_below_the_fonts_natural_height_is_a_no_op_but_still_keys() -> None:
    """The note's second "behaviour to know", and the cache consequence it does not mention.

    Noto Sans' ascent + descent is 1.362 em, and `line_gap_units` clamps the gap at zero, so every
    `ui_line` from 1.0 to 1.3 produces **byte-identical** output. All three built-in presets ship
    `ui_line` 1.2, squarely inside that band.

    But `ui_line` is in `KEYED_FIELDS`, so those identical artifacts get **different content
    keys** -- a false miss: a redundant rebuild and a second install under a new family name.
    Research2 F.1's open item D-08 says the line fields must leave the key exactly when the
    artifacts are byte-identical; M1.5 closed D-08 on "the patcher does write vertical metrics",
    which is true in general and false in this band. Safe direction (never a wrong cache hit), but
    undocumented -- see docs/research2-conformance-O1.md 9.3.
    """
    source = bundled.base_font_files("Noto Sans")["Regular"]
    with TTFont(source, lazy=True) as font:
        natural = (font["hhea"].ascent - font["hhea"].descent) / font["head"].unitsPerEm
    assert natural == pytest.approx(1.362, abs=5e-4)

    directory = Path(tempfile.mkdtemp())
    outputs = {}
    for ui_line in (1.0, 1.1, 1.2, 1.3):
        out = directory / f"ui-{ui_line}.ttf"
        patcher.patch(source, _default(), ui_line, out, family_name=FAMILY)
        outputs[ui_line] = out.read_bytes()
    assert len(set(outputs.values())) == 1, "below the natural height the gap clamps to zero"

    source_sha = naming.file_sha256(source)
    keys = {
        ui_line: naming.content_key(
            dataclasses.replace(_default(), ui_line=ui_line),
            source_sha,
            naming.GENERATOR_VERSION,
        )
        for ui_line in (1.0, 1.1, 1.2, 1.3)
    }
    assert len(set(keys.values())) == 4, "identical bytes, four keys -- the false-miss case"


# ---- task 2.2: confirming the K7 PreviewFonts shape -------------------------------------


def test_preview_fonts_has_exactly_the_k7_fields() -> None:
    """The confirmation K7's note asks O1 for: the provisional shape is the shipped shape."""
    expected = ["reading_family", "ui_family", "files", "styles", "temp_dir"]
    assert [f.name for f in dataclasses.fields(k7.PreviewFonts)] == expected
    assert [f.name for f in dataclasses.fields(generator.PreviewFonts)] == expected
    # The subclass adds behaviour, never state -- and deliberately no __del__, so loaded preview
    # files are never deleted behind the caller's back.
    assert "__del__" not in vars(generator.PreviewFonts)
    assert {"close", "__enter__", "__exit__"} <= set(vars(generator.PreviewFonts))


def test_preview_styles_is_parallel_to_files_not_a_set() -> None:
    """**Finding raised by this review.** `styles` means two different things in one contract.

    K7's `PreviewFonts` says *"styles -- styles actually generated, in the same order as files"*,
    so the implementation returns one entry per file. With both variants built, every style
    therefore appears **twice**::

        files  = (AccessSans-<key>-Regular.ttf, ..., AccessSansUI-<key>-Regular.ttf, ...)
        styles = ("Regular", "Bold", "Italic", "BoldItalic",
                  "Regular", "Bold", "Italic", "BoldItalic")

    K7's sibling `FontSet.styles` -- same name, same contract -- is the **deduplicated** set
    (``("Regular", "Bold")``). So ``len(result.styles)`` is 4 or 8 for one configuration depending
    on which object you hold.

    Both match their own wording, nothing is broken, and **no caller reads
    `PreviewFonts.styles` at all** today (O2's `ui/preview.py` uses `files` and `temp_dir`). That
    is why this is a finding and not a defect: the field is currently unused, so it is cheap to
    settle now and expensive to settle after someone writes `for style in preview.styles`.

    O1's recommendation, for the K7 owner plus one other: make it the deduplicated set, matching
    `FontSet.styles`. The per-file mapping is already recoverable from the filenames, which is
    exactly how M1.6 and M1.7 recover it.

    This test asserts **today's** behaviour, so the change is visible here when it is made.
    """
    preview = generator.build_preview(_default())
    try:
        assert len(preview.styles) == len(preview.files)
        assert len(set(preview.styles)) * 2 == len(preview.styles), "each style, once per variant"
        assert sorted(set(preview.styles)) == ["Bold", "BoldItalic", "Italic", "Regular"]
        # The divergence, stated as an assertion rather than only in prose.
        built = variants.build_variant_set(_default(), out_dir=Path(tempfile.mkdtemp()))
        assert len(built.styles) == 4 and len(preview.styles) == 8
    finally:
        preview.close()
