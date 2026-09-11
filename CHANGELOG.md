# 更新日志 (Changelog)

本项目所有显著变更均记录于此文件。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [1.4.0] - 2026-09-11

### 官方应用升级适配（2026-09-11 fnOS + 飞牛音乐升级实测）

- **整体兼容性确认**：官方升级未改变 socket 路径（`/var/run/trim_music.socket`）、
  二进制名（`trim-music`）与 nginx 转发配置，`/music/api/v1` 全部核心端点
  （搜索/播放流/歌词/收藏/播放历史/用户身份/HLS preset）与 `music.db` v1 表结构
  均保持不变，接管机制无需修改即可工作。未认证响应封套仍为
  `{"code":99999,"msg":"INVALID TOKEN","data":null}`（HTTP 状态由 200 变为 401，
  代理的身份探测与验收门均已兼容两条路径）。
- **authx 签名头透传**：新版官方前端登录后为每个请求计算签名头 `authx`
  （含时间戳与随机数）。代理显式原样转发该头；刻意不将其纳入搜索缓存 scope
  （其值逐请求变化，纳入会使缓存永远不命中）。
- **升级后失效的直接原因与恢复**：fnOS 升级重启 NAS 后官方应用晚于本项目服务
  启动，接管自启失败；重新执行 `./extend.sh` 即可恢复（extend 会自动拉起音源
  容器并完成接管与验收）。

### 修复

- **重装不再清空大模型配置（高频痛点）**：安装向导对"每日推荐"答 N 或留空时，
  此前会把 `.env` 中已保存的 `FNMUSIC_LLM_*` 以空值显式覆盖，导致每次重装都
  需重新填入 API Key。现改为：答 N/留空一律保留既有配置；仅显式传入
  `--disable-recommend` 才清除。
- **restore.sh 语义重设计**：
  - 默认：还原官方直连 + 移除代理 unit + **停止并删除音源容器/宿主机 unit**，
    但完整保留 `.env` 与全部数据（网易云登录、缓存、在线收藏、播放历史、推荐
    缓存）——重装后无需重新填写任何配置；
  - `--full`：在默认动作之外执行工厂级清理，删除 `.env`（含历史备份）、
    `musicbox-data/`、`cache/`、`online_favorites/`、`play_history/`、
    `recommend_cache/` 与 `.venv-*`，仅保留代码与 git 仓库（Docker 镜像保留
    以便重装加速）。清理仅限项目目录内的已知路径，并有行为级回归测试保障。

### 测试

- 新增：LLM 配置保留语义（decline 保留 / explicit 清除）、restore 清理范围
  行为测试（该删的删净、代码与未知目录保留）、authx 透传契约。
- 修正：接管归因测试在宿主机存在活跃 fnmusic-ext cgroup 时的环境敏感断言
  （两种拒绝消息均满足"绝不归因外部监听者"契约）。

## [1.3.1] - 2026-09-10

### 修复

- **修复接管中断后"装不上也还原不了"的死锁（严重）**：`takeover.py` 原先要求 socket 文件
  必须在归属记录（`ownership.json`）中登记过才允许删除，且只允许删除 `stale` 状态的文件。
  一旦上一次接管失败留下未登记的僵尸 socket 文件（官方 socket 已停放在
  `trim_music_upstream.socket`，而 `trim_music.socket` 是一个无人监听的残留文件），
  `publish` 与 `restore` 会同时以 `unrecorded/replaced socket; preserved` 失败并永久互锁。
  现改为：对**可证明不可达**的 socket 文件（`connect()` 返回 `ECONNREFUSED`、同一 inode
  连续两次观测一致）一律回收，并打印审计行；仍然拒绝删除任何存活（含代理自身）或身份不明
  的 socket 文件。安装与还原在任何历史失败残留之后都可继续。
- **修复 supervisor 回滚异常掩盖真实失败原因**：`supervise()` 的 `finally` 中执行回滚，
  回滚自身的异常会覆盖原始异常，导致日志只显示 `unrecorded/replaced socket; preserved`，
  真正的失败原因（例如拓扑冲突）被吞掉。现改为分别捕获并串行上报
  `<原始原因>; rollback failed: <回滚原因>`，且正常停止（SIGTERM）时不再被误判为失败。
- **修复归属记录不一致导致的死锁**：官方应用重启（PID/inode 变化）或代理崩溃后，
  `remember`/`publish`/`restore` 原先会以"归属变更"直接失败。因两类角色均由内核身份
  （`SO_PEERCRED` + `/proc/<pid>/exe`、`/_ext/livez` + peer PID）实证，记录仅作缓存，
  现改为带 `[takeover] note:` 审计行地采纳新的存活身份，并在移动后校验终态。
- **`restore-plan` 覆盖全部可恢复拓扑**：新增 `vacant-target-repair`（僵尸 target + 官方
  upstream）与 `official-direct`（含 upstream 僵尸文件清理）两类计划；`restore()` 会清理
  僵尸 upstream，并在确实不存在任何官方监听者时明确报错
  `no official listener present; restart the official music app`，不再留下自相矛盾的布局。
- **就绪窗口对齐**：supervisor 内部就绪判定由 30s 放宽到 55s，保持在 unit
  `ExecStartPost` 的 65s 之内，慢启动不再被内部判定抢先回滚。
- 回归测试同步更新：新增僵尸 target/upstream 回收、官方重启采纳、双活冲突下的
  错误串行上报等用例；文档 `docs/installation-reliability.md` 补充"vacant socket 必回收"
  契约说明。

## [1.3.0] - 2026-09-09

### 新增

- **lxmusic 音源新增 tx（QQ音乐）与 kw（酷我）子源**：tx 搜索采用 QQ 官方免登录接口
  （musicu.fcg），kw 搜索采用酷我官方 r.s 老接口；直链解析经第三方链路 + Range 探活验证。
  `LX_SOURCES` 默认值由 `kg,wy,mg` 扩展为 `kg,wy,mg,tx,kw`。
- **第三方解析链路层（移植自洛雪社区聚合源 qdy v9.3 的链路清单与多链路回退架构）**：
  数据驱动注册表 + 连续失败熔断（3 次失败暂停 10 分钟）+ 链路健康状态暴露于 `/healthz`。
  2026-09-09 对 qdy 全部 10 条链路逐条实测后仅移植存活链路：长青 kw（302→酷我 CDN 无损
  FLAC）与溯音咪咕（320k 直链+歌词）；星海/念心/溯音 oiapi/汽水等 8 条已死链路不移植，
  后续复活时在注册表追加即可。`LX_THIRD_PARTY=0` 可整体关闭（退化为纯官方免登录直连）。
- **可播性验证（"搜得到必能播"）**：kg/wy 的 VIP/付费曲目不再按 pay_type/fee 元数据一票
  否决，改为"官方解析→第三方链路→Range 探活（200/206 且非 HTML）+ 试听碎片体积防护"全链路
  验证，通过才返回并携带 `verified: true` 标记；kw/tx/mg 搜索结果同样全部探活。试听片段、
  无版权、VIP 拦截（fail_process=4）等真不可播标记的过滤保持不变。

### 变更

- proxy 的 `is_playable_online_track` 对 `verified` 条目跳过收费元数据拦截（pay_type/price/
  fee），试听/无流/坏 URL 校验全部保留——服务端已用真实探活证明可播，元数据不再作为可播性
  的代理判断。
- mg 咪咕搜索的逐曲解析升级为完整"解析+探活"（官方接口失败自动回退溯音咪咕链路），此前仅
  校验 URL 存在性、未做存活性探测。
- lxmusic-service 版本号 1.0.0 → 1.1.0；extend.sh 子源探测清单扩展为 kg/wy/mg/kw/tx。

### 修复

- **升级合并不再丢失用户 .env 注释**：`env_merge` 此前在增量合并时静默丢弃用户手写的
  注释/非赋值行；现按原顺序去重后保留在文件末尾（连续合并不堆积），键值合并规则不变。

### 工程与测试

- **新增 GitHub Actions CI**（`.github/workflows/ci.yml`）：push 到 main/dev 与所有 PR 自动
  执行 shell 语法检查、Python 编译检查与全量 pytest（Python 3.11/3.13 矩阵）；另附
  shellcheck 静态检查（error 级，仅报告不阻断）。
- **测试套件单命令化**：新增 `pytest.ini`，`python -m pytest` 一条命令跑全部 proxy +
  各音源服务测试；修复 musicbox 与 lxmusic 测试的顶层 `app` 模块名冲突（改为独立模块名
  显式加载）；musicdl 假慢源线程改可中断睡眠，测试进程退出不再挂起约 26 秒（全套约 13 秒）。
- **版本断言测试去硬编码**：`test_version_env` 改为动态读取 `VERSION` 文件比对，升级版本
  不再需要同步修改测试。
- **补齐 musicbox 服务 9 项端点测试**：healthz、播放地址（音质白名单/CLI 参数）、歌曲信息、
  歌手/专辑/歌单、歌词（成功/上游异常）、登录状态与扫码轮询、上游失败 502 与超时 504 信封。

### 已知边界

- tx（QQ音乐）直链解析当前无存活第三方链路（qdy 的 4 条 tx 链路于 2026-09-09 实测全部失效），
  tx 搜索会因探活全部失败而返回空结果但不报错；第三方链路复活后在 `THIRD_PARTY_CHAIN`
  注册表登记即自动恢复。
- kw（酷我）免登录歌词接口已全部失效，歌词暂返回空，不影响播放。
- 第三方链路属社区公益性质，随时可能失效；熔断器保证失效链路自动旁路，最坏情况退化为
  现有官方免登录能力（kg/wy/mg 免费曲），不会出现"搜得到播不出"。

## [1.2.3] - 2026-09-08

### 新增

- **Docker 构建基础镜像源自动探测回退**：fnOS 等系统在 Docker daemon 全局配置的镜像加速器
  （如 `docker.fnnas.com`）异常（401/超时）时，BuildKit 解析 `python:3.13-slim` 元数据失败且不会
  回退官方源，`docker compose up --build` 随即失败（`failed to resolve source metadata ... 401 Unauthorized`）。
  新增 `ensure_base_image.sh`：**国内镜像优先**（完整镜像源引用直连对应仓库，绕开只拦截
  docker.io 短引用的 daemon 加速器，以真实 `docker pull` 验证），逐个尝试
  docker.1ms.run / docker.m.daocloud.io / docker.1panel.live / hub.rat.dev（`FNMUSIC_DOCKER_MIRRORS`
  可覆盖），全部失败再兜底官方 `python:3.13-slim`（daemon 加速器链路在国内网络下常慢/不稳），
  结果缓存到 `.env` 的 `FNMUSIC_BASE_IMAGE` 并由 compose `build.args` 自动读取；install.sh 安装、
  extend.sh 自愈重建、手动 `docker compose up -d --build` 三条路径全部生效，后续运行先验证缓存、
  失效自动重新探测。全程不修改系统 Docker 配置，仅本应用构建生效；也可通过 `BASE_IMAGE` 环境变量
  或直接编辑 `.env` 手动指定。

### 变更

- 三个音源镜像构建内 `pip install` 默认接入清华 PyPI 源（`.env` 的 `FNMUSIC_PIP_INDEX` 可覆盖），
  与宿主机模式安装惯例对齐。
- musicdl 镜像 apt 层默认接入清华镜像（`FNMUSIC_APT_MIRROR` 可覆盖）：仅在构建层内临时替换
  `deb.debian.org`，apt update 失败（15s 超时快速判定）自动回退官方源重试，安装完成后恢复
  官方源——最终镜像与宿主机 apt 配置不受影响；国内网络下 ffmpeg 及其依赖不再长时间卡在
  deb.debian.org 慢速下载。

## [1.2.2] - 2026-09-08

### 修复

- **Docker 安装在受限 umask 环境下启动失败的严重问题**：三个音源镜像此前直接继承仓库检出文件的权限位，
  在 umask 077 环境（root shell、`sudo git clone` 等）下检出的 `app.py` 为 600，进镜像后为
  `root:root 0600`，容器内非 root 的 `appuser` 无法读取，uvicorn 启动即抛
  `PermissionError: [Errno 13] Permission denied: '/app/app.py'` 并随 `restart: unless-stopped` 无限重启。
  现镜像内源码统一 `--chown=appuser:appuser` 且权限 0644，与宿主机文件权限完全解耦（`COPY --chmod`
  仅 BuildKit 支持，故采用兼容新旧构建器的 `--chown` + `RUN chmod` 方案）。
- 修正 musicbox Dockerfile 中 `chown` 早于 `COPY` 执行而对源码文件不生效的问题。

### 变更

- `install.sh` 构建前对服务源码做权限归一化（非致命兜底），避免受限 umask/属主影响构建上下文。
- `docs/INSTALL.md` 新增「常见问题排查」章节，含上述报错的说明与升级方法。

## [1.2.1] - 2026-09-08

### 修复

- 移除生产安装中多余的 pytest 测试依赖。

## [1.2.0] - 2026-09-07

### 新增

- 安装收尾集成网易云终端扫码登录流程（ASCII 二维码过期自动刷新 + 登录状态轮询）。

## [1.1.2] - 2026-09-07

### 新增

- 全音源严格可播过滤与直链探活防线校验升级。

### 优化

- 多音源搜索 3s 首屏与 5s 首响兜底机制，缓存延长至 7 天。

## [1.1.1] - 2026-09-07

### 修复

- 过滤收费不可播歌曲，重构酷狗直链解析。
- 安装/还原流程加固与多音源搜索容错增强。

## [1.1.0] - 2026-09-07

### 新增

- 第三音源：洛雪音乐源（lxmusic，酷狗/网易/咪咕免登录解析）。

## [1.0.1] - 2026-09-07

### 新增

- 项目版本管理与安装配置增量合并机制，音源超时与自适应降级。

### 修复

- 彻底修复网易云 XDG 目录缺失导致子进程崩溃，支持命令行终端直接显示登录二维码。
- 重构验收试播逻辑，支持多音源平等遍历与多关键词重试。
- extend 与端口 5667 解耦，通过 UDS 检查安全判定启用状态。

## [1.0.0] - 2026-09-04

### 新增

- fnmusic-ext 首个发布版本：musicdl / musicbox 双音源，Docker 与宿主机双模式部署，一键安装向导与 fnOS 代理扩展接管。
