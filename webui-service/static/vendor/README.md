# vendored 前端 SDK

## trim-web-app.js

- 来源：npm 包 `@trimjs/web-app@0.4.2` 的 `dist/index.js`（原生 ESM，未改动）。
- 官方文档：https://developer.fnnas.com/api/calling/
- 用途：管理页在飞牛桌面（统一网关）内调用 `pickUserFile` 打开 NAS 文件选择器，
  选择洛雪自定义源脚本 `.js` 文件。
- 更新方式：
  `curl -L https://registry.npmjs.org/@trimjs/web-app/-/web-app-<版本>.tgz | tar -xz --strip-components=2 package/dist/index.js -O > trim-web-app.js`
- 页面以 `import("/app/fnmusic-ext/static/vendor/trim-web-app.js")` 懒加载；
  直连 8774 的独立浏览器环境加载失败会自动降级（隐藏"从 NAS 选择"按钮）。
