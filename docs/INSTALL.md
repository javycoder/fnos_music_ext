# 人工安装

适用：飞牛 NAS（fnOS）已安装「飞牛音乐」应用。本扩展**不修改** nginx、官方二进制或官方数据库。

## 0. 准备

- Python 3.11+
- 能 `sudo` 的管理员账号
- Docker 模式还需要 Docker / Compose
- 飞牛音乐应用处于运行状态（存在 `/var/run/trim_music.socket`）

```bash
git clone <this-repo> fnmusic_ext
cd fnmusic_ext
chmod +x install.sh extend.sh restore.sh proxy/run_proxy.sh
```

## 1. 一键安装配置

交互安装（会询问模式、音源、是否开启每日推荐）：

```bash
./install.sh
```

音源可多选、至少选一个：

| 编号 | 名称 | 上游 | 本地端口 |
| --- | --- | --- | --- |
| 1 | musicdl | https://github.com/CharlesPikachu/musicdl | `127.0.0.1:8768` |
| 2 | musicbox | https://github.com/darknessomi/musicbox | `127.0.0.1:8770` |

交互缺省 `1,2`（两个都装）；非交互缺省仅 `musicdl`。

非交互示例：

```bash
# 仅 musicdl
./install.sh --non-interactive --mode docker

# 仅网易云 musicbox
./install.sh --non-interactive --mode docker --sources musicbox

# 两个音源都启用
./install.sh --non-interactive --mode docker --sources musicdl,musicbox

# 开启每日推荐（OpenAI 兼容接口；密钥写入 .env，chmod 600）
./install.sh --non-interactive --mode docker --sources musicdl,musicbox --enable-recommend \
  --llm-base-url 'https://api.example.com/v1' \
  --llm-api-key 'YOUR_KEY' \
  --llm-model 'gpt-4o-mini' \
  --extend
```

- `--mode docker`：已选音源以容器运行（推荐，与飞牛系统隔离）
- `--mode host`：宿主机 venv + `fnmusic-musicdl.service` / `fnmusic-musicbox.service`

代理本身在 fnOS 上必须以 **宿主机 systemd** 运行，因为它要接管 `/var/run/trim_music.socket`。不要把代理塞进普通 bridge 网络容器。

网易云部分曲目可能需要扫码：`http://127.0.0.1:8770/api/v1/auth/login/qr.png`。

## 2. 启用 / 还原

```bash
./extend.sh          # 接管 socket，失败自动回滚
./restore.sh         # 还原官方直连
./restore.sh --full  # 同时停止 musicdl / musicbox 容器与宿主机 unit
```

## 3. 每日推荐

已登录后左侧「歌单」顶部会出现「每日推荐」（首页四张卡片不会变）。每天换一份：删掉前一天缓存，再生成当天 20 首歌。

流程：最近播放 + 收藏作为口味种子 → 大模型出 30 首候选 → 在线音源检索可用曲目（跳过已收藏）→ 凑满 20 首。仅当 `.env` 中同时有 `FNMUSIC_LLM_BASE_URL` 与 `FNMUSIC_LLM_API_KEY` 时走大模型；不填则按歌手做在线检索补齐，歌单仍会每天更新。

密钥留空不影响搜索与播放。

## 4. 校验

```bash
curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz
.venv-proxy/bin/python -m pytest proxy/tests -q
```

`healthz` 的 `ok` 需要上游健康，且至少一个已启用音源为 `ok`。未选中的音源显示 `"disabled"`。
