"""O4 — gsettings read probe for the GTK adapter (M4.3), run as `python -P -m scribesense.adapters.gsettings_probe`.

Why a fresh process: a long-running process caches its GSettings backend. With the keyfile backend (the test
sandbox) Gio returned a STALE value right after `gsettings set` until the GLib main context was iterated
(O3 CI finding, 2026-10-15). Draining the main context inside a running GTK app could fire unrelated UI
callbacks at the wrong moment, so every read happens in a new process instead: it loads the backend fresh,
follows GSETTINGS_BACKEND exactly like production, and needs no main loop. (Same pattern as pango_probe.)

    argv:   <schema> [<key> ...]
    stdout: {"v": 1, "schema": true|false,
             "values": {"<key>": {"type": "s", "user": "<GVariant text>"|null, "user_value": ...,
                                  "effective": "<GVariant text>", "value": ...}}}
    exit:   0 ok (also when the schema is missing: "schema": false) · 2 bad arguments · 1 any other failure.
On failure nothing is printed (no traceback that could echo values). Imports only Gio/GLib — never GTK (A2).
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

PROTOCOL_VERSION = 1
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BAD_ARGS = 2

_SCHEMA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$")
_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def valid_args(argv: list[str]) -> bool:
    if not argv or not _SCHEMA.fullmatch(argv[0]):
        return False
    keys = argv[1:]
    return len(keys) <= 32 and all(_KEY.fullmatch(k) for k in keys) and len(set(keys)) == len(keys)


def _load_gio() -> Any:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio

    return Gio


def read(schema: str, keys: list[str], Gio: Any) -> dict[str, Any]:
    source = Gio.SettingsSchemaSource.get_default()
    found = source.lookup(schema, True) if source is not None else None
    if found is None:  # never construct Gio.Settings for a missing schema: it aborts the process
        return {"v": PROTOCOL_VERSION, "schema": False, "values": {}}
    settings = Gio.Settings.new(schema)
    values: dict[str, Any] = {}
    for key in keys:
        if not found.has_key(key):
            raise KeyError(key)
        user = settings.get_user_value(key)
        effective = settings.get_value(key)
        values[key] = {
            "type": effective.get_type_string(),
            "user": None if user is None else user.print_(False),
            "user_value": None if user is None else user.unpack(),
            "effective": effective.print_(False),
            "value": effective.unpack(),
        }
    return {"v": PROTOCOL_VERSION, "schema": True, "values": values}


def main(argv: list[str]) -> int:
    if not valid_args(argv):
        return EXIT_BAD_ARGS
    try:
        result = read(argv[0], argv[1:], _load_gio())
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 — fail closed, silently: the caller maps a non-zero exit to an error
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
