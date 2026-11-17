"""独立测试：公共歌单名称修复、chart 异常 fallback、空 name 缓存策略与超时。"""
from __future__ import annotations

import importlib.util
import sys
import unittest.mock as mock
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent


def _load_webui():
    cached = sys.modules.get("webui_service_app")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("webui_service_app", HERE / "app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webui_service_app"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_api_public_playlists_detail_timeout_and_empty_name_no_cache():
    """测试后端详情超时为 8.0s（非详情为 65.0s），且 200 ok 空 name 不进缓存。"""
    webui = _load_webui()
    webui._public_discovery_cache.clear()

    observed_timeouts = []

    def mock_transport_handler(request: httpx.Request) -> httpx.Response:
        # 记录超时设置
        observed_timeouts.append(request.extensions.get("timeout", {}))
        path = request.url.path
        if "empty-name" in path:
            return httpx.Response(200, json={"ok": True, "data": {"id": "empty-name", "name": "   "}})
        if "valid-detail" in path:
            return httpx.Response(200, json={"ok": True, "data": {"id": "valid-detail", "name": "网易云ACG动画榜"}})
        if "tracks" in path:
            return httpx.Response(200, json={"ok": True, "data": [], "total": 0})
        return httpx.Response(404)

    client = TestClient(webui.app)
    client.headers.update({"X-Trim-Isadmin": "true"})
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport_handler))

    with mock.patch.object(webui, "get_http", return_value=mock_client), \
         mock.patch.object(webui, "preview_renew"):

        # 1. 请求详情（empty-name）
        resp_empty = client.get("/api/public-playlists/empty-name?provider=musicbox&source=netease&kind=chart")
        assert resp_empty.status_code == 200
        # 验证超时时间配置
        assert len(observed_timeouts) >= 1
        t1 = observed_timeouts[-1]
        assert t1.get("read") == 8.0 or t1.get("timeout") == 8.0

        # 空 name 验证：不应该写入 _public_discovery_cache
        cache_key_empty = ("musicbox", "empty-name", (("kind", "chart"), ("source", "netease")))
        assert cache_key_empty not in webui._public_discovery_cache

        # 2. 请求详情（valid-detail）
        resp_valid = client.get("/api/public-playlists/valid-detail?provider=musicbox&source=netease&kind=chart")
        assert resp_valid.status_code == 200
        t2 = observed_timeouts[-1]
        assert t2.get("read") == 8.0 or t2.get("timeout") == 8.0

        # 有效 name 验证：正常写入缓存
        cache_key_valid = ("musicbox", "valid-detail", (("kind", "chart"), ("source", "netease")))
        assert cache_key_valid in webui._public_discovery_cache

        # 3. 请求曲目 tracks（非 detail）
        resp_tracks = client.get("/api/public-playlists/valid-detail/tracks?provider=musicbox&source=netease&kind=chart")
        assert resp_tracks.status_code == 200
        t3 = observed_timeouts[-1]
        assert t3.get("read") == 65.0 or t3.get("timeout") == 65.0


def test_api_public_playlists_total_deadline_asyncio_timeout():
    """测试 detail 请求受 asyncio.timeout(8.0) 整体 deadline 保护，超时返回 503。"""
    webui = _load_webui()

    client = TestClient(webui.app)
    client.headers.update({"X-Trim-Isadmin": "true"})

    # 模拟 slow upstream，抛出 TimeoutError
    class MockTimeoutContext:
        def __init__(self, delay):
            self.delay = delay
        async def __aenter__(self):
            raise TimeoutError("Total deadline exceeded")
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    with mock.patch("asyncio.timeout", side_effect=lambda delay: MockTimeoutContext(delay)), \
         mock.patch.object(webui, "preview_renew"):
        resp = client.get("/api/public-playlists/slow-chart?provider=musicbox&source=netease&kind=chart")
        assert resp.status_code == 503
        payload = resp.json()
        assert payload.get("ok") is False
        assert "超时" in payload.get("error", "")
