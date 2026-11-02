# fnmusic-ext WebUI 设计规范（v1）

> 面向「换人或换模型继续实现」的落地手册。本文与 `webui-service/static/{index.html,style.css,app.js}` 一一对应；文中色值、字号、间距均为最终取值，不是建议值。

---

## 0. 定调（Tone）

**静谧 Quiet · 精密 Precise · 原生 Native(fnOS) · 克制 Restrained · 呼吸感 Breathable**

- 参照：飞牛 fnOS 系统设置窗体 + Apple「设置」分组内嵌列表卡 + Linear 的精确微边框。
- 一条铁律：**卡片上只放用户要做的决定，解释一律进 `?` 弹窗**。
- 零外部依赖：不引入 Web 字体、CDN、构建链；纯 HTML + CSS 变量 + 原生 JS + 内联 SVG symbol。

### 三级渐进式披露

| 层级 | 面向 | 承载 |
| :-- | :-- | :-- |
| 第一层 | 小白 | 一级菜单直达常用功能；每个开关只配一句副标题（≤20 字） |
| 第二层 | 想弄懂的人 | 标题/卡片右侧 `?`：结论 → 怎么选 → 细节，三段白话 |
| 第三层 | 发烧友 | 一级「高级」子页 + 卡片内 `<details class="adv-fold">` 折叠参数 |

---

## 1. 字体

| 变量 | 取值 |
| :-- | :-- |
| `--font-sans` | `-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'PingFang SC', 'HarmonyOS Sans SC', 'Hiragino Sans GB', 'Microsoft YaHei UI', 'Microsoft YaHei', 'Noto Sans CJK SC', system-ui, sans-serif` |
| `--font-mono` | `'SF Mono', 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', monospace`（路径 / URL / 日志 / 版本号 / Token 数字） |

| 用途 | 选择器 | 桌面 | 移动 | 字重 | 行高 |
| :-- | :-- | :-- | :-- | :-- | :-- |
| 品牌名 | `.brand-name` | 16px | — | 700 | 1.25（字距 -0.01em） |
| 页面标题 | `h2` | 20px | 18px | 700 | 1.35（字距 -0.015em） |
| 页副标题 | `.page-sub` | 12.5px | 12.5px | 400 | 1.6，色 `--muted` |
| 卡片标题 | `.card-title` | 15px | 15px | 600 | 1.4 |
| 侧栏一级 | `.nav-item` | 14px | — | 500（激活 600） | 1.4 |
| 侧栏二级 | `.nav-sub` | 13.5px | — | 500（激活 600） | 1.4 |
| 底栏标签 | `.bottom-nav .nav-item` | — | 10.5px | 500（激活 700） | 1.2 |
| 开关主文案 | `.switch-row > span` | 14px | 14px | 500 | 1.55 |
| 开关副文案 | `.switch-row .sub` | 12.5px | 12.5px | 400 | 1.5，色 `--muted` |
| 输入框 | `.input` | 14px | **16px** | 400 | 1.45 |
| 辅助说明 | `.hint` | 12.5px | 12.5px | 400 | 1.6，色 `--muted` |
| 徽章 / 芯片 | `.badge` `.chip` | 12 / 12.5px | 同 | 600 / 500 | 1.5 |
| 表格 | `.usage-table` | 12.5px | 同 | 400 | 1.5（tabular-nums） |

移动端输入框 16px 是硬性要求：低于 16px 时 iOS Safari 聚焦会强制放大页面。

---

## 2. 色彩（Light / Dark 成对声明）

参与对比度计算的色值必须写成 6 位十六进制 `#rrggbb`。

| Token | Light | Dark | 用途 |
| :-- | :-- | :-- | :-- |
| `--bg` | `#f3f5f9` | `#0d1117` | 页面画布 |
| `--card` | `#ffffff` | `#161b22` | 卡片 / 侧栏 / 弹窗 |
| `--surface-2` | `#f8fafc` | `#1c232d` | 折叠面板、报告、日志衬底 |
| `--surface-3` | `#eef2f7` | `#232b36` | 更深衬底（预留） |
| `--input-bg` | `#ffffff` | `#0f141b` | 输入 / 下拉 / 单选卡 |
| `--chip-bg` | `#f8fafc` | `#1c232d` | 芯片、歌单项、洛雪源项 |
| `--line` | `#e2e8f0` | `#28313e` | 边框与分割线 |
| `--line-strong` | `#cbd5e1` | `#3a4553` | 悬停描边 |
| `--text` | `#1e293b` | `#e6edf3` | 正文 / 标题 |
| `--muted` | `#64748b` | `#8b98a9` | 次要文字 / 占位符 |
| `--accent` | `#2563eb` | `#6b9bff` | 选中、主按钮、激活菜单、开关开启 |
| `--accent-soft` | `#eff6ff` | `#1b2a47` | 悬停底、选中底 |
| `--on-accent` | `#ffffff` | `#0d1117` | 实心强调 / 状态底上的前景 |
| `--ok` | `#15803d` | `#3fb97a` | 成功 / 就绪 |
| `--ok-soft` | `#dcfce7` | `#132f21` | 成功浅底 |
| `--warn` | `#b45309` | `#e3a545` | 警告 / 降级 |
| `--warn-soft` | `#fef3c7` | `#33270f` | 警告浅底 |
| `--err` | `#dc2626` | `#f87171` | 错误 / 危险 / NEW |
| `--err-soft` | `#fee2e2` | `#3a1d1d` | 错误浅底 |
| `--badge-dim-bg` | `#eef2f6` | `#232b36` | 次要徽章底 |
| `--dot-idle` | `#94a3b8` | `#4b5563` | 休眠圆点、开关关闭态 |
| `--overlay` | `rgba(15,23,42,.45)` | `rgba(0,0,0,.65)` | 遮罩 |
| `--bar-bg` | `rgba(255,255,255,.92)` | `rgba(22,27,34,.92)` | 保存条（配 blur(10px)） |
| `--nav-bg` | `rgba(255,255,255,.95)` | `rgba(22,27,34,.95)` | 移动底栏 |
| `--toast-bg` / `--toast-text` | `#1e293b` / `#ffffff` | `#283240` / `#f1f5f9` | Toast |
| `--focus-ring` | `rgba(37,99,235,.20)` | `rgba(107,155,255,.28)` | 聚焦光晕 |

**对比度（实测，均 ≥4.5:1）**：Light `--accent`/白 = 5.17、`--ok`/白 = 5.02、`--err`/白 = 4.83；Dark `--accent`/`#0d1117` = 7.18、`--ok` = 7.95、`--err` = 6.82。

**禁止**：用 `opacity` / `filter` 做悬停反馈；二维码（`#qr-img`、`.about-donate-img`）必须 `background: #fff`，深色下不反色。

---

## 3. 间距 / 圆角 / 阴影

- 网格 4 / 8px；间距序列 4·8·10·12·14·16·18·20·26。
- `--sidebar-w: 228px`；`--content-max: 860px`；`--main-pad-x: 28px`（移动 14px）。
- 圆角：`--radius: 12px`（卡片/弹窗）、`--radius-sm: 8px`（按钮/输入/行）、`--radius-pill: 999px`（芯片/徽章/开关）。
- 阴影：Light `--shadow: 0 1px 3px rgba(15,23,42,.06), 0 1px 2px rgba(15,23,42,.04)`；`--shadow-up: 0 -4px 16px rgba(15,23,42,.07)`；`--shadow-lg: 0 18px 44px rgba(15,23,42,.18)`；Dark 用黑底对应值。
- `.card`：`padding: 18px 20px`（移动 16px 15px），卡片间距 14px；`.card-title` 下 12px 且 `min-height: 28px`（容纳 `?` 不跳动）。
- `.switch-row`：上下 12px，行间 1px，末行无底线；`.field` 上间距 14px。

---

## 4. 信息架构（一级 / 二级分明）

| 一级 | 二级 | data-page | 内容 |
| :-- | :-- | :-- | :-- |
| 概览 | — | `overview` | 当前音源 · 后台组件状态 · 服务连通性检查 · 洛雪摘要（条件显示） |
| 音乐源 | — | `source` | 三选一主音源 + 各音源子面板（扫码登录 / 平台多选 / 洛雪脚本与超时） |
| 播放与推荐 | — | `quality` | 在线播放音质（7 档） · APP下载音质（8 档） · 首页推荐歌单与公共歌单 |
| 储存 ▾ | 边听边存 / 目录设置 | `tee` / `dirs` | 自动保存开关 + 高级缓存入库参数 + 清理垃圾文件；下载目录 / 归类 / 落盘方式 / 缓存目录 |
| 高级 ▾ | 搜索与直连 / 洛雪播放策略 / 大模型推荐 | `search` / `lx` / `llm` | 深分页与 302 直连；自动切换与源顺序；AI 用途、接口、高级参数、用量账本 |
| 设置 | — | `settings` | 界面外观（跟随系统 / 浅色 / 深色） |
| 关于 | — | `about` | 版本、反馈、日志导出、免责声明、打赏 |

**一级折叠**：点标题只展开或收起，不跳页、不标脏；状态存 `localStorage['fnmusic-nav-<group>-expanded']`，首访收起（`aria-expanded=false` + 子容器 `hidden`）。切入子页自动展开并高亮父级：`tee|dirs` → 储存；`search|lx|llm` → 高级（`#nav-*-title` 与 `#bottom-*-trigger` 加 `.active`）。

**卡片内折叠**：`<details class="adv-fold"><summary>高级：…</summary><div class="adv-body">…</div></details>`，默认收起；summary 13px/600/`--muted`，右侧 `▾` 旋转，展开后顶部 1px 虚线。**禁止**在折叠内部动态增删 `hidden`（表单采集依赖元素恒在）。

---

## 5. 文案规范

1. 用「用户能得到什么」命名，不用后端术语：落盘方式 → **文件写入模式**；单次解析超时 → **单次取链超时**。
2. 每个开关：主文案 ≤12 字 + 副文案 ≤26 字，讲「会发生什么」。
3. 危险操作必须写清后果（如清理垃圾文件可能误删且不可恢复）。
4. 固定叫法（测试与用户习惯）：`APP下载音质`、`清理垃圾文件（Beta）`、`音乐源（三选一）`、`边听边存`。
5. 标点用中文全角；并列用 `·`；数值范围用 `–`。

### 命名对照（旧 → 新）

| 旧 | 新 |
| :-- | :-- |
| 网易云音乐盒子 | 网易云音乐 |
| musicdl 聚合音源 | 多平台聚合 (musicdl) |
| 音乐页显示网易账号歌单 | 同步「我创建的歌单」 |
| 网易云歌单常驻 | 切换其它音源时仍保留网易歌单 |
| 启用平台（多选） | 选择启用的音乐平台（可多选） |
| lx音乐源列表 | 已添加的洛雪音源 |
| 超时设置 | 解析与探活超时（折叠：高级：超时参数） |
| 音质偏好 | 在线播放音质 |
| 推荐与公共歌单 | 首页推荐歌单 |
| 公共歌单 | 启用「精选公共歌单」 |
| 进程状态（按需加载：…） | 后台组件状态（说明移入 `?`） |
| 服务健康 | 服务连通性检查 |
| 洛雪自定义源（概览卡） | 洛雪源运行摘要 |
| 保存与素材 | 自动保存设置 |
| 关闭时滚动缓存 | 关闭边听边存时的临时缓存曲数 |
| 官方绑定等待（秒） | 飞牛曲库收录等待上限（秒） |
| 切歌续传并行数 | 切歌后后台继续下载任务数（0 = 切歌即停） |
| 曲库重扫接口（选填） | 自定义曲库扫描接口（选填） |
| 下载目录 | 歌曲下载目录 |
| 存放方式(beta) | 文件夹归类方式（Beta） |
| 落盘方式 | 文件写入模式（落盘方式，折叠） |
| 歌曲缓存目录 | 临时缓存目录 / 试听与转码缓存路径 |
| 搜索深分页 | 开启搜索自动深分页 |
| 搜索超时时间 | 单次搜索等待上限（秒，折叠） |
| 深分页页数上限 | 自动翻页最大页数（折叠） |
| 播放直连（beta） | 播放直连（302 重定向 · Beta） |
| lx音乐源（子页） | 洛雪播放策略 |
| Base URL / API Key / 模型 | 接口地址 (Base URL) / 接口密钥 (API Key) / 模型名称 (Model) |
| 大模型用量与记录 | Token 用量统计与调用记录 |
| 外观 | 界面外观 |

---

## 6. 组件规范

| 组件 | 规格 |
| :-- | :-- |
| `.btn` | 高约 36px（`padding: 8px 15px`），8px 圆角，13.5px/500；移动 `min-height: 44px` |
| `.btn.primary` | `--accent` 底 + `--on-accent` 字；悬停只加 `inset 0 0 0 1px var(--on-accent)` |
| `.btn.danger` | 文字 `--err`；悬停实心 `--err` + `--on-accent` |
| `.input` | 36px 高、8px 圆角；聚焦 `border-color: --accent` + `0 0 0 3px --focus-ring` |
| 开关 | 原生 checkbox 加 `appearance:none` → 40×22 胶囊 + 18px 白滑块；开 `--accent`、关 `--dot-idle`；DOM 与 `checked/disabled` 不变 |
| `.radio-list > label` | 11px 13px 内边距、8px 圆角；`:has(input:checked)` → `--accent` 边 + `--accent-soft` 底；`.radio-grid` 自适应两列（≥210px） |
| `.chip` | 胶囊 12.5px；`.on` → `--accent` 边 + `--accent-soft` 底 + 600 |
| `.badge` | 胶囊 12px/600；`.dim` `.ok` `.warn` 变体；`.new` 红底供 NEW |
| `.help-btn` | 视觉 22×22、`margin: 3px`、圆形、`--surface-2` 底；`::after` 撑 44×44 热区；悬停转 `--accent-soft` |
| `.adv-fold` | 上外距 14px，`--surface-2` 底，1px 边，8px 圆角 |
| `.save-bar` | 仅 `.show` 时上滑显示；左右 padding 用 `max(...)` 与内容列右缘对齐 |
| `.modal-mask` | 居中 560px；移动端贴底 + 顶部圆角 + `env(safe-area-inset-bottom)` |
| Toast | 顶部居中，3.8s 自动消失；`ok`/`fail` 用语义底 + `--on-accent` |

---

## 7. 布局与响应式

### 桌面（>760px）

- `.sidebar` 228px，`position: sticky; height: 100dvh`，右侧 1px 分隔；上品牌、中导航、底当前音源。
- 二级菜单左侧 1.5px 竖线 + 10px 内缩，视觉缩进约 34px，一二级层次一眼可辨。
- `.main`：`flex:1; max-width: var(--content-max); margin: 0 auto; padding: 26px 28px 104px`。所有页面共用同一列宽，切页不跳宽。

### 移动（≤760px）

- 隐藏侧栏；`.main` 内边距 `max(14px, env(safe-area-inset-top)) 14px calc(56px + 84px + env(safe-area-inset-bottom))`。
- `.bottom-nav`：高 `56px + safe-area`，毛玻璃；每项竖排「19px 图标 + 10.5px 标签」，激活态为透明底 + 强调色文字（不用实心块，避免拥挤）。
- 7 个一级入口：概览 / 音源 / 播放 / 储存▾ / 高级▾ / 设置 / 关于；储存与高级向上弹浮层，子项 ≥42px 高，支持外点与 Escape 关闭并回焦。
- 触控：按钮 ≥44px；`.lx-item` 竖排、按钮右对齐；`.provider-grid` 与 `.radio-grid` 单列。

---

## 8. 测试契约（改样式前必读）

由 `webui-service/test_frontend_theme.py`、`test_webui.py`、`test_app_js.js` 静态锁定：

1. `theme-boot.js` 同步且位于 `style.css` 之前，`app.js` 最后；三者均不得带 `async/defer/type`。
2. `button[data-page=settings]` 恰好 2 个（桌面 + 移动）；`input[name=theme]` 顺序为 `auto, light, dark`。
3. `:root` 与 `:root[data-theme=dark]` 必须成对声明全部 Token；CSS 中 `var(--x)` 引用的变量在 `:root` 均有定义。
4. `.main` 只允许 2 处声明（桌面主规则 + 移动媒体查询）；`.save-bar` 首块须含 `--content-max`、`--sidebar-w`、`padding-left/right: max(`；移动块 `.save-bar` 须含 `padding: 10px 14px`。
5. `.nav-item.active`、`.btn.primary`、`.btn.danger:hover`、`.badge.new`、`.toast.ok`、`.toast.fail` 必须 `background: var(--…)` + `color: var(--on-accent)` 且对比度 ≥4.5；`.btn.primary:hover`、`.badge.new:hover` 不得出现 `background/color/opacity/filter`（`color: var(--on-accent)` 例外）。
6. `.help-btn` 必须含 `width: 22px`、`height: 22px`、`margin: 3px` 与 44px 热区；`.usage-table` 必须 `border-collapse: collapse` 与 `tabular-nums`。
7. `#qr-img`、`.about-donate-img` 必须 `background: #fff`；微信码 DOM 在支付宝码之前，两个图片 id 均保留。
8. 任何初始带 `hidden` 的元素，若其 class 声明了非 none 的 `display`，必须补 `.cls[hidden]{display:none}`（集中在 style.css「hidden 兜底」块）。
9. 固定字符串：`<h2><span>大模型</span><button class="help-btn"`、`aria-label="APP下载音质帮助"`、`<p class="hint">测试功能，如遇播放异常可以尝试关闭</p>`、`id="cleanup-modal" hidden`、`aria-describedby="cleanup-warning"`、清理确认原文、`id="llm-liked-settings" hidden`、`id="llm-api-warning" role="status" hidden`、七档音质名（标准/较高/极高/无损/Hi-Res/超清母带/沉浸声）、`data-page="lx"`、`id="tee-save-mode"` 三值且与 `#dir-download` 同卡片、`scan-delay`/`scan-cooldown` 及其 label。
10. 大模型帮助须包含短语：不会自动启用、首次被客户端获取、不会到期或轮换、本地有效、未收藏候选至少 20 首、停止生成且不展示猜你喜欢。

---

## 9. 变更流程

1. 改 `style.css` 先动 Token 再动组件；深色只覆盖取值，不复制结构。
2. 新增页面：`<section class="page" id="page-xxx">` + `data-page` 入口 ×2（桌面侧栏 + 移动底栏，或并入对应分组）。
3. 新增设置项：常用开关放卡片主区；阈值、协议、并发、模式等一律进 `<details class="adv-fold">`，并在 `?` 里解释。
4. 改完必须跑：`node webui-service/test_app_js.js`（摘要行应为 `, 0 failed`）与 `python -m pytest webui-service -q`。
5. 主题联动：`sdk.getPlatformConfig()` 取初值、`sdk.$on('os/theme')` 只在 `/app/` 且 `isWeb && !isStandaloneWeb` 时注册；改动主题逻辑不得破坏该降级路径。
