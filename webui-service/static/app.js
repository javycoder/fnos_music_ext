/* fnmusic-ext WebUI — 原生 JS，无框架无外部资产。 */
"use strict";

// 飞牛桌面用 HTTPS 打开管理窗，页面必须挂在同源路径 /app/fnmusic-ext 下。
// WebUI 自己也会剥掉这个前缀。管理接口只认飞牛网关注入的管理员头。
const APP_BASE = "/app/fnmusic-ext";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const PROVIDER_LABEL = { musicdl: "musicdl 聚合音源", musicbox: "网易云音乐盒子", lxmusic: "洛雪自定义源", none: "未配置" };
const PROC_LABEL = { musicdl: "musicdl", musicbox: "musicbox", lxserver: "lxserver", lxmusic: "lxmusic", webui: "WebUI" };

let configValues = {};   // GET /api/config 的 values
let configPresence = {}; // .env 显式存在的键（区分默认空与管理员清空）
let platforms = { enabled: [], registered: [] };
let dirty = false;
let qrTimer = null;
let lxVerifiedUrl = null; // 已通过测试的 lx URL（保存时免二次校验提示用）
let lxSourceList = [];    // 洛雪源列表 [{ name, url, active }]，active 可多开
let updateInfo = null;    // /api/update-check 结果（菜单红点 / NEW 标记 / 详情弹窗共用）
let publicSelection = [];
let publicCapabilities = null;
let publicState = { provider: "musicbox", channel: "charts", source: "netease", category: "", sort: "", page: 1, items: [], hasMore: false, total: 0, cursor: "" };

async function api(path, options) {
  const resp = await fetch(APP_BASE + path, options);
  let body = {};
  try { body = await resp.json(); } catch (_) { /* 非 JSON */ }
  if (!resp.ok) throw new Error(body.error || body.detail || `HTTP ${resp.status}`);
  return body;
}

function updateLlmLikedSettings() {
  $("#llm-liked-settings").hidden = !$("#llm-liked").checked;
  const selected = $("#llm-liked").checked || $("#llm-daily").checked;
  $("#llm-api-warning").hidden = !selected || !!($("#llm-base").value.trim() && $("#llm-key").value.trim());
}

function likedNumber(selector, label, min, max, integer = false) {
  const text = $(selector).value.trim();
  const num = Number(text);
  if (!text || !Number.isFinite(num) || num < min || (max != null && num > max) || (integer && !Number.isInteger(num))) {
    throw new Error(label + (integer ? "必须是 0–100 的整数" : "必须是大于或等于 0.5 的有限数字"));
  }
  return num;
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
  if (page === "about") loadAbout().catch((exc) => { $("#about-logs").textContent = "加载失败：" + exc.message; });
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
      return `<span class="chip"><span class="dot ${cls}"></span>${escapeHtml(PROC_LABEL[name] || name)} · ${escapeHtml(p.state)}</span>`;
    }).join("");
    $("#ov-services").innerHTML = Object.entries(st.services).map(([name, s]) => {
      if (!s.reachable && s.note) return `<span class="state-line"><span class="dot"></span>${escapeHtml(PROC_LABEL[name])}：${escapeHtml(s.note)}</span>`;
      if (!s.reachable) {
        // 后端探活失败时透出 healthz 返回的具体原因（如 lxserver unreachable），
        // 便于区分"进程异常"与"进程在但底层服务不可达"
        const why = String(s.detail || "").trim();
        return `<span class="state-line"><span class="dot err"></span>${escapeHtml(PROC_LABEL[name])}：不可达${why ? "（" + escapeHtml(why) + "）" : ""}</span>`;
      }
      return `<span class="state-line"><span class="dot ok"></span>${escapeHtml(PROC_LABEL[name])}：正常</span>`;
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
          `${escapeHtml(s.name || "-")}${s.version ? " v" + escapeHtml(s.version) : ""}`).join("、")}</div>`);
        const plats = new Set();
        sources.forEach((s) => (s.platforms || []).forEach((p) => plats.add(p)));
        rows.push(`<div class="kv"><b>平台</b>${escapeHtml([...plats].join("、")) || "-"}</div>`);
      } else if (ls.source) {
        rows.push(`<div class="kv"><b>源名称</b>${escapeHtml(ls.source.name || "-")} ${ls.source.version ? "v" + escapeHtml(ls.source.version) : ""}</div>`);
        rows.push(`<div class="kv"><b>平台</b>${escapeHtml(Object.keys(ls.source.platforms || {}).join("、")) || "-"}</div>`);
      }
      if (ls.last_error) rows.push(`<div class="kv"><b>错误</b>${escapeHtml(ls.last_error)}</div>`);
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
  configPresence = cfg.presence || {};
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
  $("#public-playlists-enabled").checked = v.FNMUSIC_PUBLIC_PLAYLISTS_ENABLED === "true";
  try {
    publicSelection = JSON.parse(v.FNMUSIC_PUBLIC_PLAYLISTS_SELECTION || "[]");
    if (!Array.isArray(publicSelection)) publicSelection = [];
  } catch (_) { publicSelection = []; }
  $("#public-playlists-browser").hidden = !$("#public-playlists-enabled").checked;
  renderPublicSelected();
  $("#tee-enabled").checked = v.FNMUSIC_TEE_SAVE_ENABLED === "true";
  $("#auto-cover").checked = v.FNMUSIC_AUTO_COVER !== "false";
  $("#lyric-auto-dl").checked = v.FNMUSIC_LYRIC_AUTO_DL === "true";
  $("#fav-autobind").checked = v.FNMUSIC_FAV_AUTO_BIND === "true";
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
  $("#llm-liked").checked = v.FNMUSIC_LLM_LIKED_ENABLED === "true";
  $("#llm-daily").checked = v.FNMUSIC_LLM_DAILY_ENABLED === "true";
  $("#liked-refresh-hours").value = v.FNMUSIC_LIKED_REFRESH_HOURS ?? "0.5";
  $("#liked-size").value = v.FNMUSIC_LIKED_SIZE ?? "20";
  updateLlmLikedSettings();
  $("#search-timeout").value = v.FNMUSIC_SEARCH_TIMEOUT || "15";
  $("#search-probe").checked = v.FNMUSIC_SEARCH_PROBE === "true";
  $("#search-deep").checked = v.FNMUSIC_SEARCH_DEEP_PAGE !== "false";
  $("#search-deep-max").value = v.FNMUSIC_SEARCH_DEEP_MAX_PAGES || "10";
  $("#netease-my-playlists").checked = v.FNMUSIC_NETEASE_MY_PLAYLISTS === "true";
  $("#netease-playlist-resident").checked = v.FNMUSIC_NETEASE_PLAYLIST_RESIDENT === "true";
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
  if (lxSourceList.length && !lxSourceList.some((item) => "active" in item)) {
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
    FNMUSIC_PUBLIC_PLAYLISTS_ENABLED: $("#public-playlists-enabled").checked,
    FNMUSIC_TEE_SAVE_ENABLED: $("#tee-enabled").checked,
    FNMUSIC_AUTO_COVER: $("#auto-cover").checked,
    FNMUSIC_LYRIC_AUTO_DL: $("#lyric-auto-dl").checked,
    FNMUSIC_FAV_AUTO_BIND: $("#fav-autobind").checked,
    FNMUSIC_TEE_SAVE_DIR: $("#dir-download").value.trim(),
    FNMUSIC_CACHE_DIR: $("#dir-cache").value.trim(),
    FNMUSIC_TEE_CACHE_MAX: parseInt($("#tee-max").value || "2", 10),
    FNMUSIC_OFFICIAL_BIND_TIMEOUT_S: parseInt($("#bind-timeout").value || "120", 10) || 120,
    FNMUSIC_TEE_HANDOFF_MAX: parseInt($("#handoff-max").value || "3", 10) || 0,
    FNMUSIC_LIBRARY_SCAN_PATH: $("#scan-path").value.trim(),
    FNMUSIC_LLM_BASE_URL: $("#llm-base").value.trim(),
    FNMUSIC_LLM_API_KEY: $("#llm-key").value.trim(),
    FNMUSIC_LLM_MODEL: $("#llm-model").value.trim(),
    FNMUSIC_LLM_LIKED_ENABLED: $("#llm-liked").checked,
    FNMUSIC_LLM_DAILY_ENABLED: $("#llm-daily").checked,
    FNMUSIC_SEARCH_TIMEOUT: parseInt($("#search-timeout").value || "15", 10) || 15,
    FNMUSIC_SEARCH_PROBE: $("#search-probe").checked,
    FNMUSIC_SEARCH_DEEP_PAGE: $("#search-deep").checked,
    FNMUSIC_SEARCH_DEEP_MAX_PAGES: parseInt($("#search-deep-max").value || "10", 10) || 10,
    FNMUSIC_NETEASE_MY_PLAYLISTS: $("#netease-my-playlists").checked,
    FNMUSIC_NETEASE_PLAYLIST_RESIDENT: $("#netease-playlist-resident").checked,
    LX_SOURCE_LIST: JSON.stringify(lxSourceList),
  };
  // 隐藏的参数不提交：关闭用途不清空已保存值，也不让隐藏的无效编辑阻塞保存。
  if (values.FNMUSIC_LLM_LIKED_ENABLED) {
    values.FNMUSIC_LIKED_REFRESH_HOURS = likedNumber("#liked-refresh-hours", "猜你喜欢生成间隔", 0.5, null);
    values.FNMUSIC_LIKED_SIZE = likedNumber("#liked-size", "猜你喜欢歌曲数量", 0, 100, true);
  }
  if ($("#public-playlists-enabled").checked || publicSelection.length || configPresence.FNMUSIC_PUBLIC_PLAYLISTS_SELECTION) {
    values.FNMUSIC_PUBLIC_PLAYLISTS_SELECTION = JSON.stringify(
      publicSelection.map(({ provider, source, kind, id }) => ({ provider, source, kind, id }))
    );
  }
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
    let savedAllInactive = false;
    try {
      const saved = JSON.parse(configValues.LX_SOURCE_LIST || "[]");
      savedAllInactive = configValues.FNMUSIC_LX_ENABLED === "true"
        && saved.length > 0 && saved.every((i) => i.active === false)
        && !(configValues.LX_SOURCE_URL || "").trim();
    } catch (_) { /* 忽略 */ }
    if (!lxSourceList.some((item) => item.active) && !savedAllInactive) {
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
    const failed = (result.actions || []).filter((a) => !a.ok && !a.auto_disabled);
    const warnings = result.warnings || [];
    const parts = [];
    if (result.changed && result.changed.length) parts.push(`已保存 ${result.changed.length} 项`);
    else if (warnings.length) parts.push("配置已保存");
    (result.actions || []).forEach((a) => {
      if (a.kind === "process") parts.push(`进程 ${a.program} ${a.op} ${a.ok ? "成功" : "失败"}`);
      if (a.kind === "resident" && a.ok) {
        parts.push(a.op === "start" ? "网易云歌单常驻进程已启动" : "网易云歌单常驻进程已停止");
      }
      if (a.kind === "lx_activate" && !a.auto_disabled) {
        const label = a.source ? `「${a.source}」` : "";
        const op = a.op === "deactivate" ? "取消激活" : "激活";
        parts.push(`洛雪源${label}${op}${a.ok ? "成功" : "失败"}`);
      }
    });
    warnings.forEach((w) => {
      parts.push(`洛雪源「${w.source || lxDefaultName(w.url)}」失效，无法激活，已自动取消激活${w.message ? `（${w.message}）` : ""}`);
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

/* -------------------------------------------------------------- 公共歌单 */
const PUBLIC_PROVIDER_OPTIONS = [
  { value: "musicbox", label: "网易云音乐" },
  { value: "lxmusic", label: "洛雪内置歌单" },
];
const PUBLIC_SOURCE_LABEL = { netease: "网易", wy: "网易", kw: "酷我", kg: "酷狗", tx: "QQ", mg: "咪咕" };

function publicEntryKey(item) {
  return [item.provider, item.source, item.provider === "musicbox" ? "playlist" : item.kind, item.id].join("|");
}

function selectedPublicEntry(item) {
  return publicSelection.find((it) => publicEntryKey(it) === publicEntryKey(item));
}

function setPublicSelection(item, selected) {
  if (selected) {
    if (selectedPublicEntry(item)) return true;
    if (publicSelection.length >= 50) {
      toast("公共歌单最多选择 50 份", "fail");
      return false;
    }
    publicSelection.push({
      provider: item.provider, source: item.source, kind: item.kind, id: String(item.id),
      _name: item.name || "", _cover_url: item.cover_url || "", _track_count: item.track_count || 0,
    });
  } else {
    publicSelection = publicSelection.filter((it) => publicEntryKey(it) !== publicEntryKey(item));
  }
  renderPublicSelected();
  updateResidentAvailability();
  markDirty("公共歌单选择已修改");
  renderPublicList();
  return true;
}

function movePublicSelection(index, delta) {
  const next = index + delta;
  if (next < 0 || next >= publicSelection.length) return;
  const items = publicSelection.splice(index, 1);
  publicSelection.splice(next, 0, items[0]);
  renderPublicSelected();
  markDirty("公共歌单顺序已修改");
}

function renderPublicSelected() {
  const box = $("#public-selected");
  if (!box) return;
  box.innerHTML = publicSelection.length ? publicSelection.map((item, index) => `
    <div class="public-selected">
      <img src="${escapeHtml(item._cover_url || "")}" alt="" ${item._cover_url ? "" : "hidden"}>
      <div class="min">
        <b>${escapeHtml(item._name || item.id)}</b>
        <span>${escapeHtml(PUBLIC_SOURCE_LABEL[item.source] || item.source)} · ${item.kind === "chart" ? "榜单" : "歌单"} · ${item.id}</span>
        ${item._reason ? `<small>${escapeHtml(item._reason)}</small>` : ""}
        ${Number(item._track_count || 0) > 1000 ? '<small>超过 1,000 首，仅载入前 1,000 首。</small>' : ""}
      </div>
      <div class="btn-row">
        <button class="btn small" data-public-up="${index}" ${index === 0 ? "disabled" : ""}>↑</button>
        <button class="btn small" data-public-down="${index}" ${index === publicSelection.length - 1 ? "disabled" : ""}>↓</button>
        <button class="btn small danger" data-public-remove="${index}">移除</button>
      </div>
    </div>
  `).join("") : '<p class="hint">尚未选择歌单。</p>';
  box.querySelectorAll("[data-public-up]").forEach((btn) =>
    btn.addEventListener("click", () => movePublicSelection(Number(btn.dataset.publicUp), -1)));
  box.querySelectorAll("[data-public-down]").forEach((btn) =>
    btn.addEventListener("click", () => movePublicSelection(Number(btn.dataset.publicDown), 1)));
  box.querySelectorAll("[data-public-remove]").forEach((btn) =>
    btn.addEventListener("click", () => {
      publicSelection.splice(Number(btn.dataset.publicRemove), 1);
      updateResidentAvailability();
      renderPublicSelected(); renderPublicList(); markDirty("公共歌单选择已修改");
    }));
}

function renderPublicList() {
  const box = $("#public-list");
  if (!box) return;
  box.innerHTML = publicState.items.length ? publicState.items.map((item) => {
    const checked = !!selectedPublicEntry(item);
    return `
      <label class="public-item">
        <input type="checkbox" data-public-id="${escapeHtml(String(item.id))}" ${checked ? "checked" : ""}>
        <img src="${escapeHtml(item.cover_url || "")}" alt="" ${item.cover_url ? "" : "hidden"}>
        <span class="min">
          <b>${escapeHtml(item.name || item.id)}</b>
          <small>${escapeHtml(item.author || PUBLIC_SOURCE_LABEL[item.source] || item.source)} · ${Number(item.track_count || 0)} 首</small>
        </span>
      </label>`;
  }).join("") : '<p class="hint">当前入口没有返回歌单。</p>';
  box.querySelectorAll("input[data-public-id]").forEach((input) => {
    input.addEventListener("change", () => {
      const item = publicState.items.find((it) => String(it.id) === input.dataset.publicId);
      if (item) setPublicSelection(item, input.checked);
    });
  });
}

async function publicApi(target, params = {}, retry = true) {
  const query = new URLSearchParams({ provider: publicState.provider, ...params });
  try {
    const data = await api(`/api/public-playlists/${target}?${query.toString()}`);
    if (data.ok === false) throw new Error(data.error || "歌单请求失败");
    return data;
  } catch (exc) {
    if (!retry) throw exc;
    if (!await startPreview(publicState.provider)) throw exc;
    return await publicApi(target, params, false);
  }
}

function currentUiProvider() {
  return ($$("input[name=provider]").find((el) => el.checked) || {}).value || savedProvider();
}

async function loadPublicCapabilities(retry = true) {
  try {
    publicCapabilities = await publicApi("capabilities");
  } catch (exc) {
    publicCapabilities = null;
    $("#public-note").textContent = "音源不可达：" + exc.message;
  }
  renderPublicControls();
  return publicCapabilities;
}

function renderPublicControls() {
  const providerSelect = $("#public-provider");
  const channelSelect = $("#public-channel");
  const sourceSelect = $("#public-source");
  providerSelect.innerHTML = PUBLIC_PROVIDER_OPTIONS.map((item) =>
    `<option value="${item.value}">${item.label}</option>`).join("");
  providerSelect.value = publicState.provider;

  const channels = publicState.provider === "musicbox" ? [
    { id: "charts", name: "排行榜" }, { id: "recommend", name: "推荐歌单" },
    { id: "top", name: "热门与分类" }, { id: "highquality", name: "精品歌单" },
    { id: "account_recommend", name: "账号推荐歌单" },
  ] : [{ id: "top", name: "热门与分类" }, { id: "charts", name: "排行榜" }];
  const logged = !!(((publicCapabilities || {}).sources || []).find((item) => item.id === "netease") || {}).logged_in;
  channelSelect.innerHTML = channels.filter((item) =>
    item.id !== "account_recommend" || logged).map((item) =>
    `<option value="${item.id}">${item.name}</option>`).join("");
  if (![...channelSelect.options].some((opt) => opt.value === publicState.channel)) {
    publicState.channel = publicState.provider === "musicbox" ? "charts" : "top";
  }
  channelSelect.value = publicState.channel;

  sourceSelect.hidden = publicState.provider !== "lxmusic";
  const sources = ((publicCapabilities || {}).sources || []).filter((item) =>
    publicState.provider !== "lxmusic" || item.playback_available);
  sourceSelect.innerHTML = sources.map((item) =>
    `<option value="${item.id}">${item.playback_available ? "" : "⚠ "}${escapeHtml(item.name)}</option>`).join("");
  if (!sources.some((item) => item.id === publicState.source)) publicState.source = sources[0]?.id || "";
  sourceSelect.value = publicState.source;
}

function fillPublicCategories(data) {
  publicState.cursor = "";
  publicState.cursors = { 1: "" };
  const select = $("#public-category");
  const groups = Array.isArray(data.categories) ? data.categories : [];
  select.innerHTML = '<option value="">全部分类</option>' + groups.map((group) => {
    const list = Array.isArray(group.list) ? group.list : [group];
    return `<optgroup label="${escapeHtml(group.name || "分类")}">${list.map((tag) =>
      `<option value="${escapeHtml(String(tag.id ?? tag.name ?? ""))}">${escapeHtml(tag.name || tag.id)}</option>`).join("")}</optgroup>`;
  }).join("");
  publicState.category = "";
  const sorts = Array.isArray(data.sorts) ? data.sorts : [];
  $("#public-sort").hidden = !sorts.length;
  $("#public-sort").innerHTML = sorts.map((item) =>
    `<option value="${escapeHtml(String(item.id))}">${escapeHtml(item.name || item.id)}</option>`).join("");
  publicState.sort = sorts[0]?.id || "";
}

async function loadPublicCategories() {
  if (publicState.channel === "recommend" || publicState.channel === "account_recommend") {
    fillPublicCategories({ categories: [] });
    return;
  }
  try {
    const highquality = publicState.provider === "musicbox" && publicState.channel === "highquality";
    const data = await publicApi("categories", {
      source: publicState.source, kind: publicState.channel === "charts" ? "chart" : "playlist",
    });
    fillPublicCategories(highquality ? { categories: data.highquality_tags || data.tags || [] } : data);
  } catch (exc) {
    fillPublicCategories({ categories: [] });
    $("#public-note").textContent = "分类加载失败：" + exc.message;
  }
}

async function loadPublicList() {
  $("#public-note").textContent = "正在加载歌单…";
  try {
    const params = {
      source: publicState.source, channel: publicState.channel,
      category: publicState.category, sort: publicState.sort, page: publicState.page,
    };
    if (publicState.channel === "highquality") params.cursor = (publicState.cursors || {})[publicState.page] || "";
    const data = await publicApi("", params);
    publicState.items = (data.items || []).map((item) => ({
      ...item, provider: publicState.provider === "musicbox" ? "musicbox" : "lx", source: publicState.source,
      kind: publicState.channel === "charts" ? "chart" : "playlist",
    }));
    publicState.hasMore = !!data.has_more;
    publicState.total = Number(data.total || 0);
    publicState.cursor = data.next_cursor || publicState.cursor;
    if (publicState.channel === "highquality") {
      publicState.cursors = publicState.cursors || { 1: "" };
      publicState.cursors[publicState.page + 1] = data.next_cursor || "";
    }
    const suffix = publicState.total ? `，接口报告 ${publicState.total} 份` : "";
    $("#public-note").textContent = `返回 ${publicState.items.length} 份${suffix}；勾选后保存生效。`;
  } catch (exc) {
    publicState.items = []; publicState.hasMore = false;
    $("#public-note").textContent = "歌单加载失败：" + exc.message;
  }
  renderPublicList();
  renderPublicPage();
}

function renderPublicPage() {
  $("#public-page").textContent = String(publicState.page);
  $("#public-prev").disabled = publicState.page <= 1;
  $("#public-next").disabled = !publicState.hasMore;
}

async function initPublicDefaults() {
  const provider = currentUiProvider() === "lxmusic" ? "lx" : "musicbox";
  if (provider === "musicbox") {
    publicSelection = [
      { provider: "musicbox", source: "netease", kind: "chart", id: "3778678", _name: "热歌榜" },
      { provider: "musicbox", source: "netease", kind: "chart", id: "3779629", _name: "新歌榜" },
      { provider: "musicbox", source: "netease", kind: "chart", id: "19723756", _name: "飙升榜" },
      { provider: "musicbox", source: "netease", kind: "chart", id: "2884035", _name: "原创榜" },
    ];
    renderPublicSelected();
    markDirty("已填入默认网易榜单");
    return;
  }
  publicState.provider = "lxmusic";
  await loadPublicCapabilities();
  const caps = publicCapabilities || {};
  const wanted = [
    { source: "kg", label: "TOP500" }, { source: "kw", label: "飙升榜" }, { source: "wy", label: "新歌榜" },
  ];
  publicSelection = [];
  for (const target of wanted) {
    const cap = (caps.sources || []).find((item) => item.id === target.source && item.playback_available);
    if (!cap) continue;
    publicState.source = target.source; publicState.channel = "charts";
    try {
      const data = await publicApi("", { source: target.source, channel: "charts" });
      const row = (data.items || []).find((item) => String(item.name || "").includes(target.label));
      if (row) publicSelection.push({ ...row, provider: "lx", source: target.source, kind: "chart", id: String(row.id) });
    } catch (_) { /* 平台不可用则跳过 */ }
  }
  renderPublicSelected();
  markDirty("已按可用平台填入默认洛雪榜单");
}

async function showPublicBrowser() {
  $("#public-playlists-browser").hidden = false;
  const saved = savedProvider();
  publicState.provider = currentUiProvider() === "lxmusic" ? "lxmusic" : "musicbox";
  await loadPublicCapabilities();
  await loadPublicCategories();
  await loadPublicList();
  const browsingProvider = publicState.provider;
  for (const item of publicSelection) {
    const uiProvider = item.provider === "lx" ? "lxmusic" : "musicbox";
    if (uiProvider !== browsingProvider) continue;
    const cap = ((publicCapabilities || {}).sources || []).find((source) => source.id === item.source);
    item._reason = cap && !cap.playback_available ? cap.reason || "当前平台没有播放能力" : "";
    try {
      const response = await publicApi(encodeURIComponent(item.id), { source: item.source, kind: item.kind });
      item._name = response.data.name;
      item._cover_url = response.data.cover_url;
      item._track_count = response.data.track_count;
    } catch (exc) { item._reason = item._reason || exc.message; }
  }
  renderPublicSelected();
  if (saved === "musicdl") $("#public-note").textContent = "当前音源是 musicdl：它没有统一推荐歌单接口；可浏览网易/洛雪，保存后按常驻规则展示网易公共歌单。";
}

$("#public-playlists-enabled").addEventListener("change", async () => {
  const enabled = $("#public-playlists-enabled").checked;
  updateResidentAvailability();
  markDirty(enabled ? "公共歌单已开启" : "公共歌单已关闭");
  if (!enabled) { $("#public-playlists-browser").hidden = true; return; }
  if (!publicSelection.length && !configPresence.FNMUSIC_PUBLIC_PLAYLISTS_SELECTION) await initPublicDefaults();
  await showPublicBrowser();
});
$("#public-provider").addEventListener("change", async () => {
  publicState.provider = $("#public-provider").value;
  publicState.page = 1; publicState.channel = publicState.provider === "musicbox" ? "charts" : "top";
  markDirty(); await loadPublicCapabilities(); await loadPublicCategories(); await loadPublicList();
});
$("#public-channel").addEventListener("change", async () => {
  publicState.channel = $("#public-channel").value; publicState.page = 1;
  markDirty(); await loadPublicCategories(); await loadPublicList();
});
$("#public-source").addEventListener("change", async () => {
  publicState.source = $("#public-source").value; publicState.page = 1;
  markDirty(); await loadPublicCategories(); await loadPublicList();
});
$("#public-category").addEventListener("change", async () => {
  publicState.category = $("#public-category").value; publicState.page = 1;
  publicState.cursors = { 1: "" }; publicState.cursor = "";
  markDirty(); await loadPublicList();
});
$("#public-sort").addEventListener("change", async () => {
  publicState.sort = $("#public-sort").value; publicState.page = 1;
  markDirty(); await loadPublicList();
});
$("#public-refresh").addEventListener("click", async () => {
  await loadPublicCapabilities(); await loadPublicCategories(); await loadPublicList();
});
$("#public-prev").addEventListener("click", async () => {
  if (publicState.page > 1) { publicState.page--; await loadPublicList(); }
});
$("#public-next").addEventListener("click", async () => {
  if (publicState.hasMore) { publicState.page++; await loadPublicList(); }
});

/* -------------------------------------------------------------- 网易扫码 */
let neteaseLoggedIn = false;

function updateResidentAvailability() {
  // 账号歌单仍需登录；公共网易歌单可匿名常驻，因此仅在两者都无需求时置灰。
  const publicNeedsMusicbox = $("#public-playlists-enabled").checked
    && publicSelection.some((item) => item.provider === "musicbox");
  $("#netease-playlist-resident").disabled = !neteaseLoggedIn && !publicNeedsMusicbox;
}

async function syncNeteaseAccount(retries = 0) {
  // 把网易账号态同步到 #qr-check（已登录显示昵称；未登录/失败清空），
  // 同时驱动「网易云歌单常驻」复选框的可用态
  neteaseLoggedIn = false;
  updateResidentAvailability();
  for (let i = 0; ; i++) {
    try {
      const st = await api("/api/netease/auth/status");
      const d = st.data || st;
      if (d.logged_in) {
        neteaseLoggedIn = true;
        $("#qr-check").textContent = `当前登录：${d.nickname || d.user_id || "已登录用户"}`;
        break;
      }
    } catch (_) { /* status 不可达视为未登录 */ }
    if (i >= retries) break;
    await new Promise((r) => setTimeout(r, 1500));
  }
  updateResidentAvailability();
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

$("#netease-playlist-resident").addEventListener("change", () => {
  // 常驻为账号/公共歌单供数；仅在没有公共选择时自动带出账号歌单开关。
  const publicNeedsMusicbox = publicSelection.some((item) => item.provider === "musicbox");
  if ($("#netease-playlist-resident").checked && !$("#netease-my-playlists").checked && !publicNeedsMusicbox) {
    $("#netease-my-playlists").checked = true;
  }
});

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
  let explicitActives = false;
  try {
    const items = JSON.parse(configValues.LX_SOURCE_LIST || "[]");
    if (Array.isArray(items)) items.forEach((i) => {
      if (i && "active" in i) explicitActives = true;
      if (i && i.active && i.url) saved.add(i.url);
    });
  } catch (_) { /* 忽略 */ }
  if (!saved.size && !explicitActives) {
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

/* -------------------------------------------------- 储存目录：选择/授权/校验 */
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
  const schedule = () => {
    markDirty();
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
["#tee-max", "#llm-base", "#llm-key", "#llm-model", "#liked-refresh-hours", "#liked-size", "#search-timeout", "#search-deep-max", "#bind-timeout", "#handoff-max", "#scan-path"].forEach((sel) =>
  $(sel).addEventListener("input", () => markDirty()));
["#llm-liked", "#llm-daily"].forEach((sel) => $(sel).addEventListener("change", () => {
  updateLlmLikedSettings();
  markDirty();
}));
["#llm-base", "#llm-key"].forEach((sel) => $(sel).addEventListener("input", updateLlmLikedSettings));
$("#lx-url").addEventListener("input", () => {
  // #lx-url 仅为「添加」入口输入框，激活态由列表 active 标记决定，与它无关
  syncLxAddButton();
});
if ($("#lx-name")) {
  $("#lx-name").addEventListener("input", () => markDirty());
}
$$("input[name=quality]").forEach((el) => el.addEventListener("change", () => markDirty("音质偏好需保存后生效")));
$$("input[name=dl-quality]").forEach((el) => el.addEventListener("change", () => markDirty("下载音质需保存后生效")));
["#recommend-hot", "#recommend-daily", "#search-probe", "#search-deep", "#tee-enabled", "#fav-autobind", "#auto-cover", "#lyric-auto-dl", "#netease-my-playlists", "#netease-playlist-resident"].forEach((sel) =>
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

/* -------------------------------------------------------------- 关于页 */
function fmtBytes(n) {
  if (!Number.isFinite(n)) return "-";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

async function loadAbout() {
  const info = await api("/api/about");
  $("#about-version").textContent = `v${info.version}`;
  $("#about-deploy").textContent = info.deploy_mode === "native" ? "原生部署" : "Docker 部署";
  // 免责声明来自 README「免责与版权声明」小节；解析失败时后端返回空数组，隐藏卡片
  const card = $("#about-disclaimer-card");
  const list = $("#about-disclaimer");
  const items = Array.isArray(info.disclaimer) ? info.disclaimer : [];
  if (items.length) {
    card.hidden = false;
    list.innerHTML = items.map((d) =>
      `<li><b>${escapeHtml(d.title)}</b>${d.text ? "：" + escapeHtml(d.text) : ""}</li>`).join("");
  } else {
    card.hidden = true;
  }
  const logs = $("#about-logs");
  const files = Array.isArray(info.logs) ? info.logs : [];
  logs.innerHTML = files.length
    ? files.map((f) => `<span class="state-line"><span class="dot ok"></span>${escapeHtml(f.name)} · ${fmtBytes(f.size)}</span>`).join("")
    : '<span class="muted">暂无日志文件</span>';
  // 打赏二维码是打包时的可选资源（CI 从 ALIPAY_QRCODE 生成）；开源发布包里没有则不展示
  const donate = $("#about-donate");
  if (info.donate) {
    donate.hidden = false;
    $("#about-donate-img").src = `${APP_BASE}/static/alipay.png`;
  } else {
    donate.hidden = true;
  }
  // 进入关于页顺带刷新检查更新（后端有 TTL 缓存，命中不出网）
  checkUpdate();
}

async function exportLogs() {
  const btn = $("#log-export-btn");
  const note = $("#log-export-note");
  if (btn.disabled) return;
  btn.disabled = true;
  note.textContent = "打包中…";
  try {
    const resp = await fetch(APP_BASE + "/api/logs/export");
    if (!resp.ok) {
      let msg = `HTTP ${resp.status}`;
      try { msg = (await resp.json()).error || msg; } catch (_) { /* 非 JSON */ }
      throw new Error(msg);
    }
    const blob = await resp.blob();
    const m = (resp.headers.get("content-disposition") || "").match(/filename="?([^";]+)"?/);
    const name = m ? m[1] : `fnmusic-logs-${Date.now()}.logzip`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    note.textContent = `已导出 ${name}`;
  } catch (exc) {
    note.textContent = "";
    toast("日志导出失败：" + exc.message, "fail");
  } finally {
    btn.disabled = false;
  }
}
$("#log-export-btn").addEventListener("click", () => { exportLogs().catch(() => {}); });

/* -------------------------------------------------------------- 检查更新 */
async function checkUpdate() {
  try {
    updateInfo = await api("/api/update-check");
    renderUpdateBadge();
  } catch (_) { /* 检查失败静默：红点只是提示，不打扰用户 */ }
}

// 有新版本则红点常亮（两处"关于"入口 + 关于页 NEW 标记），不因查看而清除
function renderUpdateBadge() {
  const has = !!(updateInfo && updateInfo.has_update);
  $$(".nav-dot").forEach((el) => { el.hidden = !has; });
  $("#about-new").hidden = !has;
}

function fmtDate(iso) {
  const m = /^(\d{4}-\d{2}-\d{2})/.exec(String(iso || ""));
  return m ? m[1] : "-";
}

function openUpdateModal() {
  if (!updateInfo || !updateInfo.has_update) return;
  const latest = updateInfo.latest || {};
  $("#update-modal-version").textContent = `v${latest.version || updateInfo.current_version}`;
  $("#update-modal-date").textContent = fmtDate(latest.published_at);
  // release 说明来自外部仓库，textContent 注入防 XSS
  $("#update-modal-changelog").textContent = latest.changelog || "暂无更新说明";
  // 升级跳转跟随检查源：GitHub 查到跳 GitHub release 页，Gitee 查到跳 Gitee 发行版页
  $("#update-upgrade-btn").href = latest.html_url || "#";
  $("#update-modal").hidden = false;
}

function closeUpdateModal() {
  $("#update-modal").hidden = true;
}

$("#about-new").addEventListener("click", openUpdateModal);
$("#update-cancel-btn").addEventListener("click", closeUpdateModal);
$("#update-modal").addEventListener("click", (ev) => {
  if (ev.target === ev.currentTarget) closeUpdateModal();  // 点遮罩空白处关闭
});

/* -------------------------------------------------------------- 垃圾清理 Beta */
let cleanupJob = null;
let cleanupSubmitting = false;
let cleanupPolling = false;
let cleanupTimer = null;

function renderCleanupJob(job) {
  const labels = { running: "清理中", completed: "清理完成", failed: "清理失败" };
  $("#cleanup-status").textContent =
    `任务 ${job.id} · ${labels[job.status]} · 阶段：${job.phase || "-"}` +
    ` · 已扫描 ${job.scanned ?? 0} · 已删除 ${job.removed ?? 0}` +
    ` · 释放 ${job.bytes ?? 0} 字节 · 已跳过 ${job.skipped ?? 0}`;
  const lines = [];
  for (const [name, counts] of Object.entries(job.categories || {})) {
    lines.push(`${name}：扫描 ${counts.scanned || 0}，删除 ${counts.removed || 0}，释放 ${counts.bytes || 0} 字节，跳过 ${counts.skipped || 0}`);
  }
  for (const [key, label] of [["errors", "错误"], ["details", "详情"]]) {
    for (const item of (Array.isArray(job[key]) ? job[key] : [])) {
      lines.push(`${label}：${typeof item === "string" ? item : JSON.stringify(item)}`);
    }
  }
  // 文件名、错误及详情均不可信，绝不作为 HTML 插入。
  $("#cleanup-details").textContent = lines.join("\n");
  $("#cleanup-details").hidden = !lines.length;
  $("#cleanup-open-btn").textContent = job.status === "running" ? "查看当前清理任务" : "清理垃圾文件（Beta）";
}

function acceptCleanupJob(response) {
  const job = response && response.job;
  if (!response || response.ok !== true || !job || !job.id ||
      !["running", "completed", "failed"].includes(job.status)) {
    throw new Error("清理任务响应格式无效");
  }
  cleanupJob = job;
  renderCleanupJob(job);
  clearTimeout(cleanupTimer);
  cleanupTimer = null;
  if (job.status === "running") cleanupTimer = setTimeout(pollCleanupJob, 1000);
}

async function pollCleanupJob() {
  if (cleanupPolling || !cleanupJob || cleanupJob.status !== "running") return;
  clearTimeout(cleanupTimer);
  cleanupTimer = null;
  cleanupPolling = true;
  try {
    acceptCleanupJob(await api("/api/maintenance/cleanup/" + encodeURIComponent(cleanupJob.id)));
  } catch (exc) {
    $("#cleanup-status").textContent = `任务 ${cleanupJob.id} · 查询失败：${exc.message}；点击查看当前清理任务重试，不会重复提交。`;
  } finally {
    cleanupPolling = false;
  }
}

function closeCleanupModal() {
  $("#cleanup-modal").hidden = true;
  $("#cleanup-open-btn").focus();
}

function openCleanupModal() {
  if (cleanupSubmitting) return;
  if (cleanupJob && cleanupJob.status === "running") {
    renderCleanupJob(cleanupJob);
    pollCleanupJob();
    return;
  }
  $("#cleanup-modal").hidden = false;
  $("#cleanup-cancel-btn").focus();
}

async function startCleanup() {
  // 只有可见警告框中的确认才可提交；双击和在途提交均不创建新任务。
  if ($("#cleanup-modal").hidden || cleanupSubmitting) return;
  if (cleanupJob && cleanupJob.status === "running") {
    closeCleanupModal();
    renderCleanupJob(cleanupJob);
    return;
  }
  cleanupSubmitting = true;
  $("#cleanup-open-btn").disabled = true;
  closeCleanupModal();
  $("#cleanup-status").textContent = "正在提交清理任务…";
  $("#cleanup-details").textContent = "";
  $("#cleanup-details").hidden = true;
  try {
    acceptCleanupJob(await api("/api/maintenance/cleanup", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm_beta: true }),
    }));
  } catch (exc) {
    $("#cleanup-status").textContent = "清理请求失败：" + exc.message;
  } finally {
    cleanupSubmitting = false;
    $("#cleanup-open-btn").disabled = false;
  }
}

$("#cleanup-open-btn").addEventListener("click", openCleanupModal);
$("#cleanup-cancel-btn").addEventListener("click", closeCleanupModal);
$("#cleanup-confirm-btn").addEventListener("click", startCleanup);
$("#cleanup-modal").addEventListener("click", (ev) => {
  if (ev.target === ev.currentTarget) closeCleanupModal();
});
window.addEventListener("keydown", (ev) => {
  if ($("#cleanup-modal").hidden) return;
  if (ev.key === "Escape") {
    ev.preventDefault();
    closeCleanupModal();
  } else if (ev.key === "Tab") {
    // 对话框仅有两个按钮，将键盘焦点限制在其中，默认落在取消。
    ev.preventDefault();
    const cancel = $("#cleanup-cancel-btn");
    (document.activeElement === cancel ? $("#cleanup-confirm-btn") : cancel).focus();
  }
});

/* -------------------------------------------------------------- 启动 */
(async function boot() {
  await loadConfig();
  await loadStatus();
  checkUpdate();  // 进页面即查（后端 TTL 缓存兜住 GitHub 限额），不阻塞启动
  await loadPlatforms(false);  // 页面加载不自动拉起预览进程，等用户点选音源
  if ($("#public-playlists-enabled").checked) await showPublicBrowser();
  setInterval(loadStatus, 15000);
})();
