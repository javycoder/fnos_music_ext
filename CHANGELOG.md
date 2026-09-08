# 更新日志 (Changelog)

本项目所有显著变更均记录于此文件。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

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
