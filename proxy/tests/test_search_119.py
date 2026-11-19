"""Search pagination contracts for the 1.1.19 client."""
import httpx
import pytest
from fastapi.testclient import TestClient

import proxy.app as p


@pytest.mark.parametrize("local_count", [0, 49, 50, 99, 100, 120])
def test_unpaged_default_window(monkeypatch, tmp_path, local_count):
    p._SEARCH_CACHE.clear()
    manifest = tmp_path / "manifest"
    manifest.write_text('appname="trim.music"\nversion="1.1.19"\n', encoding="utf-8")
    monkeypatch.setattr(p.music_compat, "MANIFEST", str(manifest))
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


# ---------------------------------------------------------------- 网页单曲窗口（issue #139）

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_WEB_HEADERS = {"user-agent": _BROWSER_UA, "sec-fetch-dest": "empty", "sec-fetch-mode": "cors"}


def _wire_119(monkeypatch, tmp_path, local_count=0, online_count=80, limit=30,
              upstream_handler=None):
    p._SEARCH_CACHE.clear()
    p._USER_SEARCH_WORD.clear()
    manifest = tmp_path / "manifest"
    manifest.write_text('appname="trim.music"\nversion="1.1.19"\n', encoding="utf-8")
    monkeypatch.setattr(p.music_compat, "MANIFEST", str(manifest))
    monkeypatch.setitem(p.CONF, "musicdl_enabled", True)
    monkeypatch.setitem(p.CONF, "netease_enabled", False)
    monkeypatch.setitem(p.CONF, "lx_enabled", False)
    monkeypatch.setitem(p.CONF, "search_debounce_s", 0.0)
    monkeypatch.setitem(p.CONF, "search_deep_page", False)
    monkeypatch.setitem(p.CONF, "web_search_result_limit", limit)
    local = [{"guid": f"local-{i}", "title": f"local-{i}", "artist": "local"} for i in range(local_count)]
    online = [{"id": f"kuwo:{i}", "source": "kuwo", "title": f"online-{i}", "artist": "artist"}
              for i in range(online_count)]
    handler = upstream_handler or (
        lambda r: httpx.Response(200, json={"code": 0, "data": {"list": local, "total": local_count}}))
    p.app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://unix")
    p.app.state.musicdl_client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"ok": True, "items": online})), base_url="http://musicdl")
    p.app.state.musicbox_client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(404)), base_url="http://musicbox")
    return local, online


def test_web_unpaged_window_uses_configured_limit(monkeypatch, tmp_path):
    """网页无参单曲：窗口=配置上限（本地+在线合计），不是版本默认 100。"""
    _wire_119(monkeypatch, tmp_path, local_count=0, online_count=80, limit=30)
    with TestClient(p.app) as client:
        data = client.get("/music/api/v1/search/track?q=test", headers=_WEB_HEADERS).json()["data"]
    assert len(data["list"]) == 30
    assert data["total"] == 80
    assert data["list"][0]["guid"].startswith("online:") or len(str(data["list"][0]["guid"])) == 32


def test_web_unpaged_window_caps_local_echo(monkeypatch, tmp_path):
    """官方全量返回的本地结果超过上限时截到窗口内（在线段为空）。"""
    _wire_119(monkeypatch, tmp_path, local_count=120, online_count=80, limit=30)
    with TestClient(p.app) as client:
        data = client.get("/music/api/v1/search/track?q=test", headers=_WEB_HEADERS).json()["data"]
    assert len(data["list"]) == 30
    assert [x["guid"] for x in data["list"]] == [f"local-{i}" for i in range(30)]
    assert data["total"] == 120 + 80


@pytest.mark.parametrize("headers,expected", [
    # 无浏览器证据：走版本默认（1.1.19 → 100）
    (None, 80),
    ({"user-agent": _BROWSER_UA}, 80),  # 浏览器 UA 但无 Fetch Metadata/Referer
    # Fetch Metadata 命中 → 网页窗口
    (_WEB_HEADERS, 30),
    # UA + Referer 指向官方 /music 页面 → 网页窗口
    ({"user-agent": _BROWSER_UA, "referer": "http://nas.local/music/search"}, 30),
    # Referer 不在 /music 下 → 不算网页
    ({"user-agent": _BROWSER_UA, "referer": "http://nas.local/other/page"}, 80),
    # 原生 App 标识排除（即使带浏览器式头）
    ({"user-agent": "Dart/3.4 (dart:io)", "sec-fetch-dest": "empty"}, 80),
])
def test_web_detection_requires_browser_evidence(monkeypatch, tmp_path, headers, expected):
    _wire_119(monkeypatch, tmp_path, local_count=0, online_count=80, limit=30)
    with TestClient(p.app) as client:
        data = client.get("/music/api/v1/search/track?q=test", headers=headers).json()["data"]
    assert len(data["list"]) == expected


def test_explicit_page_size_beats_web_limit(monkeypatch, tmp_path):
    """显式 page/size 分页请求不受网页上限影响。"""
    _wire_119(monkeypatch, tmp_path, local_count=0, online_count=80, limit=30)
    with TestClient(p.app) as client:
        data = client.get("/music/api/v1/search/track?q=test&page=1&size=20",
                          headers=_WEB_HEADERS).json()["data"]
    assert len(data["list"]) == 20


def test_superseded_web_echo_capped_to_window(monkeypatch, tmp_path):
    """被新关键词取代时：网页无参请求的官方回声也截到配置窗口内。"""
    local, _online = _wire_119(monkeypatch, tmp_path, local_count=120, online_count=80, limit=30)

    def superseding_handler(request):
        # 模拟上游往返期间另一请求记入了更新的关键词
        for k in list(p._USER_SEARCH_WORD):
            p._USER_SEARCH_WORD[k] = "newer-word"
        return httpx.Response(200, json={"code": 0, "data": {"list": local, "total": len(local)}})

    p.app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(superseding_handler), base_url="http://unix")
    with TestClient(p.app) as client:
        body = client.get("/music/api/v1/search/track?q=test", headers=_WEB_HEADERS).json()
    assert body["code"] == 0
    assert len(body["data"]["list"]) == 30
    assert body["data"]["list"][0]["guid"] == "local-0"


def test_superseded_non_web_echo_not_capped(monkeypatch, tmp_path):
    """同一取代场景下非网页请求保留原有回声行为（不截断）。"""
    local, _online = _wire_119(monkeypatch, tmp_path, local_count=120, online_count=80, limit=30)

    def superseding_handler(request):
        for k in list(p._USER_SEARCH_WORD):
            p._USER_SEARCH_WORD[k] = "newer-word"
        return httpx.Response(200, json={"code": 0, "data": {"list": local, "total": len(local)}})

    p.app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(superseding_handler), base_url="http://unix")
    with TestClient(p.app) as client:
        body = client.get("/music/api/v1/search/track?q=test").json()
    assert body["code"] == 0
    assert len(body["data"]["list"]) == 120
