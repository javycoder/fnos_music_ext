"""Search pagination contracts for the 1.1.19 client."""
import httpx
import pytest
from fastapi.testclient import TestClient

import proxy.app as p


@pytest.mark.parametrize("local_count", [0, 49, 50, 99, 100, 120])
def test_unpaged_default_window(monkeypatch, local_count):
    p._SEARCH_CACHE.clear()
    monkeypatch.setattr(p.music_compat, "supports_search_default_100", lambda: True)
    monkeypatch.setitem(p.CONF, "musicdl_enabled", True)
    monkeypatch.setitem(p.CONF, "netease_enabled", False)
    monkeypatch.setitem(p.CONF, "lx_enabled", False)
    monkeypatch.setitem(p.CONF, "search_debounce_s", 0.0)
    monkeypatch.setitem(p.CONF, "search_deep_page", False)
    local = [{"guid": f"local-{i}", "title": f"local-{i}", "artist": "local"} for i in range(local_count)]
    online = [{"id": f"kuwo:{i}", "source": "kuwo", "title": f"online-{i}", "artist": "artist"} for i in range(80)]
    p.app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"code": 0, "data": {"list": local, "total": len(local)}})), base_url="http://unix")
    p.app.state.musicdl_client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"ok": True, "items": online})), base_url="http://musicdl")
    p.app.state.musicbox_client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(404)), base_url="http://musicbox")
    with TestClient(p.app) as client:
        data = client.get("/music/api/v1/search/track?q=test").json()["data"]
        explicit = client.get("/music/api/v1/search/track?q=test&page=1&size=20").json()["data"]
    assert len(data["list"]) == min(100, local_count + 80)
    assert [x["guid"] for x in data["list"][:min(local_count, 100)]] == [f"local-{i}" for i in range(min(local_count, 100))]
    assert data["total"] == local_count + 80
    assert len(explicit["list"]) == min(20, local_count + 80)
