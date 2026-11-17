"""测试网易 sky 沉浸声音质及 proxy 降档阶梯逻辑。"""
import pytest
from proxy.app import CONF, _resolve_netease_url_uncached
import httpx


@pytest.mark.anyio
async def test_resolve_netease_url_sky_downgrade_ladder(monkeypatch):
    """验证当新配置为 sky 时，proxy 按照质量阶梯逐级向下请求网易音源。"""
    from proxy.app import _NETEASE_URL_PIN
    _NETEASE_URL_PIN.pop("987654", None)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = str(request.url.params.get("quality"))
        seen.append(q)
        if q == "lossless":
            return httpx.Response(200, json={"ok": True, "data": {"code": 200, "url": "http://audio/test.flac"}})
        return httpx.Response(200, json={"ok": True, "data": {"code": 404, "url": ""}})

    monkeypatch.setitem(CONF, "play_quality", "sky")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8770") as client:
        res = await _resolve_netease_url_uncached(client, "987654", fresh=True)
        assert res == "http://audio/test.flac"

    # sky 失败 -> jymaster 失败 -> hires 失败 -> lossless 成功
    assert seen == ["sky", "jymaster", "hires", "lossless"]
    _NETEASE_URL_PIN.pop("987654", None)
