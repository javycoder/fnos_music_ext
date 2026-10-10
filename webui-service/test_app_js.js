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

global.document = {
  querySelector(sel) {
    if (!els.has(sel)) els.set(sel, stubEl());
    return els.get(sel);
  },
  querySelectorAll() { return []; },
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
