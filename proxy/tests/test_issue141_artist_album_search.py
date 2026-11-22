"""Issue #141：在线歌手与原生专辑搜索/详情契约回归。"""
import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import app as app_mod
from proxy.app import CONF, app, build_online_track, resolve_fake_artist


@pytest.fixture(autouse=True)
def clean_issue141_state(monkeypatch, tmp_path):
    app_mod._FAKE_ARTIST_REGISTRY.clear()
    app_mod._artist_pool_cache.clear()
    app_mod._ALBUM_PAYLOAD_CACHE.clear()
    app_mod._album_pool_cache.clear()
    monkeypatch.setattr(app_mod, "_ALBUM_REGISTRY_PATH", str(tmp_path / "album_registry.json"))
    monkeypatch.setattr(app_mod, "_ALBUM_PERSIST_LAST", 0.0)
    monkeypatch.setattr(app_mod, "_ARTIST_SEARCH_REGISTRY_PATH", tmp_path / "artist_search_registry.json")
    monkeypatch.setattr(app_mod, "_ARTIST_SEARCH_REGISTRY", None)
    yield
    app_mod._FAKE_ARTIST_REGISTRY.clear()
    app_mod._artist_pool_cache.clear()
    app_mod._ALBUM_PAYLOAD_CACHE.clear()
    app_mod._album_pool_cache.clear()


def _official_artist_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/user/me"):
        return httpx.Response(200, json={"code": 0, "data": {"guid": "user-141"}})
    if request.url.path.endswith("/search/artist"):
        page = int(request.url.params.get("page") or 1)
        count = 24 if page == 1 else 6
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {
            "list": [{"guid": f"local-{page}-{index}", "name": f"本地歌手{index}"}
                     for index in range(count)],
            "total": 30,
        }})
    return httpx.Response(200, json={"code": 0, "data": None})


def _netease_artist_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/v1/search" and request.url.params.get("type") == "artist":
        return httpx.Response(200, json={"ok": True, "data": [
            {"id": 6000 + index, "name": f"网易歌手{index}", "picUrl": "http://pic.test/a.jpg",
             "albumSize": index, "musicSize": index + 1}
            for index in range(10)
        ]})
    if path == "/api/v1/search" and request.url.params.get("type") == "album":
        return httpx.Response(200, json={"ok": True, "data": [{
            "album_id": 900, "album_name": "网易专辑", "artist": "网易歌手0",
            "artists": [{"id": 6000, "name": "网易歌手0"}, {"id": 6001, "name": "网易歌手1"}],
            "pic_url": "http://pic.test/a.jpg",
        }]})
    if path == "/api/v1/artist/6000":
        return httpx.Response(200, json={"ok": True, "artist": {
            "id": 6000, "name": "网易歌手0", "picUrl": "http://pic.test/a.jpg",
            "albumSize": 3, "musicSize": 4,
        }})
    if path == "/api/v1/artist/6000/tracks":
        return httpx.Response(200, json={"ok": True, "total": 1, "data": [{
            "song_id": 101, "name": "晴天", "artist": "网易歌手0",
            "artists": [{"id": 6000, "name": "网易歌手0"}],
            "artist_id": 6000, "album_name": "网易专辑", "album_id": 900,
            "album_pic_url": "http://pic.test/a.jpg", "duration_ms": 240000, "sq": {},
        }]})
    if path == "/api/v1/artist/6000/albums":
        return httpx.Response(200, json={"ok": True, "total": 1, "data": [{
            "id": 900, "name": "网易专辑", "picUrl": "http://pic.test/a.jpg",
            "artistName": "网易歌手0 / 网易歌手1", "size": 10, "publishTime": 1000,
            "artists": [{"id": 6000, "name": "网易歌手0"}, {"id": 6001, "name": "网易歌手1"}],
        }]})
    return httpx.Response(404, json={"ok": False})


def test_issue141_search_and_artist_detail_pages(monkeypatch):
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "lx_enabled", False)
    monkeypatch.setitem(CONF, "musicdl_enabled", False)
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_official_artist_handler), base_url="http://official")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_netease_artist_handler), base_url="http://netease")
    app.state.netease_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404)), base_url="http://netease")
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404)), base_url="http://lx")

    with TestClient(app) as client:
        page1 = client.get("/music/api/v1/search/artist", params={
            "q": "歌手", "page": 1, "size": 24}).json()
        page2 = client.get("/music/api/v1/search/artist", params={
            "q": "歌手", "page": 2, "size": 24}).json()
        assert page1["data"]["total"] == 40
        assert page2["data"]["total"] == 40
        assert [row["name"] for row in page2["data"]["list"][6:]] == [
            f"网易歌手{index}" for index in range(10)]

        artist_guid = page2["data"]["list"][6]["guid"]
        detail = client.get("/music/api/v1/artist/detail", params={"guid": artist_guid}).json()
        assert detail["code"] == 0
        assert detail["data"]["name"] == "网易歌手0"
        assert detail["data"]["albumCount"] == 3
        tracks = client.get("/music/api/v1/track/artist-detail/list", params={
            "artistGUID": artist_guid, "page": 1, "size": 20}).json()
        assert tracks["data"]["total"] == 1
        assert tracks["data"]["list"][0]["title"] == "晴天"
        assert tracks["data"]["list"][0]["artists"][0]["guid"] == artist_guid
        albums = client.get("/music/api/v1/album/artist-detail/list", params={
            "artistGUID": artist_guid, "page": 1, "size": 20}).json()
        assert albums["data"]["total"] == 1
        assert albums["data"]["list"][0]["name"] == "网易专辑"
        assert [resolve_fake_artist(row["guid"])["artist_id"]
                for row in albums["data"]["list"][0]["artists"]] == ["6000", "6001"]


def test_artist_search_link_survives_proxy_restart(monkeypatch):
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "lx_enabled", False)
    monkeypatch.setitem(CONF, "musicdl_enabled", False)
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_official_artist_handler), base_url="http://official")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_netease_artist_handler), base_url="http://netease")

    with TestClient(app) as client:
        search = client.get("/music/api/v1/search/artist", params={
            "q": "歌手", "page": 2, "size": 24,
        }).json()["data"]["list"]
        artist_guid = search[6]["guid"]
        monkeypatch.setattr(app_mod, "_FAKE_ARTIST_REGISTRY", {})
        monkeypatch.setattr(app_mod, "_ARTIST_SEARCH_REGISTRY", None)

        detail = client.get("/music/api/v1/artist/detail", params={"guid": artist_guid}).json()
        assert detail["data"]["guid"] == artist_guid
        assert detail["data"]["name"] == "网易歌手0"
        for kind in ("track", "album"):
            body = client.get(f"/music/api/v1/{kind}/artist-detail/list", params={
                "artistGUID": artist_guid, "page": 1, "size": 20,
            }).json()
            assert body["data"]["total"] == 1


def test_artist_search_registry_ignores_expired_and_invalid_entries():
    path = app_mod._ARTIST_SEARCH_REGISTRY_PATH
    expired = app_mod.fake_official_guid("online:netease:artist:6000")
    other = app_mod.fake_official_guid("online:netease:artist:6001")
    path.write_text(json.dumps({
        expired: {"source": "netease", "platform": "", "artist_id": "6000", "expires": 1},
        other: {"source": "netease", "platform": "", "artist_id": "6000", "expires": int(time.time()) + 60},
    }), encoding="utf-8")
    assert resolve_fake_artist(expired) is None
    assert resolve_fake_artist(other) is None


@pytest.mark.parametrize("source", ["netease", "lx"])
@pytest.mark.parametrize("kind,field", [("track", "title"), ("album", "name")])
def test_issue141_artist_lists_keep_provider_page(monkeypatch, source, kind, field):
    monkeypatch.setitem(CONF, "netease_enabled", source == "netease")
    monkeypatch.setitem(CONF, "lx_enabled", source == "lx")
    monkeypatch.setitem(CONF, "lx_sources", ["tx"])
    platform = "tx" if source == "lx" else ""
    artist_guid = app_mod.register_fake_artist(
        app_mod.artist_real_guid(source, platform, "6000"),
        source=source, platform=platform, artist_id="6000", name="在线歌手",
    )
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_official_artist_handler), base_url="http://official")

    def provider(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/{kind}s")
        assert request.url.params["page"] == "2"
        assert request.url.params["limit"] == "2"
        if source == "netease":
            assert request.url.params["offset"] == "2"
        row = {"id": "601", "name": "第 2 页", "title": "第 2 页",
               "source": "lx", "lx_source": "tx", "artist": "在线歌手"}
        return httpx.Response(200, json={
            "ok": True, "total": 3, "data" if source == "netease" else "items": [row],
        })

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider), base_url="http://source")
    if source == "netease":
        app.state.musicbox_client = upstream
        app.state.netease_client = upstream
    else:
        app.state.lx_client = upstream

    with TestClient(app) as client:
        data = client.get(f"/music/api/v1/{kind}/artist-detail/list", params={
            "artistGUID": artist_guid, "page": 2, "size": 2, "sort": "createdAt",
        }).json()["data"]

    assert data["total"] == 3
    assert data["sort"] == "createdAt"
    assert [row[field] for row in data["list"]] == ["第 2 页"]
    if kind == "track":
        for key in ("createdAt", "updatedAt"):
            assert type(data["list"][0][key]) is int
            assert data["list"][0][key] == 0


@pytest.mark.parametrize("kind,error_message", [
    ("track", "歌手歌曲获取失败"), ("album", "歌手专辑获取失败"),
])
@pytest.mark.parametrize("failure", ["timeout", "invalid_json", "missing_list"])
def test_issue141_artist_list_provider_failure_returns_business_error(monkeypatch, kind, error_message, failure):
    monkeypatch.setitem(CONF, "lx_enabled", True)
    monkeypatch.setitem(CONF, "lx_sources", ["tx"])
    artist_guid = app_mod.register_fake_artist(
        app_mod.artist_real_guid("lx", "tx", "700"),
        source="lx", platform="tx", artist_id="700", name="洛雪歌手",
    )
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_official_artist_handler), base_url="http://official")

    def provider(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/{kind}s")
        if failure == "timeout":
            raise httpx.ReadTimeout("test timeout", request=request)
        if failure == "invalid_json":
            return httpx.Response(200, content=b"<html>")
        return httpx.Response(200, json={"ok": True, "total": 3})

    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(provider), base_url="http://lx")
    with TestClient(app) as client:
        response = client.get(f"/music/api/v1/{kind}/artist-detail/list",
                              params={"artistGUID": artist_guid})
    assert response.status_code == 200
    assert response.json() == {"code": -1, "msg": error_message, "data": None}


def _lx_catalog_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    params = request.url.params
    if path == "/api/v1/catalog/search" and params.get("kind") == "artist":
        return httpx.Response(200, json={"ok": True, "total": 1, "items": [{
            "source": "lx", "lx_source": "tx", "id": "700", "name": "洛雪歌手",
            "pic_url": "http://pic.test/lx.jpg", "album_count": 2, "track_count": 3,
        }]})
    if path == "/api/v1/catalog/search" and params.get("kind") == "album":
        return httpx.Response(200, json={"ok": True, "total": 1, "items": [{
            "source": "lx", "lx_source": "tx", "id": "800", "name": "洛雪专辑",
            "artist": "洛雪歌手", "artist_id": "700", "pic_url": "http://pic.test/lx.jpg",
            "track_count": 1,
        }]})
    if path == "/api/v1/catalog/artist" and params.get("id") == "700":
        return httpx.Response(200, json={"ok": True, "artist": {
            "source": "lx", "lx_source": "tx", "id": "700", "name": "洛雪歌手",
            "pic_url": "http://pic.test/lx.jpg", "album_count": 2, "track_count": 3,
        }})
    if path == "/api/v1/catalog/artist/tracks":
        return httpx.Response(200, json={"ok": True, "total": 1, "items": [{
            "id": "tx:100", "source": "lx", "lx_source": "tx", "songmid": "100",
            "title": "洛雪歌曲", "artist": "洛雪歌手", "artists": [{"id": "700", "name": "洛雪歌手"}],
            "artist_id": "700", "album": "洛雪专辑", "album_id": "800",
            "duration_s": 200, "ext": "mp3", "cover_url": "http://pic.test/lx.jpg",
        }]})
    if path == "/api/v1/catalog/artist/albums":
        return httpx.Response(200, json={"ok": True, "total": 1, "items": [{
            "source": "lx", "lx_source": "tx", "id": "800", "name": "洛雪专辑",
            "artist": "洛雪歌手", "artist_id": "700", "pic_url": "http://pic.test/lx.jpg",
            "track_count": 1,
        }]})
    if path == "/api/v1/catalog/album" and params.get("id") == "800":
        return httpx.Response(200, json={"ok": True, "total": 1, "album": {
            "id": "800", "name": "洛雪专辑", "artist": "洛雪歌手", "artist_id": "700",
            "cover_url": "http://pic.test/lx.jpg",
        }, "items": [{
            "id": "tx:100", "source": "lx", "lx_source": "tx", "songmid": "100",
            "title": "洛雪歌曲", "artist": "洛雪歌手", "album": "洛雪专辑",
            "album_id": "800", "duration_s": 200, "ext": "mp3",
        }]})
    return httpx.Response(404, json={"ok": False})


def test_issue141_lx_native_artist_and_album(monkeypatch):
    monkeypatch.setitem(CONF, "netease_enabled", False)
    monkeypatch.setitem(CONF, "musicdl_enabled", False)
    monkeypatch.setitem(CONF, "lx_enabled", True)
    monkeypatch.setitem(CONF, "lx_sources", ["tx", "wy"])

    def official(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": "user-141"}})
        if request.url.path.endswith("/search/artist"):
            return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
        if request.url.path.endswith("/search/album"):
            return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
        return httpx.Response(200, json={"code": 0, "data": None})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(official), base_url="http://official")
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_lx_catalog_handler), base_url="http://lx")

    with TestClient(app) as client:
        artists = client.get("/music/api/v1/search/artist", params={
            "q": "洛雪", "page": 1, "size": 24}).json()
        artist_guid = artists["data"]["list"][0]["guid"]
        assert artists["data"]["list"][0]["name"] == "洛雪歌手"
        detail = client.get("/music/api/v1/artist/detail", params={"guid": artist_guid}).json()
        assert detail["data"]["trackCount"] == 3
        tracks = client.get("/music/api/v1/track/artist-detail/list", params={
            "artistGUID": artist_guid}).json()
        assert tracks["data"]["list"][0]["title"] == "洛雪歌曲"
        albums = client.get("/music/api/v1/album/artist-detail/list", params={
            "artistGUID": artist_guid}).json()
        assert albums["data"]["list"][0]["name"] == "洛雪专辑"

        found = client.get("/music/api/v1/search/album", params={
            "q": "洛雪", "page": 1, "size": 24}).json()
        album_guid = found["data"]["list"][0]["guid"]
        album = client.get("/music/api/v1/album/detail", params={"guid": album_guid}).json()
        assert album["data"]["name"] == "洛雪专辑"
        assert album["data"]["tracks"][0]["title"] == "洛雪歌曲"
        album_tracks = client.get("/music/api/v1/track/album-detail/list", params={
            "albumGUID": album_guid}).json()
        assert album_tracks["data"]["total"] == 1


def test_issue141_pending_lx_track_artist_requires_native_verification(monkeypatch):
    monkeypatch.setitem(CONF, "netease_enabled", False)
    monkeypatch.setitem(CONF, "musicdl_enabled", False)
    monkeypatch.setitem(CONF, "lx_enabled", True)
    monkeypatch.setitem(CONF, "lx_sources", ["tx"])
    track = build_online_track({
        "id": "lx:tx:100", "source": "lx", "lx_source": "tx", "songmid": "100",
        "title": "洛雪歌曲", "artist": "洛雪歌手", "album": "洛雪专辑",
        "album_id": "8220", "duration_s": 200, "ext": "mp3",
    })
    pending_guid = track["artists"][0]["guid"]
    assert resolve_fake_artist(pending_guid)["pending"] is True

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_official_artist_handler), base_url="http://official")
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_lx_catalog_handler), base_url="http://lx")
    with TestClient(app) as client:
        detail = client.get("/music/api/v1/artist/detail", params={"guid": pending_guid}).json()
    assert detail["code"] == 0
    assert detail["data"]["guid"] == pending_guid
    entry = resolve_fake_artist(pending_guid)
    assert entry["pending"] is False
    assert entry["artist_id"] == "700"


@pytest.mark.parametrize("artist_available", [True, False])
def test_issue141_pending_artist_checks_track_in_same_name_albums(monkeypatch, artist_available):
    monkeypatch.setitem(CONF, "lx_enabled", True)
    monkeypatch.setitem(CONF, "lx_sources", ["tx"])
    track = build_online_track({
        "id": "lx:tx:100", "source": "lx", "lx_source": "tx", "songmid": "100",
        "title": "洛雪歌曲", "artist": "洛雪歌手", "album": "洛雪专辑",
        "album_id": "wrong", "duration_s": 200, "ext": "mp3",
    })
    pending_guid = track["artists"][0]["guid"]
    album_ids = []

    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/catalog/album":
            album_id = request.url.params["id"]
            album_ids.append(album_id)
            if album_id != "800":
                return httpx.Response(200, json={
                    "album": {"name": "洛雪专辑", "artist": "同名专辑歌手", "artist_id": "123"},
                    "items": [{"songmid": "999", "artist": "同名专辑歌手"}],
                })
        if request.url.path == "/api/v1/catalog/search":
            return httpx.Response(200, json={"items": [
                {"id": "wrong", "name": "洛雪专辑"}, {"id": "800", "name": "洛雪专辑"},
            ]})
        if request.url.path == "/api/v1/catalog/artist" and not artist_available:
            raise httpx.ReadTimeout("test timeout", request=request)
        return _lx_catalog_handler(request)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_official_artist_handler), base_url="http://official")
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(provider), base_url="http://lx")
    with TestClient(app) as client:
        response = client.get("/music/api/v1/artist/detail", params={"guid": pending_guid})

    assert album_ids == ["wrong", "800"]
    assert response.status_code == 200
    assert response.json()["code"] == (0 if artist_available else -1)
    assert resolve_fake_artist(pending_guid)["pending"] is not artist_available


def test_issue141_online_album_dedup_keeps_different_artists(monkeypatch):
    async def netease_rows(_request, _keyword, _limit):
        return [
            {"guid": "n1", "name": "叶惠美", "trackCount": 18,
             "artists": [{"name": "王珏子乔"}]},
            {"guid": "n2", "name": "同名合辑", "trackCount": 2,
             "artists": [{"name": ""}]},
        ]

    async def lx_rows(_keyword, _limit):
        return [
            {"guid": "l1", "name": "叶惠美", "trackCount": 11,
             "artists": [{"name": "周杰伦"}]},
            {"guid": "l2", "name": "同名合辑", "trackCount": 3,
             "artists": [{"name": "周杰伦"}]},
        ]

    async def aggregate_rows(_request, _keyword, _limit):
        return []

    monkeypatch.setattr(app_mod, "_netease_album_search_rows", netease_rows)
    monkeypatch.setattr(app_mod, "_lx_catalog_album_search_rows", lx_rows)
    monkeypatch.setattr(app_mod, "_aggregate_album_search_rows", aggregate_rows)

    rows = asyncio.run(app_mod._online_album_search(None, "叶惠美", 10))
    assert [row["guid"] for row in rows] == ["n1", "l1", "l2"]


def test_issue141_netease_album_card_artist_is_clickable(monkeypatch):
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "lx_enabled", False)
    monkeypatch.setitem(CONF, "musicdl_enabled", False)
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_official_artist_handler), base_url="http://official")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_netease_artist_handler), base_url="http://netease")
    app.state.netease_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404)), base_url="http://netease")
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404)), base_url="http://lx")

    with TestClient(app) as client:
        found = client.get("/music/api/v1/search/album", params={"q": "网易专辑"}).json()
        card = next(row for row in found["data"]["list"] if row["name"] == "网易专辑")
        assert [row["name"] for row in card["artists"]] == ["网易歌手0", "网易歌手1"]
        assert [resolve_fake_artist(row["guid"])["artist_id"]
                for row in card["artists"]] == ["6000", "6001"]
        artist_guid = card["artists"][0]["guid"]
        detail = client.get("/music/api/v1/artist/detail", params={"guid": artist_guid}).json()

    assert detail["code"] == 0
    assert detail["data"]["name"] == "网易歌手0"
