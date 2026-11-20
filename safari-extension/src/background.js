const { DEFAULT_BASE_URL, isModelUrl, normalizeBaseUrl, resultFor } = globalThis.B2M;

const TIMEOUT_MS = 15000;
const BADGE_CLEAR_MS = 8000;

function toastInPage(message, color) {
  const id = "b2m-sender-toast";
  const old = document.getElementById(id);
  if (old) old.remove();
  const d = document.createElement("div");
  d.id = id;
  d.textContent = message;
  d.style.cssText =
    "position:fixed;top:16px;right:16px;max-width:320px;padding:12px 16px;" +
    "z-index:2147483647;border-radius:10px;color:#fff;font:14px/1.4 -apple-system,system-ui,sans-serif;" +
    "box-shadow:0 4px 16px rgba(0,0,0,.35);background:" + color + ";";
  document.documentElement.appendChild(d);
  setTimeout(() => d.remove(), 5000);
}

async function toast(tabId, message, color) {
  try {
    await browser.scripting.executeScript({
      target: { tabId },
      func: toastInPage,
      args: [message, color],
    });
  } catch (e) {
    console.warn("toast failed", e);
  }
}

async function setBadge(tabId, text, color) {
  try {
    await browser.action.setBadgeText({ tabId, text });
    if (color) await browser.action.setBadgeBackgroundColor({ tabId, color });
  } catch (e) {
    // Badge unsupported (e.g. iOS)
  }
}

async function send(url) {
  const { baseUrl, token } = await browser.storage.local.get(["baseUrl", "token"]);
  const base = normalizeBaseUrl(baseUrl || DEFAULT_BASE_URL);
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${String(token).trim()}`;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(`${base}/api/makerworld-urls/start`, {
      method: "POST",
      headers,
      body: JSON.stringify({ urls: url }),
      signal: ctrl.signal,
    });
    return resultFor(res.status);
  } catch (e) {
    return resultFor("network");
  } finally {
    clearTimeout(timer);
  }
}

browser.action.onClicked.addListener(async (tab) => {
  if (!tab || !isModelUrl(tab.url)) {
    if (tab && tab.id != null) await toast(tab.id, "Open a MakerWorld model page first", "#d03030");
    return;
  }
  const result = await send(tab.url);
  await setBadge(tab.id, result.badge, result.color);
  setTimeout(() => setBadge(tab.id, ""), BADGE_CLEAR_MS);
  await toast(tab.id, result.message, result.color);
});

// The button stays enabled on every tab. Disabling it per tab from
// tabs.onUpdated left it switched off after the first click in Safari, until
// the page was reloaded, because Safari doesn't always give tab.url there.
// A click on a page that isn't a model page shows a toast instead.
