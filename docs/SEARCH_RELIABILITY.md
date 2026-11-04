# 搜索可靠性（在线歌曲）诊断与回归

> 适用版本：v2.12.0-4+。本文记录 2026-11-04 对「搜索时有时只返回本地歌曲」的根因分析、
> 修复方案，以及配套的集成/诊断脚本。

## 1. 现象

用户在 fnOS 音乐搜索页输入关键词时， intermittently 只显示本地歌曲，没有在线歌曲。
代理日志出现：

```
musicdl search partial errors: {'KuwoMusicClient': 'timeout after 8.0s', ...}
circuit breaker open
```

## 2. 根因

1. **源白名单过宽**：`FNMUSIC_ONLINE_SOURCES` / `MUSICDL_SOURCES` 默认写入全部 11 个
   musicdl 平台。CT201 仅 2 核，11 个 worker 进程同时 import musicdl + 搜索 + 探活，
   启动阶段就占满 CPU，单源预算 8s 被完全耗尽（worker 启动 ~5s + 搜索 ~6s）。
2. **自适应超时地板过低**：`MUSICDL_ADAPTIVE_MIN_TIMEOUT` 默认 8s，且 `hardening.py`
   的 `slow_degrade_s` 默认 8s，导致「成功但偏慢」的源（如咪咕 6-11s）被误判为故障，
   进一步压低下一次预算。
3. **熔断器冷却过长**：`MUSICDL_BREAKER_COOLDOWN` 默认 120s，一次全源超时就让所有
   源在 2 分钟内持续返回 `circuit breaker open`，重试无效。
4. **代理把「主动跳过慢源」当作失败**：musicdl 的 `skipped (grace elapsed)` 被代理
   的 `hard_failure` 逻辑计入，导致 `entry["partial"]=True`、搜索结果不落缓存，
   并在每次搜索时打 `WARNING`。
5. **`trim.music` 目录权限**：`/usr/local/apps/@appdata/trim.music` 为 `770 root:root`，
   代理（uid=1000）无法读取 `music.db`，导致 `liked user discovery failed`，
   影响 liked/歌单列表注入。

## 3. 修复

| 文件 | 改动 |
| --- | --- |
| `musicdl-service/app.py` | `breaker_threshold` 4→6；`breaker_cooldown` 120→30；`slow_degrade_s` 8→15；`adaptive_min_timeout` 8→15 |
| `proxy/app.py` | `search_timeout` 默认 15→30；`fetch_musicdl_search` 过滤 `skipped ` 前缀的错误，避免误判 |
| `webui-service/app.py` | `FNMUSIC_SEARCH_TIMEOUT` 默认 15→30 |
| `webui-service/static/app.js` | 回填/提交默认 15→30 |
| `webui-service/test_webui.py` | 默认值断言 15→30 |
| CT201 `.env` | `FNMUSIC_ONLINE_SOURCES`/`MUSICDL_SOURCES` 全 11 源 → `KuwoMusicClient,MiguMusicClient`；`FNMUSIC_SEARCH_TIMEOUT` 15→30 |
| CT201 文件系统 | `/usr/local/apps/@appdata/trim.music` 权限 770→755 |

推荐源白名单为酷我 + 咪咕（代码默认），其余平台可在 WebUI「搜索设置」中按需启用，
但 2 核环境下启用过多平台会导致首屏超时。

## 4. 集成 / 诊断脚本

| 脚本 | 位置 | 用途 |
| --- | --- | --- |
| `musicdl_search_reliability.py` | `tests/integration/` | CT201 实机：musicdl 双源/单源搜索返回在线结果、无熔断误触发 |
| `smoke_playlist_detail.py` | `tests/integration/` | CT201 实机：歌单详情端点 + 页面结构（variant 3 重构回归） |
| `measure_musicdl_concurrency.py` | `tests/integration/` | 诊断：并发 worker 启动/搜索时延基线 |

运行（在 CT201 上）：

```bash
python3 tests/integration/musicdl_search_reliability.py --musicdl-url http://127.0.0.1:8768 --repeat 2
python3 tests/integration/smoke_playlist_detail.py
```

在主机上通过 pct exec：

```bash
sudo -n pct push 201 fnos_music_ext/tests/integration/musicdl_search_reliability.py /tmp/musicdl_search_reliability.py
sudo -n pct exec 201 -- python3 /tmp/musicdl_search_reliability.py
```

## 5. 验证命令

```bash
# 1. musicdl 服务
sudo -n pct exec 201 -- curl -sS http://127.0.0.1:8768/sources | python3 -c 'import sys,json; print(json.load(sys.stdin)["enabled"])'
# 2. 代理配置
sudo -n pct exec 201 -- grep -E "search_timeout|online_sources" /vol1/@appcenter/fnmusic-ext/repo/logs/proxy.log | tail -2
# 3. 搜索（需用户鉴权，浏览器 F12 复制 authx）
curl ... /music/api/v1/search/track?q=周杰伦
```

## 6. 已知限制

- 咪咕单源搜索需要 ~11s（搜索 + 探活），在 2 核高负载时可能逼近 15s 预算；
  若咪咕预算耗尽，酷我仍可单独返回结果。
- 熔断器为进程内状态，重启 musicdl 容器会重置。
- 代理搜索缓存 TTL 为 10s（空结果）/ 30s（部分结果）/ 最多 5min（完整结果），
  调试时注意缓存影响。
EOF
echo done
## 7. 平台批量测试（WebUI）

WebUI 提供「批量测试平台」功能，对 musicdl 全部注册平台执行有效性与速度测试：

- 入口：管理页 → 音源选择 → musicdl 平台卡片 → 「批量测试平台」按钮
- 端点：`POST /api/platforms/test/start`（启动）+ `GET /api/platforms/test/status?job=<id>`（轮询）
- 结果表格：平台、状态（✓/✗）、耗时、结果数、示例标题
- 一键操作：「启用成功的 (N)」「启用全部」「停用全部」（标记 dirty，保存时写入 `.env`）

### CLI 诊断

```bash
# 对全部注册平台运行测试（并发 4，~2 分钟）
python3 tests/integration/musicdl_search_reliability.py --batch
```

实测结果（CT201，周杰伦关键词）：13/57 可用，最快 Kuwo 3.7s，最慢 Yinyuedao 15.0s。
