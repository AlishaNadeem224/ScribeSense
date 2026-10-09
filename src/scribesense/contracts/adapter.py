"""K3 — Adapter (owner O4, O4-only). FROZEN 2026-10-09 · amended R2.

Rules every adapter follows:
1.  snapshot() writes nothing; its items are journaled BEFORE apply() is called.
2.  plan() is read-only. apply() writes exactly one PlannedWrite, only to a location snapshot()
    returned for that target. The controller journals the intent between plan() and apply().
3.  Drift checks are NOT done by adapters (M4.11 does them).
4.  All paths come from K8 (contracts.paths).
5.  Errors: raise ScribeSenseError(code). Never show UI, never return None to signal failure.
    The controller maps errors to coverage — an adapter never decides coverage.
6.  detect() and snapshot() are read-only.
7.  revert() restores the ORIGINAL state: file existed -> write backup (missing -> BACKUP_MISSING);
    file didn't exist -> delete it; key -> write old_value; key was unset -> unset it.
8.  Atomic file writes: temp file in the same dir -> copy permissions -> os.replace; fsync per §4.
9.  External commands only via contracts.paths.run_cmd() with timeouts (DEFAULT_TIMEOUTS,
    overridable in the adapter's TIMEOUTS on its card).
10. Location format: files = absolute path; settings = "<schema> <key>".
11. Every comparison uses State.sha256 from read_state(). For settings, present = explicitly set by
    the user; an unset key is restored by unsetting it, never by writing its default.
12. Immediately before apply(), re-read the state; if it no longer equals the snapshot ->
    CHANGED_DURING_APPLY, nothing written for that target (best-effort race detection).
13. One owner per location; the controller rejects overlapping plans (PLAN_CONFLICT).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal, Protocol

from scribesense.contracts.config import Configuration
from scribesense.contracts.fonts import FontSet

#: K3 rule 9 — default deadlines in seconds.
DEFAULT_TIMEOUTS: dict[str, float] = {
    "gsettings": 5.0,
    "dconf": 5.0,
    "fc-match": 10.0,
    "flatpak-fc-match": 20.0,
    "fc-cache": 60.0,
    "other": 10.0,
}


def state_sha256(present: bool, data: bytes | None) -> str:
    """Canonical state hash (K3): sha256(b"absent") if not present, else sha256(b"present\\0" + data)."""
    if not present:
        return hashlib.sha256(b"absent").hexdigest()
    return hashlib.sha256(b"present\0" + (data or b"")).hexdigest()


@dataclass(frozen=True)
class Target:
    adapter: str  # "fontconfig", "gtk", "flatpak", "qt", "firefox", "chromium"
    target_id: str  # e.g. "flatpak:app.zen_browser.zen", "brave:Default"
    display_name: str  # shown in the coverage report
    running: bool  # needed for must_be_closed and the restart notice


@dataclass(frozen=True)
class State:
    """Canonical state of one location."""

    kind: Literal["file", "key"]
    present: bool  # file: exists · key: explicitly set by the user (NOT "has a schema default")
    data: bytes | None  # file: exact bytes (b"" = present but empty) · key: GVariant text
    value_type: str | None  # key only: GVariant type string, e.g. "s", "d"
    sha256: str  # = state_sha256(present, data)


@dataclass(frozen=True)
class SnapshotItem:
    target: Target
    kind: Literal["file", "key"]
    location: str  # file path, or "<schema> <key>" for settings
    existed: bool  # = state.present
    old_value: str | None  # key: GVariant text; files: None (bytes kept as backup, K4)
    old_sha256: str  # = state.sha256 (also defined when absent)
    file_mode: int | None  # file permissions to restore (files only)
    value_type: str | None


@dataclass(frozen=True)
class PlannedWrite:
    """Computed before anything is written (write-ahead intent)."""

    target: Target
    location: str
    payload: bytes | str  # exact file bytes, or the key value
    new_sha256: str  # hash of payload (canonical, see State)


@dataclass(frozen=True)
class AppliedItem:
    target: Target
    location: str
    new_sha256: str  # what we wrote


@dataclass(frozen=True)
class VerifyResult:
    target: Target
    chosen_ok: bool  # the app/config resolves to our font
    drawn_ok: bool | None  # None = no drawn check for this target (A14)
    detail_code: str


class Adapter(Protocol):
    name: str
    requires_restart: bool
    must_be_closed: bool

    def detect(self) -> list[Target]: ...

    def read_state(self, location: str) -> State: ...

    def snapshot(self, target: Target) -> list[SnapshotItem]: ...

    def plan(self, target: Target, fonts: FontSet, config: Configuration) -> list[PlannedWrite]: ...

    def apply(self, planned: PlannedWrite) -> AppliedItem: ...

    def verify(self, target: Target, fonts: FontSet) -> VerifyResult: ...

    def revert(self, item: SnapshotItem, backup: bytes | None) -> None: ...
