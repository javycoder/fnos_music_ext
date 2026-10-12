# Contributing

## Ground rules

- Keep the Unix Socket takeover model. Do not change nginx files.
- `./extend.sh` and `./restore.sh` must remain idempotent and safe.
- No secrets in commits, tests, or sample configs (use `.env.example` placeholders).
- Prefer adding tests in `proxy/tests/` for any proxy behavior change.

## Dev loop

每次 push 前本地跑一遍全量检查（与 CI `.github/workflows/ci.yml` 一致）：

```bash
python3 -m py_compile proxy/app.py proxy/recommend.py proxy/env_merge.py
while IFS= read -r -d '' script; do bash -n "$script"; done < <(git ls-files -z '*.sh')
python3 -m pytest   # 单条命令跑全部测试（proxy + 各音源服务），见 pytest.ini
```

提 PR 前请确保 `python3 -m pytest` 全绿。CI 在 main/dev 的 push 和所有 PR 上执行：

- Python 3.11/3.13 确定性测试、逐文件 Shell 语法检查、阻断式 Shellcheck error 检查。
- 三个音源各自安装真实生产依赖，运行离线导入和 CLI 契约检查。
- 三个 Python 3.13 生产镜像构建、非 root 运行和隔离网络内 HTTP 启动检查。

安装接管测试只使用临时 Unix socket 和替身命令，禁止在开发机运行安装/恢复来代替测试。镜像启动 smoke 不连接真实音乐平台；它验证运行环境，不证明远端歌曲可播。

真实远端检查是单独的可选验收：先启动独立的 LX 测试实例，再运行：

```bash
python3 tests/integration/source_smoke.py --base-url http://127.0.0.1:8773 --keyword '有权访问的测试音频标题'
```

该命令逐源低频搜索并解析一条结果，输出带时间戳的脱敏报告，不打印直链、不上传配置，也不把有限探活当作完整播放证明。公共第三方故障不进入确定性 PR 门禁。完整时长、解码和实际音质仍需使用获得授权的音频做独立验收。

Default music source is [musicdl](https://github.com/CharlesPikachu/musicdl) wrapped by `musicdl-service/`.
