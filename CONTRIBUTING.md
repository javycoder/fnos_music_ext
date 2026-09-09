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
bash -n extend.sh restore.sh install.sh netease_login.sh ensure_base_image.sh proxy/run_proxy.sh
python3 -m pytest   # 单条命令跑全部测试（proxy + 各音源服务），见 pytest.ini
```

提 PR 前请确保 `python3 -m pytest` 全绿；CI 会对每个 push / PR 自动执行同样的检查。

Default music source is [musicdl](https://github.com/CharlesPikachu/musicdl) wrapped by `musicdl-service/`.
