"""M1.2 -- Bundled base fonts (contract K2, owner O1).

K2: "Base fonts are BUNDLED in the repo (fonts/ + manifest.toml), never taken from the
system." This module finds them -- `base_font_files` is K2's ``PresetApi`` function --
and measures the one metric preset resolution needs, the Regular face's space width.

Moved here from `scribesense.fontgen.variants` (2026-10-15 integration, recorded in
ARCHITECTURE.md M1.2) so that K2's interface lives in `scribesense.config`, where K2 says
it is implemented; `variants` re-exports every name, so callers are unaffected.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from scribesense.contracts.config import SHORTLIST
from scribesense.contracts.errors import ErrorCode, ScribeSenseError

__all__ = [
    "FONTS_ROOT",
    "OPTIONAL_STYLES",
    "REQUIRED_STYLE",
    "STYLES",
    "FONT_SUFFIXES",
    "BundledFontMissing",
    "base_font_files",
    "space_width_em",
]

REQUIRED_STYLE = "Regular"
"""The one style every base family must provide [D R2]."""

OPTIONAL_STYLES: tuple[str, ...] = ("Bold", "Italic", "BoldItalic")
"""Built when the base family ships them; their absence is not an error [D R2]."""

STYLES: tuple[str, ...] = (REQUIRED_STYLE, *OPTIONAL_STYLES)
"""Every style this module knows, in the order `VariantSet.styles` reports them.

A fixed order matters beyond tidiness: ``styles`` reaches the user through K7's
``FontSet`` and is compared between builds, so it must not depend on directory
iteration order, which differs between filesystems and between Windows and Fedora.
"""

FONTS_ROOT = Path(__file__).resolve().parents[3] / "fonts"
"""Repository root of the bundled font tree, per K2's layout.

Resolved from this file rather than the working directory, so a build behaves the same
whatever the process was started from.
"""

FONT_SUFFIXES = (".ttf", ".otf")

_STYLE_ALIASES: Mapping[str, str] = {
    "regular": "Regular",
    "bold": "Bold",
    "italic": "Italic",
    "oblique": "Italic",
    "bolditalic": "BoldItalic",
    "boldoblique": "BoldItalic",
}
"""Filename style tokens, lowercased, mapped onto `STYLES`.

Oblique is folded into Italic because the two occupy the same slot in a four-style
family -- a face is offered to the user as "italic" whether its designer slanted or
redrew it. Anything else (``Light``, ``SemiBold``, ``Condensed``, a variable font's
``VF``) is ignored rather than guessed at: a four-style family is what K2's manifest
describes, and silently promoting ``SemiBold`` to ``Bold`` would ship a weight the user
did not choose.
"""


class BundledFontMissing(ScribeSenseError):
    """The base family has no usable Regular style.

    The card's only hard failure: "Missing Regular -> ``BUNDLED_FONT_MISSING``; other
    missing styles are not an error". K2 names the same code for `base_font_files`, so
    one code covers "the family is not bundled", "its directory is empty" and "it ships
    everything except a Regular" -- from the caller's side these are one situation, and
    M1.8 maps all of them to ``status="refused"`` with this ``reason_code``.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(ErrorCode.BUNDLED_FONT_MISSING)
        # The message stays the bare code (errors contract: never paths or text);
        # the diagnosis is kept here for tests and the developer.
        self.detail = detail




def base_font_files(family: str) -> dict[str, Path]:
    """Bundled ``style -> file`` map for one family, per K2's ``fonts/`` layout.

    K2's interface (`PresetApi.base_font_files`), in `scribesense.config` as K2 places it. K2 specifies
    ``base_font_files(family) -> dict[str, Path]`` raising ``BUNDLED_FONT_MISSING``, and
    records that neither it nor the ``fonts/`` tree exists yet -- M1.2 hit the same gap
    and resolved it by injection (K2 B1). M1.5 cannot: the card's interface is
    ``build_variants(config)`` with no second argument, so a default reader has to
    exist. When K2's own lands, import it here and delete this body; the signature and
    the failure code are already its.

    Styles are inferred from file names (``NotoSans-BoldItalic.ttf`` -> ``BoldItalic``,
    a stem with no ``-`` -> ``Regular``), which is the convention the four shortlisted
    upstreams use. That inference is the part K2's ``manifest.toml`` will replace, and
    it is the weaker half: the manifest *declares* the mapping instead of guessing it.
    Where two files claim one style, the first in sorted order wins -- arbitrary, but
    deterministic, which is the property that matters to the key.

    Raises `BundledFontMissing` when the family is unknown, its directory is absent, or
    it ships no Regular.
    """
    if family not in SHORTLIST:
        raise BundledFontMissing(f"{family!r} is not in SHORTLIST")

    directory = FONTS_ROOT / family
    if not directory.is_dir():
        raise BundledFontMissing(f"no bundled directory for {family!r}: {directory}")

    found: dict[str, Path] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in FONT_SUFFIXES:
            continue
        stem = path.stem
        token = stem.rsplit("-", 1)[-1] if "-" in stem else "regular"
        style = _STYLE_ALIASES.get(token.replace(" ", "").lower())
        if style is not None:
            found.setdefault(style, path)

    if REQUIRED_STYLE not in found:
        raise BundledFontMissing(f"{family!r} ships no {REQUIRED_STYLE} style")
    return found


def space_width_em(family: str) -> float:
    """The bundled Regular face's space advance, in em (K2 ``resolve``).

    ``hmtx`` advance of the glyph mapped to U+0020, divided by ``unitsPerEm``. Raises
    `BundledFontMissing` when the family is not bundled, and ``ValueError`` when the
    Regular face has no usable space glyph or ``unitsPerEm``.
    """
    from fontTools.ttLib import TTFont  # only resolve() of a target_em preset needs it

    path = base_font_files(family)["Regular"]
    with TTFont(path, lazy=True) as font:
        glyph = font.getBestCmap().get(0x20)
        units_per_em = font["head"].unitsPerEm
        if glyph is None or not units_per_em:
            raise ValueError(f"no space glyph or unitsPerEm in {path.name}")
        return font["hmtx"][glyph][0] / units_per_em
