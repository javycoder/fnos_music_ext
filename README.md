# fnmusic-ext 飞牛音乐扩展代理

`fnmusic-ext` 是专为 fnOS（飞牛私有云）自带音乐应用（`trim.music`）量身定制的无侵入式扩展代理。通过 Unix Domain Socket 接管官方后端入口，为飞牛原生客户端无缝提供全网在线聚合搜索、流式播放、歌词与封面解析、边播边落盘，以及基于大模型的每日推荐歌单。

可选音源支持：[musicdl](https://github.com/CharlesPikachu/musicdl)（酷我/咪咕等）、[musicbox](https://github.com/darknessomi/musicbox)（网易云高品质）。本项目完全不修改飞牛官方 nginx 配置、不 Patch 官方二进制、不改动官方数据库。

> ⚠️ **使用前须知**：本项目基于 MIT 协议开源，仅供个人技术研究与交流使用，请务必阅读文末的 [免责与版权声明](#免责与版权声明disclaimer--copyright-notice)。

---

## 目录

- [大概实现原理（工作原理）](#大概实现原理工作原理)
  - [1. 核心目的：为何要零侵入扩展](#1-核心目的为何要零侵入扩展)
  - [2. 整体架构与 Inode 接管机制](#2-整体架构与-inode-接管机制)
  - [3. 数据流转与接口拦截链路](#3-数据流转与接口拦截链路)
  - [4. 容灾防护与降级设计](#4-容灾防护与降级设计)
- [核心扩展特性](#核心扩展特性)
  - [1. 多源快速聚合搜索与渐进式翻页](#1-多源快速聚合搜索与渐进式翻页)
  - [2. 在线流式播放与独立后台 Task Tee 边播边落盘](#2-在线流式播放与独立后台-task-tee-边播边落盘)
  - [3. 本地与在线歌曲收藏合并（多用户隔离）](#3-本地与在线歌曲收藏合并多用户隔离)
  - [4. 歌词与封面代理转发与智能缓存](#4-歌词与封面代理转发与智能缓存)
  - [5. 每日推荐（Daily Recommend）](#5-每日推荐daily-recommend)
- [目录结构](#目录结构)
- [环境变量配置](#环境变量配置)
- [安装](#安装)
- [标准运维指南](#标准运维指南)
  - [一键启用扩展 (extend.sh)](#1-一键启用扩展)
  - [一键还原官方模式 (restore.sh)](#2-一键还原官方模式)
  - [健康检查与状态探测](#3-健康检查与状态探测)
  - [日志排障与调试](#4-日志排障与调试)
  - [本地代码与测试验证](#5-本地代码与测试验证)
- [免责与版权声明（Disclaimer & Copyright Notice）](#免责与版权声明disclaimer--copyright-notice)
  - [1. 技术研究与非商业用途](#1-技术研究与非商业用途)
  - [2. 致谢上游开源项目与无侵权声明](#2-致谢上游开源项目与无侵权声明)
  - [3. 音频及视听数据版权归属](#3-音频及视听数据版权归属)
  - [4. 免责与使用者风险自担](#4-免责与使用者风险自担)

---

## 大概实现原理（工作原理）

### 1. 核心目的：为何要零侵入扩展

飞牛私有云（fnOS）自带的音乐应用（`trim.music`）界面设计现代美观、交互流畅，并且与系统的用户权限体系和 NAS 底层存储深度打通，是私有云用户管理音频的首选。

然而，官方原生音乐应用的定位更偏向于**“本地媒体库播放器”**：
- **曲库受限与冷启动门槛**：对于没有长期积累本地无损曲库（FLAC/MP3）的新用户，打开应用空空如也，缺少在线检索、试听与发现新歌的能力；
- **各平台音源受限分散**：主流音乐平台的版权分散且随时可能变动，单一平台难以满足全面的听歌需求。

如果要给飞牛音乐增加在线曲库，常见的侵入式改造存在严重的稳定性隐患：
1. **反编译/修改官方 Go 后端二进制（`trim-music`）**：难度极高且极为脆弱，飞牛系统 OTA 升级时会被官方二进制无情覆盖，甚至导致服务崩溃；
2. **篡改 Web 前端代码**：无法作用于手机 App（iOS/Android）或桌面客户端，且同样会在前端静态资源更新时丢失；
3. **逆向改写官方 SQLite 数据库**：并发读写极易引发数据库死锁损坏，造成用户私有媒体资产元数据丢失。

**`fnmusic-ext` 的零侵入解决思路**：
坚持**“不改官方一行代码、不改官方数据库、不改官方 nginx 配置”**。通过在 Linux 传输层接管 Unix Domain Socket，以透明反向代理的身份坐落于 nginx 与官方 Go 后端之间。本地请求 100% 透明透传，在线请求按需拦截扩展。飞牛官方的 Web 端、移动端 App、桌面端均**无需任何魔改或安装客户端插件**，开箱即拥有全网在线试听与 AI 推荐能力。

---

### 2. 整体架构与 Inode 接管机制

#### 系统架构拓扑

```text
┌─────────────────────────┐      (HTTPS: 5667 / 443)      ┌─────────────────────────┐
│  浏览器 / 移动端 App / 桌面端  ├──────────────────────────────►│       fnOS nginx        │
└─────────────────────────┘                               │        (/music)         │
                                                          └────────────┬────────────┘
                                                                       │ (proxy_pass http://unix:/var/run/trim_music.socket)
                                                                       ▼
                                                          ┌─────────────────────────┐
                                                          │    fnmusic-ext Proxy    │
                                                          │(/var/run/trim_music.sock)
                                                          └───┬─────────────────┬───┘
                                                              │                 │
                                                (1) 本地透传与合并 │                 │ (2) 在线聚合取流与元数据
                                                              ▼                 ▼
                                    ┌───────────────────────────┐ ┌───────────────────────────┐
                                    │ fnOS Go 后端 (trim-music) │ │ 外部音源扩展集群          │
                                    │(/var/run/trim_music_      │ ├───────────────────────────┤
                                    │        upstream.socket)   │ │ • musicbox (:8770)       │ (网易云，可选)
                                    └───────────────────────────┘ │ • musicdl (:8768)        │ (酷我/咪咕，可选)
                                                                  └─────────────┬─────────────┘
                                                                                │
                                                                                ▼ (独立后台 Task Tee 边播边落盘)
                                                                  ┌───────────────────────────┐
                                                                  │ 音乐库 / cache 缓存落盘    │
                                                                  │ (*.flac / *.mp3 / *.lrc)  │
                                                                  └───────────────────────────┘
```

#### 为什么坚决不改动 nginx 配置文件？

实机排查与测试表明，fnOS 部署了高优先级的**系统配置管理守护进程**。每当系统发生网络切换、证书更新、应用安装卸载、Docker 容器变动或系统定时巡检时，守护进程会无条件根据官方配置模板，全量重写 `/usr/trim/nginx/conf/` 整棵配置目录（含 `nginx.conf` 和 `conf.d/*`）。

任何手工或脚本对 nginx 配置的改动，都会在秒级至小时级内被官方模板全量回滚。因此，直接篡改 nginx 反代配置在生产环境中完全不可行。

#### Unix Socket Inode 重命名接管原理

飞牛官方后端架构中，nginx 对 `/music` 路径的反向代理配置指向一个 Unix Domain Socket：
`proxy_pass http://unix:/var/run/trim_music.socket;`

在 Linux 操作系统内核中，Unix Domain Socket 的监听与通信是绑定在虚拟文件系统（VFS）的 **Inode（索引节点）** 对象上的，而不是由文件路径字符串直接驱动：
1. **透明重命名（Inode 转移）**：
   ```bash
   mv /var/run/trim_music.socket /var/run/trim_music_upstream.socket
   ```
   重命名仅仅改变了文件系统目录项的指针，原官方 Go 后端（`trim-music`）在内核中持有的监听套接字句柄和 Inode 完全没有发生变化，依然可以平稳正常地 `accept` 新连接。
2. **代理原位接管**：
   扩展代理（基于 FastAPI / Uvicorn）立即在原路径 `/var/run/trim_music.socket` 建立新的 Unix Domain Socket，并执行 `chmod 666` 赋权，确保运行在 `www-data` 用户下的 nginx worker 进程拥有完整的读写权限。
3. **零侵入透明代理链路达成**：
   此时形成级联链条：`Client -> fnOS nginx -> fnmusic-ext proxy -> 官方 trim-music / 外部音源`。系统 nginx 配置与官方二进制完全无需任何补丁。

#### 系统重启与服务自愈机制（为什么重启会自愈？）

- **官方服务自愈保障**：当系统重启，或者管理员执行 `systemctl restart trim-music` 重启官方服务时，官方 Go 二进制的初始化逻辑会主动 `unlink` 原生路径 `/var/run/trim_music.socket` 并重新创建绑定。
- **天然保底**：此时，即使扩展代理服务尚未启动或异常退出，nginx 的请求也会直接流入官方 Go 后端，系统自动恢复为飞牛出厂的原生直连状态，**绝对不会因为扩展代理异常而导致用户本地音乐无法播放**。
- **智能幂等再接管**：扩展代理的启动守护脚本 `run_proxy.sh` 具备状态感知与探针能力。每次启动时会自动检测原位 socket 与 upstream socket 的归属与健康状态，仅在检测到官方后端健康就绪时平滑重命名并安全接管。

---

### 3. 数据流转与接口拦截链路

扩展代理坐落于核心通信链路后，将所有 HTTP 请求分类并按照预设的业务流水线进行智能分流：

```text
客户端请求 (/music/api/v1/...)
       │
       ├─► 搜索请求 (/search/track) ──► 鉴权快判 ──► 双轨并行 (本地Go + 在线音源) ──► 2.5s快返回 + 异步聚合缓存
       │
       ├─► 播放请求 (/track/stream) ──► 本地曲目直传上游 / 在线曲目 Task Tee 管道 ──► 边播边下 + 标签注入与Sidecar
       │
       ├─► 收藏请求 (/favorite-track/*) ► 本地曲目直传上游 / 在线曲目按用户GUID落盘 ──► 多用户隔离 + 列表倒序动态合并
       │
       ├─► 歌词与封面 (/lyric/*, /static/cover) ──► 优先命中Sidecar歌词缓存 / 在线封面 302 CDN 重定向
       │
       └─► 歌单请求 (/playlist/*) ──► 注入虚拟「每日推荐」歌单 ──► 口味画像 + LLM候选 + 在线可用性验证
```

#### ① 搜索聚合与双层缓存（Fast Return & Background Aggregation）
- **接口拦截**：`/music/api/v1/search/track?keyword=...`
- **鉴权快速通道**：代理层先透传上游进行鉴权，若用户未登录或 Token 无效（HTTP 401），直接毫秒级返回鉴权失败，绝不浪费算力等待外部音源。
- **本地与在线双轨并行**：鉴权通过后，并行请求飞牛官方 Go 后端（获取本地曲库）与已启用的在线音源（网易云 musicbox / 多源聚合 musicdl）。
- **第 1 页极速快返回（2.5 秒窗口）**：针对用户最关心的首屏结果，设置 2.5 秒的快速等待窗口（`FNMUSIC_NETEASE_WAIT_S`）。主音源网易云若在 2.5s 内返回，立即将本地曲目与主音源结果合并响应给前端，告别加载转圈。
- **后台 Task 异步聚合**：副音源与主音源耗时较长的其余结果，移交后台 `asyncio.Task` 协程继续并发抓取。多音源返回的数据按 `(title, artist)` 统一小写清洗去重（网易云无损音源优先），写入内存 LRU 搜索缓存池（TTL 默认 300 秒）。
- **翻页瞬时命中**：当用户在客户端滚动浏览翻至第 2 页及以后时，直接从内存缓存池做切片返回，翻页操作丝滑秒开。

#### ② 歌曲播放与独立后台 Task Tee 边播边落盘（Tee Streaming & Safe Cache）
- **接口拦截**：`/music/api/v1/track/play-url` 与 `/music/api/v1/track/stream`。
- 本地曲目直接透传官方 Go 后端；在线曲目（`online:*` 格式）由代理层调度解析。
- **解决断连孤儿残留的独立后台 Task Tee 架构**：
  - *传统边播边下的缺陷*：传统实现往往将磁盘写文件与客户端 HTTP 连接直接挂钩。用户频繁切歌、拖动进度条或网络抖动断开时，HTTP 连接强行中断，下载进程被迫终止，在磁盘中留下大量未写完的 `.part` 孤儿碎片文件。
  - *本项目解耦设计*：采用独立后台 Task Tee 管道（基于 `asyncio.Queue` 内存队列）：
    1. 下载协程 `_downloader` 独立于客户端 HTTP 连接运行；
    2. 数据块通过内存 Queue 分发给 HTTP 响应流，透传 `206 Partial Content` 满足客户端即点即播与进度拖拽；
    3. 独立的后台异步 Task 持续将完整音频流写入带 UUID 隔离的临时文件（`.<guid>.<hex>.part`）；
    4. 客户端断开连接不影响后台任务完整写入。
- **元数据标签注入与智能落盘**：
  - 后台写入完毕后，系统严格校验音频字节完整性（排除 `< 1KB` 的错误假流）；
  - 校验通过后，借助 `mutagen` 库自动将在线抓取的歌曲名、艺术家、专辑等 ID3/Vorbis 元数据标签写入音频二进制头，并原子重命名（`os.replace`）正式落盘；
  - 同步拉取并生成同名 `.lrc` Sidecar 歌词文件同目录存放；
  - 若下载不完整，后台任务会立刻主动执行 unlink 清理临时 `.part` 文件，绝不留存任何磁盘脏数据。

#### ③ 收藏夹拦截与多用户本地存储（Multi-User Favorites Merge）
- **接口拦截**：`/music/api/v1/favorite-track/create`、`delete`、`list` 等。
- **本地与在线分流**：本地曲目透传官方 Go 后端处理（持久化于官方 SQLite）；在线曲目（`online:*`）复用 `/music/api/v1/user/me` 接口解析当前登录用户的 GUID，按用户独立持久化存储在 `online_favorites/<user_guid>.json`，避免多家庭成员数据串扰。
- **严格适配前端契约**：飞牛 Web/App 前端对 Track Model 有严苛校验。代理层会自动为在线歌曲补全包含 `guid`、`title`、`duration`、`isFavorite: true`、`artists[]`、`album{}`、`audioSpec{}`、`accessStatus: 0`、`coverId` 等全量结构，杜绝客户端白屏。
- **动态合并与并发安全**：获取收藏列表时，将本地收藏与在线收藏按收藏时间倒序动态合并分页，并动态回传真实的 `total` 统计总数。写入过程全程采用“临时文件写入 + 原子重命名（`os.replace`）”以及 `asyncio.Lock` 协程锁，杜绝并发冲突。

#### ④ 歌词与封面代理（Lyric Sidecar & Cover Redirect）
- **歌词双级解析**（拦截 `/music/api/v1/lyric/list`、`track/lyrics`）：优先读取本地同名 `.lrc` Sidecar 缓存；未命中时异步向上游在线音源拉取 LRC 格式歌词，适配飞牛前端期望的行级载荷结构（`$n.lyric.list` / `preferred`），并自动持久化为本地 Sidecar 歌词文件。
- **封面极速重定向**（拦截 `/music/api/v1/static/cover`）：本地封面直接透传官方 Go 后端；在线封面解析对应音源平台的高清封面 URL，并通过 HTTP `302 Found` 重定向直接引导客户端从官方源站 CDN 下载图片，零占用 NAS 本地带宽与内存。

#### ⑤ 每日推荐生成（LLM + Hybrid Recommendation）
- **接口拦截**：`playlist/list`、`playlist/detail`、`playlist/batch-detail`、`track/playlist-detail/list` 等。
- **虚拟歌单注入**：在前端左侧「歌单」列表顶部动态注入名为「每日推荐」的专属虚拟歌单（`online:playlist:daily:YYYYMMDD:<user>`）。
- **口味画像种子生成**：提取飞牛只读数据库中用户最近播放记录（`play_history`）、在线播放历史以及本地与在线收藏歌曲作为口味种子，并自动过滤掉已收藏歌曲。
- **LLM 候选生成与平滑降级**：调用兼容 OpenAI 规范的大模型接口生成 30 首候选推荐曲目；若未配置 LLM 或调用超时，自动无缝降级为基于歌手种子的高可用检索策略。
- **可用性验证与缓存自洁**：将候选歌曲逐一送入外部音源进行实时可用性探测，凑满 20 首真正可播放的在线曲目并持久化为当天缓存；缓存每日一换，换日自动清理昨日历史，保证推荐常听常新。

---

### 4. 容灾防护与降级设计

为保障私有云系统的极致稳定性，`fnmusic-ext` 遵循 **Fail-Safe（失效安全）** 原则设计了多层防御与熔断机制：

1. **官方原生直连保底（Zero-Downtime Fallback）**：
   当扩展代理由于任何意外未运行或崩溃时，官方 `trim-music` 自动重新绑定原 socket 路径，飞牛本地音乐即刻回归原生运行状态，基础播放体验稳如磐石。
2. **严苛的超时阶梯与故障隔离（Timeout Cascading & Fault Isolation）**：
   - 官方上游探针超时：`2.0s`；
   - 网易云搜索首屏超时：`2.5s`；
   - 外部音源搜索全量兜底：`8.0s ~ 15.0s`；
   - 大模型推荐生成超时：`10.0s ~ 15.0s`。
   外部音源服务（musicbox、musicdl）与 LLM 接口各自独立运行、互不影响。任一外部音源出现网络抖动、失效或异常超时，系统会自动跳过并返回健康源的数据，绝不拖垮本地服务。
3. **大模型失效自动熔断降级**：
   当大模型 API Key 额度耗尽、接口网络不可达或超时未响应时，每日推荐模块自动熔断并降级为“基于用户常听歌手的本地种子关联搜索”，确保虚拟歌单永不落空。
4. **下载异常自洁机制**：
   若在线音频下载遇到源站断流、网络异常或下载字节数异常（如 `< 1KB` 的假流），后台协程自动触发清理机制，删除临时文件，防止产生脏数据。
5. **全链路状态探针**：
   通过 `GET /_ext/healthz` 接口实时暴露核心代理、官方上游、musicdl 容器、musicbox 容器与 LLM 的连通性，上游正常且至少一个音源正常即可维持对外服务。

---

---

## 核心扩展特性

### 1. 多源快速聚合搜索与渐进式翻页

- **可选音源（安装时可多选，至少启用一个）**：
  - **网易云音乐 (`musicbox` :8770)**：[darknessomi/musicbox](https://github.com/darknessomi/musicbox)，无损品质（`lossless`/FLAC）、LRC 歌词、封面，单次搜索默认最多 50 条。部分曲目可能需要扫码登录。
  - **多平台聚合 (`musicdl` :8768)**：[CharlesPikachu/musicdl](https://github.com/CharlesPikachu/musicdl)，酷我、咪咕等。
  由 `.env` 中 `FNMUSIC_MUSICDL_ENABLED` / `FNMUSIC_NETEASE_ENABLED` 开关；`/_ext/healthz` 在上游健康且**至少一个已启用音源健康**时为 `ok`。
- **毫秒级鉴权与快速路径**：
  - 未登录（401 / Token 无效）请求走本地毫秒级判定，不等待外部音源，耗时 `< 0.1s`。
  - 仅在上游鉴权通过且返回 `code: 0` 时触发在线音源聚合。
- **渐进式聚合与快返回（Fast Return & Background Aggregation）**：
  - **第 1 页极速响应**：`page=1` 请求时，采用 2.5 秒（`FNMUSIC_NETEASE_WAIT_S`）快速等待窗口。若主音源网易云在 2.5s 内返回，立即与本地搜索结果合并返回前端。
  - **后台异步聚合缓存**：副音源（`musicdl`）与主音源未耗尽的聚合任务继续在后台 `asyncio.Task` 中执行，合并去重（按 `(title, artist)` 小写去重，网易云优先）后写入内存搜索缓存（默认 TTL 300 秒，LRU 淘汰）。
  - **后续翻页秒级响应**：当用户请求 `page > 1` 时，直接从后台聚合缓存中切片返回，翻页流畅丝滑。

### 2. 在线流式播放与独立后台 Task Tee 边播边落盘

- **全格式保真流式播放**：支持 `flac`、`mp3`、`wav`、`m4a`、`ogg` 等主流音频容器。自动透传与转换客户端 `Range` 请求（`206 Partial Content`）。
- **客户端断连安全（解决 `.part` 孤儿残留问题）**：
  - 传统流式 Tee 写入与客户端 HTTP 响应绑定，一旦客户端切歌、拖动进度条或断开连接，下行连接中断会导致下载流程异常终止，在磁盘留下垃圾 `.part` 临时文件。
  - `fnmusic-ext` 采用**独立后台 Task Tee 架构**（`asyncio.Queue` 管道）：
    - 下载协程 `_downloader` 独立于客户端 HTTP 连接运行；
    - 数据块通过内存 `Queue` 分发给播放流，同时由后台 Task 持续落盘至独立临时文件（带有 UUID 的 `.<guid>.<hex>.part`）；
    - 客户端断开连接不影响后台任务完整写入；若最终下载不完整（字节数不符或小于 1KB），自动清理临时 `.part` 文件，绝不留存损坏垃圾。
- **智能落盘与元数据写入**：
  - 完整播放/下载完成后，自动将音频落盘至音乐库或缓存目录，并通过 `mutagen` 库自动将在线提取的 `title`、`artist`、`album` ID3/Vorbis 元数据标签写入音频文件，生成同名 `.lrc` 歌词 Sidecar 文件。

### 3. 本地与在线歌曲收藏合并（多用户隔离）

- **多用户数据隔离存储**：
  - 在线歌曲收藏按用户 GUID 隔离存储于 `online_favorites/<user_guid>.json`。
  - 写入操作采用“临时文件 + 原子重命名（`os.replace`）”与 `asyncio.Lock` 机制，保证并发安全性。
- **接口无缝拦截与状态合并**：
  - 拦截 `/music/api/v1/favorite-track/create`、`delete`、`list` 接口。
  - **本地曲目**：直接透传飞牛官方 Go 后端与 SQLite 数据库处理。
  - **在线曲目（`online:*`）**：
    - 代理层复用上游 `/music/api/v1/user/me` 鉴权；
    - 补齐符合飞牛前端严格契约的 Track 全量结构（`guid`、`title`、`duration`、`isFavorite: true`、`artists[]`、`album{}`、`audioSpec{}`、`accessStatus: 0`、`coverId` 等）；
    - 列表查询时将官方本地收藏与在线收藏列表按时间倒序合并，正确计算并回传 `total` 总数。

### 4. 歌词与封面代理转发与智能缓存

- **歌词双级解析与 Sidecar 缓存**：
  - 拦截 `/music/api/v1/lyric/list` 与 `/music/api/v1/track/lyrics`。
  - 优先读取本地 `.lrc` Sidecar 歌词缓存；未命中时按音源类型异步调取网易云或 musicdl 歌词接口，解析并格式化为飞牛前端适配的 LRC 载荷（`$n.lyric.list` / `preferred` 结构），并同步落盘至 Sidecar 缓存。
- **高清封面代理与重定向**：
  - 拦截 `/music/api/v1/static/cover`。
  - 支持 `guid` 或 `coverId` 参数，自动解析在线音源高清封面 URL 并通过 `302 Found` 重定向至源站 CDN 高清大图，同时兼容本地封面透传。

### 5. 每日推荐（Daily Recommend）

默认对已登录用户开启，出现在左侧「歌单」列表顶部（首页四张漫游/收藏卡片是官方 UI，不会多出第五张）。**每天只保留当天一份**：换日会删掉前一天的缓存与歌单，再生成新的「每日推荐」。

```text
最近播放 + 收藏（口味种子，不直接放进歌单）
        │
        ▼
  大模型产出 30 首候选（未配置则按歌手降级）
        │
        ▼
  在线音源检索可用曲目，跳过已收藏 / 刚听过的同曲
        │
        ▼
  凑满 20 首 online:* 曲目
        │
        ▼
  虚拟歌单 online:playlist:daily:YYYYMMDD:<user>
```

- 每天一份：缓存按日期文件存放，生成当天时删除该用户其余日期文件；列表只注入今天的 guid。
- 种子：飞牛 `play_history`（只读）+ 在线播放 + 本地/在线收藏，用来告诉模型口味，不把已收藏的歌再推荐回去。
- 流程：LLM 出 30 首 → musicdl / musicbox 在线搜索能播的 → 去重并排除收藏 → 凑满 20 首。
- 契约：拦截 `playlist/list`、`playlist/detail`、`playlist/batch-detail`、`track/playlist-detail/list`。未登录仍透传 `INVALID TOKEN`。
- 密钥：只存在 `.env`（`chmod 600`）。不填则跳过 LLM，仍按歌手做在线检索凑歌单。

人工安装见 [docs/INSTALL.md](docs/INSTALL.md)，Agent 安装提示词见 [docs/AGENT_INSTALL.md](docs/AGENT_INSTALL.md)。

---

## 目录结构

```text
fnmusic_ext/
├── install.sh                 # 一键安装配置（host / docker + 可选 LLM）
├── extend.sh                  # 一键启用扩展（Unix Socket 接管）
├── restore.sh                 # 一键还原官方直连
├── docker-compose.yml         # musicdl / musicbox 音源容器（按需启动）
├── .env.example               # 环境变量模板（不含密钥）
├── proxy/
│   ├── app.py                 # FastAPI 代理
│   ├── recommend.py           # 每日推荐（LLM + 检索）
│   ├── run_proxy.sh
│   ├── requirements.txt
│   └── tests/
├── musicdl-service/           # CharlesPikachu/musicdl HTTP 包装 (:8768)
├── musicbox-service/          # darknessomi/musicbox HTTP 包装 (:8770)
├── docs/
│   ├── INSTALL.md             # 人工安装
│   └── AGENT_INSTALL.md       # Agent 安装提示词
└── README.md
```

运行时数据（均 gitignore）：`cache/`、`online_favorites/`、`play_history/`、`recommend_cache/`、`.env`。

---

## 环境变量配置

支持在 `fnmusic-ext.service` 或系统环境中进行以下配置：

| 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `FNMUSIC_HOME` | 仓库根目录 | 缓存/收藏/推荐等数据根路径 |
| `FNMUSIC_MUSICDL_ENABLED` | `true` | 是否启用 musicdl 音源 |
| `FNMUSIC_MUSICBOX_URL` | `http://127.0.0.1:8770` | 网易云 (musicbox) 服务地址 |
| `FNMUSIC_NETEASE_ENABLED` | `true` | 是否启用网易云 / musicbox 音源 |
| `FNMUSIC_NETEASE_WAIT_S` | `2.5` | 第 1 页搜索网易云快速等待超时（秒） |
| `FNMUSIC_NETEASE_SEARCH_LIMIT` | `50` | 网易云搜索单次最大抓取条数 |
| `FNMUSIC_NETEASE_QUALITY` | `lossless` | 网易云音频音质偏好（`lossless` 无损 / `exhigh` / `standard`） |
| `FNMUSIC_MUSICDL_URL` | `http://127.0.0.1:8768` | musicdl 共享音源服务地址 |
| `FNMUSIC_UPSTREAM_SOCK` | `/var/run/trim_music_upstream.socket` | 飞牛音乐原生 Unix Socket 路径 |
| `FNMUSIC_ONLINE_LIMIT` | `30` | 默认在线合并返回条目数限制 |
| `FNMUSIC_SEARCH_CACHE_TTL` | `300` | 搜索聚合缓存有效期（秒） |
| `FNMUSIC_CACHE_DIR` | `$FNMUSIC_HOME/cache` | 在线音频 Tee 缓存落盘目录 |
| `FNMUSIC_FAV_DIR` | `$FNMUSIC_HOME/online_favorites/` | 多用户在线收藏存储目录 |
| `FNMUSIC_ONLINE_SOURCES` | `KuwoMusicClient,MiguMusicClient` | musicdl 音源白名单 |
| `FNMUSIC_LLM_BASE_URL` | 空 | OpenAI 兼容 Base URL；与 API Key 同时非空才开启每日推荐 |
| `FNMUSIC_LLM_API_KEY` | 空 | 只放 `.env`，禁止提交 |
| `FNMUSIC_LLM_MODEL` | `gpt-4o-mini` | 推荐模型名 |

---

## 安装

```bash
chmod +x install.sh extend.sh restore.sh
./install.sh                 # 交互：选 host/docker、多选音源、可选填 LLM
./extend.sh                  # 启用扩展
```

- `--sources musicdl,musicbox`：可多选，至少选一个。非交互缺省仅 `musicdl`；交互缺省两者都选。
- Docker 模式只把**已选音源**放进容器；代理必须在宿主机 systemd 接管 Unix Socket。
- 详细步骤：[docs/INSTALL.md](docs/INSTALL.md)
- 给 Agent 的整段提示词：[docs/AGENT_INSTALL.md](docs/AGENT_INSTALL.md)

---

## 标准运维指南

### 1. 一键启用扩展

执行根目录下的 `extend.sh`：

```bash
./extend.sh
```

**`extend.sh` 自动化流程**：
1. **环境预检**：检查 sudo 权限、飞牛音乐原生 socket、Python 虚拟环境及语法合法性。
2. **容器自愈**：若音源容器未就绪，自动通过 `docker compose` 启动服务并等待就绪。
3. **幂等性判定**：若检测到代理已正常接管且上游健康，执行全链路验收后直接退出。
4. **服务挂载**：安装 `fnmusic-ext.service` 并启动，由 `run_proxy.sh` 自动完成 socket 安全接管与权限赋权。
5. **全链路验收测试**：
   - 验证未登录 401 快速响应（`< 3s`）；
   - 验证在线音频流 `Range 206` 接收（数据流传输正常）；
   - 若验收失败，自动触发安全回滚，保证系统稳定。

### 2. 一键还原官方模式

如需切回飞牛官方原生直连模式：

```bash
# 1. 标准还原 (复位 Socket、停用代理服务，保留音源容器与缓存)
./restore.sh

# 2. 深度还原 (额外停止并删除音源 Docker 容器)
./restore.sh --full
```

### 3. 健康检查与状态探测

通过 Unix Socket 直接探测扩展代理端点：

```bash
# 探测代理端点及上游连通性
curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz
```

**正常输出示例**：
```json
{
  "ok": true,
  "upstream": "ok",
  "musicdl": "ok",
  "musicbox": "ok",
  "llm": "disabled"
}
```

### 4. 日志排障与调试

```bash
# 查看代理服务实时运行日志
sudo journalctl -u fnmusic-ext -f -n 100

# 检查音源容器状态与日志
docker ps --filter "name=fnmusic-"
docker logs -f fnmusic-musicdl
docker logs -f fnmusic-musicbox

# 检查 Unix Socket 挂载与权限状态
ls -la /var/run/trim_music*.socket
# 正常状态输出应形如：
# srw-rw-rw- 1 root root ... /var/run/trim_music.socket (代理监听)
# srw-rw-rw- 1 root root ... /var/run/trim_music_upstream.socket (官方Go后端)

# 手动清理在线歌曲缓存
rm -rf ./cache/*
```

### 5. 本地代码与测试验证

在修改代码或配置文件后，可运行以下命令进行本地静态检查与测试：

```bash
# 1. Shell 脚本语法检查
bash -n extend.sh restore.sh install.sh proxy/run_proxy.sh

# 2. Python 语法编译检查
python3 -m py_compile proxy/app.py proxy/recommend.py

# 3. 运行全套单元测试 (包含网易云/聚合搜索/Tee缓存/收藏合并等测试)
.venv-proxy/bin/python -m pytest proxy/tests -q
```

---

## 免责与版权声明（Disclaimer & Copyright Notice）

### 1. 技术研究与非商业用途
- 本项目（`fnmusic-ext`）基于 **MIT 许可证** 开源发布，立项初衷仅为个人开发者探讨 Linux Unix Domain Socket 机制、透明反向代理技术、流式媒体传输与多协程并发架构的技术验证与学习交流。
- 本项目严格限定于**个人技术研究与非商业用途**。任何个人、团队或商业实体严禁将本项目、其衍生版本或相关工具用于任何形式的商业营利、付费订阅、软硬件捆绑销售或非法牟利行为。

### 2. 致谢上游开源项目与无侵权声明
- 本项目在线音源检索与元数据抓取能力依赖于社区优秀的开源组件：
  - [CharlesPikachu/musicdl](https://github.com/CharlesPikachu/musicdl)
  - [darknessomi/musicbox](https://github.com/darknessomi/musicbox)
  在此向上游开源项目的原作者与贡献者致以崇高的敬意。
- 本项目仅在本地私有云环境充当**协议中继与数据适配胶水层**，本身不具备任何音源破解或版权规避逻辑，主观上绝无任何侵犯各音乐平台、唱片公司或第三方知识产权的意图。

### 3. 音频及视听数据版权归属
- **音频及元数据版权全权归属各原始版权方**（包括但不限于各唱片公司、独立音乐人及各在线音乐服务平台）。
- **零托管、零存储原则**：本项目服务器及开源代码仓库**不托管、不分发、不直接存储任何受版权保护的音频、视频、歌词或专辑封面文件**。所有音频流与图文元数据均系客户端发起请求时，由代理服务实时转发自公开网络接口或源站 CDN。
- **本地缓存试听合规要求**：边播边落盘功能所生成的本地临时缓存文件（Cache），仅供个人离线技术分析、音频标签兼容性测试与学习评估。**使用者请在试听或测试后 24 小时内自行删除相关音频文件**。
- **倡导正版**：请大家支持正版数字音乐事业！如需长期收听、收藏或获得更高品质的音乐体验，请前往网易云音乐、酷我音乐、咪咕音乐、QQ音乐等官方平台开通正版会员并购买正版专辑。

### 4. 免责与使用者风险自担
- 使用者在下载、部署或运行本项目前，应充分知悉并自愿遵守所在国家/地区的法律法规，以及第三方服务平台的用户协议。
- **风险自担**：由于使用者滥用、恶意传播、商业化使用或不当配置本项目而导致的一切法律责任、版权纠纷、账号封禁、IP 拦截或连带经济损失，**概由使用者本人自行承担全部责任**，本项目发起人、维护者及社区贡献者不承担任何直接、间接或连带的法律责任。
- **权利人联系通道**：若相关版权权利人认为本项目的代码实现或接口中继涉嫌侵犯其合法权益，请通过 GitHub Issue 或电子邮件向项目维护团队提交权属证明通知。我们将在收到通知并核实后的第一时间积极配合，并及时下架、修改或删除涉嫌侵权的代码与功能。

