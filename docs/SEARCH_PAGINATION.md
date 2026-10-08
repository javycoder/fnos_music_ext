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
| `size` | 页大小，默认 **50**；部分列表接口另有约定 `size=-1` = 返回全部 |

响应信封：

```json
{"code": 0, "msg": "OK", "data": {"list": ["<track VO>..."], "total": 123}}
```

- **没有 `hasMore` 字段**。官方客户端是否继续翻页完全由 `data.total` 驱动。
- track VO 关键字段：`guid`（32-hex）、`title`、`artists[]`、`album{name,...}`、
  `audioSpec{duration(毫秒),format,...}`、`isFavorite`。

官方接口的两个实测怪癖（代理均已兼容，见 `proxy/app.py` `search_track`）：

1. **越界页回钳第 1 页**：`total=4` 时 `page=2` 仍返回同样 4 条（搜索接口特有，
   收藏/歌单接口无此行为）。本页全局起点越过本地段时代理必须清空官方回声，
   否则官方条目会拼上在线切片在每个后续页重复出现。
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
- `data.total = 官方 total + 在线去重条数`，客户端据此继续翻页或停页。
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

已知边界：kw 平台搜索后按标题相关度页内重排，跨页顺序可能轻微穿插（代理
去重兜底）；各平台页内过滤（VIP/试听剔除）可能造成页边界少量缺条——上游
原始流按页号无缝拼接，已是该架构下的最优解。

## 4. 相关测试

- `proxy/tests/test_merge.py`：`test_search_track_deep_pagination_user_scenario`
  （30 本地 + size=50 的逐页构成）、`test_search_track_deep_pagination_disabled`、
  `test_search_track_single_page_under_size`、既有本地优先布局/越界钳制用例。
- `proxy/tests/test_lxmusic.py`：`test_search_track_deep_pagination_lx_page_passthrough`。
- `proxy/tests/test_musicbox_service.py`：`test_search_offset_routes_to_web_fallback`。
- `scripts/e2e_check.sh`：实机 page=1/2/3 跨页零重复校验。
