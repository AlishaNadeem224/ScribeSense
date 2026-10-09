"""K7 — Font generator API (owner O1). FROZEN 2026-10-09 · amended R2.

Rules:
1. build() is synchronous; callers run it off the UI thread. Single-flight per key; the
   install + fc-cache step is serialized.
2. build_preview() never installs: temp dir, loaded privately for the preview only; deleted when
   the preview closes.
3. remove_unused() keeps a font referenced by: the active configuration, a saved preset, an
   unfinished tx, any LocationRecord with managed_font_key/unresolved set, or any live setting
   found by inspecting supported font fields. If usage can't be determined, keep it. Runs only
   when no tx is active.
4. Partial styles allowed (e.g. no italic); only available styles are built and reported.
Family names: "AccessSans-<key>" (reading) / "AccessSansUI-<key>" (UI)  — A19.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from scribesense.contracts.config import Configuration

READING_FAMILY_PREFIX = "AccessSans-"
UI_FAMILY_PREFIX = "AccessSansUI-"


@dataclass(frozen=True)
class FontSet:
    key: str  # content key (M1.5)
    reading_family: str  # "AccessSans-<key>"
    ui_family: str  # "AccessSansUI-<key>"
    files: tuple[str, ...]  # installed file paths
    styles: tuple[str, ...]  # styles actually built, e.g. ("Regular", "Bold")


@dataclass(frozen=True)
class FontBuildResult:
    status: Literal["built", "cached", "refused", "failed"]
    fonts: FontSet | None  # None unless built/cached
    reason_code: str | None  # an ErrorCode value, e.g. "BASE_IS_MONOSPACE"


@dataclass(frozen=True)
class PreviewFonts:
    """Result of build_preview(). Shape PROVISIONAL (accepted 2026-10-09) — pending O1 confirmation.

    Family identification:
        reading_family / ui_family use the same names build() would produce for this
        configuration ("AccessSans-<key>" / "AccessSansUI-<key>"), so the preview widget asks for
        exactly the family Apply would install. They are resolvable ONLY inside ScribeSense's own
        process, through files loaded privately from temp_dir — never through the user's fontconfig.
    Files and styles:
        files   — absolute paths of the generated preview files, all inside temp_dir.
        styles  — styles actually generated, in the same order as files (partial allowed, K7 rule 4).
    Ownership and cleanup:
        temp_dir is created by build_preview() and owned by the caller (the preview screen, M2.5).
        The caller deletes temp_dir (recursively) when the preview closes or is replaced. The
        generator never reuses or shares a temp_dir between calls.
    Must never:
        install anything into fonts_dir(), run fc-cache, write fontconfig rules, or change any
        desktop setting. A preview has no effect outside ScribeSense's own window.
    """

    reading_family: str
    ui_family: str
    files: tuple[str, ...]  # absolute paths inside temp_dir
    styles: tuple[str, ...]  # same order as files
    temp_dir: str  # created by build_preview(); deleted by the caller when the preview closes


class FontGenerator(Protocol):
    """Implemented by O1 in scribesense.fontgen (M1.8)."""

    def build(self, config: Configuration) -> FontBuildResult: ...

    def build_preview(self, config: Configuration) -> PreviewFonts: ...

    def remove_unused(self) -> list[str]: ...
