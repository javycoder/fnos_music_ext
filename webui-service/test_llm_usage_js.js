"use strict";
// Test the real usage UI independently of other page DOM stubs.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const code = fs.readFileSync(path.join(__dirname, "static/app.js"), "utf8");
const section = code.slice(code.indexOf("let llmUsageOffset ="), code.indexOf("function likedNumber("));
const elements = new Map();
function $(id) {
  if (!elements.has(id)) elements.set(id, { dataset: {}, textContent: "", innerHTML: "", addEventListener(type, fn) { this[type] = fn; } });
  return elements.get(id);
}
let payload;
let fail = false;
let timerCount = 0;
const calls = [];
const doc = { hidden: false, addEventListener(type, fn) { this[type] = fn; } };
const context = vm.createContext({ $, document: doc, Date, Number, String, Math,
  api: async (url) => { calls.push(url); if (fail) throw Error("offline"); return { usage: payload }; },
  setInterval: () => { timerCount++; return 1; },
  clearInterval: (timer) => { if (timer) timerCount--; },
});
vm.runInContext(section, context);
(async () => {
  payload = { date: "20261028", total: 100, prompt: 70, completion: 30, requests: 2, dailyLimit: 10000000, status: "ok", statsStartedAt: "20261027", historyIncomplete: true,
    totalDays: 31, allTime: { total: 300, prompt: 200, completion: 100, requests: 4 },
    history: [{ date: "20261027", total: 200, prompt: 130, completion: 70, requests: 2 }] };
  await context.refreshLlmUsage();
  assert.match($("#llm-usage-line").textContent, /100/);
  assert.match($("#llm-usage-summary").textContent, /300/);
  assert.match($("#llm-usage-summary").textContent, /历史不完整/);
  assert.match($("#llm-usage-history").innerHTML, /2026-10-27/);
  assert.equal($("#llm-usage-next").disabled, false);
  $("#llm-usage-next").click();
  await context.refreshLlmUsage();
  assert.match(calls.at(-1), /offset=30/);
  assert.equal($("#llm-usage-next").disabled, true);
  fail = true;
  await context.refreshLlmUsage();
  assert.match($("#llm-usage-line").textContent, /上次成功结果/);
  assert.match($("#llm-usage-line").textContent, /100/);
  assert.match($("#llm-usage-history").innerHTML, /2026-10-27/);
  fail = false;
  payload = { status: "corrupt", statusMessage: "已保护现场", history: [] };
  await context.refreshLlmUsage();
  assert.match($("#llm-usage-line").textContent, /不能视为已用 0/);
  assert.match($("#llm-usage-summary").textContent, /不能视为零用量/);
  assert.match($("#llm-usage-history").innerHTML, /暂不可读取/);
  context.setLlmUsageActive(true);
  assert.equal(timerCount, 1);
  doc.hidden = true; doc.visibilitychange();
  assert.equal(timerCount, 0);
  doc.hidden = false; doc.visibilitychange();
  assert.equal(timerCount, 1);
  context.setLlmUsageActive(false);
  assert.equal(timerCount, 0);
  console.log("LLM usage UI tests passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
