"""M1.3 -- the isolated parse, and OTF/CFF coverage (PLAN_v2 Phase III, W1.3 and W1.7).

Task 4 of O1's remaining-work list is W1, user font import. Most of W1 is blocked on a K1/K2
contract change that needs a second owner (proposal in PLAN_v2 2026-11-14). Two parts are not
blocked, because they are improvements to O1's own module that stand on their own:

**W1.3's parse budget.** `rules.MAX_FONT_BYTES` is a size cap, and its docstring said so:
"A small but pathological file -- a decompression bomb, or a table whose declared counts
force quadratic work -- can still consume unbounded time inside fontTools." Research2 D.2
and D.6 require a real budget on security grounds. `is_eligible(path, isolate=True)` now
provides one, and `FONT_PARSE_TIMEOUT` makes Research2's ``E_PARSE_TIMEOUT`` reachable
instead of being folded onto the size cap. W1 is its intended caller -- an imported font is
untrusted and the font it generates is later loaded by every application on the machine --
but the capability is useful the moment it exists.

**W1.7's OTF/CFF tests.** No bundled family is CFF (all four are TrueType `glyf`), so the
shortlist cannot exercise the CFF charstring path. `tests/unit/fontgen/test_fontgen.py`
covers CFF patching on a tiny fixture; these tests build a fuller CFF font and run the M1.4
acceptance criteria and the eligibility gate over it, which is what W1.7 asks for before
imported OTFs are accepted.

These tests launch real child processes. They are outside `tests/`, so they do not inherit
`tests/conftest.py`'s process blocking -- which is also why the child is always
`sys.executable` running a module from this package, never anything else.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path

import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.ttLib import TTFont

from scribesense.config import bundled, presets, rules
from scribesense.config.fontprobe import EXIT_BAD_ARGS, EXIT_OK, PROTOCOL_VERSION, main
from scribesense.fontgen import patcher, variants

UPM = 1000
SPACE_ADVANCE = 260

#: The `src` directory, i.e. what an installed package would put on the child's path anyway.
SRC_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _child_can_import_scribesense(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the probe's child process import `scribesense` from a plain checkout.

    `rules._isolated_verdict` runs ``sys.executable -P -m scribesense.config.fontprobe``, which
    assumes the package is importable by that interpreter -- true once installed, and the same
    assumption M4.13's Pango probe makes. In a checkout it is not, and every isolated parse
    would refuse with `FONT_UNREADABLE`: a correct fail-closed outcome, and useless for testing
    the thing itself.

    `run_cmd` passes no ``env``, so the child inherits this process's, and ``-P`` suppresses the
    working directory without touching ``PYTHONPATH``. Setting it here stands in for
    installation rather than working around the design.
    """
    existing = os.environ.get("PYTHONPATH")
    monkeypatch.setenv(
        "PYTHONPATH", str(SRC_ROOT) + (os.pathsep + existing if existing else "")
    )


def _cff_font(path: Path, *, family: str = "Probe CFF") -> Path:
    """A small but complete CFF/OTF font: ASCII, an accented letter, and a Greek alpha.

    Greek is there so the non-Latin invariant has something to protect; `é` so the Latin set
    reaches beyond ASCII. Advances vary per glyph, or `is_monospace` would refuse it.
    """
    codepoints = list(range(0x20, 0x7F)) + [0xE9, 0x3B1]
    names = {cp: ("space" if cp == 0x20 else f"uni{cp:04X}") for cp in codepoints}
    order = [".notdef"] + [names[cp] for cp in codepoints]
    advances = {
        ".notdef": 500,
        **{names[cp]: (SPACE_ADVANCE if cp == 0x20 else 420 + (cp % 11) * 15)
           for cp in codepoints},
    }

    builder = FontBuilder(UPM, isTTF=False)
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap(names)
    charstrings = {}
    for name in order:
        pen = T2CharStringPen(advances[name], None)
        if name != "space":
            width = advances[name]
            pen.moveTo((40, 0))
            pen.lineTo((width - 40, 0))
            pen.lineTo((width - 40, 640))
            pen.lineTo((40, 640))
            pen.closePath()
        charstrings[name] = pen.getCharString()
    builder.setupCFF(family.replace(" ", ""), {"FullName": family}, charstrings, {})
    builder.setupHorizontalMetrics({n: (advances[n], 40) for n in order})
    builder.setupHorizontalHeader(ascent=800, descent=-200, lineGap=0)
    builder.setupNameTable({"familyName": family, "styleName": "Regular"})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, sTypoLineGap=0)
    builder.setupPost()
    builder.save(path)
    return path


@pytest.fixture
def cff_font(tmp_path: Path) -> Path:
    return _cff_font(tmp_path / "probe.otf")


def _default_config():  # type: ignore[no-untyped-def]
    return presets.resolve(presets.builtin("default").values)


# ---- W1.3: the verdict does not depend on where it is computed --------------------------


@pytest.mark.parametrize("family", ["Carlito", "Atkinson Hyperlegible", "Lexend", "Noto Sans"])
def test_isolating_the_parse_gives_the_identical_verdict(family: str) -> None:
    """One implementation, two places to run it.

    `is_eligible` and `fontprobe` both call `rules.verdict`; isolation changes only what
    happens when the parser does not come back. Two code paths producing two verdicts is the
    obvious way to get this wrong, so it is asserted on every bundled face.
    """
    for style, path in bundled.base_font_files(family).items():
        assert rules.is_eligible(path) == rules.is_eligible(path, isolate=True), (family, style)


def test_an_isolated_refusal_is_still_a_verdict_not_a_failure(tmp_path: Path) -> None:
    """A monospace font is refused identically in or out of process.

    The distinction the probe protocol has to carry: exit 0 with ``"ok": false`` is a
    *verdict the probe reached*, and a non-zero exit is a failure. Collapsing them would make
    every deliberate refusal look like a broken child.
    """
    mono = _cff_font(tmp_path / "mono.otf")
    with TTFont(mono) as font:  # force every advance to one width
        hmtx = font["hmtx"]
        for name in font.getGlyphOrder():
            hmtx[name] = (600, 40)
        font.save(tmp_path / "fixed.otf")

    assert rules.is_eligible(tmp_path / "fixed.otf").reason_code == rules.BASE_IS_MONOSPACE
    isolated = rules.is_eligible(tmp_path / "fixed.otf", isolate=True)
    assert isolated.reason_code == rules.BASE_IS_MONOSPACE and not isolated.ok


# ---- W1.3: what isolation is actually for ------------------------------------------------


def test_a_parse_that_runs_past_the_deadline_is_killed_and_refused() -> None:
    """The capability `MAX_FONT_BYTES` could not provide, demonstrated.

    An impossibly short deadline stands in for the pathological file, because constructing a
    real decompression bomb would mean shipping one. What is being proved is the mechanism:
    `run_cmd` kills the child and the caller gets `FONT_PARSE_TIMEOUT` rather than hanging.
    """
    path = bundled.base_font_files("Noto Sans")["Regular"]
    assert rules.is_eligible(path, isolate=True).ok, "the same font passes with a real deadline"

    killed = rules.is_eligible(path, isolate=True, timeout=0.001)
    assert killed.reason_code == rules.FONT_PARSE_TIMEOUT
    assert not killed.ok and killed.warnings == ()


@pytest.mark.parametrize(
    "content,label",
    [(b"not a font at all", "not a font"), (b"", "empty"), (b"\x00\x01\x00\x00" + b"\xff" * 64, "truncated sfnt")],
)
def test_a_child_that_cannot_parse_is_a_refusal_never_an_exception(
    tmp_path: Path, content: bytes, label: str
) -> None:
    """Containment, which is the whole point: nothing escapes to the caller.

    Research2 D.2 treats corrupt and adversarial fonts as an expected input class, so the
    contract is a reason code every time -- the same `FONT_UNREADABLE` an in-process parse
    returns, because the caller's response is identical.
    """
    bad = tmp_path / f"{label.replace(' ', '-')}.ttf"
    bad.write_bytes(content)
    assert rules.is_eligible(bad, isolate=True).reason_code == rules.FONT_UNREADABLE
    assert rules.is_eligible(bad).reason_code == rules.FONT_UNREADABLE


def test_the_size_cap_is_applied_before_any_process_is_started(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Refusing an oversized file must cost one stat, not an interpreter start-up."""
    big = tmp_path / "big.ttf"
    big.write_bytes(b"\x00" * 64)
    monkeypatch.setattr(rules, "MAX_FONT_BYTES", 8)

    def explode(*_a: object, **_k: object) -> None:
        raise AssertionError("a child process was started for an oversized file")

    monkeypatch.setattr("scribesense.contracts.paths.run_cmd", explode)
    assert rules.is_eligible(big, isolate=True).reason_code == rules.FONT_TOO_LARGE


def test_the_timeout_code_folds_to_font_unreadable_for_k7() -> None:
    """K7's `reason_code` is the frozen `ErrorCode` list; the fine code rides on `detail`."""
    assert variants.error_code_for(rules.FONT_PARSE_TIMEOUT) == "FONT_UNREADABLE"


# ---- the probe's own protocol -------------------------------------------------------------


def test_the_probe_prints_one_json_line_and_nothing_else(cff_font: Path) -> None:
    """Launched through `run_cmd`, as K3 rule 9 and import rule R2 require.

    Not `subprocess.run`: R2 allows only `contracts/paths.py` to import `subprocess`, and a
    test is not exempt from the rule it is testing the observance of. `run_cmd` raises on a
    non-zero exit, so reaching the assertions is itself the exit-code check.
    """
    from scribesense.contracts.paths import run_cmd

    finished = run_cmd(
        [sys.executable, "-P", "-m", "scribesense.config.fontprobe", str(cff_font)],
        timeout=60,
    )
    assert finished.returncode == EXIT_OK
    assert finished.stderr == b"", "a traceback could echo the path it was given"
    reply = json.loads(finished.stdout)
    assert reply == {"v": PROTOCOL_VERSION, "ok": True, "reason_code": None, "warnings": []}
    assert finished.stdout.decode().count("\n") == 1


@pytest.mark.parametrize("argv", [[], ["a", "b"], [""], ["   "], ["x\x00y"], ["x" * 5000]])
def test_the_probe_refuses_malformed_arguments(argv: list[str]) -> None:
    assert main(argv) == EXIT_BAD_ARGS


def test_a_protocol_version_the_caller_does_not_know_is_a_refusal(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newer probe talking to an older caller must refuse, not be half-understood."""
    monkeypatch.setattr(rules, "fontprobe_version", lambda: PROTOCOL_VERSION + 1)
    path = bundled.base_font_files("Noto Sans")["Regular"]
    assert rules.is_eligible(path, isolate=True).reason_code == rules.FONT_UNREADABLE


# ---- W1.7: OTF/CFF ------------------------------------------------------------------------


def test_a_cff_font_is_eligible_in_and_out_of_process(cff_font: Path) -> None:
    """No bundled family is CFF, so this is the only coverage the format gets here."""
    with TTFont(cff_font, lazy=True) as font:
        assert "CFF " in font and "glyf" not in font
    assert rules.is_eligible(cff_font).ok
    assert rules.is_eligible(cff_font, isolate=True).ok


def test_patching_a_cff_font_meets_the_m14_acceptance_criteria(
    cff_font: Path, tmp_path: Path
) -> None:
    """The M1.4 card's "TrueType and CFF fonts handled", on a font with real coverage.

    The existing synthetic check asserts three characters; this asserts every Latin glyph,
    the space, the non-Latin invariant and the ligature rule, which is what W1.7 wants in
    place before an imported OTF is accepted.
    """
    config = _default_config()
    out = tmp_path / "patched.otf"
    patcher.patch(cff_font, config, config.reading_line, out, family_name="AccessSans-000000000000")

    with TTFont(cff_font, lazy=True) as before, TTFont(out, lazy=True) as after:
        assert "CFF " in after, "the outline format must survive the patch"
        cmap = before.getBestCmap()
        latin = rules.latin_glyphs(before)
        delta = patcher.letter_delta_units(config.letter_em, before["head"].unitsPerEm)

        for glyph in sorted(latin):
            assert after["hmtx"][glyph][0] == before["hmtx"][glyph][0] + delta, glyph

        space = cmap[patcher.SPACE_CODEPOINT]
        assert after["hmtx"][space][0] == round(before["hmtx"][space][0] * config.word_scale)

        greek = cmap[0x3B1]
        assert greek not in latin
        assert after["hmtx"][greek][0] == before["hmtx"][greek][0], "non-Latin must not move"

        assert after["name"].getDebugName(1) == "AccessSans-000000000000"


def test_a_cff_font_keeps_its_suffix_through_the_variant_build(cff_font: Path, tmp_path: Path) -> None:
    """M1.5 `variant_filename` follows the source format: a CFF source stays `.otf`."""
    built = variants.build_variant_set(
        _default_config(), files={"Regular": cff_font}, out_dir=tmp_path / "out"
    )
    assert built.files, "a CFF source must build"
    assert all(path.suffix == ".otf" for path in built.files), [p.name for p in built.files]


def test_zero_spacing_on_cff_leaves_every_advance_identical(cff_font: Path, tmp_path: Path) -> None:
    """Research2 I.4's neutrality rule, on the format the shortlist cannot exercise."""
    config = dataclasses.replace(_default_config(), letter_em=0.0, word_scale=1.0)
    out = tmp_path / "neutral.otf"
    patcher.patch(cff_font, config, config.reading_line, out, family_name="AccessSans-000000000000")

    with TTFont(cff_font, lazy=True) as before, TTFont(out, lazy=True) as after:
        for glyph in before.getGlyphOrder():
            assert after["hmtx"][glyph][0] == before["hmtx"][glyph][0], glyph
