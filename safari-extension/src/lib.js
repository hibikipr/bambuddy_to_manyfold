(() => {
const DEFAULT_BASE_URL = "https://modelsync.townsville.cc";

const MODEL_RE = /^\/(?:[A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*\/)?models\/\d+(?:[/-].*)?$/;

function isModelUrl(url) {
  if (typeof url !== "string") return false;
  let u;
  try {
    u = new URL(url);
  } catch {
    return false;
  }
  if (u.protocol !== "https:" || u.hostname !== "makerworld.com") return false;
  return MODEL_RE.test(u.pathname);
}

function normalizeBaseUrl(s) {
  const t = String(s ?? "").trim().replace(/\/+$/, "");
  return t || DEFAULT_BASE_URL;
}

const GREEN = "#2e9e4f";
const ORANGE = "#e08a00";
const RED = "#d03030";

function resultFor(status) {
  if (status === 202) {
    return { badge: "\u2713", color: GREEN, message: "Sent to Manyfold sync \u2014 import started" };
  }
  if (status === 409) {
    return { badge: "BUSY", color: ORANGE, message: "Busy: another sync is running, try again shortly" };
  }
  if (status === 400) {
    return { badge: "\u2717", color: RED, message: "Server config is missing (open the web GUI settings)" };
  }
  if (status === 401 || status === 403) {
    return { badge: "\u2717", color: RED, message: "Not authorised \u2014 check the API token in the extension settings" };
  }
  if (status === "network") {
    return { badge: "\u2717", color: RED, message: "Can't reach the server \u2014 are you on the home network or VPN?" };
  }
  return { badge: "\u2717", color: RED, message: `Server error (HTTP ${status})` };
}

// Classic script: Safari ignores "type": "module" for background scripts.
globalThis.B2M = { DEFAULT_BASE_URL, isModelUrl, normalizeBaseUrl, resultFor };
})();
