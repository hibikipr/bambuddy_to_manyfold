import test from "node:test";
import assert from "node:assert/strict";
import "../src/lib.js";
const { DEFAULT_BASE_URL, isModelUrl, normalizeBaseUrl, resultFor } = globalThis.B2M;

test("isModelUrl positives", () => {
  for (const u of [
    "https://makerworld.com/models/123456",
    "https://makerworld.com/en/models/123456-some-title",
    "https://makerworld.com/zh-hans/models/99?from=x#plate-1",
    "https://makerworld.com/en/models/123456/",
  ]) assert.equal(isModelUrl(u), true, u);
});

test("isModelUrl negatives", () => {
  for (const u of [
    "http://makerworld.com/models/123",
    "https://makerworld.com/models",
    "https://makerworld.com/models/",
    "https://makerworld.com/en/models/abc",
    "https://makerworld.com/en/@someuser",
    "https://makerworld.com/en/u/someuser/upload",
    "https://makerworld.com.evil.com/models/123",
    "https://evil.com/makerworld.com/models/123",
    "https://example.com/models/123",
    "not a url",
    "",
    undefined,
    null,
  ]) assert.equal(isModelUrl(u), false, String(u));
});

test("normalizeBaseUrl", () => {
  assert.equal(normalizeBaseUrl("  https://a.b/// "), "https://a.b");
  assert.equal(normalizeBaseUrl(""), DEFAULT_BASE_URL);
  assert.equal(normalizeBaseUrl("   "), DEFAULT_BASE_URL);
  assert.equal(normalizeBaseUrl(undefined), DEFAULT_BASE_URL);
  assert.equal(normalizeBaseUrl("http://x:8080/"), "http://x:8080");
});

test("resultFor branches", () => {
  assert.equal(resultFor(202).badge, "✓");
  assert.match(resultFor(202).message, /import started/);
  assert.match(resultFor(409).message, /^Busy/);
  assert.match(resultFor(400).message, /config is missing/);
  for (const s of [401, 403]) assert.match(resultFor(s).message, /Not authorised/);
  assert.match(resultFor("network").message, /Can't reach the server/);
  assert.equal(resultFor(500).message, "Server error (HTTP 500)");
  for (const s of [202, 409, 400, 401, 403, "network", 500, 418]) {
    const r = resultFor(s);
    assert.ok(r.badge.length <= 4);
    assert.match(r.color, /^#[0-9a-f]{6}$/i);
  }
});
