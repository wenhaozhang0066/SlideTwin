import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { LANGS, currentLang, detectLang, keysOf, setLang, t } from "../../slidetwin/web/i18n.js";

const WEB = new URL("../../slidetwin/web/", import.meta.url);

test("every language has exactly the English keys", () => {
  assert.deepEqual(LANGS, ["en", "zh"]);
  assert.deepEqual([...keysOf("zh")].sort(), [...keysOf("en")].sort());
});

test("every key the UI uses exists", () => {
  const html = readFileSync(new URL("index.html", WEB), "utf8");
  const app = readFileSync(new URL("app.js", WEB), "utf8");
  const used = new Set([
    ...[...html.matchAll(/data-i18n(?:-label)?="([^"]+)"/g)].map((m) => m[1]),
    ...[...app.matchAll(/\bt\("([^"]+)"/g)].map((m) => m[1]),
    ...[...app.matchAll(/"((?:tool|status)\.[a-z_]+)"/g)].map((m) => m[1]),
  ]);
  const known = new Set(keysOf("en"));
  assert.ok(used.size > 40);
  assert.deepEqual([...used].filter((k) => !known.has(k)), []);
  for (const status of ["pass", "fail", "not_applicable", "not_verifiable"]) {
    assert.ok(known.has(`status.${status}`));
  }
});

test("placeholders are filled and unknown ones kept", () => {
  setLang("en");
  assert.equal(t("reader.time", { s: "3.0" }), "Time used: 3.0 s");
  assert.equal(t("reader.time"), "Time used: {s} s");
  assert.equal(t("no.such.key"), "no.such.key");
});

test("language detection and fallback", () => {
  assert.equal(detectLang("zh-CN"), "zh");
  assert.equal(detectLang("en-US"), "en");
  assert.equal(detectLang(undefined), "en");
  assert.equal(setLang("fr"), "en");
  assert.equal(setLang("zh"), "zh");
  assert.equal(currentLang(), "zh");
  assert.equal(t("report.submit"), "提交报告");
  setLang("en");
});
