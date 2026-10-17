/* 首屏主题引导（在样式表之前同步执行）：
   把 localStorage 里的外观偏好解析成 <html data-theme="light|dark">，
   避免深色用户先闪一下白底。偏好键与 app.js 的 THEME_KEY 保持一致（fnmusic-theme）；
   飞牛宿主主题由 app.js 异步取回后刷新。 */
(function () {
  var pref = "auto";
  try {
    var saved = window.localStorage.getItem("fnmusic-theme");
    if (saved === "light" || saved === "dark") pref = saved;
  } catch (_) { /* 隐私模式禁止 localStorage 时按跟随系统处理 */ }
  var dark = pref === "dark" || (pref !== "light" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
})();
