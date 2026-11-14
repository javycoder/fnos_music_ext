/* fnmusic-ext WebUI — 原生 JS，无框架无外部资产。 */
"use strict";

// 飞牛桌面用 HTTPS 打开管理窗，页面必须挂在同源路径 /app/fnmusic-ext 下。
// WebUI 自己也会剥掉这个前缀。管理接口只认飞牛网关注入的管理员头。
const APP_BASE = "/app/fnmusic-ext";

// 直连模式自动跳转：port 8774 直连时维护功能（整理/清理/用户列表）不可用，
// 自动跳转到飞牛桌面网关以获得完整功能。
(function redirectToDesktopIfNeeded() {
  try {
    if (window.location.port === "8774") {
      const desktopUrl = window.location.protocol + "//" + window.location.hostname + ":5666/app/fnmusic-ext/";
      window.location.replace(desktopUrl);
    }
  } catch (_) { /* ignore */ }
})();

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const PROVIDER_LABEL = { musicdl: "musicdl 聚合音源", netease: "网易云音乐", musicbox: "网易云音乐", lxmusic: "洛雪自定义源", none: "未配置" };
const PROC_LABEL = { musicdl: "musicdl", netease: "netease", musicbox: "musicbox", lxserver: "lxserver", lxmusic: "lxmusic", webui: "WebUI" };

let configValues = {};   // GET /api/config 的 values
let configPresence = {}; // .env 显式存在的键（区分默认空与管理员清空）
let configSchema = {};   // 旧后端缺新键时禁用对应控件且不提交未知键
let currentConfigRevision = "";
let userStorageStatus = null;
let userStorageReady = false;
let userStorageError = "";
let savedUserDirs = {};
let savedUserScans = {};
let scanAdminPasswordConfigured = false;
let scanAdminPasswordDirty = false;
let scanAdminPasswordClear = false;
let platforms = { enabled: [], registered: [] };
let dirty = false;
let currentPage = "overview";
let lxSourceListDirty = false; // 避免高级策略保存时重复触发原源列表的激活/校验流程
const DOWNLOAD_LAYOUT_LABELS = {
  flat: "歌曲",
  artist: "歌手/歌曲",
  album: "专辑/歌曲",
  artist_album: "歌手/专辑/歌曲",
};
let qrTimer = null;
let lxVerifiedUrl = null; // 已通过测试的 lx URL（保存时免二次校验提示用）
let lxSourceList = [];    // 本地待保存配置；运行状态轮询不可覆盖 active
let lxSourceOrder = [];   // 洛雪稳定源 ID 顺序；停用时保留历史位置
let lxRuntimeSnapshot = null; // /api/status 的只读运行快照
// CPU 异常按稳定源 ID 缓存：刷新失败时保留已知红标，只把当前读数降为未知。
let lxCpuAlerts = new Map();
let lxRuntimeStatusUnknown = false;
let lxCpuHelpSourceId = "";
const LX_STATE_LABEL = { unconfigured: "未配置", loading: "初始化中", ready: "可用", degraded: "部分可用", failed: "全部失败", unavailable: "服务不可用" };
const LX_SOURCE_STATUS_LABEL = { success: "运行就绪", failed: "运行失败", loading: "初始化中", unknown: "运行状态未知" };
let updateInfo = null;    // /api/update-check 结果（菜单红点 / NEW 标记 / 详情弹窗共用）
let publicSelection = [];
let publicUsers = [];
let publicUsersReady = false;
let publicUsersSeq = 0;
let publicUserGuid = "";
let publicSelectionMap = {};
const publicUserChanges = new Set();
let publicSyncAll = false;
let publicSharedSelection = [];
let publicSharedChanged = false;
let publicCapabilities = null;
let publicState = {
  provider: "musicbox", channel: "charts", source: "netease", category: "", sort: "",
  keyword: "", page: 1, items: [], hasMore: false, total: 0, cursor: "",
  cursors: { 1: "" }, seq: 0, loading: false, bulk: null,
};
// 已选歌单的展示元数据缓存（key=publicEntryKey）：保存后重新回填、跨 provider
// 补齐时复用，避免每次渲染都打详情接口
const publicDetailCache = new Map();
let publicSelectionDetailSeq = 0;
const PUBLIC_METADATA_STORAGE_KEY = "fnmusic_public_metadata_cache_v1";
const PUBLIC_METADATA_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const PUBLIC_FAILED_TTL_MS = 30 * 1000;
const MAX_PUBLIC_DETAIL_CONCURRENCY = 2;
const publicDetailAbortControllers = new Set();
const publicDetailInflight = new Map();
let publicDetailRun = null;
let publicDetailRunTarget = "";
let publicProcessSnapshot = null;

async function api(path, options, timeoutMs = 0) {
  const controller = timeoutMs > 0 ? new AbortController() : null;
  let timer;
  const request = async () => {
    const resp = await fetch(APP_BASE + path, controller ? { ...options, signal: controller.signal } : options);
    let body = {};
    try { body = await resp.json(); } catch (_) { /* 非 JSON */ }
    if (!resp.ok) {
      const err = new Error(body.error || body.detail || `HTTP ${resp.status}`);
      err.status = resp.status;
      err.body = body;
      throw err;
    }
    return body;
  };
  try {
    if (!controller) return await request();
    return await Promise.race([request(), new Promise((_, reject) => {
      timer = setTimeout(() => {
        const error = new Error("请求超时，结果待核实，请刷新运行状态");
        error.name = "TimeoutError";
        reject(error);
        controller.abort();
      }, timeoutMs);
    })]);
  } finally {
    clearTimeout(timer);
  }
}

function updateLlmLikedSettings() {
  $("#llm-liked-settings").hidden = !$("#llm-liked").checked;
  const selected = $("#llm-liked").checked || $("#llm-daily").checked;
  $("#llm-api-warning").hidden = !selected || !!($("#llm-base").value.trim() && $("#llm-key").value.trim());
}

let llmUsageOffset = 0;
let llmUsageTimer = null;
let llmUsageActive = false;
let llmUsageSequence = 0;
let llmUsageLast = null;
const LLM_USAGE_PAGE_SIZE = 30;
const usageNumber = (value) => Math.max(0, Number(value) || 0).toLocaleString();
const usageDate = (value) => /^\d{8}$/.test(String(value || ""))
  ? `${String(value).slice(0, 4)}-${String(value).slice(4, 6)}-${String(value).slice(6, 8)}` : "未知";

function renderLlmUsage(u) {
  const el = $("#llm-usage-line");
  if (!el) return;
  const available = !u.status || u.status === "ok";
  let text = available ? `今日 token：输入 ${usageNumber(u.prompt)} / 输出 ${usageNumber(u.completion)} / 总计 ${usageNumber(u.total)}`
    : `账本${u.status === "missing" ? "尚无记录" : "不可用"}：${u.statusMessage || u.status}（不能视为已用 0）`;
  text += Number(u.dailyLimit) > 0 ? `；每日上限 ${usageNumber(u.dailyLimit)}` : "；每日不限额";
  if (available) text += `；实际请求 ${usageNumber(u.requests)} 次`;
  if (u.withinLimit === false) text += "；已达上限，次日重置";
  if (Number(u.unknownUsageRequests)) text += `；${usageNumber(u.unknownUsageRequests)} 次未返回用量，未计入 token`;
  if (u.lastFailure?.reason) text += `；最近失败：${u.lastFailure.reason}${u.lastFailure.detail ? `（${String(u.lastFailure.detail).slice(0, 120)}）` : ""}`;
  text += `；北京时间 UTC+08:00；更新于 ${new Date().toLocaleTimeString()}`;
  el.textContent = text;
  el.dataset.lastUsageText = text;
  const summary = $("#llm-usage-summary");
  if (summary) summary.textContent = !available ? "累计与历史数据暂不可读取，不能视为零用量；请检查账本状态后重试。" : `累计 token ${usageNumber(u.allTime?.total)}（输入 ${usageNumber(u.allTime?.prompt)} / 输出 ${usageNumber(u.allTime?.completion)}），实际请求 ${usageNumber(u.allTime?.requests)} 次。统计起点 ${usageDate(u.statsStartedAt)}。${u.historyIncomplete ? "历史不完整：旧版已丢失的记录无法恢复。" : "仅统计本扩展记录到的调用。"}`;
  const history = $("#llm-usage-history");
  if (history) history.innerHTML = available && (u.history || []).length
    ? `<table class="usage-table"><thead><tr><th>日期（北京时间）</th><th>输入</th><th>输出</th><th>总 token</th><th>请求</th></tr></thead><tbody>${u.history.map((day) => `<tr><td>${usageDate(day.date)}</td><td>${usageNumber(day.prompt)}</td><td>${usageNumber(day.completion)}</td><td>${usageNumber(day.total)}</td><td>${usageNumber(day.requests)}${Number(day.unknownUsageRequests) ? `（${usageNumber(day.unknownUsageRequests)} 次用量未知）` : ""}</td></tr>`).join("")}</tbody></table>`
    : `<p class="hint">${available ? "暂无历史记录。" : "历史记录暂不可读取。"}</p>`;
  if ($("#llm-usage-prev")) $("#llm-usage-prev").disabled = llmUsageOffset === 0;
  if ($("#llm-usage-next")) $("#llm-usage-next").disabled = llmUsageOffset + LLM_USAGE_PAGE_SIZE >= Number(u.totalDays || 0);
  if ($("#llm-usage-page")) $("#llm-usage-page").textContent = `第 ${Math.floor(llmUsageOffset / LLM_USAGE_PAGE_SIZE) + 1} 页，共 ${usageNumber(u.totalDays)} 个记录日`;
}

async function refreshLlmUsage() {
  const el = $("#llm-usage-line");
  if (!el) return;
  const seq = ++llmUsageSequence;
  try {
    const { usage: u } = await api(`/api/llm/usage?limit=${LLM_USAGE_PAGE_SIZE}&offset=${llmUsageOffset}`);
    if (seq !== llmUsageSequence) return;
    if (!u || typeof u !== "object") throw new Error("账本响应无效");
    llmUsageLast = u;
    renderLlmUsage(u);
  } catch (_) {
    if (seq !== llmUsageSequence) return;
    if (llmUsageLast) {
      const previousText = el.dataset.lastUsageText || "";
      el.textContent = `刷新失败，显示上次成功结果（可能已过期）：${previousText}`;
    } else el.textContent = "Token 用量暂不可用，请重试（不能视为已用 0）。";
  }
}

function setLlmUsageActive(active) {
  llmUsageActive = active;
  clearInterval(llmUsageTimer);
  llmUsageTimer = null;
  if (active && !document.hidden) {
    void refreshLlmUsage();
    llmUsageTimer = setInterval(refreshLlmUsage, 30000);
  }
}
if (typeof document.addEventListener === "function") document.addEventListener("visibilitychange", () => setLlmUsageActive(llmUsageActive));
$("#llm-usage-refresh")?.addEventListener("click", () => refreshLlmUsage());
$("#llm-usage-prev")?.addEventListener("click", () => { llmUsageOffset = Math.max(0, llmUsageOffset - LLM_USAGE_PAGE_SIZE); void refreshLlmUsage(); });
$("#llm-usage-next")?.addEventListener("click", () => { if (llmUsageOffset + LLM_USAGE_PAGE_SIZE < Number(llmUsageLast?.totalDays || 0)) { llmUsageOffset += LLM_USAGE_PAGE_SIZE; void refreshLlmUsage(); } });

function likedNumber(selector, label, min, max, integer = false) {
  const text = $(selector).value.trim();
  const num = Number(text);
  if (!text || !Number.isFinite(num) || num < min || (max != null && num > max) || (integer && !Number.isInteger(num))) {
    throw new Error(label + (integer ? "必须是 0–100 的整数" : "必须是大于或等于 0.5 的有限数字"));
  }
  return num;
}

function lxTimeoutNumber(selector, label) {
  const value = Number.parseFloat($(selector).value);
  if (!Number.isFinite(value) || value < 1 || value > 300) {
    throw new Error(`${label}必须是 1–300 秒的数字`);
  }
  return value;
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

/* -------------------------------------------------------------- 帮助弹窗 */
let helpReturnFocus = null;
let helpPreviousOverflow = "";
let helpBackgroundState = [];

function helpFocusables() {
  return $$("#help-modal button, #help-modal [href], #help-modal input, #help-modal select, #help-modal textarea")
    .filter((el) => !el.disabled);
}

function openHelp(helpId, trigger = null) {
  const template = document.querySelector(`template[data-help-id="${helpId}"]`);
  if (!template) return;
  if (!$("#help-modal").hidden) return;
  const title = (trigger && trigger.getAttribute && trigger.getAttribute("aria-label")) || "帮助";
  helpReturnFocus = trigger || document.activeElement;
  $("#help-title").textContent = title.replace(/帮助$/, "");
  $("#help-body").replaceChildren(template.content.cloneNode(true));
  $("#help-modal").hidden = false;
  helpPreviousOverflow = document.body.style.overflow || "";
  helpBackgroundState = $$(".layout, #save-bar").map((element) => [element, element.inert]);
  helpBackgroundState.forEach(([element]) => { element.inert = true; });
  document.body.style.overflow = "hidden";
  $("#help-close-btn").focus();
}

function closeHelp() {
  if ($("#help-modal").hidden) return;
  $("#help-modal").hidden = true;
  $("#help-body").replaceChildren();
  document.body.style.overflow = helpPreviousOverflow;
  helpBackgroundState.forEach(([element, inert]) => { element.inert = inert; });
  helpBackgroundState = [];
  if (helpReturnFocus && typeof helpReturnFocus.focus === "function") helpReturnFocus.focus();
  helpReturnFocus = null;
  lxCpuHelpSourceId = "";
}

$$("[data-help]").forEach((btn) => btn.addEventListener("click", () => openHelp(btn.dataset.help, btn)));
$("#help-close-btn").addEventListener("click", closeHelp);
$("#help-modal").addEventListener("click", (ev) => {
  if (ev.target === ev.currentTarget) closeHelp();
});
window.addEventListener("keydown", (ev) => {
  if ($("#help-modal").hidden || !$("#cleanup-modal").hidden) return;
  if (ev.key === "Escape") {
    ev.preventDefault();
    closeHelp();
  } else if (ev.key === "Tab") {
    const items = helpFocusables();
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (ev.shiftKey && document.activeElement === first) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && document.activeElement === last) {
      ev.preventDefault();
      first.focus();
    }
  }
});

/* -------------------------------------------------------------- 导航 */
let activeBottomMenu = null;
let activeBottomTrigger = null;

function isNavGroupExpanded(group) {
  try {
    return typeof window !== "undefined" && window.localStorage
      ? window.localStorage.getItem("fnmusic-nav-" + group + "-expanded") === "true"
      : false;
  } catch (_) { return false; }
}

function setNavGroupExpanded(group, expanded, save = true) {
  const titleBtn = $("#nav-" + group + "-title");
  const subsEl = $("#nav-" + group + "-subs");
  if (titleBtn && typeof titleBtn.setAttribute === "function") {
    titleBtn.setAttribute("aria-expanded", String(expanded));
  }
  if (subsEl) subsEl.hidden = !expanded;
  if (save) {
    try {
      if (typeof window !== "undefined" && window.localStorage) {
        window.localStorage.setItem("fnmusic-nav-" + group + "-expanded", String(expanded));
      }
    } catch (_) {}
  }
}

function closeBottomSubMenu(restoreFocus = true) {
  if (!activeBottomMenu) return;
  activeBottomMenu.hidden = true;
  if (activeBottomTrigger) {
    if (typeof activeBottomTrigger.setAttribute === "function") {
      activeBottomTrigger.setAttribute("aria-expanded", "false");
    }
    if (restoreFocus && typeof activeBottomTrigger.focus === "function") {
      activeBottomTrigger.focus();
    }
  }
  activeBottomMenu = null;
  activeBottomTrigger = null;
}

function openBottomSubMenu(trigger, menu) {
  if (activeBottomMenu === menu) {
    closeBottomSubMenu(false);
    return;
  }
  closeBottomSubMenu(false);
  activeBottomMenu = menu;
  activeBottomTrigger = trigger;
  menu.hidden = false;
  if (typeof trigger.setAttribute === "function") {
    trigger.setAttribute("aria-expanded", "true");
  }
  const firstItem = menu.querySelector ? menu.querySelector(".bottom-sub-item") : null;
  if (firstItem && typeof firstItem.focus === "function") firstItem.focus();
}

function switchPage(page) {
  currentPage = page;
  $$(".page").forEach((el) => el.classList.toggle("active", el.id === "page-" + page));
  $$("[data-page]").forEach((el) => el.classList.toggle("active", el.dataset.page === page));

  const isStorageSub = page === "tee" || page === "dirs";
  const isAdvancedSub = page === "llm" || page === "lx" || page === "search";

  const storageTitle = $("#nav-storage-title");
  if (storageTitle) storageTitle.classList.toggle("active", isStorageSub);
  const advancedTitle = $("#nav-advanced-title");
  if (advancedTitle) advancedTitle.classList.toggle("active", isAdvancedSub);

  const bottomStorage = $("#bottom-storage-trigger");
  if (bottomStorage) bottomStorage.classList.toggle("active", isStorageSub);
  const bottomAdvanced = $("#bottom-advanced-trigger");
  if (bottomAdvanced) bottomAdvanced.classList.toggle("active", isAdvancedSub);

  if (isStorageSub) setNavGroupExpanded("storage", true, true);
  if (isAdvancedSub) setNavGroupExpanded("advanced", true, true);

  closeBottomSubMenu(false);

  if (typeof setLlmUsageActive === "function") {
    setLlmUsageActive(page === "llm");
  }

  if (page === "about") loadAbout().catch((exc) => { $("#about-logs").textContent = "加载失败：" + exc.message; });
}

$$("[data-page]").forEach((btn) => btn.addEventListener("click", () => switchPage(btn.dataset.page)));

// 一级菜单纯折叠：不跳页、不标脏、首访收起、记忆到 localStorage
["storage", "advanced"].forEach((group) => {
  const expanded = isNavGroupExpanded(group);
  setNavGroupExpanded(group, expanded, false);
  const titleBtn = $("#nav-" + group + "-title");
  if (titleBtn) {
    titleBtn.addEventListener("click", (ev) => {
      if (ev && typeof ev.preventDefault === "function") ev.preventDefault();
      const cur = titleBtn.getAttribute ? titleBtn.getAttribute("aria-expanded") === "true" : false;
      setNavGroupExpanded(group, !cur, true);
    });
  }
});

// 移动端底导航父项弹子菜单：外点、Escape、focus 返回
[
  ["#bottom-storage-trigger", "#bottom-storage-menu"],
  ["#bottom-advanced-trigger", "#bottom-advanced-menu"],
].forEach(([triggerSel, menuSel]) => {
  const trigger = $(triggerSel);
  const menu = $(menuSel);
  if (!trigger || !menu) return;
  trigger.addEventListener("click", (ev) => {
    if (ev && typeof ev.stopPropagation === "function") ev.stopPropagation();
    openBottomSubMenu(trigger, menu);
  });
});

window.addEventListener("click", (ev) => {
  if (!activeBottomMenu || activeBottomMenu.hidden) return;
  const target = ev.target;
  const inMenu = activeBottomMenu.contains ? activeBottomMenu.contains(target) : false;
  const inTrigger = activeBottomTrigger && activeBottomTrigger.contains ? activeBottomTrigger.contains(target) : false;
  if (!inMenu && !inTrigger) {
    closeBottomSubMenu(true);
  }
});

window.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && activeBottomMenu && !activeBottomMenu.hidden) {
    if (typeof ev.preventDefault === "function") ev.preventDefault();
    closeBottomSubMenu(true);
  }
});

/* -------------------------------------------------------------- 主题 / 外观
   默认「跟随系统」：飞牛 Web 宿主里以宿主主题为准（getPlatformConfig 取初值，
   $on('os/theme') 监听后续切换，见 fnOS「页面交互」文档）；宿主不可用或独立浏览器
   退回 prefers-color-scheme。用户选择只存当前浏览器，不进 .env，也不参与保存条。 */
const THEME_KEY = "fnmusic-theme";
const HOST_THEME_KEY = "fnmusic-host-theme";
const themeMedia = (typeof window !== "undefined" && typeof window.matchMedia === "function")
  ? window.matchMedia("(prefers-color-scheme: dark)")
  : null;
let themePref = "auto";  // auto | light | dark —— 「设置 → 外观」里的选择
let hostTheme = null;    // 飞牛宿主报告的主题：light | dark（无宿主时为 null）

function normalizeTheme(value) {
  return value === "dark" || value === "light" ? value : null;
}

function readThemePref() {
  try {
    const saved = window.localStorage.getItem(THEME_KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch (_) { /* 隐私模式/直连没有 localStorage：按跟随系统处理 */ }
  return "auto";
}

function updateHostTheme(value) {
  hostTheme = normalizeTheme(value);
  try {
    if (hostTheme) window.localStorage.setItem(HOST_THEME_KEY, hostTheme);
    else window.localStorage.removeItem(HOST_THEME_KEY);
  } catch (_) { /* 宿主主题仍可在本次页面生效 */ }
}

function effectiveTheme() {
  if (themePref !== "auto") return themePref;
  if (hostTheme) return hostTheme;
  return themeMedia && themeMedia.matches ? "dark" : "light";
}

function applyTheme() {
  const theme = effectiveTheme();
  // 与飞牛文档示例一致：主题写到 <html data-theme>，CSS 按此覆盖配色变量
  if (typeof document !== "undefined" && document.documentElement && document.documentElement.dataset) {
    document.documentElement.dataset.theme = theme;
  }
  $$("input[name=theme]").forEach((el) => { el.checked = el.value === themePref; });
}

function setThemePref(pref) {
  themePref = normalizeTheme(pref) || "auto";
  try {
    if (themePref === "auto") window.localStorage.removeItem(THEME_KEY);
    else window.localStorage.setItem(THEME_KEY, themePref);
  } catch (_) { /* 存不了就只对本次会话生效 */ }
  applyTheme();
}

async function initTheme() {
  themePref = readThemePref();
  const pathname = (typeof window !== "undefined" && window.location && window.location.pathname) || "";
  const embedded = pathname.startsWith("/app/");
  hostTheme = null;
  if (embedded) {
    try { hostTheme = normalizeTheme(window.localStorage.getItem(HOST_THEME_KEY)); } catch (_) {}
  }
  applyTheme();
  $$("input[name=theme]").forEach((el) => el.addEventListener("change", () => {
    if (el.checked) setThemePref(el.value);
  }));
  // 独立浏览器 / 设备级深浅色变化（宿主主题优先，变化由下面的 $on 刷新）
  if (themeMedia && typeof themeMedia.addEventListener === "function") {
    themeMedia.addEventListener("change", () => { if (themePref === "auto" && !hostTheme) applyTheme(); });
  }
  if (!embedded) return;  // 直连 8774：没有飞牛宿主，不碰 SDK
  const sdk = await loadTrimSdk();
  if (!sdk) {
    updateHostTheme(null);
    applyTheme();
    return;
  }
  try {
    // 移动端 App 内嵌页也能取到主题；独立浏览器没有宿主桥，抛错后走媒体查询
    const config = await sdk.getPlatformConfig();
    updateHostTheme(config && (config.theme || (config.os && config.os.theme)));
  } catch (_) {
    updateHostTheme(null);
  }
  applyTheme();
  if (!sdk.isWeb || sdk.isStandaloneWeb) return;  // 只有 Web 宿主支持 $on 主题事件
  try {
    if (typeof sdk.$on === "function") {
      // 仅 Web 宿主支持；移动端 App 内嵌页与独立浏览器没有该事件
      await sdk.$on("os/theme", (theme) => {
        updateHostTheme(theme);
        applyTheme();
      });
    }
  } catch (_) { /* 宿主不支持事件推送时退回媒体查询 */ }
}

function formatVersionLabel(version) {
  return `v${String(version || "-").replace(/^v/i, "")}`;
}

/* -------------------------------------------------------------- 概览 */
async function loadStatus(timeoutMs = 0) {
  try {
    const st = await api("/api/status", undefined, timeoutMs);
    publicProcessSnapshot = { at: Date.now(), processes: st.processes || {} };
    for (const item of publicSelection) {
      if (item._detail_status === "stopped"
          && publicProcessSnapshot.processes[publicUiProviderOf(item)]?.state === "RUNNING") {
        item._detail_status = "idle";
        item._detail_failed = false;
      }
    }
    void ensurePublicSelectionDetails();
    $("#brand-version").textContent = formatVersionLabel(st.version);
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
    lxRuntimeSnapshot = st.lx_source || null;
    lxRuntimeStatusUnknown = false;
    syncLxCpuAlerts((lxRuntimeSnapshot || {}).sources);
    syncLxSourceOrder();
    renderLxSourceList(); // 只同步运行徽标，不重载配置或丢弃尚未保存的编辑。
    renderLxSourceOrder();
    const lxCard = $("#ov-lx-card");
    if (st.current_provider === "lxmusic" && st.lx_source) {
      lxCard.hidden = false;
      const ls = st.lx_source;
      const rows = [];
      const sources = ls.sources || [];
      const ready = sources.filter((s) => s.status === "success").length;
      const failed = sources.filter((s) => s.status === "failed").length;
      const state = ls.state || (ls.error ? "unavailable" : sources.length ? "loading" : "unconfigured");
      rows.push(`<div class="kv"><b>状态</b>${escapeHtml(LX_STATE_LABEL[state] || state)}</div>`);
      rows.push(`<div class="kv"><b>源统计</b>就绪 ${escapeHtml(ls.ready_count ?? ready)} · 失败 ${escapeHtml(ls.failed_count ?? failed)} · 待定 ${escapeHtml(ls.pending_count ?? (sources.length - ready - failed))}</div>`);
      if (sources.length) {
        rows.push(`<div class="kv"><b>已配置源</b>${sources.length} 个</div>`);
        sources.forEach((s) => {
          const status = s.status || "unknown";
          rows.push(`<div class="kv"><b>${escapeHtml(s.name || s.id || "-")}${s.version ? " v" + escapeHtml(s.version) : ""}</b>${escapeHtml(LX_SOURCE_STATUS_LABEL[status] || status)}${s.error ? "：" + escapeHtml(s.error) : ""}</div>`);
        });
        const plats = new Set();
        sources.forEach((s) => (s.platforms || []).forEach((p) => plats.add(p)));
        rows.push(`<div class="kv"><b>平台</b>${escapeHtml([...plats].join("、")) || "-"}</div>`);
      } else if (ls.source) {
        rows.push(`<div class="kv"><b>源名称</b>${escapeHtml(ls.source.name || "-")} ${ls.source.version ? "v" + escapeHtml(ls.source.version) : ""}</div>`);
        rows.push(`<div class="kv"><b>平台</b>${escapeHtml(Object.keys(ls.source.platforms || {}).join("、")) || "-"}</div>`);
      }
      if (ls.last_error || ls.error) rows.push(`<div class="kv"><b>错误</b>${escapeHtml(ls.last_error || ls.error)}</div>`);
      $("#ov-lx").innerHTML = rows.join("");
    } else {
      lxCard.hidden = true;
    }
  } catch (exc) {
    // 旧快照不能无限期显示绿色“运行就绪”；保留用户尚未保存的编辑。
    lxRuntimeSnapshot = null;
    lxRuntimeStatusUnknown = true;
    renderLxSourceList();
    $("#ov-provider-body").textContent = "状态加载失败：" + exc.message;
    $("#ov-processes").innerHTML = '<span class="chip"><span class="dot"></span>进程状态未知（获取失败）</span>';
    $("#ov-services").innerHTML = '<span class="state-line"><span class="dot"></span>服务状态未知（获取失败）</span>';
    $("#ov-lx").innerHTML = '<div class="kv"><b>状态</b>运行状态未知（获取失败）</div>';
  }
}

/* -------------------------------------------------------------- 配置读写 */
async function loadConfig(timeoutMs = 0) {
  const cfg = await api("/api/config", undefined, timeoutMs);
  configValues = cfg.values;
  configPresence = cfg.presence || {};
  currentConfigRevision = cfg.revision || "";
  configSchema = cfg.schema || {};
  scanAdminPasswordConfigured = Boolean(cfg.scan_admin_password_configured);
  const expectedBuild = typeof window !== "undefined" ? window.FNMUSIC_WEBUI_BUILD || "" : "";
  const apiBuild = cfg.webui_build || "";
  const mismatch = Boolean(expectedBuild && apiBuild && expectedBuild !== apiBuild)
    || Boolean(expectedBuild && !apiBuild);
  $("#webui-build-mismatch").hidden = !mismatch;
  applyConfigToForm();
  clearDirty();
  if (configSchema.FNMUSIC_PUBLIC_PLAYLISTS_BY_USER) {
    try { await loadMusicUsers(); } catch (err) { console.warn("加载歌单用户列表异常", err); }
  }
  try { await loadUserStorage(); } catch (err) { console.warn("加载用户目录异常", err); }
}

function applyConfigToForm() {
  const v = configValues;
  const provider = v.FNMUSIC_NETEASE_ENABLED === "true" ? "musicbox"
    : v.FNMUSIC_MUSICDL_ENABLED === "true" ? "musicdl"
    : v.FNMUSIC_LX_ENABLED === "true" ? "lxmusic" : "";
  $$("input[name=provider]").forEach((el) => { el.checked = el.value === provider; });
  syncProviderPanels(provider);
  if (provider === "musicbox") syncNeteaseAccount();
  const hasAutoSwitch = Boolean(configSchema.FNMUSIC_LX_AUTO_SWITCH);
  const hasPlatformPreferred = Boolean(configSchema.FNMUSIC_LX_PLATFORM_PREFERRED);
  $("#lx-auto-switch").disabled = !hasAutoSwitch;
  $("#lx-auto-switch").checked = hasAutoSwitch
    ? ["true", "1", "yes", "on"].includes(String(v.FNMUSIC_LX_AUTO_SWITCH ?? "true").trim().toLowerCase()) : false;
  $("#lx-platform-preferred").disabled = !hasPlatformPreferred;
  $("#lx-platform-preferred").checked = hasPlatformPreferred
    ? ["true", "1", "yes", "on"].includes(String(v.FNMUSIC_LX_PLATFORM_PREFERRED || "false").trim().toLowerCase()) : false;
  const playQuality = v.FNMUSIC_PLAY_QUALITY || "lossless";
  $$("input[name=play-quality]").forEach((el) => { el.checked = el.value === playQuality; });
  const downloadQuality = v.FNMUSIC_DOWNLOAD_QUALITY || "app";
  $$("input[name=download-quality]").forEach((el) => { el.checked = el.value === downloadQuality; });
  [
    ["#direct-netease", "FNMUSIC_NETEASE_DIRECT_PLAY"],
    ["#direct-musicdl", "FNMUSIC_MUSICDL_DIRECT_PLAY"],
    ["#direct-lx", "FNMUSIC_LX_DIRECT_PLAY"],
  ].forEach(([selector, key]) => {
    const el = $(selector);
    el.disabled = !configSchema[key];
    el.checked = configSchema[key] ? ["true", "1", "yes", "on"].includes(String(v[key] || "").trim().toLowerCase()) : false;
  });
  $("#recommend-hot").checked = v.FNMUSIC_RECOMMEND_HOT === "true";
  $("#recommend-daily").checked = v.FNMUSIC_RECOMMEND_DAILY === "true";
  $("#public-playlists-enabled").checked = v.FNMUSIC_PUBLIC_PLAYLISTS_ENABLED === "true";
  const supportsSyncAll = Boolean(configSchema.FNMUSIC_PUBLIC_PLAYLISTS_SYNC_ALL);
  const syncAllEl = $("#public-playlists-sync-all");
  syncAllEl.disabled = !supportsSyncAll || !$("#public-playlists-enabled").checked;
  publicSyncAll = supportsSyncAll && v.FNMUSIC_PUBLIC_PLAYLISTS_SYNC_ALL === "true";
  syncAllEl.checked = publicSyncAll;
  publicSelectionDetailSeq++;
  abortActivePublicDetailRequests();
  publicUserChanges.clear();
  publicSharedChanged = false;
  try {
    const sharedSelection = JSON.parse(v.FNMUSIC_PUBLIC_PLAYLISTS_SELECTION || "[]");
    publicSharedSelection = Array.isArray(sharedSelection) ? sharedSelection : [];
  } catch (_) { publicSharedSelection = []; }
  if (configSchema.FNMUSIC_PUBLIC_PLAYLISTS_BY_USER) {
    try {
      const mapping = JSON.parse(v.FNMUSIC_PUBLIC_PLAYLISTS_BY_USER || "{}");
      publicSelectionMap = mapping && typeof mapping === "object" && !Array.isArray(mapping) ? mapping : {};
    } catch (_) { publicSelectionMap = {}; }
    publicSelection = publicSyncAll
      ? publicSharedSelection
      : (Array.isArray(publicSelectionMap[publicUserGuid]) ? publicSelectionMap[publicUserGuid] : []);
  } else try {
    publicSelection = publicSharedSelection;
  } catch (_) { publicSelection = []; }
  $("#public-playlists-browser").hidden = !$("#public-playlists-enabled").checked;
  renderMusicUsers();
  renderPublicSelected();
  // 刷新/保存后重建的 publicSelection 只有身份字段：浏览器可见时补回展示名称
  void ensurePublicSelectionDetails();
  $("#tee-enabled").checked = v.FNMUSIC_TEE_SAVE_ENABLED === "true";
  $("#auto-cover").checked = v.FNMUSIC_AUTO_COVER !== "false";
  $("#lyric-auto-dl").checked = v.FNMUSIC_LYRIC_AUTO_DL === "true";
  $("#fav-autobind").checked = v.FNMUSIC_FAV_AUTO_BIND === "true";
  $("#dir-download").value = v.FNMUSIC_TEE_SAVE_DIR || "";
  $("#download-layout").value = v.FNMUSIC_DOWNLOAD_LAYOUT || "flat";
  $("#download-layout").disabled = !configSchema.FNMUSIC_DOWNLOAD_LAYOUT;
  $("#tee-save-mode").value = ["auto", "direct", "staged"].includes(v.FNMUSIC_TEE_SAVE_MODE) ? v.FNMUSIC_TEE_SAVE_MODE : "auto";
  $("#tee-save-mode").disabled = !configSchema.FNMUSIC_TEE_SAVE_MODE;
  $("#dir-cache").value = v.FNMUSIC_CACHE_DIR || "";
  $("#tee-max").value = v.FNMUSIC_TEE_CACHE_MAX || "2";
  $("#bind-timeout").value = v.FNMUSIC_OFFICIAL_BIND_TIMEOUT_S || "120";
  $("#handoff-max").value = v.FNMUSIC_TEE_HANDOFF_MAX != null ? v.FNMUSIC_TEE_HANDOFF_MAX : "3";
  $("#scan-path").value = v.FNMUSIC_LIBRARY_SCAN_PATH || "";
  $("#scan-delay").value = v.FNMUSIC_SCAN_DELAY_S ?? "3";
  $("#scan-cooldown").value = v.FNMUSIC_SCAN_COOLDOWN_S ?? "90";
  updateScanLabels();
  updateTeeCountLabel();
  updateBindTimeoutLabel();
  if ($("#tee-save-by-user")) {
    $("#tee-save-by-user").checked = v.FNMUSIC_TEE_SAVE_BY_USER === "true";
    $("#tee-save-by-user").disabled = !configSchema.FNMUSIC_TEE_SAVE_BY_USER;
  }
  savedUserDirs = parseUserMap(v.FNMUSIC_TEE_SAVE_DIR_BY_USER);
  savedUserScans = parseUserMap(v.FNMUSIC_LIBRARY_SCAN_BY_USER);
  if ($("#scan-admin-username")) $("#scan-admin-username").value = v.FNMUSIC_SCAN_ADMIN_USERNAME || "";
  if ($("#scan-admin-device-id")) $("#scan-admin-device-id").value = v.FNMUSIC_SCAN_ADMIN_DEVICE_ID || "";
  scanAdminPasswordDirty = false;
  scanAdminPasswordClear = false;
  if ($("#scan-admin-password")) {
    $("#scan-admin-password").value = "";
    $("#scan-admin-password").placeholder = scanAdminPasswordConfigured ? "已保存，留空表示不修改" : "未配置";
  }
  $("#llm-base").value = v.FNMUSIC_LLM_BASE_URL || "";
  $("#llm-key").value = v.FNMUSIC_LLM_API_KEY || "";
  $("#llm-model").value = v.FNMUSIC_LLM_MODEL || "";
  $("#llm-protocol").value = v.FNMUSIC_LLM_PROTOCOL === "responses" ? "responses" : "chat";
  $("#llm-max-tokens").value = v.FNMUSIC_LLM_MAX_TOKENS || "8192";
  $("#llm-daily-limit").value = v.FNMUSIC_LLM_DAILY_TOKEN_LIMIT ?? "1000000";
  $("#llm-extra-body").value = v.FNMUSIC_LLM_EXTRA_BODY || "";
  $("#llm-liked").checked = v.FNMUSIC_LLM_LIKED_ENABLED === "true";
  $("#llm-daily").checked = v.FNMUSIC_LLM_DAILY_ENABLED === "true";
  $("#liked-refresh-hours").value = v.FNMUSIC_LIKED_REFRESH_HOURS ?? "0.5";
  $("#liked-size").value = v.FNMUSIC_LIKED_SIZE ?? "20";
  updateLlmLikedSettings();
  refreshLlmUsage();
  $("#search-timeout").value = v.FNMUSIC_SEARCH_TIMEOUT || "30";
  $("#search-deep").checked = v.FNMUSIC_SEARCH_DEEP_PAGE !== "false";
  $("#search-deep-max").value = v.FNMUSIC_SEARCH_DEEP_MAX_PAGES || "10";
  $("#netease-my-playlists").checked = v.FNMUSIC_NETEASE_MY_PLAYLISTS === "true";
  $("#netease-playlist-resident").checked = v.FNMUSIC_NETEASE_PLAYLIST_RESIDENT === "true";
  $("#lx-url").value = "";
  lxVerifiedUrl = null;
  $("#lx-resolver-timeout").value = v.LX_RESOLVER_TIMEOUT || "8.0";
  $("#lx-url-timeout").value = v.LX_URL_TIMEOUT || "15.0";
  $("#lx-probe-timeout").value = v.LX_PROBE_TIMEOUT || "3.0";
  lxSourceListDirty = false;
  try {
    const rawList = v.LX_SOURCE_LIST;
    lxSourceList = rawList ? JSON.parse(rawList) : [];
    if (!Array.isArray(lxSourceList)) lxSourceList = [];
  } catch (_) {
    lxSourceList = [];
  }
  try {
    const parsedOrder = JSON.parse(v.FNMUSIC_LX_SOURCE_ORDER || "[]");
    lxSourceOrder = Array.isArray(parsedOrder)
      ? [...new Set(parsedOrder.filter((id) => typeof id === "string" && id.trim()).map((id) => id.trim()))]
      : [];
  } catch (_) { lxSourceOrder = []; }
  // 兼容旧数据：列表无任何 active 标记时按 LX_SOURCE_URL 推导
  const savedUrl = (v.LX_SOURCE_URL || "").trim();
  if (lxSourceList.length && !lxSourceList.some((item) => "active" in item)) {
    lxSourceList.forEach((item) => { item.active = !!(savedUrl && item.url === savedUrl); });
  }
  // .env 里有激活地址但不在列表中（异常状态）：补进列表展示真实激活态
  if (savedUrl && !lxSourceList.some((item) => item.url === savedUrl)) {
    lxSourceList.unshift({ name: lxDefaultName(savedUrl), url: savedUrl, active: true });
  }
  syncLxSourceOrder();
  updateLxAutoSwitchSettings();
  renderLxSourceList();
  renderLxSourceOrder();
  syncLxAddButton();
  renderPlatformChips();
}

function collectConfig() {
  const provider = ($$("input[name=provider]").find((el) => el.checked) || {}).value || "";
  const values = {
    FNMUSIC_MUSICDL_ENABLED: provider === "musicdl",
    FNMUSIC_NETEASE_ENABLED: provider === "musicbox",
    FNMUSIC_LX_ENABLED: provider === "lxmusic",
    ...(configSchema.FNMUSIC_NETEASE_DIRECT_PLAY ? { FNMUSIC_NETEASE_DIRECT_PLAY: $("#direct-netease").checked } : {}),
    ...(configSchema.FNMUSIC_MUSICDL_DIRECT_PLAY ? { FNMUSIC_MUSICDL_DIRECT_PLAY: $("#direct-musicdl").checked } : {}),
    ...(configSchema.FNMUSIC_LX_DIRECT_PLAY ? { FNMUSIC_LX_DIRECT_PLAY: $("#direct-lx").checked } : {}),
    ...(configSchema.FNMUSIC_PLAY_QUALITY ? {
      FNMUSIC_PLAY_QUALITY: ($$("input[name=play-quality]").find((el) => el.checked) || {}).value || "lossless",
    } : {}),
    ...(configSchema.FNMUSIC_DOWNLOAD_QUALITY ? {
      FNMUSIC_DOWNLOAD_QUALITY: ($$("input[name=download-quality]").find((el) => el.checked) || {}).value || "app",
    } : {}),
    ...(configSchema.FNMUSIC_LX_AUTO_SWITCH ? { FNMUSIC_LX_AUTO_SWITCH: $("#lx-auto-switch").checked } : {}),
    ...(configSchema.FNMUSIC_LX_PLATFORM_PREFERRED && $("#lx-auto-switch").checked
      ? { FNMUSIC_LX_PLATFORM_PREFERRED: $("#lx-platform-preferred").checked } : {}),
    ...(configSchema.FNMUSIC_LX_SOURCE_ORDER
      ? { FNMUSIC_LX_SOURCE_ORDER: JSON.stringify(lxSourceOrder) } : {}),
    FNMUSIC_RECOMMEND_HOT: $("#recommend-hot").checked,
    FNMUSIC_RECOMMEND_DAILY: $("#recommend-daily").checked,
    FNMUSIC_PUBLIC_PLAYLISTS_ENABLED: $("#public-playlists-enabled").checked,
    ...(configSchema.FNMUSIC_PUBLIC_PLAYLISTS_SYNC_ALL
      ? { FNMUSIC_PUBLIC_PLAYLISTS_SYNC_ALL: publicSyncAll } : {}),
    FNMUSIC_TEE_SAVE_ENABLED: $("#tee-enabled").checked,
    FNMUSIC_AUTO_COVER: $("#auto-cover").checked,
    FNMUSIC_LYRIC_AUTO_DL: $("#lyric-auto-dl").checked,
    FNMUSIC_FAV_AUTO_BIND: $("#fav-autobind").checked,
    FNMUSIC_TEE_SAVE_DIR: $("#dir-download").value.trim(),
    ...(configSchema.FNMUSIC_DOWNLOAD_LAYOUT ? { FNMUSIC_DOWNLOAD_LAYOUT: $("#download-layout").value } : {}),
    ...(configSchema.FNMUSIC_TEE_SAVE_MODE ? { FNMUSIC_TEE_SAVE_MODE: ["auto", "direct", "staged"].includes($("#tee-save-mode").value) ? $("#tee-save-mode").value : "auto" } : {}),
    FNMUSIC_CACHE_DIR: $("#dir-cache").value.trim(),
    FNMUSIC_TEE_CACHE_MAX: parseInt($("#tee-max").value || "2", 10),
    FNMUSIC_OFFICIAL_BIND_TIMEOUT_S: parseInt($("#bind-timeout").value || "120", 10) || 120,
    FNMUSIC_TEE_HANDOFF_MAX: parseInt($("#handoff-max").value || "3", 10) || 0,
    FNMUSIC_LIBRARY_SCAN_PATH: $("#scan-path").value.trim(),
    FNMUSIC_SCAN_DELAY_S: parseFloat($("#scan-delay").value) || 0,
    FNMUSIC_SCAN_COOLDOWN_S: parseFloat($("#scan-cooldown").value) || 0,
    ...(configSchema.FNMUSIC_TEE_SAVE_BY_USER && $("#tee-save-by-user")
      ? { FNMUSIC_TEE_SAVE_BY_USER: $("#tee-save-by-user").checked } : {}),
    ...(configSchema.FNMUSIC_SCAN_ADMIN_USERNAME && $("#scan-admin-username")
      ? { FNMUSIC_SCAN_ADMIN_USERNAME: $("#scan-admin-username").value.trim() } : {}),
    ...(configSchema.FNMUSIC_SCAN_ADMIN_DEVICE_ID && $("#scan-admin-device-id")
      ? { FNMUSIC_SCAN_ADMIN_DEVICE_ID: $("#scan-admin-device-id").value.trim() } : {}),
    ...scanAdminPasswordUpdate(),
    FNMUSIC_LLM_BASE_URL: $("#llm-base").value.trim(),
    FNMUSIC_LLM_API_KEY: $("#llm-key").value.trim(),
    FNMUSIC_LLM_MODEL: $("#llm-model").value.trim(),
    FNMUSIC_LLM_PROTOCOL: $("#llm-protocol").value === "responses" ? "responses" : "chat",
    FNMUSIC_LLM_MAX_TOKENS: parseInt($("#llm-max-tokens").value || "8192", 10) || 8192,
    FNMUSIC_LLM_DAILY_TOKEN_LIMIT: parseInt($("#llm-daily-limit").value ?? "1000000", 10),
    FNMUSIC_LLM_EXTRA_BODY: $("#llm-extra-body").value.trim(),
    FNMUSIC_LLM_LIKED_ENABLED: $("#llm-liked").checked,
    FNMUSIC_LLM_DAILY_ENABLED: $("#llm-daily").checked,
    FNMUSIC_SEARCH_TIMEOUT: parseInt($("#search-timeout").value || "30", 10) || 30,
    FNMUSIC_SEARCH_DEEP_PAGE: $("#search-deep").checked,
    FNMUSIC_SEARCH_DEEP_MAX_PAGES: parseInt($("#search-deep-max").value || "10", 10) || 10,
    FNMUSIC_NETEASE_MY_PLAYLISTS: $("#netease-my-playlists").checked,
    FNMUSIC_NETEASE_PLAYLIST_RESIDENT: $("#netease-playlist-resident").checked,
    LX_SOURCE_LIST: JSON.stringify(lxSourceList),
  };
  if (!$("#panel-lxmusic").hidden) {
    values.LX_RESOLVER_TIMEOUT = lxTimeoutNumber("#lx-resolver-timeout", "单次解析超时");
    values.LX_URL_TIMEOUT = lxTimeoutNumber("#lx-url-timeout", "取链总超时");
    values.LX_PROBE_TIMEOUT = lxTimeoutNumber("#lx-probe-timeout", "直链探活超时");
  }
  // 隐藏的参数不提交：关闭用途不清空已保存值，也不让隐藏的无效编辑阻塞保存。
  if (values.FNMUSIC_LLM_LIKED_ENABLED) {
    values.FNMUSIC_LIKED_REFRESH_HOURS = likedNumber("#liked-refresh-hours", "猜你喜欢生成间隔", 0.5, null);
    values.FNMUSIC_LIKED_SIZE = likedNumber("#liked-size", "猜你喜欢歌曲数量", 0, 100, true);
  }
  if (configSchema.FNMUSIC_PUBLIC_PLAYLISTS_BY_USER) {
    stashPublicUser();
    if (publicUserChanges.size) {
      const changes = {};
      for (const user of publicUserChanges) {
        changes[user] = publicSelectionPayload(publicSelectionMap[user] || []);
      }
      values.FNMUSIC_PUBLIC_PLAYLISTS_BY_USER = JSON.stringify(changes);
    }
    if (configSchema.FNMUSIC_PUBLIC_PLAYLISTS_SYNC_ALL
        && (publicSharedChanged || configPresence.FNMUSIC_PUBLIC_PLAYLISTS_SELECTION)) {
      values.FNMUSIC_PUBLIC_PLAYLISTS_SELECTION = JSON.stringify(
        publicSelectionPayload(publicSharedSelection)
      );
    }
  } else if ($("#public-playlists-enabled").checked || publicSelection.length || configPresence.FNMUSIC_PUBLIC_PLAYLISTS_SELECTION) {
    values.FNMUSIC_PUBLIC_PLAYLISTS_SELECTION = JSON.stringify(
      publicSelectionPayload(publicSelection)
    );
  }
  Object.assign(values, userStorageConfigPatch());
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
  validateUserStoragePatch(values);
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

function filterConfigBySchema(values) {
  const supported = new Set(Object.keys(configSchema || {}));
  // 正常后端总会返回 Schema；无 Schema 的旧测试桩/旧服务保留原有兼容流程。
  if (!supported.size) return values;
  return Object.fromEntries(Object.entries(values).filter(([key]) => supported.has(key)));
}

async function pollConfigJob(jobId, onProgress, timeoutMs = 60000) {
  const deadline = performance.now() + timeoutMs;
  const interval = globalThis.CONFIG_JOB_POLL_INTERVAL ?? 600;
  while (performance.now() < deadline) {
    try {
      const data = await api(`/api/config/jobs/${encodeURIComponent(jobId)}`, undefined,
        Math.max(1, deadline - performance.now()));
      const job = data.job;
      if (!job) throw new Error("无法获取任务详情");
      if (onProgress) onProgress(job);
      if (["success", "partial"].includes(job.status)) return job.result || {};
      if (["failed", "conflict"].includes(job.status)) throw Object.assign(
        new Error(job.error || job.message || "保存失败"), {terminal: true});
    } catch (exc) {
      if (exc.status === 404 || exc.terminal) throw exc;
    }
    if (interval > 0) await new Promise((resolve) => setTimeout(resolve, interval));
  }
  throw new Error("保存已达到时限，请刷新运行状态查看结果");
}

async function checkActiveConfigJob() {
  try {
    const data = await api("/api/config/jobs/active", undefined, 3000);
    if (!data.active || !data.job) return;
    const btn = $("#save-btn");
    btn.disabled = true;
    try {
      await pollConfigJob(data.job.job_id, (job) => {
        btn.textContent = `保存中（${job.progress || 0}%）…`;
      });
      await loadConfig(3000);
      clearDirty();
      toast("配置已保存生效", "ok");
    } catch (exc) {
      toast("保存任务失败：" + exc.message, "fail");
    } finally {
      btn.disabled = false;
      btn.textContent = "保存并生效";
    }
  } catch (_) { /* 旧后端可无任务接口 */ }
}

async function saveConfig() {
  const btn = $("#save-btn");
  if (btn.disabled) return;
  btn.disabled = true;
  const started = performance.now();
  const deadline = started + 60000;
  const remaining = () => Math.max(1, deadline - performance.now());
  try {
    let values;
    try { values = filterConfigBySchema(collectConfig()); } catch (exc) { toast(exc.message, "fail"); return; }
    // 从点击起计时，目录检查共用三秒，不能在按钮禁用前无限等待。
    btn.textContent = "保存中（检查目录）…";
    for (const [key, label] of [["FNMUSIC_TEE_SAVE_DIR", "下载目录"], ["FNMUSIC_CACHE_DIR", "歌曲缓存目录"]]) {
      const next = values[key] || "";
      if (next && next !== (configValues[key] || "")) {
        const res = await fsCheckDir(next, Math.max(1, started + 3000 - performance.now()));
        if (res.mode === "fail") { toast(label + "不可用：" + res.message, "fail"); return; }
      }
    }
    const dirPatch = values.FNMUSIC_TEE_SAVE_DIR_BY_USER || {};
    for (const [guid, path] of Object.entries(dirPatch)) {
      if (!path || path === (savedUserDirs[guid] || "")) continue;
      const res = await fsCheckDir(path, Math.max(1, started + 3000 - performance.now()));
      if (res.mode === "fail") { toast("用户目录不可用：" + res.message, "fail"); return; }
    }
    const pendingNew = lxPendingActivationCount();
    btn.textContent = pendingNew > 0
      ? `保存中（快速确认 ${pendingNew} 个新源，一分钟内返回）…`
      : "保存中（一分钟内返回）…";
    if (currentPage === "lx" && !lxSourceListDirty) delete values.LX_SOURCE_LIST;
    let result = await api("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values, base_revision: currentConfigRevision || null }),
    }, remaining());
    if (result.job_id && ["pending", "running"].includes(result.status)) {
      result = await pollConfigJob(result.job_id, (job) => {
        btn.textContent = `保存中（${job.progress || 0}%）…`;
      }, remaining());
    }
    if (result.new_revision) currentConfigRevision = result.new_revision;
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
        const outcome = a.ok ? "成功" : a.category === "not_executed" ? "未执行"
          : ["result_unknown", "initialization_pending"].includes(a.category) ? "结果待核实" : "失败";
        parts.push(`洛雪源${label}${op}${outcome}`);
      }
      if (a.kind === "lx_reorder") parts.push(`洛雪源排序${a.ok ? "成功" : "未确认，保留原顺序"}`);
    });
    warnings.forEach((w) => {
      if (w.category && !["runtime", "runtime_error"].includes(w.category)) {
        const reason = { service_unavailable: "服务暂不可用", initialization_pending: "初始化待定", source_operation_busy: "源操作忙碌",
          result_unknown: "结果待核实", not_executed: "未执行" }[w.category] || "状态暂不可确认";
        const state = w.saved_active === false ? "未保存为激活" : "激活配置已保留";
        parts.push(`洛雪源「${w.source || lxDefaultName(w.url)}」${reason}，${state}${w.message ? `（${w.message}）` : ""}`);
      } else if (w.auto_disabled === false) {
        parts.push(`洛雪源「${w.source || lxDefaultName(w.url)}」初始化失败，保留原激活配置${w.message ? `（${w.message}）` : ""}`);
      } else {
        parts.push(`洛雪源「${w.source || lxDefaultName(w.url)}」失效，无法激活，已自动取消激活${w.message ? `（${w.message}）` : ""}`);
      }
    });
    if (failed.length || result.complete === false) {
      toast((parts.join("；") || "") + ` —— ${failed.map((f) => f.error).join("；")}`, "fail");
    } else {
      toast(parts.join("；") || "配置无变化", "ok");
    }
    // 只按本次已确认成功的停用操作清标；部分失败或超时不能凭本地表单状态清除。
    clearLxCpuAlertsAfterSavedConfig(result);
    if (result.values) {
      configValues = { ...configValues, ...result.values };
      applyConfigToForm();
      clearDirty();
    } else {
      // 兼容旧后端；配置回读仍使用本次剩余预算。
      await loadConfig(remaining());
    }
    void loadStatus(3000);
    refreshLlmUsage();
  } catch (exc) {
    lxRuntimeSnapshot = null;
    renderLxSourceList();
    toast("保存失败：" + exc.message, "fail");
    void loadStatus(3000);
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
$("#platform-test-btn").addEventListener("click", () => {
  const panel = $("#platform-test-panel");
  panel.hidden = !panel.hidden;
  if (!panel.hidden && !$("#platform-test-tbody").children.length) renderPlatformTestTable([]);
});

/* -------------------------------------------------------------- 平台批量测试 */
let platformTestJob = null;
let platformTestTimer = null;
let platformTestPolling = false;

function formatPlatformLatency(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n < 1000) return `${Math.round(n)}ms`;
  return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}s`;
}

function renderPlatformTestStats(done, total, okCount, failCount) {
  const setText = (sel, text) => { const el = $(sel); if (el) el.textContent = text; };
  setText("#platform-test-stat-done", `${done}/${total}`);
  setText("#platform-test-stat-ok", String(okCount));
  setText("#platform-test-stat-fail", String(failCount));
}

function setPlatformTestMessage(text) {
  const el = $("#platform-test-summary");
  if (el) el.textContent = text || "";
}

function renderPlatformTestTable(results) {
  const tbody = $("#platform-test-tbody");
  const wrap = $("#platform-test-table-wrap");
  if (!results || !results.length) { wrap.hidden = true; tbody.innerHTML = ""; return; }
  wrap.hidden = false;
  const enabledSet = new Set(platforms.enabled);
  tbody.innerHTML = results.map((r) => {
    const ok = r.status === "ok";
    const cls = ok ? "ok" : "err";
    const titles = ok ? (r.titles || [r.title]).filter(Boolean).join("、") : (r.error || "");
    const isOn = enabledSet.has(r.platform);
    const badge = ok ? '<span class="pt-badge ok">✓ 可用</span>' : '<span class="pt-badge err">✗ 不可用</span>';
    const toggle = `<button class="btn small platform-toggle${isOn ? " on" : ""}" data-platform="${escapeHtml(r.platform)}" data-on="${isOn}">${isOn ? "停用" : "启用"}</button>`;
    const count = Number.isFinite(Number(r.count)) ? Number(r.count) : 0;
    return `<tr class="${cls}">`
      + `<td class="pt-name">${escapeHtml(r.platform)}</td>`
      + `<td><div class="pt-status">${badge}${toggle}</div></td>`
      + `<td class="num">${formatPlatformLatency(r.latency_ms)}</td>`
      + `<td class="num">${count}</td>`
      + `<td class="pt-sample" title="${escapeHtml(titles)}">${escapeHtml(titles || "—")}</td>`
      + `</tr>`;
  }).join("");
  tbody.querySelectorAll(".platform-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const p = btn.dataset.platform;
      const set = new Set(platforms.enabled);
      if (set.has(p)) set.delete(p); else set.add(p);
      platforms.enabled = Array.from(set);
      renderPlatformChips();
      renderPlatformTestTable(platformTestJob ? platformTestJob.results : results);
      markDirty("musicdl 平台已修改");
    });
  });
}

function updatePlatformTestProgress(done, total) {
  const wrap = $("#platform-test-progress-wrap");
  const fill = $("#platform-test-progress");
  const text = $("#platform-test-progress-text");
  wrap.hidden = false;
  const pct = total ? Math.round((done / total) * 100) : 0;
  fill.style.width = pct + "%";
  text.textContent = `${done}/${total} · ${pct}%`;
}

async function pollPlatformTestJob() {
  if (platformTestPolling || !platformTestJob || platformTestJob.status !== "running") return;
  const job = platformTestJob;
  platformTestPolling = true;
  try {
    const data = await api("/api/platforms/test/status?job=" + encodeURIComponent(job.job_id), undefined, 5000);
    if (platformTestJob !== job || job.status !== "running") return;
    if (!data.ok) throw new Error(data.error || "status failed");
    platformTestJob.status = data.status;
    platformTestJob.results = data.results || [];
    platformTestJob.progress = data.progress || platformTestJob.progress;
    renderPlatformTestTable(platformTestJob.results);
    updatePlatformTestProgress(platformTestJob.progress.done, platformTestJob.progress.total);
    const okCount = data.ok_count ?? platformTestJob.results.filter((r) => r.status === "ok").length;
    const failCount = (data.fail_count ?? platformTestJob.results.filter((r) => r.status !== "ok").length);
    renderPlatformTestStats(platformTestJob.progress.done, platformTestJob.progress.total, okCount, failCount);
    $("#platform-test-enable-ok").textContent = `启用成功的 (${okCount})`;
    $("#platform-test-disable-fail").textContent = `关闭无效的 (${failCount})`;
    if (data.status !== "running") {
      $("#platform-test-actions").hidden = false;
      $("#platform-test-start").disabled = false;
      $("#platform-test-stop").disabled = true;
      setPlatformTestMessage(data.status === "done" ? "测试完成，可批量启停平台" :
        data.status === "cancelled" ? "已停止，可操作已完成的结果" : "测试失败，可重新测试");
      clearTimeout(platformTestTimer);
      platformTestTimer = null;
    } else {
      setPlatformTestMessage("测试进行中…");
      platformTestTimer = setTimeout(pollPlatformTestJob, 1000);
    }
  } catch (exc) {
    if (platformTestJob !== job || job.status !== "running") return;
    if (exc.status === 404) {
      job.status = "expired";
      $("#platform-test-start").disabled = false;
      $("#platform-test-stop").disabled = true;
      setPlatformTestMessage("测试记录已过期，请重新测试");
    } else {
      setPlatformTestMessage(`查询失败，将重试：${exc.message}`);
      platformTestTimer = setTimeout(pollPlatformTestJob, 1000);
    }
  } finally {
    platformTestPolling = false;
  }
}

async function startPlatformTest() {
  const keyword = $("#platform-test-keyword").value.trim() || "周杰伦";
  const concurrency = parseInt($("#platform-test-concurrency").value, 10) || 4;
  $("#platform-test-start").disabled = true;
  $("#platform-test-stop").disabled = true;
  clearTimeout(platformTestTimer);
  platformTestJob = null;
  $("#platform-test-actions").hidden = true;
  setPlatformTestMessage("正在启动…");
  renderPlatformTestStats(0, 0, 0, 0);
  renderPlatformTestTable([]);
  $("#platform-test-progress-wrap").hidden = true;
  try {
    const data = await api("/api/platforms/test/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ keyword, concurrency }) }, 15000);
    if (!data.ok) throw new Error(data.error || "start failed");
    platformTestJob = { job_id: data.job_id, status: "running", results: [], progress: { done: 0, total: data.total } };
    $("#platform-test-stop").disabled = false;
    updatePlatformTestProgress(0, data.total);
    platformTestTimer = setTimeout(pollPlatformTestJob, 1000);
  } catch (exc) {
    setPlatformTestMessage(`启动失败：${exc.message}`);
    $("#platform-test-start").disabled = false;
    $("#platform-test-stop").disabled = true;
  }
}

async function stopPlatformTest() {
  const job = platformTestJob;
  if (!job || job.status !== "running") return;
  $("#platform-test-stop").disabled = true;
  try {
    const data = await api("/api/platforms/test/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ job_id: job.job_id }) }, 10000);
    if (!data.ok) throw new Error(data.error || "stop failed");
    if (platformTestJob !== job) return;
    job.status = "cancelled";
    clearTimeout(platformTestTimer);
    platformTestTimer = null;
    $("#platform-test-start").disabled = false;
    $("#platform-test-actions").hidden = false;
    setPlatformTestMessage("已停止，可操作已完成的结果");
  } catch (exc) {
    if (platformTestJob !== job) return;
    $("#platform-test-stop").disabled = job.status !== "running";
    setPlatformTestMessage(`停止失败：${exc.message}`);
  }
}

$("#platform-test-start").addEventListener("click", startPlatformTest);
$("#platform-test-stop").addEventListener("click", stopPlatformTest);
$("#platform-test-enable-ok").addEventListener("click", () => {
  const okPlatforms = platformTestJob ? platformTestJob.results.filter((r) => r.status === "ok").map((r) => r.platform) : [];
  const set = new Set(platforms.enabled);
  okPlatforms.forEach((p) => set.add(p));
  platforms.enabled = Array.from(set);
  renderPlatformChips();
  renderPlatformTestTable(platformTestJob ? platformTestJob.results : []);
  markDirty("已启用测试成功的平台");
});
$("#platform-test-disable-fail").addEventListener("click", () => {
  const failPlatforms = platformTestJob ? platformTestJob.results.filter((r) => r.status !== "ok").map((r) => r.platform) : [];
  const set = new Set(platforms.enabled);
  failPlatforms.forEach((p) => set.delete(p));
  platforms.enabled = Array.from(set);
  renderPlatformChips();
  renderPlatformTestTable(platformTestJob ? platformTestJob.results : []);
  markDirty("已关闭测试失败的平台");
});
$("#platform-test-enable-all").addEventListener("click", () => {
  const all = platformTestJob ? platformTestJob.results.map((r) => r.platform) : platforms.registered;
  platforms.enabled = Array.from(new Set(all));
  renderPlatformChips();
  renderPlatformTestTable(platformTestJob ? platformTestJob.results : []);
  markDirty("已启用全部测试平台");
});
$("#platform-test-disable-all").addEventListener("click", () => {
  platforms.enabled = [];
  renderPlatformChips();
  renderPlatformTestTable(platformTestJob ? platformTestJob.results : []);
  markDirty("已停用全部平台");
});

/* -------------------------------------------------------------- 公共歌单 */
const PUBLIC_PROVIDER_OPTIONS = [
  { value: "musicbox", label: "网易云音乐" },
  { value: "lxmusic", label: "洛雪内置歌单" },
];
const PUBLIC_SOURCE_LABEL = { netease: "网易", wy: "网易", kw: "酷我", kg: "酷狗", tx: "QQ", mg: "咪咕" };

function publicSelectionPayload(selection) {
  return (selection || []).map(({ provider, source, kind, id }) =>
    ({ provider, source, kind, id }));
}

function publicSelectionTarget() {
  return publicSyncAll ? "all" : `user:${publicUserGuid}`;
}

function publicSelectionEditable() {
  if (!$("#public-playlists-enabled").checked) return false;
  if (publicSyncAll) return true;
  if (configSchema.FNMUSIC_PUBLIC_PLAYLISTS_BY_USER) {
    return publicUsersReady && Boolean(publicUserGuid);
  }
  return true;
}

function stashPublicUser(changed = false) {
  if (publicSyncAll) {
    publicSharedSelection = publicSelection;
    if (changed) publicSharedChanged = true;
    return;
  }
  if (!configSchema.FNMUSIC_PUBLIC_PLAYLISTS_BY_USER || !publicUserGuid) return;
  publicSelectionMap[publicUserGuid] = publicSelection;
  if (changed) publicUserChanges.add(publicUserGuid);
}

function renderMusicUsers() {
  const panel = $("#public-users-panel");
  if (!panel) return;
  const perUser = !!configSchema.FNMUSIC_PUBLIC_PLAYLISTS_BY_USER;
  panel.hidden = !perUser || !$("#public-playlists-enabled").checked;
  if (!perUser) return;
  const selector = $("#public-user");
  const syncAllEl = $("#public-playlists-sync-all");
  syncAllEl.disabled = !configSchema.FNMUSIC_PUBLIC_PLAYLISTS_SYNC_ALL
    || !$("#public-playlists-enabled").checked;
  syncAllEl.checked = publicSyncAll;
  selector.innerHTML = '<option value="">请先选择飞牛音乐用户</option>' + publicUsers.map((user) =>
    `<option value="${escapeHtml(user.guid)}">${escapeHtml(user.name || user.guid)}${user.role === "admin" ? "（管理员）" : ""}</option>`).join("");
  selector.value = publicUserGuid;
  selector.disabled = publicSyncAll || !publicUsersReady || !publicUsers.length;
  $("#public-playlists-browser").hidden = !publicSelectionEditable();
  $("#public-users-note").textContent = publicSyncAll
    ? "正在配置所有用户共享的歌单；个人配置已保留。"
    : (publicUsersReady
      ? (publicUsers.length
        ? `共 ${publicUsers.length} 位有效用户。先选择用户，再配置歌单；新用户默认没有公共歌单。`
        : "飞牛音乐中还没有有效用户。请先登录飞牛音乐，再刷新用户。")
      : $("#public-users-note").textContent);
}

async function loadMusicUsers() {
  const seq = ++publicUsersSeq;
  $("#public-users-retry").disabled = true;
  $("#public-users-note").textContent = "正在读取飞牛音乐用户…";
  try {
    const data = await api("/api/maintenance/music-users");
    if (seq !== publicUsersSeq) return;
    if (data.ok === false || !Array.isArray(data.users)) throw new Error(data.error || "用户列表响应无效");
    publicUsers = data.users;
    publicUsersReady = true;
    if (!publicUserChanges.size && data.public_playlists_by_user) {
      publicSelectionMap = data.public_playlists_by_user;
    }
    if (!publicUsers.some((user) => user.guid === publicUserGuid)) publicUserGuid = "";
    if (!publicSyncAll) publicSelection = publicSelectionMap[publicUserGuid] || [];
  } catch (exc) {
    if (seq !== publicUsersSeq) return;
    publicUsersReady = false;
    if (!publicSyncAll) {
      invalidatePublicBrowse();
      publicSelectionDetailSeq++;
      abortActivePublicDetailRequests();
    }
    const isDirectRestricted = (exc && (exc.status === 403 || exc.status === 501))
      || String(exc && exc.message || "").includes("403")
      || String(exc && exc.message || "").includes("501")
      || String(exc && exc.message || "").includes("直连模式")
      || String(exc && exc.message || "").includes("安全架构")
      || String(exc && exc.message || "").includes("管理员");
    if (isDirectRestricted) {
      $("#public-users-note").textContent = "安全架构限制：直连模式不支持读取飞牛音乐用户，请在飞牛桌面内打开管理台。";
    } else {
      $("#public-users-note").textContent = "读取用户失败：" + exc.message + "。请点击刷新用户重试。";
    }
  } finally {
    if (seq === publicUsersSeq) {
      $("#public-users-retry").disabled = false;
      renderMusicUsers();
      renderPublicSelected();
      if (publicSelectionEditable()) void ensurePublicSelectionDetails();
      updateResidentAvailability();
    }
  }
}

async function selectPublicUser(guid) {
  stashPublicUser();
  if (publicSyncAll) {
    renderMusicUsers();
    return;
  }
  invalidatePublicBrowse();
  publicSelectionDetailSeq++;
  abortActivePublicDetailRequests();
  publicUserGuid = publicUsersReady && publicUsers.some((user) => user.guid === guid) ? guid : "";
  publicSelection = publicSelectionMap[publicUserGuid] || [];
  renderMusicUsers();
  renderPublicSelected();
  void ensurePublicSelectionDetails();
  if (publicUserGuid && $("#public-playlists-enabled").checked) await showPublicBrowser();
}

$("#public-user").addEventListener("change", () => selectPublicUser($("#public-user").value));
$("#public-users-retry").addEventListener("click", async () => {
  await loadMusicUsers();
  if (publicSelectionEditable()) await showPublicBrowser();
});

function anyPublicMusicboxSelection() {
  stashPublicUser();
  const selections = publicSyncAll
    ? [publicSharedSelection]
    : (configSchema.FNMUSIC_PUBLIC_PLAYLISTS_BY_USER ? Object.values(publicSelectionMap) : [publicSelection]);
  return selections.some((selection) => Array.isArray(selection) && selection.some((item) => item.provider === "netease" || item.provider === "musicbox"));
}

function publicEntryKey(item) {
  const isNetease = item.provider === "netease" || item.provider === "musicbox";
  return [isNetease ? "netease" : item.provider, item.source, isNetease ? "playlist" : item.kind, item.id].join("|");
}

function selectedPublicEntry(item) {
  return publicSelection.find((it) => publicEntryKey(it) === publicEntryKey(item));
}

function setPublicSelection(item, selected) {
  if (!publicSelectionEditable()) return false;
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
    setPublicDetailCache(publicEntryKey(item), item, true);
  } else {
    publicSelection = publicSelection.filter((it) => publicEntryKey(it) !== publicEntryKey(item));
  }
  stashPublicUser(true);
  renderPublicSelected();
  updateResidentAvailability();
  markDirty("公共歌单选择已修改");
  renderPublicList();
  void ensurePublicSelectionDetails();
  return true;
}

function movePublicSelection(index, delta) {
  const next = index + delta;
  if (next < 0 || next >= publicSelection.length) return;
  const items = publicSelection.splice(index, 1);
  publicSelection.splice(next, 0, items[0]);
  stashPublicUser(true);
  renderPublicSelected();
  markDirty("公共歌单顺序已修改");
}

function loadPublicMetadataStorage() {
  try {
    if (typeof localStorage === "undefined") return {};
    const raw = localStorage.getItem(PUBLIC_METADATA_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return typeof parsed === "object" && parsed !== null ? parsed : {};
  } catch (_) {
    return {};
  }
}

function savePublicMetadataStorage(data) {
  try {
    if (typeof localStorage === "undefined") return;
    localStorage.setItem(PUBLIC_METADATA_STORAGE_KEY, JSON.stringify(data));
  } catch (_) {}
}

function initPublicDetailCacheFromStorage() {
  const stored = loadPublicMetadataStorage();
  const now = Date.now();
  let changed = false;
  for (const [key, record] of Object.entries(stored)) {
    if (!record || typeof record !== "object") continue;
    if (record.failed) {
      delete stored[key];
      changed = true;
    } else if (record.name && String(record.name).trim() && record.cachedAt && (now - record.cachedAt < PUBLIC_METADATA_TTL_MS)) {
      publicDetailCache.set(key, {
        name: String(record.name).trim(),
        cover_url: record.cover_url || "",
        track_count: Number(record.track_count) || 0,
        cachedAt: record.cachedAt,
      });
    } else {
      delete stored[key];
      changed = true;
    }
  }
  if (changed) {
    savePublicMetadataStorage(stored);
  }
}
initPublicDetailCacheFromStorage();

function setPublicDetailCache(key, entry, isFromNetwork = false) {
  const existing = publicDetailCache.get(key);
  const now = Date.now();
  if (entry.failed) {
    const failedAt = entry.failedAt || now;
    // 失败只保留在内存缓存中（30s 瞬时失败），绝不写入 localStorage 持久化
    publicDetailCache.set(key, { failed: true, failedAt });
    return;
  }
  const name = String(entry.name || "").trim();
  if (!name) return;
  const cachedAt = (isFromNetwork || !existing?.cachedAt) ? now : existing.cachedAt;
  const itemData = {
    name,
    cover_url: entry.cover_url || "",
    track_count: Number(entry.track_count) || 0,
    cachedAt,
  };
  publicDetailCache.set(key, itemData);

  // 仅在网络返回或新元数据时写入 localStorage，避免每次 render 频繁读写
  if (isFromNetwork || !existing) {
    const parts = key.split("|");
    const stored = loadPublicMetadataStorage();
    stored[key] = {
      provider: parts[0] || "",
      source: parts[1] || "",
      kind: parts[2] || "",
      id: parts[3] || "",
      name: itemData.name,
      cover_url: itemData.cover_url,
      track_count: itemData.track_count,
      cachedAt: itemData.cachedAt,
    };
    savePublicMetadataStorage(stored);
  }
}

function renderPublicSelected() {
  const box = $("#public-selected");
  if (!box) return;
  box.innerHTML = publicSelection.length ? publicSelection.map((item, index) => {
    // Unified cache entry preserves its original expiry when rendering.
    if (item._name) {
      const key = publicEntryKey(item);
      if (!publicDetailCache.has(key)) {
        setPublicDetailCache(key, {
          name: item._name, cover_url: item._cover_url || "", track_count: item._track_count || 0,
        });
      }
    }
    // 状态判定：idle / loading / success / error / unavailable
    // 只在 inflight 显示 loading，空 name 失败、缓存不当成功；兼容原有 _detail_failed 断言
    let title = "";
    if (item._name) {
      title = escapeHtml(item._name);
    } else if (item._detail_status === "stopped") {
      title = "音源未运行";
    } else if (item._detail_status === "unavailable") {
      title = "音源不可用";
    } else if (item._detail_status === "error" || item._detail_failed) {
      title = "名称获取失败";
    } else if (item._detail_status === "loading") {
      title = "正在获取歌单名称…";
    } else {
      title = "等待获取歌单名称";
    }
    const showRetry = item._detail_failed || ["error", "unavailable", "stopped"].includes(item._detail_status);
    return `
    <div class="public-selected">
      <img src="${escapeHtml(item._cover_url || "")}" alt="" ${item._cover_url ? "" : "hidden"}>
      <div class="min">
        <b>${title}</b>
        <span>${escapeHtml(PUBLIC_SOURCE_LABEL[item.source] || item.source)} · ${item.kind === "chart" ? "榜单" : "歌单"} · ${item.id}</span>
        ${item._reason ? `<small>${escapeHtml(item._reason)}</small>` : ""}
        ${Number(item._track_count || 0) > 1000 ? '<small>超过 1,000 首，仅载入前 1,000 首。</small>' : ""}
      </div>
      <div class="btn-row">
        ${showRetry ? `<button class="btn small" data-public-retry="${index}">重试</button>` : ""}
        <button class="btn small" data-public-up="${index}" ${index === 0 ? "disabled" : ""}>↑</button>
        <button class="btn small" data-public-down="${index}" ${index === publicSelection.length - 1 ? "disabled" : ""}>↓</button>
        <button class="btn small danger" data-public-remove="${index}">移除</button>
      </div>
    </div>
  `; }).join("") : '<p class="hint">尚未选择歌单。</p>';
  box.querySelectorAll("[data-public-up]").forEach((btn) =>
    btn.addEventListener("click", () => movePublicSelection(Number(btn.dataset.publicUp), -1)));
  box.querySelectorAll("[data-public-down]").forEach((btn) =>
    btn.addEventListener("click", () => movePublicSelection(Number(btn.dataset.publicDown), 1)));
  box.querySelectorAll("[data-public-retry]").forEach((btn) =>
    btn.addEventListener("click", () => retryPublicDetail(Number(btn.dataset.publicRetry))));
  box.querySelectorAll("[data-public-remove]").forEach((btn) =>
    btn.addEventListener("click", () => {
      publicSelection.splice(Number(btn.dataset.publicRemove), 1);
      stashPublicUser(true);
      void ensurePublicSelectionDetails();
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

async function publicApi(target, params = {}, retry = true, provider = publicState.provider) {
  // provider 显式传参：请求期间用户切换音源时，重试仍用发起时的音源，不会串台
  const query = new URLSearchParams({ provider, ...params });
  try {
    const data = await api(`/api/public-playlists/${target}?${query.toString()}`);
    if (data.ok === false) throw new Error(data.error || "歌单请求失败");
    return data;
  } catch (exc) {
    if (!retry) throw exc;
    if (!await startPreview(provider)) throw exc;
    return await publicApi(target, params, false, provider);
  }
}

function publicUiProviderOf(item) {
  return item.provider === "lx" ? "lxmusic" : "musicbox";
}

// 已选条目的展示元数据补齐：按条目自身的 provider 请求，不依赖当前浏览的音源；
// 配置只保存身份四字段，刷新/保存后靠这里把名称补回来。不经过 startPreview
// 重试——不为取名称单独拉起另一个音源的进程，失败交给手动重试。
async function fetchPublicItemDetail(item, signal) {
  const query = new URLSearchParams({
    provider: publicUiProviderOf(item), source: item.source, kind: item.kind,
  });
  const data = await api(`/api/public-playlists/${encodeURIComponent(item.id)}?${query.toString()}`, { signal });
  if (data.ok === false) throw new Error(data.error || "歌单详情请求失败");
  const detail = data.data || {};
  const name = typeof detail.name === "string" ? detail.name.trim() : "";
  if (!name) throw new Error("歌单名称为空");
  return { ...detail, name };
}

function applyPublicDetailCache(item) {
  const key = publicEntryKey(item);
  const cached = publicDetailCache.get(key);
  if (!cached) return false;
  if (cached.failed) {
    if (Date.now() - cached.failedAt < PUBLIC_FAILED_TTL_MS) {
      item._detail_failed = true;
      item._detail_status = "error";
      return true;
    }
    publicDetailCache.delete(key);
    const stored = loadPublicMetadataStorage();
    delete stored[key];
    savePublicMetadataStorage(stored);
    return false;
  }
  if (cached.cachedAt && (Date.now() - cached.cachedAt > PUBLIC_METADATA_TTL_MS)) {
    publicDetailCache.delete(key);
    const stored = loadPublicMetadataStorage();
    delete stored[key];
    savePublicMetadataStorage(stored);
    return false;
  }
  const name = String(cached.name || "").trim();
  if (!name) return false;
  item._name = name;
  item._cover_url = cached.cover_url || "";
  item._track_count = Number(cached.track_count) || 0;
  item._detail_failed = false;
  item._detail_status = "success";
  return true;
}

function abortActivePublicDetailRequests() {
  for (const ctrl of publicDetailAbortControllers) {
    try { ctrl.abort(); } catch (_) {}
  }
  publicDetailAbortControllers.clear();
  publicDetailInflight.clear();
  publicDetailRun = null;
}

async function ensurePublicSelectionDetails() {
  if (!publicSelectionEditable()) return;
  // Removing entries cancels only their own work. Browsing never cancels names.
  const keys = new Set(publicSelection.map(publicEntryKey));
  for (const ctrl of publicDetailAbortControllers) {
    if (!keys.has(ctrl.publicKey)) ctrl.abort();
  }
  const target = publicSelectionTarget();
  if (publicDetailRun && publicDetailRunTarget === target) return publicDetailRun;
  publicDetailRunTarget = target;
  const run = runPublicSelectionDetails();
  publicDetailRun = run;
  try {
    await run;
  } finally {
    if (publicDetailRun === run) {
      publicDetailRun = null;
      // Entries added while workers were busy join the next bounded queue.
      if (publicSelection.some((item) => !item._name && !item._detail_failed
          && !["error", "unavailable"].includes(item._detail_status))) {
        void ensurePublicSelectionDetails();
      }
    }
  }
}

async function runPublicSelectionDetails() {
  const seq = ++publicSelectionDetailSeq;
  const currentTarget = publicSelectionTarget();

  for (const item of publicSelection) {
    if (item._name) {
      item._detail_status = "success";
      item._detail_failed = false;
      continue;
    }
    if (!applyPublicDetailCache(item)) {
      const process = publicProcessSnapshot?.processes[publicUiProviderOf(item)];
      const stopped = publicProcessSnapshot && Date.now() - publicProcessSnapshot.at < 30000
        && ["STOPPED", "EXITED", "FATAL"].includes(process?.state);
      item._detail_status = stopped ? "stopped" : "idle";
      item._detail_failed = Boolean(stopped);
    }
  }
  renderPublicSelected();

  const toFetch = [];
  for (const item of publicSelection) {
    if (item._name || item._detail_failed || item._detail_status === "error" || item._detail_status === "unavailable") continue;
    toFetch.push(item);
  }
  if (!toFetch.length) return;

  let queueIndex = 0;
  let activeWorkers = 0;

  return new Promise((resolve) => {
    function scheduleNext() {
      if (seq !== publicSelectionDetailSeq || publicSelectionTarget() !== currentTarget) {
        resolve();
        return;
      }
      if (queueIndex >= toFetch.length && activeWorkers === 0) {
        resolve();
        return;
      }
      while (activeWorkers < MAX_PUBLIC_DETAIL_CONCURRENCY && queueIndex < toFetch.length) {
        if (seq !== publicSelectionDetailSeq || publicSelectionTarget() !== currentTarget) {
          resolve();
          return;
        }
        const item = toFetch[queueIndex++];
        const key = publicEntryKey(item);
        if (!publicSelection.includes(item)) continue;

        if (publicDetailInflight.has(key)) {
          activeWorkers++;
          item._detail_status = "loading";
          renderPublicSelected();
          publicDetailInflight.get(key).then(() => {
            activeWorkers--;
            if (seq === publicSelectionDetailSeq && publicSelectionTarget() === currentTarget) {
              for (const it of publicSelection) {
                if (publicEntryKey(it) === key) applyPublicDetailCache(it);
              }
              renderPublicSelected();
            }
            scheduleNext();
          }).catch(() => {
            activeWorkers--;
            if (seq === publicSelectionDetailSeq && publicSelectionTarget() === currentTarget) {
              for (const it of publicSelection) {
                if (publicEntryKey(it) === key) applyPublicDetailCache(it);
              }
              renderPublicSelected();
            }
            scheduleNext();
          });
          continue;
        }

        activeWorkers++;
        item._detail_status = "loading";
        renderPublicSelected();

        const controller = new AbortController();
        controller.publicKey = key;
        let unavailable = false;
        publicDetailAbortControllers.add(controller);
        const timer = setTimeout(() => {
          try { controller.abort(); } catch (_) {}
        }, 10000);
        if (timer && typeof timer.unref === "function") timer.unref();

        const fetchPromise = (async () => {
          try {
            const detail = await fetchPublicItemDetail(item, controller.signal);
            if (seq !== publicSelectionDetailSeq || publicSelectionTarget() !== currentTarget
                || !publicSelection.includes(item)) return;
            setPublicDetailCache(key, {
              name: detail.name || "",
              cover_url: detail.cover_url || "",
              track_count: detail.track_count || 0,
            }, true);
          } catch (exc) {
            if (seq !== publicSelectionDetailSeq || publicSelectionTarget() !== currentTarget) return;
            if (!publicSelection.includes(item)) return;
            setPublicDetailCache(key, { failed: true, failedAt: Date.now() });
            unavailable = exc.status === 503 && /未运行|not.running|not.started/i.test(exc.message);
          } finally {
            clearTimeout(timer);
            publicDetailAbortControllers.delete(controller);
            if (publicDetailInflight.get(key) === fetchPromise) publicDetailInflight.delete(key);
            activeWorkers--;
            if (seq === publicSelectionDetailSeq && publicSelectionTarget() === currentTarget) {
              for (const it of publicSelection) {
                if (publicEntryKey(it) === key) applyPublicDetailCache(it);
              }
              if (unavailable) item._detail_status = "unavailable";
              renderPublicSelected();
            }
            scheduleNext();
          }
        })();

        publicDetailInflight.set(key, fetchPromise);
      }
    }

    scheduleNext();
  });
}

async function retryPublicDetail(index) {
  const item = publicSelection[index];
  if (!item) return;
  const key = publicEntryKey(item);
  publicDetailCache.delete(key);
  try {
    const stored = loadPublicMetadataStorage();
    delete stored[key];
    savePublicMetadataStorage(stored);
  } catch (_) {}
  delete item._detail_failed;
  delete item._cap_retried;
  item._detail_status = "idle";
  renderPublicSelected();
  // explicit retry 可通过现有 startPreview 拉起 item provider 再重试，自动补名不启动预览
  const uiProvider = publicUiProviderOf(item);
  try {
    await startPreview(uiProvider);
  } catch (_) {}
  publicProcessSnapshot = null;
  delete item._detail_failed;
  item._detail_status = "idle";
  await ensurePublicSelectionDetails();
}

function annotatePublicSelectionAvailability() {
  const browsingProvider = publicState.provider;
  for (const item of publicSelection) {
    if (publicUiProviderOf(item) !== browsingProvider) continue;
    const cap = ((publicCapabilities || {}).sources || []).find((source) => source.id === item.source);
    item._reason = cap && !cap.playback_available ? cap.reason || "当前平台没有播放能力" : "";
    if (cap && !cap.playback_available) {
      item._detail_status = "unavailable";
      item._detail_failed = true;
    }
  }
}

function currentUiProvider() {
  return ($$("input[name=provider]").find((el) => el.checked) || {}).value || savedProvider();
}

async function loadPublicCapabilities(seq = publicState.seq) {
  const provider = publicState.provider;
  try {
    const data = await publicApi("capabilities", {}, true, provider);
    if (seq !== publicState.seq || provider !== publicState.provider) return;
    publicCapabilities = data;
    annotatePublicSelectionAvailability();
    // 音源就绪时自动恢复先于预览启动的失败条目，但通过 _cap_retried 防止后续 capabilities 刷新反复清失败
    for (const item of publicSelection) {
      if (publicUiProviderOf(item) !== provider) continue;
      if (item._cap_retried) continue;
      const key = publicEntryKey(item);
      if (publicDetailCache.get(key)?.failed) {
        publicDetailCache.delete(key);
        delete item._detail_failed;
        item._detail_status = "idle";
        item._cap_retried = true;
      }
    }
  } catch (exc) {
    if (seq !== publicState.seq || provider !== publicState.provider) return;
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

  const isNetease = publicState.provider === "netease" || publicState.provider === "musicbox";
  const channels = isNetease ? [
    { id: "charts", name: "排行榜" }, { id: "recommend", name: "推荐歌单" },
    { id: "top", name: "热门与分类" }, { id: "highquality", name: "精品歌单" },
    { id: "account_recommend", name: "账号推荐歌单" },
  ] : [{ id: "top", name: "热门与分类" }, { id: "charts", name: "排行榜" }];
  const logged = !!(((publicCapabilities || {}).sources || []).find((item) => item.id === "netease") || {}).logged_in;
  channelSelect.innerHTML = channels.filter((item) =>
    item.id !== "account_recommend" || logged).map((item) =>
    `<option value="${item.id}">${item.name}</option>`).join("");
  if (![...channelSelect.options].some((opt) => opt.value === publicState.channel)) {
    publicState.channel = isNetease ? "charts" : "top";
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
  publicState.hasSorts = sorts.length > 0;
  $("#public-sort").hidden = !sorts.length || publicSearchActive();
  $("#public-sort").innerHTML = sorts.map((item) =>
    `<option value="${escapeHtml(String(item.id))}">${escapeHtml(item.name || item.id)}</option>`).join("");
  publicState.sort = sorts[0]?.id || "";
}

async function loadPublicCategories(seq = publicState.seq) {
  const { provider, source, channel } = publicState;
  if (seq !== publicState.seq) return;
  if (channel === "recommend" || channel === "account_recommend") {
    fillPublicCategories({ categories: [] });
    return;
  }
  try {
    const highquality = (provider === "netease" || provider === "musicbox") && channel === "highquality";
    const data = await publicApi("categories", {
      source, kind: channel === "charts" ? "chart" : "playlist",
    }, true, provider);
    if (seq !== publicState.seq) return;
    fillPublicCategories(highquality ? { categories: data.highquality_tags || data.tags || [] } : data);
  } catch (exc) {
    if (seq !== publicState.seq) return;
    fillPublicCategories({ categories: [] });
    $("#public-note").textContent = "分类加载失败：" + exc.message;
  }
}

// 必须在 capabilities/categories 的 await 之前失效旧请求并阻止翻页。
function invalidatePublicBrowse() {
  const seq = ++publicState.seq;
  publicState.loading = true;
  publicState.bulk = null;
  publicState.items = []; publicState.hasMore = false;
  renderPublicList(); renderPublicPage();
  return seq;
}

async function reloadPublicBrowser(withCapabilities = false) {
  const seq = invalidatePublicBrowse();
  void ensurePublicSelectionDetails();
  if (withCapabilities) {
    await loadPublicCapabilities(seq);
    if (seq !== publicState.seq) return;
  }
  await loadPublicCategories(seq);
  if (seq !== publicState.seq) return;
  await loadPublicList(seq);
  if (seq !== publicState.seq) return;
  annotatePublicSelectionAvailability();
  renderPublicSelected();
  void ensurePublicSelectionDetails();
  return seq;
}

function publicSearchActive() {
  return !!publicState.keyword;
}

// 只有按上游分页推进的入口才提供"加载剩余"；排行榜/推荐是一次性有限目录
function paginatedPublicChannel() {
  return publicSearchActive() || publicState.channel === "top" || publicState.channel === "highquality";
}

function publicListRequestParams(page, state = publicState) {
  const params = {
    source: state.source,
    channel: state.keyword ? "search" : state.channel,
    keyword: state.keyword,
    category: state.category, sort: state.sort, page,
  };
  if (!state.keyword && state.channel === "highquality") {
    params.cursor = (state.cursors || {})[page] || "";
  }
  return params;
}

function publicItemsFromResponse(data, state) {
  return (data.items || []).map((item) => ({
    ...item,
    provider: state.provider === "musicbox" ? "musicbox" : "lx",
    source: state.source,
    kind: !state.keyword && state.channel === "charts" ? "chart" : "playlist",
  }));
}

function publicRawCount(data) {
  return Math.max((data.items || []).length, Number(data.raw_count) || 0);
}

function syncPublicSearchUi() {
  const searching = publicSearchActive();
  const clearBtn = $("#public-search-clear");
  if (clearBtn) clearBtn.hidden = !searching;
  const category = $("#public-category");
  if (category) category.hidden = searching;
  const sort = $("#public-sort");
  if (sort) sort.hidden = searching || !publicState.hasSorts;
}

function updatePublicNote() {
  const note = $("#public-note");
  if (!note || publicState.bulk) return; // 批量加载的进度文案由加载循环维护
  const suffix = publicState.total ? `，接口报告 ${publicState.total} 份` : "";
  note.textContent = publicSearchActive()
    ? `搜索“${publicState.keyword}”：返回 ${publicState.items.length} 份${suffix}。`
    : `返回 ${publicState.items.length} 份${suffix}；勾选后保存生效。`;
}

async function loadPublicList(seq = invalidatePublicBrowse()) {
  if (seq !== publicState.seq) return;
  const state = { ...publicState };
  const params = publicListRequestParams(state.page, state);
  syncPublicSearchUi();
  $("#public-note").textContent = state.keyword ? `正在搜索“${state.keyword}”…` : "正在加载歌单…";
  try {
    const data = await publicApi("", params, true, state.provider);
    if (seq !== publicState.seq) return; // 请求期间条件已变化，丢弃过期响应
    publicState.items = publicItemsFromResponse(data, state);
    publicState.hasMore = !!data.has_more && publicRawCount(data) > 0;
    publicState.total = Number(data.total || 0);
    publicState.cursor = data.next_cursor || "";
    if (params.channel === "highquality") {
      publicState.cursors = publicState.cursors || { 1: "" };
      publicState.cursors[state.page + 1] = data.next_cursor || "";
      if (!data.next_cursor || data.next_cursor === params.cursor) publicState.hasMore = false;
    }
    updatePublicNote();
  } catch (exc) {
    if (seq !== publicState.seq) return;
    publicState.items = []; publicState.hasMore = false;
    $("#public-note").textContent = (state.keyword ? "搜索失败：" : "歌单加载失败：") + exc.message;
  } finally {
    if (seq === publicState.seq) {
      publicState.loading = false;
      renderPublicList(); renderPublicPage();
    }
  }
}

// 单次"加载剩余"连续翻页的安全上限：按上游真实尽头推进，但避免无界请求串；
// 触顶后按钮仍在，可再次点击继续
const PUBLIC_BULK_MAX_PAGES = 500;

async function loadPublicRest() {
  if (publicState.loading) return;
  if (publicState.bulk?.active) { publicState.bulk.abort = true; return; }
  if (!publicState.hasMore || !paginatedPublicChannel()) return;
  const seq = publicState.seq;
  const state = { ...publicState, cursors: { ...publicState.cursors } };
  const bulk = {
    active: true, abort: false, basePage: publicState.bulk?.basePage || state.page, pages: 0,
    seen: new Set(publicState.items.map((it) => publicEntryKey(it))),
  };
  publicState.bulk = bulk;
  renderPublicPage();
  $("#public-note").textContent = "正在加载剩余歌单…";
  let stopReason = "";
  try {
    while (!bulk.abort && bulk.pages < PUBLIC_BULK_MAX_PAGES) {
      const params = publicListRequestParams(state.page + 1, state);
      const data = await publicApi("", params, true, state.provider);
      if (seq !== publicState.seq || publicState.bulk !== bulk) return; // 条件已切换
      state.page += 1;
      publicState.page = state.page;
      bulk.pages += 1;
      const rows = publicItemsFromResponse(data, state);
      let added = 0;
      for (const row of rows) {
        const key = publicEntryKey(row);
        if (bulk.seen.has(key)) continue;
        bulk.seen.add(key);
        publicState.items.push(row);
        added += 1;
      }
      publicState.hasMore = !!data.has_more;
      publicState.total = Number(data.total || 0);
      if (params.channel === "highquality") {
        state.cursors[state.page + 1] = data.next_cursor || "";
        publicState.cursors = { ...state.cursors };
        if (!data.next_cursor || data.next_cursor === params.cursor) publicState.hasMore = false;
      }
      renderPublicList();
      renderPublicPage();
      $("#public-note").textContent = `正在加载剩余歌单：已累计 ${publicState.items.length} 份（第 ${bulk.basePage}–${publicState.page} 页）…`;
      if (!publicState.hasMore) { stopReason = "done"; break; }
      if (!publicRawCount(data) || (rows.length && added === 0)) {
        // 真空页或整页重复才判停；非空原始窗口被过滤后仍按原生分页推进。
        publicState.hasMore = false;
        stopReason = "stalled"; break;
      }
    }
    if (bulk.abort) stopReason = "stopped";
    else if (!stopReason && bulk.pages >= PUBLIC_BULK_MAX_PAGES) stopReason = "capped";
  } catch (exc) {
    if (seq === publicState.seq && publicState.bulk === bulk) {
      // 失败≠取尽：页码在成功响应后才推进，重试会重新请求失败的那一页
      publicState.hasMore = true;
      $("#public-note").textContent = `加载中断：${exc.message}。已累计 ${publicState.items.length} 份，可点击“加载剩余歌单”重试。`;
    }
  } finally {
    if (publicState.bulk === bulk) {
      bulk.active = false;
      renderPublicPage();
      if (stopReason === "done") {
        $("#public-note").textContent = `已加载到尽头：第 ${bulk.basePage}–${publicState.page} 页共 ${publicState.items.length} 份${publicState.total ? `（接口报告 ${publicState.total} 份）` : ""}。`;
      } else if (stopReason === "stalled") {
        $("#public-note").textContent = `上游不再返回新歌单：已加载 ${publicState.items.length} 份（第 ${bulk.basePage}–${publicState.page} 页）。`;
      } else if (stopReason === "capped") {
        $("#public-note").textContent = `单次连续加载已达 ${PUBLIC_BULK_MAX_PAGES} 页：目前共 ${publicState.items.length} 份，如需继续可再次点击。`;
      } else if (stopReason === "stopped") {
        $("#public-note").textContent = `已停止加载：当前显示第 ${bulk.basePage}–${publicState.page} 页共 ${publicState.items.length} 份。`;
      }
    }
  }
}

function renderPublicPage() {
  $("#public-page").textContent = publicState.bulk
    ? `第 ${publicState.bulk.basePage}–${publicState.page} 页`
    : String(publicState.page);
  const busy = publicState.loading || !!publicState.bulk?.active;
  $("#public-prev").disabled = publicState.page <= 1 || busy;
  $("#public-next").disabled = !publicState.hasMore || busy;
  const loadRest = $("#public-load-rest");
  if (loadRest) {
    const bulkBusy = !!publicState.bulk?.active;
    loadRest.hidden = publicState.loading || !(paginatedPublicChannel() && (publicState.hasMore || bulkBusy));
    loadRest.disabled = !!publicState.loading;
    loadRest.textContent = bulkBusy ? "停止加载" : "加载剩余歌单";
  }
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
      if (row) publicSelection.push({
        provider: "lx", source: target.source, kind: "chart", id: String(row.id),
        _name: row.name || "", _cover_url: row.cover_url || "", _track_count: row.track_count || 0,
      });
    } catch (_) { /* 平台不可用则跳过 */ }
  }
  renderPublicSelected();
  markDirty("已按可用平台填入默认洛雪榜单");
}

async function showPublicBrowser() {
  if (!publicSelectionEditable()) {
    renderMusicUsers();
    return;
  }
  $("#public-playlists-browser").hidden = false;
  const saved = savedProvider();
  publicState.provider = currentUiProvider() === "lxmusic" ? "lxmusic" : "musicbox";
  const seq = await reloadPublicBrowser(true);
  if (seq === publicState.seq && saved === "musicdl") $("#public-note").textContent = "当前音源是 musicdl：它没有统一推荐歌单接口；可浏览网易/洛雪，保存后按常驻规则展示网易公共歌单。";
}

$("#public-playlists-sync-all").addEventListener("change", async () => {
  const nextSyncAll = $("#public-playlists-sync-all").checked;
  stashPublicUser();
  invalidatePublicBrowse();
  publicSelectionDetailSeq++;
  abortActivePublicDetailRequests();
  publicSyncAll = nextSyncAll;
  $("#public-playlists-sync-all").checked = publicSyncAll;
  publicSelection = publicSyncAll
    ? publicSharedSelection
    : (publicSelectionMap[publicUserGuid] || []);
  renderMusicUsers();
  renderPublicSelected();
  updateResidentAvailability();
  markDirty(publicSyncAll ? "公共歌单已切换为全部用户共享" : "公共歌单已切换为按用户配置");
  if (!$("#public-playlists-enabled").checked) return;
  if (!publicSyncAll && !publicUsersReady) await loadMusicUsers();
  if (publicSelectionEditable()) await showPublicBrowser();
});

$("#public-playlists-enabled").addEventListener("change", async () => {
  const enabled = $("#public-playlists-enabled").checked;
  updateResidentAvailability();
  markDirty(enabled ? "公共歌单已开启" : "公共歌单已关闭");
  renderMusicUsers();
  if (!enabled) {
    invalidatePublicBrowse();
    publicState.loading = false;
    publicSelectionDetailSeq++;
    abortActivePublicDetailRequests();
    $("#public-playlists-browser").hidden = true;
    return;
  }
  if (configSchema.FNMUSIC_PUBLIC_PLAYLISTS_BY_USER) {
    if (!publicSyncAll && !publicUsersReady) await loadMusicUsers();
  } else if (!publicSelection.length && !configPresence.FNMUSIC_PUBLIC_PLAYLISTS_SELECTION) await initPublicDefaults();
  await showPublicBrowser();
});
function runPublicSearch() {
  const keyword = $("#public-keyword").value.trim();
  if (!keyword) return clearPublicSearch();
  publicState.keyword = keyword;
  publicState.page = 1;
  return loadPublicList();
}

function clearPublicSearch() {
  publicState.keyword = "";
  $("#public-keyword").value = "";
  publicState.page = 1;
  publicState.cursors = { 1: "" }; publicState.cursor = "";
  return loadPublicList();
}

$("#public-search").addEventListener("click", runPublicSearch);
$("#public-keyword").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") { ev.preventDefault(); runPublicSearch(); }
});
$("#public-search-clear").addEventListener("click", clearPublicSearch);
// 浏览条件（音源/入口/平台/分类/排序/关键词/翻页）只影响浏览视图，不改保存的配置
$("#public-provider").addEventListener("change", async () => {
  publicState.provider = $("#public-provider").value;
  publicState.page = 1; publicState.channel = publicState.provider === "musicbox" ? "charts" : "top";
  await reloadPublicBrowser(true);
});
$("#public-channel").addEventListener("change", async () => {
  publicState.channel = $("#public-channel").value; publicState.page = 1;
  await reloadPublicBrowser();
});
$("#public-source").addEventListener("change", async () => {
  publicState.source = $("#public-source").value; publicState.page = 1;
  await reloadPublicBrowser();
});
$("#public-category").addEventListener("change", async () => {
  publicState.category = $("#public-category").value; publicState.page = 1;
  publicState.cursors = { 1: "" }; publicState.cursor = "";
  await loadPublicList();
});
$("#public-sort").addEventListener("change", async () => {
  publicState.sort = $("#public-sort").value; publicState.page = 1;
  await loadPublicList();
});
$("#public-refresh").addEventListener("click", async () => {
  publicState.page = 1; // 刷新=从头重新浏览，与其它条件切换一致
  await reloadPublicBrowser(true);
});
$("#public-load-rest").addEventListener("click", () => { void loadPublicRest(); });
$("#public-prev").addEventListener("click", async () => {
  if (publicState.page > 1 && !publicState.loading && !publicState.bulk?.active) {
    // 累计视图离开后，loadPublicList 清除范围并恢复目标页的普通单页视图。
    publicState.page--; await loadPublicList();
  }
});
$("#public-next").addEventListener("click", async () => {
  if (publicState.hasMore && !publicState.loading && !publicState.bulk?.active) {
    publicState.page++; await loadPublicList();
  }
});

/* -------------------------------------------------------------- 网易扫码 */
let neteaseLoggedIn = false;

function updateResidentAvailability() {
  // 账号歌单仍需登录；公共网易歌单可匿名常驻，因此仅在两者都无需求时置灰。
  const publicNeedsMusicbox = $("#public-playlists-enabled").checked
    && anyPublicMusicboxSelection();
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
  const publicNeedsMusicbox = anyPublicMusicboxSelection();
  if ($("#netease-playlist-resident").checked && !$("#netease-my-playlists").checked && !publicNeedsMusicbox) {
    $("#netease-my-playlists").checked = true;
  }
});

/* -------------------------------------------------------------- lx 源测试 */
function escapeHtml(str) {
  return String(str ?? "")
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

function lxCpuAlertFromSource(source) {
  if (!source || typeof source !== "object") return null;
  const usage = source.resource_usage;
  if (!usage || typeof usage !== "object" || usage.abnormal !== true) return null;
  const sourceId = String(source.id || "").trim();
  if (!sourceId) return null;
  const finite = (value) => Number.isFinite(Number(value)) ? Number(value) : null;
  return {
    sourceId,
    name: String(source.name || sourceId),
    runtimeId: String(usage.runtime_id || source.runtime_id || ""),
    cpuPercent: finite(usage.cpu_percent),
    thresholdPercent: finite(usage.threshold_percent) ?? 50,
    requiredDurationS: finite(usage.required_duration_s) ?? 180,
    highDurationS: finite(usage.high_duration_s),
    monitoringStatus: String(usage.monitoring_status || "unknown"),
    detectedAt: String(usage.detected_at || ""),
    processState: String(source.process_state || ""),
  };
}

function syncLxCpuAlerts(sources) {
  if (!Array.isArray(sources)) return; // 状态不可读时不能据空列表清除历史告警。
  for (const source of sources) {
    if (!source || typeof source !== "object") continue;
    const sourceId = String(source.id || "").trim();
    if (!sourceId) continue;
    const alert = lxCpuAlertFromSource(source);
    if (alert) {
      lxCpuAlerts.set(sourceId, alert);
      continue;
    }
    const cached = lxCpuAlerts.get(sourceId);
    if (!cached) continue;
    const usage = source.resource_usage;
    const runtimeId = String((usage && usage.runtime_id) || source.runtime_id || "");
    if (String((usage && usage.monitoring_status) || "") === "stopped"
        || ["stopped", "disabled"].includes(String(source.process_state || ""))) {
      lxCpuAlerts.delete(sourceId);
      continue;
    }
    // 进程实例变化即重新计时；同一实例只认后端锁定状态，不能因短暂低载清除。
    if (cached.runtimeId && runtimeId && runtimeId !== cached.runtimeId) {
      lxCpuAlerts.delete(sourceId);
    }
  }
  // 当前快照缺少某源时不能确认是停用还是状态接口暂时不完整；保留红标，
  // 等成功停用操作、显式 stopped 状态或新 runtime_id 再清除。
}

function findLxCpuAlert(item) {
  const sourceId = lxStableSourceId(item);
  return sourceId ? (lxCpuAlerts.get(sourceId) || null) : null;
}

function lxCpuReadingText(alert) {
  if (!alert) return "未知";
  if (lxRuntimeStatusUnknown || alert.cpuPercent == null
      || alert.monitoringStatus === "unknown") return "未知（当前读数不可用）";
  return `${alert.cpuPercent.toFixed(1)}%（单核口径）`;
}

function lxCpuAlertHtml(item) {
  const alert = findLxCpuAlert(item);
  if (!alert) return "";
  const name = escapeHtml(item.name || lxDefaultName(item.url));
  return `<button class="lx-cpu-alert" type="button" data-cpu-source-id="${escapeHtml(alert.sourceId)}"
      aria-label="资源异常占用：${name}，查看判定说明" title="资源异常占用">!</button>
    <span class="lx-cpu-alert-label">资源异常占用</span>`;
}

function clearLxCpuAlertsAfterSavedConfig(result) {
  const actions = Array.isArray(result && result.actions) ? result.actions : [];
  if (!actions.length || !lxCpuAlerts.size) return;
  const runtimeSources = Array.isArray(lxRuntimeSnapshot && lxRuntimeSnapshot.sources)
    ? lxRuntimeSnapshot.sources : [];
  const idForUrl = (url) => {
    const raw = String(url || "");
    if (!raw) return "";
    const local = lxSourceList.find((item) => item && String(item.url || "") === raw);
    const localId = local ? lxStableSourceId(local) : "";
    if (localId) return localId;
    const matches = runtimeSources.filter((source) => source && source.id
      && String(source.url || "") === raw);
    return matches.length === 1 ? String(matches[0].id) : "";
  };
  for (const action of actions) {
    if (!action || action.kind !== "lx_activate" || action.op !== "deactivate" || action.ok !== true) continue;
    const sourceId = String(action.source_id || action.id || "").trim() || idForUrl(action.url);
    if (sourceId) lxCpuAlerts.delete(sourceId);
  }
  renderLxSourceList();
}

function lxCpuDetectedText(value) {
  const text = String(value || "");
  if (!text) return "未知";
  const date = new Date(text);
  return Number.isNaN(date.getTime()) ? text : date.toLocaleString();
}

function fillLxCpuHelp(alert) {
  const body = $("#help-body");
  if (!body || !alert || typeof body.querySelectorAll !== "function") return;
  const values = {
    source: alert.name,
    reading: lxCpuReadingText(alert),
    threshold: `${alert.thresholdPercent}%`,
    required: `${alert.requiredDurationS} 秒（5 秒统计窗口）`,
    high: alert.highDurationS == null ? "未知" : `${alert.highDurationS} 秒`,
    detected: lxCpuDetectedText(alert.detectedAt),
    runtime: alert.runtimeId || "未知",
    process: alert.processState || "未知",
  };
  body.querySelectorAll("[data-cpu-help]").forEach((node) => {
    const key = node.getAttribute("data-cpu-help");
    if (key && Object.prototype.hasOwnProperty.call(values, key)) {
      node.textContent = String(values[key]);
    }
  });
}

function openLxCpuHelp(item, trigger) {
  const alert = findLxCpuAlert(item);
  if (!alert) return;
  openHelp("lx-source-cpu", trigger);
  lxCpuHelpSourceId = alert.sourceId;
  $("#help-title").textContent = "资源异常占用";
  fillLxCpuHelp(alert);
}

function findLxRuntimeSource(item, sources) {
  const list = Array.isArray(sources) ? sources : [];
  const itemId = String((item && (item.id || item.source_id)) || "").trim();
  if (itemId) {
    const byId = list.find((s) => s && String(s.id || "") === itemId);
    if (byId) return byId;
  }
  const itemUrl = (item && item.url) || "";
  if (itemUrl) {
    const exact = list.find((s) => s && s.url && s.url === itemUrl);
    if (exact) return exact;
  }
  if (itemUrl.toLowerCase().startsWith("file://")) {
    let base = itemUrl.replace(/\\/g, "/").split("/").pop() || "";
    try { base = decodeURIComponent(base); } catch (_) { /* 忽略 */ }
    const stem = base.replace(/\.js$/i, "");
    const byFile = list.filter((s) => s && (
      (base && (s.id === base || s.name === base))
      || (stem && (s.id === stem || s.name === stem))
    ));
    if (byFile.length) return byFile.length === 1 ? byFile[0] : undefined;
    const byName = list.filter((s) => s && item.name && (s.name === item.name || s.id === item.name));
    if (byName.length === 1) return byName[0];
  }
  return undefined;
}

// 只用持久化 source.id、准确 URL 或本地上传文件 ID 建立排序身份，不按显示名称猜测。
function lxStableSourceId(item) {
  if (item && typeof item.id === "string" && item.id.trim()) return item.id.trim();
  if (item && typeof item.source_id === "string" && item.source_id.trim()) return item.source_id.trim();
  const sources = Array.isArray(lxRuntimeSnapshot?.sources) ? lxRuntimeSnapshot.sources : [];
  const url = String(item?.url || "");
  const exactUrl = sources.filter((source) => source && source.url === url && source.id);
  if (exactUrl.length === 1) return String(exactUrl[0].id);
  if (url.toLowerCase().startsWith("file://")) {
    let filename = url.replace(/\\/g, "/").split("/").pop() || "";
    try { filename = decodeURIComponent(filename); } catch (_) { /* 保留原始路径尾段 */ }
    const ids = sources.filter((source) => source && source.id === filename).map((source) => String(source.id));
    if (ids.length === 1) return ids[0];
  }
  return "";
}

function syncLxSourceOrder() {
  lxSourceList.forEach((item) => {
    const id = lxStableSourceId(item);
    if (id && !item.id) {
      item.id = id;
      lxSourceListDirty = true;
    }
    if (id && item.active && !lxSourceOrder.includes(id)) lxSourceOrder.push(id);
  });
}

function getLxSourceOrder() {
  return lxSourceOrder.slice();
}

function activeLxOrderItems() {
  return lxSourceList.filter((item) => item && item.active).map((item, index) => ({
    item, index, id: lxStableSourceId(item),
  })).sort((left, right) => {
    const leftPosition = left.id ? lxSourceOrder.indexOf(left.id) : -1;
    const rightPosition = right.id ? lxSourceOrder.indexOf(right.id) : -1;
    if (leftPosition >= 0 && rightPosition >= 0) return leftPosition - rightPosition;
    return left.index - right.index;
  });
}

function canReorderLxSources() {
  if (!$("#lx-auto-switch").checked || !configSchema.FNMUSIC_LX_AUTO_SWITCH
      || !configSchema.FNMUSIC_LX_SOURCE_ORDER) return false;
  const ids = activeLxOrderItems().map((entry) => entry.id);
  return ids.every(Boolean) && new Set(ids).size === ids.length;
}

function updateLxAutoSwitchSettings() {
  const autoSwitch = $("#lx-auto-switch");
  const preferredRow = $("#lx-platform-preferred-row");
  const preferred = $("#lx-platform-preferred");
  const orderCard = $("#lx-source-order-card");
  const enabled = Boolean(configSchema.FNMUSIC_LX_AUTO_SWITCH && autoSwitch.checked);
  preferredRow.hidden = !enabled;
  preferred.disabled = !configSchema.FNMUSIC_LX_PLATFORM_PREFERRED || !enabled;
  const reorderable = canReorderLxSources();
  orderCard.classList.toggle("is-disabled", !reorderable);
  orderCard.setAttribute("aria-disabled", String(!reorderable));
  renderLxSourceOrder();
}

function reorderLxSourceTo(sourceId, targetIndex) {
  if (!canReorderLxSources()) return false;
  const visibleIds = activeLxOrderItems().map((entry) => entry.id);
  const oldIndex = visibleIds.indexOf(sourceId);
  if (oldIndex < 0 || targetIndex < 0 || targetIndex >= visibleIds.length || oldIndex === targetIndex) return false;
  visibleIds.splice(targetIndex, 0, visibleIds.splice(oldIndex, 1)[0]);
  const activeIds = new Set(visibleIds);
  let pointer = 0;
  const next = lxSourceOrder.map((id) => activeIds.has(id) ? visibleIds[pointer++] : id);
  while (pointer < visibleIds.length) next.push(visibleIds[pointer++]);
  if (next.every((id, index) => id === lxSourceOrder[index]) && next.length === lxSourceOrder.length) return false;
  lxSourceOrder = next;
  renderLxSourceOrder();
  markDirty("洛雪源顺序已修改，保存后生效");
  return true;
}

function moveLxSource(sourceId, offset) {
  if (!canReorderLxSources()) return false;
  const visibleIds = activeLxOrderItems().map((entry) => entry.id);
  const index = visibleIds.indexOf(sourceId);
  if (index < 0) return false;
  return reorderLxSourceTo(sourceId, index + offset);
}

let draggingLxSourceId = "";

function renderLxSourceOrder() {
  const container = $("#lx-source-order-list");
  const note = $("#lx-source-order-note");
  if (!container || !note) return;
  syncLxSourceOrder();
  const rows = activeLxOrderItems();
  const reorderable = canReorderLxSources();
  if (!rows.length) {
    container.innerHTML = '<span class="muted">暂无已激活源</span>';
    note.textContent = "";
    return;
  }
  container.innerHTML = rows.map(({ item, id }, index) => {
    const label = escapeHtml(item.name || lxDefaultName(item.url));
    const disabled = !reorderable || !id;
    const idText = id ? "" : "（待同步稳定 ID）";
    return `<div class="lx-order-item" role="listitem" draggable="${disabled ? "false" : "true"}"
        tabindex="${disabled ? "-1" : "0"}" aria-disabled="${disabled}" data-source-id="${escapeHtml(id)}">
      <span class="lx-order-handle" aria-hidden="true">⋮⋮</span>
      <span class="lx-order-name">${label}${idText}</span>
      <div class="lx-order-buttons">
        <button class="btn small lx-order-up" type="button" data-source-id="${escapeHtml(id)}" aria-label="${label}上移" ${disabled || index === 0 ? "disabled" : ""}>上移</button>
        <button class="btn small lx-order-down" type="button" data-source-id="${escapeHtml(id)}" aria-label="${label}下移" ${disabled || index === rows.length - 1 ? "disabled" : ""}>下移</button>
      </div>
    </div>`;
  }).join("");
  if (!$("#lx-auto-switch").checked) {
    note.textContent = "自动切换已关闭，排序已禁用；已保存顺序不会清空。";
  } else if (rows.some((row) => !row.id)) {
    note.textContent = "部分旧源还没有稳定 ID。切到洛雪主音源后，WebUI 会按已验证 URL 或上传文件身份同步；不会按名称猜测。";
  } else {
    note.textContent = "可拖动排序，也可用上移/下移按钮操作。";
  }

  container.querySelectorAll(".lx-order-up").forEach((button) => {
    button.addEventListener("click", () => moveLxSource(button.dataset.sourceId, -1));
  });
  container.querySelectorAll(".lx-order-down").forEach((button) => {
    button.addEventListener("click", () => moveLxSource(button.dataset.sourceId, 1));
  });
  container.querySelectorAll(".lx-order-item").forEach((row) => {
    row.addEventListener("dragstart", (event) => {
      if (!canReorderLxSources()) { event.preventDefault(); return; }
      draggingLxSourceId = row.dataset.sourceId || "";
      if (event.dataTransfer) event.dataTransfer.setData("text/plain", draggingLxSourceId);
    });
    row.addEventListener("touchstart", (event) => {
      if (!canReorderLxSources()) event.preventDefault();
    }, { passive: false });
    row.addEventListener("dragover", (event) => {
      if (canReorderLxSources()) event.preventDefault();
    });
    row.addEventListener("drop", (event) => {
      if (!canReorderLxSources()) return;
      event.preventDefault();
      const sourceId = (event.dataTransfer && event.dataTransfer.getData("text/plain")) || draggingLxSourceId;
      const targetIndex = activeLxOrderItems().findIndex((entry) => entry.id === row.dataset.sourceId);
      reorderLxSourceTo(sourceId, targetIndex);
      draggingLxSourceId = "";
    });
    row.addEventListener("dragend", () => { draggingLxSourceId = ""; });
    row.addEventListener("keydown", (event) => {
      if (!canReorderLxSources()) {
        if (event.key.startsWith("Arrow")) event.preventDefault();
        return;
      }
      if (event.key === "ArrowUp" || event.key === "ArrowDown") {
        event.preventDefault();
        moveLxSource(row.dataset.sourceId, event.key === "ArrowUp" ? -1 : 1);
      }
    });
  });
}

function renderLxSourceList() {
  const container = $("#lx-source-list");
  if (!container) return;

  if (!lxSourceList.length) {
    container.innerHTML = `<span class="muted pad">暂无洛雪音乐源，上传 .js 或填写脚本地址后点击「添加」加入列表</span>`;
    return;
  }

  const saved = lxSavedActiveUrls();
  container.innerHTML = lxSourceList.map((item, idx) => {
    const isActive = !!item.active;
    const wasActive = saved.has(item.url);
    const configLabel = isActive !== wasActive
      ? (isActive ? "待保存激活" : "待保存取消") : wasActive ? "已配置启用" : "未启用";
    const runtime = findLxRuntimeSource(item, (lxRuntimeSnapshot || {}).sources);
    const status = runtime?.status || "unknown";
    const runtimeLabel = !lxRuntimeSnapshot ? "运行状态未知"
      : lxRuntimeSnapshot.state === "unavailable" ? "服务不可用"
      : runtime ? (LX_SOURCE_STATUS_LABEL[status] || status) : wasActive ? "运行状态未知" : "未运行";
    const runtimeError = runtime?.error || "";
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
            ${lxCpuAlertHtml(item)}
            ${isActive || wasActive ? `<span class="lx-badge">${configLabel}</span>` : `<span class="muted">${configLabel}</span>`}
            <span class="chip"><span class="dot ${runtime && status === "success" ? "ok" : runtime && status === "failed" ? "err" : ""}"></span>${escapeHtml(runtimeLabel)}</span>
          </div>
          <div class="lx-item-url" title="${safeUrl}">${safeUrl}</div>
          ${runtimeError ? `<div class="muted">${escapeHtml(runtimeError)}</div>` : ""}
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
  container.querySelectorAll(".lx-cpu-alert").forEach((btn) => {
    btn.addEventListener("click", () => {
      const sourceId = String(btn.dataset.cpuSourceId || "");
      const item = lxSourceList.find((entry) => lxStableSourceId(entry) === sourceId);
      if (item) openLxCpuHelp(item, btn);
    });
  });
  // 轮询重绘会替换按钮节点；弹窗仍开着时把返回焦点改绑到新节点。
  if (lxCpuHelpSourceId && !$("#help-modal").hidden) {
    const replacement = Array.from(container.querySelectorAll(".lx-cpu-alert"))
      .find((btn) => String(btn.dataset.cpuSourceId || "") === lxCpuHelpSourceId);
    helpReturnFocus = replacement || container;
    const alert = lxCpuAlerts.get(lxCpuHelpSourceId);
    if (alert) fillLxCpuHelp(alert);
  }
}

function activateLxSource(idx) {
  const item = lxSourceList[idx];
  if (!item || item.active) return;
  item.active = true;
  lxSourceListDirty = true;
  syncLxSourceOrder();
  renderLxSourceList();
  renderLxSourceOrder();
  markDirty("已标记激活，需点击下方「保存并生效」（新激活源会先自动校验）");
  toast(`已标记激活「${item.name || lxDefaultName(item.url)}」，请点击下方「保存并生效」`, "ok");
}

function deactivateLxSource(idx) {
  const item = lxSourceList[idx];
  if (!item || !item.active) return;
  item.active = false;
  lxSourceListDirty = true;
  renderLxSourceList();
  renderLxSourceOrder();
  markDirty("已标记取消激活，需点击下方「保存并生效」");
  toast(`已标记取消激活「${item.name || lxDefaultName(item.url)}」，保存后停用`, "ok");
}

function deleteLxSource(idx) {
  const item = lxSourceList[idx];
  if (!item) return;
  const name = item.name || lxDefaultName(item.url);
  const sourceId = lxStableSourceId(item);
  lxSourceList.splice(idx, 1);
  lxSourceListDirty = true;
  if (sourceId) lxSourceOrder = lxSourceOrder.filter((id) => id !== sourceId);
  renderLxSourceList();
  renderLxSourceOrder();
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
    const r = await callVerify();
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
  return api("/api/lx/upload", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename, script }),
  });
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
  lxSourceListDirty = true;
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
$("#download-layout").addEventListener("change", () => {
  markDirty("存放方式需保存后生效；已有歌曲可稍后点击「整理」调整");
});
let _fsCheckSeq = 0;                // 防抖竞态：只采纳最后一次输入的校验结果
const _authorizedDirs = new Set();  // authorizeUserFile 每个路径只尝试一次，避免重复弹授权框

/* 目录权限校验：桌面链路由宿主机网关（root，真实落盘视角）拦截应答；
   直连 8774 时 WebUI 返回 501（容器内看不到宿主路径），降级为 skip 放行。 */
async function fsCheckDir(path, timeoutMs = 0) {
  try {
    const r = await api("/api/fs-check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    }, timeoutMs);
    if (!r.writable) {
      return { mode: "fail", message: r.exists ? "目录不可写（权限不足或只读挂载）" : "目录无法创建（父目录不可写）" };
    }
    if (!r.exists) return { mode: "warn", message: "目录不存在；父目录可写，保存后自动创建" };
    return { mode: "ok", message: "目录可写 ✓" };
  } catch (exc) {
    if (exc.name === "TimeoutError") return { mode: "fail", message: "目录检查超时，请重试" };
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
    $("#log-nas-btn").hidden = false;
    const sdk = await loadTrimSdk();
    if (sdk) {
      $("#lx-pick").hidden = false;
      DIR_FIELDS.forEach((f) => { $(f.pick).hidden = false; });
    }
  }
})();

/* -------------------------------------------------------------- 表单脏标记 */
["#tee-max", "#llm-base", "#llm-key", "#llm-model", "#liked-refresh-hours", "#liked-size", "#search-timeout", "#search-deep-max", "#bind-timeout", "#handoff-max", "#scan-path", "#lx-resolver-timeout", "#lx-url-timeout", "#lx-probe-timeout"].forEach((sel) =>
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
$$("input[name=play-quality]").forEach((el) => el.addEventListener("change", () => markDirty("在线播放音质需保存后生效")));
$$("input[name=download-quality]").forEach((el) => el.addEventListener("change", () => markDirty("APP下载音质需保存后生效")));
$("#lx-auto-switch").addEventListener("change", () => {
  updateLxAutoSwitchSettings();
  markDirty("洛雪自动切源设置需保存后生效");
});
$("#lx-platform-preferred").addEventListener("change", () => markDirty("洛雪平台优选设置需保存后生效"));
["#recommend-hot", "#recommend-daily", "#search-deep", "#tee-enabled", "#fav-autobind", "#auto-cover", "#lyric-auto-dl", "#netease-my-playlists", "#netease-playlist-resident"].forEach((sel) =>
  $(sel).addEventListener("change", () => markDirty()));
["#direct-netease", "#direct-musicdl", "#direct-lx"].forEach((sel) =>
  $(sel).addEventListener("change", () => markDirty("播放直连策略需保存后生效")));

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

function updateScanLabels() {
  $("#scan-delay-label").textContent = `（${$("#scan-delay").value || 0} 秒）`;
  $("#scan-cooldown-label").textContent = `（${$("#scan-cooldown").value || 0} 秒）`;
}
$("#scan-delay").addEventListener("input", updateScanLabels);
$("#scan-cooldown").addEventListener("input", updateScanLabels);

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
  $("#about-version").textContent = formatVersionLabel(info.version);
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
  // 打赏二维码是打包时的可选资源（CI 从 WECHAT_QRCODE / ALIPAY_QRCODE 生成）；开源发布包里没有则不展示
  const donate = $("#about-donate");
  const wechatWrap = $("#about-donate-wechat");
  const alipayWrap = $("#about-donate-alipay");
  const wechatImg = $("#about-donate-wechat-img");
  const alipayImg = $("#about-donate-img");

  const hasWechat = Boolean(info.donate_wechat);
  const hasAlipay = info.donate_alipay !== undefined
    ? Boolean(info.donate_alipay)
    : Boolean(info.donate);

  const syncDonateCard = () => {
    if (!donate) return;
    const wechatVisible = wechatWrap ? !wechatWrap.hidden : hasWechat;
    const alipayVisible = alipayWrap ? !alipayWrap.hidden : hasAlipay;
    donate.hidden = !wechatVisible && !alipayVisible;
  };

  if (wechatWrap) wechatWrap.hidden = !hasWechat;
  if (wechatImg) {
    wechatImg.onerror = () => {
      if (wechatWrap) wechatWrap.hidden = true;
      wechatImg.removeAttribute("src");
      syncDonateCard();
    };
    if (hasWechat) {
      wechatImg.src = `${APP_BASE}/static/wechat.png`;
    } else {
      wechatImg.removeAttribute("src");
    }
  }

  if (alipayWrap) alipayWrap.hidden = !hasAlipay;
  if (alipayImg) {
    alipayImg.onerror = () => {
      if (alipayWrap) alipayWrap.hidden = true;
      alipayImg.removeAttribute("src");
      syncDonateCard();
    };
    if (hasAlipay) {
      alipayImg.src = `${APP_BASE}/static/alipay.png`;
    } else {
      alipayImg.removeAttribute("src");
    }
  }
  syncDonateCard();
  // 进入关于页顺带刷新检查更新（后端有 TTL 缓存，命中不出网）
  checkUpdate();
}

let logShareFile = null;

function isMobileExportDevice() {
  const coarse = !!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches);
  const userAgent = (globalThis.navigator && globalThis.navigator.userAgent) || "";
  return coarse || /Android|iPhone|iPad|Mobile/i.test(userAgent);
}

function canShareLogFiles() {
  const nav = globalThis.navigator;
  return !!(nav && typeof nav.canShare === "function" && typeof nav.share === "function" &&
    typeof File === "function");
}

function setLogExportBusy(busy) {
  ["#log-export-btn", "#log-share-btn", "#log-nas-btn"].forEach((id) => {
    $(id).disabled = busy;
  });
}

function clearPreparedLogShare(clearNote) {
  logShareFile = null;
  $("#log-share-btn").hidden = true;
  if (clearNote) $("#log-export-note").textContent = "";
}

function browserDownloadLog(blob, name) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
  $("#log-export-note").textContent =
    `已发起下载 ${name}，保存位置由浏览器或系统设置决定`;
}

async function fetchLogArchive() {
  const resp = await fetch(APP_BASE + "/api/logs/export");
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    try { msg = (await resp.json()).error || msg; } catch (_) { /* 非 JSON */ }
    throw new Error(msg);
  }
  const blob = await resp.blob();
  const match = (resp.headers.get("content-disposition") || "").match(/filename="?([^";]+)"?/);
  const name = match ? match[1] : `fnmusic-logs-${Date.now()}.zip`;
  return { blob, name };
}

async function exportLogs() {
  if ($("#log-export-btn").disabled) return;
  setLogExportBusy(true);
  $("#log-export-note").textContent = "打包中…";
  $("#log-export-result").hidden = true;
  try {
    const archive = await fetchLogArchive();
    if (isMobileExportDevice() && canShareLogFiles()) {
      logShareFile = new File([archive.blob], archive.name, { type: "application/zip" });
      const supported = globalThis.navigator.canShare({
        files: [logShareFile], title: "fnmusic-ext 日志",
      });
      if (supported) {
        $("#log-share-btn").hidden = false;
        $("#log-export-note").textContent =
          `ZIP 已准备好（${archive.name}），请点击“保存或分享 ZIP”`;
        return;
      }
      logShareFile = null;
    }
    browserDownloadLog(archive.blob, archive.name);
  } catch (exc) {
    $("#log-export-note").textContent = "";
    toast("日志导出失败：" + exc.message, "fail");
    clearPreparedLogShare(false);
  } finally {
    setLogExportBusy(false);
  }
}
$("#log-export-btn").addEventListener("click", () => { exportLogs().catch(() => {}); });

async function sharePreparedLogs() {
  if (!logShareFile || $("#log-share-btn").disabled) return;
  setLogExportBusy(true);
  try {
    await globalThis.navigator.share({
      files: [logShareFile],
      title: "fnmusic-ext 日志",
      text: "fnmusic-ext 运行日志",
    });
    $("#log-export-note").textContent = "系统分享流程已结束";
  } catch (exc) {
    if (exc && exc.name === "AbortError") {
      $("#log-export-note").textContent = "已取消分享，ZIP 仍保留，可再次保存或分享";
    } else {
      browserDownloadLog(logShareFile, logShareFile.name);
      toast("系统分享不可用，已改为下载 ZIP；也可保存到 NAS", "fail");
    }
  } finally {
    setLogExportBusy(false);
  }
}
$("#log-share-btn").addEventListener("click", () => { sharePreparedLogs().catch(() => {}); });

async function exportLogsToNas() {
  if ($("#log-nas-btn").disabled) return;
  setLogExportBusy(true);
  $("#log-export-result").hidden = true;
  $("#log-export-note").textContent = "选择 NAS 保存目录…";
  try {
    const sdk = await loadTrimSdk();
    if (!sdk) {
      $("#log-export-note").textContent = "当前环境不支持 NAS 保存，请在飞牛 App 或飞牛桌面内打开";
      return;
    }
    if (sdk.isStandaloneWeb) {
      $("#log-export-note").textContent = "请从飞牛 App 或飞牛桌面打开管理台后再保存到 NAS";
      return;
    }
    if (typeof sdk.pickUserFile !== "function") {
      $("#log-export-note").textContent = "当前飞牛版本不支持目录选择，请升级飞牛 App 或系统";
      return;
    }
    const picked = await sdk.pickUserFile({
      directory: true,
      title: "选择日志保存位置",
      okText: "保存到这里",
      creatable: true,
      sidebarGroup: ["myFiles", "otherShare", "external", "remote", "favorites"],
    });
    const paths = (picked && picked.data) || [];
    if (!paths.length) {
      $("#log-export-note").textContent = "已取消选择目录，未导出日志";
      return;
    }
    const directory = paths[0];
    $("#log-export-note").textContent = "正在打包并保存到 NAS…";
    const resp = await fetch(APP_BASE + "/api/logs/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ directory }),
    });
    let body = {};
    try { body = await resp.json(); } catch (_) { /* 非 JSON */ }
    if (!resp.ok || !body.ok) throw new Error(body.error || body.detail || `HTTP ${resp.status}`);
    const result = $("#log-export-result");
    $("#log-export-path").textContent = body.path;
    $("#log-export-path").dataset.directory = directory;
    $("#log-export-path").dataset.path = body.path;
    result.hidden = false;
    $("#log-export-note").textContent = `已保存 ${body.filename}`;
    clearPreparedLogShare(false);
  } catch (exc) {
    $("#log-export-note").textContent = "";
    toast("保存到 NAS 失败：" + exc.message, "fail");
  } finally {
    setLogExportBusy(false);
  }
}
$("#log-nas-btn").addEventListener("click", () => { exportLogsToNas().catch(() => {}); });

async function copyLogExportPath() {
  const path = $("#log-export-path").dataset.path || "";
  if (!path) return;
  try {
    await globalThis.navigator.clipboard.writeText(path);
    toast("路径已复制", "ok");
  } catch (_) {
    toast("复制失败，请手动复制显示的路径", "fail");
  }
}
$("#log-copy-path").addEventListener("click", () => { copyLogExportPath().catch(() => {}); });

async function openLogExportFolder() {
  const directory = $("#log-export-path").dataset.directory || "";
  if (!directory) return;
  try {
    const sdk = await loadTrimSdk();
    if (!sdk || typeof sdk.openFileManager !== "function") {
      throw new Error("当前环境不支持打开文件管理器");
    }
    await sdk.openFileManager(directory);
  } catch (exc) {
    toast("打开文件夹失败：" + exc.message, "fail");
  }
}
$("#log-open-folder").addEventListener("click", () => { openLogExportFolder().catch(() => {}); });

window.addEventListener("pagehide", () => { clearPreparedLogShare(false); });

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
  $$(".nav-dot").forEach((el) => {
    el.hidden = !has;
    if (!el._updateBound) {
      el._updateBound = true;
      el.addEventListener("click", (ev) => {
        ev.stopPropagation();
        openUpdateModal();
      });
    }
  });
  $("#about-new").hidden = !has;
}

function fmtDate(iso) {
  const m = /^(\d{4}-\d{2}-\d{2})/.exec(String(iso || ""));
  return m ? m[1] : "-";
}

let updateTargetSnapshot = null;
let selfUpdateJobId = null;
let selfUpdatePollTimer = null;

function openUpdateModal() {
  if (!updateInfo || !updateInfo.has_update) return;
  const latest = updateInfo.latest || {};
  $("#update-modal-version").textContent = formatVersionLabel(latest.version || updateInfo.current_version);
  $("#update-modal-date").textContent = fmtDate(latest.published_at);
  // release 说明来自外部仓库，textContent 注入防 XSS
  $("#update-modal-changelog").textContent = latest.changelog || "暂无更新说明";
  // 升级跳转跟随检查源：GitHub 查到跳 GitHub release 页，Gitee 查到跳 Gitee 发行版页
  $("#update-upgrade-btn").href = latest.html_url || "#";
  $("#update-modal").hidden = false;

  // 重置弹窗内动态区域
  $("#update-modal-prompt").hidden = true;
  $("#update-modal-error").hidden = true;
  $("#update-modal-progress").hidden = !selfUpdateJobId;
  $("#update-install-btn").hidden = true;
  $("#update-install-btn").disabled = false;
  $("#update-upgrade-btn").hidden = false;

  if (selfUpdateJobId) {
    $("#update-upgrade-btn").hidden = true;
    return;
  }

  // 异步解析安装目标与架构匹配（若失败或为直连 501 则降级展示手动升级外链）
  const tagStr = String(latest.tag_name || latest.version || "").trim();
  if (!tagStr) return;
  const tag = tagStr.startsWith("v") ? tagStr : `v${tagStr}`;
  api(`/api/self-update/target?tag=${encodeURIComponent(tag)}`).then((res) => {
    if (res && res.ok && res.target) {
      updateTargetSnapshot = res.target;
      const verText = formatVersionLabel(res.target.tag || res.target.formal_base);
      const installVer = res.target.install_version || res.target.formal_base;
      $("#update-modal-prompt").textContent = `是否下载并安装 ${verText}（安装版本 ${installVer}）？`;
      $("#update-modal-prompt").hidden = false;
      $("#update-install-btn").hidden = false;
      $("#update-install-btn").disabled = false;
      $("#update-upgrade-btn").hidden = true;
    }
  }).catch(() => {
    $("#update-upgrade-btn").hidden = false;
  });
}

async function startSelfUpdate() {
  if (!updateTargetSnapshot || !updateTargetSnapshot.target_id) return;
  $("#update-install-btn").disabled = true;
  $("#update-install-btn").hidden = true;
  $("#update-upgrade-btn").hidden = true;
  $("#update-modal-prompt").hidden = true;
  $("#update-modal-error").hidden = true;
  $("#update-modal-progress").hidden = false;
  $("#update-progress-phase").textContent = "正在提交更新任务...";
  $("#update-progress-bar").style.width = "0%";
  $("#update-progress-detail").textContent = "准备中...";

  try {
    const res = await api("/api/self-update", {
      method: "POST",
      body: JSON.stringify({
        target_id: updateTargetSnapshot.target_id,
        confirm: true,
      }),
    });
    if (res && res.ok && res.job) {
      selfUpdateJobId = res.job.id;
      try { localStorage.setItem("fnmusic_self_update_job_id", selfUpdateJobId); } catch (_) {}
      pollSelfUpdate(selfUpdateJobId);
    } else {
      throw new Error((res && res.error) || "创建更新任务失败");
    }
  } catch (err) {
    $("#update-modal-progress").hidden = true;
    $("#update-modal-error").hidden = false;
    $("#update-error-text").textContent = err.message || "启动更新失败";
    $("#update-install-btn").disabled = false;
    $("#update-install-btn").hidden = false;
  }
}

function pollSelfUpdate(jobId) {
  if (selfUpdatePollTimer) clearTimeout(selfUpdatePollTimer);

  const phaseLabels = {
    downloading: "正在下载更新包",
    verifying: "正在校验安装包哈希值",
    backing_up: "正在备份配置与持久数据",
    staging: "正在暂存安装包到应用中心",
    installing: "正在执行原生升级，服务可能短暂重启",
    restarting: "正在等待服务重启恢复",
  };

  async function step() {
    try {
      const res = await api(`/api/self-update/jobs/${jobId}`);
      if (!res || !res.ok || !res.job) {
        throw new Error("查询任务失败");
      }
      const job = res.job;
      const phase = job.phase || "";
      const status = job.status || "";

      let phaseText = phaseLabels[phase] || "处理中...";
      if (phase === "downloading" && job.source) {
        phaseText += ` (${job.source === "gitee" ? "Gitee" : "GitHub"})`;
      }
      $("#update-progress-phase").textContent = phaseText;

      if (phase === "downloading") {
        const dl = Number(job.downloaded_bytes || 0);
        const total = Number(job.total_bytes || 0);
        if (total > 0) {
          const pct = Math.min(100, Math.round((dl / total) * 100));
          $("#update-progress-bar").style.width = pct + "%";
          $("#update-progress-detail").textContent =
            `${(dl / 1024 / 1024).toFixed(1)} MB / ${(total / 1024 / 1024).toFixed(1)} MB (${pct}%)`;
        } else {
          $("#update-progress-detail").textContent = `${(dl / 1024 / 1024).toFixed(1)} MB`;
        }
      } else {
        $("#update-progress-bar").style.width = "100%";
        $("#update-progress-detail").textContent = status === "completed" ? "已完成" : "请稍候...";
      }

      if (status === "completed") {
        $("#update-progress-phase").textContent = "升级成功，正在刷新页面...";
        selfUpdateJobId = null;
        try { localStorage.removeItem("fnmusic_self_update_job_id"); } catch (_) {}
        setTimeout(() => { window.location.reload(); }, 3000);
        return;
      }

      if (status === "failed") {
        selfUpdateJobId = null;
        try { localStorage.removeItem("fnmusic_self_update_job_id"); } catch (_) {}
        $("#update-modal-progress").hidden = true;
        $("#update-modal-error").hidden = false;
        const msg = job.message || "";
        if (msg.includes("下载失败") || msg.includes("GitHub 下载最新版本") || msg.includes("所有下载源均失败")) {
          $("#update-error-text").textContent = "下载失败，请到 GitHub 下载最新版本。";
          $("#update-error-link").href = "https://github.com/javycoder/fnos_music_ext/releases";
          $("#update-error-link").hidden = false;
        } else {
          $("#update-error-text").textContent = `升级失败: ${msg || "未知错误"}`;
          $("#update-error-link").href = "https://github.com/javycoder/fnos_music_ext/releases";
          $("#update-error-link").hidden = false;
        }
        $("#update-install-btn").disabled = false;
        $("#update-install-btn").hidden = false;
        $("#update-install-btn").textContent = "重试下载并安装";
        return;
      }

      if (status === "unknown") {
        selfUpdateJobId = null;
        try { localStorage.removeItem("fnmusic_self_update_job_id"); } catch (_) {}
        $("#update-modal-progress").hidden = true;
        $("#update-modal-error").hidden = false;
        $("#update-error-text").textContent = `安装结果待确认: ${job.message || "请前往飞牛应用中心查看状态"}`;
        $("#update-error-link").hidden = true;
        return;
      }

      const pollInterval = (typeof window !== "undefined" && typeof window.SELF_UPDATE_POLL_INTERVAL === "number")
        ? window.SELF_UPDATE_POLL_INTERVAL
        : 1500;
      selfUpdatePollTimer = setTimeout(step, pollInterval);
    } catch (_) {
      const errInterval = (typeof window !== "undefined" && typeof window.SELF_UPDATE_POLL_INTERVAL === "number")
        ? window.SELF_UPDATE_POLL_INTERVAL
        : 2000;
      selfUpdatePollTimer = setTimeout(step, errInterval);
    }
  }

  step();
}

if (typeof window !== "undefined") {
  window.pollSelfUpdate = pollSelfUpdate;
  window.startSelfUpdate = startSelfUpdate;
}

async function checkActiveSelfUpdateJob() {
  try {
    const res = await api("/api/self-update");
    if (res && res.ok && res.job) {
      if (res.job.status === "queued" || res.job.status === "running") {
        selfUpdateJobId = res.job.id;
        openUpdateModal();
        pollSelfUpdate(selfUpdateJobId);
      }
    }
  } catch (_) {}
}

function closeUpdateModal() {
  $("#update-modal").hidden = true;
}

$("#about-new").addEventListener("click", openUpdateModal);
$("#update-install-btn").addEventListener("click", () => {
  startSelfUpdate().catch(() => {});
});
$("#update-cancel-btn").addEventListener("click", closeUpdateModal);
$("#update-modal").addEventListener("click", (ev) => {
  if (ev.target === ev.currentTarget) closeUpdateModal();  // 点遮罩空白处关闭
});

/* -------------------------------------------------------------- 垃圾清理 Beta */
let cleanupJob = null;
let cleanupSubmitting = false;
let cleanupPolling = false;
let cleanupTimer = null;

function formatCleanupBytes(size) {
  const value = Number(size);
  if (!Number.isFinite(value) || value < 0) return "0.0 KB";
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function renderCleanupJob(job) {
  const labels = { running: "清理中", completed: "清理完成", failed: "清理失败" };
  $("#cleanup-status").textContent =
    `任务 ${job.id} · ${labels[job.status]} · 阶段：${job.phase || "-"}` +
    ` · 已扫描 ${job.scanned ?? 0} · 已删除 ${job.removed ?? 0}` +
    ` · 释放 ${formatCleanupBytes(job.bytes ?? 0)} · 已跳过 ${job.skipped ?? 0}`;
  const lines = [];
  for (const [name, counts] of Object.entries(job.categories || {})) {
    lines.push(`${name}：扫描 ${counts.scanned || 0}，删除 ${counts.removed || 0}，释放 ${formatCleanupBytes(counts.bytes || 0)}，跳过 ${counts.skipped || 0}`);
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
  const modal = $("#cleanup-modal").hidden ? $("#organize-modal") : $("#cleanup-modal");
  if (modal.hidden) return;
  if (ev.key === "Escape") {
    ev.preventDefault();
    if (modal === $("#cleanup-modal")) closeCleanupModal();
    else closeOrganizeModal();
  } else if (ev.key === "Tab") {
    // 对话框仅有两个按钮，将键盘焦点限制在其中，默认落在取消。
    ev.preventDefault();
    const cancel = modal === $("#cleanup-modal") ? $("#cleanup-cancel-btn") : $("#organize-cancel-btn");
    const confirm = modal === $("#cleanup-modal") ? $("#cleanup-confirm-btn") : $("#organize-confirm-btn");
    (document.activeElement === cancel ? confirm : cancel).focus();
  }
});

/* ------------------------------------------------------ 下载目录整理 Beta */
function parseUserMap(raw) {
  try {
    const value = typeof raw === "string" ? JSON.parse(raw || "{}") : (raw || {});
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  } catch (_) {
    return {};
  }
}

function absoluteDirError(label, path) {
  if (!path) return "";
  if (!path.startsWith("/") || path.split("/").includes("..") || /[\u0000-\u001f\u007f]/.test(path)) {
    return label + "必须是以 / 开头的绝对路径，且不能包含 .. 或控制字符";
  }
  return "";
}

function scanAdminPasswordUpdate() {
  if (!configSchema.FNMUSIC_SCAN_ADMIN_PASSWORD || !$("#scan-admin-password")) return {};
  if (scanAdminPasswordClear) return { FNMUSIC_SCAN_ADMIN_PASSWORD: "" };
  if (!scanAdminPasswordDirty) return {};
  return { FNMUSIC_SCAN_ADMIN_PASSWORD: $("#scan-admin-password").value };
}

function userDirInput(guid) {
  return Array.from(document.querySelectorAll("[data-user-dir]")).find((el) => el.dataset.userDir === guid) || null;
}

function userStorageConfigPatch() {
  if (!userStorageReady) return {};
  const directories = {};
  const scans = {};
  for (const row of document.querySelectorAll("[data-user-row]")) {
    const guid = row.dataset.userRow;
    const dir = (userDirInput(guid) || {}).value || "";
    directories[guid] = dir.trim() || null;
    const mode = (row.querySelector("[data-user-scan-mode]") || {}).value || "inherit";
    if (mode === "disable") scans[guid] = "";
    else if (mode === "override") scans[guid] = (row.querySelector("[data-user-scan-value]") || {}).value.trim();
    else scans[guid] = null;
  }
  for (const row of document.querySelectorAll("[data-stale-delete]:checked")) {
    directories[row.dataset.staleDelete] = null;
    scans[row.dataset.staleDelete] = null;
  }
  const patch = {};
  if (configSchema.FNMUSIC_TEE_SAVE_DIR_BY_USER) patch.FNMUSIC_TEE_SAVE_DIR_BY_USER = directories;
  if (configSchema.FNMUSIC_LIBRARY_SCAN_BY_USER) patch.FNMUSIC_LIBRARY_SCAN_BY_USER = scans;
  return patch;
}

function validateUserStoragePatch(values) {
  const directories = values.FNMUSIC_TEE_SAVE_DIR_BY_USER || {};
  const scans = values.FNMUSIC_LIBRARY_SCAN_BY_USER || {};
  for (const path of Object.values(directories)) {
    if (path == null) continue;
    const error = absoluteDirError("用户下载目录", path);
    if (error) throw new Error(error);
  }
  for (const row of document.querySelectorAll("[data-user-row]")) {
    if ((row.querySelector("[data-user-scan-mode]") || {}).value !== "override") continue;
    const value = scans[row.dataset.userRow];
    if (!value) throw new Error("选择单独设置扫描时必须填写扫描配置");
    if (/[\u0000-\u001f\u007f]/.test(value)) throw new Error("扫描配置包含控制字符");
  }
}

function renderUserStorage() {
  const list = $("#user-storage-list");
  const staleBox = $("#user-storage-stale");
  const organizeUser = $("#organize-user");
  const organizeField = $("#organize-user-field");
  if (!list || !staleBox) return;
  const enabled = Boolean($("#tee-save-by-user") && $("#tee-save-by-user").checked);
  if (organizeField) organizeField.hidden = !enabled;
  if (!userStorageReady || !userStorageStatus) {
    list.textContent = enabled
      ? ("用户目录状态不可用：" + (userStorageError || "未知错误") + "。本次保存不会修改各用户目录和扫描配置。")
      : "";
    staleBox.textContent = "";
    if (organizeUser) organizeUser.innerHTML = "";
    return;
  }
  const users = Array.isArray(userStorageStatus.users) ? userStorageStatus.users : [];
  if (organizeUser) {
    const previous = organizeUser.value;
    organizeUser.innerHTML = users.map((user) =>
      `<option value="${escapeHtml(user.guid)}">${escapeHtml(user.name || user.guid)}</option>`).join("");
    if (previous && Array.from(organizeUser.options).some((option) => option.value === previous)) {
      organizeUser.value = previous;
    }
  }
  if (!enabled) {
    list.textContent = "";
    staleBox.textContent = "";
    return;
  }
  list.innerHTML = users.map((user) => {
    const custom = savedUserDirs[user.guid] || "";
    const hasScan = Object.prototype.hasOwnProperty.call(savedUserScans, user.guid);
    const scan = hasScan ? savedUserScans[user.guid] : null;
    const mode = scan == null ? "inherit" : (String(scan).trim() ? "override" : "disable");
    const writable = user.root_writable ? "可写" : (user.root_exists ? "存在但不可写" : "目录尚不可用");
    return `<div class="user-storage-row" data-user-row="${escapeHtml(user.guid)}">
      <div class="user-storage-head"><b>${escapeHtml(user.name || "未命名用户")}</b><span class="user-storage-meta">${escapeHtml(user.role || "")}</span></div>
      <div class="user-storage-status">有效目录：${escapeHtml(user.root || "-")} · ${writable}</div>
      <div class="user-storage-status">最近扫描：${escapeHtml(user.scan_status || "尚无结果")}</div>
      <label class="field"><span>自定义目录（留空恢复自动目录）</span>
        <input class="input" data-user-dir="${escapeHtml(user.guid)}" value="${escapeHtml(custom)}" placeholder="留空使用自动目录">
      </label>
      <div class="user-storage-actions">
        <button class="btn" type="button" data-user-dir-pick="${escapeHtml(user.guid)}">从 NAS 选择</button>
        <button class="btn" type="button" data-user-scan="${escapeHtml(user.guid)}">扫描此用户</button>
      </div>
      <label class="field"><span>曲库扫描</span>
        <select class="input" data-user-scan-mode="${escapeHtml(user.guid)}">
          <option value="inherit"${mode === "inherit" ? " selected" : ""}>继承全局扫描</option>
          <option value="disable"${mode === "disable" ? " selected" : ""}>显式禁用</option>
          <option value="override"${mode === "override" ? " selected" : ""}>单独设置</option>
        </select>
      </label>
      <label class="field"><span>单独扫描配置</span>
        <input class="input" data-user-scan-value="${escapeHtml(user.guid)}" value="${escapeHtml(mode === "override" ? scan : "")}" placeholder="路径|JSON正文"${mode === "override" ? "" : " hidden"}>
      </label>
    </div>`;
  }).join("") || "没有读取到飞牛音乐用户。";
  const stale = Array.isArray(userStorageStatus.stale_user_guids) ? userStorageStatus.stale_user_guids : [];
  staleBox.innerHTML = stale.length
    ? "以下配置对应的用户已不在飞牛音乐中。勾选后保存才会删除配置，不会删除已保存的歌曲。" +
      stale.map((guid) => `<label class="user-storage-stale-row"><input type="checkbox" data-stale-delete="${escapeHtml(guid)}">删除失效用户 ${escapeHtml(guid)} 的目录和扫描配置</label>`).join("")
    : "";
}

async function loadUserStorage() {
  if (!configSchema.FNMUSIC_TEE_SAVE_BY_USER || !$("#user-storage-list")) {
    userStorageReady = false;
    renderUserStorage();
    return;
  }
  try {
    const response = await api("/api/maintenance/user-storage");
    if (!response || response.ok !== true || !Array.isArray(response.users)) {
      throw new Error((response && response.error) || "用户目录响应无效");
    }
    userStorageStatus = response;
    userStorageReady = true;
    userStorageError = "";
  } catch (exc) {
    userStorageStatus = null;
    userStorageReady = false;
    userStorageError = exc.message;
  }
  renderUserStorage();
}

function maintenanceSucceeded(response) {
  return Boolean(response) && response.ok === true;
}

async function testScanAdmin() {
  const status = $("#scan-admin-status");
  if (!status) return;
  status.textContent = "正在测试已保存的管理员登录…";
  try {
    const response = await api("/api/maintenance/scan-admin-test", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    if (!maintenanceSucceeded(response)) throw new Error((response && response.error) || "管理员登录测试响应无效");
    status.textContent = "管理员登录测试成功";
  } catch (exc) {
    status.textContent = "管理员登录测试失败：" + exc.message;
  }
}

async function scanOneUser(guid) {
  const status = $("#scan-admin-status");
  if (status) status.textContent = "正在提交用户扫描…";
  try {
    const response = await api("/api/maintenance/scan-user", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_guid: guid }),
    });
    if (!maintenanceSucceeded(response)) throw new Error((response && response.error) || "用户扫描响应无效");
    if (status) status.textContent = "用户扫描已确认";
    await loadUserStorage();
  } catch (exc) {
    if (status) status.textContent = "用户扫描失败：" + exc.message;
  }
}

function bindUserStorageControls() {
  const list = $("#user-storage-list");
  const staleBox = $("#user-storage-stale");
  if (list) {
    list.addEventListener("input", (ev) => {
      if (ev.target && ev.target.matches && ev.target.matches("[data-user-dir], [data-user-scan-value]")) markDirty();
    });
    list.addEventListener("change", (ev) => {
      const mode = ev.target && ev.target.matches && ev.target.matches("[data-user-scan-mode]") ? ev.target : null;
      if (!mode) return;
      const row = mode.closest("[data-user-row]");
      const input = row && row.querySelector("[data-user-scan-value]");
      if (input) input.hidden = mode.value !== "override";
      markDirty();
    });
    list.addEventListener("click", async (ev) => {
      const pick = ev.target && ev.target.closest && ev.target.closest("[data-user-dir-pick]");
      const scan = ev.target && ev.target.closest && ev.target.closest("[data-user-scan]");
      if (pick) {
        const sdk = await loadTrimSdk();
        if (!sdk) { toast("当前环境不支持 NAS 目录选择（直连 8774 时请直接粘贴路径）", "fail"); return; }
        try {
          const result = await sdk.pickUserFile({
            directory: true, title: "选择用户下载目录", okText: "选择",
            sidebarGroup: ["myFiles", "otherShare", "favorites"],
          });
          const paths = (result && result.data) || [];
          const input = userDirInput(pick.dataset.userDirPick);
          if (paths.length && input) {
            input.value = paths[0];
            markDirty();
          }
        } catch (exc) {
          toast("NAS 目录选择失败：" + exc.message, "fail");
        }
      } else if (scan) {
        await scanOneUser(scan.dataset.userScan);
      }
    });
  }
  if (staleBox) staleBox.addEventListener("change", () => markDirty());
  if ($("#tee-save-by-user")) $("#tee-save-by-user").addEventListener("change", () => {
    markDirty();
    renderUserStorage();
  });
  if ($("#scan-admin-username")) $("#scan-admin-username").addEventListener("input", () => markDirty());
  if ($("#scan-admin-device-id")) $("#scan-admin-device-id").addEventListener("input", () => markDirty());
  if ($("#scan-admin-password")) $("#scan-admin-password").addEventListener("input", () => {
    scanAdminPasswordDirty = true;
    scanAdminPasswordClear = false;
    markDirty();
  });
  if ($("#scan-admin-password-clear")) $("#scan-admin-password-clear").addEventListener("click", () => {
    scanAdminPasswordClear = true;
    scanAdminPasswordDirty = false;
    if ($("#scan-admin-password")) {
      $("#scan-admin-password").value = "";
      $("#scan-admin-password").placeholder = "保存后清除已保存密码";
    }
    markDirty("将清除已保存的扫描管理员密码");
  });
  if ($("#scan-admin-test")) $("#scan-admin-test").addEventListener("click", () => { testScanAdmin(); });
}
bindUserStorageControls();

let organizeInfo = null;
let organizeConfirmation = null;
let organizeJob = null;
let organizeSubmitting = false;
let organizePolling = false;
let organizeTimer = null;
const ORGANIZE_PATH = "/api/maintenance/organize";

function renderOrganizeJob(job) {
  const labels = { running: "整理中", completed: "整理完成", failed: "整理失败" };
  const layout = DOWNLOAD_LAYOUT_LABELS[job.layout] || job.layout || "-";
  let text = `任务 ${job.id} · ${labels[job.status]} · 阶段：${job.phase || "-"}` +
    ` · 已扫描 ${job.scanned ?? 0} · 已移动 ${job.moved ?? 0}` +
    ` · 未变化 ${job.unchanged ?? 0} · 已跳过 ${job.skipped ?? 0} · 失败 ${job.failed ?? 0}`;
  if (job.status === "completed") {
    text += "。整理后请在飞牛音乐中重新扫描曲库";
  }
  $("#organize-status").textContent = text;
  $("#organize-status").dataset.layout = layout;
  const lines = [];
  if (job.journal) lines.push(`迁移日志：${job.journal}`);
  for (const item of (Array.isArray(job.details) ? job.details : [])) {
    lines.push(`详情：${typeof item === "string" ? item : JSON.stringify(item)}`);
  }
  for (const item of (Array.isArray(job.errors) ? job.errors : [])) {
    lines.push(`错误：${typeof item === "string" ? item : JSON.stringify(item)}`);
  }
  $("#organize-details").textContent = lines.join("\n");
  $("#organize-details").hidden = !lines.length;
  $("#organize-open-btn").textContent = job.status === "running" ? "查看当前整理任务" : "整理";
}

function acceptOrganizeJob(response) {
  const job = response && response.job;
  if (!response || response.ok !== true || !job || !job.id ||
      !["running", "completed", "failed"].includes(job.status)) {
    throw new Error("整理任务响应格式无效");
  }
  organizeJob = job;
  renderOrganizeJob(job);
  clearTimeout(organizeTimer);
  organizeTimer = null;
  if (job.status === "running") organizeTimer = setTimeout(pollOrganizeJob, 1000);
}

async function loadOrganizeStatus() {
  try {
    const response = await api(ORGANIZE_PATH);
    if (!response || response.ok !== true || !DOWNLOAD_LAYOUT_LABELS[response.layout]) {
      throw new Error("整理状态响应格式无效");
    }
    if (response.requires_user === true) {
      organizeInfo = { root: "", layout: response.layout, requiresUser: true };
    } else if (typeof response.root !== "string" || !response.root) {
      throw new Error("整理状态响应格式无效");
    } else {
      organizeInfo = { root: response.root, layout: response.layout, requiresUser: false };
    }
    if (response.job) acceptOrganizeJob(response);
  } catch (exc) {
    organizeInfo = null;
    $("#organize-status").textContent = `整理不可用：${exc.message}`;
  }
}

async function pollOrganizeJob() {
  if (organizePolling || !organizeJob || organizeJob.status !== "running") return;
  clearTimeout(organizeTimer);
  organizeTimer = null;
  organizePolling = true;
  try {
    acceptOrganizeJob(await api(ORGANIZE_PATH + "/" + encodeURIComponent(organizeJob.id)));
  } catch (exc) {
    $("#organize-status").textContent =
      `任务 ${organizeJob.id} · 查询失败：${exc.message}；点击整理重试查询，不会重复提交。`;
  } finally {
    organizePolling = false;
  }
}

function closeOrganizeModal() {
  $("#organize-modal").hidden = true;
  organizeConfirmation = null;
  $("#organize-open-btn").focus();
}

function organizeSettingsSaved() {
  const savedLayout = configValues.FNMUSIC_DOWNLOAD_LAYOUT || "flat";
  const savedDir = (configValues.FNMUSIC_TEE_SAVE_DIR || "").trim();
  if (($("#download-layout").value || "flat") !== savedLayout) return false;
  if ($("#dir-download").value.trim() !== savedDir) return false;
  if (!organizeInfo || organizeInfo.layout !== savedLayout) return false;
  if (!organizeInfo.requiresUser) return true;
  const guid = ($("#organize-user") && $("#organize-user").value) || "";
  const input = userDirInput(guid);
  return Boolean(guid) && Boolean(input) && input.value.trim() === (savedUserDirs[guid] || "");
}

async function openOrganizeModal() {
  if (organizeSubmitting) return;
  if (organizeJob && organizeJob.status === "running") {
    renderOrganizeJob(organizeJob);
    pollOrganizeJob();
    return;
  }
  await loadOrganizeStatus();
  if (!organizeInfo) return;
  if (organizeJob && organizeJob.status === "running") return;
  if (organizeInfo.requiresUser) {
    await loadUserStorage();
    const guid = ($("#organize-user") && $("#organize-user").value) || "";
    const user = ((userStorageStatus && userStorageStatus.users) || []).find((item) => item.guid === guid);
    if (!user || typeof user.root !== "string" || !user.root) {
      toast("请先选择一个可整理的用户目录", "fail");
      return;
    }
    organizeInfo = { ...organizeInfo, root: user.root, userGuid: guid };
  }
  if (!organizeSettingsSaved()) {
    toast("请先保存下载目录和存放方式，再执行整理", "fail");
    return;
  }
  const label = DOWNLOAD_LAYOUT_LABELS[organizeInfo.layout];
  $("#organize-warning").textContent =
    `将按“${label}”整理“${organizeInfo.root}”内的全部歌曲，并同步处理可识别的歌词和封面。` +
    "此功能为 Beta，移动文件可能导致歌曲暂时无法播放，收藏或歌单关联也可能受影响，" +
    "整理后可能需要在飞牛音乐中重新扫描曲库。建议先备份，是否继续？";
  $("#organize-modal").hidden = false;
  organizeConfirmation = { ...organizeInfo };
  $("#organize-cancel-btn").focus();
}

async function startOrganize() {
  if ($("#organize-modal").hidden || organizeSubmitting || !organizeConfirmation) return;
  if (!organizeSettingsSaved()) {
    closeOrganizeModal();
    toast("请先保存下载目录和存放方式，再执行整理", "fail");
    return;
  }
  if (organizeJob && organizeJob.status === "running") {
    closeOrganizeModal();
    renderOrganizeJob(organizeJob);
    return;
  }
  organizeSubmitting = true;
  const confirmed = organizeConfirmation;
  $("#organize-open-btn").disabled = true;
  closeOrganizeModal();
  $("#organize-status").textContent = "正在提交整理任务…";
  $("#organize-details").textContent = "";
  $("#organize-details").hidden = true;
  try {
    acceptOrganizeJob(await api(ORGANIZE_PATH, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        confirm_beta: true,
        root: confirmed.root,
        layout: confirmed.layout,
      ...(confirmed.userGuid ? { user_guid: confirmed.userGuid } : {}),
      }),
    }));
  } catch (exc) {
    $("#organize-status").textContent = "整理请求失败：" + exc.message;
  } finally {
    organizeSubmitting = false;
    $("#organize-open-btn").disabled = false;
  }
}

$("#organize-open-btn").addEventListener("click", openOrganizeModal);
$("#organize-cancel-btn").addEventListener("click", closeOrganizeModal);
$("#organize-confirm-btn").addEventListener("click", startOrganize);
$("#organize-modal").addEventListener("click", (ev) => {
  if (ev.target === ev.currentTarget) closeOrganizeModal();
});

/* -------------------------------------------------------------- 启动 */
(async function boot() {
  initTheme().catch(() => {});  // 外观先应用（内部等宿主 SDK，不阻塞配置加载）
  await loadConfig();
  await loadStatus();
  await loadOrganizeStatus();
  checkActiveSelfUpdateJob().catch(() => {});  // 恢复可能仍在执行的自更新任务
  checkUpdate();  // 进页面即查（后端 TTL 缓存兜住 GitHub 限额），不阻塞启动
  await loadPlatforms(false);  // 页面加载不自动拉起预览进程，等用户点选音源
  void checkActiveConfigJob();
  if ($("#public-playlists-enabled").checked) await showPublicBrowser();
  setInterval(loadStatus, 15000);
})();
