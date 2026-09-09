# 人工安装与部署指南

适用环境：飞牛 NAS（fnOS）已安装并启动「飞牛音乐」官方应用。本项目采用无侵入接管设计，**完全不修改**飞牛官方 nginx 配置、不 Patch 官方 Go 二进制、不改动官方数据库。

> 💡 **自动化部署提示**：若使用 AI Agent（如 OpenCode、Claude Code、Cursor 等）进行全流程自动化部署与自检验收，请直接查阅 [Agent 安装提示词](AGENT_INSTALL.md)。

---

## 0. 前置准备

- **操作系统**：fnOS（Debian 12 基础系统）；
- **基础运行组件**：Python 3.11+ 及 `python3-venv` 虚拟环境模块；
  ```bash
  sudo apt-get update && sudo apt-get install -y python3 python3-venv git
  ```
- **管理员权限**：具备 `sudo` 执行权限的管理员账号；
- **官方音乐应用**：必须先在 fnOS「应用中心」安装并启动「飞牛音乐」（确保存在 `/var/run/trim_music.socket`）；
- **Docker 环境（若选 Docker 模式）**：必须先在 fnOS「应用中心」安装好 Docker，**脚本绝不会擅自安装 Docker 引擎**；

克隆项目并进入根目录赋予执行权限：
```bash
git clone https://github.com/javycoder/fnos_music_ext.git fnmusic_ext
cd fnmusic_ext
chmod +x install.sh extend.sh restore.sh proxy/run_proxy.sh
```

---

## 1. 安装模式透明说明（消除黑盒困惑）

为了让用户完全掌控系统的变动，下文对扩展代理的两种部署模式进行彻底、透明的直白说明：

### 共通的核心运行原则（必读）
> ⚠️ **无论选择「Docker 容器模式」还是「Host 宿主机本地服务模式」，核心代理服务（`fnmusic-ext`）都必须以宿主机 systemd 运行！**
> 
> **原因**：核心代理的核心任务是零侵入接管宿主机上的 Unix Domain Socket（`/var/run/trim_music.socket`），使官方 nginx 与飞牛原生后端透明桥接。若将代理塞入普通 Docker bridge 容器，将面临复杂的跨容器与宿主机 socket 权限穿透问题，因此核心代理始终由宿主机 systemd（`fnmusic-ext.service`，运行在独立的 `.venv-proxy` 虚拟环境中）原生管理。
> 
> **结论**：两种安装模式的**唯一区别**，仅在于**「音乐源服务（musicdl / musicbox / lxmusic）」以何种方式运行与隔离**。

---

### 方式 A：Docker 容器模式（推荐）

适合绝大多数已在 fnOS「应用中心」启用 Docker 的用户，享有最干净的环境隔离与省心的更新体验。

- **前置条件**：
  * 必须先在 fnOS「应用中心」安装好 Docker。**脚本不会擅自安装 Docker 引擎**；若未安装，向导会友好提醒并引导切换至 Host 模式。
- **会部署什么**：
  * 通过 `docker-compose.yml` 在本地构建并启动按需启用的音源容器：
    1. **`fnmusic-musicdl`**：监听 **`127.0.0.1:8768`**（酷我/咪咕聚合）；
    2. **`fnmusic-musicbox`**：监听 **`0.0.0.0:8770`**（网易云；局域网扫码登录）；
    3. **`fnmusic-lxmusic`**：监听 **`127.0.0.1:8772`**（洛雪风格免登录解析：酷狗/网易/咪咕）；
- **容器网络与权限**：
  * 容器内运行无特权（以非 root 的普通用户运行）；
  * 数据卷严格挂载并隔离在当前项目目录下的 `musicbox-data/` 目录中，不与系统其他目录发生交叉。

---

### 方式 B：Host 宿主机本地服务模式（纯净无 Docker）

适合未安装 Docker、不希望引入容器虚拟化，或追求极致轻量与低资源占用的机器。

- **适用场景**：
  * 未装 Docker 或 NAS 内存/CPU 资源极为宝贵的环境。
- **会部署什么**：
  * **独立的 Python 虚拟环境**：在当前项目目录下分别创建 `.venv-musicdl`、`.venv-musicbox`、`.venv-lxmusic` 与 `.venv-proxy`。依赖严格限制在各自虚拟环境内，**绝不污染系统全局 Python 环境**；
  * **注册轻量 systemd 服务**：
    1. `fnmusic-musicdl.service`：`127.0.0.1:8768`；
    2. `fnmusic-musicbox.service`：`0.0.0.0:8770`（局域网扫码）；
    3. `fnmusic-lxmusic.service`：`127.0.0.1:8772`；
- **数据与缓存管理**：
  * 所有运行时数据（音频缓存 `cache/`、用户收藏 `online_favorites/`、历史记录 `play_history/`、网易云配置与缓存 `musicbox-data/`）严格保存在当前项目根目录下，**绝对不会散落到系统其他地方**。

---

### 双模式透明对照表

| 对比维度 | 方式 A：Docker 容器模式（推荐） | 方式 B：Host 宿主机本地服务模式（纯净无 Docker） |
| :--- | :--- | :--- |
| **推荐指数** | ⭐⭐⭐⭐⭐（环境隔离最彻底） | ⭐⭐⭐⭐（免 Docker、极致轻量） |
| **适用群体** | 已安装 Docker，注重系统纯净度与隔离性 | 未装 Docker、低内存小主机、或追求直接运行 |
| **前置要求** | fnOS 应用中心安装 Docker（**脚本不擅自安装**） | 仅需宿主机具备 `python3` 及 `python3-venv` |
| **核心代理部署** | 宿主机 systemd 服务（`.venv-proxy` 独立虚拟环境） | 宿主机 systemd 服务（`.venv-proxy` 独立虚拟环境） |
| **音源运行形态** | Docker 容器（通过 `docker-compose` 编排管理） | 宿主机 systemd 服务（通过独立 Python venv 隔离） |
| **部署组件与端口** | • `fnmusic-musicdl`：`127.0.0.1:8768`<br>• `fnmusic-musicbox`：`0.0.0.0:8770`（局域网可扫码）<br>• `fnmusic-lxmusic`：`127.0.0.1:8772` | • `fnmusic-musicdl.service`：`127.0.0.1:8768`<br>• `fnmusic-musicbox.service`：`0.0.0.0:8770`<br>• `fnmusic-lxmusic.service`：`127.0.0.1:8772` |
| **Python 环境隔离** | 依赖封装在容器镜像内，宿主机零依赖污染 | 项目目录下 `.venv-musicdl` / `.venv-musicbox`，不污染全局 |
| **权限与安全性** | 容器内无特权用户运行，隔离网络端口 | 独立 systemd 进程，仅监听本地回环网络 |
| **数据落盘路径** | 项目根目录 `cache/`、`online_favorites/`、`musicbox-data/` | 项目根目录 `cache/`、`online_favorites/`、`musicbox-data/` |
| **常规日常管理** | `docker logs -f fnmusic-musicdl`<br>`docker compose ps` | `journalctl -u fnmusic-musicdl -f`<br>`systemctl status fnmusic-musicbox` |
| **一键恢复直连** | 执行 `./restore.sh`（秒级切回原生直连，保留音源与数据） | 执行 `./restore.sh`（秒级切回原生直连，保留音源与数据） |
| **一键彻底卸载** | 执行 `./restore.sh --full`（自动停止并删除 Docker 容器） | 执行 `./restore.sh --full`（自动停止并注销 systemd 音源服务） |

---

### 两种模式的清理与卸载保障（零残留承诺）

不论您采用哪种模式安装，项目均提供了完善、清晰的还原与彻底卸载方案：

1. **日常无损还原**：
   ```bash
   ./restore.sh
   ```
   * 会立即复位 `/var/run/trim_music.socket`，停用代理服务，秒级恢复官方原生直连；
   * 音源服务与本地缓存数据完好保留，日后执行 `./extend.sh` 可秒级重新启用。
2. **彻底清理卸载**：
   ```bash
   ./restore.sh --full
   ```
   * **在 Docker 模式下**：自动停止并删除 `fnmusic-musicdl` 与 `fnmusic-musicbox` 容器；
   * **在 Host 模式下**：自动停止并禁用 `fnmusic-musicdl.service` 与 `fnmusic-musicbox.service`，移除 `/etc/systemd/system/fnmusic-ext.service`；
   * 真正做到系统级服务干净利索、彻底无残留。

---

## 2. 一键安装与配置

### 交互向导安装（新手首选）

```bash
./install.sh
```

向导将自动执行环境安全预检，并提供直观的交互选择：
1. **安装模式**：输入 `1`（Docker 模式）或 `2`（Host 模式）；
2. **音源选择**：可多选，至少选一个：
   * `1`：`musicdl`（酷我/咪咕等，覆盖绝大多数华语热门流行曲目）；
   * `2`：`musicbox`（网易云高品质解析，支持 FLAC/歌词/封面）；
   * `1,2`：双音源并行聚合（强烈推荐，网易云高品质优先，未命中自动回退检索）；
3. **每日推荐（可选）**：支持填入兼容 OpenAI 规范的 API Key，自动为登录用户定制每日歌单；若不使用直接回车跳过；
4. **一键启用**：向导完成后直接确认即可调用 `./extend.sh` 自动接管上线。

### 非交互静默部署示例（进阶运维 / 自动化脚本）

```bash
# 示例 1：推荐配置 —— Docker 模式 + 双音源 + 自动启用
./install.sh --non-interactive --mode docker --sources musicdl,musicbox,lxmusic --extend

# 示例 2：纯净轻量 —— Host 宿主机模式 + 仅 musicdl 音源
./install.sh --non-interactive --mode host --sources musicdl --extend

# 示例 3：启用大模型每日推荐（密钥保存在项目本地 .env 中，权限为 600）
./install.sh --non-interactive --mode docker --sources musicdl,musicbox,lxmusic --enable-recommend \
  --llm-base-url 'https://api.openai.com/v1' \
  --llm-api-key 'sk-xxxxxx' \
  --llm-model 'gpt-4o-mini' \
  --extend
```

---

## 3. 启用与还原

```bash
# 1. 启用扩展接管（包含全链路健康与流式验收，失败自动秒级回滚）
./extend.sh

# 2. 还原官方原生直连（保留音源组件与本地缓存）
./restore.sh

# 3. 深度彻底还原（同时停止并删除音源 Docker 容器或宿主机 systemd 音源服务）
./restore.sh --full
```

---

## 4. 网易云登录扫码（若启用 musicbox）

网易云部分 VIP 或无损音质曲目需要用户登录。在局域网内任意设备的浏览器访问：
```text
http://<飞牛NAS的IP地址>:8770/api/v1/auth/login/qr.png
```
使用手机【网易云音乐 App】扫码确认登录即可，登录凭证自动持久化在本地 `musicbox-data/` 目录中，无需重复扫码。

---

## 5. 每日推荐工作机制（可选）

当在 `.env` 中配置了大模型密钥后，用户登录飞牛音乐 Web 端或 App，左侧歌单顶部将呈现专属「每日推荐」歌单：
1. **收集口味种子**：根据当前用户的近期播放历史与收藏歌曲提取音乐风格与歌手；
2. **大模型智能推荐**：通过 LLM 生成 30 首风格契合的候选曲目；
3. **在线音源校验**：自动检索音源有效可用曲目（自动过滤已收藏曲目），凑满 20 首；
4. **每天定时换新**：每日清晨自动失效旧缓存并生成最新推荐，保持听歌新鲜感。

---

## 6. 健康检查与验收

在终端执行以下命令探测代理服务与上游各组件的连通状态：

```bash
# 探测代理端点健康状态
curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz

# 运行本地自动化测试集（需先 pip install -r proxy/requirements.txt pytest qrcode pillow）
python3 -m pytest
```

`healthz` 响应正常示例：
```json
{"ok":true,"upstream":"ok","musicdl":"ok","musicbox":"ok","llm":"disabled"}
```
- `ok` 为 `true` 表示上游官方音乐后端连通正常，且至少有一个音源处于工作状态；
- 未启用的音源会显示为 `"disabled"`。

## 7. 常见问题排查

### 容器日志刷 `PermissionError: [Errno 13] Permission denied: '/app/app.py'`

v1.2.1 及更早版本的已知问题：镜像内源码文件权限继承了仓库检出时的 umask。
若曾在 umask 077 的环境（root shell、`sudo git clone` 等）下检出仓库，`app.py` 为 600，
容器内非 root 的 `appuser` 无法读取，uvicorn 启动失败并随 `restart: unless-stopped` 无限重启。

v1.2.2 起已修复（镜像内文件统一 `--chown=appuser` 且权限 644，与宿主机文件权限解耦）。
升级方法：

```bash
git pull && ./install.sh
```

Dockerfile 的变更会使对应构建层缓存失效，重新安装时会自动重建镜像，无需 `--no-cache`。

### 构建时报 `failed to resolve source metadata for python:3.13-slim ... 401 Unauthorized` 或拉取超时

多为 fnOS 等系统在 Docker daemon 全局配置的镜像加速器（如 `docker.fnnas.com`）异常所致：
BuildKit 解析 `python:3.13-slim` 元数据时会先经过该加速器，失败后不会自动回退官方 Docker Hub，
`docker compose up --build` 随即失败。

v1.2.3 起安装脚本会在构建前自动探测可用源：**国内镜像优先**（完整镜像源引用直连，绕开 daemon
加速器，真实拉取验证），逐个尝试 docker.1ms.run / docker.m.daocloud.io / docker.1panel.live /
hub.rat.dev，全部失败再兜底官方源，结果缓存到 `.env` 的 `FNMUSIC_BASE_IMAGE`。
全程不修改系统 Docker 配置，仅本应用构建生效。

手动指定（例如自动探测全部失败、或偏好特定镜像源时）：

```bash
# 方式一：安装时通过环境变量指定
BASE_IMAGE=docker.m.daocloud.io/library/python:3.13-slim ./install.sh

# 方式二：写入 .env（之后所有重建自动沿用）
# FNMUSIC_BASE_IMAGE=docker.m.daocloud.io/library/python:3.13-slim
```

自定义国内镜像候选列表：设置环境变量 `FNMUSIC_DOCKER_MIRRORS`（空格分隔，按序尝试）。

Dockerfile 的变更会使对应构建层缓存失效，重新安装时会自动重建镜像，无需 `--no-cache`。
