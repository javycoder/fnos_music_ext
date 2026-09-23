# 安装与部署指南

适用环境：飞牛 NAS（fnOS）已安装并启动「飞牛音乐」官方应用。本项目采用无侵入接管设计，**完全不修改**飞牛官方 nginx 配置、不 Patch 官方二进制、不改动官方数据库。

> 💡 **自动化部署提示**：若使用 AI Agent（如 OpenCode、Claude Code、Cursor 等）进行全流程自动化部署与自检验收，请直接查阅 [Agent 安装提示词](AGENT_INSTALL.md)。

---

## 0. 前置准备

- **操作系统**：fnOS（Debian 12 基础系统）；
- **基础运行组件**：Python 3.11+ 及 `python3-venv`（核心代理运行在宿主机）：
  ```bash
  sudo apt-get update && sudo apt-get install -y python3 python3-venv git
  ```
- **管理员权限**：具备 `sudo` 执行权限的管理员账号（非交互安装需免密 sudo）；
- **官方音乐应用**：必须先在 fnOS「应用中心」安装并启动「飞牛音乐」（确保存在 `/var/run/trim_music.socket`）；
- **Docker（必需）**：v2.0.0 起**仅支持 Docker 部署**，必须先在 fnOS「应用中心」安装 Docker。未安装 Docker 的机器执行 `install.sh` 会直接报错退出；**脚本绝不会擅自安装 Docker 引擎**。

克隆项目并进入根目录赋予执行权限（**仅脚本安装需要**，fpk 安装可跳过）：

```bash
git clone https://github.com/javycoder/fnos_music_ext.git fnmusic_ext
cd fnmusic_ext
chmod +x install.sh extend.sh restore.sh proxy/run_proxy.sh
```

---

## 1. 部署形态说明

v2.0.0 起部署形态固定为两部分：

1. **核心代理（宿主机 systemd）**：`fnmusic-ext.service` 运行在项目内独立虚拟环境 `.venv-proxy`，负责零侵入接管 `/var/run/trim_music.socket`。核心代理必须在宿主机运行——塞入容器会面临跨容器 socket 权限穿透问题。
2. **音源单容器（Docker）**：`fnmusic-sources` 一个容器内含 musicdl / musicbox / lxmusic / WebUI 四个程序（supervisor 管理），**按 `.env` 只启动当前所选音源进程**，其余不驻留内存。运行期切源在 WebUI 内完成（写配置 + 同容器秒级 stop/start），无需重建容器。

| 端口 | 绑定地址 | 用途 |
| :--- | :--- | :--- |
| 8768 | 127.0.0.1 | musicdl 音源（仅代理访问） |
| 8770 | 0.0.0.0 | musicbox 音源（局域网扫码登录） |
| 8772 | 127.0.0.1 | lxmusic 音源（仅代理访问） |
| 8774 | 0.0.0.0 | 管理 WebUI（无鉴权，仅限可信内网） |

数据卷：项目目录 `sources-data/`（网易登录态、洛雪源脚本缓存与状态）；仓库目录挂载到容器 `/repo`（WebUI 读写 `.env` 用）。容器无特权、不挂 docker.sock。

---

## 2. 一键安装与配置

### 方式 A：应用中心 fpk 安装（推荐）

从 [GitHub Releases](https://github.com/javycoder/fnos_music_ext/releases) 下载最新 `fnmusic-ext-<版本>.fpk`，在 fnOS「应用中心 → 手动安装」选择该文件：

1. 向导中选择**初始音源**（musicdl / musicbox / lxmusic，选 lxmusic 需填写源脚本 URL）；
2. 保持「安装完成后立即启用扩展」开启，安装即自动完成容器构建、代理接管与全链路验收；
3. 桌面出现「fnMusic 扩展管理」图标，点击在飞牛桌面窗口内打开管理页（`http://<NAS_IP>:8774`）。

日常启停在应用中心完成：「停止」秒级还原官方直连，「启动」恢复扩展。卸载会先自动备份配置与数据到存储卷根目录（`fnmusic-ext-backup-<时间戳>.tar.gz`）再清理。

命令行等价操作（需 root）：

```bash
sudo appcenter-cli install-fpk fnmusic-ext-<版本>.fpk   # 安装
sudo appcenter-cli list                                 # 查看状态
sudo appcenter-cli stop fnmusic-ext                     # 停止（还原官方直连）
sudo appcenter-cli start fnmusic-ext                    # 启动（恢复扩展）
sudo appcenter-cli uninstall fnmusic-ext                # 卸载（自动备份后清理）
```

> 升级请在应用中心内安装新版本 fpk；`install-fpk` 在已安装时不会自动升级。升级脚本会自动备份并恢复用户数据（.env、网易云登录、收藏、播放历史）。
>
> 曾用 git clone 脚本方式部署的用户：请先在原部署目录执行 `sudo ./restore.sh` 释放部署登记，再安装 fpk，否则安装会因部署冲突而中止（保护既有部署不被静默接管）。

### 方式 B：交互向导安装（脚本方式）

```bash
./install.sh
```

向导自动执行环境预检，依次引导：

1. **音源三选一**（v2.0.0 起互斥单选）：
   - `1` 网易云 musicbox：安装后自动进入扫码登录；
   - `2` musicdl：进入平台多选子菜单（默认精选酷我+咪咕；全部平台编号见 [../musicdl-service/PLATFORMS.md](../musicdl-service/PLATFORMS.md)）；
   - `3` 洛雪 lxmusic：直接安装（无源状态），源脚本装好在管理页 WebUI 配置；也可在安装命令附 `--lx-source-url`（URL / 本机 `.js` 路径），安装时进行「下载→初始化→搜索→解析→探活」全链路校验；
2. **是否安装管理 WebUI**（端口 8774，默认否；无鉴权，仅限可信内网）；
3. **大模型每日推荐（可选）**：OpenAI 兼容 API，仅在未启用网易音源时作为推荐兜底；
4. **一键启用**：确认后自动调用 `./extend.sh` 接管验收。

### 非交互静默部署（自动化脚本）

```bash
# 网易云 + WebUI
./install.sh --non-interactive --sources musicbox --webui --extend

# musicdl（酷我+咪咕；平台粒度用短名或编号）
./install.sh --non-interactive --sources musicdl-kuwo,musicdl-migu --extend

# 洛雪自定义源（--lx-source-url 可选：http(s) URL / 本机 .js 路径 / 留空无源安装）
./install.sh --non-interactive --sources lxmusic \
  --lx-source-url 'https://example.com/your-source.js' --extend
./install.sh --non-interactive --sources lxmusic \
  --lx-source-url "$HOME/scripts/my-source.js" --extend   # 本机路径自动复制进数据卷
./install.sh --non-interactive --sources lxmusic --webui --extend  # 无源安装，装后在管理页配置

# 附带大模型推荐兜底（密钥仅写入本地 .env，权限 600）
./install.sh --non-interactive --sources musicdl --enable-recommend \
  --llm-base-url 'https://api.openai.com/v1' \
  --llm-api-key '<KEY>' --llm-model 'gpt-4o-mini' --extend
```

常用参数：`--sources`（音源三选一）、`--lx-source-url`（可选：http(s) URL 或
宿主机 `.js` 路径，路径会自动复制进数据卷）、`--lx-skip-verify`
（跳过洛雪源可用性校验直接激活，源是否可用装好后在管理页 WebUI 查看）、
`--webui` / `--no-webui`、`--extend`（安装后自动接管）、`--adopt`（迁移部署登记）、
`--qr`（仅扫码登录）。`--mode` 参数已随 host 模式移除。

---

## 3. 启用、切换与还原

```bash
# 启用扩展接管（含全链路验收，失败自动回滚）
./extend.sh

# 手动改 .env 后使其对容器生效（重启容器重选进程集 + 重新验收）
./extend.sh

# 还原官方原生直连（停止音源容器；保留 .env 与全部数据）
./restore.sh

# 彻底清理卸载（额外删除 .env、登录态、缓存、收藏、历史）
./restore.sh --full
```

**运行期换源/调参**：浏览器打开 `http://<NAS_IP>:8774`（若已装 WebUI），
在「音乐源」分区单选切换——写配置与容器内进程切换一步完成，代理侧由热重载
同步，全程无需命令行。音质偏好、推荐开关、边听边存、LLM 同理。

### 单机多副本约束与部署迁移（--adopt）

代理单元名、音源容器名与安装锁在本机全局唯一，且本机会登记当前部署目录
（`/var/lib/fnmusic-ext/deployment`）。因此：

- 请只维护一份部署目录。从**另一份仍存在的仓库副本**执行安装/扩展/还原会被
  拒绝，并提示先回到原部署目录操作；原目录执行 `./restore.sh` 释放部署后，
  新目录即可正常安装；
- 确认要把部署迁移到当前目录（例如旧目录准备废弃）时，追加 `--adopt`：
  ```bash
  ./install.sh --non-interactive --sources musicbox --webui --adopt --extend
  ```
  接管成功后登记自动指向当前目录。`extend.sh` / `restore.sh` 同样支持 `--adopt`；
- 原登记目录已被删除时不拦截，任意目录可直接重新安装；
- 安装锁若被**本副本**挂起的旧进程占用（如向导停在扫码登录），新命令会自动
  终止旧进程并接管；锁若属于另一份副本或无关进程则绝不终止，仅报告后退出。

---

## 4. 网易云登录扫码（musicbox 源）

网易云部分 VIP 或无损音质曲目需要登录。任选其一：

```bash
# 终端交互式扫码（推荐；ASCII 二维码、过期自动刷新）
./install.sh --qr        # 或 ./netease_login.sh / ./extend.sh --qr
```

或局域网浏览器访问 `http://<NAS_IP>:8770/api/v1/auth/login/qr.png` 扫码。
登录凭证持久化在 `sources-data/`，无需重复扫码；WebUI 内亦可扫码。

---

## 5. 洛雪自定义源（lxmusic 源）

播放解析依赖你提供的洛雪自定义源脚本（社区格式，`@name/@version` 头部注释的 JS）：

- **管理页配置（推荐）**：WebUI「音乐源 → 洛雪自定义源」提供三种方式——
  1. **粘贴 URL**：`http(s)://.../*.js`（原有方式）；
  2. **上传 .js 文件**：从电脑选择脚本上传，落盘到数据卷
     `sources-data/lxmusic/uploads/`，以 `file:///data/lxmusic/uploads/<名字>.js`
     形态参与后续流程（随数据卷持久化，重启/升级不丢）；
  3. **从 NAS 选择**：飞牛桌面内打开管理页时可用（走 fnOS 开放 API
     `pickUserFile` 文件选择器；直连 8774 的浏览器环境自动隐藏该按钮），
     选中 NAS 上的 `.js` 后由宿主侧网关代读内容，等效于上传。
  任一方式选定后点「测试」（下载→初始化→内置搜索→128k 解析→Range 探活），
  通过后保存即热切换；
- **安装时配置（可选）**：非交互 `--lx-source-url` 接受 `http(s)` URL 或宿主机
  `.js` 文件路径（自动复制进数据卷并转 `file://`）；安装器在容器内运行
  `verify_source.py` 做全链路校验，失败按分类提示。不提供则无源安装，
  装好后在管理页配置；fpk 安装向导不再出现任何洛雪源输入项；
- **生效范围**：源脚本只在容器内 Node 沙箱中运行、仅可发起 HTTP 请求；
  搜索/歌词/热门榜单始终走内置平台接口，不依赖源脚本；
- 未配置源时 lxmusic 的播放解析不可用（搜索/榜单不受影响），`/healthz` 的
  `user_source` 字段与 `./extend.sh` 输出均会提示。

源脚本是第三方代码，请仅使用可信来源，并仅访问您有权收听的内容。

---

## 6. 推荐歌单工作机制

用户登录飞牛音乐后，左侧歌单顶部呈现两个独立推荐歌单，各自受开关控制（默认都开）：

**「每日推荐 MM-DD」**（`FNMUSIC_RECOMMEND_DAILY` 控制），按当前音源裁剪：

1. **网易每日推荐**：musicbox 源且已扫码登录时，直连网易云个性化推荐；
2. **大模型兜底**：仅非网易音源且配置了 `FNMUSIC_LLM_*` 时启用；
3. **关键词兜底**：全链路失败时按听歌历史关键词检索，确保歌单始终可用。

**「热门推荐」**（`FNMUSIC_RECOMMEND_HOT` 控制），榜单原味、不排除已收藏曲目：

1. **网易热歌榜**：musicbox 源（免登录）；
2. **洛雪榜单**：lxmusic 源启用时聚合酷狗 TOP500 / 酷我飙升榜 / 网易新歌速递
   （按 `LX_SOURCES` 平台过滤）；全部榜单不可用时该歌单不显示。

**封面图标**：两个歌单的封面都取列表里第一首带可用封面直链的曲目（依次向后找）；
全部曲目都没有封面时封面接口返回 404，客户端显示自带默认样式（不伪造占位图）。

---

## 7. 健康检查与验收

```bash
# 代理端点健康状态
curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz

# 各音源容器内健康端点
curl -s http://127.0.0.1:8768/healthz   # musicdl（若启用）
curl -s http://127.0.0.1:8770/healthz   # musicbox（若启用）
curl -s http://127.0.0.1:8772/healthz   # lxmusic（若启用；含 user_source 状态）
curl -s http://127.0.0.1:8774/healthz   # WebUI（若启用）

# 本地自动化测试集（无需 Docker/飞牛环境）
python3 -m pytest
```

---

## 8. 常见问题排查

### 安装报「未检测到 docker」

v2.0.0 起仅支持 Docker 部署。请先在 fnOS「应用中心」安装 Docker 后重试；
`docker` 命令存在但 daemon 未运行时同样会报错退出。

### 升级自 v1.x 的音源切换问题

v2.0.0 结构变化较大（三容器→单容器、数据目录迁移、配置键增删），**推荐升级流程**：
`git pull` → `./restore.sh`（先还原官方直连并清理旧部署，兼容 v1.x 旧容器/宿主机服务；
`.env` 与数据保留）→ `./install.sh` 全新安装。直接原地升级同样支持。

v1.x 允许多音源并存，v2.0.0 起三音源互斥单选。升级安装时检测到旧 `.env` 多源
并存会要求重新三选一（非交互需 `--sources` 指定唯一音源）。此后换源在 WebUI
里秒级完成，不必重装。

### 洛雪源测试失败

WebUI 或安装向导的错误分类含义：**下载失败**（URL 不可达/超 9MB）、**格式无效**
（缺少洛雪源头部注释）、**初始化失败**（脚本运行报错，多为与主流源规范不兼容）、
**无可用平台**、**解析失败**（源声明平台均无法出直链）。依次检查 URL、换源后重试。

### 构建时报 `failed to resolve source metadata for python:3.13-slim ... 401 Unauthorized` 或拉取超时

多为系统 Docker daemon 全局镜像加速器异常。安装脚本会在构建前自动探测可用源
（国内镜像直连优先、官方源兜底），结果缓存到 `.env` 的 `FNMUSIC_BASE_IMAGE`，
全程不修改系统 Docker 配置。手动指定：

```bash
BASE_IMAGE=docker.m.daocloud.io/library/python:3.13-slim ./install.sh
```

自定义镜像候选列表：环境变量 `FNMUSIC_DOCKER_MIRRORS`（空格分隔，按序尝试）。

### 容器日志刷 `PermissionError: [Errno 13] Permission denied: '/app/app.py'`

v1.2.1 及更早的已知问题（umask 077 检出导致镜像内非 root 用户读不了源码），
v1.2.2 起已修复。若遇旧镜像残留：`git pull && ./install.sh` 重建即可。
