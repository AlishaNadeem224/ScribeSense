// ScribeSense popup (M4.9): connection state, the local Disable switch, and the last error code.
// It only reads and writes chrome.storage.local; the background worker reacts to "disabled".

export const STATUS_TEXT = Object.freeze({
  connected: "Connected to ScribeSense.",
  disconnected: "Not connected to ScribeSense. The last reading style stays on pages.",
  disabled: "Disabled in this browser. Pages show their own fonts.",
});

export const ERROR_TEXT = Object.freeze({
  APP_NOT_RUNNING: "ScribeSense is not running.",
  HOST_MISSING: "The ScribeSense helper is not installed for this browser.",
  TOO_LARGE: "The last selection was too long (more than 200,000 characters).",
  BAD_MESSAGE: "ScribeSense sent or received a message it could not read.",
});

/** What the popup shows for a stored state. Pure, so node --test can check it. */
export function describe(stored) {
  const state = stored && typeof stored.state === "object" && stored.state !== null ? stored.state : {};
  const disabled = stored?.disabled === true;
  const status = disabled ? "disabled" : (STATUS_TEXT[state.status] ? state.status : "disconnected");
  const code = Object.hasOwn(ERROR_TEXT, state.lastError) ? state.lastError : null;
  const restricted = Number.isInteger(state.restricted) && state.restricted > 0 ? state.restricted : 0;
  return {
    status,
    statusText: STATUS_TEXT[status],
    enabled: !disabled,
    errorCode: code,
    errorText: code ? `${code}: ${ERROR_TEXT[code]}` : "",
    restrictedText: restricted
      ? `${restricted} open page(s) could not be styled (the browser does not allow it there).`
      : "",
  };
}

function render(doc, view) {
  const status = doc.getElementById("status");
  status.textContent = view.statusText;
  status.dataset.status = view.status;
  doc.getElementById("enabled").checked = view.enabled;
  const error = doc.getElementById("error");
  error.textContent = view.errorText;
  error.hidden = view.errorText === "";
  const restricted = doc.getElementById("restricted");
  restricted.textContent = view.restrictedText;
  restricted.hidden = view.restrictedText === "";
}

async function init(chromeApi, doc) {
  const refresh = async () => {
    try {
      render(doc, describe(await chromeApi.storage.local.get(["state", "disabled"])));
    } catch {
      render(doc, describe({}));
    }
  };
  doc.getElementById("enabled").addEventListener("change", (event) => {
    chromeApi.storage.local.set({ disabled: !event.target.checked }).catch(() => {});
  });
  chromeApi.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && (changes.state || changes.disabled)) refresh();
  });
  await refresh();
}

if (typeof document !== "undefined" && typeof chrome !== "undefined" && chrome?.storage?.local) {
  init(chrome, document);
}
