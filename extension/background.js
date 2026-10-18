// ScribeSense browser extension: background service worker (M4.9, contract K11).
//
// Plain JavaScript, no libraries, no build step, and no network requests: the only peer is the local
// native-messaging host "com.scribesense.host", which forwards to the running ScribeSense service.
//
// Rules (K11, R2-5):
// - The app sends the CSS. This file never builds CSS; it only inserts and removes the text it got.
// - On every connect and reconnect: ask for the full confirmed style ({"v":1,"type":"get_style"}).
// - Revisions order everything: a reply older than the last applied revision is ignored.
// - Disconnected: keep the last styling, show "disconnected", retry with backoff.
// - Local Disable (chrome.storage.local "disabled"): remove styling without the app, stop injecting.
// - Page text leaves the page only on an explicit "Open in ScribeSense" click, and only to the
//   local app. More than 200 000 characters is rejected locally (A17), never truncated.
//
// Push: the native host forwards the service's "style_changed" (an ipc.subscribe_style subscription, M4.9) as
// soon as a Keep / Original is confirmed; this file then asks get_style. A push is only a hint: the get_style
// reply is what counts, and every (re)connect still fetches the full confirmed state.

export const HOST_NAME = "com.scribesense.host";
export const PROTOCOL_VERSION = 1;
export const MAX_READER_CHARS = 200000; // A17
export const MAX_MESSAGE_BYTES = 1024 * 1024; // K11, complete encoded JSON message
export const MENU_ID = "scribesense-open-in-reader";
export const ERROR_CODES = Object.freeze(["APP_NOT_RUNNING", "HOST_MISSING", "TOO_LARGE", "BAD_MESSAGE"]);
export const BACKOFF_MIN_MS = 1000;
export const BACKOFF_MAX_MS = 60000;

// Pages the extension tries to style. Others (browser pages, the web store, other extensions) are
// restricted by the browser; failures there are counted and reported, not promised.
const ELIGIBLE_URL = /^(https?|file):/i;

// Chrome's onDisconnect reasons that mean the host is not installed or not allowed for this ID.
const HOST_MISSING_REASON = /not found|forbidden|failed to start/i;

export const USER_MESSAGES = Object.freeze({
  APP_NOT_RUNNING: "ScribeSense is not running. Start ScribeSense and try again.",
  HOST_MISSING: "The ScribeSense helper is not installed for this browser.",
  TOO_LARGE: "The selection is too long for ScribeSense (more than 200,000 characters). Select a shorter part.",
  BAD_MESSAGE: "ScribeSense could not handle this request.",
});

function isObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** An error code the extension knows; anything else is reported as BAD_MESSAGE. */
export function knownCode(code) {
  return ERROR_CODES.includes(code) ? code : "BAD_MESSAGE";
}

/** Characters as the app counts them (Unicode code points), stopping early past `limit`. */
export function codePointLength(text, limit = Infinity) {
  let count = 0;
  for (const _ of text) {
    count += 1;
    if (count > limit) break;
  }
  return count;
}

export function eligible(tab) {
  return Boolean(tab) && Number.isInteger(tab.id) && typeof tab.url === "string" && ELIGIBLE_URL.test(tab.url);
}

/** A valid K11 style reply -> {revision, active, css}; anything else -> null. */
export function parseStyleReply(msg) {
  if (!isObject(msg) || msg.v !== PROTOCOL_VERSION || msg.ok !== true) return null;
  if (typeof msg.active !== "boolean" || !Number.isInteger(msg.revision)) return null;
  if (msg.active && typeof msg.css !== "string") return null;
  return { revision: msg.revision, active: msg.active, css: msg.active ? msg.css : null };
}

// Runs inside the page (chrome.scripting.executeScript), only after the user clicked the menu item.
// Reads the full selection; Chrome's info.selectionText is only the fallback.
function selectedTextInPage() {
  const el = document.activeElement;
  const textInput = /^(text|search|url|tel|email)$/i;
  if (el && (el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && textInput.test(el.type)))
      && typeof el.selectionStart === "number") {
    return el.value.slice(el.selectionStart, el.selectionEnd);
  }
  const selection = window.getSelection();
  return selection ? selection.toString() : "";
}

// Runs inside the page to show a visible message (no "notifications" permission needed).
function showMessageInPage(message) {
  window.alert(message);
}

export function createBackground(chromeApi, options = {}) {
  const setTimer = options.setTimeout ?? globalThis.setTimeout.bind(globalThis);
  const clearTimer = options.clearTimeout ?? globalThis.clearTimeout.bind(globalThis);

  const state = {
    port: null,
    status: "disconnected", // "connected" | "disconnected"; "disabled" is shown when disabled
    lastError: null, // one of ERROR_CODES or null
    disabled: false,
    style: null, // last applied confirmed style {revision, active, css}, persisted
    pending: [], // requests awaiting a reply, in order: [{type, tabId}]
    retryTimer: null,
    backoffMs: BACKOFF_MIN_MS,
    restricted: 0, // eligible-looking tabs the browser refused to style on the last update
  };

  // Every change to page styling runs through this queue, one at a time.
  let chain = Promise.resolve();
  function enqueue(task) {
    const run = chain.then(task);
    chain = run.catch(() => {});
    return run;
  }

  // --- state shown to the popup -------------------------------------------------------------------

  function shownStatus() {
    return state.disabled ? "disabled" : state.status;
  }

  async function publish() {
    const status = shownStatus();
    const badge = status === "disabled" ? "OFF" : status === "disconnected" ? "!" : "";
    try {
      await chromeApi.storage.local.set({
        state: { status, lastError: state.lastError, restricted: state.restricted },
      });
    } catch { /* storage unavailable: the badge still shows the state */ }
    try {
      await chromeApi.action.setBadgeText({ text: badge });
    } catch { /* no badge on this browser */ }
  }

  function setError(code) {
    state.lastError = knownCode(code);
    return publish();
  }

  // --- page styling --------------------------------------------------------------------------------

  // The css tabs carry when the local Disable flag is `disabled`.
  function cssWhen(disabled) {
    return !disabled && state.style && state.style.active ? state.style.css : null;
  }

  function currentCss() {
    return cssWhen(state.disabled);
  }

  function cssTarget(tabId, css) {
    return { target: { tabId, allFrames: true }, css, origin: "USER" };
  }

  async function removeFromTab(tabId, css) {
    try {
      await chromeApi.scripting.removeCSS(cssTarget(tabId, css));
    } catch { /* restricted page or tab gone */ }
  }

  // Idempotent: removing first means the same sheet is never stacked twice in one document.
  async function styleTab(tab, css) {
    if (!css || !eligible(tab)) return true;
    await removeFromTab(tab.id, css);
    try {
      await chromeApi.scripting.insertCSS(cssTarget(tab.id, css));
      return true;
    } catch {
      return false;
    }
  }

  async function updateAllTabs(oldCss, newCss) {
    let tabs = [];
    try {
      tabs = await chromeApi.tabs.query({});
    } catch {
      return;
    }
    let restricted = 0;
    for (const tab of tabs) {
      if (!eligible(tab)) continue;
      if (oldCss && oldCss !== newCss) await removeFromTab(tab.id, oldCss);
      if (newCss && !(await styleTab(tab, newCss))) restricted += 1;
    }
    state.restricted = restricted;
  }

  async function applyStyle(style) {
    if (state.style && style.revision < state.style.revision) return false; // older: ignore
    const oldCss = currentCss();
    state.style = style;
    try {
      await chromeApi.storage.local.set({ style });
    } catch { /* kept in memory */ }
    await updateAllTabs(oldCss, currentCss());
    return true;
  }

  // `wasDisabled` comes from the storage change event, not from memory: a worker that Chrome stopped
  // while idle is restarted by this very event, and its load() has already read the new value.
  async function setDisabled(disabled, wasDisabled = state.disabled) {
    state.disabled = disabled;
    if (wasDisabled === disabled) {
      await publish();
      return;
    }
    await updateAllTabs(cssWhen(wasDisabled), cssWhen(disabled));
    await publish();
    if (!disabled) requestStyle(); // re-enabled: reconcile with the app
  }

  // --- native messaging ----------------------------------------------------------------------------

  function clearRetry() {
    if (state.retryTimer !== null) {
      clearTimer(state.retryTimer);
      state.retryTimer = null;
    }
  }

  function scheduleRetry() {
    if (state.retryTimer !== null) return;
    const delay = state.backoffMs;
    state.backoffMs = Math.min(state.backoffMs * 2, BACKOFF_MAX_MS);
    state.retryTimer = setTimer(() => {
      state.retryTimer = null;
      requestStyle();
    }, delay);
  }

  function post(message, meta) {
    if (!state.port) return false;
    try {
      state.port.postMessage(message);
    } catch {
      return false;
    }
    state.pending.push(meta);
    return true;
  }

  function requestStyle() {
    if (!state.port) {
      connect(); // connect always asks for the style
      return;
    }
    if (!post({ v: PROTOCOL_VERSION, type: "get_style" }, { type: "get_style" })) scheduleRetry();
  }

  function connect() {
    clearRetry();
    let port;
    try {
      port = chromeApi.runtime.connectNative(HOST_NAME);
    } catch {
      state.port = null;
      state.status = "disconnected";
      setError("HOST_MISSING");
      scheduleRetry();
      return;
    }
    state.port = port;
    state.pending = [];
    port.onMessage.addListener((msg) => onPortMessage(port, msg));
    port.onDisconnect.addListener(() => onDisconnect(port));
    if (!post({ v: PROTOCOL_VERSION, type: "get_style" }, { type: "get_style" })) scheduleRetry();
  }

  function onDisconnect(port) {
    if (port !== state.port) return; // an old port
    const reason = chromeApi.runtime.lastError?.message ?? "";
    const lost = state.pending;
    state.port = null;
    state.pending = [];
    state.status = "disconnected";
    if (HOST_MISSING_REASON.test(reason)) state.lastError = "HOST_MISSING";
    // Styling is KEPT: nothing is removed from any tab here.
    for (const request of lost) {
      if (request.type === "open_in_reader") showMessage(request.tabId, "APP_NOT_RUNNING");
    }
    publish();
    scheduleRetry();
  }

  function onPortMessage(port, msg) {
    if (port !== state.port) return;
    if (isObject(msg) && msg.v === PROTOCOL_VERSION && msg.type === "style_changed") {
      requestStyle(); // a notification only: the reply to get_style is what counts
      return;
    }
    const request = state.pending.shift() ?? { type: "unknown" };
    if (!isObject(msg) || msg.v !== PROTOCOL_VERSION || typeof msg.ok !== "boolean") {
      setError("BAD_MESSAGE");
      return;
    }
    if (msg.ok === false) {
      const code = knownCode(msg.error);
      if (code === "APP_NOT_RUNNING") state.status = "disconnected";
      setError(code);
      if (request.type === "open_in_reader") showMessage(request.tabId, code);
      if (code === "APP_NOT_RUNNING") scheduleRetry(); // the host is up, the app is not: poll again
      return;
    }
    if (request.type === "open_in_reader") return; // delivered; the reader opens in the app
    const style = parseStyleReply(msg);
    if (style === null) {
      setError("BAD_MESSAGE");
      return;
    }
    state.status = "connected";
    state.lastError = null;
    state.backoffMs = BACKOFF_MIN_MS;
    clearRetry();
    enqueue(async () => {
      await applyStyle(style);
      await publish();
    });
  }

  // --- "Open in ScribeSense" -----------------------------------------------------------------------

  function showMessage(tabId, code) {
    if (!Number.isInteger(tabId)) return;
    const message = USER_MESSAGES[knownCode(code)];
    chromeApi.scripting
      .executeScript({ target: { tabId }, func: showMessageInPage, args: [message] })
      .catch(() => { /* restricted page: the badge and popup still show the error */ });
  }

  async function readSelection(info, tab) {
    if (tab && Number.isInteger(tab.id)) {
      try {
        const target = { tabId: tab.id };
        if (Number.isInteger(info.frameId)) target.frameIds = [info.frameId];
        const results = await chromeApi.scripting.executeScript({ target, func: selectedTextInPage });
        const text = results?.[0]?.result;
        if (typeof text === "string" && text.length > 0) return text;
      } catch { /* fall back to the browser's copy of the selection */ }
    }
    return typeof info.selectionText === "string" ? info.selectionText : "";
  }

  async function openInReader(info, tab) {
    if (info.menuItemId !== MENU_ID) return;
    const tabId = tab?.id;
    const text = await readSelection(info, tab);
    if (text.length === 0) return; // nothing selected: nothing is sent
    const message = { v: PROTOCOL_VERSION, type: "open_in_reader", text };
    if (codePointLength(text, MAX_READER_CHARS) > MAX_READER_CHARS
        || new TextEncoder().encode(JSON.stringify(message)).length > MAX_MESSAGE_BYTES) {
      await setError("TOO_LARGE");
      showMessage(tabId, "TOO_LARGE");
      return;
    }
    if (!post(message, { type: "open_in_reader", tabId })) {
      const code = state.lastError === "HOST_MISSING" ? "HOST_MISSING" : "APP_NOT_RUNNING";
      await setError(code);
      showMessage(tabId, code);
      if (!state.port) connect();
    }
  }

  function createMenu() {
    chromeApi.contextMenus.create(
      { id: MENU_ID, title: "Open in ScribeSense", contexts: ["selection"] },
      () => void chromeApi.runtime.lastError, // already exists after an update: fine
    );
  }

  // --- startup -------------------------------------------------------------------------------------

  async function load() {
    try {
      const stored = await chromeApi.storage.local.get(["disabled", "style"]);
      state.disabled = stored.disabled === true;
      const style = stored.style;
      if (isObject(style) && Number.isInteger(style.revision) && typeof style.active === "boolean"
          && (!style.active || typeof style.css === "string")) {
        state.style = { revision: style.revision, active: style.active, css: style.active ? style.css : null };
      }
    } catch { /* first run or storage unavailable */ }
  }

  function start() {
    // MV3: listeners are registered synchronously, before any await.
    chromeApi.runtime.onInstalled.addListener(createMenu);
    chromeApi.contextMenus.onClicked.addListener((info, tab) => {
      openInReader(info, tab).catch(() => {});
    });
    chromeApi.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
      if (changeInfo.status !== "loading" && changeInfo.status !== "complete") return;
      enqueue(() => styleTab(tab ?? { id: tabId }, currentCss()));
    });
    chromeApi.storage.onChanged.addListener((changes, area) => {
      if (area !== "local" || !changes.disabled) return;
      const { oldValue, newValue } = changes.disabled;
      enqueue(() => setDisabled(newValue === true, oldValue === true));
    });
    return enqueue(async () => {
      await load();
      await publish();
      connect();
    });
  }

  return {
    start,
    state,
    idle: () => chain,
    openInReader,
    requestStyle,
  };
}

// In the browser: start. Under node --test, `chrome` is undefined and the tests drive createBackground.
if (typeof chrome !== "undefined" && chrome?.runtime?.connectNative) {
  createBackground(chrome).start();
}
