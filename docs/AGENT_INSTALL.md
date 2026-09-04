# Agent 安装提示词

本提示词专为 AI CLI Agent（如 OpenCode、Claude Code、Cursor 等）自动化部署与运维设计。
人工详细安装步骤、部署模式差异与背景说明见：[人工安装与部署指南](INSTALL.md)。

把下面代码块内的整段内容复制给 Agent。要求：只改本仓库与本机配置，禁止改飞牛系统文件，禁止把密钥写入 git。

```text
你在一台已安装飞牛 NAS（fnOS）和「飞牛音乐」(trim.music) 的机器上工作。
仓库是 fnmusic-ext：无侵入 Unix Socket 代理，扩展在线搜索/播放/歌词/封面/边听边存。

【核心运行原则与硬约束（违反即失败）】
1. 禁止修改 /usr/trim/nginx 以及任何 nginx 配置；飞牛系统更新或配置重载会回写覆盖。
2. 禁止 patch trim-music 官方二进制，禁止写入官方 music.db（只读读取 play_history 进行口味分析可以）。
3. 禁止把 API Key、密码、token 写进源码、测试、README、commit、issue 或 echo 打印到终端日志。所有密钥仅保存在仓库根目录 .env（文件权限 chmod 600）。
4. 绝对禁止擅自安装 Docker 引擎：fnOS 的 Docker 必须在「应用中心」由系统管理员安装。若环境未安装 Docker，必须选用 Host 宿主机模式，严禁执行 apt-get install docker 等命令。
5. 核心代理运行原则：无论选择 Docker 模式还是 Host 模式，核心代理服务（fnmusic-ext）都必须由宿主机 systemd（运行在项目根目录 .venv-proxy 独立虚拟环境中）原生管理，负责无侵入接管 /var/run/trim_music.socket。两种模式的区别仅在于「音源服务（musicdl / musicbox）」以何种方式运行与隔离。
6. 一键扩展 ./extend.sh 与一键还原 ./restore.sh（含彻底清理 ./restore.sh --full）必须始终保持可用；扩展失败必须安全秒级回滚到官方直连。
7. 音源组件与端口规划：
   - musicdl: https://github.com/CharlesPikachu/musicdl（负责酷我/咪咕聚合搜索与直链解析，服务仅监听本地 127.0.0.1:8768 本地回环，不对外暴露）。
   - musicbox: https://github.com/darknessomi/musicbox（负责网易云高品质解析，Docker 模式下映射 0.0.0.0:8770 绑定所有网络接口，以便局域网内用手机访问 http://<NAS-IP>:8770/api/v1/auth/login/qr.png 进行网易云 App 扫码登录；核心代理内部通过 127.0.0.1:8770 调用；登录凭证持久化隔离在 musicbox-data/）。
   - 音源可多选、至少选一个。用户未指定时非交互默认仅 musicdl；用户需要网易云或双音源时使用 --sources musicdl,musicbox 或 --sources musicbox。

【自动化部署执行步骤】

步骤 1：准备脚本权限
在仓库根目录执行：
  chmod +x install.sh extend.sh restore.sh proxy/run_proxy.sh

步骤 2：环境预检（Preflight Checks）与模式决策
在执行安装前进行环境检测判断（install.sh 会自动执行安全预检，Agent 应先行确认或理解预检逻辑）：
  1. Python 环境：检查系统具备 python3 (>=3.11) 以及 python3-venv 模块。若缺失，需先安装：sudo apt-get update && sudo apt-get install -y python3 python3-venv。
  2. 管理员权限：确认当前执行用户具备 sudo 权限（非交互脚本需免密 sudo）。
  3. 飞牛音乐运行套接字：确认官方套接字 /var/run/trim_music.socket 存在。若不存在，提示用户必须先在 fnOS「应用中心」安装并启动「飞牛音乐」。
  4. Docker / Host 模式决策：
     - 运行 command -v docker 检测 Docker 环境：
       * 若已安装 Docker：采用 --mode docker（推荐，音源在 compose 轻量无特权容器中运行，隔离最彻底）。
       * 若未安装 Docker：采用 --mode host（使用项目内独立的 .venv-musicdl / .venv-musicbox 虚拟环境运行并注册为 systemd 服务，纯净轻量，零全局依赖污染；切勿擅自安装 Docker）。

步骤 3：执行安装与一步到位启用（--extend）
推荐使用 --extend 参数，让 install.sh 在依赖就绪、服务配置完成后自动调用 ./extend.sh 进行 Unix Domain Socket 接管与端到端验收自检，实现安装+接管一步到位：
  - Docker 模式（推荐，双音源一步到位）：
    ./install.sh --non-interactive --mode docker --sources musicdl,musicbox --extend
  - Docker 模式（单音源）：
    ./install.sh --non-interactive --mode docker --sources musicdl --extend
  - Host 模式（宿主机独立 venv，双音源一步到位）：
    ./install.sh --non-interactive --mode host --sources musicdl,musicbox --extend
  - 若用户提供了 OpenAI 兼容的 Base URL 与 API Key，则追加大模型参数（密钥仅写入 .env，禁止 echo 到日志）：
    ./install.sh --non-interactive --mode docker --sources musicdl,musicbox \
      --enable-recommend \
      --llm-base-url '<用户提供的 URL>' \
      --llm-api-key '<用户提供的 KEY>' \
      --llm-model '<模型名称或 gpt-4o-mini>' \
      --extend
    （注：若用户未提供密钥，切勿开启每日推荐）。
  - 若安装时未加 --extend，则需在安装完成后显式执行 ./extend.sh。

步骤 4：端到端健康检查与验收
部署完成后执行以下验证命令：
  1. 探测代理接管与各组件健康端点：
     curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz
     期望输出 JSON 中 "ok": true 且 "upstream": "ok"；已启用的音源状态为 "ok"，未启用的音源为 "disabled"。
  2. Shell 语法检查：
     bash -n extend.sh restore.sh install.sh proxy/run_proxy.sh
  3. Python 代码语法检查：
     .venv-proxy/bin/python -m py_compile proxy/app.py proxy/recommend.py
     （可选运行自动化测试集：.venv-proxy/bin/python -m pytest proxy/tests -q）

步骤 5：还原机制与彻底卸载规范（知悉与必要时使用）
  - 日常无损还原（秒级恢复官方原生直连）：
    ./restore.sh
    复位 Unix Socket，停用 fnmusic-ext 代理服务，秒级恢复官方直连；保留音源容器/服务及本地缓存数据，日后执行 ./extend.sh 可秒级重新上线。
  - 彻底清理卸载（系统级服务零残留）：
    ./restore.sh --full
    在复位 Socket 并停用核心代理的同时，自动停止并删除音源 Docker 容器（fnmusic-musicdl、fnmusic-musicbox）或注销宿主机 systemd 音源服务（fnmusic-musicdl.service、fnmusic-musicbox.service）。

【完成汇报规范】
任务完成后用简短中文输出总结，内容包含：
1. 部署模式判断（Docker 模式或 Host 模式，及环境预检依据）；
2. 启用的音源与端口说明（musicdl: 127.0.0.1:8768，musicbox: 0.0.0.0:8770 局域网扫码地址）；
3. 每日推荐是否开启（严禁复述敏感密钥）；
4. healthz 接口探测响应 JSON；
5. extend 链路接管与验收状态。
```
