// M4.9 background worker against the fake chrome.* API. Run: node --test 'extension/test/*.test.mjs'

import test from "node:test";
import assert from "node:assert/strict";

import {
  BACKOFF_MAX_MS, BACKOFF_MIN_MS, HOST_NAME, MAX_READER_CHARS, MENU_ID, USER_MESSAGES,
  codePointLength, createBackground, knownCode, parseStyleReply,
} from "../background.js";
import { createFakeChrome, createFakeTimers, settle } from "./fake_chrome.test.mjs";

const GET_STYLE = { v: 1, type: "get_style" };
const CSS_A = ':root :not(code) { font-family: "AccessSans-aaaa", sans-serif !important; }';
const CSS_B = ':root :not(code) { font-family: "AccessSans-bbbb", sans-serif !important; }';
const active = (revision, css) => ({ v: 1, ok: true, active: true, revision, css });
const inactive = (revision) => ({ v: 1, ok: true, active: false, revision });

const TABS = [
  { id: 1, url: "https://example.test/a" },
  { id: 2, url: "http://example.test/b" },
  { id: 3, url: "chrome://settings/" }, // never eligible
  { id: 4, url: "https://chromewebstore.example.test/" }, // eligible-looking, but the browser refuses
];

async function setup({ storage = {}, tabs = TABS, restricted = [4], selections = {} } = {}) {
  const fake = createFakeChrome({ tabs, restricted, storage, selections });
  const timers = createFakeTimers();
  const bg = createBackground(fake.chrome, { setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout });
  await bg.start();
  await settle(bg);
  return { fake, timers, bg };
}

async function connectedWith(reply, opts) {
  const env = await setup(opts);
  env.fake.port().reply(reply);
  await settle(env.bg);
  return env;
}

function stylesOf(fake, tabId) {
  return fake.styles.get(tabId) ?? [];
}

// --- connection and message shapes ----------------------------------------------------------------

test("start connects to the host and asks for the style with the exact K11 shape", async () => {
  const { fake } = await setup();
  assert.deepEqual(fake.connectAttempts, [HOST_NAME]);
  assert.equal(HOST_NAME, "com.scribesense.host");
  assert.deepEqual(fake.port().sent, [GET_STYLE]);
});

test("the context menu item is created on install for selections only", async () => {
  const { fake } = await setup();
  fake.chrome.runtime.onInstalled.emit({ reason: "install" });
  assert.deepEqual(fake.menus, [{ id: MENU_ID, title: "Open in ScribeSense", contexts: ["selection"] }]);
});

test("an active reply styles every eligible tab with origin USER and reports restricted ones", async () => {
  const { fake, bg } = await connectedWith(active(7, CSS_A));
  assert.deepEqual(stylesOf(fake, 1), [CSS_A]);
  assert.deepEqual(stylesOf(fake, 2), [CSS_A]);
  assert.deepEqual(stylesOf(fake, 3), [], "chrome:// pages are not touched");
  assert.equal(bg.state.restricted, 1);
  assert.equal(bg.state.status, "connected");
  assert.equal(fake.badge, "");
  assert.deepEqual(fake.store.state, { status: "connected", lastError: null, restricted: 1 });
  assert.deepEqual(fake.store.style, { revision: 7, active: true, css: CSS_A });
});

test("a newer style replaces the previous css instead of stacking", async () => {
  const { fake, bg } = await connectedWith(active(7, CSS_A));
  bg.requestStyle();
  fake.port().reply(active(8, CSS_B));
  await settle(bg);
  assert.deepEqual(stylesOf(fake, 1), [CSS_B]);
  assert.deepEqual(stylesOf(fake, 2), [CSS_B]);
});

test("replies with an older revision are ignored, inactive ones included", async () => {
  const { fake, bg } = await connectedWith(active(7, CSS_A));
  for (const stale of [active(6, CSS_B), inactive(5)]) {
    bg.requestStyle();
    fake.port().reply(stale);
    await settle(bg);
    assert.deepEqual(stylesOf(fake, 1), [CSS_A], `revision ${stale.revision} ignored`);
  }
  assert.equal(bg.state.style.revision, 7);
});

test("an inactive reply (Original) removes the styling", async () => {
  const { fake, bg } = await connectedWith(active(7, CSS_A));
  bg.requestStyle();
  fake.port().reply(inactive(8));
  await settle(bg);
  assert.deepEqual(stylesOf(fake, 1), []);
  assert.deepEqual(stylesOf(fake, 2), []);
  assert.deepEqual(fake.store.style, { revision: 8, active: false, css: null });
});

test("a pushed style_changed makes the extension fetch get_style", async () => {
  const { fake, bg } = await connectedWith(active(7, CSS_A));
  fake.port().reply({ v: 1, type: "style_changed", revision: 9 });
  await settle(bg);
  assert.deepEqual(fake.port().sent, [GET_STYLE, GET_STYLE]);
  assert.deepEqual(stylesOf(fake, 1), [CSS_A], "the notification itself changes nothing");
  fake.port().reply(active(9, CSS_B));
  await settle(bg);
  assert.deepEqual(stylesOf(fake, 1), [CSS_B]);
});

// --- disconnected and reconnect ------------------------------------------------------------------

test("disconnected keeps the last styling, shows the state and retries with backoff", async () => {
  const { fake, timers, bg } = await connectedWith(active(7, CSS_A));
  fake.port().drop("Native host has exited.");
  await settle(bg);
  assert.deepEqual(stylesOf(fake, 1), [CSS_A], "styling kept");
  assert.equal(bg.state.status, "disconnected");
  assert.equal(fake.badge, "!");
  assert.equal(fake.store.state.status, "disconnected");
  assert.deepEqual(timers.pendingDelays(), [BACKOFF_MIN_MS]);

  timers.fire(); // reconnect attempt
  assert.equal(fake.connectAttempts.length, 2);
  fake.port().drop("Native host has exited.");
  await settle(bg);
  assert.deepEqual(timers.pendingDelays(), [BACKOFF_MIN_MS * 2], "backoff grows");
});

test("backoff is capped", async () => {
  const { fake, timers, bg } = await setup();
  for (let i = 0; i < 12; i += 1) {
    fake.port().drop();
    await settle(bg);
    timers.fire();
  }
  fake.port().drop();
  await settle(bg);
  assert.deepEqual(timers.pendingDelays(), [BACKOFF_MAX_MS]);
});

test("reconnect always fetches the full style, even when the cached revision looks current", async () => {
  const { fake, timers, bg } = await connectedWith(active(7, CSS_A));
  fake.port().drop();
  await settle(bg);
  timers.fire();
  assert.deepEqual(fake.port().sent, [GET_STYLE], "new port asks again");
  fake.port().reply(active(7, CSS_A));
  await settle(bg);
  assert.deepEqual(stylesOf(fake, 1), [CSS_A], "reconciled without stacking");
  assert.equal(bg.state.status, "connected");
  assert.deepEqual(timers.pendingDelays(), [], "no retry once connected");
  assert.equal(bg.state.backoffMs, BACKOFF_MIN_MS, "backoff reset");
});

test("a host that is not installed is reported as HOST_MISSING", async () => {
  const fake = createFakeChrome({ tabs: TABS });
  fake.hostMissing = true;
  const timers = createFakeTimers();
  const bg = createBackground(fake.chrome, timers);
  await bg.start();
  await settle(bg);
  assert.equal(bg.state.lastError, "HOST_MISSING");
  assert.equal(fake.store.state.lastError, "HOST_MISSING");
  assert.equal(timers.pendingDelays().length, 1, "still retries");
});

test("APP_NOT_RUNNING keeps styling, marks disconnected and polls again on the same port", async () => {
  const { fake, timers, bg } = await connectedWith(active(7, CSS_A));
  bg.requestStyle();
  fake.port().reply({ v: 1, ok: false, error: "APP_NOT_RUNNING" });
  await settle(bg);
  assert.deepEqual(stylesOf(fake, 1), [CSS_A]);
  assert.equal(bg.state.status, "disconnected");
  assert.equal(fake.store.state.lastError, "APP_NOT_RUNNING");
  assert.equal(fake.badge, "!");
  timers.fire();
  assert.equal(fake.ports.length, 1, "host is alive: same port");
  assert.deepEqual(fake.port().sent, [GET_STYLE, GET_STYLE, GET_STYLE]);
});

test("the last style survives a worker restart and styles new tabs while disconnected", async () => {
  const storage = {};
  const first = await connectedWith(active(7, CSS_A), { storage });
  first.fake.port().drop();
  await settle(first.bg);

  const fake = createFakeChrome({ tabs: [], storage }); // browser restarted: tabs reload
  fake.hostMissing = true;
  const bg = createBackground(fake.chrome, createFakeTimers());
  await bg.start();
  await settle(bg);
  const tab = { id: 9, url: "https://example.test/new" };
  fake.addTab(tab);
  fake.chrome.tabs.onUpdated.emit(9, { status: "loading" }, tab);
  await settle(bg);
  assert.deepEqual(stylesOf(fake, 9), [CSS_A]);
});

// --- tabs ----------------------------------------------------------------------------------------

test("new and updated tabs get the current css once", async () => {
  const { fake, bg } = await connectedWith(active(7, CSS_A));
  const tab = { id: 5, url: "https://example.test/new" };
  fake.addTab(tab);
  fake.chrome.tabs.onUpdated.emit(5, { status: "loading" }, tab);
  fake.chrome.tabs.onUpdated.emit(5, { status: "complete" }, tab);
  fake.chrome.tabs.onUpdated.emit(5, { title: "x" }, tab);
  await settle(bg);
  assert.deepEqual(stylesOf(fake, 5), [CSS_A], "loading + complete never stack two copies");
  const page = { id: 6, url: "chrome://newtab/" };
  fake.chrome.tabs.onUpdated.emit(6, { status: "complete" }, page);
  await settle(bg);
  assert.deepEqual(stylesOf(fake, 6), []);
});

test("no styling is injected before the app has sent any css", async () => {
  const { fake, bg } = await setup();
  fake.chrome.tabs.onUpdated.emit(1, { status: "complete" }, TABS[0]);
  await settle(bg);
  assert.deepEqual(stylesOf(fake, 1), []);
});

// --- local Disable -------------------------------------------------------------------------------

test("local Disable removes styling without the app and stops injecting until re-enabled", async () => {
  const { fake, bg } = await connectedWith(active(7, CSS_A));
  fake.port().drop(); // the app does not need to be reachable
  await settle(bg);

  await fake.chrome.storage.local.set({ disabled: true });
  await settle(bg);
  assert.deepEqual(stylesOf(fake, 1), []);
  assert.deepEqual(stylesOf(fake, 2), []);
  assert.equal(fake.store.state.status, "disabled");
  assert.equal(fake.badge, "OFF");

  const tab = { id: 5, url: "https://example.test/new" };
  fake.addTab(tab);
  fake.chrome.tabs.onUpdated.emit(5, { status: "complete" }, tab);
  await settle(bg);
  assert.deepEqual(stylesOf(fake, 5), [], "no injection while disabled");

  await fake.chrome.storage.local.set({ disabled: false });
  await settle(bg);
  for (const id of [1, 2, 5]) assert.deepEqual(stylesOf(fake, id), [CSS_A], `tab ${id} restyled`);
  assert.equal(fake.store.state.status, "disconnected");
});

test("style updates while disabled are remembered but not injected", async () => {
  const { fake, bg } = await connectedWith(active(7, CSS_A), { storage: { disabled: true } });
  assert.deepEqual(stylesOf(fake, 1), []);
  assert.equal(fake.store.state.status, "disabled");
  await fake.chrome.storage.local.set({ disabled: false });
  await settle(bg);
  assert.deepEqual(stylesOf(fake, 1), [CSS_A]);
  assert.deepEqual(fake.port().sent.at(-1), GET_STYLE, "re-enable reconciles with the app");
});

// A worker that Chrome stopped while idle (host missing, port dropped) is woken by the popup's
// storage change. load() already reads the new value, so the change event itself must drive the tabs.
function wakeWorker(fake) {
  for (const event of [fake.chrome.runtime.onInstalled, fake.chrome.contextMenus.onClicked,
    fake.chrome.tabs.onUpdated, fake.chrome.storage.onChanged]) event.listeners.length = 0;
  const bg = createBackground(fake.chrome, createFakeTimers());
  const started = bg.start(); // still loading when the change event arrives
  return { bg, started };
}

test("Disable and re-enable reach open tabs when the toggle wakes a stopped worker", async () => {
  const { fake, bg: first } = await connectedWith(active(7, CSS_A));
  fake.hostMissing = true;
  fake.port().drop("Specified native messaging host not found.");
  await settle(first);
  assert.deepEqual(stylesOf(fake, 1), [CSS_A]);

  // Worker stopped; the popup writes disabled=true; Chrome starts a new worker for the event.
  fake.store.disabled = true;
  let woken = wakeWorker(fake);
  fake.chrome.storage.onChanged.emit({ disabled: { oldValue: undefined, newValue: true } }, "local");
  await woken.started;
  await settle(woken.bg);
  assert.deepEqual(stylesOf(fake, 1), [], "tab 1 cleared without the app");
  assert.deepEqual(stylesOf(fake, 2), [], "tab 2 cleared without the app");
  assert.equal(woken.bg.state.disabled, true);
  assert.equal(fake.store.state.status, "disabled");
  assert.equal(fake.badge, "OFF");

  // Stopped again; the popup writes disabled=false; the stored style comes back with no host reply.
  fake.store.disabled = false;
  woken = wakeWorker(fake);
  fake.chrome.storage.onChanged.emit({ disabled: { oldValue: true, newValue: false } }, "local");
  await woken.started;
  await settle(woken.bg);
  for (const id of [1, 2]) assert.deepEqual(stylesOf(fake, id), [CSS_A], `tab ${id} restyled once`);
  assert.equal(woken.bg.state.disabled, false);
  assert.equal(fake.store.state.status, "disconnected");
});

// --- Open in ScribeSense -------------------------------------------------------------------------

const click = (fake, overrides = {}) => fake.chrome.contextMenus.onClicked.emit(
  { menuItemId: MENU_ID, frameId: 0, selectionText: "short copy", ...overrides }, TABS[0]);

test("the selection is sent only on an explicit click of our menu item", async () => {
  const { fake, bg } = await connectedWith(active(7, CSS_A), { selections: { 1: "Line one\nLine two" } });
  assert.ok(fake.allSent().every((m) => m.type === "get_style"), "nothing but get_style before a click");
  fake.chrome.contextMenus.onClicked.emit({ menuItemId: "other", selectionText: "x" }, TABS[0]);
  await settle(bg);
  click(fake);
  await settle(bg);
  assert.deepEqual(fake.port().sent.at(-1), { v: 1, type: "open_in_reader", text: "Line one\nLine two" });
  assert.equal(fake.allSent().filter((m) => m.type === "open_in_reader").length, 1);
  fake.port().reply({ v: 1, ok: true });
  await settle(bg);
  assert.deepEqual(fake.alerts, []);
});

test("the browser's selectionText is the fallback when the page cannot be read", async () => {
  const { fake, bg } = await connectedWith(active(7, CSS_A));
  fake.executeScriptFails = true;
  click(fake);
  await settle(bg);
  assert.deepEqual(fake.port().sent.at(-1), { v: 1, type: "open_in_reader", text: "short copy" });
});

test("an empty selection sends nothing", async () => {
  const { fake, bg } = await connectedWith(active(7, CSS_A));
  click(fake, { selectionText: "" });
  await settle(bg);
  assert.ok(fake.allSent().every((m) => m.type === "get_style"));
});

test("more than 200000 characters is rejected locally with a visible message", async () => {
  const tooLong = "a".repeat(MAX_READER_CHARS + 1);
  const { fake, bg } = await connectedWith(active(7, CSS_A), { selections: { 1: tooLong } });
  click(fake);
  await settle(bg);
  assert.ok(fake.allSent().every((m) => m.type === "get_style"), "nothing sent, nothing truncated");
  assert.deepEqual(fake.alerts, [{ tabId: 1, message: USER_MESSAGES.TOO_LARGE }]);
  assert.equal(fake.store.state.lastError, "TOO_LARGE");
});

test("exactly 200000 characters (code points, like the app counts) is sent", async () => {
  const astral = "\u{1F600}".repeat(MAX_READER_CHARS); // 400000 UTF-16 units, 200000 characters
  const { fake, bg } = await connectedWith(active(7, CSS_A), { selections: { 1: astral } });
  click(fake);
  await settle(bg);
  assert.equal(fake.port().sent.at(-1).text, astral);
  assert.deepEqual(fake.alerts, []);
});

test("a selection whose encoded message exceeds 1 MiB is TOO_LARGE", async () => {
  const controls = "\u0001".repeat(MAX_READER_CHARS); // each becomes a 6-byte JSON escape
  const { fake, bg } = await connectedWith(active(7, CSS_A), { selections: { 1: controls } });
  click(fake);
  await settle(bg);
  assert.ok(fake.allSent().every((m) => m.type === "get_style"));
  assert.equal(fake.store.state.lastError, "TOO_LARGE");
});

test("clicking while disconnected shows 'not running' and sends nothing", async () => {
  const { fake, bg } = await connectedWith(active(7, CSS_A), { selections: { 1: "hello" } });
  fake.port().drop();
  await settle(bg);
  click(fake);
  await settle(bg);
  assert.deepEqual(fake.alerts, [{ tabId: 1, message: USER_MESSAGES.APP_NOT_RUNNING }]);
  assert.ok(fake.allSent().every((m) => m.type === "get_style"));
  assert.deepEqual(fake.port().sent, [GET_STYLE], "a reconnect was attempted");
});

test("an error reply to open_in_reader is shown in the page it came from", async () => {
  const { fake, bg } = await connectedWith(active(7, CSS_A), { selections: { 1: "hello" } });
  click(fake);
  await settle(bg);
  fake.port().reply({ v: 1, ok: false, error: "APP_NOT_RUNNING" });
  await settle(bg);
  assert.deepEqual(fake.alerts, [{ tabId: 1, message: USER_MESSAGES.APP_NOT_RUNNING }]);
  assert.deepEqual(stylesOf(fake, 1), [CSS_A], "styling kept");
});

// --- malformed replies ---------------------------------------------------------------------------

test("malformed or unknown replies are BAD_MESSAGE and change no styling", async () => {
  const { fake, bg } = await connectedWith(active(7, CSS_A));
  for (const bad of [null, "text", [1], { v: 2, ok: true, active: true, revision: 9, css: CSS_B },
    { v: 1, ok: true, active: true, revision: 9 }, { v: 1, ok: true, active: "yes", revision: 9 },
    { v: 1, ok: true, active: true, revision: 9.5, css: CSS_B }]) {
    bg.requestStyle();
    fake.port().reply(bad);
    await settle(bg);
    assert.equal(bg.state.lastError, "BAD_MESSAGE", JSON.stringify(bad));
  }
  assert.deepEqual(stylesOf(fake, 1), [CSS_A]);
  bg.requestStyle();
  fake.port().reply({ v: 1, ok: false, error: "SOMETHING_ELSE" });
  await settle(bg);
  assert.equal(bg.state.lastError, "BAD_MESSAGE");
});

test("helpers: parseStyleReply, knownCode, codePointLength", () => {
  assert.deepEqual(parseStyleReply(active(1, "x")), { revision: 1, active: true, css: "x" });
  assert.deepEqual(parseStyleReply(inactive(2)), { revision: 2, active: false, css: null });
  assert.equal(parseStyleReply({ v: 1, ok: false, error: "APP_NOT_RUNNING" }), null);
  assert.equal(knownCode("TOO_LARGE"), "TOO_LARGE");
  assert.equal(knownCode("whatever"), "BAD_MESSAGE");
  assert.equal(codePointLength("a\u{1F600}b"), 3);
  assert.equal(codePointLength("abcdef", 2), 3, "stops just past the limit");
});
