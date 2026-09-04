# Contributing

## Ground rules

- Keep the Unix Socket takeover model. Do not change nginx files.
- `./extend.sh` and `./restore.sh` must remain idempotent and safe.
- No secrets in commits, tests, or sample configs (use `.env.example` placeholders).
- Prefer adding tests in `proxy/tests/` for any proxy behavior change.

## Dev loop

```bash
python3 -m py_compile proxy/app.py proxy/recommend.py
bash -n extend.sh restore.sh install.sh proxy/run_proxy.sh
.venv-proxy/bin/python -m pytest proxy/tests -q
```

Default music source is [musicdl](https://github.com/CharlesPikachu/musicdl) wrapped by `musicdl-service/`.
