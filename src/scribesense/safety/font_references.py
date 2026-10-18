"""O4 — Which generated fonts are still in use (K7 rule 3): the `ReferenceSource` for `fontgen.generator.remove_unused`.

K7 rule 3 keeps a font referenced by any of five sources; this module reads all five and folds them into one
`References`:

  1. the active configuration                 Journal.active().font_key
  2. a saved preset (keeps switching fast)     each preset (built-in, custom, user) resolved, then its font key
  3. an unfinished transaction                 generated family names in its planned writes
  4. a LocationRecord with managed / unresolved  managed_font_key
  5. a live setting                            each adapter's targets, read now, scanned for generated family names

"If usage can't be determined, keep it": a source that cannot be read makes the answer incomplete
(`complete=False`), and `remove_unused` then removes nothing. An open or unfinished transaction blocks the whole
sweep (`transaction_active`).

Source 5 inspects the FONT FIELDS adapters write — the values at the locations an adapter snapshots for a target
(fontconfig rule file, gsettings font keys, qt6ct fonts, browser font prefs / CSS, Flatpak overrides) — never a
whole-file hash. It only reads (`detect`, `snapshot`, `read_state`; K3: no side effects) and never stores what it
read. Adapter errors do not escape: they make the answer incomplete.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence

from scribesense.contracts.adapter import Adapter
from scribesense.contracts.config import Configuration
from scribesense.contracts.fonts import READING_FAMILY_PREFIX, UI_FAMILY_PREFIX
from scribesense.contracts.journal import Journal
from scribesense.contracts.presets import Preset
from scribesense.fontgen.generator import References
from scribesense.safety.lock import ApplyLock

#: A generated family name: "AccessSans-<key>" / "AccessSansUI-<key>" (K7, A19), key = 12 lowercase hex (M1.5).
_FAMILY = re.compile(
    rf"(?:{re.escape(UI_FAMILY_PREFIX)}|{re.escape(READING_FAMILY_PREFIX)})([0-9a-f]{{12}})(?![0-9a-f])")


def font_keys_in(value: bytes | str | None) -> set[str]:
    """Content keys of every generated family named in a setting value or file."""
    if value is None:
        return set()
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    return set(_FAMILY.findall(text))


def bundled_font_key(config: Configuration) -> str | None:
    """The key `build(config)` would install from the bundled base fonts; None if it cannot build one at all."""
    from scribesense.config.bundled import BundledFontMissing
    from scribesense.fontgen.variants import BaseFontIneligible, plan

    try:
        return plan(config).key
    except (BundledFontMissing, BaseFontIneligible):
        return None  # build() would refuse, so no font for this preset can exist


class FontReferenceSource:
    """K7 rule 3 for `remove_unused(references=...)`. Built by the service with its journal, store presets and
    adapters; `lock` is the process's `ApplyLock` (another process holding it also blocks the sweep)."""

    def __init__(
        self,
        journal: Journal,
        adapters: Sequence[Adapter],
        *,
        presets: Callable[[], Iterable[Preset]],
        lock: ApplyLock | None = None,
        resolve: Callable[..., Configuration] | None = None,
        preset_font_key: Callable[[Configuration], str | None] = bundled_font_key,
    ) -> None:
        self._journal = journal
        self._adapters = list(adapters)
        self._presets = presets
        self._lock = lock
        self._resolve = resolve
        self._preset_font_key = preset_font_key

    def transaction_active(self) -> bool:
        """Any tx open or unfinished here, or the apply lock held by anyone: the whole sweep waits."""
        if self._journal.unfinished():
            return True
        return self._lock is not None and self._lock.is_locked()

    def references(self) -> References:
        keys: set[str] = set()
        complete = True
        for source in (self._active, self._locations, self._unfinished, self._saved_presets, self._live_settings):
            try:
                found, ok = source()
            except Exception:  # noqa: BLE001 — a source that cannot be read: usage can't be determined
                found, ok = set(), False
            keys |= found
            complete = complete and ok
        return References(frozenset(keys), complete)

    # ---------------------------------------------------------------- the five sources -> (keys, complete)

    def _active(self) -> tuple[set[str], bool]:
        active = self._journal.active()
        return ({active.font_key} if active is not None and active.font_key else set()), True

    def _locations(self) -> tuple[set[str], bool]:
        return {r.managed_font_key for r in self._journal.locations()
                if r.managed_font_key and (r.managed_sha256 is not None or r.unresolved is not None)}, True

    def _unfinished(self) -> tuple[set[str], bool]:
        keys: set[str] = set()
        for tx in self._journal.unfinished():
            for item in self._journal.items(tx):
                if item.intent is not None:
                    keys |= font_keys_in(item.intent.payload)
        return keys, True

    def _saved_presets(self) -> tuple[set[str], bool]:
        resolve = self._resolve
        if resolve is None:
            from scribesense.config.presets import resolve
        keys: set[str] = set()
        for preset in self._presets():
            key = self._preset_font_key(resolve(preset.values))  # any error -> incomplete (references())
            if key:
                keys.add(key)
        return keys, True

    def _live_settings(self) -> tuple[set[str], bool]:
        keys: set[str] = set()
        complete = True
        for adapter in self._adapters:
            try:
                for target in adapter.detect():
                    for item in adapter.snapshot(target):
                        keys |= font_keys_in(adapter.read_state(item.location).data)
            except Exception:  # noqa: BLE001 — one unreadable adapter: keep everything, still read the rest
                complete = False
        return keys, complete
