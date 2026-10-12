// M4.9 popup: what it shows for each stored state.

import test from "node:test";
import assert from "node:assert/strict";

import { ERROR_TEXT, STATUS_TEXT, describe } from "../popup.js";

test("connected, no error", () => {
  const view = describe({ state: { status: "connected", lastError: null, restricted: 0 } });
  assert.equal(view.status, "connected");
  assert.equal(view.statusText, STATUS_TEXT.connected);
  assert.equal(view.enabled, true);
  assert.equal(view.errorText, "");
  assert.equal(view.restrictedText, "");
});

test("disconnected with each known error code", () => {
  for (const code of ["APP_NOT_RUNNING", "HOST_MISSING", "TOO_LARGE", "BAD_MESSAGE"]) {
    const view = describe({ state: { status: "disconnected", lastError: code } });
    assert.equal(view.status, "disconnected");
    assert.equal(view.errorCode, code);
    assert.equal(view.errorText, `${code}: ${ERROR_TEXT[code]}`);
  }
});

test("the local switch wins over the connection state", () => {
  const view = describe({ disabled: true, state: { status: "connected" } });
  assert.equal(view.status, "disabled");
  assert.equal(view.enabled, false);
});

test("missing or odd stored state falls back to disconnected without an error", () => {
  for (const stored of [undefined, {}, { state: null }, { state: { status: "weird", lastError: "toString" } }]) {
    const view = describe(stored);
    assert.equal(view.status, "disconnected");
    assert.equal(view.errorCode, null);
  }
});

test("restricted pages are reported", () => {
  assert.match(describe({ state: { status: "connected", restricted: 2 } }).restrictedText, /^2 open page/);
});
