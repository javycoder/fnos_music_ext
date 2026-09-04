# Agent 安装提示词

把下面整段复制给 Agent。要求：只改本仓库与本机配置，禁止改飞牛系统文件，禁止把密钥写入 git。

```text
你在一台已安装飞牛 NAS（fnOS）和「飞牛音乐」(trim.music) 的机器上工作。
仓库是 fnmusic-ext：无侵入 Unix Socket 代理，扩展在线搜索/播放/歌词/封面/边听边存。

硬约束（违反即失败）：
1. 禁止修改 /usr/trim/nginx 以及任何 nginx 配置；飞牛会回写。
2. 禁止 patch trim-music 二进制，禁止写入官方 music.db（只读 play_history 可以）。
3. 禁止把 API Key、密码、token 写进源码、测试、README、commit、issue。
4. 一键扩展 ./extend.sh 与一键还原 ./restore.sh 必须保持可用；失败要能回滚到官方直连。
5. 音源可多选、至少选一个：
   - musicdl: https://github.com/CharlesPikachu/musicdl（musicdl-service，127.0.0.1:8768）
   - musicbox: https://github.com/darknessomi/musicbox（musicbox-service，127.0.0.1:8770）
   用户未指定时非交互默认仅 musicdl；用户要求网易云则加 --sources musicdl,musicbox 或 --sources musicbox。

安装步骤：
1. 进入仓库根目录，chmod +x install.sh extend.sh restore.sh proxy/run_proxy.sh
2. 执行安装（示例）：
   - 仅 musicdl（Docker，推荐）：./install.sh --non-interactive --mode docker
   - 两个音源：./install.sh --non-interactive --mode docker --sources musicdl,musicbox
   - 仅 musicbox：./install.sh --non-interactive --mode docker --sources musicbox
   - 宿主机音源：把 --mode docker 换成 --mode host
3. 若用户提供了 OpenAI 兼容的 base url 和 api key，则加上：
     --enable-recommend --llm-base-url '<用户给的 URL>' --llm-api-key '<用户给的 KEY>' --llm-model '<模型或 gpt-4o-mini>'
   密钥只写入仓库 .env（chmod 600），不要 echo 到日志。
   用户没给密钥：不要开启每日推荐。
4. 确认飞牛音乐已运行且存在 /var/run/trim_music.socket 后执行 ./extend.sh
5. 验证：
   curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz
   应含 "ok": true, "upstream":"ok"；已启用音源为 "ok"，未启用为 "disabled"。
   bash -n extend.sh restore.sh install.sh
   python3 -m py_compile proxy/app.py proxy/recommend.py
6. 还原演练（可选）：./restore.sh 后再 ./extend.sh，官方音乐不能坏。

完成后用简短中文说明：安装模式、启用了哪些音源、推荐是否开启（不要复述密钥）、healthz 结果、extend 是否成功。
```
