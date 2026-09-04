# musicbox-service

HTTP 包装 [darknessomi/musicbox](https://github.com/darknessomi/musicbox)（PyPI：`NetEase-MusicBox`），供 fnmusic-ext 作为网易云音源（默认 `127.0.0.1:8770`）。

部分曲目可能需要扫码登录：`GET /api/v1/auth/login/qr.png`。

本地运行：

```bash
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8770
```
