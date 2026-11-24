"""M1.2 -- Bundled base fonts (contract K2, owner O1).

K2: "Base fonts are BUNDLED in the repo (fonts/ + manifest.toml), never taken from the
system." This module finds them -- `base_font_files` is K2's ``PresetApi`` function --
and measures the one metric preset resolution needs, the Regular face's space width.

Moved here from `scribesense.fontgen.variants` (2026-10-15 integration, recorded in
ARCHITECTURE.md M1.2) so that K2's interface lives in `scribesense.config`, where K2 says
it is implemented; `variants` re-exports every name, so callers are unaffected.

Where the tree lives (2026-11-14, PLAN_v2 Phase III W2.1)
---------------------------------------------------------
K2 says "bundled in the repo", and until now this module took that literally: a module
constant ``FONTS_ROOT`` three parents above this file. That is right in a checkout and
wrong in an installed package, where the ``src/`` layer does not exist -- see
`_repo_fonts_root` for the arithmetic. `fonts_root` replaces the constant with a search:
an explicit override, then installed data directories, then the repository tree.

"Bundled" is unchanged as a *rule* -- the fonts still ship with ScribeSense and are never
read from the system's font directories. Only the question of where ScribeSense's own
copy sits after installation is answered differently.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

from scribesense.contracts.config import SHORTLIST
from scribesense.contracts.errors import ErrorCode, ScribeSenseError

__all__ = [
    "BUNDLED_FONTS_ENV",
    "MANIFEST_NAME",
    "OPTIONAL_STYLES",
    "REQUIRED_STYLE",
    "STYLES",
    "FONT_SUFFIXES",
    "BundledFontMissing",
    "base_font_files",
    "candidate_roots",
    "fonts_root",
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

MANIFEST_NAME = "manifest.toml"
"""K2's manifest, and the marker that a candidate directory is *our* font tree.

A candidate counts only if it carries this file. A bare ``is_dir()`` test would not do:
``fonts`` is one of the commonest directory names on a Linux system -- ``/usr/share/fonts``
exists on every machine -- and resolving to somebody else's would mean `base_font_files`
reporting "not bundled" for a directory full of fonts, which is the most confusing
failure available here.
"""

BUNDLED_FONTS_ENV = "SCRIBESENSE_BUNDLED_FONTS"
"""Environment override naming the bundled font tree directly.

Deliberately **not** ``SCRIBESENSE_FONTS_ROOT``: K8 already defines ``SCRIBESENSE_FONTS_DIR``
for the *opposite* end of the pipeline -- where generated fonts are installed for the user --
and two variables one letter apart meaning opposite things is a defect waiting to be filed.

Pending a K8 decision (O1 + O3; see PLAN_v2 2026-11-14). If O3 would rather own the
resolution, `fonts_root` moves to `contracts.paths` and this module calls it; the
name and the search order are the parts that need agreeing, not the mechanism.
"""

_PACKAGE_DATA_SUBDIR = ("scribesense", "fonts")
"""Where an installed package keeps its font tree inside a data directory."""

_DEFAULT_DATA_DIRS = os.pathsep.join(("/usr/local/share", "/usr/share"))
"""XDG's own default for ``XDG_DATA_DIRS`` when it is unset or empty.

Joined with `os.pathsep` rather than written as a literal so it survives the split in
`candidate_roots` on a development machine as well as on the target."""


def _repo_fonts_root() -> Path:
    """``<repo>/fonts`` -- the development tree, three levels above this package.

    The hop count is load-bearing and is the whole reason this module needed fixing.
    In a checkout this file is ``<repo>/src/scribesense/config/bundled.py``, so three
    parents reach the repository root. In an **installed** package there is no ``src/``
    layer -- ``uv_build`` is a src-layout backend and ships ``scribesense/`` alone -- so
    the same three parents land on the Python library directory instead
    (``/usr/lib/python3.14`` rather than ``/usr/share/scribesense``). Nothing raised;
    `base_font_files` simply found no directory and every build was refused with
    ``BUNDLED_FONT_MISSING`` (PLAN_v2 Phase III, W2.1).

    Resolved from ``__file__`` rather than the working directory, so a build behaves the
    same whatever the process was started from.
    """
    return Path(__file__).resolve().parents[3] / "fonts"


def candidate_roots() -> tuple[Path, ...]:
    """Every place the bundled font tree may live, best first.

    An explicit override is the **only** candidate when it is set. Falling through from
    one would be worse than useless in a test: a test pointing at an empty directory to
    prove a refusal would quietly find the repository's real fonts and pass for the wrong
    reason. The sandbox (M3.3) relies on the same property.

    Otherwise: installed data directories first (``$XDG_DATA_DIRS``, defaulting to XDG's
    own ``/usr/local/share:/usr/share``), then the development tree, which is PLAN_v2
    W2.1's "the repo path as the development fallback".

    Public because the search path is worth reporting: `base_font_files` names it in the
    failure detail, and `doctor` can print it. Not cached -- an override set after import
    must still take effect, which is exactly what the previous module constant could not do.
    """
    override = os.environ.get(BUNDLED_FONTS_ENV)
    if override:
        return (Path(override),)

    # `os.pathsep`, not a literal ":". The XDG specification says colon, and on the target
    # (Linux) `os.pathsep` IS a colon -- but development happens on Windows, where a colon
    # is part of every absolute path, so splitting "C:/share" on ":" yields two fragments
    # and nothing resolves. Windows has no XDG_DATA_DIRS in production, so using ";" there
    # costs nothing and keeps the tests runnable on the machine the code is written on.
    data_dirs = os.environ.get("XDG_DATA_DIRS") or _DEFAULT_DATA_DIRS
    candidates = [
        Path(part).joinpath(*_PACKAGE_DATA_SUBDIR)
        for part in data_dirs.split(os.pathsep)
        if part
    ]
    candidates.append(_repo_fonts_root())
    return tuple(candidates)


def fonts_root() -> Path:
    """Directory holding the bundled font tree: ``<Family>/*.ttf`` plus `MANIFEST_NAME`.

    A function, not a constant, for the reason every K8 path is a function: the value has
    to be re-read, so a test or the bubblewrap sandbox can redirect it after import.

    Never raises and never returns ``None``. When no candidate carries the manifest it
    returns the last one, so the caller fails through `base_font_files` with K2's
    ``BUNDLED_FONT_MISSING`` and the error contract is unchanged -- a missing font tree is
    a refusal with a code, not an exception from a path helper.
    """
    candidates = candidate_roots()
    for candidate in candidates:
        if (candidate / MANIFEST_NAME).is_file():
            return candidate
    return candidates[-1]


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




# --------------------------------------------------------------------------- #
# Imported families (K2 rule 2 + K1 rule 6, amendment 2026-11-24; W1)
# --------------------------------------------------------------------------- #

_imported_files: Callable[[str], Mapping[str, Path] | None] | None = None


def set_imported_files(provider: Callable[[str], Mapping[str, Path] | None] | None) -> None:
    """Registered by the composition root (the service): ``family -> {style: stored copy}`` for an imported family,
    or None when the family is not imported. ``config`` never imports the store (K1 rule 6). ``None`` unregisters."""
    global _imported_files
    _imported_files = provider


def _imported_font_files(family: str) -> dict[str, Path]:
    """K2 (amendment 2026-11-24): an unknown name -> BUNDLED_FONT_MISSING, as before; an imported family whose
    stored copy is gone or has no Regular -> FONT_UNREADABLE. Never reads a font in place from the system: the
    provider returns copies under K8 ``user_fonts_dir()``, and anything outside it is refused."""
    from scribesense.config.settings import imported_name_ok
    from scribesense.contracts.paths import user_fonts_dir

    provider = _imported_files
    files = provider(family) if provider is not None and imported_name_ok(family) else None
    if files is None:
        raise BundledFontMissing(f"{family!r} is neither bundled nor imported")
    root = user_fonts_dir().resolve()
    found: dict[str, Path] = {}
    for style, path in files.items():
        real = Path(path).resolve()
        if not real.is_relative_to(root) or not real.is_file():
            raise ScribeSenseError(ErrorCode.FONT_UNREADABLE)
        found[style] = real
    if REQUIRED_STYLE not in found:
        raise ScribeSenseError(ErrorCode.FONT_UNREADABLE)
    return found


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
        return _imported_font_files(family)

    directory = fonts_root() / family
    if not directory.is_dir():
        # The search path goes in the detail, not the message: a packaging failure is
        # otherwise indistinguishable from a genuinely unbundled family, and the two
        # need opposite fixes.
        searched = ", ".join(str(root) for root in candidate_roots())
        raise BundledFontMissing(
            f"no bundled directory for {family!r}: {directory} (searched: {searched})"
        )

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
