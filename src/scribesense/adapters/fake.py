"""O4 — Fake adapter (M4.1): an in-memory K3 Adapter for other owners' tests.

Everything lives in a FakeWorld (a dict of locations). It never touches the real filesystem,
desktop settings or external commands, so unit tests can drive the controller, journal and
recovery logic safely.

Failure injection:
    FakeAdapter(..., fail_on={"apply": ErrorCode.ADAPTER_TIMEOUT})   -> raises before acting
    FakeAdapter(..., crash_after="apply")                            -> raises FakeCrash AFTER the write
    FakeAdapter(..., verify_ok=False)                                -> verify() reports chosen_ok=False
    world.external_edit(location, ...)                               -> simulates another program / the user
Valid fail_on / crash_after steps: detect, read_state, snapshot, plan, apply, verify, revert.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from scribesense.contracts.adapter import (
    AppliedItem,
    PlannedWrite,
    SnapshotItem,
    State,
    Target,
    VerifyResult,
    state_sha256,
)
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import FontSet

STEPS = frozenset({"detect", "read_state", "snapshot", "plan", "apply", "verify", "revert"})


class FakeCrash(BaseException):
    """Simulates the process dying mid-operation. BaseException so ordinary `except Exception`
    handlers in the code under test cannot swallow it — just like a real crash."""


@dataclass
class _Entry:
    kind: Literal["file", "key"]
    present: bool
    data: bytes | None
    value_type: str | None = None
    file_mode: int | None = None


@dataclass
class FakeWorld:
    """The fake 'desktop': location -> entry. Absent locations are simply not present."""

    _entries: dict[str, _Entry] = field(default_factory=dict)

    # ---- setup helpers (tests) ----
    def set_file(self, location: str, data: bytes | None, mode: int = 0o644) -> None:
        """data=None -> file absent; b"" -> present but empty."""
        self._entries[location] = _Entry("file", data is not None, data, None, mode if data is not None else None)

    def set_key(self, location: str, value: str | None, value_type: str = "s") -> None:
        """value=None -> key unset (not explicitly set by the user)."""
        data = None if value is None else value.encode()
        self._entries[location] = _Entry("key", value is not None, data, value_type)

    def external_edit(self, location: str, data: bytes | str | None) -> None:
        """Another program (or the user) changes a location behind ScribeSense's back."""
        entry = self._entries[location]
        raw = data.encode() if isinstance(data, str) else data
        entry.present = raw is not None
        entry.data = raw

    # ---- reads ----
    def state(self, location: str) -> State:
        entry = self._entries.get(location)
        if entry is None:
            return State("file", False, None, None, state_sha256(False, None))
        data = entry.data if entry.present else None
        return State(entry.kind, entry.present, data, entry.value_type, state_sha256(entry.present, data))

    def kind(self, location: str) -> Literal["file", "key"]:
        entry = self._entries.get(location)
        return entry.kind if entry else "file"

    def mode(self, location: str) -> int | None:
        entry = self._entries.get(location)
        return entry.file_mode if entry else None

    def snapshot_all(self) -> dict[str, str]:
        """location -> state hash, for 'is everything back to the original?' assertions."""
        return {loc: self.state(loc).sha256 for loc in self._entries}

    # ---- writes (used by FakeAdapter only) ----
    def _write(self, location: str, data: bytes | None, mode: int | None = None) -> None:
        entry = self._entries.setdefault(location, _Entry(self.kind(location), False, None))
        entry.present = data is not None
        entry.data = data
        if mode is not None:
            entry.file_mode = mode


def payload_bytes(payload: bytes | str) -> bytes:
    return payload if isinstance(payload, bytes) else payload.encode()


class FakeAdapter:
    """In-memory K3 Adapter. One adapter can expose several targets, each owning its locations."""

    def __init__(
        self,
        world: FakeWorld,
        locations: dict[str, list[str]],
        *,
        name: str = "fake",
        requires_restart: bool = False,
        must_be_closed: bool = False,
        running: frozenset[str] = frozenset(),
        fail_on: dict[str, ErrorCode] | None = None,
        crash_after: str | None = None,
        verify_ok: bool = True,
        drawn_ok: bool | None = None,
    ) -> None:
        bad = set(fail_on or {}) | ({crash_after} if crash_after else set())
        if not bad <= STEPS:
            raise ValueError(f"unknown step(s): {sorted(bad - STEPS)}")
        self.world = world
        self.name = name
        self.requires_restart = requires_restart
        self.must_be_closed = must_be_closed
        self._locations = {tid: list(locs) for tid, locs in locations.items()}
        self._running = running
        self.fail_on = dict(fail_on or {})
        self.crash_after = crash_after
        self.verify_ok = verify_ok
        self.drawn_ok = drawn_ok
        self.calls: list[tuple[str, str]] = []  # (step, location-or-target_id)
        self._snapshot_sha: dict[str, str] = {}

    # ---- helpers ----
    def _enter(self, step: str, what: str) -> None:
        self.calls.append((step, what))
        code = self.fail_on.get(step)
        if code is not None:
            raise ScribeSenseError(code)

    def _leave(self, step: str) -> None:
        if self.crash_after == step:
            raise FakeCrash(step)

    def _target(self, target_id: str) -> Target:
        return Target(self.name, target_id, target_id, target_id in self._running)

    def target_for(self, location: str) -> Target:
        for tid, locs in self._locations.items():
            if location in locs:
                return self._target(tid)
        raise KeyError(location)

    # ---- K3 Adapter ----
    def detect(self) -> list[Target]:
        self._enter("detect", "*")
        result = [self._target(tid) for tid in self._locations]
        self._leave("detect")
        return result

    def read_state(self, location: str) -> State:
        self._enter("read_state", location)
        state = self.world.state(location)
        self._leave("read_state")
        return state

    def snapshot(self, target: Target) -> list[SnapshotItem]:
        self._enter("snapshot", target.target_id)
        items = []
        for loc in self._locations[target.target_id]:
            st = self.world.state(loc)
            self._snapshot_sha[loc] = st.sha256
            items.append(
                SnapshotItem(
                    target=target,
                    kind=st.kind,
                    location=loc,
                    existed=st.present,
                    old_value=(st.data.decode() if st.kind == "key" and st.data is not None else None),
                    old_sha256=st.sha256,
                    file_mode=self.world.mode(loc) if st.kind == "file" else None,
                    value_type=st.value_type,
                )
            )
        self._leave("snapshot")
        return items

    def plan(self, target: Target, fonts: FontSet, config: Configuration) -> list[PlannedWrite]:
        self._enter("plan", target.target_id)
        planned = []
        for loc in self._locations[target.target_id]:
            if self.world.kind(loc) == "key":
                payload: bytes | str = f"'{fonts.ui_family} 11'"
            else:
                payload = (
                    f"family={fonts.reading_family}\nline={config.reading_line}\n".encode()
                )
            planned.append(PlannedWrite(target, loc, payload, state_sha256(True, payload_bytes(payload))))
        self._leave("plan")
        return planned

    def apply(self, planned: PlannedWrite) -> AppliedItem:
        self._enter("apply", planned.location)
        expected = self._snapshot_sha.get(planned.location)
        if expected is None:
            raise ValueError("apply() for a location that snapshot() did not return (K3 rule 2)")
        if self.world.state(planned.location).sha256 != expected:
            raise ScribeSenseError(ErrorCode.CHANGED_DURING_APPLY)  # K3 rule 12
        self.world._write(planned.location, payload_bytes(planned.payload))
        self._leave("apply")
        return AppliedItem(planned.target, planned.location, planned.new_sha256)

    def verify(self, target: Target, fonts: FontSet) -> VerifyResult:
        self._enter("verify", target.target_id)
        code = "OK" if self.verify_ok else ErrorCode.ADAPTER_VERIFY_FAILED.value
        result = VerifyResult(target, self.verify_ok, self.drawn_ok, code)
        self._leave("verify")
        return result

    def revert(self, item: SnapshotItem, backup: bytes | None) -> None:
        """K3 rule 7: restore the ORIGINAL state, never a new one."""
        self._enter("revert", item.location)
        if item.kind == "file":
            if item.existed:
                if backup is None:
                    raise ScribeSenseError(ErrorCode.BACKUP_MISSING)
                self.world._write(item.location, backup, item.file_mode)  # b"" is a valid empty file
            else:
                self.world._write(item.location, None)  # file didn't exist -> delete it
        else:
            if item.existed:
                self.world._write(item.location, (item.old_value or "").encode())
            else:
                self.world._write(item.location, None)  # key was unset -> unset it
        self._leave("revert")
