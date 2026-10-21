/* 宿主主题异步取得前，嵌入页用上次宿主主题引导首屏；直连页只跟随设备。 */
(function () {
  var pref = "auto";
  var host = null;
  try {
    var saved = window.localStorage.getItem("fnmusic-theme");
    if (saved === "light" || saved === "dark") pref = saved;
    if (window.location && window.location.pathname.startsWith("/app/")) {
      var cached = window.localStorage.getItem("fnmusic-host-theme");
      if (cached === "light" || cached === "dark") host = cached;
    }
  } catch (_) { /* 隐私模式禁止 localStorage 时按跟随系统处理 */ }
  var resolved = pref === "auto" ? host : pref;
  var dark = resolved === "dark" || (!resolved &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
})();
