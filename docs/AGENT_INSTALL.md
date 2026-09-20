# Agent 安装提示词

本提示词专为 AI CLI Agent（如 OpenCode、Claude Code、Cursor 等）自动化部署与运维设计。
人工详细安装步骤、部署形态说明与背景见：[安装与部署指南](INSTALL.md)。

把下面代码块内的整段内容复制给 Agent。要求：只改本仓库与本机配置，禁止改飞牛系统文件，禁止把密钥写入 git。

```text
你在一台已安装飞牛 NAS（fnOS）和「飞牛音乐」(trim.music) 的机器上工作。
仓库是 fnmusic-ext v2.0.0：无侵入 Unix Socket 代理，扩展在线搜索/播放/歌词/封面/
边听边存；三个音源 + WebUI 合并为单个 Docker 容器 fnmusic-sources（supervisor 按
.env 只启动所选音源进程）。

【核心运行原则与硬约束（违反即失败）】
1. 禁止修改 /usr/trim/nginx 以及任何 nginx 配置；飞牛系统更新或配置重载会回写覆盖。
2. 禁止 patch trim-music 官方二进制，禁止写入官方 music.db（只读读取 play_history 进行口味分析可以）。
3. 禁止把 API Key、密码、token 写进源码、测试、README、commit、issue 或 echo 打印到终端日志。所有密钥仅保存在仓库根目录 .env（文件权限 chmod 600）。
4. 绝对禁止擅自安装 Docker 引擎：fnOS 的 Docker 必须在「应用中心」由系统管理员安装。v2.0.0 仅支持 Docker 部署：若环境未安装 Docker 或 docker daemon 不可用，install.sh 会直接报错退出——此时应报告用户先安装 Docker，严禁执行 apt-get install docker 等命令，严禁尝试任何 host 模式替代。
5. 部署形态：核心代理由宿主机 systemd（项目根 .venv-proxy 虚拟环境）运行并接管 /var/run/trim_music.socket；音源（musicdl 8768 / musicbox 8770 / lxmusic 8772）与 WebUI（8774）全部在单容器 fnmusic-sources 内按需运行。
6. 音源三选一互斥：--sources 与 .env 的三个 FNMUSIC_*_ENABLED 开关只能有一个为 true，跨音源组合会被 install.sh 拒绝。换源属于运行期操作（WebUI 或改 .env 后 ./extend.sh），不要通过重装切换。
7. 洛雪 lxmusic 源：播放解析依赖用户提供的洛雪自定义源脚本 URL（LX_SOURCE_URL）。非交互安装选 lxmusic 时必须携带 --lx-source-url '<URL>'；安装器会在容器内做全链路校验（下载→初始化→搜索→解析→探活）。校验失败分类提示，Agent 应把原始错误转告用户而不是自行编造 URL。
8. 一键扩展 ./extend.sh 与一键还原 ./restore.sh（含彻底清理 ./restore.sh --full）必须始终保持可用；扩展失败必须安全秒级回滚到官方直连。
9. 单机单部署：代理单元名、音源容器名与安装锁全局唯一，本机以 /var/lib/fnmusic-ext/deployment 登记当前部署目录。从另一份仍存在的仓库副本执行安装/扩展/还原会被拒绝；Agent 不得用克隆目录绕过，应在原部署目录操作，或经用户确认后使用 --adopt 显式迁移部署。原登记目录已删除时不拦截。
10. WebUI（端口 8774）无鉴权，仅限可信内网；安装开关为 --webui / --no-webui，非交互默认不装。

【自动化部署执行步骤】

步骤 1：准备脚本权限
在仓库根目录执行：
  chmod +x install.sh extend.sh restore.sh proxy/run_proxy.sh

步骤 2：环境预检（install.sh 会自动执行，Agent 应先行确认）
  1. Python 环境：python3 (>=3.11) 与 python3-venv；缺失时安装：sudo apt-get update && sudo apt-get install -y python3 python3-venv。
  2. 管理员权限：当前用户具备 sudo 权限（非交互需免密 sudo）。
  3. 飞牛音乐运行套接字 /var/run/trim_music.socket 存在；不存在则提示用户先在「应用中心」安装并启动「飞牛音乐」。
  4. Docker：command -v docker 且 docker info 可用。任一不满足即停止并报告（见硬约束 4）。

步骤 3：执行安装与一步到位启用（--extend）
推荐命令（按用户所选音源三选一）：
  - 网易云 musicbox（含 WebUI）：
    ./install.sh --non-interactive --sources musicbox --webui --extend
  - musicdl（默认精选酷我+咪咕；平台粒度用 musicdl-<短名> 或编号）：
    ./install.sh --non-interactive --sources musicdl --extend
  - 洛雪 lxmusic（--lx-source-url 必填）：
    ./install.sh --non-interactive --sources lxmusic --lx-source-url '<URL>' --extend
  - 大模型推荐兜底（可选；密钥仅写入 .env，禁止 echo）：
    追加 --enable-recommend --llm-base-url '<URL>' --llm-api-key '<KEY>' --llm-model '<模型>'
  - 若安装时未加 --extend，则需在安装完成后显式执行 ./extend.sh。

步骤 4：端到端健康检查与验收
  1. 探测代理接管与各组件健康端点：
     curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz
     期望 "ok": true 且 "upstream": "ok"；已启用音源为 "ok"，未启用为 "disabled"。
  2. 所选音源容器内端点（仅启用项）：
     curl -s http://127.0.0.1:8768/healthz   # musicdl
     curl -s http://127.0.0.1:8770/healthz   # musicbox
     curl -s http://127.0.0.1:8772/healthz   # lxmusic（检查 user_source.initialized）
  3. Shell 语法检查：bash -n install.sh extend.sh restore.sh proxy/run_proxy.sh
  4. Python 语法检查：.venv-proxy/bin/python -m py_compile proxy/app.py proxy/recommend.py
     （可选：.venv-proxy/bin/python -m pytest -q）

步骤 5：还原机制与彻底卸载规范（知悉与必要时使用）
  - 日常无损还原：./restore.sh —— 复位 Socket、停用代理、停止音源容器，秒级恢复官方直连；.env 与全部数据保留。
  - 彻底清理：./restore.sh --full —— 额外删除 .env、登录态、缓存、收藏、历史与 .venv-*。

【完成汇报规范】
任务完成后用简短中文输出总结，内容包含：
1. 部署形态确认（Docker 单容器 + 宿主机核心代理）与环境预检结论；
2. 所选音源与端口（musicdl 127.0.0.1:8768 / musicbox 0.0.0.0:8770 / lxmusic 127.0.0.1:8772）及 WebUI 是否安装（8774）；
3. 洛雪源校验结论（若适用：源名称/版本/推导平台）；每日推荐是否开启（严禁复述敏感密钥）；
4. healthz 接口探测响应 JSON；
5. extend 链路接管与验收状态。
```
