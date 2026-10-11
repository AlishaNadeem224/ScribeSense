"""M4.15 — privacy-safe logging. Acceptance: canary text never reaches the logs."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from scribesense.contracts import paths
from scribesense.contracts.errors import ErrorCode
from scribesense.safety import logging as sslog

CANARY = "CANARY-7f3a91-do-not-log"


def lines() -> list[dict]:  # type: ignore[type-arg]
    return [json.loads(x) for x in sslog.log_path().read_text().splitlines()]


def test_writes_one_json_line_with_code_category_and_safe_fields() -> None:
    assert sslog.log(ErrorCode.ADAPTER_VERIFY_FAILED, "adapter", adapter="qt", target="qt:default", attempt=2,
                     ratio=1.5, ok=False, missing=None)
    (rec,) = lines()
    assert rec["code"] == "ADAPTER_VERIFY_FAILED" and rec["category"] == "adapter"
    assert rec["adapter"] == "qt" and rec["target"] == "qt:default" and rec["attempt"] == 2
    assert rec["ratio"] == 1.5 and rec["ok"] is False and rec["missing"] is None
    assert rec["ts"].endswith("+00:00") and "dropped" not in rec


def test_log_lives_in_logs_dir_with_private_permissions() -> None:
    sslog.log("OK", "journal")
    assert sslog.log_path() == paths.logs_dir() / "scribesense.log"
    assert oct(sslog.log_path().stat().st_mode & 0o777) == "0o600"
    assert oct(paths.logs_dir().stat().st_mode & 0o777) == "0o700"


@pytest.mark.parametrize("value", [
    "/home/alice/Documents/medical-letter.pdf",   # a path
    "Dear Dr. Smith, my results are",             # prose
    "x" * 65,                                     # too long
    "Résumé",                                      # non-ASCII text
    b"bytes",                                     # bytes
    ["list", "of", "words"],                      # containers
    {"nested": "dict"},
    float("nan"),
])
def test_content_like_values_are_dropped_not_written(value: object) -> None:
    assert sslog.log(ErrorCode.TOO_LARGE, "reader", text=value)
    (rec,) = lines()
    assert "text" not in rec and rec["dropped"] == 1


@pytest.mark.parametrize("code, category", [
    ("Some free text", "adapter"),
    ("NOT_A_REAL_CODE", "adapter"),
    (ErrorCode.TOO_LARGE, "Reader Window"),
    (ErrorCode.TOO_LARGE, "a" * 40),
])
def test_invalid_code_or_category_is_not_written(code: object, category: str) -> None:
    assert sslog.log(code, category) is False  # type: ignore[arg-type]
    assert not sslog.log_path().exists()


def test_bad_or_reserved_field_names_are_dropped() -> None:
    sslog.log(ErrorCode.BAD_MESSAGE, "ipc", **{"Window Title": "x", "code": "OVERRIDE", "ts": 1, "good": 1})
    (rec,) = lines()
    assert rec["code"] == "BAD_MESSAGE" and rec["good"] == 1 and rec["dropped"] == 3


def test_canary_never_reaches_the_logs() -> None:
    """Acceptance (M4.15): canary grep over the whole logs dir after many events -> zero hits."""
    sneaky = [CANARY, f"/tmp/{CANARY}", f"{CANARY} with spaces", CANARY.encode(), [CANARY], {"k": CANARY}]
    for i, value in enumerate(sneaky):
        sslog.log(ErrorCode.NOTHING_SELECTED, "reader", text=value, index=i)
        sslog.log(CANARY, "reader")  # as a code
        sslog.log(ErrorCode.TOO_LARGE, CANARY.lower()[:20], x=1)  # as a category
    # CANARY is identifier-shaped on purpose: it must still be refused, because `text` is not an
    # identifier field. Shape alone must never be enough.
    hits = [p for p in paths.logs_dir().rglob("*") if p.is_file() and CANARY.encode() in p.read_bytes()]
    assert hits == [], hits


def test_identifier_fields_accept_identifiers_only() -> None:
    """Strings are allowed ONLY in IDENTIFIER_FIELDS, and only identifier-shaped. Callers must never put
    user content there — that residual risk is covered by review, not by code."""
    sslog.log(ErrorCode.ADAPTER_TIMEOUT, "adapter", target="flatpak:app.zen_browser.zen",
              adapter="flatpak", reason=ErrorCode.ADAPTER_TIMEOUT, note="short-word", setting="ui_line")
    rec = lines()[0]
    assert rec["target"] == "flatpak:app.zen_browser.zen" and rec["adapter"] == "flatpak"
    assert rec["reason"] == "ADAPTER_TIMEOUT" and rec["setting"] == "ui_line"
    assert "note" not in rec and rec["dropped"] == 1  # identifier-shaped, but not an identifier field


def test_paths_in_identifier_fields_are_still_dropped() -> None:
    sslog.log(ErrorCode.PROFILE_NOT_FOUND, "adapter", target="/home/alice/.mozilla/firefox/x")
    assert "target" not in lines()[0]


def test_logging_never_raises_when_the_logs_dir_is_unusable(sandbox) -> None:  # type: ignore[no-untyped-def]
    paths.data_dir().mkdir(parents=True, exist_ok=True)
    paths.logs_dir().write_text("i am a file, not a directory")
    assert sslog.log(ErrorCode.STORE_BUSY, "store") is False  # no exception


def test_rotation_keeps_one_old_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sslog, "MAX_LOG_BYTES", 200)
    for i in range(20):
        sslog.log(ErrorCode.STORE_BUSY, "store", attempt=i)
    old = sslog.log_path().with_name("scribesense.log.1")
    assert old.exists() and sslog.log_path().exists()
    assert sslog.log_path().stat().st_size < 400
    assert sorted(p.name for p in paths.logs_dir().iterdir()) == ["scribesense.log", "scribesense.log.1"]


def test_concurrent_writers_produce_whole_lines() -> None:
    def worker(n: int) -> None:
        for i in range(50):
            sslog.log(ErrorCode.STORE_BUSY, "store", worker=n, i=i)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(lines()) == 200  # every line parses as JSON


def test_logging_module_does_not_shadow_stdlib(tmp_path: Path) -> None:
    import logging as stdlib_logging

    assert hasattr(stdlib_logging, "getLogger")
