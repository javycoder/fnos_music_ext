"""测试网易 sky 沉浸声音质及 NEMbox 真实能力映射与防静默改低逻辑。"""
import sys
import types
import pytest
from starlette.testclient import TestClient

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "musicbox-service"))
import app as mb_app
import playback as mb_playback
from proxy.app import CONF, _NETEASE_QUALITY_LADDER, _resolve_netease_url_uncached
from proxy import quality_policy as qp
import httpx


def test_musicbox_app_whitelist_contains_sky():
    """验证 musicbox-service 的 QUALITY_WHITELIST 包含 sky 档位。"""
    assert "sky" in mb_app.QUALITY_WHITELIST


def test_musicbox_app_allows_sky_query(monkeypatch):
    """验证 GET /api/v1/song/{id}/url 允许 quality=sky 查询，不抛出 400 校验错误。"""
    async def mock_get_url(sid, quality, fresh=False):
        return {"code": 200, "url": "http://cdn/sky.flac", "level": quality}

    monkeypatch.setattr(mb_app, "get_song_url_async", mock_get_url)
    with TestClient(mb_app.app) as client:
        resp = client.get("/api/v1/song/123/url", params={"quality": "sky"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["data"]["level"] == "sky"


@pytest.mark.anyio
async def test_playback_sky_prevents_silent_downgrade_by_nembox(monkeypatch):
    """当 NEMbox 的 music_quality_to_level 对 sky 未知并静默返回 standard 时，系统必须强制纠正为 sky。"""
    # 模拟一个未更新的 NEMbox helper，它把未知档位 sky 静默改低成 standard
    fake_api = types.ModuleType("NEMbox.api")
    fake_api.BASE_URL = "http://base.test"
    fake_api.EAPI_BASE_URL = "http://eapi.test"
    fake_api.eapi_encrypt = lambda path, payload: b"encrypted"
    fake_api.eapi_response_decrypt = lambda raw: {}
    fake_api.encrypted_request = lambda data: b"enc"
    # 旧 helper 把 sky 静默改成了 standard
    fake_api.music_quality_to_level = lambda q: "standard" if q == "sky" else q
    fake_api.level_to_encode_type = lambda lvl: None if lvl == "sky" else "mp3"
    monkeypatch.setitem(sys.modules, "NEMbox.api", fake_api)

    captured_payload = {}

    def mock_snapshot(gen):
        return ({}, {}, [])

    monkeypatch.setattr(mb_playback, "_snapshot", mock_snapshot)

    async def mock_post(url, data=None, headers=None):
        return None

    # 调用 safe 映射逻辑检验
    level, encode_type = mb_playback._safe_level_and_encode_type("sky")
    assert level == "sky", "网易 sky 不能被 NEMbox 未知值静默改低"
    assert encode_type == "flac", "网易 sky 的 encodeType 应正确映射为 flac"


@pytest.mark.anyio
async def test_playback_sky_does_not_forge_high_tier_via_weapi_fallback(monkeypatch):
    """当 sky 档位在 eapi 查无音源（如无权限或无全景声）时，绝不走 weapi 999k 降级伪造 sky。"""
    fake_api = types.ModuleType("NEMbox.api")
    fake_api.BASE_URL = "http://base.test"
    fake_api.EAPI_BASE_URL = "http://eapi.test"
    fake_api.eapi_encrypt = lambda path, payload: b"encrypted"
    fake_api.eapi_response_decrypt = lambda raw: {"code": 200, "data": []}
    fake_api.encrypted_request = lambda data: b"enc"
    fake_api.music_quality_to_level = lambda q: q
    fake_api.level_to_encode_type = lambda lvl: "flac"
    monkeypatch.setitem(sys.modules, "NEMbox.api", fake_api)

    monkeypatch.setattr(mb_playback, "_snapshot", lambda gen: ({}, {}, []))

    class MockClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        @property
        def cookies(self):
            return httpx.Cookies()
        async def post(self, url, data=None, headers=None):
            class MockResponse:
                status_code = 200
                cookies = httpx.Cookies()
                content = b""
                def raise_for_status(self):
                    pass
                def json(self):
                    return {"code": 200, "data": []}
            return MockResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: MockClient())

    # 请求 sky 档位：eapi 无数据时应直接返回 404/url=None，而不是向 weapi 请求 999k 伪造
    result = await mb_playback._request(12345, "sky", mb_playback._generation)
    assert result.get("url") is None
    assert result.get("code") == 404


def test_seven_quality_levels_defined_with_sky():
    """验证统一共享模块质量策略中包含完整的七档（含 sky）。"""
    assert "sky" in qp.QUALITY_LEVELS
    assert qp.QUALITY_LEVELS == ("standard", "higher", "exhigh", "lossless", "hires", "jymaster", "sky")


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
