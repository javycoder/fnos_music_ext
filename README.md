# fnmusic-ext 飞牛音乐扩展代理

[![CI](https://github.com/javycoder/fnos_music_ext/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/javycoder/fnos_music_ext/actions/workflows/ci.yml)

`fnmusic-ext` 是专为 fnOS（飞牛私有云）自带音乐应用（`trim.music`）量身定制的无侵入式增强扩展。通过接管系统后端通信入口，在**完全不修改官方程序与数据库**的前提下，让原生飞牛音乐秒变全能音乐播放器。

### 🎵 核心带来什么功能？
- **全网在线聚合搜播**：直接在官方搜索框输入歌名/歌手，聚合搜索网易云、酷狗、酷我、咪咕等多平台曲库，在线音乐即点即播；
- **精准歌词与高清封面**：自动补齐在线歌曲的动态滚动 LRC 歌词与高清专辑封面，播放界面完整美观；
- **智能边播边存（无感离线）**：在线听歌时后台自动缓存音频文件，再次播放直接走本地，省外网流量且秒开；
- **全平台原生无感适配**：飞牛网页端、官方手机 App、车载端开箱即用，无需安装任何客户端第三方插件；
- **多用户隔离收藏**：家庭多成员在 App 里点「红心」收藏在线歌曲，彼此数据独立隔离，与本地曲库完美融合；
- **音源原生每日推荐**：默认采信音源原生推荐（网易每日推荐/榜单 + 洛雪免登录榜单，平台 ID 直连并复用可播性验证）；网易未启用时可选接入大模型兜底；
- **多音源自由组合（可选到平台粒度）**：
  - [musicbox](https://github.com/darknessomi/musicbox)（网易云高品质解析，支持扫码登录 VIP/收藏）；
  - [musicdl](https://github.com/CharlesPikachu/musicdl)（酷我/咪咕等 57 个平台聚合，可按平台启用，全部平台编号见 [musicdl-service/PLATFORMS.md](musicdl-service/PLATFORMS.md)）；
  - **lxmusic**（洛雪风格解析：酷狗 kg / 网易 wy / 咪咕 mg / 酷我 kw / QQ tx，可按平台启用。QQ tx 保留搜索/歌词适配，但当前没有可用播放解析链路，默认不启用）。解析结果会进行有限媒体探活；这不能保证完整歌曲、账户权限或直链后续始终可用，请仅访问您有权收听的内容。

---

## 快速开始

### 前置准备
1. 已在 fnOS「应用中心」安装并启动官方 **【飞牛音乐】** 应用
   （v1.4.0 起已适配 2026-09-11 升级后的新版官方应用，旧版同样兼容；
   官方应用升级后若扩展未生效，重新执行 `./extend.sh` 即可恢复）；
2. 宿主机已安装基础依赖（Python 3 及 venv）：
   ```bash
   sudo apt-get update && sudo apt-get install -y python3 python3-venv git
   ```

---

### 1. 拉取项目与赋予权限
```bash
# 1. 克隆仓库代码
git clone https://github.com/javycoder/fnos_music_ext.git fnmusic_ext
cd fnmusic_ext

# 2. 赋予脚本执行权限
chmod +x install.sh extend.sh restore.sh proxy/run_proxy.sh
```

---

### 2. 运行安装向导
执行交互式向导：
```bash
./install.sh
```
向导将引导您选择：
- **安装模式**：
  - `1) Docker 容器模式（推荐）`：音源服务容器化运行，隔离干净；
  - `2) Host 宿主机模式`：通过独立 Python venv 和 systemd 运行，免装 Docker。
- **音源选择**（可多选；编号是全局「源+平台」ID，向导只列出精选）：
  - `1` 网易云 musicbox [端口 8770]
  - `2`–`7` 精选 musicdl：酷我 / 酷狗 / 咪咕 / QQ / 千千 / B站
  - `59`–`62` 精选 lxmusic：酷狗 / 网易 / 咪咕 / 酷我（`63` lx-QQ 仅搜索，见文档）
  - 其余平台对照 [musicdl-service/PLATFORMS.md](musicdl-service/PLATFORMS.md) 的编号直接输入，例如 `49` = mdl-gequhai
  - 示例：输入 `1,2,62` = 网易云 + mdl-酷我 + lx-酷我（对应容器一起安装，搜索按多源并发策略聚合）
  - 整源启用请写名字（默认平台）：`musicbox,musicdl,lxmusic`（**不要**再用 `1,2,3` 表示三整源）
- **每日推荐**：默认使用音源原生推荐（网易云每日推荐/榜单 + 洛雪免登录榜单，榜单按所选 lx 平台自动过滤），无需额外配置；未启用网易音源时可选配置大模型作为兜底。

> 💡 **进阶：非交互静默安装示例**（一行命令全自动完成并启用）：
> ```bash
> ./install.sh --non-interactive --mode docker --sources=musicbox,musicdl,lxmusic --extend
> # 或按平台粒度：网易云 + lx-酷我 + mdl-酷我
> ./install.sh --non-interactive --mode docker --sources=1,2,62 --extend
> ```
> 全部平台编号请查看 [musicdl-service/PLATFORMS.md](musicdl-service/PLATFORMS.md)（`1,2,3` 现为平台编号，不再表示三整源）。

---

### 3. 启用与验证
若安装向导中未选择自动启用，可随时手动执行：
```bash
# 一键接管并启用扩展（含全链路自动化验收测试）
./extend.sh
```
- **在线听歌**：打开飞牛音乐 Web 端或手机 App，在搜索框输入歌曲名（如“晴天”），直接在线即点即播；
- **网易云扫码**（若启用了 musicbox）：终端运行 `./netease_login.sh`（或 `./install.sh --qr` / `./extend.sh --qr`）进行交互式扫码登录（终端展示 ASCII 二维码、过期自动刷新与状态轮询；局域网亦可访问可选图片 `http://<NAS_IP>:8770/api/v1/auth/login/qr.png`）；
- **健康检查**：在终端探测各组件连通状态：
  ```bash
  curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz
  ```

---

### 4. 维护与一键还原
- **还原官方原生直连**（秒级切回；同时停止并删除音源容器，保留 .env 与全部数据）：
  ```bash
  ./restore.sh
  ```
- **彻底卸载清理**（在上一步基础上，连同 .env、网易云登录、缓存、收藏、历史一并删除）：
  ```bash
  ./restore.sh --full
  ```
- **多副本部署提示**：容器名（`fnmusic-musicdl/musicbox/lxmusic`）与端口（8768/8770/8772）全局固定。
  安装/恢复会检查部署归属并串行化操作；遇到其他目录的服务或容器会拒绝接管，不再自动删除。
  请在现有部署目录维护服务。身份不明或官方 socket 已改变时，恢复会保留现场并报告未完成，禁止手工猜测后删除 socket。

---

## 环境变量配置

项目根目录的 `.env` 文件由安装向导自动生成与维护，主要配置项说明：

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `FNMUSIC_MODE` | `docker` | 运行模式：`docker` 或 `host` |
| `FNMUSIC_NETEASE_ENABLED` | `true` | 是否启用网易云音源 (`musicbox`) |
| `FNMUSIC_MUSICBOX_URL` | `http://127.0.0.1:8770` | 网易云音源服务地址 |
| `FNMUSIC_MUSICDL_ENABLED` | `true` | 是否启用聚合音源 (`musicdl`) |
| `FNMUSIC_MUSICDL_URL` | `http://127.0.0.1:8768` | 聚合音源服务地址 |
| `FNMUSIC_LX_ENABLED` | `true` | 是否启用洛雪免登录音源 (`lxmusic`) |
| `FNMUSIC_LX_URL` | `http://127.0.0.1:8772` | 洛雪音源服务地址 |
| `LX_SOURCES` | `kg,wy,mg,kw` | lxmusic 启用的平台列表（`kg/wy/mg/kw/tx`）；`install.sh --sources lx-<平台>` 或向导选择后写入，代理搜索/播放/榜单按此过滤；修改后重装生效 |
| `LX_THIRD_PARTY` | `1` | lxmusic 第三方解析链路总开关（关闭后退化为官方免登录直连，kw/tx 无结果） |
| `LX_RESOLVER_TIMEOUT` | `4.0` | 第三方链路单次解析超时（秒） |
| `FNMUSIC_ONLINE_SOURCES` | `MiguMusicClient,KuwoMusicClient` | musicdl 启用的子平台列表（短名或全名均可）；`install.sh --sources musicdl-<平台>` 或向导选择后写入 |
| `MUSICDL_SOURCES` | `KuwoMusicClient,MiguMusicClient` | musicdl 容器/服务的默认平台白名单（与上键联动写入，重装生效） |
| `FNMUSIC_TEE_SAVE_ENABLED` | `true` | 边听边存开关：完整试听在线歌后自动保存到本地曲库 |
| `FNMUSIC_TEE_SAVE_DIR` | *(空)* | 边听边存保存路径；留空=自动探测飞牛共享曲库，配置后以配置为准，不可用自动回退 |
| `FNMUSIC_TEE_CACHE_MAX` | `2` | 关闭边听边存时滚动保留的最新试听缓存条数（仅在关闭时生效；restore 会清理） |
| `FNMUSIC_SEARCH_TIMEOUT` | `3.0` | 多音源并发搜索常规等待预算（秒） |
| `FNMUSIC_SEARCH_CACHE_TTL`| `604800` | 搜索缓存上限；成功结果最长 5 分钟后刷新，空/部分失败结果使用更短时效 |
| `FNMUSIC_LLM_BASE_URL` | *(空)* | 大模型 Base URL（仅网易音源未启用时作为每日推荐兜底） |
| `FNMUSIC_LLM_API_KEY` | *(空)* | 大模型 API Key（仅网易音源未启用时使用） |
| `FNMUSIC_LLM_MODEL` | `gpt-4o-mini` | 兜底推荐生成模型 |
| `FNMUSIC_VERSION` | `1.5.0` | 当前安装的版本号 |

---

## 实现原理

### 1. Inode 接管与零侵入无缝串联
飞牛官方架构中，前端 Nginx 通过本地 Unix Domain Socket（`/var/run/trim_music.socket`）与官方 Go 编写的后端服务通信。

`fnmusic-ext` 巧妙利用 Linux 文件系统的 Socket Inode 机制：
1. 将官方套接字平滑重命名为 `trim_music_upstream.socket`；
2. 代理服务在原路径 `/var/run/trim_music.socket` 建立同名监听并赋予相同权限；
3. 官方 Nginx 与客户端对此完全无感知。

```text
[飞牛音乐客户端 (Web/App)]
          │
          ▼
    [飞牛 Nginx 代理]
          │ (通过 Unix Socket 请求)
          ▼
┌────────────────────────────────────────────────────────┐
│  fnmusic-ext 扩展代理 (/var/run/trim_music.socket)     │
│  ├─ 本地接口透传 ──► 官方后端 (trim_music_upstream.sock)│
│  ├─ 在线搜索聚合 ──► 并发调度 musicbox / musicdl / lx   │
│  ├─ 边播边落盘   ──► 流式 Tee 写入本地 cache/ 目录      │
│  └─ 每日推荐歌单 ──► 注入音源原生推荐/榜单（或 LLM 兜底） │
└────────────────────────────────────────────────────────┘
```

### 2. 核心拦截与增强逻辑
- **搜索拦截 (`/music/api/v1/search/track`)**：
  透传请求给官方服务获取本地歌曲，同时并发调度已启用的在线音源服务；按 `(title, artist)` 去重合并后渐进式返回。
- **流媒体播放与边播边存 (`/music/api/v1/track/stream`)**：
  拦截带有在线标识（`online:...`）的 GUID，解析真实流直链后返回 `206 Partial Content` 流式切片，并在后台通过独立的 Tee Task 异步将音频写入本地缓存。下次播放直接命中本地文件，秒开且省外网流量。
  边听边存由 `FNMUSIC_TEE_SAVE_ENABLED` 控制（默认开）：开启时完整试听的歌曲以
  `歌手 - 歌名` 保存到 `FNMUSIC_TEE_SAVE_DIR`（留空自动探测飞牛共享曲库，配置不可用自动回退）；
  关闭时仅滚动缓存最新 `FNMUSIC_TEE_CACHE_MAX` 首（默认 2）到 cache 目录供秒开重播，
  不进曲库，`restore.sh` 还原时一并清理。
- **多用户隔离在线收藏 (`/music/api/v1/favorite/track`)**：
  用户点击红心时，拦截请求按当前登录用户的 GUID 独立记录在本地 `online_favorites/`，获取收藏列表时与官方本地收藏自动合并展示。
- **容灾与自动降级**：
  若任何在线音源出现异常或超时，代理自动隔离故障源并仅返回可用数据；若扩展代理进程异常退出，系统自动触发还原逻辑切回官方直连，绝不影响 NAS 原有音乐库的使用。

---

## 免责与版权声明

### 1. 技术研究与非商业用途
- 本项目（`fnmusic-ext`）基于 **MIT 许可证** 开源发布，立项初衷仅为个人开发者探讨 Linux Unix Domain Socket 机制、透明反向代理技术、流式媒体传输与多协程并发架构的技术验证与学习交流。
- 本项目严格限定于**个人技术研究与非商业用途**。任何个人、团队或商业实体严禁将本项目、其衍生版本或相关工具用于任何形式的商业营利、付费订阅、软硬件捆绑销售或非法牟利行为。

### 2. 致谢上游开源项目与无侵权声明
- 本项目在线音源检索与元数据抓取能力依赖于社区优秀的开源组件：
  - [CharlesPikachu/musicdl](https://github.com/CharlesPikachu/musicdl)
  - [darknessomi/musicbox](https://github.com/darknessomi/musicbox)
  - 洛雪音乐（LX Music）社区音源思路与 [pdone/lx-music-source](https://github.com/pdone/lx-music-source) 社区聚合音源的链路清单思路（本仓库 `lxmusic-service` 为独立 Python 实现，仅移植其多链路回退架构，不包含其脚本代码）
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
