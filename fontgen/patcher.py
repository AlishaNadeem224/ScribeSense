"""M1.4 -- Font patcher (owner O1).

    patch(source_path, config, line, out_path) -> None

Produces one patched font file: wider Latin advances, a wider space glyph, raised
line metrics, `liga` removed, and a new family name. Reads one font, writes one file,
and touches nothing else.

What is modified, and nothing else
----------------------------------
Per the card's postcondition and Research2 E.1:

* ``hmtx`` advances for Latin glyphs only -- the set comes from M1.3's `latin_glyphs`.
* ``hmtx`` advance of the space glyph, by the ``word_scale`` factor.
* Vertical line metrics (``hhea.lineGap``, ``OS/2.sTypoLineGap``, and the
  USE_TYPO_METRICS bit).
* The ``liga`` feature, removed from ``GSUB``.
* ``name`` table entries for the renamed variant.
* ``hdmx``/``LTSH``, **deleted** if present -- see `_drop_stale_advance_caches`.

Outlines, non-Latin advances, GPOS, kerning values, ``cmap``, ``post``, and every
other table are left exactly as they were. The tests assert this by diffing the
compiled font rather than by trusting the code.

Failure contract
----------------
Anything that cannot be patched safely raises `PatchUnsupported` (code
`PATCH_UNSUPPORTED`) and **writes nothing**. Research2 E.6 states the reason this is
a hard contract rather than a nicety: "a partially patched font is worse than no font,
because it would render inconsistently and could not be distinguished from a correct
artifact by hash." The output is assembled entirely in memory and only then written,
via a temporary file in the destination directory followed by an atomic replace, so a
crash mid-patch cannot leave a plausible-looking artifact behind.

Platform: pure fontTools plus stdlib. No display server, no fontconfig, no subprocess
-- identical on Windows (development) and Fedora/Wayland (target). Installing the
result and refreshing the font cache belong to M1.7.

Scope note. Research2 E.9 rates font-file spacing as the *fallback* and
application-level spacing as *preferred*, because application tracking is reversible
and does not invalidate kerning or hinting. This module exists because the
architecture chose the font-file route; that choice is not re-litigated here, but the
kerning consequence is real and is left for M1.6 to report (Research2 E.10 asks for a
kerning-pair diff).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from hashlib import blake2b
from io import BytesIO
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

from config.rules import latin_glyphs, open_font
from config.settings import Configuration, validate

__all__ = [
    "LIGATURE_FEATURES",
    "PATCH_UNSUPPORTED",
    "PatchUnsupported",
    "SPACE_CODEPOINT",
    "USE_TYPO_METRICS",
    "letter_delta_units",
    "line_gap_units",
    "patch",
    "space_advance",
]

PATCH_UNSUPPORTED = "PATCH_UNSUPPORTED"
"""Reason code surfaced through K7's ``FontBuildResult.reason_code``."""


class PatchUnsupported(Exception):
    """The font cannot be patched safely; no output was written.

    The card requires the patcher to "raise ``PATCH_UNSUPPORTED``". That names a
    *code*, not a Python type, so the code is carried on the exception as `.code` and
    is also the module constant `PATCH_UNSUPPORTED` -- K7 stores it as a string in
    ``FontBuildResult.reason_code``, and M1.8 maps the raise to a ``failed`` or
    ``refused`` status.
    """

    code = PATCH_UNSUPPORTED

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"{PATCH_UNSUPPORTED}: {detail}")


SPACE_CODEPOINT = 0x20

LIGATURE_FEATURES: frozenset[str] = frozenset({"liga"})
"""GSUB feature tags removed by the patch.

**Only ``liga``** -- standard (Latin) ligatures. This is a deliberate, narrow choice
that resolves a direct conflict between the card and the research:

* The card requires "ligatures removed" and its acceptance test is "no ``liga``
  feature".
* Research2 E.5 says the opposite -- "Do not remove or rewrite ligature lookups in
  GSUB -- that would alter the font's shaping behaviour and could break
  script-specific rendering (Nishikimi et al. 2007)" -- and recommends merely
  *reporting* ligature presence instead.

The card governs, and in fact it solves the very problem E.5 identifies in its own
first sentence: "letter_em modifies individual glyph advances. Ligatures combine
multiple characters into one glyph, so the spacing that would have been applied to the
component characters is not applied within the ligature." A ligature glyph has no cmap
entry of its own, so it is not in `latin_glyphs` and receives no tracking -- leaving
``fi`` visibly tighter than the letters around it at every spacing setting above zero.
E.5's answer is to document that; the card's is to remove the feature, which actually
fixes it.

E.5's concern is nevertheless honoured where it bites. ``rlig`` (Required Ligatures) is
**not** touched, and neither are the Indic and Arabic shaping features (``akhn``,
``rphf``, ``half``, ``pres``, ``init``, ``medi``, ``fina`` and friends). Those are what
Nishikimi et al. are about: scripts whose correct rendering *depends* on substitution.
``liga`` is the discretionary Latin fi/fl/ffi set, and removing it degrades nothing
but Latin typographic polish.

**Residual risk, stated rather than hidden:** a mixed-script font that routes conjunct
formation through ``liga`` rather than the script-specific features would lose those
conjuncts. M1.3 refuses non-Latin-only fonts, so such a font would have to be a
Latin-plus-Indic family to reach here. The card's acceptance test asks for ``liga`` to
be gone outright, so it is removed for every script; scoping removal to the ``latn``
and ``DFLT`` scripts would be the finer-grained alternative if this ever bites.
``dlig`` and ``clig`` are left alone -- the card names ``liga``, and discretionary
ligatures are off by default in most renderers anyway.
"""

_POSTSCRIPT_FORBIDDEN = re.compile(r"[\[\]\(\)\{\}<>/%\x00-\x20\x7f-\xff]")

_NAME_FAMILY = 1
_NAME_SUBFAMILY = 2
_NAME_UNIQUE_ID = 3
_NAME_FULL = 4
_NAME_POSTSCRIPT = 6
_NAME_TYPO_FAMILY = 16
_NAME_TYPO_SUBFAMILY = 17

USE_TYPO_METRICS = 1 << 7
"""``OS/2.fsSelection`` bit 7. Public so M1.6 can assert the patch set it."""


# --------------------------------------------------------------------------- #
# Formulas (Research2 E.2)
# --------------------------------------------------------------------------- #


def letter_delta_units(letter_em: float, units_per_em: int) -> int:
    """``round(letter_em * unitsPerEm)`` -- the advance delta in font design units.

    The em here is the font's **design** em from ``head.unitsPerEm``, not the CSS em
    (Research2 E.2.1). The same ``letter_em`` therefore yields different integer
    deltas per format -- 0.12 em is +120 units at UPM 1000 (conventional for CFF) and
    +246 at UPM 2048 (conventional for TrueType) -- which is why no test here
    hard-codes a delta without also fixing the UPM.
    """
    return round(letter_em * units_per_em)


def space_advance(original: int, word_scale: float) -> int:
    """``round(original_space_advance * word_scale)`` -- Research2 E.2.3.

    The multiplicative reading, per K1's frozen ``word_scale: 1.0-3.0 x space width``.
    Research2 left the additive/multiplicative question open as D-01b; K1 settled it,
    so E.2.2's additive formula is deliberately not used.
    """
    return round(original * word_scale)


def line_gap_units(
    line: float, units_per_em: int, ascender: int, descender: int
) -> int:
    """``max(0, round(line * upm) - (ascender - descender))`` -- Research2 E.2.4.

    Leading is ``L = lh - (A + D)`` (Panchekha & Torlak 2016). The clamp at zero is
    the limitation `_apply_line_metrics` documents: a line gap cannot be negative, so
    a requested line height below the font's intrinsic ``A + D`` is a no-op in the file.

    Public, and the third of this module's formulas to be so, for the same reason as
    the other two: M1.6 has to predict what a correct artifact contains, and the only
    way for its expectation and this module's edit to stay in agreement is for both to
    call one function. ``ascender``/``descender`` are passed in rather than read from a
    font because ``hhea`` and ``OS/2`` carry their own pair and each line gap is
    computed against its own.
    """
    return max(0, round(line * units_per_em) - (ascender - descender))


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #


def _flatten_variable(font: TTFont, instance: Mapping[str, float] | None) -> TTFont:
    """Pin a variable font to one static instance, or raise `PatchUnsupported`.

    Research2 E.4 is explicit about why this must happen *before* any advance is
    touched: advances vary along the axes and are stored as deltas, so "editing the
    default hmtx alone is unsafe" (Parikh et al. 2026). Each weight must be
    instantiated and patched independently.

    ``instance`` gives the axis coordinates; omitted axes are pinned to their
    ``fvar`` default. A font with no ``fvar`` is returned untouched.

    The pre-check matters. ``instantiateVariableFont`` does **not** raise on an
    ``fvar`` font that carries no ``gvar`` or ``CFF2`` deltas -- verified against
    fontTools 4.60 -- it silently returns something that is not a usable static font.
    So the deltas are checked for first, and the result is checked afterwards for a
    surviving ``fvar``. Research2 E.4: "If instantiation fails for any weight, return
    PATCH_UNSUPPORTED and produce no output font."
    """
    if "fvar" not in font:
        if instance:
            raise PatchUnsupported(
                "instance axis values given for a font with no fvar axes"
            )
        return font

    if "gvar" not in font and "CFF2" not in font:
        raise PatchUnsupported(
            "variable font declares fvar but carries no gvar or CFF2 deltas"
        )

    axes = {axis.axisTag: axis for axis in font["fvar"].axes}
    requested = dict(instance or {})
    unknown = sorted(set(requested) - set(axes))
    if unknown:
        raise PatchUnsupported(f"font has no such axis: {', '.join(unknown)}")

    location = {tag: axis.defaultValue for tag, axis in axes.items()}
    for tag, value in requested.items():
        axis = axes[tag]
        if not axis.minValue <= value <= axis.maxValue:
            raise PatchUnsupported(
                f"axis {tag}={value} outside [{axis.minValue}, {axis.maxValue}]"
            )
        location[tag] = value

    try:
        flattened = instancer.instantiateVariableFont(
            font, location, inplace=False, updateFontNames=False
        )
    except Exception as exc:  # fontTools raises a wide variety here
        raise PatchUnsupported(f"variable font could not be instantiated: {exc}") from exc

    if "fvar" in flattened:
        raise PatchUnsupported("instancing left the font variable")
    return flattened


def _apply_spacing(font: TTFont, config: Configuration, units_per_em: int) -> None:
    """Widen Latin advances and the space glyph. Nothing else is touched."""
    hmtx = font["hmtx"]
    targets = latin_glyphs(font)
    if not targets:
        raise PatchUnsupported("no Latin glyphs to space")

    cmap = font.getBestCmap()
    space = cmap.get(SPACE_CODEPOINT)
    if space is None or space not in font.getGlyphOrder():
        # word_scale is one of the card's required transformations, so a font with no
        # space glyph cannot satisfy the contract. Refusing beats silently skipping it.
        raise PatchUnsupported("font has no space glyph, so word spacing cannot apply")

    # The space is Unicode script Common, never Latin, so it is not in `targets` and
    # cannot receive the letter delta on top of the word factor. Asserted rather than
    # assumed, because a change to M1.3's script rule would silently double-apply here.
    assert space not in targets, "space glyph must not be in the Latin spacing set"

    delta = letter_delta_units(config.letter_em, units_per_em)
    if delta:
        for name in sorted(targets):
            advance, lsb = hmtx[name]
            # Advance only. The outline is not shifted: moving glyphs would change
            # `glyf`/CFF data, which the card's "nothing else changed" forbids, and
            # Research2 E.2.1 scopes the edit to "the horizontal advance ... in hmtx".
            # The visible consequence is that tracking is added entirely on the right
            # of each glyph rather than split either side.
            hmtx[name] = (max(0, advance + delta), lsb)

    original_space, space_lsb = hmtx[space]
    hmtx[space] = (max(0, space_advance(original_space, config.word_scale)), space_lsb)


def _apply_line_metrics(font: TTFont, line: float, units_per_em: int) -> None:
    """Raise the line box to ``line x upm`` by widening the line gap.

    Leading is ``L = lh - (A + D)`` (Research2 E.2.4, Panchekha & Torlak 2016), so the
    gap needed for a target line height is ``round(line * upm) - (ascender - descender)``.
    Each metric set is kept internally consistent against *its own* ascender and
    descender, because ``hhea`` and ``OS/2`` need not agree and renderers disagree
    about which set defines the default line box.

    ``usWinAscent``/``usWinDescent`` are left alone: they are clipping bounds derived
    from the glyph bounding box, not line-height inputs, and shrinking them would clip
    accents.

    **Limitation, by construction.** A line gap cannot be negative in any useful sense,
    so when a font's intrinsic ``A + D`` already exceeds the requested line height the
    gap is clamped to 0 and the font cannot express the smaller line box. Many text
    faces have ``A + D`` around 1.2-1.4 em, so a requested ``line`` of 1.0 is typically
    a no-op in the file. This is not a bug to work around here: Research2 K-28 and E.9
    both hold that line height is an application property and that "metric editing
    alone [is] insufficient", so the rendering layer remains authoritative and the font
    metrics are a fallback for renderers that compute line height from the font.
    """
    hhea = font["hhea"]
    hhea.lineGap = line_gap_units(line, units_per_em, hhea.ascent, hhea.descent)

    os2 = font.get("OS/2")
    if os2 is not None:
        os2.sTypoLineGap = line_gap_units(
            line, units_per_em, os2.sTypoAscender, os2.sTypoDescender
        )
        # Without USE_TYPO_METRICS many renderers prefer usWin*/hhea and ignore the
        # sTypo* set entirely, which would make the OS/2 edit above invisible.
        os2.fsSelection |= USE_TYPO_METRICS


def _drop_stale_advance_caches(font: TTFont) -> None:
    """Delete ``hdmx`` and ``LTSH``, which cache advance widths that are now wrong.

    Both tables store per-size device advance widths derived from ``hmtx``. Leaving
    them after changing ``hmtx`` yields a font that contradicts itself and renders at
    the old widths at some ppem sizes. They are optional caches, so deleting is the
    correct repair; recomputing them would require rasterising at every size.

    This is the one edit not named in the card's permitted list, and it is a
    consequence of the edit the card *does* require rather than an independent change.
    Research2 E.1 anticipates it, naming "hdmx/hinting data" among what a patcher may
    touch "to prevent hinting from overriding the new advances".

    TrueType *instructions* are deliberately left in place. Research2 E.1 and Shamir
    2003 note that hinting can override advances at small ppem, but stripping
    instructions would change ``glyf`` and cross the card's "nothing else changed"
    line. The risk is therefore documented and handed to M1.6's rendering checks.
    """
    for table in ("hdmx", "LTSH"):
        if table in font:
            del font[table]


def _remove_ligatures(font: TTFont) -> None:
    """Remove `LIGATURE_FEATURES` from GSUB, re-indexing everything that refers to them.

    Deleting a ``FeatureRecord`` renumbers every record after it, so every
    ``FeatureIndex`` in the script/language tree has to be remapped or the font ends up
    applying the *wrong* features -- a far worse outcome than leaving ``liga`` in
    place. Lookups are left in the ``LookupList`` even when they become unreachable:
    removing them would renumber ``LookupListIndex`` across all remaining features,
    and an orphaned lookup is inert.
    """
    gsub = font.get("GSUB")
    table = getattr(gsub, "table", None)
    if table is None or table.FeatureList is None:
        return

    records = table.FeatureList.FeatureRecord
    keep = [
        index
        for index, record in enumerate(records)
        if record.FeatureTag not in LIGATURE_FEATURES
    ]
    if len(keep) == len(records):
        return

    remap = {old: new for new, old in enumerate(keep)}
    table.FeatureList.FeatureRecord = [records[index] for index in keep]
    table.FeatureList.FeatureCount = len(keep)

    def _fix(lang_sys) -> None:
        if lang_sys is None:
            return
        lang_sys.FeatureIndex = [
            remap[i] for i in lang_sys.FeatureIndex if i in remap
        ]
        lang_sys.FeatureCount = len(lang_sys.FeatureIndex)
        required = getattr(lang_sys, "ReqFeatureIndex", 0xFFFF)
        if required != 0xFFFF:
            lang_sys.ReqFeatureIndex = remap.get(required, 0xFFFF)

    if table.ScriptList is not None:
        for script_record in table.ScriptList.ScriptRecord:
            script = script_record.Script
            _fix(script.DefaultLangSys)
            for lang_record in script.LangSysRecord or []:
                _fix(lang_record.LangSys)

    # Feature variations index into the same FeatureList. Instancing normally strips
    # them, but a static font may still carry one.
    variations = getattr(table, "FeatureVariations", None)
    if variations is not None:
        for record in variations.FeatureVariationRecord:
            substitution = record.FeatureTableSubstitution
            if substitution is None:
                continue
            kept = []
            for sub in substitution.SubstitutionRecord:
                if sub.FeatureIndex in remap:
                    sub.FeatureIndex = remap[sub.FeatureIndex]
                    kept.append(sub)
            substitution.SubstitutionRecord = kept
            substitution.SubstitutionCount = len(kept)


def _postscript_safe(value: str) -> str:
    """Sanitise a PostScript name: no forbidden characters, at most 63 bytes."""
    return (_POSTSCRIPT_FORBIDDEN.sub("", value) or "Variant")[:63]


def _source_family(font: TTFont) -> str:
    name = font.get("name")
    if name is not None:
        for name_id in (_NAME_TYPO_FAMILY, _NAME_FAMILY):
            value = name.getDebugName(name_id)
            if value:
                return value
    return "Unknown"


def _source_subfamily(font: TTFont) -> str:
    name = font.get("name")
    if name is not None:
        for name_id in (_NAME_TYPO_SUBFAMILY, _NAME_SUBFAMILY):
            value = name.getDebugName(name_id)
            if value:
                return value
    return "Regular"


def _default_family_name(
    source_family: str, config: Configuration, line: float,
    location: Mapping[str, float],
) -> str:
    """A deterministic family name that cannot collide with the source family.

    Used only when the caller supplies no ``family_name``. M1.5 owns real naming and
    derives it from the content key (K7: ``AccessSans-<key>``), which `patch` cannot
    compute -- it is not given the key, nor told whether it is building the reading or
    the UI variant. Rather than guess at M1.5's scheme or, worse, leave the source name
    in place, the fallback is a short digest of everything that affects the artifact.

    Research2 E.7: "Never reuse the source family name for a modified font, because
    that would allow the generated font to shadow or be shadowed by the original."
    That is also the card's must-never, so it holds for the fallback too.
    """
    seed = "|".join(
        [
            source_family,
            f"{config.letter_em:.4f}",
            f"{config.word_scale:.4f}",
            f"{line:.4f}",
            ",".join(f"{tag}={location[tag]:.4f}" for tag in sorted(location)),
        ]
    )
    digest = blake2b(seed.encode("utf-8"), digest_size=4).hexdigest()
    return f"{source_family} Spaced {digest}"


def _rename(font: TTFont, family: str, subfamily: str) -> None:
    """Rewrite the name table so the font identifies as ``family``.

    Every platform/encoding/language record already present is rewritten, so a font
    carrying both Macintosh and Windows records stays consistent; writing only one
    platform is how a renamed font ends up resolving under its old name on some
    systems. ``nameID`` 16/17 are removed rather than rewritten -- with the family
    name no longer split into a typographic pair, leaving them would reintroduce the
    source family through the back door.
    """
    table = font["name"]
    full = f"{family} {subfamily}".strip()
    postscript = _postscript_safe(f"{family}-{subfamily}".replace(" ", ""))

    platforms = {
        (record.platformID, record.platEncID, record.langID)
        for record in table.names
    } or {(3, 1, 0x409)}

    for name_id in (_NAME_TYPO_FAMILY, _NAME_TYPO_SUBFAMILY):
        table.names = [record for record in table.names if record.nameID != name_id]

    for platform_id, encoding_id, language_id in sorted(platforms):
        for name_id, value in (
            (_NAME_FAMILY, family),
            (_NAME_SUBFAMILY, subfamily),
            (_NAME_UNIQUE_ID, f"{full};{postscript}"),
            (_NAME_FULL, full),
            (_NAME_POSTSCRIPT, postscript),
        ):
            table.setName(value, name_id, platform_id, encoding_id, language_id)


def _write_atomically(font: TTFont, out_path: Path) -> None:
    """Compile to memory first, then land the file in one atomic replace.

    Nothing is written until the whole font has compiled, so a compile error leaves no
    artifact. The temporary file is created in the destination directory -- a rename
    across filesystems is not atomic -- and is removed on every failure path.

    Research2 H.4/K-45 caution that POSIX does not define crash outcomes and that
    renames are not unconditionally atomic, so this is "atomic enough that no partial
    artifact is observable in normal operation", not a durability guarantee. Making
    the installed state crash-safe is M1.7's problem.

    **Byte determinism (added for M1.5, 2026-10-14).** ``recalcTimestamp`` is disabled
    before compiling. fontTools defaults it to ``True``, which makes ``head.compile``
    overwrite ``head.modified`` with the current time on every save -- so patching one
    source twice, a second apart, produced two files differing in those four bytes.
    That is harmless to render but fatal to M1.5, whose must-never is "produce two
    different outputs for the same inputs" and whose acceptance test is "same inputs ->
    identical key and bytes"; it would also defeat every hash-based check downstream
    (M1.6's artifact comparison, M1.7's cache). With it off, ``head.modified`` is
    carried over from the source font, which is itself hashed into the key -- so the
    timestamp becomes a property of the inputs rather than of the clock. See K7 D7.
    """
    font.recalcTimestamp = False

    buffer = BytesIO()
    font.save(buffer)
    data = buffer.getvalue()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = out_path.with_name(f".{out_path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, out_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# The card's interface
# --------------------------------------------------------------------------- #


def patch(
    source_path: str | os.PathLike[str],
    config: Configuration,
    line: float,
    out_path: str | os.PathLike[str],
    *,
    family_name: str | None = None,
    instance: Mapping[str, float] | None = None,
) -> None:
    """Patch the font at ``source_path`` into ``out_path``.

    ``config``
        A resolved, validated K1 `Configuration`. ``letter_em`` and ``word_scale`` are
        used; ``base_family`` selects the source upstream, and ``reading_line``,
        ``ui_line`` and ``text_scale`` are not read here -- the line height arrives as
        ``line`` precisely because one configuration yields two variants.
    ``line``
        The line-height ratio for *this* variant: the caller passes
        ``config.reading_line`` for the reading font and ``config.ui_line`` for the UI
        font (K7 builds both).
    ``family_name``
        The generated family name. **Keyword-only with a default**, so the card's
        four-positional-argument call is unchanged. M1.5 passes the real name derived
        from the content key; the fallback is a deterministic digest-suffixed name that
        can never equal the source family.
    ``instance``
        Axis coordinates for flattening a variable font, e.g. ``{"wght": 700}``.
        Keyword-only with a default of the font's own ``fvar`` defaults. Needed because
        the card requires "variable fonts flattened per weight" while the frozen
        signature carries no weight -- without it a variable family could only ever
        produce its default instance.

    Order of operations, which is load-bearing: flatten first (advances are stored as
    deltas, so editing ``hmtx`` before pinning is unsafe -- Research2 E.4), then
    spacing, then line metrics, then drop the stale advance caches, then ligatures,
    then rename, then write.

    Raises `PatchUnsupported` and writes nothing if the font cannot be patched safely.
    Raises ``ValueError`` for a caller error -- an unvalidated configuration or a
    nonsensical ``line`` -- because those are bugs in the caller rather than
    properties of the font, and conflating them would let a bad config masquerade as
    an unsupported font.
    """
    errors = [issue for issue in validate(config) if issue.level == "error"]
    if errors:
        detail = ", ".join(f"{i.field}={i.code}" for i in errors)
        raise ValueError(f"patch() requires a validated Configuration: {detail}")
    if not isinstance(line, (int, float)) or isinstance(line, bool):
        raise ValueError(f"line must be a real number, got {line!r}")
    line = float(line)
    if not (line > 0) or line != line or line in (float("inf"), float("-inf")):
        raise ValueError(f"line must be finite and positive, got {line!r}")

    source = Path(source_path)
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise PatchUnsupported(f"source font unreadable: {exc}") from exc

    try:
        font = open_font(data)
    except Exception as exc:
        raise PatchUnsupported(f"source font could not be parsed: {exc}") from exc

    try:
        if "glyf" not in font and "CFF " not in font and "CFF2" not in font:
            raise PatchUnsupported("font has neither glyf nor CFF/CFF2 outlines")
        for required in ("head", "hhea", "hmtx", "cmap", "name"):
            if not font.has_key(required):
                raise PatchUnsupported(f"font is missing the {required} table")

        # The table *directory* can list a table whose bytes are truncated or
        # corrupt; `lazy=True` means that only surfaces when the table is decoded.
        # Force it here so damage is reported as PATCH_UNSUPPORTED at a predictable
        # point rather than escaping from whichever step happened to touch it first.
        for required in ("head", "hhea", "hmtx", "cmap", "name"):
            font[required]
        font.getBestCmap()
        font.getGlyphOrder()

        font = _flatten_variable(font, instance)
        location = (
            {tag: float(value) for tag, value in (instance or {}).items()}
            if instance
            else {}
        )

        units_per_em = font["head"].unitsPerEm
        if not isinstance(units_per_em, int) or units_per_em <= 0:
            raise PatchUnsupported(f"unusable unitsPerEm: {units_per_em!r}")

        _apply_spacing(font, config, units_per_em)
        _apply_line_metrics(font, line, units_per_em)
        _drop_stale_advance_caches(font)
        _remove_ligatures(font)

        family = family_name or _default_family_name(
            _source_family(font), config, line, location
        )
        if family == _source_family(font):
            raise ValueError("family_name must differ from the source family name")
        _rename(font, family, _source_subfamily(font))

        _write_atomically(font, Path(out_path))
    except (PatchUnsupported, ValueError):
        # ValueError is a caller error and must stay one; re-wrapping it as
        # PATCH_UNSUPPORTED would let a bad configuration masquerade as a bad font.
        raise
    except Exception as exc:
        # Research2 E.6: "Patch raises mid-operation -> return PATCH_UNSUPPORTED."
        # Deliberately broad: a corrupt font makes fontTools raise anything from
        # struct.error to a bare AssertionError (its sfnt reader asserts on a
        # truncated table), and the contract is that nothing escapes but the two
        # cases above. KeyboardInterrupt and SystemExit are not Exceptions and still
        # propagate, which is what we want -- the `finally` below still closes.
        raise PatchUnsupported(f"patch failed mid-operation: {exc!r}") from exc
    finally:
        try:
            font.close()
        except Exception:
            pass
