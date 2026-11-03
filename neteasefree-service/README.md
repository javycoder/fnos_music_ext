# neteasefree-service

网易云免扫码音源（默认 `127.0.0.1:8776`，容器内 `8006`），对外暴露稳定的 `/api/v1/*` 契约，供 fnmusic-ext 代理调用。

数据来自兼容 [NeteaseCloudMusicApi](https://github.com/Binaryify/NeteaseCloudMusicApi) 协议的 HTTP 后端
（默认 `https://zm.wwoyun.cn`，可用 `.env` 的 `FNMUSIC_NETEASE_API_BASE` 覆盖，见 `netease_api.py`）。
后端自带服务端登录态，免扫码即可解析 VIP 直链，因此无需用户级扫码登录；网易账号歌单恒返回未登录，由代理静默跳过。

本地运行：

```bash
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8776
```

健康检查：`GET /healthz` 返回 `{"status": "ok", "source": "neteasefree"}`。
