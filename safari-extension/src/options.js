const DEFAULT_BASE_URL = "https://modelsync.townsville.cc";
const $ = (id) => document.getElementById(id);

function normalize(s) {
  return String(s || "").trim().replace(/\/+$/, "") || DEFAULT_BASE_URL;
}

function setStatus(text, ok) {
  const el = $("status");
  el.textContent = text;
  el.style.color = ok === true ? "#2e9e4f" : ok === false ? "#d03030" : "";
}

async function load() {
  const { baseUrl, token } = await browser.storage.local.get(["baseUrl", "token"]);
  $("baseUrl").value = baseUrl || "";
  $("token").value = token || "";
}

async function save() {
  const baseUrl = $("baseUrl").value.trim() ? normalize($("baseUrl").value) : "";
  await browser.storage.local.set({ baseUrl, token: $("token").value.trim() });
  $("baseUrl").value = baseUrl;
  setStatus("Saved.", true);
}

async function test() {
  const base = normalize($("baseUrl").value);
  const token = $("token").value.trim();
  const headers = token ? { Authorization: `Bearer ${token}` } : {};
  setStatus("Testing…");
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 10000);
  try {
    const res = await fetch(`${base}/api/health`, { headers, signal: ctrl.signal });
    setStatus(res.ok ? "OK" : `HTTP ${res.status}`, res.ok);
  } catch (e) {
    setStatus("Unreachable", false);
  } finally {
    clearTimeout(timer);
  }
}

$("save").addEventListener("click", () => save().catch((e) => setStatus(String(e), false)));
$("test").addEventListener("click", test);
load().catch((e) => setStatus(String(e), false));
