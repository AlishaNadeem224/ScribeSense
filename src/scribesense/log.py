"""O4 — Privacy-safe logging for every module (M4.15).

Lives at the package top level (not in safety/) so every module — adapters included — may import it
without breaking import rule R5 (adapters never import scribesense.safety). Decided 2026-10-12.

    from scribesense.log import log
    log(ErrorCode.ADAPTER_VERIFY_FAILED, "adapter", adapter="qt", target="qt:default", attempt=2)

Writes one JSON line per event to contracts.paths.logs_dir()/scribesense.log (dir 0700, file 0600).

Privacy by construction — content cannot get in:
  code      must be an ErrorCode (or the literal "OK"); anything else -> the event is not written.
  category  short lowercase identifier ("adapter", "journal", "ipc", ...).
  fields    names: lowercase identifiers. values: int, float, bool, None or an ErrorCode — in any field.
            STRING values only in the closed list IDENTIFIER_FIELDS (adapter, target, role, ...) and only
            if they look like an identifier ([A-Za-z0-9_.:+-], at most 64 chars: no spaces, no '/',
            no newline — patterns are applied with fullmatch, so "adapter\n" is refused).
            A string in any other field is DROPPED, however harmless it looks — shape alone cannot tell
            a short word from a document apart from an identifier.
            Callers must put only identifiers in identifier fields (reviewers check this).
            Anything refused is never written; the event records how many fields were dropped.
Logging never raises and never breaks the app: every failure returns False.
Size cap: when the file exceeds MAX_LOG_BYTES it is rotated to scribesense.log.1 (one old file kept).
Must never import GTK/Adw (A2): recovery logs too.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from scribesense.contracts import paths
from scribesense.contracts.errors import ErrorCode

LOG_NAME = "scribesense.log"
MAX_LOG_BYTES = 1024 * 1024

# Always used with fullmatch(): `re.match` + `$` would let a trailing newline through ("adapter\n").
_CATEGORY = re.compile(r"[a-z][a-z0-9_.-]{0,31}")
_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
_SAFE_STR = re.compile(r"[A-Za-z0-9_.:+-]{0,64}")
_RESERVED = frozenset({"ts", "code", "category", "dropped"})
#: The only fields that may carry strings — and only identifier-shaped ones.
IDENTIFIER_FIELDS = frozenset({"adapter", "target", "role", "state", "step", "setting", "preset", "kind", "reason"})
_VALID_CODES = frozenset(c.value for c in ErrorCode) | {"OK"}
_lock = threading.Lock()


def log_path() -> Path:
    return paths.logs_dir() / LOG_NAME


def _safe_value(name: str, value: object) -> tuple[bool, object]:
    if value is None or isinstance(value, bool):
        return True, value
    if isinstance(value, (int, float)):
        return (value == value and value not in (float("inf"), float("-inf"))), value  # no NaN/inf
    if isinstance(value, ErrorCode):
        return True, value.value
    if isinstance(value, str) and name in IDENTIFIER_FIELDS and _SAFE_STR.fullmatch(value):
        return True, value
    return False, None


def build_record(code: ErrorCode | str, category: str, fields: dict[str, object]) -> dict[str, object] | None:
    """The exact record that would be written, or None if the event is refused. Pure; for tests too."""
    code_value = code.value if isinstance(code, ErrorCode) else code
    if code_value not in _VALID_CODES or not isinstance(category, str) or not _CATEGORY.fullmatch(category):
        return None
    record: dict[str, object] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "code": code_value,
        "category": category,
    }
    dropped = 0
    for name, value in fields.items():
        ok, safe = _safe_value(name, value)
        if not ok or not _FIELD_NAME.fullmatch(name) or name in _RESERVED:
            dropped += 1
            continue
        record[name] = safe
    if dropped:
        record["dropped"] = dropped
    return record


def log(code: ErrorCode | str, category: str, /, **fields: object) -> bool:
    """Write one privacy-safe event. Returns True if written; never raises."""
    try:
        record = build_record(code, category, fields)
        if record is None:
            return False
        line = (json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
        with _lock:
            target = log_path()
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _rotate_if_needed(target)
            fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        return True
    except Exception:  # noqa: BLE001 — logging must never break the app
        return False


def _rotate_if_needed(target: Path) -> None:
    try:
        if target.stat().st_size >= MAX_LOG_BYTES:
            os.replace(target, target.with_name(LOG_NAME + ".1"))
    except FileNotFoundError:
        pass
