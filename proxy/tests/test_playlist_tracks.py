"""Tests for online tracks inside official playlists (add/remove-track + list merge)."""
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

import proxy.app as appmod
from proxy import recommend as dailyrec
from proxy.app import CONF, _SEARCH_CACHE, app, fake_official_guid, resolve_real_guid, save_online_favorites

PL1 = "pl-official-1"
FAKE_KUWO1 = "online:kuwo:228908"
FAKE_KUWO2 = "online:kuwo:228909"
FAKE_MIGU = "online:migu:600908000006810533"


@pytest.fixture(autouse=True)
def setup_plt_env(tmp_path, monkeypatch):
    _SEARCH_CACHE.clear()
    plt_dir = str(tmp_path / "playlist_tracks")
    fav_dir = str(tmp_path / "online_favorites")
    os.makedirs(plt_dir, exist_ok=True)
    os.makedirs(fav_dir, exist_ok=True)
    monkeypatch.setitem(CONF, "plt_dir", plt_dir)
    monkeypatch.setitem(CONF, "fav_dir", fav_dir)
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "lyric_field", "data.lyric")
    # 隔离伪装注册表的全局状态（同进程内各测试文件共享 app）
    monkeypatch.setattr(appmod, "_REGISTRY_WARMED", False)
    appmod._FAKE_GUID_REVERSE.clear()
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8770",
    )
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8772",
    )


def _upstream_authed(handler=None):
    """已登录上游：user/me 返回 user-a，其余路径走自定义 handler（默认 500 兜底）。"""
    def wrapped(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"guid": "user-a"}})
        if handler is not None:
            return handler(request)
        return httpx.Response(500, text="unexpected upstream call")
    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped), base_url="http://unix")


def _musicdl_info():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json={
                "ok": True, "id": "kuwo:228908", "source": "kuwo",
                "title": "晴天", "artist": "周杰伦", "album": "叶惠美",
                "duration_s": 269, "ext": "mp3",
            })
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8768")


def _read_plt(user="user-a") -> dict:
    path = os.path.join(CONF["plt_dir"], f"{user}.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f).get("items") or {}


def _add_online_track(client, guid=FAKE_KUWO1, playlist=PL1):
    fake = fake_official_guid(guid)
    return client.post("/music/api/v1/playlist/add-track", json={"guid": playlist, "trackGUIDs": [fake]})


def test_add_track_online_authorized():
    """全在线批次：code:0 落盘按歌单分桶，快照带 musicdl 元数据。"""
    app.state.upstream_client = _upstream_authed()
    app.state.musicdl_client = _musicdl_info()
    with TestClient(app) as client:
        resp = _add_online_track(client)
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "", "data": None}
    items = _read_plt()
    bucket = items[PL1]
    assert len(bucket) == 1
    assert bucket[0]["guid"] == FAKE_KUWO1
    assert bucket[0]["track"]["title"] == "晴天"
    assert bucket[0]["track"]["artists"][0]["name"] == "周杰伦"


def test_add_track_online_unauthorized():
    """未登录：上游 INVALID TOKEN 原样透传，本地不落盘。"""
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(
            200, json={"code": 99999, "msg": "INVALID TOKEN", "data": None})),
        base_url="http://unix",
    )
    with TestClient(app) as client:
        resp = _add_online_track(client)
        assert resp.json() == {"code": 99999, "msg": "INVALID TOKEN", "data": None}
    assert _read_plt() == {}


def test_add_track_all_official_passthrough():
    """全官方批次：原样透传（上游收到完整 trackGUIDs），不写本地。"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlist/add-track"):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"code": 0, "msg": "", "data": None})
        return httpx.Response(500)

    app.state.upstream_client = _upstream_authed(handler)
    with TestClient(app) as client:
        resp = client.post("/music/api/v1/playlist/add-track",
                           json={"guid": PL1, "trackGUIDs": ["official-1", "official-2"]})
        assert resp.json()["code"] == 0
    assert seen == [{"guid": PL1, "trackGUIDs": ["official-1", "official-2"]}]
    assert _read_plt() == {}


def test_add_track_mixed_batch_split():
    """混合批次：官方部分转发官方（body 只含官方 guid），在线部分写本地。"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlist/add-track"):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"code": 0, "msg": "", "data": None})
        return httpx.Response(500)

    app.state.upstream_client = _upstream_authed(handler)
    app.state.musicdl_client = _musicdl_info()
    with TestClient(app) as client:
        fake = fake_official_guid(FAKE_KUWO1)
        resp = client.post("/music/api/v1/playlist/add-track",
                           json={"guid": PL1, "trackGUIDs": [fake, "official-1"]})
        assert resp.json()["code"] == 0
    assert seen == [{"guid": PL1, "trackGUIDs": ["official-1"]}]
    assert [it["guid"] for it in _read_plt()[PL1]] == [FAKE_KUWO1]


def test_add_track_mixed_official_failure_blocks_local():
    """混合批次官方部分被拒：整体返回官方错误，本地不写（与官方失败语义一致）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlist/add-track"):
            return httpx.Response(200, json={"code": 100002, "msg": "invalid arguments", "data": None})
        return httpx.Response(500)

    app.state.upstream_client = _upstream_authed(handler)
    with TestClient(app) as client:
        fake = fake_official_guid(FAKE_KUWO1)
        resp = client.post("/music/api/v1/playlist/add-track",
                           json={"guid": PL1, "trackGUIDs": [fake, "official-1"]})
        assert resp.json()["code"] == 100002
    assert _read_plt() == {}


def test_add_track_idempotent():
    """重复加同一首：条目不重复，addedAt 刷新（对齐官方 UNIQUE+added_at 语义）。"""
    app.state.upstream_client = _upstream_authed()
    app.state.musicdl_client = _musicdl_info()
    with TestClient(app) as client:
        assert _add_online_track(client).json()["code"] == 0
        first = _read_plt()[PL1][0]["addedAt"]
        assert _add_online_track(client).json()["code"] == 0
    bucket = _read_plt()[PL1]
    assert len(bucket) == 1
    assert bucket[0]["addedAt"] >= first


def test_add_track_recommend_playlist_noop():
    """推荐歌单是虚拟只读歌单：吸收请求 code:0，不透传官方、不写本地。"""
    app.state.upstream_client = _upstream_authed()  # 任何透传都会打 500 兜底
    with TestClient(app) as client:
        rec_guid = dailyrec.daily_playlist_guid("20260923", "user-a")
        resp = client.post("/music/api/v1/playlist/add-track",
                           json={"guid": rec_guid, "trackGUIDs": [fake_official_guid(FAKE_KUWO1)]})
        assert resp.json() == {"code": 0, "msg": "", "data": None}
    assert _read_plt() == {}


def test_remove_track_online_idempotent():
    """删在线条目：本地清桶；再删（桶已空）仍 code:0。"""
    app.state.upstream_client = _upstream_authed()
    app.state.musicdl_client = _musicdl_info()
    with TestClient(app) as client:
        _add_online_track(client)
        fake = fake_official_guid(FAKE_KUWO1)
        resp = client.post("/music/api/v1/playlist/remove-track", json={"guid": PL1, "trackGUIDs": [fake]})
        assert resp.json()["code"] == 0
        assert _read_plt() == {}
        resp2 = client.post("/music/api/v1/playlist/remove-track", json={"guid": PL1, "trackGUIDs": [fake]})
        assert resp2.json()["code"] == 0


def test_remove_track_mixed_split():
    """混合删除：官方部分转发官方，在线部分删本地。"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlist/remove-track"):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"code": 0, "msg": "", "data": None})
        if request.url.path.endswith("/playlist/add-track"):
            return httpx.Response(200, json={"code": 0, "msg": "", "data": None})
        return httpx.Response(500)

    app.state.upstream_client = _upstream_authed(handler)
    app.state.musicdl_client = _musicdl_info()
    with TestClient(app) as client:
        fake1 = fake_official_guid(FAKE_KUWO1)
        fake2 = fake_official_guid(FAKE_KUWO2)
        client.post("/music/api/v1/playlist/add-track", json={"guid": PL1, "trackGUIDs": [fake1, fake2]})
        resp = client.post("/music/api/v1/playlist/remove-track",
                           json={"guid": PL1, "trackGUIDs": [fake1, "official-1"]})
        assert resp.json()["code"] == 0
    assert seen == [{"guid": PL1, "trackGUIDs": ["official-1"]}]
    assert [it["guid"] for it in _read_plt()[PL1]] == [FAKE_KUWO2]


def _official_track_list_handler(official_tracks, total=None):
    """官方 track/playlist-detail/list mock：按请求 page/size 分页返回官方条目。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/track/playlist-detail/list"):
            try:
                page = max(int(request.url.params.get("page") or 1), 1)
            except ValueError:
                page = 1
            try:
                size = int(request.url.params.get("size") or 50)
            except ValueError:
                size = 50
            if size == -1:
                window = official_tracks
            else:
                if size < 1:
                    size = 50
                start = (page - 1) * size
                window = official_tracks[start:start + size]
            return httpx.Response(200, json={
                "code": 0, "msg": "ok",
                "data": {"list": window, "total": total if total is not None else len(official_tracks),
                         "sort": "trackAddedAt,desc"},
            })
        return httpx.Response(500)
    return handler


_OFFICIAL_TRACKS_2 = [
    {"guid": "official-1", "title": "本地歌一", "coverId": "track_aaaa", "duration": 200000},
    {"guid": "official-2", "title": "本地歌二", "coverId": "track_bbbb", "duration": 180000},
]


def test_playlist_track_list_merge_shape():
    """合并下发：官方 2 + 本地 2 在线 → total=4；在线条目伪装 id 可反解、形状对齐官方。"""
    app.state.upstream_client = _upstream_authed(_official_track_list_handler(_OFFICIAL_TRACKS_2))
    app.state.musicdl_client = _musicdl_info()
    with TestClient(app) as client:
        _add_online_track(client, FAKE_KUWO1)
        _add_online_track(client, FAKE_MIGU)
        resp = client.get("/music/api/v1/track/playlist-detail/list",
                          params={"playlistGUID": PL1, "page": 1, "size": 50})
        body = resp.json()
        assert body["code"] == 0
        assert body["data"]["total"] == 4
        guids = [it["guid"] for it in body["data"]["list"]]
        assert guids[:2] == ["official-1", "official-2"]
        assert resolve_real_guid(guids[2]) == FAKE_KUWO1
        assert resolve_real_guid(guids[3]) == FAKE_MIGU
        online = body["data"]["list"][2]
        assert online["title"] == "晴天"
        assert online["artists"][0]["name"] == "周杰伦"
        assert online["album"]["name"] == "叶惠美"
        assert online["audioSpec"]["format"] == "mp3"
        assert "online:" not in resp.text


def test_playlist_track_list_pagination_window():
    """分页区间拼接：官方 total=2、在线 3 条、size=2 → 三页不重不漏。"""
    app.state.upstream_client = _upstream_authed(_official_track_list_handler(_OFFICIAL_TRACKS_2))
    app.state.musicdl_client = _musicdl_info()
    with TestClient(app) as client:
        for g in (FAKE_KUWO1, FAKE_KUWO2, FAKE_MIGU):
            _add_online_track(client, g)
        base = "/music/api/v1/track/playlist-detail/list"
        p1 = client.get(base, params={"playlistGUID": PL1, "page": 1, "size": 2}).json()["data"]
        p2 = client.get(base, params={"playlistGUID": PL1, "page": 2, "size": 2}).json()["data"]
        p3 = client.get(base, params={"playlistGUID": PL1, "page": 3, "size": 2}).json()["data"]
        assert [it["guid"] for it in p1["list"]] == ["official-1", "official-2"]
        assert len(p2["list"]) == 2 and all(resolve_real_guid(it["guid"]).startswith("online:") for it in p2["list"])
        assert len(p3["list"]) == 1 and resolve_real_guid(p3["list"][0]["guid"]).startswith("online:")
        for page_data in (p1, p2, p3):
            assert page_data["total"] == 5


def test_playlist_track_list_is_favorite_reflects_favorites():
    """在线条目 isFavorite 按本地在线收藏动态置位，不是硬编码 True。"""
    app.state.upstream_client = _upstream_authed(_official_track_list_handler(_OFFICIAL_TRACKS_2))
    app.state.musicdl_client = _musicdl_info()
    save_online_favorites("user-a", [{"guid": FAKE_KUWO1, "createdAt": 1, "track": {}}])
    with TestClient(app) as client:
        _add_online_track(client, FAKE_KUWO1)
        _add_online_track(client, FAKE_MIGU)
        data = client.get("/music/api/v1/track/playlist-detail/list",
                          params={"playlistGUID": PL1, "page": 1, "size": 50}).json()["data"]
    by_fake = {it["guid"]: it for it in data["list"]}
    assert by_fake[fake_official_guid(FAKE_KUWO1)]["isFavorite"] is True
    assert by_fake[fake_official_guid(FAKE_MIGU)]["isFavorite"] is False


def test_playlist_detail_track_count():
    """detail 的 trackCount = 官方数 + 本地附加数。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlist/detail"):
            return httpx.Response(200, json={
                "code": 0, "msg": "ok",
                "data": {"guid": PL1, "name": "牛一", "coverId": "c1", "createdAt": 1, "updatedAt": 1, "trackCount": 3},
            })
        return httpx.Response(500)

    app.state.upstream_client = _upstream_authed(handler)
    app.state.musicdl_client = _musicdl_info()
    with TestClient(app) as client:
        _add_online_track(client, FAKE_KUWO1)
        _add_online_track(client, FAKE_MIGU)
        data = client.get("/music/api/v1/playlist/detail", params={"guid": PL1}).json()["data"]
    assert data["trackCount"] == 5


def test_playlist_batch_detail_track_count():
    """batch-detail 官方歌单 trackCount 同样修正；无本地数据的歌单保持原值。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlist/batch-detail"):
            return httpx.Response(200, json={
                "code": 0, "msg": "ok",
                "data": {"list": [
                    {"guid": PL1, "name": "牛一", "trackCount": 3},
                    {"guid": "pl-other", "name": "其它", "trackCount": 7},
                ]},
            })
        return httpx.Response(500)

    app.state.upstream_client = _upstream_authed(handler)
    app.state.musicdl_client = _musicdl_info()
    with TestClient(app) as client:
        _add_online_track(client, FAKE_KUWO1)
        resp = client.get("/music/api/v1/playlist/batch-detail", params={"guids": f"{PL1},pl-other"})
        plist = {it["guid"]: it for it in resp.json()["data"]["list"]}
    assert plist[PL1]["trackCount"] == 4
    assert plist["pl-other"]["trackCount"] == 7


def test_registry_warm_rebuilds_from_playlist_tracks():
    """重启后注册表清空：warm 能从 playlist_tracks 文件重建伪装映射。"""
    app.state.upstream_client = _upstream_authed()
    app.state.musicdl_client = _musicdl_info()
    with TestClient(app) as client:
        _add_online_track(client)
    fake = fake_official_guid(FAKE_KUWO1)
    appmod._FAKE_GUID_REVERSE.clear()
    appmod._REGISTRY_WARMED = False
    assert resolve_real_guid(fake) == FAKE_KUWO1


def test_add_track_storage_write_failure_no_500(tmp_path, monkeypatch):
    """plt_dir 不可写：落盘失败仅告警，仍返回 code:0，绝不 500。"""
    app.state.upstream_client = _upstream_authed()
    app.state.musicdl_client = _musicdl_info()
    plt_dir = CONF["plt_dir"]
    blocker = os.path.join(str(tmp_path), "plt-blocker")
    with open(blocker, "w", encoding="utf-8") as f:
        f.write("not a dir")
    monkeypatch.setitem(CONF, "plt_dir", blocker)
    with TestClient(app) as client:
        resp = _add_online_track(client)
        assert resp.status_code == 200
        assert resp.json()["code"] == 0
    assert not os.path.exists(os.path.join(plt_dir, "user-a.json"))


def test_playlist_delete_purges_local_bucket():
    """官方删除成功后清掉本地该歌单的附加条目（防孤儿）；官方错误时保留。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlist/delete"):
            return httpx.Response(200, json={"code": 0, "msg": "", "data": None})
        return httpx.Response(500)

    app.state.upstream_client = _upstream_authed(handler)
    app.state.musicdl_client = _musicdl_info()
    with TestClient(app) as client:
        _add_online_track(client, FAKE_KUWO1, PL1)
        _add_online_track(client, FAKE_MIGU, "pl-official-2")
        resp = client.post("/music/api/v1/playlist/delete", json={"guid": PL1})
        assert resp.json()["code"] == 0
        items = _read_plt()
        assert PL1 not in items
        assert [it["guid"] for it in items["pl-official-2"]] == [FAKE_MIGU]
