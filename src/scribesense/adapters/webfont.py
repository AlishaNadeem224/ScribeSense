"""O4 — Web-font copies of the reading font for the browser extension (M4.8, K11 amendment 2026-11-22).

Why: a browser may hide INSTALLED fonts from websites (Brave Shields' font protection did, on Linux, 2026-11-22:
the page fell back although our CSS named the font). Fonts a page brings itself ("web fonts") are always allowed.
So the extension asks for each style with K11 `get_font` and registers the bytes in the page as a FontFace under
the same family name; the M4.8 CSS stays exactly as it is (no url(), no @font-face).

What is served: the READING variant of an INSTALLED ScribeSense font set (`fontgen.generator.installed_sets()`),
cut to Latin (the only script the patcher spaces) with fontTools' subsetter, so one style is ~50 KB instead of
~450 KB. Layout features and name records are kept, so the copy draws exactly like the installed font. Nothing
else is ever read: the file must be `<reading family>-<style>.<ttf|otf>` inside `paths.fonts_dir()`.

Copies are cached in memory per (key, style, file size, mtime): the service is long-running and a key's files
never change after install (a new configuration is a new key).

Errors: FONT_UNAVAILABLE for an unknown style, a missing file, a file outside the fonts dir, a subset that fails,
or one larger than MAX_WEBFONT_BYTES. Logging carries codes only.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import FontSet
from scribesense.contracts.messages import MAX_WEBFONT_BYTES, WEBFONT_STYLES
from scribesense.contracts.paths import fonts_dir
from scribesense.log import log

#: Code points kept in the web copy: Basic Latin, Latin-1, Latin Extended-A/B, general punctuation (quotes,
#: dashes, ellipsis), currency signs. Other scripts fall back to the page's own fonts, unspaced, as in apps.
LATIN_RANGES: tuple[tuple[int, int], ...] = (
    (0x0020, 0x007E), (0x00A0, 0x024F), (0x2000, 0x206F), (0x20A0, 0x20CF),
)
FONT_SUFFIXES = (".ttf", ".otf")

_cache: dict[tuple[str, str, int, int], bytes] = {}


def latin_codepoints() -> list[int]:
    return [cp for lo, hi in LATIN_RANGES for cp in range(lo, hi + 1)]


def reading_file(fonts: FontSet, style: str) -> Path:
    """The installed reading-variant file for `style`; FONT_UNAVAILABLE if it is not one we installed."""
    if style not in WEBFONT_STYLES or style not in fonts.styles:
        raise ScribeSenseError(ErrorCode.FONT_UNAVAILABLE)
    root = os.path.realpath(fonts_dir())
    for name in fonts.files:
        path = Path(name)
        if path.stem != f"{fonts.reading_family}-{style}" or path.suffix.lower() not in FONT_SUFFIXES:
            continue
        real = os.path.realpath(path)
        if os.path.commonpath([root, real]) != root or not os.path.isfile(real):
            break  # never serve a file outside the generated-fonts dir
        return Path(real)
    raise ScribeSenseError(ErrorCode.FONT_UNAVAILABLE)


def subset_latin(data: bytes) -> bytes:
    """`data` cut to LATIN_RANGES, keeping layout features and names (so it draws like the original)."""
    from fontTools import subset
    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(data))
    options = subset.Options()
    options.layout_features = ["*"]
    options.name_IDs = ["*"]
    options.name_languages = ["*"]
    options.notdef_outline = True
    subsetter = subset.Subsetter(options)
    subsetter.populate(unicodes=latin_codepoints())
    subsetter.subset(font)
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def web_font(fonts: FontSet, style: str) -> bytes:
    """The Latin web-font copy of `fonts`' reading variant in `style` (cached)."""
    path = reading_file(fonts, style)
    st = path.stat()
    cache_key = (fonts.key, style, st.st_size, st.st_mtime_ns)
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        data = subset_latin(path.read_bytes())
    except Exception:  # noqa: BLE001 — a font fontTools cannot cut is simply not served
        log(ErrorCode.FONT_UNAVAILABLE, "webfont", step="subset")
        raise ScribeSenseError(ErrorCode.FONT_UNAVAILABLE) from None
    if len(data) > MAX_WEBFONT_BYTES:
        log(ErrorCode.FONT_UNAVAILABLE, "webfont", step="size")
        raise ScribeSenseError(ErrorCode.FONT_UNAVAILABLE)
    _cache[cache_key] = data
    return data


def served_styles(fonts: FontSet) -> list[str]:
    """Styles get_font can serve for this set, in WEBFONT_STYLES order (K11 StyleActive.styles)."""
    return [s for s in WEBFONT_STYLES if s in fonts.styles]
