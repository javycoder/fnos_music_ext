"""客户端歌单搜索路由：官方结果优先，在线结果可打开。"""
import httpx
import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from proxy import playlistsearch
from proxy import publicplaylists as pubpl
from proxy.app import CONF, app, resolve_real_guid


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    playlistsearch.reset_for_test()
    pubpl.reset_for_test()
    monkeypatch.setenv("FNMUSIC_PLAYLIST_SEARCH_DIR", str(tmp_path / "grants"))
    monkeypatch.setenv("FNMUSIC_PUBLIC_PLAYLISTS_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    for key, value in {
        "netease_enabled": False,
        "netease_playlist_resident": True,
        "lx_enabled": True,
        "lx_sources": ["kw"],
        "public_playlists_enabled": False,
        "recommend_daily": False,
        "recommend_hot": False,
        "netease_my_playlists": False,
    }.items():
        monkeypatch.setitem(CONF, key, value)
    yield
    playlistsearch.reset_for_test()
    pubpl.reset_for_test()
    app.state.upstream_client = None
    app.state.musicbox_client = None
    app.state.netease_client = None
    app.state.lx_client = None


def _wire(user="user-a", official_total=1):
    calls = {"netease": [], "lx": []}

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": user}}, headers={"x-upstream-test": "yes"})
        if request.url.path.endswith("/search/playlist"):
            return httpx.Response(200, json={
                "code": 0, "msg": "ok",
                "data": {"list": [{"guid": "official-1", "name": "官方歌单", "trackCount": 1}], "total": official_total},
            }, headers={"x-upstream-test": "yes"})
        return httpx.Response(200, json={"code": 0, "data": None}, headers={"x-upstream-test": "yes"})

    def netease(request: httpx.Request) -> httpx.Response:
        calls["netease"].append((request.url.path, dict(request.url.params)))
        return httpx.Response(200, json={
            "ok": True,
            "items": [{
                "id": "900", "name": "网易公开", "source": "netease", "kind": "playlist",
                "cover_url": "https://img.example/cover.jpg", "track_count": 2,
            }],
            "raw_count": 1, "total": 1, "has_more": False,
        })

    def lx(request: httpx.Request) -> httpx.Response:
        calls["lx"].append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, json={"ok": True, "sources": [
                {"id": "kw", "playback_available": True},
                {"id": "kg", "playback_available": False},
            ]})
        if request.url.path.endswith("/kw1/tracks"):
            return httpx.Response(200, json={"ok": True, "data": [], "total": 0, "truncated": False})
        if request.url.path.endswith("/kw1"):
            return httpx.Response(200, json={"ok": True, "data": {
                "id": "kw1", "name": "洛雪公开", "source": "kw", "kind": "playlist",
                "cover_url": "", "track_count": 4,
            }})
        return httpx.Response(200, json={
            "ok": True,
            "items": [{
                "id": "kw1", "name": "洛雪公开", "source": "kw", "kind": "playlist",
                "cover_url": "", "track_count": 4,
            }],
            "raw_count": 1, "total": 1, "has_more": False,
        })

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url="http://unix")
    app.state.netease_client = httpx.AsyncClient(transport=httpx.MockTransport(netease), base_url="http://netease")
    app.state.musicbox_client = app.state.netease_client
    app.state.lx_client = httpx.AsyncClient(transport=httpx.MockTransport(lx), base_url="http://lx")
    return calls


def test_search_merges_enabled_sources_and_keeps_official_header():
    calls = _wire()
    with TestClient(app) as client:
        response = client.get("/music/api/v1/search/playlist", params={"q": "公开", "page": 1, "size": 24})
    assert response.status_code == 200
    assert response.headers.get("x-upstream-test") == "yes"
    body = response.json()
    assert body["code"] == 0
    names = [row["name"] for row in body["data"]["list"]]
    assert names == ["官方歌单", "[网易] 网易公开", "[酷我] 洛雪公开"]
    assert body["data"]["total"] == 3
    netease_search = [params for path, params in calls["netease"] if params.get("channel") == "search"]
    lx_search = [params for path, params in calls["lx"] if params.get("channel") == "search"]
    assert netease_search and netease_search[0]["timeout_ms"]
    assert any(path.endswith("/capabilities") for path, _params in calls["lx"])
    assert lx_search and lx_search[0]["source"] == "kw" and "timeout_s" in lx_search[0]
    assert all(row["guid"] != "online" and not str(row["guid"]).startswith("online:") for row in body["data"]["list"][1:])


def test_discovered_playlist_opens_for_same_user_only(monkeypatch):
    calls = _wire()

    async def cover(*_args, **_kwargs):
        return Response(status_code=204)

    monkeypatch.setattr("proxy.app._playlist_cover_response", cover)
    with TestClient(app) as client:
        found = client.get("/music/api/v1/search/playlist", params={"keyword": "公开", "size": 24}).json()
        online = found["data"]["list"][2]
        guid = online["guid"]
        assert resolve_real_guid(guid).startswith("online:playlist:public:lx:kw:playlist:")
        detail = client.get("/music/api/v1/playlist/detail", params={"guid": guid}).json()
        assert detail["code"] == 0
        assert detail["data"]["guid"] == guid
        batch = client.get("/music/api/v1/playlist/batch-detail", params={"guids": guid}).json()
        assert len(batch["data"]["list"]) == 1
        assert batch["data"]["list"][0]["guid"] == guid
        assert client.get("/music/api/v1/track/playlist-detail/list", params={"playlistGUID": guid}).json()["code"] == 0
        assert client.get("/music/api/v1/static/cover", params={"id": guid}).status_code == 204

        _wire(user="user-b")
        assert client.get("/music/api/v1/playlist/detail", params={"guid": guid}).json()["code"] != 0
        assert client.get("/music/api/v1/static/cover", params={"id": guid}).status_code == 404
    assert any(path.endswith("/capabilities") for path, _params in calls["lx"])


def test_official_failure_is_not_replaced():
    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 99999, "msg": "login"})

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url="http://unix")
    with TestClient(app) as client:
        response = client.get("/music/api/v1/search/playlist", params={"q": "公开"})
    assert response.json()["code"] == 99999
