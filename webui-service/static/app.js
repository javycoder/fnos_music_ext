/* fnmusic-ext WebUI — 原生 JS，无框架无外部资产。 */
"use strict";

// 飞牛桌面用 HTTPS 打开管理窗，页面必须挂在同源路径 /app/fnmusic-ext 下。
// WebUI 自己也会剥掉这个前缀。管理接口只认飞牛网关注入的管理员头。
const APP_BASE = "/app/fnmusic-ext";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const PROVIDER_LABEL = { musicdl: "musicdl 聚合音源", musicbox: "网易云音乐盒子", lxmusic: "洛雪自定义源", none: "未配置" };
const PROC_LABEL = { musicdl: "musicdl", musicbox: "musicbox", lxmusic: "lxmusic", webui: "WebUI" };

let configValues = {};   // GET /api/config 的 values
let platforms = { enabled: [], registered: [] };
let dirty = false;
let qrTimer = null;
let lxVerifiedUrl = null; // 已通过测试的 lx URL（保存时免二次校验提示用）
let lxSourceList = [];    // 洛雪源列表 [{ name, url, active }]，active 可多开

async function api(path, options) {
  const resp = await fetch(APP_BASE + path, options);
  let body = {};
  try { body = await resp.json(); } catch (_) { /* 非 JSON */ }
  if (!resp.ok) throw new Error(body.error || body.detail || `HTTP ${resp.status}`);
  return body;
}

function toast(message, kind) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast " + (kind || "");
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.hidden = true; }, 3800);
}

function markDirty(note) {
  dirty = true;
  const bar = $("#save-bar");
  if (bar) bar.classList.add("show");
  const noteEl = $("#save-note");
  if (noteEl) noteEl.textContent = note || "有未保存的修改";
}

function clearDirty() {
  dirty = false;
  const bar = $("#save-bar");
  if (bar) bar.classList.remove("show");
  const noteEl = $("#save-note");
  if (noteEl) noteEl.textContent = "";
}

/* -------------------------------------------------------------- 导航 */
function switchPage(page) {
  $$(".page").forEach((el) => el.classList.toggle("active", el.id === "page-" + page));
  $$("[data-page]").forEach((el) => el.classList.toggle("active", el.dataset.page === page));
  // 储存父标题：边听边存/目录设置任一子页激活时强调（子项保持各自的实心高亮）
  const storageTitle = $("#nav-storage-title");
  if (storageTitle) storageTitle.classList.toggle("active", page === "tee" || page === "dirs");
}
$$("[data-page]").forEach((btn) => btn.addEventListener("click", () => switchPage(btn.dataset.page)));
const storageTitleBtn = $("#nav-storage-title");
if (storageTitleBtn) storageTitleBtn.addEventListener("click", () => switchPage("tee"));

/* -------------------------------------------------------------- 概览 */
async function loadStatus() {
  try {
    const st = await api("/api/status");
    $("#brand-version").textContent = `v${st.version}`;
    $("#sidebar-foot").textContent = `${PROVIDER_LABEL[st.current_provider] || st.current_provider}`;
    $("#ov-provider-body").innerHTML =
      `<span class="state-line"><span class="dot ok"></span>${PROVIDER_LABEL[st.current_provider] || st.current_provider}</span>`;
    $("#ov-processes").innerHTML = Object.entries(st.processes).map(([name, p]) => {
      const ok = p.state === "RUNNING";
      const cls = ok ? "ok" : p.state === "FATAL" ? "err" : "";
      return `<span class="chip"><span class="dot ${cls}"></span>${PROC_LABEL[name] || name} · ${p.state}</span>`;
    }).join("");
    $("#ov-services").innerHTML = Object.entries(st.services).map(([name, s]) => {
      if (!s.reachable && s.note) return `<span class="state-line"><span class="dot"></span>${PROC_LABEL[name]}：${s.note}</span>`;
      return `<span class="state-line"><span class="dot ${s.reachable ? "ok" : "err"}"></span>${PROC_LABEL[name]}：${s.reachable ? "正常" : "不可达"}</span>`;
    }).join("");
    const lxCard = $("#ov-lx-card");
    if (st.current_provider === "lxmusic" && st.lx_source) {
      lxCard.hidden = false;
      const ls = st.lx_source;
      const rows = [];
      rows.push(`<div class="kv"><b>状态</b>${ls.initialized ? "已加载" : "未加载"}</div>`);
      const sources = ls.sources || [];
      if (sources.length) {
        rows.push(`<div class="kv"><b>已激活源</b>${sources.length} 个：${sources.map((s) =>
          `${escapeHtml(s.name || "-")}${s.version ? " v" + s.version : ""}`).join("、")}</div>`);
        const plats = new Set();
        sources.forEach((s) => (s.platforms || []).forEach((p) => plats.add(p)));
        rows.push(`<div class="kv"><b>平台</b>${[...plats].join("、") || "-"}</div>`);
      } else if (ls.source) {
        rows.push(`<div class="kv"><b>源名称</b>${ls.source.name || "-"} ${ls.source.version ? "v" + ls.source.version : ""}</div>`);
        rows.push(`<div class="kv"><b>平台</b>${Object.keys(ls.source.platforms || {}).join("、") || "-"}</div>`);
      }
      if (ls.last_error) rows.push(`<div class="kv"><b>错误</b>${ls.last_error}</div>`);
      $("#ov-lx").innerHTML = rows.join("");
    } else {
      lxCard.hidden = true;
    }
  } catch (exc) {
    $("#ov-provider-body").textContent = "状态加载失败：" + exc.message;
  }
}

/* -------------------------------------------------------------- 配置读写 */
async function loadConfig() {
  const cfg = await api("/api/config");
  configValues = cfg.values;
  applyConfigToForm();
  clearDirty();
}

function applyConfigToForm() {
  const v = configValues;
  const provider = v.FNMUSIC_NETEASE_ENABLED === "true" ? "musicbox"
    : v.FNMUSIC_MUSICDL_ENABLED === "true" ? "musicdl"
    : v.FNMUSIC_LX_ENABLED === "true" ? "lxmusic" : "";
  $$("input[name=provider]").forEach((el) => { el.checked = el.value === provider; });
  syncProviderPanels(provider);
  if (provider === "musicbox") syncNeteaseAccount();
  const quality = v.FNMUSIC_QUALITY_MODE || "high";
  $$("input[name=quality]").forEach((el) => { el.checked = el.value === quality; });
  const dlQuality = v.FNMUSIC_DL_QUALITY || "app";
  $$("input[name=dl-quality]").forEach((el) => { el.checked = el.value === dlQuality; });
  $("#recommend-hot").checked = v.FNMUSIC_RECOMMEND_HOT === "true";
  $("#recommend-daily").checked = v.FNMUSIC_RECOMMEND_DAILY === "true";
  $("#tee-enabled").checked = v.FNMUSIC_TEE_SAVE_ENABLED === "true";
  $("#auto-cover").checked = v.FNMUSIC_AUTO_COVER !== "false";
  $("#lyric-auto-dl").checked = v.FNMUSIC_LYRIC_AUTO_DL === "true";
  $("#fav-autobind").checked = v.FNMUSIC_FAV_AUTO_BIND === "true";
  $("#tee-dir").value = v.FNMUSIC_TEE_SAVE_DIR || "";
  $("#dir-download").value = v.FNMUSIC_TEE_SAVE_DIR || "";
  $("#dir-cache").value = v.FNMUSIC_CACHE_DIR || "";
  $("#tee-max").value = v.FNMUSIC_TEE_CACHE_MAX || "2";
  $("#bind-timeout").value = v.FNMUSIC_OFFICIAL_BIND_TIMEOUT_S || "120";
  $("#handoff-max").value = v.FNMUSIC_TEE_HANDOFF_MAX != null ? v.FNMUSIC_TEE_HANDOFF_MAX : "3";
  $("#scan-path").value = v.FNMUSIC_LIBRARY_SCAN_PATH || "";
  updateTeeCountLabel();
  updateBindTimeoutLabel();
  $("#llm-base").value = v.FNMUSIC_LLM_BASE_URL || "";
  $("#llm-key").value = v.FNMUSIC_LLM_API_KEY || "";
  $("#llm-model").value = v.FNMUSIC_LLM_MODEL || "";
  $("#search-timeout").value = v.FNMUSIC_SEARCH_TIMEOUT || "15";
  $("#search-probe").checked = v.FNMUSIC_SEARCH_PROBE === "true";
  $("#search-deep").checked = v.FNMUSIC_SEARCH_DEEP_PAGE !== "false";
  $("#search-deep-max").value = v.FNMUSIC_SEARCH_DEEP_MAX_PAGES || "10";
  $("#netease-my-playlists").checked = v.FNMUSIC_NETEASE_MY_PLAYLISTS === "true";
  $("#lx-url").value = "";
  lxVerifiedUrl = null;
  try {
    const rawList = v.LX_SOURCE_LIST;
    lxSourceList = rawList ? JSON.parse(rawList) : [];
    if (!Array.isArray(lxSourceList)) lxSourceList = [];
  } catch (_) {
    lxSourceList = [];
  }
  // 兼容旧数据：列表无任何 active 标记时按 LX_SOURCE_URL 推导
  const savedUrl = (v.LX_SOURCE_URL || "").trim();
  if (lxSourceList.length && !lxSourceList.some((item) => item.active)) {
    lxSourceList.forEach((item) => { item.active = !!(savedUrl && item.url === savedUrl); });
  }
  // .env 里有激活地址但不在列表中（异常状态）：补进列表展示真实激活态
  if (savedUrl && !lxSourceList.some((item) => item.url === savedUrl)) {
    lxSourceList.unshift({ name: lxDefaultName(savedUrl), url: savedUrl, active: true });
  }
  renderLxSourceList();
  syncLxAddButton();
  renderPlatformChips();
}

function collectConfig() {
  const provider = ($$("input[name=provider]").find((el) => el.checked) || {}).value || "";
  const values = {
    FNMUSIC_MUSICDL_ENABLED: provider === "musicdl",
    FNMUSIC_NETEASE_ENABLED: provider === "musicbox",
    FNMUSIC_LX_ENABLED: provider === "lxmusic",
    FNMUSIC_QUALITY_MODE: ($$("input[name=quality]").find((el) => el.checked) || {}).value || "high",
    FNMUSIC_DL_QUALITY: ($$("input[name=dl-quality]").find((el) => el.checked) || {}).value || "app",
    FNMUSIC_RECOMMEND_HOT: $("#recommend-hot").checked,
    FNMUSIC_RECOMMEND_DAILY: $("#recommend-daily").checked,
    FNMUSIC_TEE_SAVE_ENABLED: $("#tee-enabled").checked,
    FNMUSIC_AUTO_COVER: $("#auto-cover").checked,
    FNMUSIC_LYRIC_AUTO_DL: $("#lyric-auto-dl").checked,
    FNMUSIC_FAV_AUTO_BIND: $("#fav-autobind").checked,
    FNMUSIC_TEE_SAVE_DIR: $("#tee-dir").value.trim(),
    FNMUSIC_CACHE_DIR: $("#dir-cache").value.trim(),
    FNMUSIC_TEE_CACHE_MAX: parseInt($("#tee-max").value || "2", 10),
    FNMUSIC_OFFICIAL_BIND_TIMEOUT_S: parseInt($("#bind-timeout").value || "120", 10) || 120,
    FNMUSIC_TEE_HANDOFF_MAX: parseInt($("#handoff-max").value || "3", 10) || 0,
    FNMUSIC_LIBRARY_SCAN_PATH: $("#scan-path").value.trim(),
    FNMUSIC_LLM_BASE_URL: $("#llm-base").value.trim(),
    FNMUSIC_LLM_API_KEY: $("#llm-key").value.trim(),
    FNMUSIC_LLM_MODEL: $("#llm-model").value.trim(),
    FNMUSIC_SEARCH_TIMEOUT: parseInt($("#search-timeout").value || "15", 10) || 15,
    FNMUSIC_SEARCH_PROBE: $("#search-probe").checked,
    FNMUSIC_SEARCH_DEEP_PAGE: $("#search-deep").checked,
    FNMUSIC_SEARCH_DEEP_MAX_PAGES: parseInt($("#search-deep-max").value || "10", 10) || 10,
    FNMUSIC_NETEASE_MY_PLAYLISTS: $("#netease-my-playlists").checked,
    LX_SOURCE_LIST: JSON.stringify(lxSourceList),
  };
  const dlDir = values.FNMUSIC_TEE_SAVE_DIR;
  const cacheDir = values.FNMUSIC_CACHE_DIR;
  for (const [label, p] of [["下载目录", dlDir], ["歌曲缓存目录", cacheDir]]) {
    if (p && (!p.startsWith("/") || p.split("/").includes(".."))) {
      throw new Error(label + "必须是以 / 开头的绝对路径，且不能包含 ..");
    }
  }
  if (dlDir && cacheDir && (dlDir === cacheDir || dlDir.startsWith(cacheDir + "/") || cacheDir.startsWith(dlDir + "/"))) {
    throw new Error("歌曲缓存目录与下载目录不能相同或互为父子目录");
  }
  if (provider === "musicdl") {
    values.FNMUSIC_ONLINE_SOURCES = platforms.enabled.join(",");
    values.MUSICDL_SOURCES = platforms.enabled.join(",");
  }
  if (provider === "lxmusic") {
    if (!lxSourceList.some((item) => item.active)) {
      throw new Error("请至少激活一个洛雪源");
    }
    // LX_SOURCE_URL 由后端按第一个激活项派生写入
  }
  return values;
}

async function saveConfig() {
  let values;
  try { values = collectConfig(); } catch (exc) { toast(exc.message, "fail"); return; }
  // 目录门禁：变更过的目录先经宿主网关校验可写，不可写直接阻断保存
  // （直连 8774 等校验不可用的环境降级放行，不阻塞配置保存）
  for (const [key, label] of [["FNMUSIC_TEE_SAVE_DIR", "下载目录"], ["FNMUSIC_CACHE_DIR", "歌曲缓存目录"]]) {
    const next = values[key] || "";
    if (next && next !== (configValues[key] || "")) {
      const res = await fsCheckDir(next);
      if (res.mode === "fail") { toast(label + "不可用：" + res.message, "fail"); return; }
    }
  }
  const btn = $("#save-btn");
  btn.disabled = true;
  // 新激活的源保存时要逐个端到端校验（每源可达 1-2 分钟），给出预期提示
  const pendingNew = lxPendingActivationCount();
  btn.textContent = pendingNew > 0
    ? `保存中（校验 ${pendingNew} 个新激活源，可能需要 1-2 分钟）…`
    : "保存中…";
  try {
    const result = await api("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    });
    const failed = (result.actions || []).filter((a) => !a.ok);
    const parts = [];
    if (result.changed && result.changed.length) parts.push(`已保存 ${result.changed.length} 项`);
    (result.actions || []).forEach((a) => {
      if (a.kind === "process") parts.push(`进程 ${a.program} ${a.op} ${a.ok ? "成功" : "失败"}`);
      if (a.kind === "lx_activate") {
        const label = a.source ? `「${a.source}」` : "";
        const op = a.op === "deactivate" ? "取消激活" : "激活";
        parts.push(`洛雪源${label}${op}${a.ok ? "成功" : "失败"}`);
      }
    });
    if (failed.length) {
      toast((parts.join("；") || "") + ` —— ${failed.map((f) => f.error).join("；")}`, "fail");
    } else {
      toast(parts.join("；") || "配置无变化", "ok");
    }
    clearDirty();
    await loadConfig();
    await loadStatus();
  } catch (exc) {
    toast("保存失败：" + exc.message, "fail");
  } finally {
    btn.disabled = false;
    btn.textContent = "保存并生效";
  }
}
$("#save-btn").addEventListener("click", saveConfig);

/* -------------------------------------------------------------- 音源选择 */
function savedProvider() {
  const v = configValues;
  return v.FNMUSIC_NETEASE_ENABLED === "true" ? "musicbox"
    : v.FNMUSIC_MUSICDL_ENABLED === "true" ? "musicdl"
    : v.FNMUSIC_LX_ENABLED === "true" ? "lxmusic" : "";
}

// 点选未启用的音源：临时拉起其进程供预览（不写配置；5 分钟内未保存自动停止）
async function startPreview(provider) {
  try {
    const r = await api("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider }),
    });
    if (r.preview) toast(`已临时启动 ${PROVIDER_LABEL[provider]}（预览）：5 分钟内未保存将自动停止`);
    return true;
  } catch (exc) {
    toast(`临时启动 ${PROVIDER_LABEL[provider]} 失败：${exc.message}`, "fail");
    return false;
  }
}

function syncProviderPanels(provider) {
  $$(".provider-card").forEach((el) => el.classList.toggle("selected", el.dataset.provider === provider));
  $("#panel-musicbox").hidden = provider !== "musicbox";
  $("#panel-musicdl").hidden = provider !== "musicdl";
  $("#panel-lxmusic").hidden = provider !== "lxmusic";
}
$$("input[name=provider]").forEach((el) =>
  el.addEventListener("change", async () => {
    syncProviderPanels(el.value);
    if (el.value === "musicbox") syncNeteaseAccount();
    markDirty("音源切换需保存后生效");
    if (el.value && el.value !== savedProvider() && await startPreview(el.value)) {
      if (el.value === "musicdl") await loadPlatforms(false);
    }
  }));

/* -------------------------------------------------------------- musicdl 平台 */
function setPlatformFallback() {
  platforms = { enabled: (configValues.FNMUSIC_ONLINE_SOURCES || "").split(",").filter(Boolean), registered: [] };
  $("#platform-note").textContent = "musicdl 进程未运行，暂无法获取平台列表（点选 musicdl 音源可临时启动预览）";
  renderPlatformChips();
}

async function fetchPlatforms() {
  const data = await api("/api/platforms");
  platforms = { enabled: data.enabled || [], registered: data.registered || [] };
  $("#platform-note").textContent = `共 ${platforms.registered.length} 个注册平台，已启用 ${platforms.enabled.length} 个`;
  renderPlatformChips();
}

async function loadPlatforms(autoPreview = true) {
  try {
    await fetchPlatforms();
  } catch (_) {
    // 进程未运行：autoPreview（用户点选/刷新触发）时临时拉起后重试；页面加载不自动拉起
    if (!autoPreview) { setPlatformFallback(); return; }
    try {
      if (await startPreview("musicdl")) await fetchPlatforms();
      else setPlatformFallback();
    } catch (_) {
      setPlatformFallback();
    }
  }
}

function renderPlatformChips() {
  const keyword = $("#platform-search").value.trim().toLowerCase();
  const container = $("#platform-list");
  const enabledSet = new Set(platforms.enabled);
  const items = platforms.registered.filter((p) => !keyword || p.toLowerCase().includes(keyword));
  container.innerHTML = items.length
    ? items.map((p) => `<span class="chip ${enabledSet.has(p) ? "on" : ""}" data-platform="${p}">${p}</span>`).join("")
    : `<span class="muted">无匹配平台</span>`;
  container.querySelectorAll(".chip[data-platform]").forEach((chip) =>
    chip.addEventListener("click", () => {
      const p = chip.dataset.platform;
      const set = new Set(platforms.enabled);
      if (set.has(p)) { set.delete(p); chip.classList.remove("on"); }
      else { set.add(p); chip.classList.add("on"); }
      platforms.enabled = Array.from(set);
      markDirty("musicdl 平台已修改");
    }));
}
$("#platform-search").addEventListener("input", renderPlatformChips);
$("#platform-reload").addEventListener("click", loadPlatforms);

/* -------------------------------------------------------------- 网易扫码 */
async function syncNeteaseAccount(retries = 0) {
  // 把网易账号态同步到 #qr-check（已登录显示昵称；未登录/失败清空）
  for (let i = 0; ; i++) {
    try {
      const st = await api("/api/netease/auth/status");
      const d = st.data || st;
      if (d.logged_in) {
        $("#qr-check").textContent = `当前登录：${d.nickname || d.user_id || "已登录用户"}`;
        return;
      }
    } catch (_) { /* status 不可达视为未登录 */ }
    if (i >= retries) { $("#qr-check").textContent = ""; return; }
    await new Promise((r) => setTimeout(r, 1500));
  }
}

async function startQrLogin() {
  stopQrPolling();
  $("#qr-status").textContent = "正在生成二维码…";
  $("#qr-img").hidden = true;
  syncNeteaseAccount(); // 生成前同步当前账号态（非致命，不阻塞出码）
  const callLogin = () => api("/api/netease/auth/login", { method: "POST" });
  try {
    let data;
    try {
      data = await callLogin();
    } catch (exc) {
      // musicbox 进程未运行（未点选/预览过期）：临时拉起后重试一次
      if (!await startPreview("musicbox")) throw exc;
      data = await callLogin();
    }
    const unikey = data.unikey || data.codekey || (data.data && (data.data.unikey || data.data.codekey)) || "";
    if (!unikey) throw new Error("未获取到 unikey");
    $("#qr-img").src = `${APP_BASE}/api/netease/qr?unikey=${encodeURIComponent(unikey)}`;
    $("#qr-img").hidden = false;
    $("#qr-status").textContent = "请用手机网易云音乐 App 扫码";
    pollQr(unikey);
  } catch (exc) {
    $("#qr-status").textContent = "生成失败：" + exc.message;
  }
}

async function checkQrStatus(unikey) {
  const st = await api(`/api/netease/auth/login/check?unikey=${encodeURIComponent(unikey)}`);
  // musicbox CLI 返回 {ok, data:{code}} 信封结构；兼容扁平 {code}
  const code = st?.data?.code ?? st?.code;
  if (code === 803) {
    stopQrPolling();
    $("#qr-status").textContent = "登录成功 ✓";
    syncNeteaseAccount(2); // 803 后 cookie 落盘需要一点时间，带重试取昵称
    toast("网易账号登录成功", "ok");
  } else if (code === 802) {
    $("#qr-status").textContent = "已扫码，请在手机上确认";
  } else if (code === 800) {
    stopQrPolling();
    $("#qr-status").textContent = "二维码已过期，请重新生成";
  } else {
    $("#qr-status").textContent = "等待扫码…";
  }
}

function pollQr(unikey, intervalMs = 2000) {
  stopQrPolling();
  qrTimer = setInterval(() => { checkQrStatus(unikey).catch(() => {}); }, intervalMs);
}

function stopQrPolling() {
  if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
}
$("#qr-btn").addEventListener("click", startQrLogin);

/* -------------------------------------------------------------- lx 源测试 */
function escapeHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function lxDefaultName(url) {
  if (!url) return "自定义源";
  try {
    const raw = url.split("?")[0].split("#")[0];
    const segment = raw.split("/").filter(Boolean).pop() || "lx-source";
    return decodeURIComponent(segment);
  } catch (_) {
    return "自定义源";
  }
}

function syncLxAddButton() {
  const url = ($("#lx-url")?.value || "").trim();
  const addBtn = $("#lx-add");
  if (addBtn) addBtn.disabled = !url;
}

// 已保存的激活 URL 集合（旧数据无 active 标记时回退 LX_SOURCE_URL 单值）
function lxSavedActiveUrls() {
  const saved = new Set();
  try {
    const items = JSON.parse(configValues.LX_SOURCE_LIST || "[]");
    if (Array.isArray(items)) items.forEach((i) => { if (i && i.active && i.url) saved.add(i.url); });
  } catch (_) { /* 忽略 */ }
  if (!saved.size) {
    const u = (configValues.LX_SOURCE_URL || "").trim();
    if (u) saved.add(u);
  }
  return saved;
}

function lxPendingActivationCount() {
  const saved = lxSavedActiveUrls();
  return lxSourceList.filter((i) => i.active && !saved.has(i.url)).length;
}

function renderLxSourceList() {
  const container = $("#lx-source-list");
  if (!container) return;

  if (!lxSourceList.length) {
    container.innerHTML = `<span class="muted pad">暂无洛雪音乐源，上传 .js 或填写脚本地址后点击「添加」加入列表</span>`;
    return;
  }

  container.innerHTML = lxSourceList.map((item, idx) => {
    const isActive = !!item.active;
    const displayName = escapeHtml(item.name || lxDefaultName(item.url));
    const safeUrl = escapeHtml(item.url);
    const btns = isActive
      ? `<button class="btn small lx-btn-deactivate" data-idx="${idx}">取消激活</button>
         <button class="btn small danger lx-btn-del" data-idx="${idx}">删除</button>`
      : `<button class="btn small lx-btn-activate" data-idx="${idx}">激活</button>
         <button class="btn small danger lx-btn-del" data-idx="${idx}">删除</button>`;
    return `
      <div class="lx-item${isActive ? " on" : ""}">
        <div class="lx-item-main">
          <div class="lx-item-header">
            <span class="lx-item-name">${displayName}</span>
            ${isActive ? `<span class="lx-badge">已激活</span>` : ""}
          </div>
          <div class="lx-item-url" title="${safeUrl}">${safeUrl}</div>
        </div>
        <div class="lx-item-btns">${btns}</div>
      </div>`;
  }).join("");

  container.querySelectorAll(".lx-btn-activate").forEach((btn) => {
    btn.addEventListener("click", () => activateLxSource(parseInt(btn.dataset.idx, 10)));
  });
  container.querySelectorAll(".lx-btn-deactivate").forEach((btn) => {
    btn.addEventListener("click", () => deactivateLxSource(parseInt(btn.dataset.idx, 10)));
  });
  container.querySelectorAll(".lx-btn-del").forEach((btn) => {
    btn.addEventListener("click", () => deleteLxSource(parseInt(btn.dataset.idx, 10)));
  });
}

function activateLxSource(idx) {
  const item = lxSourceList[idx];
  if (!item || item.active) return;
  item.active = true;
  renderLxSourceList();
  markDirty("已标记激活，需点击下方「保存并生效」（新激活源会先自动校验）");
  toast(`已标记激活「${item.name || lxDefaultName(item.url)}」，请点击下方「保存并生效」`, "ok");
}

function deactivateLxSource(idx) {
  const item = lxSourceList[idx];
  if (!item || !item.active) return;
  item.active = false;
  renderLxSourceList();
  markDirty("已标记取消激活，需点击下方「保存并生效」");
  toast(`已标记取消激活「${item.name || lxDefaultName(item.url)}」，保存后停用`, "ok");
}

function deleteLxSource(idx) {
  const item = lxSourceList[idx];
  if (!item) return;
  const name = item.name || lxDefaultName(item.url);
  lxSourceList.splice(idx, 1);
  renderLxSourceList();
  markDirty("洛雪源列表已修改，需保存后生效");
  toast(`已从列表移除「${name}」${item.active ? "（保存后将一并停用）" : ""}`, "ok");
}

$("#lx-test").addEventListener("click", async () => {
  const url = $("#lx-url").value.trim();
  const box = $("#lx-report");
  if (!url) { toast("请先填写源 URL", "fail"); return; }
  box.hidden = false;
  box.className = "report";
  box.textContent = "测试中（下载脚本 → 沙箱初始化 → 多首抽样搜索/解析/探活）…";
  const callVerify = () => api("/api/lx/verify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ values: { url } }),
  });
  try {
    let r;
    try {
      r = await callVerify();
    } catch (exc) {
      // lxmusic 进程未运行（未点选/预览过期）：临时拉起后重试一次
      if (!await startPreview("lxmusic")) throw exc;
      r = await callVerify();
    }
    if (r.ok) {
      const d = r.data || {};
      const meta = d.meta || {};
      const probe = d.probe || {};
      lxVerifiedUrl = url;
      if (meta.name && $("#lx-name") && !$("#lx-name").value.trim()) {
        $("#lx-name").value = meta.name;
      }
      box.className = "report ok";
      box.innerHTML =
        `<div class="kv"><b>源名称</b>${meta.name || "-"} ${meta.version ? "v" + meta.version : ""}（${meta.author || "未知作者"}）</div>` +
        `<div class="kv"><b>可用平台</b>${(d.platforms || []).join("、")}</div>` +
        (probe.title ? `<div class="kv"><b>实测</b>${probe.title} - ${probe.artist} [${probe.platform}/${probe.quality}] ${probe.content_type || ""}</div>` : "") +
        `<div class="kv"><b>结论</b>可用 ✓（保存后激活）</div>`;
    } else {
      const d = r.data || {};
      box.className = "report fail";
      box.innerHTML = `<div class="kv"><b>不可用</b>${d.message || r.error || "校验失败"}</div>`;
    }
  } catch (exc) {
    box.className = "report fail";
    box.textContent = "测试失败：" + exc.message;
  }
});

/* -------------------------------------------------- lx 源：文件上传 / NAS 选择 */
async function lxUploadScript(filename, script) {
  // 先确保 lxmusic 进程可用（预览拉起），再转发落盘
  const callUpload = () => api("/api/lx/upload", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename, script }),
  });
  try {
    return await callUpload();
  } catch (exc) {
    if (!await startPreview("lxmusic")) throw exc;
    return await callUpload();
  }
}

async function lxAfterUpload(r) {
  const d = r.data || {};
  $("#lx-url").value = d.url || "";
  lxVerifiedUrl = null; // 上传地址仍需走一次"测试"
  const scriptName = (d.meta && d.meta.name) || "";
  if (scriptName && $("#lx-name") && !$("#lx-name").value.trim()) {
    $("#lx-name").value = scriptName;
  }
  $("#lx-upload-note").textContent = scriptName ? `已上传：${scriptName}` : "已上传";
  syncLxAddButton();
  toast("脚本已上传，请点「测试」验证，确认可用后点「添加」加入列表", "ok");
}

$("#lx-upload").addEventListener("click", () => $("#lx-file").click());
$("#lx-file").addEventListener("change", async () => {
  const file = $("#lx-file").files && $("#lx-file").files[0];
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".js")) { toast("只支持 .js 后缀文件", "fail"); return; }
  if (file.size > 9_000_000) { toast("脚本超过 9MB 上限", "fail"); return; }
  $("#lx-upload-note").textContent = "读取并上传中…";
  try {
    const script = await file.text();
    const r = await lxUploadScript(file.name, script);
    await lxAfterUpload(r);
  } catch (exc) {
    $("#lx-upload-note").textContent = "";
    toast("上传失败：" + exc.message, "fail");
  } finally {
    $("#lx-file").value = ""; // 允许重复选择同一文件
  }
});

/* NAS 文件选择：仅桌面环境（统一网关 /app/fnmusic-ext 内）可用。
   洛雪源选中的主机路径经 /api/host-file 代读（webui_gateway 本地处理），再走上传落盘；
   储存目录选中即完成飞牛授权（pickUserFile directory 模式）。
   SDK 全站共享单例：加载失败（直连 8774）置 false，调用方自行降级。 */
let trimSdk = null;
async function loadTrimSdk() {
  if (trimSdk !== null) return trimSdk;
  try {
    const mod = await import("/app/fnmusic-ext/static/vendor/trim-web-app.js");
    trimSdk = new mod.TrimApp();
  } catch (_) {
    trimSdk = false;
  }
  return trimSdk;
}

async function lxPickFromNas() {
  const sdk = await loadTrimSdk();
  if (!sdk) { toast("当前环境不支持 NAS 文件选择（直连 8774 时请用上传或 URL）", "fail"); return; }
  try {
    const result = await sdk.pickUserFile({
      directory: false,
      accept: [".js"],
      title: "选择洛雪源脚本",
      okText: "选择",
      sidebarGroup: ["myFiles", "otherShare", "favorites"],
    });
    const paths = (result && result.data) || [];
    if (!paths.length) return;
    const hostPath = paths[0];
    $("#lx-upload-note").textContent = "读取 NAS 文件中…";
    const resp = await fetch(APP_BASE + "/api/host-file", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: hostPath }),
    });
    let body = {};
    try { body = await resp.json(); } catch (_) { /* 非 JSON */ }
    if (!resp.ok) throw new Error(body.error || body.detail || `HTTP ${resp.status}`);
    const filename = hostPath.split("/").pop() || "source.js";
    const r = await lxUploadScript(filename, body.script || "");
    await lxAfterUpload(r);
  } catch (exc) {
    $("#lx-upload-note").textContent = "";
    toast("NAS 选择失败：" + exc.message, "fail");
  }
}
$("#lx-pick").addEventListener("click", lxPickFromNas);

$("#lx-add").addEventListener("click", () => {
  const url = ($("#lx-url")?.value || "").trim();
  if (!url) {
    toast("请先填写脚本地址或上传 .js 文件", "fail");
    return;
  }
  const nameInput = ($("#lx-name")?.value || "").trim();
  const name = nameInput || lxDefaultName(url);

  const existingIdx = lxSourceList.findIndex((item) => item.url === url);
  if (existingIdx >= 0) {
    lxSourceList[existingIdx].name = name;
    toast(`已更新列表中该源的名称为「${name}」`, "ok");
  } else {
    lxSourceList.push({ name, url, active: false });
    toast(`已添加「${name}」至洛雪源列表，点其「激活」并保存后生效`, "ok");
  }
  $("#lx-url").value = "";
  if ($("#lx-name")) $("#lx-name").value = "";
  syncLxAddButton();
  renderLxSourceList();
  markDirty("洛雪源列表已修改，需保存后生效");
});

/* -------------------------------------------------- 储存目录：选择/授权/校验/镜像同步 */
const DIR_FIELDS = [
  { input: "#dir-cache", pick: "#dir-cache-pick", check: "#dir-cache-check", label: "歌曲缓存目录" },
  { input: "#dir-download", pick: "#dir-download-pick", check: "#dir-download-check", label: "下载目录" },
];
let _fsCheckSeq = 0;                // 防抖竞态：只采纳最后一次输入的校验结果
const _authorizedDirs = new Set();  // authorizeUserFile 每个路径只尝试一次，避免重复弹授权框

/* 目录权限校验：桌面链路由宿主机网关（root，真实落盘视角）拦截应答；
   直连 8774 时 WebUI 返回 501（容器内看不到宿主路径），降级为 skip 放行。 */
async function fsCheckDir(path) {
  try {
    const r = await api("/api/fs-check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    if (!r.writable) {
      return { mode: "fail", message: r.exists ? "目录不可写（权限不足或只读挂载）" : "目录无法创建（父目录不可写）" };
    }
    if (!r.exists) return { mode: "warn", message: "目录不存在；父目录可写，保存后自动创建" };
    return { mode: "ok", message: "目录可写 ✓" };
  } catch (exc) {
    return { mode: "skip", message: "目录校验不可用（" + exc.message + "），保存时不校验权限" };
  }
}

function renderDirCheck(el, res) {
  el.hidden = false;
  el.className = "dir-check " + res.mode;
  el.textContent = res.message;
}

/* 粘贴路径补一次官方授权（飞牛 userAccess；选择器选中的路径已自动授权，无需重复）。
   授权失败不阻断：代理以 root 落盘，网关 fs-check 才是真实写入视角。 */
async function authorizeDir(path) {
  if (_authorizedDirs.has(path)) return;
  _authorizedDirs.add(path);
  try {
    const sdk = await loadTrimSdk();
    if (sdk && typeof sdk.authorizeUserFile === "function") await sdk.authorizeUserFile(path);
  } catch (_) { /* 忽略 */ }
}

function bindDirField(field) {
  const input = $(field.input);
  let timer = null;
  const mirror = () => {
    // 下载目录与边听边存页的保存路径是同一配置，输入后同步到另一处
    if (field.input === "#dir-download") $("#tee-dir").value = input.value;
  };
  const schedule = () => {
    markDirty();
    mirror();
    clearTimeout(timer);
    const seq = ++_fsCheckSeq;
    const path = input.value.trim();
    if (!path) { $(field.check).hidden = true; return; }
    timer = setTimeout(async () => {
      await authorizeDir(path);
      const res = await fsCheckDir(path);
      if (seq === _fsCheckSeq) renderDirCheck($(field.check), res);
    }, 500);
  };
  input.addEventListener("input", schedule);
  $(field.pick).addEventListener("click", async () => {
    const sdk = await loadTrimSdk();
    if (!sdk) { toast("当前环境不支持 NAS 目录选择（直连 8774 时请直接粘贴路径）", "fail"); return; }
    try {
      const result = await sdk.pickUserFile({
        directory: true, // 目录授权只支持单选（官方文档：multiple 也会按单目录处理）
        title: "选择" + field.label,
        okText: "选择",
        sidebarGroup: ["myFiles", "otherShare", "favorites"],
      });
      const paths = (result && result.data) || [];
      if (!paths.length) return;
      input.value = paths[0];
      _authorizedDirs.add(paths[0]); // 选择器选中即完成授权
      schedule();
    } catch (exc) {
      toast("NAS 目录选择失败：" + exc.message, "fail");
    }
  });
}
DIR_FIELDS.forEach(bindDirField);

// 边听边存页的保存路径与目录设置页的下载目录是同一配置：此处输入同步过去并标脏
// （反向同步由 bindDirField 的 mirror 完成）
$("#tee-dir").addEventListener("input", () => {
  markDirty();
  $("#dir-download").value = $("#tee-dir").value;
});

(async function detectNasPicker() {
  // 桌面网关路径下才尝试加载 SDK；探测失败（直连 8774）保持隐藏
  const pathname = (typeof window !== "undefined" && window.location && window.location.pathname) || "";
  if (pathname.startsWith("/app/")) {
    const sdk = await loadTrimSdk();
    if (sdk) {
      $("#lx-pick").hidden = false;
      DIR_FIELDS.forEach((f) => { $(f.pick).hidden = false; });
    }
  }
})();

/* -------------------------------------------------------------- 表单脏标记 */
["#tee-max", "#llm-base", "#llm-key", "#llm-model", "#search-timeout", "#search-deep-max", "#bind-timeout", "#handoff-max", "#scan-path"].forEach((sel) =>
  $(sel).addEventListener("input", () => markDirty()));
$("#lx-url").addEventListener("input", () => {
  // #lx-url 仅为「添加」入口输入框，激活态由列表 active 标记决定，与它无关
  syncLxAddButton();
});
if ($("#lx-name")) {
  $("#lx-name").addEventListener("input", () => markDirty());
}
$$("input[name=quality]").forEach((el) => el.addEventListener("change", () => markDirty("音质偏好需保存后生效")));
$$("input[name=dl-quality]").forEach((el) => el.addEventListener("change", () => markDirty("下载音质需保存后生效")));
["#recommend-hot", "#recommend-daily", "#search-probe", "#search-deep", "#tee-enabled", "#fav-autobind", "#auto-cover", "#lyric-auto-dl", "#netease-my-playlists"].forEach((sel) =>
  $(sel).addEventListener("change", () => markDirty()));

function updateTeeCountLabel() {
  const n = $("#tee-max").value || configValues.FNMUSIC_TEE_CACHE_MAX || "2";
  $("#tee-count-label").textContent = `（缓存数 ${n} 首）`;
}
$("#tee-max").addEventListener("input", updateTeeCountLabel);

function updateBindTimeoutLabel() {
  const n = $("#bind-timeout").value || configValues.FNMUSIC_OFFICIAL_BIND_TIMEOUT_S || "120";
  $("#bind-timeout-label").textContent = `（${n} 秒）`;
}
$("#bind-timeout").addEventListener("input", updateBindTimeoutLabel);

window.addEventListener("beforeunload", (ev) => {
  if (dirty) ev.preventDefault();
});

/* -------------------------------------------------------------- 启动 */
(async function boot() {
  await loadConfig();
  await loadStatus();
  await loadPlatforms(false);  // 页面加载不自动拉起预览进程，等用户点选音源
  setInterval(loadStatus, 15000);
})();
