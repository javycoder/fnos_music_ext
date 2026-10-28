/* 独立前端测试：公共歌单详情状态机、并发去重、localStorage 版本化缓存（仅成功不存失败）、TTL防延长、AbortController、显式重试等。
 * 零额外 npm 依赖，Node.js 原生 assert，不修改既有 test_app_js.js。
 */
"use strict";

const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

/* ------------------------------------------------ 最小 DOM/浏览器桩 -------- */
function stubEl() {
  const classes = new Set();
  const listeners = {};
  const attrs = new Map();
  return {
    hidden: false, textContent: "", innerHTML: "", className: "", value: "",
    checked: false, disabled: false, dataset: {}, _t: null, style: {},
    get src() { return attrs.get("src") || ""; },
    set src(v) { attrs.set("src", String(v)); },
    hasAttribute(name) { return attrs.has(name); },
    getAttribute(name) { return attrs.has(name) ? attrs.get(name) : null; },
    setAttribute(name, v) { attrs.set(name, String(v)); },
    removeAttribute(name) { attrs.delete(name); },
    classList: {
      add(...c) { c.forEach((x) => classes.add(x)); },
      remove(...c) { c.forEach((x) => classes.delete(x)); },
      toggle(c, force) {
        if (force === undefined) { classes.has(c) ? classes.delete(c) : classes.add(c); }
        else if (force) { classes.add(c); }
        else { classes.delete(c); }
      },
      contains(c) { return classes.has(c); },
    },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    appendChild(child) { (this._children = this._children || []).push(child); return child; },
    replaceChildren(...children) { this._children = children; },
    removeChild(child) { return child; },
    remove() {},
    dispatch(type, props = {}) { return Promise.all((listeners[type] || []).slice().map((fn) => fn({ type, ...props }))); },
    get options() {
      return Array.from(this.innerHTML.matchAll(/<option value="([^"]*)"/g), (match) => ({ value: match[1] }));
    },
    focus() { document.activeElement = this; },
    _resetState() {
      classes.clear();
      attrs.clear();
      this.hidden = false; this.textContent = ""; this.innerHTML = "";
      this.className = ""; this.value = ""; this.checked = false;
      this.disabled = false; this.dataset = {}; this._t = null;
    },
    querySelectorAll(sel) {
      if (!this.innerHTML) return [];
      const matches = [];
      const btnMatches = this.innerHTML.matchAll(/<button[^>]*data-public-retry="(\d+)"[^>]*>/g);
      for (const m of btnMatches) {
        matches.push({
          dataset: { publicRetry: m[1] },
          addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
        });
      }
      return matches;
    },
    get content() { return { cloneNode: () => ({ nodeName: "help-content" }) }; },
  };
}

const els = new Map();
const qsaRegistry = new Map();

global.document = {
  querySelector(sel) {
    if (!els.has(sel)) els.set(sel, stubEl());
    return els.get(sel);
  },
  querySelectorAll(sel) { return qsaRegistry.get(sel) || []; },
  createElement() { return stubEl(); },
  body: stubEl(),
};
global.window = stubEl();
const localStore = new Map();
global.localStorage = {
  getItem: (k) => (localStore.has(k) ? localStore.get(k) : null),
  setItem: (k, v) => localStore.set(k, String(v)),
  removeItem: (k) => localStore.delete(k),
  clear: () => localStore.clear(),
};
global.window.localStorage = global.localStorage;

const realSetInterval = global.setInterval;
global.setInterval = (fn, ms, ...a) => {
  const h = realSetInterval(fn, ms, ...a);
  h.unref && h.unref();
  return h;
};
const realClearInterval = global.clearInterval;
global.clearInterval = (h) => realClearInterval(h);
const realSetTimeout = global.setTimeout;
global.setTimeout = (fn, ms, ...a) => {
  const h = realSetTimeout(fn, ms, ...a);
  h.unref && h.unref();
  return h;
};

/* ------------------------------------------------ fetch 桩 ----------------- */
const defaults = new Map([
  ["/app/fnmusic-ext/api/config", { values: {} }],
  ["/app/fnmusic-ext/api/status", { version: "t", current_provider: "none", processes: {}, services: {}, lx_source: null }],
  ["/app/fnmusic-ext/api/platforms", { ok: true, enabled: [], registered: [] }],
]);
let routes = new Map();
const fetchCalls = [];

global.fetch = async (url, options = {}) => {
  const fullUrl = String(url);
  const p = fullUrl.split("?")[0];
  const item = { path: p, url: fullUrl, method: (options.method || "GET").toUpperCase(), options };
  fetchCalls.push(item);
  if (routes.has(p) && routes.get(p).length) {
    const handler = routes.get(p).shift();
    if (typeof handler === "function") {
      const res = await handler(options);
      if (res && res.__rawResponse) return res.resp;
      return { ok: true, status: 200, json: async () => res };
    }
    if (handler instanceof Error) {
      return { ok: false, status: 502, json: async () => ({ detail: handler.message }) };
    }
    return { ok: true, status: 200, json: async () => handler };
  }
  return { ok: true, status: 200, json: async () => (defaults.get(p) || { ok: true }) };
};

function enqueue(p, ...handlers) {
  if (!routes.has(p)) routes.set(p, []);
  routes.get(p).push(...handlers);
}

const tick = (ms) => new Promise((r) => realSetTimeout(r, ms));

/* ------------------------------------------------ 加载 app.js -------------- */
const src = fs.readFileSync(path.join(__dirname, "static", "app.js"), "utf8");
vm.runInThisContext(src, { filename: "app.js" });

/* ------------------------------------------------ 测试套件 ----------------- */
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

function resetTestState() {
  els.forEach((el) => el._resetState && el._resetState());
  routes = new Map();
  fetchCalls.length = 0;
  localStore.clear();
  publicDetailCache.clear();
  publicSelection = [];
  publicDetailAbortControllers.clear();
  publicDetailInflight.clear();
}

test("详情状态机：idle -> loading (仅 inflight) -> success，且 localStorage 版本化缓存 7 天", async () => {
  resetTestState();
  const browser = $("#public-playlists-browser");
  browser.hidden = false;

  const item = {
    provider: "musicbox", source: "netease", kind: "chart", id: "3001835560",
  };
  publicSelection = [item];

  enqueue("/app/fnmusic-ext/api/public-playlists/3001835560", async (opts) => {
    assert.ok(opts.signal, "fetch 必须带有 AbortSignal");
    assert.strictEqual(item._detail_status, "loading", "在请求 inflight 期间状态应为 loading");
    return {
      ok: true, data: { name: "网易云ACG动画榜", cover_url: "http://cover/1.jpg", track_count: 50 },
    };
  });

  await ensurePublicSelectionDetails();

  // 成功完成
  assert.strictEqual(item._detail_status, "success");
  assert.strictEqual(item._name, "网易云ACG动画榜");
  assert.strictEqual(item._detail_failed, false, "成功时兼容 _detail_failed === false");
  assert.ok($("#public-selected").innerHTML.includes("网易云ACG动画榜"));

  // 验证 localStorage 7 天缓存，且不含凭证
  const storedJson = global.localStorage.getItem(PUBLIC_METADATA_STORAGE_KEY);
  assert.ok(storedJson, "元数据应存入 localStorage");
  const stored = JSON.parse(storedJson);
  const cacheKey = publicEntryKey(item);
  const entry = stored[cacheKey];
  assert.ok(entry, "条目应在缓存中");
  assert.strictEqual(entry.name, "网易云ACG动画榜");
  assert.strictEqual(entry.id, "3001835560");
  assert.ok(entry.cachedAt > 0);
  assert.strictEqual(entry.token, undefined);
  assert.strictEqual(entry.cookie, undefined);
  assert.strictEqual(entry.auth, undefined);
});

test("注意不要 render 每次延长 TTL：多次 renderPublicSelected 不会修改 cachedAt", async () => {
  resetTestState();
  const item = {
    provider: "musicbox", source: "netease", kind: "chart", id: "3001795926",
    _name: "网易云ACG游戏榜", _cover_url: "", _track_count: 30,
  };
  publicSelection = [item];

  // 初次渲染，写入内存缓存
  renderPublicSelected();
  const cacheKey = publicEntryKey(item);
  const firstCachedAt = publicDetailCache.get(cacheKey).cachedAt;
  assert.ok(firstCachedAt > 0, "初次应生成 cachedAt");

  await tick(15);
  // 多次后续渲染
  renderPublicSelected();
  renderPublicSelected();

  const secondCachedAt = publicDetailCache.get(cacheKey).cachedAt;
  assert.strictEqual(firstCachedAt, secondCachedAt, "renderPublicSelected 绝不能在每次 render 时延长 TTL");
});

test("空 name 失败与缓存不当成功：200 空 name 标记为 error，提供重试，且失败不写入 localStorage 持久化", async () => {
  resetTestState();
  const browser = $("#public-playlists-browser");
  browser.hidden = false;

  const item = {
    provider: "musicbox", source: "netease", kind: "chart", id: "2809577409",
  };
  publicSelection = [item];

  enqueue("/app/fnmusic-ext/api/public-playlists/2809577409", {
    ok: true, data: { name: "   ", cover_url: "", track_count: 0 },
  });

  await ensurePublicSelectionDetails();

  assert.strictEqual(item._detail_status, "error", "空 name 应判定为 error 状态");
  assert.strictEqual(item._detail_failed, true, "兼容现有测试的 _detail_failed 断言");
  assert.ok($("#public-selected").innerHTML.includes("名称获取失败"));
  assert.ok($("#public-selected").innerHTML.includes("data-public-retry"), "失败条目必须展示重试按钮");

  // 验证失败绝不写入 localStorage
  const storedJson = global.localStorage.getItem(PUBLIC_METADATA_STORAGE_KEY);
  if (storedJson) {
    const stored = JSON.parse(storedJson);
    assert.strictEqual(stored[publicEntryKey(item)], undefined, "失败条目绝不应写入 localStorage 持久化");
  }

  // 验证空 name 不当成功：不应产生有效名称
  assert.strictEqual(Boolean(item._name), false, "空 name 不应产生有效名称");
});

test("最多 2 并发与同 key 去重", async () => {
  resetTestState();
  const browser = $("#public-playlists-browser");
  browser.hidden = false;

  const item1 = { provider: "musicbox", source: "netease", kind: "chart", id: "101" };
  const item2 = { provider: "musicbox", source: "netease", kind: "chart", id: "102" };
  const item3 = { provider: "musicbox", source: "netease", kind: "chart", id: "103" };
  const item1_dup = { provider: "musicbox", source: "netease", kind: "chart", id: "101" };

  publicSelection = [item1, item2, item3, item1_dup];

  let currentConcurrency = 0;
  let maxObservedConcurrency = 0;
  let item1Requests = 0;

  const handleWithDelay = (name, isItem1 = false) => async () => {
    currentConcurrency++;
    if (isItem1) item1Requests++;
    if (currentConcurrency > maxObservedConcurrency) {
      maxObservedConcurrency = currentConcurrency;
    }
    await tick(30);
    currentConcurrency--;
    return { ok: true, data: { name, cover_url: "", track_count: 10 } };
  };

  enqueue("/app/fnmusic-ext/api/public-playlists/101", handleWithDelay("榜单1", true));
  enqueue("/app/fnmusic-ext/api/public-playlists/102", handleWithDelay("榜单2"));
  enqueue("/app/fnmusic-ext/api/public-playlists/103", handleWithDelay("榜单3"));

  await ensurePublicSelectionDetails();

  assert.ok(maxObservedConcurrency <= 2, `最大并发应 <= 2，实际观测到: ${maxObservedConcurrency}`);
  assert.strictEqual(item1Requests, 1, "同 key 条目应合并去重，不重复发起请求");
  assert.strictEqual(item1._name, "榜单1");
  assert.strictEqual(item1_dup._name, "榜单1");
});

test("换用户与取消过期请求防串：序列递增或用户切换中止正在 inflight 的请求", async () => {
  resetTestState();
  const browser = $("#public-playlists-browser");
  browser.hidden = false;

  const itemUserA = { provider: "musicbox", source: "netease", kind: "chart", id: "999" };
  publicSelection = [itemUserA];

  let aborted = false;
  enqueue("/app/fnmusic-ext/api/public-playlists/999", async (opts) => {
    opts.signal.addEventListener("abort", () => { aborted = true; });
    await tick(50);
    return { ok: true, data: { name: "用户A的榜单" } };
  });

  const p1 = ensurePublicSelectionDetails();

  // 模拟切换用户：publicUserGuid 改变并中止活跃请求
  publicUserGuid = "user-guid-b";
  publicSelectionDetailSeq++;
  abortActivePublicDetailRequests();

  await p1;
  assert.strictEqual(aborted, true, "切换序列/换用户时应 abort 之前的请求");
  assert.notStrictEqual(itemUserA._name, "用户A的榜单", "已过期的请求结果绝不落入当前状态防串台");
});

test("capabilities 刷新不能反复清失败", async () => {
  resetTestState();
  const item = {
    provider: "musicbox", source: "netease", kind: "chart", id: "888",
    _detail_failed: true, _detail_status: "error", _cap_retried: true,
  };
  publicSelection = [item];
  const key = publicEntryKey(item);
  publicDetailCache.set(key, { failed: true, failedAt: Date.now() });

  enqueue("/app/fnmusic-ext/api/public-playlists/capabilities", {
    ok: true, sources: [{ id: "netease", playback_available: true }],
  });

  publicState.provider = "musicbox";
  await loadPublicCapabilities();

  assert.strictEqual(item._detail_failed, true, "刷新 capabilities 不能反复重置失败标志");
  assert.ok(publicDetailCache.get(key)?.failed, "缓存中的失败标记不应被反复清除");
});

test("explicit retry 可通过现有 startPreview 拉起 item provider 再重试，自动补名不启动预览", async () => {
  resetTestState();
  const browser = $("#public-playlists-browser");
  browser.hidden = false;

  const item = {
    provider: "musicbox", source: "netease", kind: "chart", id: "777",
    _detail_failed: true, _detail_status: "error",
  };
  publicSelection = [item];
  const key = publicEntryKey(item);
  publicDetailCache.set(key, { failed: true, failedAt: Date.now() });

  let previewCalled = false;
  enqueue("/app/fnmusic-ext/api/preview", () => {
    previewCalled = true;
    return { preview: true };
  });
  enqueue("/app/fnmusic-ext/api/public-playlists/777", {
    ok: true, data: { name: "重试成功的歌单" },
  });

  // 1. 自动补名不会调用 startPreview
  fetchCalls.length = 0;
  await ensurePublicSelectionDetails();
  assert.strictEqual(previewCalled, false, "自动补名绝不能启动预览");

  // 2. explicit retry (点击重试按钮) 会拉起预览并重试
  await retryPublicDetail(0);
  assert.strictEqual(previewCalled, true, "explicit retry 必须通过 startPreview 拉起预览");
  assert.strictEqual(item._name, "重试成功的歌单");
  assert.strictEqual(item._detail_failed, false);
  assert.strictEqual(item._detail_status, "success");
});

/* ------------------------------------------------ 运行测试 ----------------- */
async function run() {
  await tick(50); // 等待 boot() 启动异步流程平息
  console.log(`运行 ${tests.length} 个独立前端测试用例...`);
  let passed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log(`  PASS: ${name}`);
      passed++;
    } catch (err) {
      console.error(`  FAIL: ${name}`);
      console.error(err);
      process.exitCode = 1;
    }
  }
  console.log(`\n结果: ${passed}/${tests.length} 通过`);
  if (process.exitCode) {
    console.error("存在失败测试");
    process.exit(1);
  }
}

run();
