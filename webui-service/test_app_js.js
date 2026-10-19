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
    dispatch(type) { (listeners[type] || []).slice().forEach((fn) => fn({ type })); },
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
global.window = { addEventListener() {} };
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

test("lxUploadScript：lxmusic 未运行时先拉预览再重试", async () => {
  reset();
  // 第一次 upload 502（进程未起），预览成功后重试成功
  enqueue("/app/fnmusic-ext/api/lx/upload", new Error("lxmusic 服务不可达"));
  enqueue("/app/fnmusic-ext/api/preview", { ok: true, preview: true });
  enqueue("/app/fnmusic-ext/api/lx/upload", {
    ok: true,
    data: { path: "/p", url: "file:///p", meta: { name: "n" } },
  });
  const r = await global.lxUploadScript("a.js", "/*s*/");
  assert.strictEqual(r.ok, true);
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
