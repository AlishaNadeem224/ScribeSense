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
// Web fonts (K11 amendment 2026-11-22): a browser may hide INSTALLED fonts from websites (Brave Shields), so the
// page silently falls back although our CSS names the font. An active style therefore names its font key and
// styles; this file fetches each style once with get_font (a Latin web-font copy, ~50 KB), keeps it in
// chrome.storage.local, and registers it in every page as a FontFace under the same family name BEFORE the CSS
// goes in. The CSS itself is unchanged: it never links to or embeds a font file.
//
// Push: the native host forwards the service's "style_changed" (an ipc.subscribe_style subscription, M4.9) as
// soon as a Keep / Original is confirmed; this file then asks get_style. A push is only a hint: the get_style
// reply is what counts, and every (re)connect still fetches the full confirmed state.

export const HOST_NAME = "com.scribesense.host";
export const PROTOCOL_VERSION = 1;
export const MAX_READER_CHARS = 200000; // A17
export const MAX_MESSAGE_BYTES = 1024 * 1024; // K11, complete encoded JSON message
export const MENU_ID = "scribesense-open-in-reader";
export const ERROR_CODES = Object.freeze(["APP_NOT_RUNNING", "HOST_MISSING", "TOO_LARGE", "BAD_MESSAGE", "FONT_UNAVAILABLE"]);
export const WEBFONT_STYLES = Object.freeze(["Regular", "Bold", "Italic", "BoldItalic"]);
const FONT_KEY = /^[0-9a-f]{12}$/;
const READING_FAMILY = /^AccessSans-[0-9a-f]{12}$/;
const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/;
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
  FONT_UNAVAILABLE: "ScribeSense could not send its font to this page.",
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
  return Boolean(tab) && Number.isInteger(tab.id) && typeof tab.url === "string" && ELIGIBLE_URL.test(tab.url)
    && tab.discarded !== true && tab.status !== "unloaded"; // unloaded tabs are styled when they load
}

/** A valid K11 style reply -> {revision, active, css, fontKey, styles}; anything else -> null.
 *  fontKey/styles are null/[] when the reply has none (an older app): the CSS is still applied. */
export function parseStyleReply(msg) {
  if (!isObject(msg) || msg.v !== PROTOCOL_VERSION || msg.ok !== true) return null;
  if (typeof msg.active !== "boolean" || !Number.isInteger(msg.revision)) return null;
  if (msg.active && typeof msg.css !== "string") return null;
  const hasFonts = msg.active && typeof msg.font_key === "string" && FONT_KEY.test(msg.font_key)
    && Array.isArray(msg.styles) && msg.styles.every((s) => WEBFONT_STYLES.includes(s));
  return {
    revision: msg.revision, active: msg.active, css: msg.active ? msg.css : null,
    fontKey: hasFonts ? msg.font_key : null, styles: hasFonts ? [...new Set(msg.styles)] : [],
  };
}

/** A valid K11 get_font reply for the request {key, style} -> {style, family, data}; anything else -> null. */
export function parseFontReply(msg, request) {
  if (!isObject(msg) || msg.v !== PROTOCOL_VERSION || msg.ok !== true) return null;
  if (msg.key !== request.key || msg.style !== request.style) return null;
  if (typeof msg.family !== "string" || !READING_FAMILY.test(msg.family) || !msg.family.endsWith(request.key)) return null;
  if (typeof msg.data !== "string" || msg.data.length === 0 || !BASE64.test(msg.data)) return null;
  return { style: msg.style, family: msg.family, data: msg.data };
}

// Runs inside the page, in the PAGE's own world (chrome.scripting.executeScript world "MAIN"): registers the
// web-font copies as FontFaces of the document, once per document. Fonts a page brings itself are always allowed.
function registerFontsInPage(faces) {
  const done = (globalThis.__scribesenseFonts ||= new Set());
  for (const f of faces) {
    const id = `${f.family}|${f.style}`;
    if (done.has(id)) continue;
    const bytes = Uint8Array.from(atob(f.data), (c) => c.charCodeAt(0));
    const descriptors = {
      weight: f.style.startsWith("Bold") ? "700" : "400",
      style: f.style.endsWith("Italic") ? "italic" : "normal",
    };
    const face = new FontFace(f.family, bytes, descriptors);
    document.fonts.add(face);
    face.load().catch(() => {});
    done.add(id);
  }
  return true;
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
    style: null, // last applied confirmed style {revision, active, css, fontKey, styles}, persisted
    fonts: null, // web-font copies of style.fontKey: {key, faces: {style: {style, family, data}}}, persisted
    pending: [], // requests awaiting a reply, in order: [{type, tabId}]
    retryTimer: null,
    backoffMs: BACKOFF_MIN_MS,
    restricted: 0, // eligible-looking tabs the browser refused to style on the last update
    restrictedReason: null, // the browser's message for the last refusal (shown nowhere; for diagnosis)
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
        state: { status, lastError: state.lastError, restricted: state.restricted,
          restrictedReason: state.restrictedReason },
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

  // The web-font copies for the current style, ready to register (empty until they arrived).
  function currentFaces() {
    const style = state.style;
    if (!style || !style.active || !style.fontKey || !state.fonts || state.fonts.key !== style.fontKey) return [];
    return style.styles.map((s) => state.fonts.faces[s]).filter(Boolean);
  }

  async function registerFonts(tabId, faces) {
    if (faces.length === 0) return;
    try {
      await chromeApi.scripting.executeScript({
        target: { tabId, allFrames: true }, world: "MAIN", func: registerFontsInPage, args: [faces],
      });
    } catch { /* restricted page: the CSS still asks for the installed font */ }
  }

  // Idempotent: removing first means the same sheet is never stacked twice in one document.
  async function styleTab(tab, css) {
    if (!css || !eligible(tab)) return true;
    await registerFonts(tab.id, currentFaces()); // before the CSS, so the first paint can already use it
    await removeFromTab(tab.id, css);
    try {
      await chromeApi.scripting.insertCSS(cssTarget(tab.id, css));
      return true;
    } catch (error) {
      state.restrictedReason = String(error?.message ?? error).slice(0, 160);
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

  // Web fonts arrived: register them in every page that carries our CSS (text redraws when they load).
  async function registerAllTabs() {
    const faces = currentFaces();
    if (faces.length === 0 || !currentCss()) return;
    let tabs = [];
    try {
      tabs = await chromeApi.tabs.query({});
    } catch {
      return;
    }
    for (const tab of tabs) {
      if (eligible(tab)) await registerFonts(tab.id, faces);
    }
  }

  // Ask for every style of the active font set that is not stored yet (one get_font per style, K11).
  function requestFonts() {
    const style = state.style;
    if (!style || !style.active || !style.fontKey) return;
    if (!state.fonts || state.fonts.key !== style.fontKey) state.fonts = { key: style.fontKey, faces: {} };
    for (const s of style.styles) {
      if (state.fonts.faces[s]) continue;
      post({ v: PROTOCOL_VERSION, type: "get_font", key: style.fontKey, style: s },
        { type: "get_font", key: style.fontKey, style: s });
    }
  }

  async function storeFont(request, face) {
    if (!state.fonts || state.fonts.key !== request.key) return; // a newer style replaced this key meanwhile
    state.fonts.faces[face.style] = face;
    try {
      await chromeApi.storage.local.set({ fonts: state.fonts });
    } catch { /* kept in memory */ }
    await registerAllTabs();
  }

  async function applyStyle(style) {
    if (state.style && style.revision < state.style.revision) return false; // older: ignore
    const oldCss = currentCss();
    state.style = style;
    try {
      await chromeApi.storage.local.set({ style });
    } catch { /* kept in memory */ }
    await updateAllTabs(oldCss, currentCss());
    requestFonts();
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
    if (request.type === "get_font") {
      // A missing web font is not an error the user must see: the CSS still names the installed font.
      if (msg.ok === false && knownCode(msg.error) === "APP_NOT_RUNNING") {
        state.status = "disconnected";
        publish();
        scheduleRetry();
        return;
      }
      const face = msg.ok === true ? parseFontReply(msg, request) : null;
      if (face !== null) enqueue(() => storeFont(request, face));
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
      const stored = await chromeApi.storage.local.get(["disabled", "style", "fonts"]);
      state.disabled = stored.disabled === true;
      const style = stored.style;
      if (isObject(style) && Number.isInteger(style.revision) && typeof style.active === "boolean"
          && (!style.active || typeof style.css === "string")) {
        const fontKey = typeof style.fontKey === "string" && FONT_KEY.test(style.fontKey) ? style.fontKey : null;
        const styles = fontKey && Array.isArray(style.styles)
          ? style.styles.filter((s) => WEBFONT_STYLES.includes(s)) : [];
        state.style = { revision: style.revision, active: style.active, css: style.active ? style.css : null,
          fontKey, styles };
      }
      const fonts = stored.fonts;
      if (isObject(fonts) && typeof fonts.key === "string" && FONT_KEY.test(fonts.key) && isObject(fonts.faces)) {
        const faces = {};
        for (const [s, f] of Object.entries(fonts.faces)) {
          if (parseFontReply({ v: PROTOCOL_VERSION, ok: true, key: fonts.key, ...f }, { key: fonts.key, style: s })) {
            faces[s] = { style: s, family: f.family, data: f.data };
          }
        }
        state.fonts = { key: fonts.key, faces };
      }
    } catch { /* first run or storage unavailable */ }
  }

  function start() {
    // MV3: listeners are registered synchronously, before any await.
    chromeApi.runtime.onInstalled.addListener(createMenu);
    // The open native port keeps this worker busy, so the browser would hold a new version back until it restarts.
    // Reload as soon as one is ready; start() then reconnects and restyles every tab (2026-11-22).
    chromeApi.runtime.onUpdateAvailable?.addListener(() => chromeApi.runtime.reload());
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
