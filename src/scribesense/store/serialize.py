"""O3 — K9 serialization: versioned JSON, fixed-decimal numbers, UTC timestamps (decisions D8, D9).

`Configuration` / `Preset` are stored as `{"v":1,…}` JSON. The six canonical numeric fields are written as
fixed-decimal STRINGS ("0.12", not 0.12000000000000001), so equal values always have equal text. SQLite has no
exact decimal type, which is why the store does not use decimal columns. Validation stays in O1's
`validate()` — this module only checks structure and refuses values that are not real numbers.

Corrupt stored data raises ScribeSenseError(STORE_CORRUPT); a value that cannot be stored raises ValueError.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.presets import Preset, PresetValues, WordSpacing
from scribesense.contracts.store import SERIALIZATION_VERSION

#: Decimals of each canonical numeric field = decimals of its K1 step (K1 RANGES).
DECIMALS: dict[str, int] = {"letter_em": 2, "word_scale": 2, "reading_line": 1, "ui_line": 1, "text_scale": 2}
#: The fields that change the generated font files. `text_scale` is deliberately absent (M1.5, decision D8).
FONT_FIELDS: tuple[str, ...] = ("base_family", "letter_em", "word_scale", "reading_line", "ui_line")


def _corrupt() -> ScribeSenseError:
    return ScribeSenseError(ErrorCode.STORE_CORRUPT)


def fixed(name: str, value: object) -> str:
    """The canonical text of a numeric field. Only real numbers (not bool, not NaN/inf) can be stored."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} is not a finite number")
    return f"{float(value):.{DECIMALS[name]}f}"


def _number(text: object) -> float:
    if not isinstance(text, str):
        raise _corrupt()
    try:
        value = float(text)
    except ValueError:
        raise _corrupt() from None
    if not math.isfinite(value):
        raise _corrupt()
    return value


def _dumps(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _loads(text: object) -> dict[str, Any]:
    try:
        obj = json.loads(text)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise _corrupt() from None
    if not isinstance(obj, dict) or obj.get("v") != SERIALIZATION_VERSION:
        raise _corrupt()
    return obj


# ---------------------------------------------------------------- Configuration

def config_to_json(c: Configuration) -> str:
    if not isinstance(c.base_family, str) or not c.base_family:
        raise ValueError("base_family must be a non-empty string")
    return _dumps({"v": SERIALIZATION_VERSION, "base_family": c.base_family,
                   **{name: fixed(name, getattr(c, name)) for name in DECIMALS}})


def config_from_json(text: object) -> Configuration:
    obj = _loads(text)
    try:
        base = obj["base_family"]
        if not isinstance(base, str) or not base:
            raise _corrupt()
        return Configuration(base_family=base, **{name: _number(obj[name]) for name in DECIMALS})
    except KeyError:
        raise _corrupt() from None


def font_identity(c: Configuration) -> tuple[object, ...]:
    """The font-building fields as compared by the font registry (D8): never `text_scale`."""
    return (c.base_family, *(fixed(n, getattr(c, n)) for n in FONT_FIELDS[1:]))


# ---------------------------------------------------------------- Preset

def _word_value(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("word value is not a finite number")
    return repr(float(value))  # shortest text that round-trips exactly; the mode says what the unit is


def preset_values_to_json(v: PresetValues) -> str:
    if v.word.mode not in ("target_em", "multiplier"):
        raise ValueError("word mode must be 'target_em' or 'multiplier'")
    if not isinstance(v.base_family, str) or not v.base_family:
        raise ValueError("base_family must be a non-empty string")
    return _dumps({
        "v": SERIALIZATION_VERSION,
        "base_family": v.base_family,
        "letter_em": fixed("letter_em", v.letter_em),
        "word": {"mode": v.word.mode, "value": _word_value(v.word.value)},
        "reading_line": fixed("reading_line", v.reading_line),
        "ui_line": fixed("ui_line", v.ui_line),
        "text_scale": fixed("text_scale", v.text_scale),
    })


def preset_values_from_json(text: object) -> PresetValues:
    obj = _loads(text)
    try:
        word = obj["word"]
        if not isinstance(word, dict) or word.get("mode") not in ("target_em", "multiplier"):
            raise _corrupt()
        base = obj["base_family"]
        if not isinstance(base, str) or not base:
            raise _corrupt()
        return PresetValues(
            base_family=base,
            letter_em=_number(obj["letter_em"]),
            word=WordSpacing(mode=word["mode"], value=_number(word["value"])),
            reading_line=_number(obj["reading_line"]),
            ui_line=_number(obj["ui_line"]),
            text_scale=_number(obj["text_scale"]),
        )
    except (KeyError, TypeError):
        raise _corrupt() from None


def preset_from_row(preset_id: str, name: str, kind: str, data: str) -> Preset:
    if kind not in ("user", "custom"):
        raise _corrupt()
    return Preset(id=preset_id, name=name, values=preset_values_from_json(data), kind=kind)  # type: ignore[arg-type]


# ---------------------------------------------------------------- arrays and timestamps

def array_to_json(items: tuple[str, ...]) -> str:
    if not all(isinstance(i, str) for i in items):
        raise ValueError("array items must be strings")
    return json.dumps(list(items), separators=(",", ":"), ensure_ascii=False)


def array_from_json(text: object) -> tuple[str, ...]:
    try:
        items = json.loads(text)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise _corrupt() from None
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        raise _corrupt()
    return tuple(items)


def ts_to_text(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware (UTC)")
    return moment.astimezone(timezone.utc).isoformat()


def ts_from_text(text: object) -> datetime:
    try:
        moment = datetime.fromisoformat(text)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise _corrupt() from None
    if moment.tzinfo is None:
        raise _corrupt()
    return moment.astimezone(timezone.utc)
