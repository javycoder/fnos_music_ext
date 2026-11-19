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
    unknownUsageRequests: 3, unknownUsageByCause: { httpError: 2, missingUsage: 1, unclassified: 0 },
    lastFailure: { reason: "http-503", detail: '<img src=x onerror="alert(1)">no provider', at: 1763553170, code: "no_provider" },
    totalDays: 31,
    allTime: { total: 300, prompt: 200, completion: 100, requests: 4, unknownUsageRequests: 5 },
    history: [{ date: "20261027", total: 200, prompt: 130, completion: 70, requests: 2,
      unknownUsageRequests: 2, unknownUsageByCause: { httpError: 1, unclassified: 1 },
      lastFailure: { reason: "timeout", detail: "<b>x</b>", at: 1763466770, code: "" } }] };
  await context.refreshLlmUsage();
  assert.match($("#llm-usage-line").textContent, /100/);
  assert.match($("#llm-usage-line").textContent, /3 次用量未知/);
  assert.match($("#llm-usage-line").textContent, /未知部分未计入 token/);
  assert.match($("#llm-usage-summary").textContent, /未知部分未计入 token/);
  assert.match($("#llm-usage-line").textContent, /HTTP 错误 2/);
  assert.match($("#llm-usage-line").textContent, /响应缺用量 1/);
  assert.ok(!$("#llm-usage-line").textContent.includes("历史未分类"), "零值分类不应出现在摘要里");
  assert.match($("#llm-usage-line").textContent, /no_provider/);
  assert.match($("#llm-usage-line").textContent, /请核对模型名称、账号权限与服务商路由/);
  assert.match($("#llm-usage-summary").textContent, /300/);
  assert.match($("#llm-usage-summary").textContent, /历史不完整/);
  assert.match($("#llm-usage-summary").textContent, /不等于服务商账单/);
  assert.match($("#llm-usage-history").innerHTML, /2026-10-27/);
  assert.match($("#llm-usage-history").innerHTML, /2 次（HTTP 错误 1、历史未分类 1）/);
  assert.match($("#llm-usage-history").innerHTML, /最近失败（该日最后一次）/);
  assert.match($("#llm-usage-history").innerHTML, /timeout/);
  assert.ok(!$("#llm-usage-history").innerHTML.includes("<img"), "历史详情必须转义后写入");
  assert.ok(!$("#llm-usage-history").innerHTML.includes("<b>x</b>"), "历史详情必须转义后写入");
  assert.match($("#llm-usage-history").innerHTML, /&lt;b&gt;x&lt;\/b&gt;/);
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
