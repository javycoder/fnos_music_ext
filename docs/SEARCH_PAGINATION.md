# 搜索分页机制（本地优先布局 + 在线深分页）

> 适用于 v2.7.0+。本文记录飞牛官方音乐搜索接口的分页契约、代理层的本地优先
> 全局布局，以及在线结果的深分页（增量取页）机制。

## 1. 飞牛官方搜索接口的分页契约（实测）

```
GET /music/api/v1/search/track?q=<关键词>&page=<页号>&size=<页大小>
```

| 参数 | 说明 |
| --- | --- |
| `q` / `keyword` / `query` | 关键词（三种别名等价，官方前端用 `q`） |
| `page` | 页号，**1 起始**，默认 1 |
| `size` | 显式提供时遵从请求；飞牛音乐 1.1.19 及以上且 `page`、`size` 均未提供时扩展默认 100，其余默认 50；部分列表接口另有约定 `size=-1` = 返回全部 |

各官方客户端的实际行为（1.1.19 网页 bundle 与 Android 1.1.1 静态取证 + NAS
日志）：

| 客户端 | 单曲 | 专辑/歌手/歌单 |
| --- | --- | --- |
| 网页版（1.1.19 实测 bundle） | **只发一次 `GET /search/track?q=…`，不带 `page/size`，也不请求第二页**（一次展示全部返回） | `page/size` 显式分页，`page=1` 起、`size=24`；返回满页且 `page*24 < total` 才续页 |
| Android App（1.1.1） | 同一套 `/search/*` 端点；AOT 内含四类路由常量，请求原生发起 | 同左 |

响应信封：

```json
{"code": 0, "msg": "OK", "data": {"list": ["<track VO>..."], "total": 123}}
```

- **没有 `hasMore` 字段**。`data.total` 提供总数，但客户端是否翻页还取决于自身实现：网页单曲不翻页，因此网页单曲的可控变量是**单次返回数量**（见第 5 节）。由于本地结果优先，若本地命中已占满窗口，网页不会显示后面的在线结果；显式分页的客户端不受这一网页限制。
- track VO 关键字段：`guid`（32-hex）、`title`、`artists[]`、`album{name,...}`、
  `audioSpec{duration(毫秒),format,...}`、`isFavorite`。

官方接口的两个实测怪癖（代理均已兼容，见 `proxy/app.py` `search_track`）：

1. **越界页回钳第 1 页**：`total=4` 时 `page=2` 仍返回同样 4 条（搜索接口特有，
   收藏/歌单接口无此行为）。本页全局起点越过本地段时代理必须清空官方回声，
   否则官方条目会拼上在线切片在每个后续页重复出现。`/search/album` 显式分页
   同样按此防御（`_search_album_paged`）。
2. **忽略 size 全量返回**（2026-09-25 官方更新实测）：`total=11`、`size=10` 时
   `page=1/2` 均返回 11 条。代理按请求窗口对本地段原地切片。

## 2. 本地优先全局布局

代理把本地（官方透传）与在线（音源聚合）结果拼成一个**全局虚拟列表**：

```
[ 本地段 0 .. local_total ) [ 在线段 local_total .. local_total + len(items) )
```

- 本页全局区间 = `[(page-1)*size, page*size)`；本地段、在线段分别与该区间求交集。
- 例：一页 50 条、本地命中 30 条 → 第 1 页 = 30 本地 + 20 在线；第 2 页起 =
  纯在线 50 条，如此类推。**总量 ≤ size 时第 1 页装下全部，天然不分页。**
- 在线池（`entry["items"]`）只追加不重排：同一 `(page, size, local_total)` 的
  切片稳定，已返回页的前缀永不移动（跨页零重复的根基）。
- `data.total = 官方 total + 在线去重条数`；支持分页的客户端可据此决定继续翻页，实际行为由客户端实现决定。
- 在线段内部顺序固定为：网易 > musicdl > 洛雪（聚合序）。

## 3. 在线深分页（v2.7.0）

首屏聚合只向各音源取第一页（网易 50 / musicdl ≤30 / 洛雪 ≤20×平台数）。
翻页越过在线池时，**深分页**自动向未取尽的源增量取下一页把本页在线段填满：

- 触发：本页在线段末尾 `page*size - local_total` 超过池长时，`_wait_deep_pages`
  创建/加入增量任务，预算内（`min(search_timeout, 10s)`）等待填满；到点没填满
  就交现有切片（短页自愈——total 仍大于客户端已见条数，下次翻页继续触发）。
- 预取：每次响应前若下一页在线段未填满，后台先取一轮，连续翻页零等待。
- 取尽判定：某源本轮返回空列表、或去重后对池 0 新增（上游开始重复自身）。
- 封顶：`FNMUSIC_SEARCH_DEEP_MAX_PAGES`（默认 10，含首屏）防止无限翻页打爆上游。

各音源的分页能力与接入方式：

| 音源 | 深分页 | 机制 |
| --- | --- | --- |
| 网易（musicbox） | ✅ | `/api/v1/search` 新增 `offset` 参数；CLI 无 offset，`offset>0` 直接走官方 web 搜索接口（`api/search/get/web`，原生 offset 分页） |
| 洛雪（lxmusic） | ✅ | `/api/v1/search` 新增 `page` 参数，透传到五平台上游分页参数：kg `page`、wy `offset`、mg `pageNo`、tx `page_num`、kw `pn` |
| musicdl | ❌ | musicdl 库 `search()` 无分页参数（`search_size_per_source` 为页大小且封顶 10 保延迟），只参与首屏聚合 |

配置（均可热重载、管理页「搜索」页可改）：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `FNMUSIC_SEARCH_DEEP_PAGE` | `true` | 深分页总开关；关闭后在线结果只显示首屏聚合部分 |
| `FNMUSIC_SEARCH_DEEP_MAX_PAGES` | `10` | 每个关键词向单个音源最多取多少页（1-50） |
| `FNMUSIC_WEB_SEARCH_RESULT_LIMIT` | `50` | 网页版单曲搜索一次返回的合计上限（1-200），见第 5 节 |

已知边界：kw 平台搜索后按标题相关度页内重排，跨页顺序可能轻微穿插（代理
去重兜底）；各平台页内过滤（VIP/试听剔除）可能造成页边界少量缺条——上游
原始流按页号无缝拼接，已是该架构下的最优解。

## 5. 网页版单曲窗口（issue #139）与专辑显式分页

### 5.1 Web 单曲返回上限

官方网页单曲搜索不翻页、一次展示接口返回的全部结果，所以可控变量是**单次
返回数量**：`FNMUSIC_WEB_SEARCH_RESULT_LIMIT`（1-200，默认 50，热重载）。它只
作用于「网页版且未显式分页」的单曲搜索，上限是**本地 + 在线的合计**（本地优
先布局不变），不是保证数量——音源超时/不足时允许短页。识别逻辑
（`_is_unpaged_web_track_search`，只做窗口选择、不作安全边界）：

1. 排除原生 App（Dart/Flutter UA）与任何显式 `page`/`size` 参数；
2. 首选 Fetch Metadata（浏览器 fetch 必带 `Sec-Fetch-*`）；
3. 无 Fetch Metadata 时要求浏览器 UA 且 `Referer` 指向官方 `/music` 页面；
   反代丢头或证据不足一律走原有版本默认（1.1.19+ 为 100，其余 50）。

诊断日志 `search_finished` 带 `window_policy=web_config|explicit|legacy_default`，
可用于发现反代丢头或误分类。网页无参请求不触发下一页预取（它不会翻页），
被新关键词取代时官方回声同样截到窗口内。显式分页请求与 App 完全不受该配置
影响。**尚未在真实浏览器/实机上逐端录制请求矩阵前，不要把「网页已显示全部
结果」当成已验证结论**（见第 6 节验证边界）。

### 5.2 专辑显式分页

官方网页专辑 Tab 发 `page=1` 起、`size=24`，满页且 `page*24 < total` 才续页。
带 `page`/`size` 的 `/search/album` 走 `_search_album_paged`：

- 布局与单曲一致：`[官方 0..official_total) [在线 official_total..+len(pool))`，
  本页 = 请求窗口与两段的交集（`playlistsearch.online_bounds`）；
- `data.total = official_total + len(pool)`，在线池最多 10 个（`_ALBUM_SEARCH_LIMIT`，
  不向第三方取第二页），同一搜索的池做 120s 快照缓存（失败/空 5s、至多 8 个
  并发构建、等待上限 10s），跨页内容与顺序稳定、按 guid 去重；
- 官方短页且仍有官方后续（异常形态）时原样交付本页，不用在线条目冒充；
- `size=-1`、非法显式值或未知分页形态直接透传官方响应，不在线扩展；
- 无 `page/size` 的请求保留旧行为（官方结果后追加一批在线专辑、同名去重）。

## 6. 相关测试

- `proxy/tests/test_merge.py`：`test_search_track_deep_pagination_user_scenario`
  （30 本地 + size=50 的逐页构成）、`test_search_track_deep_pagination_disabled`、
  `test_search_track_single_page_under_size`、既有本地优先布局/越界钳制用例。
- `proxy/tests/test_search_119.py`：网页无参窗口（配置上限、浏览器证据要求、
  显式分页不受影响、被取代回声截断）。
- `proxy/tests/test_album.py`：专辑显式分页 24/24/11 布局、越界钳制、非法参数
  透传、短页降级、在线池跨请求复用。
- `proxy/tests/test_search_artist.py`：歌手搜索原样透传（查询与鉴权头不打扰）。
- `proxy/tests/test_lxmusic.py`：`test_search_track_deep_pagination_lx_page_passthrough`。
- `proxy/tests/test_musicbox_service.py`：`test_search_offset_routes_to_web_fallback`。
- `scripts/e2e_check.sh`：实机 page=1/2/3 跨页零重复校验。

### 验证边界（截至 2026-11-19）

- 已验证：1.1.19 网页 bundle 的请求形态（单曲单次、其他三类 page/size 分页）、
  Android 1.1.1 AOT 内四类搜索路由常量、NAS 日志中 Dart UA 请求四类搜索均
  HTTP 200 且代理按 100 窗口交付（其中一次 2 本地 + 98 在线）。
- 未验证：真实浏览器经反代的请求头保留情况、Android App 是否对四类搜索发起
  第二页请求、UI 实际渲染条数。上线后以 `window_policy` 日志与实机抽查补齐，
  未验证前不得宣称「双端翻页已适配完成」。
