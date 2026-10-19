/* static/app.js 的 Node 行为测试 —— 零 npm 依赖，node 原生 assert。
 *
 * 背景：musicbox CLI 的 auth 接口返回 {ok, data:{...}} 信封结构（与
 * netease_login.sh / proxy/tests/test_musicbox_service.py 的契约一致），
 * 前端 pollQr/checkQrStatus 必须从 data.code 取扫码状态码。
 * 本文件用最小 DOM/fetch 桩直接加载 app.js 驱动真实函数，防止信封解析回归。
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
  return {
    hidden: false, textContent: "", innerHTML: "", className: "", value: "",
    checked: false, disabled: false, src: "", dataset: {}, _t: null,
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
    removeChild(child) { return child; },
    remove() {},
    // 测试用：手动触发某类事件监听器（真实 DOM 的 input/change 事件在此不可用）
    dispatch(type, props = {}) { (listeners[type] || []).slice().forEach((fn) => fn({ type, ...props })); },
    focus() { document.activeElement = this; },
    // 测试用：重置标量状态但保留元素身份与监听器——app.js 在加载期把监听器
    // 绑到元素上，元素必须终身同一个对象（真实 DOM 亦如此），不能整体换新
    _resetState() {
      classes.clear();
      this.hidden = false; this.textContent = ""; this.innerHTML = "";
      this.className = ""; this.value = ""; this.checked = false;
      this.disabled = false; this.src = ""; this.dataset = {}; this._t = null;
    },
    querySelectorAll() { return []; },
  };
}

const els = new Map();
// querySelectorAll 注册表：app.js 的 $$ 只在个别选择器上需要多元素（如 .nav-dot），
// 未注册的选择器返回 []（保持既有用例行为）
const qsaRegistry = new Map();

global.document = {
  querySelector(sel) {
    if (!els.has(sel)) els.set(sel, stubEl());
    return els.get(sel);
  },
  querySelectorAll(sel) { return qsaRegistry.get(sel) || []; },
  createElement() {
    // 关于页日志导出用的 <a download> 桩：记录点击与 download 文件名
    const el = stubEl();
    el._clicked = 0;
    el.click = () => { el._clicked += 1; };
    return el;
  },
  body: stubEl(),
};
global.window = stubEl();
// 日志导出的 blob 下载链路（URL.createObjectURL + a[download]）
const createdUrls = [];
global.URL.createObjectURL = (blob) => { createdUrls.push(blob); return `blob:fake-${createdUrls.length}`; };
global.URL.revokeObjectURL = () => {};
global.createdUrls = createdUrls;

// 定时器：unref 避免挡住进程退出；记录句柄开关状态供停表断言
let openIntervals = 0;
const realSetInterval = global.setInterval;
global.setInterval = (fn, ms, ...a) => {
  const h = realSetInterval(fn, ms, ...a);
  h.unref && h.unref();
  h._open = true;
  openIntervals += 1;
  return h;
};
const realClearInterval = global.clearInterval;
global.clearInterval = (h) => {
  if (h && h._open) { h._open = false; openIntervals -= 1; }
  return realClearInterval(h);
};
const realSetTimeout = global.setTimeout;
global.setTimeout = (fn, ms, ...a) => {
  const h = realSetTimeout(fn, ms, ...a);
  h.unref && h.unref();
  return h;
};

/* ------------------------------------------------ fetch 桩（可编排队列）---- */
const defaults = new Map([
  ["/app/fnmusic-ext/api/config", { values: {} }],
  ["/app/fnmusic-ext/api/status", { version: "t", current_provider: "none", processes: {}, services: {}, lx_source: null }],
  ["/app/fnmusic-ext/api/platforms", { ok: true, enabled: [], registered: [] }],
]);
let routes = new Map(); // path -> bodies 队列（Error 表示 HTTP 非 2xx）
const fetchCalls = [];  // 断言用：{path, method, body}
global.fetchCalls = fetchCalls;

function enqueue(p, ...bodies) {
  if (!routes.has(p)) routes.set(p, []);
  routes.get(p).push(...bodies);
}

global.fetch = async (url, options = {}) => {
  const p = String(url).split("?")[0];
  fetchCalls.push({ path: p, method: (options.method || "GET").toUpperCase(), body: options.body });
  if (routes.has(p) && routes.get(p).length) {
    const body = routes.get(p).shift();
    if (body && body.__rawResponse) { return body.resp; }
    if (body instanceof Error) {
      return { ok: false, status: 502, json: async () => ({ detail: body.message }) };
    }
    return { ok: true, status: 200, json: async () => body };
  }
  return { ok: true, status: 200, json: async () => (defaults.get(p) || { ok: true }) };
};

function reset() {
  els.forEach((el) => el._resetState && el._resetState());
  routes = new Map();
  fetchCalls.length = 0;
}

const tick = (ms) => new Promise((r) => realSetTimeout(r, ms));

/* ------------------------------------------------ 加载 app.js -------------- */
const src = fs.readFileSync(path.join(__dirname, "static", "app.js"), "utf8");
vm.runInThisContext(src, { filename: "app.js" });

for (const fn of ["pollQr", "checkQrStatus", "startQrLogin", "syncNeteaseAccount", "stopQrPolling"]) {
  assert.strictEqual(typeof global[fn], "function", `app.js 应在全局暴露 ${fn}`);
}

/* ------------------------------------------------ 用例 --------------------- */
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("checkQrStatus：信封 data.code 各状态分支文案", async () => {
  const cases = [
    [801, "等待扫码…"],
    [802, "已扫码，请在手机上确认"],
    [800, "二维码已过期，请重新生成"],
  ];
  for (const [code, text] of cases) {
    reset();
    enqueue("/app/fnmusic-ext/api/netease/auth/login/check", { ok: true, data: { code, message: "x" } });
    await global.checkQrStatus("K1");
    assert.strictEqual(els.get("#qr-status").textContent, text);
  }
});

test("checkQrStatus：803 信封 → 登录成功并同步账号昵称", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/netease/auth/login/check", { ok: true, data: { code: 803, cookie: "c" } });
  enqueue("/app/fnmusic-ext/api/netease/auth/status", { ok: true, data: { logged_in: true, nickname: "小明", user_id: 1 } });
  await global.checkQrStatus("K1");
  await tick(5); // 等 syncNeteaseAccount 的异步 fetch 落定
  assert.ok(els.get("#qr-status").textContent.includes("登录成功"));
  assert.strictEqual(els.get("#qr-check").textContent, "当前登录：小明");
});

test("checkQrStatus：803 扁平 {code} 结构兼容", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/netease/auth/login/check", { code: 803 });
  enqueue("/app/fnmusic-ext/api/netease/auth/status", { ok: true, data: { logged_in: true, user_id: 42 } });
  await global.checkQrStatus("K1");
  await tick(5);
  assert.ok(els.get("#qr-status").textContent.includes("登录成功"));
  assert.strictEqual(els.get("#qr-check").textContent, "当前登录：42"); // 无昵称回退 user_id
});

test("checkQrStatus：803 停止轮询定时器", async () => {
  reset();
  const before = openIntervals;
  global.pollQr("K1"); // 默认 2000ms，测试期内不会自然触发
  assert.strictEqual(openIntervals, before + 1);
  enqueue("/app/fnmusic-ext/api/netease/auth/login/check", { ok: true, data: { code: 803 } });
  enqueue("/app/fnmusic-ext/api/netease/auth/status", { ok: true, data: { logged_in: false } });
  await global.checkQrStatus("K1");
  await tick(5);
  assert.strictEqual(openIntervals, before); // 803 后停表
});

test("startQrLogin：信封 data.unikey 出码并启动轮询", async () => {
  reset();
  const before = openIntervals;
  enqueue("/app/fnmusic-ext/api/netease/auth/status", { ok: true, data: { logged_in: false } });
  enqueue("/app/fnmusic-ext/api/netease/auth/login", { ok: true, data: { unikey: "KEY9", qr_url: "u" } });
  await global.startQrLogin();
  assert.strictEqual(els.get("#qr-img").src, "/app/fnmusic-ext/api/netease/qr?unikey=KEY9");
  assert.strictEqual(els.get("#qr-img").hidden, false);
  assert.strictEqual(els.get("#qr-status").textContent, "请用手机网易云音乐 App 扫码");
  assert.strictEqual(openIntervals, before + 1); // 轮询已启动
  global.stopQrPolling();
});

test("startQrLogin：上游不可达 → 生成失败文案", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/netease/auth/login", new Error("musicbox 服务不可达"), new Error("musicbox 服务不可达"));
  await global.startQrLogin();
  assert.ok(els.get("#qr-status").textContent.startsWith("生成失败："));
  assert.strictEqual(els.get("#qr-img").hidden, true);
});

test("syncNeteaseAccount：未登录清空 / 请求失败清空", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/netease/auth/status", { ok: true, data: { logged_in: false } });
  await global.syncNeteaseAccount();
  assert.strictEqual(els.get("#qr-check").textContent, "");
  reset();
  enqueue("/app/fnmusic-ext/api/netease/auth/status", new Error("down"));
  await global.syncNeteaseAccount();
  assert.strictEqual(els.get("#qr-check").textContent, "");
});

test("大模型：接口完整也默认不勾选，隐藏参数保留缺省值", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: {
    FNMUSIC_LLM_BASE_URL: "https://example.test/v1", FNMUSIC_LLM_API_KEY: "secret",
  } });
  await global.loadConfig();
  assert.strictEqual(els.get("#llm-liked").checked, false);
  assert.strictEqual(els.get("#llm-daily").checked, false);
  assert.strictEqual(els.get("#llm-liked-settings").hidden, true);
  assert.strictEqual(els.get("#llm-api-warning").hidden, true);
  assert.strictEqual(els.get("#liked-refresh-hours").value, "0.5");
  assert.strictEqual(els.get("#liked-size").value, "20");
  const values = global.collectConfig();
  assert.strictEqual(values.FNMUSIC_LLM_LIKED_ENABLED, false);
  assert.strictEqual(values.FNMUSIC_LLM_DAILY_ENABLED, false);
  assert.ok(!("FNMUSIC_LIKED_SIZE" in values));
});

test("大模型：已存空白参数不在回填时静默恢复默认", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: {
    FNMUSIC_LLM_LIKED_ENABLED: "true", FNMUSIC_LIKED_SIZE: "", FNMUSIC_LIKED_REFRESH_HOURS: "",
  } });
  await global.loadConfig();
  assert.strictEqual(els.get("#liked-size").value, "");
  assert.strictEqual(els.get("#liked-refresh-hours").value, "");
  assert.throws(() => global.collectConfig(), /必须是/);
});

test("大模型：取消猜你喜欢隐藏但不清空，保存不覆盖已存参数", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: {
    FNMUSIC_LLM_LIKED_ENABLED: "true", FNMUSIC_LIKED_SIZE: "0", FNMUSIC_LIKED_REFRESH_HOURS: "100000.25",
  } });
  await global.loadConfig();
  assert.strictEqual(els.get("#llm-liked-settings").hidden, false);
  assert.strictEqual(global.collectConfig().FNMUSIC_LIKED_SIZE, 0);
  assert.strictEqual(global.collectConfig().FNMUSIC_LIKED_REFRESH_HOURS, 100000.25);
  els.get("#llm-liked").checked = false;
  els.get("#llm-liked").dispatch("change");
  assert.strictEqual(els.get("#llm-liked-settings").hidden, true);
  assert.strictEqual(els.get("#liked-size").value, "0");
  assert.strictEqual(els.get("#liked-refresh-hours").value, "100000.25");
  els.get("#liked-size").value = "";
  els.get("#liked-refresh-hours").value = "invalid";
  const values = global.collectConfig();
  assert.ok(!("FNMUSIC_LIKED_SIZE" in values));
  assert.ok(!("FNMUSIC_LIKED_REFRESH_HOURS" in values));
  await global.saveConfig();
  const saved = JSON.parse(fetchCalls.find((c) => c.method === "PUT").body).values;
  assert.strictEqual(saved.FNMUSIC_LLM_LIKED_ENABLED, false);
  assert.ok(!("FNMUSIC_LIKED_SIZE" in saved));
  assert.ok(!("FNMUSIC_LIKED_REFRESH_HOURS" in saved));
});

test("大模型：任一用途显式勾选且接口不完整才警告，输入即时更新", async () => {
  for (const flag of ["#llm-liked", "#llm-daily"]) {
    reset();
    await global.loadConfig();
    els.get(flag).checked = true;
    els.get(flag).dispatch("change");
    assert.strictEqual(els.get("#llm-api-warning").hidden, false);
    assert.strictEqual(els.get("#save-bar").classList.contains("show"), true);
    els.get("#llm-base").value = "https://example.test/v1";
    els.get("#llm-base").dispatch("input");
    assert.strictEqual(els.get("#llm-api-warning").hidden, false);
    els.get("#llm-key").value = "secret";
    els.get("#llm-key").dispatch("input");
    assert.strictEqual(els.get("#llm-api-warning").hidden, true);
    els.get("#llm-base").value = "  ";
    els.get("#llm-base").dispatch("input");
    assert.strictEqual(els.get("#llm-api-warning").hidden, false);
    els.get(flag).checked = false;
    els.get(flag).dispatch("change");
    assert.strictEqual(els.get("#llm-api-warning").hidden, true);
  }
});

test("猜你喜欢：有限小时无上限，整数数量端点与小数小时完整提交", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: { FNMUSIC_LLM_LIKED_ENABLED: "true" } });
  await global.loadConfig();
  for (const hours of ["0.5", "0.51", "1.75", "100000.25", "1e100"]) {
    for (const size of ["0", "20", "100"]) {
      els.get("#liked-refresh-hours").value = hours;
      els.get("#liked-size").value = size;
      const values = global.collectConfig();
      assert.strictEqual(values.FNMUSIC_LIKED_REFRESH_HOURS, Number(hours));
      assert.strictEqual(values.FNMUSIC_LIKED_SIZE, Number(size));
    }
  }
});

test("猜你喜欢：无效编辑不截断、不静默恢复默认、不发 PUT", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: { FNMUSIC_LLM_LIKED_ENABLED: "true" } });
  await global.loadConfig();
  for (const [selector, badValues] of [
    ["#liked-size", ["", " ", "20.9", "20junk", "-1", "101", "NaN", "Infinity"]],
    ["#liked-refresh-hours", ["", " ", "0", "0.49", "-1", "0.5junk", "NaN", "Infinity", "1e9999"]],
  ]) {
    for (const bad of badValues) {
      els.get("#liked-size").value = "20";
      els.get("#liked-refresh-hours").value = "0.5";
      els.get(selector).value = bad;
      assert.throws(() => global.collectConfig(), /必须是/);
      await global.saveConfig();
      assert.ok(els.get("#toast").textContent.includes("必须是"));
      assert.strictEqual(fetchCalls.some((c) => c.method === "PUT"), false);
    }
  }
});

test("猜你喜欢参数输入标脏，每日用途不影响每日总开关", async () => {
  reset();
  await global.loadConfig();
  for (const selector of ["#liked-size", "#liked-refresh-hours"]) {
    global.clearDirty();
    els.get(selector).dispatch("input");
    assert.strictEqual(els.get("#save-bar").classList.contains("show"), true);
  }
  els.get("#recommend-daily").checked = true;
  els.get("#llm-daily").checked = false;
  assert.strictEqual(global.collectConfig().FNMUSIC_RECOMMEND_DAILY, true);
  els.get("#llm-daily").checked = true;
  assert.strictEqual(global.collectConfig().FNMUSIC_LLM_DAILY_ENABLED, true);
  els.get("#recommend-daily").checked = false;
  assert.strictEqual(global.collectConfig().FNMUSIC_RECOMMEND_DAILY, false);
});

test("大模型页面：无最大小时限制、初始隐藏且语义说明完整", () => {
  const html = fs.readFileSync(path.join(__dirname, "static", "index.html"), "utf8");
  const hours = html.match(/<input[^>]*id="liked-refresh-hours"[^>]*>/)[0];
  assert.ok(!/\bmax=/.test(hours));
  assert.ok(hours.includes('step="any"'));
  assert.ok(html.includes('id="llm-liked-settings" hidden'));
  assert.ok(html.includes('id="llm-api-warning" role="status" hidden'));
  for (const phrase of ["不会自动启用", "首次被客户端获取", "不会到期或轮换", "本地有效、未收藏候选至少 20 首", "停止生成且不展示猜你喜欢"]) {
    assert.ok(html.includes(phrase), phrase);
  }
});

test("markDirty / clearDirty：save-bar 的 show 类显隐与文案同步", () => {
  reset();
  global.markDirty("已修改配置");
  assert.strictEqual(els.get("#save-note").textContent, "已修改配置");
  assert.strictEqual(els.get("#save-bar").classList.contains("show"), true);
  global.clearDirty();
  assert.strictEqual(els.get("#save-note").textContent, "");
  assert.strictEqual(els.get("#save-bar").classList.contains("show"), false);
});

test("lxUploadScript→lxAfterUpload：上传成功后 file:// URL 填入输入框（上传本身不改配置不标脏）", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/lx/upload", {
    ok: true,
    data: { path: "/data/lxmusic/uploads/mine.js", url: "file:///data/lxmusic/uploads/mine.js", meta: { name: "上传源" } },
  });
  const r = await global.lxUploadScript("mine.js", "/*stub*/");
  assert.strictEqual(r.data.url, "file:///data/lxmusic/uploads/mine.js");
  await global.lxAfterUpload(r);
  assert.strictEqual(els.get("#lx-url").value, "file:///data/lxmusic/uploads/mine.js");
  assert.strictEqual(els.get("#lx-upload-note").textContent, "已上传：上传源");
  // 添加进列表后才标脏（桩元素常驻以保留加载期监听器绑定，改查 save-bar 的 show 类）
  assert.strictEqual(els.get("#save-bar").classList.contains("show"), false);
});

test("洛雪源列表：旧数据（无 active 标记）按 LX_SOURCE_URL 推导激活态", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", {
    values: {
      LX_SOURCE_URL: "file:///a.js",
      LX_SOURCE_LIST: '[{"name":"甲","url":"file:///a.js"},{"name":"乙","url":"file:///b.js"}]',
    },
  });
  await global.loadConfig();
  const html = els.get("#lx-source-list").innerHTML;
  assert.ok(html.includes("已激活"), "匹配 LX_SOURCE_URL 的项应显示已激活徽标");
  assert.ok(html.includes("取消激活"), "激活项应有取消激活按钮");
  assert.ok(html.includes("激活"), "未激活项应有激活按钮");
  // 恰好一处已激活
  assert.strictEqual((html.match(/lx-badge/g) || []).length, 1);
});

test("洛雪源列表：多源同时激活共存渲染", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", {
    values: {
      LX_SOURCE_LIST: JSON.stringify([
        { name: "甲", url: "file:///a.js", active: true },
        { name: "乙", url: "file:///b.js", active: true },
        { name: "丙", url: "file:///c.js", active: false },
      ]),
    },
  });
  await global.loadConfig();
  const html = els.get("#lx-source-list").innerHTML;
  assert.strictEqual((html.match(/lx-badge/g) || []).length, 2, "两个已激活徽标");
  assert.strictEqual((html.match(/lx-btn-deactivate/g) || []).length, 2, "两个取消激活按钮");
  assert.strictEqual((html.match(/lx-btn-activate/g) || []).length, 1, "一个激活按钮");
});

test("collectConfig：lxmusic 下无激活源时拒绝提交", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", {
    values: { LX_SOURCE_LIST: '[{"name":"甲","url":"file:///a.js","active":false}]' },
  });
  await global.loadConfig();
  const qsa = global.document.querySelectorAll;
  global.document.querySelectorAll = (sel) =>
    sel === "input[name=provider]" ? [{ value: "lxmusic", checked: true }] : qsa(sel);
  try {
    assert.throws(() => global.collectConfig(), /请至少激活一个洛雪源/);
  } finally {
    global.document.querySelectorAll = qsa;
  }
});

test("collectConfig：有激活源时提交列表且不提交 LX_SOURCE_URL（后端派生）", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", {
    values: {
      LX_SOURCE_LIST: JSON.stringify([
        { name: "甲", url: "file:///a.js", active: true },
        { name: "乙", url: "file:///b.js", active: false },
      ]),
    },
  });
  await global.loadConfig();
  const qsa = global.document.querySelectorAll;
  global.document.querySelectorAll = (sel) =>
    sel === "input[name=provider]" ? [{ value: "lxmusic", checked: true }] : qsa(sel);
  try {
    const values = global.collectConfig();
    assert.strictEqual(values.LX_SOURCE_URL, undefined);
    assert.deepStrictEqual(JSON.parse(values.LX_SOURCE_LIST), [
      { name: "甲", url: "file:///a.js", active: true },
      { name: "乙", url: "file:///b.js", active: false },
    ]);
  } finally {
    global.document.querySelectorAll = qsa;
  }
});

test("saveConfig：自动取消失效源后提示保存成功并刷新激活状态", async () => {
  reset();
  const items = [
    { name: "失效源", url: "file:///bad.js", active: true },
    { name: "可用源", url: "file:///good.js", active: true },
  ];
  enqueue("/app/fnmusic-ext/api/config", { values: {
    FNMUSIC_LX_ENABLED: "true", LX_SOURCE_LIST: JSON.stringify(items),
  } });
  await global.loadConfig();
  global.markDirty();
  enqueue("/app/fnmusic-ext/api/config", {
    ok: true, changed: ["LX_SOURCE_LIST"],
    actions: [
      { kind: "lx_activate", op: "activate", source: "失效源", ok: false, error: "源错误", auto_disabled: true },
      { kind: "lx_activate", op: "activate", source: "可用源", ok: true },
    ],
    warnings: [{ source: "失效源", url: "file:///bad.js", message: "源错误" }],
  }, { values: {
    FNMUSIC_LX_ENABLED: "true", LX_SOURCE_URL: "file:///good.js",
    LX_SOURCE_LIST: JSON.stringify(items.map((i, index) => ({ ...i, active: index === 1 }))),
  } });
  const qsa = global.document.querySelectorAll;
  global.document.querySelectorAll = (sel) =>
    sel === "input[name=provider]" ? [{ value: "lxmusic", checked: true }] : qsa(sel);
  try {
    await global.saveConfig();
  } finally {
    global.document.querySelectorAll = qsa;
  }
  const message = els.get("#toast").textContent;
  assert.ok(message.includes("已保存"));
  assert.ok(message.includes("「失效源」失效，无法激活，已自动取消激活（源错误）"));
  assert.strictEqual(els.get("#toast").className, "toast ok");
  assert.strictEqual((els.get("#lx-source-list").innerHTML.match(/lx-badge/g) || []).length, 1);
  assert.strictEqual(els.get("#save-bar").classList.contains("show"), false);
  assert.strictEqual(els.get("#save-btn").disabled, false);
});

test("collectConfig：全部源自动停用后仍可保存其它设置且不恢复旧激活标记", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: {
    FNMUSIC_LX_ENABLED: "true", LX_SOURCE_URL: "",
    LX_SOURCE_LIST: '[{"name":"失效源","url":"file:///bad.js","active":false}]',
  } });
  await global.loadConfig();
  assert.strictEqual((els.get("#lx-source-list").innerHTML.match(/lx-badge/g) || []).length, 0);
  const qsa = global.document.querySelectorAll;
  global.document.querySelectorAll = (sel) =>
    sel === "input[name=provider]" ? [{ value: "lxmusic", checked: true }] : qsa(sel);
  try {
    assert.strictEqual(JSON.parse(global.collectConfig().LX_SOURCE_LIST)[0].active, false);
  } finally {
    global.document.querySelectorAll = qsa;
  }
});

test("洛雪测试：服务失败直接显示，不拉预览或重复校验", async () => {
  reset();
  $("#lx-url").value = "https://example.com/source.js";
  enqueue("/app/fnmusic-ext/api/lx/verify", new Error("洛雪脚本服务 lxserver 不可连接"));
  $("#lx-test").dispatch("click");
  await tick(0);
  assert.ok($("#lx-report").textContent.includes("lxserver 不可连接"));
  assert.strictEqual(global.fetchCalls.filter((c) => c.path.endsWith("/api/lx/verify")).length, 1);
  assert.strictEqual(global.fetchCalls.filter((c) => c.path.endsWith("/api/preview")).length, 0);
});

test("lxUploadScript：就绪补启交给后端，失败不再重复提交", async () => {
  reset();
  // issue #78：后端负责补启 lxserver/lxmusic；前端只提交一次，失败直接上抛
  enqueue("/app/fnmusic-ext/api/lx/upload", new Error("洛雪脚本服务 lxserver 不可连接"));
  let rejected = false;
  try {
    await global.lxUploadScript("a.js", "/*s*/");
  } catch (_) {
    rejected = true;
  }
  assert.strictEqual(rejected, true);
  // 提交次数必须为 1（重复提交会把同一脚本导入两次）
  const uploads = global.fetchCalls.filter((c) => c.path === "/app/fnmusic-ext/api/lx/upload");
  assert.strictEqual(uploads.length, 1);
});

test("网易账号歌单开关：loadConfig 回填 + collectConfig 收集", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: { FNMUSIC_NETEASE_MY_PLAYLISTS: "true" } });
  await global.loadConfig();
  assert.strictEqual(els.get("#netease-my-playlists").checked, true);
  els.get("#netease-my-playlists").checked = false;
  assert.strictEqual(global.collectConfig().FNMUSIC_NETEASE_MY_PLAYLISTS, false);
  // 回填 false：缺省/关闭都表现为未勾选
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: { FNMUSIC_NETEASE_MY_PLAYLISTS: "false" } });
  await global.loadConfig();
  assert.strictEqual(els.get("#netease-my-playlists").checked, false);
});

test("网易云歌单常驻开关：loadConfig 回填 + collectConfig 收集", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: { FNMUSIC_NETEASE_PLAYLIST_RESIDENT: "true" } });
  await global.loadConfig();
  assert.strictEqual(els.get("#netease-playlist-resident").checked, true);
  els.get("#netease-playlist-resident").checked = false;
  assert.strictEqual(global.collectConfig().FNMUSIC_NETEASE_PLAYLIST_RESIDENT, false);
  // 回填 false：缺省/关闭都表现为未勾选
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: { FNMUSIC_NETEASE_PLAYLIST_RESIDENT: "false" } });
  await global.loadConfig();
  assert.strictEqual(els.get("#netease-playlist-resident").checked, false);
});

test("网易云歌单常驻：勾选时自动带出账号歌单开关", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: {} });
  await global.loadConfig();
  const resident = els.get("#netease-playlist-resident");
  const myPlaylists = els.get("#netease-my-playlists");
  assert.strictEqual(myPlaylists.checked, false);
  resident.checked = true;
  resident.dispatch("change");
  assert.strictEqual(myPlaylists.checked, true);
});

test("网易云歌单常驻：未登录置灰 / 登录恢复可用", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/netease/auth/status", { ok: true, data: { logged_in: false } });
  await global.syncNeteaseAccount();
  assert.strictEqual(els.get("#netease-playlist-resident").disabled, true);
  reset();
  enqueue("/app/fnmusic-ext/api/netease/auth/status", { ok: true, data: { logged_in: true, nickname: "小明" } });
  await global.syncNeteaseAccount();
  assert.strictEqual(els.get("#netease-playlist-resident").disabled, false);
});

test("自动下载歌词开关：loadConfig 回填 + collectConfig 收集", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: { FNMUSIC_LYRIC_AUTO_DL: "true" } });
  await global.loadConfig();
  assert.strictEqual(els.get("#lyric-auto-dl").checked, true);
  els.get("#lyric-auto-dl").checked = false;
  assert.strictEqual(global.collectConfig().FNMUSIC_LYRIC_AUTO_DL, false);
  // 缺省/关闭都表现为未勾选（默认关）
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: {} });
  await global.loadConfig();
  assert.strictEqual(els.get("#lyric-auto-dl").checked, false);
});

test("储存目录：loadConfig 回填 + collectConfig 收集 + 格式/互斥校验", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: { FNMUSIC_CACHE_DIR: "/vol2/cache", FNMUSIC_TEE_SAVE_DIR: "/vol1/music" } });
  await global.loadConfig();
  assert.strictEqual(els.get("#dir-cache").value, "/vol2/cache");
  assert.strictEqual(els.get("#dir-download").value, "/vol1/music");
  const values = global.collectConfig();
  assert.strictEqual(values.FNMUSIC_CACHE_DIR, "/vol2/cache");
  assert.strictEqual(values.FNMUSIC_TEE_SAVE_DIR, "/vol1/music");
  // 互为父子 → 拒绝
  els.get("#dir-cache").value = "/vol1/music/sub";
  assert.throws(() => global.collectConfig(), /父子/);
  // 相对路径 / .. → 拒绝
  els.get("#dir-cache").value = "vol2/cache";
  assert.throws(() => global.collectConfig(), /绝对路径/);
  els.get("#dir-cache").value = "/vol1/../etc";
  assert.throws(() => global.collectConfig(), /\.\./);
  // 恢复合法值 → 通过
  els.get("#dir-cache").value = "/vol2/cache";
  assert.strictEqual(global.collectConfig().FNMUSIC_CACHE_DIR, "/vol2/cache");
});

test("下载目录输入即标脏（保存路径已在边听边存页移除，此处为唯一入口）", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: {} });
  await global.loadConfig();
  els.get("#dir-download").value = "/vol1/new";
  els.get("#dir-download").dispatch("input");
  assert.strictEqual(els.get("#save-bar").classList.contains("show"), true);
});

test("目录输入防抖 fs-check：目录不存在但父目录可写 → warn 提示", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: {} });
  await global.loadConfig();
  enqueue("/app/fnmusic-ext/api/fs-check", { ok: true, path: "/vol1/new", exists: false, writable: true });
  els.get("#dir-download").value = "/vol1/new";
  els.get("#dir-download").dispatch("input");
  await tick(560); // 防抖 500ms 后校验
  assert.strictEqual(els.get("#dir-download-check").hidden, false);
  assert.ok(els.get("#dir-download-check").textContent.includes("自动创建"));
  assert.strictEqual(els.get("#dir-download-check").className.includes("warn"), true);
});

test("目录输入防抖 fs-check：清空输入隐藏状态行", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: {} });
  await global.loadConfig();
  els.get("#dir-cache").value = "";
  els.get("#dir-cache").dispatch("input");
  await tick(560);
  assert.strictEqual(els.get("#dir-cache-check").hidden, true);
});

test("saveConfig：fs-check 目录不可写 → 阻断保存（不发 PUT）", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: {} });
  await global.loadConfig();
  els.get("#dir-cache").value = "/vol2/ro";
  enqueue("/app/fnmusic-ext/api/fs-check", { ok: true, path: "/vol2/ro", exists: true, writable: false });
  await global.saveConfig();
  assert.ok(!fetchCalls.some((c) => c.method === "PUT"), "不可写时不得发 PUT /api/config");
  assert.ok(els.get("#toast").textContent.includes("不可用"));
});

test("saveConfig：fs-check 不可用（直连 501/网络错误）→ 降级放行保存", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: {} });
  await global.loadConfig();
  els.get("#dir-cache").value = "/vol2/ok";
  enqueue("/app/fnmusic-ext/api/fs-check", new Error("直连模式不支持目录权限校验"));
  await global.saveConfig();
  assert.ok(fetchCalls.some((c) => c.method === "PUT" && c.path === "/app/fnmusic-ext/api/config"));
});

/* ------------------------------------------------ 关于页 ------------------- */
test("loadAbout：版本/部署徽章/免责声明/日志清单渲染，donate=false 隐藏打赏", async () => {
  enqueue("/app/fnmusic-ext/api/about", {
    ok: true, version: "2.8.0e", deploy_mode: "docker",
    author: "javycoder", author_url: "https://github.com/javycoder",
                repo_url: "https://github.com/javycoder/fnos_music_ext",
    issues_url: "https://github.com/javycoder/fnos_music_ext/issues",
    disclaimer: [
      { title: "本项目基于 MIT 许可证开源", text: "仅限个人技术研究" },
      { title: "不托管受版权保护的音频", text: "" },
    ],
    donate: false,
    logs: [{ name: "proxy.log", size: 2048, mtime: 1 }, { name: "webui.log", size: 512, mtime: 1 }],
    log_retention_days: 3,
  });
  await global.loadAbout();
  assert.strictEqual(global.document.querySelector("#about-version").textContent, "v2.8.0e");
  assert.strictEqual(global.document.querySelector("#about-deploy").textContent, "Docker 部署");
  const html = global.document.querySelector("#about-disclaimer").innerHTML;
  assert.ok(html.includes("本项目基于 MIT 许可证开源") && html.includes("仅限个人技术研究"));
  assert.ok(global.document.querySelector("#about-logs").innerHTML.includes("proxy.log"));
  assert.ok(global.document.querySelector("#about-logs").innerHTML.includes("2.0 KB"));
  assert.strictEqual(global.document.querySelector("#about-donate").hidden, true);
  assert.strictEqual(global.document.querySelector("#about-disclaimer-card").hidden, false);
});

test("loadAbout：donate=true 展示打赏卡片并指向静态二维码", async () => {
  enqueue("/app/fnmusic-ext/api/about", {
    ok: true, version: "2.8.0e", deploy_mode: "native",
    disclaimer: [], donate: true, logs: [], log_retention_days: 3,
  });
  await global.loadAbout();
  assert.strictEqual(global.document.querySelector("#about-donate").hidden, false);
  assert.strictEqual(
    global.document.querySelector("#about-donate-img").src,
    "/app/fnmusic-ext/static/alipay.png",
  );
  // 解析不到免责声明时隐藏该卡片
  assert.strictEqual(global.document.querySelector("#about-disclaimer-card").hidden, true);
});

test("exportLogs：走 /api/logs/export 并以 a[download] 触发下载", async () => {
  enqueue("/app/fnmusic-ext/api/logs/export", {
    __rawResponse: true,
    resp: {
      ok: true, status: 200,
      headers: { get: (n) => (String(n).toLowerCase() === "content-disposition"
        ? 'attachment; filename="20261010220000.logzip"' : null) },
      blob: async () => ({ __blob: true }),
    },
  });
  await global.exportLogs();
  assert.ok(fetchCalls.some((c) => c.method === "GET" && c.path === "/app/fnmusic-ext/api/logs/export"));
  assert.strictEqual(createdUrls.length, 1);  // blob → objectURL
  // 下载文件名取自 Content-Disposition 并展示在导出备注
  assert.ok(global.document.querySelector("#log-export-note").textContent.includes("20261010220000.logzip"));
});

test("switchPage('about') 懒加载关于数据", async () => {
  enqueue("/app/fnmusic-ext/api/about", {
    ok: true, version: "2.8.0e", deploy_mode: "docker",
    disclaimer: [], donate: false, logs: [], log_retention_days: 3,
  });
  global.switchPage("about");
  await new Promise((r) => realSetTimeout(r, 0));  // 等 fetch 微任务
  assert.ok(fetchCalls.some((c) => c.path === "/app/fnmusic-ext/api/about"));
});

/* ------------------------------------------------ 检查更新 ------------------ */
// 真实 DOM 有两处"关于"入口（桌面侧栏 + 移动底栏），红点必须两处同时显隐；
// 元素须经 document.querySelector 创建后登记（els.get 不会自动建桩）
qsaRegistry.set(".nav-dot", [
  global.document.querySelector("#nav-dot-desktop"),
  global.document.querySelector("#nav-dot-mobile"),
]);
const UPDATE_PAYLOAD = {
  ok: true, current_version: "2.8.3", has_update: true, source: "github", checked_at: 1,
  latest: {
    version: "2.9.0", name: "fnmusic-ext 2.9.0", published_at: "2026-10-11T08:00:00Z",
    changelog: "关于页新增检查更新", html_url: "https://github.com/javycoder/fnos_music_ext/releases/tag/v2.9.0",
  },
};

test("检查更新：has_update → 两处红点与 NEW 常亮；无更新 → 全部隐藏", async () => {
  enqueue("/app/fnmusic-ext/api/update-check", { ok: true, current_version: "2.8.3", has_update: false });
  await global.checkUpdate();
  assert.strictEqual(els.get("#nav-dot-desktop").hidden, true);
  assert.strictEqual(els.get("#nav-dot-mobile").hidden, true);
  assert.strictEqual(els.get("#about-new").hidden, true);
  enqueue("/app/fnmusic-ext/api/update-check", UPDATE_PAYLOAD);
  await global.checkUpdate();
  assert.strictEqual(els.get("#nav-dot-desktop").hidden, false);
  assert.strictEqual(els.get("#nav-dot-mobile").hidden, false);
  assert.strictEqual(els.get("#about-new").hidden, false);
});

test("检查更新：接口失败静默，不炸不弹错", async () => {
  enqueue("/app/fnmusic-ext/api/update-check", new Error("服务器失联"));
  await global.checkUpdate();  // 不应抛出
  assert.ok(fetchCalls.some((c) => c.path === "/app/fnmusic-ext/api/update-check"));
  assert.strictEqual(els.get("#toast").textContent, "");  // 没有弹错误提示
});

test("openUpdateModal：填充版本/日期/changelog/升级链接（跟随检查源）", async () => {
  enqueue("/app/fnmusic-ext/api/update-check", UPDATE_PAYLOAD);
  await global.checkUpdate();
  global.openUpdateModal();
  assert.strictEqual(els.get("#update-modal").hidden, false);
  assert.strictEqual(els.get("#update-modal-version").textContent, "v2.9.0");
  assert.strictEqual(els.get("#update-modal-date").textContent, "2026-10-11");
  assert.strictEqual(els.get("#update-modal-changelog").textContent, "关于页新增检查更新");
  assert.strictEqual(els.get("#update-upgrade-btn").href, "https://github.com/javycoder/fnos_music_ext/releases/tag/v2.9.0");
});

test("updateModal：取消按钮与遮罩空白点击均可关闭", async () => {
  enqueue("/app/fnmusic-ext/api/update-check", UPDATE_PAYLOAD);
  await global.checkUpdate();
  global.openUpdateModal();
  els.get("#update-cancel-btn").dispatch("click");
  assert.strictEqual(els.get("#update-modal").hidden, true);
  global.openUpdateModal();
  els.get("#update-modal").dispatch("click");  // 桩上 target/currentTarget 均为 undefined，等价点空白处
  assert.strictEqual(els.get("#update-modal").hidden, true);
});

test("openUpdateModal：无更新信息时不弹窗（守卫）", async () => {
  // 先覆盖上一用例可能遗留的 has_update 状态
  enqueue("/app/fnmusic-ext/api/update-check", { ok: true, current_version: "2.8.3", has_update: false });
  await global.checkUpdate();
  els.get("#update-modal").hidden = true;
  global.openUpdateModal();
  assert.strictEqual(els.get("#update-modal").hidden, true);
});

test("switchPage('about') 顺带触发检查更新", async () => {
  enqueue("/app/fnmusic-ext/api/about", {
    ok: true, version: "2.8.0e", deploy_mode: "docker",
    disclaimer: [], donate: false, logs: [], log_retention_days: 3,
  });
  enqueue("/app/fnmusic-ext/api/update-check", UPDATE_PAYLOAD);
  global.switchPage("about");
  await new Promise((r) => realSetTimeout(r, 0));
  assert.ok(fetchCalls.some((c) => c.path === "/app/fnmusic-ext/api/update-check"));
  assert.strictEqual(els.get("#nav-dot-desktop").hidden, false);
});

const CLEANUP_PATH = "/app/fnmusic-ext/api/maintenance/cleanup";
const cleanupResponse = (status, extra = {}) => ({ ok: true, job: {
  id: "cleanup-1", status, phase: "scan", scanned: 10, removed: 2,
  bytes: 1024, skipped: 8, errors: [], details: [], ...extra,
} });

test("清理 Beta：默认取消焦点，按钮/Escape/遮罩取消均不请求；卡片内点击不取消", () => {
  global.openCleanupModal();
  assert.strictEqual(document.activeElement, els.get("#cleanup-cancel-btn"));
  window.dispatch("keydown", { key: "Tab", preventDefault() {} });
  assert.strictEqual(document.activeElement, els.get("#cleanup-confirm-btn"));
  window.dispatch("keydown", { key: "Tab", preventDefault() {} });
  assert.strictEqual(document.activeElement, els.get("#cleanup-cancel-btn"));
  els.get("#cleanup-cancel-btn").dispatch("click");
  assert.strictEqual(els.get("#cleanup-modal").hidden, true);
  assert.strictEqual(document.activeElement, els.get("#cleanup-open-btn"));
  global.openCleanupModal();
  window.dispatch("keydown", { key: "Escape", preventDefault() {} });
  assert.strictEqual(els.get("#cleanup-modal").hidden, true);
  global.openCleanupModal();
  const mask = els.get("#cleanup-modal");
  mask.dispatch("click", { target: {}, currentTarget: mask });
  assert.strictEqual(mask.hidden, false);
  mask.dispatch("click", { target: mask, currentTarget: mask });
  assert.strictEqual(mask.hidden, true);
  assert.strictEqual(fetchCalls.length, 0);
});

test("清理 Beta：确认 POST，双击及重复点击只展示同任务，GET 轮询进度至完成，详情安全输出", async () => {
  enqueue(CLEANUP_PATH, cleanupResponse("running"));
  global.openCleanupModal();
  const first = global.startCleanup();
  await global.startCleanup();
  await first;
  const post = fetchCalls.filter((c) => c.method === "POST");
  assert.strictEqual(post.length, 1);
  assert.deepStrictEqual(JSON.parse(post[0].body), { confirm_beta: true });
  assert.ok(els.get("#cleanup-status").textContent.includes("清理中"));
  assert.ok(els.get("#cleanup-status").textContent.includes("已扫描 10"));
  enqueue(CLEANUP_PATH + "/cleanup-1", cleanupResponse("running", { scanned: 20 }));
  global.openCleanupModal();
  await tick(5);
  assert.strictEqual(fetchCalls.filter((c) => c.method === "POST").length, 1);
  assert.strictEqual(els.get("#cleanup-modal").hidden, true);
  assert.ok(els.get("#cleanup-status").textContent.includes("已扫描 20"));
  const attack = '<img src=x onerror="alert(1)">';
  enqueue(CLEANUP_PATH + "/cleanup-1", cleanupResponse("completed", {
    details: [attack, { path: attack }], errors: [attack],
  }));
  await tick(1100); // 真正触发自动 GET 轮询
  assert.ok(els.get("#cleanup-status").textContent.includes("清理完成"));
  assert.ok(els.get("#cleanup-status").textContent.includes("1024 字节"));
  assert.ok(els.get("#cleanup-details").textContent.includes(attack));
  assert.strictEqual(els.get("#cleanup-details").innerHTML, "");
  assert.strictEqual(els.get("#cleanup-details").hidden, false);
  const count = fetchCalls.filter((c) => c.path.startsWith(CLEANUP_PATH)).length;
  await tick(1100);
  assert.strictEqual(fetchCalls.filter((c) => c.path.startsWith(CLEANUP_PATH)).length, count); // terminal 状态停表
});

test("清理 Beta：查询失败保留任务，重试只 GET，failed 停止轮询", async () => {
  enqueue(CLEANUP_PATH, cleanupResponse("running"));
  global.openCleanupModal();
  await global.startCleanup();
  enqueue(CLEANUP_PATH + "/cleanup-1", new Error("离线"));
  await global.pollCleanupJob();
  assert.ok(els.get("#cleanup-status").textContent.includes("查询失败"));
  enqueue(CLEANUP_PATH + "/cleanup-1", cleanupResponse("failed", { errors: ["无权限"] }));
  global.openCleanupModal();
  await tick(5);
  assert.ok(els.get("#cleanup-status").textContent.includes("清理失败"));
  assert.ok(els.get("#cleanup-details").textContent.includes("无权限"));
  assert.strictEqual(fetchCalls.filter((c) => c.method === "POST").length, 1);
  const count = fetchCalls.filter((c) => c.path.startsWith(CLEANUP_PATH)).length;
  await tick(1100);
  assert.strictEqual(fetchCalls.filter((c) => c.path.startsWith(CLEANUP_PATH)).length, count);
});

test("清理 Beta：501 直连错误可见；取消后确认无效；无效响应不启动轮询", async () => {
  enqueue(CLEANUP_PATH, { __rawResponse: true, resp: {
    ok: false, status: 501, json: async () => ({ detail: "直连模式不支持清理垃圾文件" }),
  } });
  global.openCleanupModal();
  await global.startCleanup();
  assert.ok(els.get("#cleanup-status").textContent.includes("直连模式不支持"));
  global.openCleanupModal();
  global.closeCleanupModal();
  await global.startCleanup();
  assert.strictEqual(fetchCalls.length, 1);
  enqueue(CLEANUP_PATH, { ok: true });
  global.openCleanupModal();
  await global.startCleanup();
  assert.ok(els.get("#cleanup-status").textContent.includes("响应格式无效"));
});

test("loadStatus：概览进程含 lxserver，不可达透出 healthz 详情并转义", async () => {
  enqueue("/app/fnmusic-ext/api/status", {
    version: "t",
    current_provider: "lxmusic",
    processes: {
      musicdl: { state: "STOPPED", detail: "" },
      lxserver: { state: "FATAL", detail: "" },
      lxmusic: { state: "RUNNING", detail: "" },
      webui: { state: "RUNNING", detail: "" },
    },
    services: {
      lxmusic: { reachable: false, detail: "lxserver unreachable <script>alert(1)</script>" },
    },
    lx_source: null,
    previews: {},
  });
  await global.loadStatus();
  const procHtml = els.get("#ov-processes").innerHTML;
  assert.ok(procHtml.includes("lxserver"), "进程概览应包含 lxserver 标签");
  assert.ok(procHtml.includes("FATAL"));
  const svcHtml = els.get("#ov-services").innerHTML;
  assert.ok(svcHtml.includes("不可达"), "不可达状态应显示");
  assert.ok(svcHtml.includes("lxserver unreachable"), "healthz 失败详情应透出，便于定位底层进程故障");
  assert.ok(!svcHtml.includes("<script>"), "错误详情必须 HTML 转义");
  assert.ok(svcHtml.includes("&lt;script&gt;"));
});

test("loadStatus：洛雪卡片源信息与错误文本转义", async () => {
  enqueue("/app/fnmusic-ext/api/status", {
    version: "t",
    current_provider: "lxmusic",
    processes: {},
    services: {},
    previews: {},
    lx_source: {
      initialized: true,
      sources: [],
      source: { name: "<b>evil</b>", version: "1.0", platforms: { kw: {} } },
      last_error: "<img src=x onerror=alert(1)>",
    },
  });
  await global.loadStatus();
  const lxHtml = els.get("#ov-lx").innerHTML;
  assert.ok(!lxHtml.includes("<img src=x"), "last_error 必须转义");
  assert.ok(lxHtml.includes("&lt;img"));
  assert.ok(!lxHtml.includes("<b>evil</b>"), "源名称必须转义");
  assert.ok(lxHtml.includes("&lt;b&gt;evil&lt;/b&gt;"));
});

/* ------------------------------------------------ 主题 / 设置 -------------- */
const themeBootSrc = fs.readFileSync(path.join(__dirname, "static", "theme-boot.js"), "utf8");

// 每例完整加载生产 app.js 到独立页面，避免主题偏好、媒体/宿主监听器污染既有用例。
// 仅以同名函数桩替换 SDK 加载边界，不改主题实现；其余启动流程照常执行。
async function themeHarness(options = {}) {
  const elements = new Map();
  const get = (sel) => {
    if (!elements.has(sel)) elements.set(sel, stubEl());
    return elements.get(sel);
  };
  const appliedThemes = [];
  const root = { dataset: new Proxy({}, { set(target, key, value) {
    if (key === "theme") appliedThemes.push(value);
    target[key] = value;
    return true;
  } }) };
  const radios = ["auto", "light", "dark"].map((value) => Object.assign(stubEl(), { value }));
  const pages = ["overview", "settings", "llm", "tee"].map((page) =>
    Object.assign(stubEl(), { id: "page-" + page }));
  const nav = ["settings", "settings", "llm", "tee"].map((page) =>
    Object.assign(stubEl(), { dataset: { page } }));
  const media = Object.assign(stubEl(), { matches: !!options.dark });
  const saved = new Map(options.saved == null ? [] : [["fnmusic-theme", options.saved]]);
  if (options.hostSaved != null) saved.set("fnmusic-host-theme", options.hostSaved);
  const storage = {
    getItem(key) { if (options.blockStorage) throw new Error("storage denied"); return saved.get(key) ?? null; },
    setItem(key, value) { if (options.blockStorage) throw new Error("storage denied"); saved.set(key, value); },
    removeItem(key) { if (options.blockStorage) throw new Error("storage denied"); saved.delete(key); },
  };
  const win = Object.assign(stubEl(), {
    location: { pathname: options.pathname || "/" }, localStorage: storage,
    matchMedia: options.noMedia ? undefined : () => media,
  });
  const calls = [];
  const context = vm.createContext({
    window: win,
    document: {
      documentElement: root,
      querySelector: get,
      querySelectorAll(sel) {
        return ({ "input[name=theme]": radios, ".page": pages, "[data-page]": nav })[sel] || [];
      },
    },
    fetch: async (url, request = {}) => {
      calls.push({ url, ...request });
      return { ok: true, json: async () => defaults.get(url) || { ok: true } };
    },
    setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {},
    __sdk: options.sdk || false, __sdkCalls: 0,
  });
  vm.runInContext(themeBootSrc, context, { filename: "theme-boot.js" });
  const initialTheme = root.dataset.theme;
  vm.runInContext(src + '\nasync function loadTrimSdk() { __sdkCalls++; return __sdk; }', context, { filename: "app.js" });
  await tick(0);
  return { context, root, initialTheme, appliedThemes, radios, media, saved, win, calls, elements, pages, nav };
}

function themeSelected(h) {
  return h.radios.filter((el) => el.checked).map((el) => el.value);
}

function changeTheme(h, value) {
  h.radios.forEach((el) => { el.checked = el.value === value; });
  h.radios.find((el) => el.checked).dispatch("change");
}

test("首屏主题：偏好优先，非法/缺省/存储禁用回退系统，无媒体查询默认浅色", async () => {
  for (const [options, expected, selected] of [
    [{ dark: true }, "dark", "auto"],
    [{ dark: false }, "light", "auto"],
    [{ saved: "light", dark: true }, "light", "light"],
    [{ saved: "dark", dark: false }, "dark", "dark"],
    [{ saved: "invalid", dark: true }, "dark", "auto"],
    [{ saved: "auto", dark: true }, "dark", "auto"],
    [{ blockStorage: true, dark: true }, "dark", "auto"],
    [{ noMedia: true }, "light", "auto"],
  ]) {
    const h = await themeHarness(options);
    assert.strictEqual(h.initialTheme, expected, "样式表前已写入正确主题");
    assert.strictEqual(h.root.dataset.theme, expected, "app 初始化与首屏引导一致");
    assert.deepStrictEqual(themeSelected(h), [selected]);
    assert.strictEqual(h.context.__sdkCalls, 0, "直连不加载宿主 SDK");
  }
});

test("设置主题：立即生效、持久化/恢复 auto，不标脏、不写业务配置", async () => {
  const h = await themeHarness();
  const configBefore = JSON.stringify(h.context.collectConfig());
  for (const value of ["dark", "light"]) {
    changeTheme(h, value);
    assert.strictEqual(h.root.dataset.theme, value);
    assert.strictEqual(h.saved.get("fnmusic-theme"), value);
    assert.deepStrictEqual(themeSelected(h), [value]);
    const reloaded = await themeHarness({ saved: h.saved.get("fnmusic-theme"), dark: value !== "dark" });
    assert.strictEqual(reloaded.initialTheme, value);
  }
  changeTheme(h, "auto");
  assert.strictEqual(h.saved.has("fnmusic-theme"), false);
  h.media.matches = true;
  h.media.dispatch("change");
  assert.strictEqual(h.root.dataset.theme, "dark");
  assert.deepStrictEqual(themeSelected(h), ["auto"]);
  assert.strictEqual(JSON.stringify(h.context.collectConfig()), configBefore);
  assert.strictEqual(h.elements.get("#save-bar").classList.contains("show"), false);
  h.win.dispatch("beforeunload", { preventDefault() { assert.fail("主题不应产生未保存警告"); } });
  assert.ok(h.calls.every((call) => !call.method || call.method === "GET"));
  h.context.markDirty("已编辑业务配置");
  changeTheme(h, "dark");
  assert.strictEqual(h.elements.get("#save-bar").classList.contains("show"), true, "不清除既有脏标记");
});

test("主题：存储拒绝写入仍在本次页面生效，手动偏好不受系统切换影响", async () => {
  const h = await themeHarness({ blockStorage: true });
  changeTheme(h, "dark");
  h.media.dispatch("change");
  assert.strictEqual(h.root.dataset.theme, "dark");
  changeTheme(h, "light");
  h.media.matches = true;
  h.media.dispatch("change");
  assert.strictEqual(h.root.dataset.theme, "light");
  changeTheme(h, "auto");
  assert.strictEqual(h.root.dataset.theme, "dark");
});

test("设置导航：桌面和移动入口切换同一页面，不破坏 LLM/储存导航", async () => {
  const h = await themeHarness();
  for (const button of h.nav.slice(0, 2)) {
    h.context.switchPage("tee");
    button.dispatch("click");
    assert.deepStrictEqual(h.pages.filter((el) => el.classList.contains("active")).map((el) => el.id), ["page-settings"]);
    assert.ok(h.nav.slice(0, 2).every((el) => el.classList.contains("active")));
    assert.strictEqual(h.elements.get("#nav-storage-title").classList.contains("active"), false);
    h.context.switchPage("llm");
    assert.strictEqual(h.pages.find((el) => el.id === "page-llm").classList.contains("active"), true);
  }
});

test("宿主主题：初值、os/theme 动态切换优先系统，手动偏好优先宿主", async () => {
  let notify;
  const h = await themeHarness({
    pathname: "/app/fnmusic-ext/", dark: false,
    sdk: {
      isWeb: true, isStandaloneWeb: false,
      getPlatformConfig: async () => ({ theme: "dark" }),
      $on: async (name, fn) => { assert.strictEqual(name, "os/theme"); notify = fn; },
    },
  });
  assert.strictEqual(h.initialTheme, "light");
  assert.strictEqual(h.root.dataset.theme, "dark");
  assert.deepStrictEqual(themeSelected(h), ["auto"]);
  h.media.dispatch("change");
  assert.strictEqual(h.root.dataset.theme, "dark");
  notify("light");
  assert.strictEqual(h.root.dataset.theme, "light");
  changeTheme(h, "dark");
  notify("light");
  assert.strictEqual(h.root.dataset.theme, "dark");
  changeTheme(h, "auto");
  assert.strictEqual(h.root.dataset.theme, "light");
  h.media.matches = true;
  notify("invalid");
  assert.strictEqual(h.root.dataset.theme, "dark", "未知宿主主题回退媒体查询");
});

test("宿主主题：刷新沿用缓存直到异步校正，直连和手动选择不受缓存影响", async () => {
  let notify;
  const sdk = {
    isWeb: true,
    getPlatformConfig: async () => ({ theme: "dark" }),
    $on: async (_, fn) => { notify = fn; },
  };
  const first = await themeHarness({ pathname: "/app/fnmusic-ext/", sdk });
  assert.strictEqual(first.saved.get("fnmusic-host-theme"), "dark");
  let resolveConfig;
  const reloaded = await themeHarness({
    pathname: "/app/fnmusic-ext/", hostSaved: first.saved.get("fnmusic-host-theme"),
    sdk: { getPlatformConfig: () => new Promise((resolve) => { resolveConfig = resolve; }) },
  });
  assert.strictEqual(reloaded.initialTheme, "dark");
  assert.ok(reloaded.appliedThemes.every((value) => value === "dark"), "SDK 返回前不能切回设备浅色");
  resolveConfig({ theme: "light" });
  await tick(0);
  assert.strictEqual(reloaded.root.dataset.theme, "light");
  assert.strictEqual(reloaded.saved.get("fnmusic-host-theme"), "light");
  notify("light");
  assert.strictEqual(first.saved.get("fnmusic-host-theme"), "light");
  notify("invalid");
  assert.strictEqual(first.saved.has("fnmusic-host-theme"), false);
  for (const options of [
    { hostSaved: "dark" },
    { pathname: "/app/fnmusic-ext/", hostSaved: "dark", saved: "light", sdk },
    { pathname: "/app/fnmusic-ext/", hostSaved: "invalid" },
  ]) {
    const h = await themeHarness(options);
    assert.strictEqual(h.initialTheme, "light");
    assert.strictEqual(h.root.dataset.theme, "light");
  }
});

test("宿主主题：缓存失效或宿主不可用时清缓存并回退系统，存储禁用不阻断", async () => {
  for (const sdk of [false, {},
    { getPlatformConfig: async () => { throw new Error("no host"); } },
    { getPlatformConfig: async () => ({ theme: "invalid" }) },
  ]) {
    const h = await themeHarness({ pathname: "/app/fnmusic-ext/", hostSaved: "dark", sdk });
    assert.strictEqual(h.initialTheme, "dark");
    assert.strictEqual(h.root.dataset.theme, "light");
    assert.strictEqual(h.saved.has("fnmusic-host-theme"), false);
    h.media.matches = true;
    h.media.dispatch("change");
    assert.strictEqual(h.root.dataset.theme, "dark");
  }
  const h = await themeHarness({ pathname: "/app/fnmusic-ext/", blockStorage: true,
    sdk: { getPlatformConfig: async () => ({ theme: "dark" }) } });
  assert.strictEqual(h.root.dataset.theme, "dark");
});

test("宿主主题：移动嵌入与独立 Web 可读取 os.theme，但不订阅 Web 宿主事件", async () => {
  for (const flags of [{ isWeb: false }, { isWeb: true, isStandaloneWeb: true }]) {
    const h = await themeHarness({
      pathname: "/app/fnmusic-ext/",
      sdk: { ...flags, getPlatformConfig: async () => ({ os: { theme: "dark" } }),
        $on() { assert.fail("非 Web 宿主不应订阅事件"); } },
    });
    assert.strictEqual(h.root.dataset.theme, "dark");
  }
});

test("宿主主题：SDK/配置/事件不可用均不阻断业务配置与系统主题", async () => {
  for (const sdk of [false, {},
    { isWeb: true, getPlatformConfig: async () => { throw new Error("no bridge"); },
      $on: async () => { throw new Error("no event bridge"); } },
    { isWeb: true, getPlatformConfig: async () => ({ theme: "invalid" }) },
  ]) {
    const h = await themeHarness({ pathname: "/app/fnmusic-ext/", sdk, dark: true });
    assert.strictEqual(h.root.dataset.theme, "dark");
    h.media.matches = false;
    h.media.dispatch("change");
    assert.strictEqual(h.root.dataset.theme, "light");
    assert.strictEqual(h.elements.get("#liked-size").value, "20", "LLM 配置照常回填");
    assert.ok(h.calls.some((call) => call.url.endsWith("/api/status")));
  }
});

test("宿主主题：异步初值未返回不阻塞配置加载，返回后仍尊重期间选择的手动偏好", async () => {
  let resolveConfig;
  const h = await themeHarness({
    pathname: "/app/fnmusic-ext/",
    sdk: { getPlatformConfig: () => new Promise((resolve) => { resolveConfig = resolve; }) },
  });
  assert.strictEqual(h.elements.get("#liked-size").value, "20");
  assert.ok(h.calls.some((call) => call.url.endsWith("/api/status")));
  changeTheme(h, "light");
  resolveConfig({ theme: "dark" });
  await tick(0);
  assert.strictEqual(h.root.dataset.theme, "light");
  changeTheme(h, "auto");
  assert.strictEqual(h.root.dataset.theme, "dark");
});

/* ------------------------------------------------ 运行 --------------------- */
(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      reset();
      await fn();
      console.log(`ok - ${name}`);
    } catch (exc) {
      failed += 1;
      console.error(`not ok - ${name}\n  ${exc && exc.stack ? exc.stack : exc}`);
    }
  }
  console.log(`${tests.length - failed}/${tests.length} passed, ${failed} failed`);
  process.exitCode = failed ? 1 : 0;
})();
