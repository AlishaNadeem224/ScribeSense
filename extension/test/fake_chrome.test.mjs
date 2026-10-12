// A hand-written fake of the chrome.* APIs the extension uses (no libraries), plus fake timers.
// Exported for the other test files; the tests at the bottom check the fake itself.

import test from "node:test";
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

function fakeEvent() {
  const listeners = [];
  return {
    listeners,
    addListener(fn) { listeners.push(fn); },
    removeListener(fn) { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); },
    emit(...args) { return listeners.map((fn) => fn(...args)); },
  };
}

/**
 * tabs: [{id, url}]; restricted: tab ids where scripting fails (like chrome:// pages);
 * storage: initial chrome.storage.local content (shared object: pass the same one to "restart").
 */
export function createFakeChrome({ tabs = [], restricted = [], storage = {}, selections = {} } = {}) {
  const fake = {
    tabsList: tabs.map((t) => ({ ...t })),
    restricted: new Set(restricted),
    store: storage,
    selections: { ...selections }, // tabId -> selected text in the page
    styles: new Map(), // tabId -> list of injected css strings (stacking is visible)
    ports: [],
    connectAttempts: [],
    menus: [],
    alerts: [], // {tabId, message}
    badge: undefined,
    hostMissing: false,
    executeScriptFails: false,
  };

  function stylesOf(tabId) {
    if (!fake.styles.has(tabId)) fake.styles.set(tabId, []);
    return fake.styles.get(tabId);
  }

  function makePort(name) {
    const port = {
      name,
      sent: [],
      closed: false,
      onMessage: fakeEvent(),
      onDisconnect: fakeEvent(),
      postMessage(message) {
        if (port.closed) throw new Error("Attempting to use a disconnected port object");
        port.sent.push(JSON.parse(JSON.stringify(message)));
      },
      disconnect() { port.closed = true; },
      // test helpers
      reply(message) { port.onMessage.emit(message); },
      drop(reason = "Native host has exited.") {
        port.closed = true;
        chromeApi.runtime.lastError = { message: reason };
        port.onDisconnect.emit(port);
        chromeApi.runtime.lastError = undefined;
      },
    };
    return port;
  }

  const chromeApi = {
    runtime: {
      lastError: undefined,
      onInstalled: fakeEvent(),
      connectNative(name) {
        fake.connectAttempts.push(name);
        const port = makePort(name);
        fake.ports.push(port);
        if (fake.hostMissing) {
          queueMicrotask(() => port.drop("Specified native messaging host not found."));
        }
        return port;
      },
    },
    scripting: {
      async insertCSS({ target, css, origin }) {
        assert.equal(origin, "USER");
        assert.equal(target.allFrames, true);
        if (fake.restricted.has(target.tabId)) throw new Error("Cannot access contents of the page");
        stylesOf(target.tabId).push(css);
      },
      async removeCSS({ target, css, origin }) {
        assert.equal(origin, "USER");
        if (fake.restricted.has(target.tabId)) throw new Error("Cannot access contents of the page");
        const list = stylesOf(target.tabId);
        const i = list.lastIndexOf(css);
        if (i >= 0) list.splice(i, 1); // like Chrome: one matching sheet per call
      },
      async executeScript({ target, func, args = [] }) {
        if (fake.executeScriptFails || fake.restricted.has(target.tabId)) {
          throw new Error("Cannot access contents of the page");
        }
        if (func.name === "selectedTextInPage") {
          return [{ frameId: 0, result: fake.selections[target.tabId] ?? "" }];
        }
        if (func.name === "showMessageInPage") {
          fake.alerts.push({ tabId: target.tabId, message: args[0] });
          return [{ frameId: 0, result: undefined }];
        }
        throw new Error(`unexpected injected function ${func.name}`);
      },
    },
    tabs: {
      onUpdated: fakeEvent(),
      async query() { return fake.tabsList.map((t) => ({ ...t })); },
    },
    storage: {
      onChanged: fakeEvent(),
      local: {
        async get(keys) {
          const out = {};
          for (const k of keys) if (k in fake.store) out[k] = JSON.parse(JSON.stringify(fake.store[k]));
          return out;
        },
        async set(items) {
          const changes = {};
          for (const [k, v] of Object.entries(items)) {
            changes[k] = { oldValue: fake.store[k], newValue: v };
            fake.store[k] = JSON.parse(JSON.stringify(v));
          }
          queueMicrotask(() => chromeApi.storage.onChanged.emit(changes, "local"));
        },
      },
    },
    contextMenus: {
      onClicked: fakeEvent(),
      create(props, callback) { fake.menus.push(props); if (callback) callback(); },
    },
    action: {
      async setBadgeText({ text }) { fake.badge = text; },
    },
  };

  fake.chrome = chromeApi;
  fake.port = () => fake.ports[fake.ports.length - 1];
  fake.allSent = () => fake.ports.flatMap((p) => p.sent);
  fake.addTab = (tab) => fake.tabsList.push({ ...tab });
  return fake;
}

/** Fake setTimeout/clearTimeout: nothing runs until the test calls fire(). */
export function createFakeTimers() {
  const timers = new Map();
  let next = 1;
  return {
    timers,
    setTimeout(fn, ms) { const id = next++; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    pendingDelays() { return [...timers.values()].map((t) => t.ms); },
    fire() {
      const due = [...timers.entries()];
      timers.clear();
      for (const [, t] of due) t.fn();
      return due.length;
    },
  };
}

/** Let promises, microtasks and the background's queue run. */
export async function settle(bg) {
  for (let i = 0; i < 6; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
    if (bg) await bg.idle();
  }
}

// Self-tests run only when this file is the test file, not when another test imports the fake.
const runDirectly = Boolean(process.argv[1]) && import.meta.url === pathToFileURL(process.argv[1]).href;

if (runDirectly) test("fake removeCSS removes one matching sheet per call, like Chrome", async () => {
  const fake = createFakeChrome({ tabs: [{ id: 1, url: "https://example.test/" }] });
  const opts = { target: { tabId: 1, allFrames: true }, css: "a{}", origin: "USER" };
  await fake.chrome.scripting.insertCSS(opts);
  await fake.chrome.scripting.insertCSS(opts);
  await fake.chrome.scripting.removeCSS(opts);
  assert.deepEqual(fake.styles.get(1), ["a{}"]);
});

if (runDirectly) test("fake port drop sets lastError only during onDisconnect", () => {
  const fake = createFakeChrome();
  const port = fake.chrome.runtime.connectNative("x");
  let seen;
  port.onDisconnect.addListener(() => { seen = fake.chrome.runtime.lastError?.message; });
  port.drop("boom");
  assert.equal(seen, "boom");
  assert.equal(fake.chrome.runtime.lastError, undefined);
  assert.throws(() => port.postMessage({}));
});
