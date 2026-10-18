"""M4.15 — identifier checks match the WHOLE string (O3 review note (1), 2026-10-18).

`re.match` with `$` accepts a trailing newline ("adapter\\n"); log.py uses fullmatch. The main logging
tests live in test_logging.py.
"""

from __future__ import annotations

import pytest

from scribesense import log as sslog
from scribesense.contracts.errors import ErrorCode


@pytest.mark.parametrize("value", ["adapter\n", "qt:default\n", "adapter\r\n", "\nadapter"])
def test_identifier_field_with_a_newline_is_dropped(value: str) -> None:
    rec = sslog.build_record(ErrorCode.ADAPTER_VERIFY_FAILED, "adapter", {"adapter": value})
    assert rec is not None
    assert "adapter" not in rec and rec["dropped"] == 1


def test_category_with_a_trailing_newline_is_refused() -> None:
    assert sslog.build_record("OK", "adapter\n", {}) is None
    assert sslog.log("OK", "adapter\n") is False


def test_field_name_with_a_trailing_newline_is_dropped() -> None:
    rec = sslog.build_record("OK", "adapter", {"attempt\n": 2})
    assert rec is not None
    assert "attempt\n" not in rec and rec["dropped"] == 1


def test_plain_identifiers_are_still_written() -> None:
    rec = sslog.build_record("OK", "adapter", {"adapter": "qt", "target": "qt:default", "attempt": 2})
    assert rec is not None
    assert rec["adapter"] == "qt" and rec["target"] == "qt:default" and "dropped" not in rec
