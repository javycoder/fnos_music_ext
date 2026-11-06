"""Tests for favorite / playlist track auto bind to local library (v2.4.0)."""
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import proxy.app as appmod
from proxy import recommend as dailyrec
from proxy.app import (
    CONF,
    _ENV_WATCH_KEYS,
    _FULL_FETCH_COOLDOWN_S,
    _SEARCH_CACHE,
    app,
    apply_env_hot_reload,
    fake_official_guid,
)

FAKE_KUWO1 = "online:kuwo:228908"
FAKE_KUWO2 = "online:kuwo:228909"
PL_CUSTOM = "pl-custom-1"


def test_list_retry_never_downloads_across_batches(monkeypatch):
    from starlette.requests import Request

    monkeypatch.setitem(CONF, "fav_auto_bind", True)
    request = Request({"type": "http", "headers": []})
    pending = [{"guid": f"online:kuwo:{number}", "bind": "pending"}
               for number in range(20)]
    downloads = []
    bindings = []
    monkeypatch.setattr(appmod, "_register_background_fetch", lambda *args: downloads.append(args))
    monkeypatch.setattr(appmod, "_dispatch_official_binding", lambda *args: bindings.append(args))
    monkeypatch.setattr(appmod, "find_cache_file", lambda guid: None)
    import asyncio

    for iteration in range(4):
        appmod._bind_retry_last.clear()
        # issue #120：歌单 pending 意图不可证实为显式保存，不恢复绑定意图
        asyncio.run(appmod._maybe_retry_official_binds(request, "user-a", pending, {PL_CUSTOM: pending}))
        asyncio.run(appmod._maybe_retry_official_binds(request, "user-b", pending))
    assert downloads == []
    assert bindings == []
    # 只有收藏意图照旧恢复/派发（与歌单意图判定互不影响）
    monkeypatch.setattr(appmod, "find_cache_file", lambda guid: "/isolated/existing.mp3")
    appmod._bind_retry_last.clear()
    asyncio.run(appmod._maybe_retry_official_binds(request, "user-a", pending))
    assert len(bindings) == appmod._BIND_RETRY_BATCH
    assert downloads == []


@pytest.fixture(autouse=True)
def setup_fav_autobind_env(tmp_path, monkeypatch):
    _SEARCH_CACHE.clear()
    appmod._full_fetch_tasks.clear()
    appmod._full_fetch_failed.clear()
    appmod._bind_pending.clear()
    appmod._bind_tasks.clear()
    appmod._bind_retry_last.clear()
    appmod._tee_active.clear()
    appmod._tee_handoff_active.clear()
    appmod._bind_registry.clear()
    # 阻止读取真实磁盘上的绑定登记文件
    monkeypatch.setattr(appmod, "_bind_registry_loaded", True)

    plt_dir = str(tmp_path / "playlist_tracks")
    fav_dir = str(tmp_path / "online_favorites")
    cache_dir = str(tmp_path / "cache")
    library_dir = str(tmp_path / "library")
    os.makedirs(plt_dir, exist_ok=True)
    os.makedirs(fav_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(library_dir, exist_ok=True)

    monkeypatch.setitem(CONF, "plt_dir", plt_dir)
    monkeypatch.setitem(CONF, "fav_dir", fav_dir)
    monkeypatch.setitem(CONF, "cache_dir", cache_dir)
    monkeypatch.setitem(CONF, "library_dir", library_dir)
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "lyric_field", "data.lyric")
    monkeypatch.setattr(appmod, "_REGISTRY_WARMED", False)
    appmod._FAKE_GUID_REVERSE.clear()

    # 默认 mock 上游与 musicdl 客户端，避免任何真实联网
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"guid": "user-a"}})
        if request.url.path.endswith("/playlist/add-track"):
            return httpx.Response(200, json={"code": 0, "msg": "", "data": None})
        return httpx.Response(200, json={"code": 0, "msg": "", "data": None})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler),
        base_url="http://unix",
    )

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json={
                "ok": True, "id": "kuwo:228908", "source": "kuwo",
                "title": "晴天", "artist": "周杰伦", "album": "叶惠美",
                "duration_s": 269, "ext": "mp3",
            })
        return httpx.Response(404)

    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler),
        base_url="http://127.0.0.1:8768",
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8770",
    )
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8772",
    )

    # 本文件聚焦下载注册门禁：官方绑定任务以 no-op 隔离（真实闭环见 test_official_bind.py）
    async def _noop_bind_task(guid, user_guid, meta=None):
        return None

    monkeypatch.setattr(appmod, "_bind_official_task", _noop_bind_task)

    yield

    appmod._full_fetch_tasks.clear()
    appmod._full_fetch_failed.clear()
    appmod._bind_pending.clear()
    appmod._bind_tasks.clear()
    appmod._bind_retry_last.clear()
    appmod._tee_active.clear()
    appmod._tee_handoff_active.clear()


def test_scenario1_fav_autobind_disabled_no_task(monkeypatch):
    """场景 1：开关关闭时，favorite-track/create（在线 guid）不注册下载任务。"""
    monkeypatch.setitem(CONF, "fav_auto_bind", False)
    recorded = []

    async def fake_download(guid, headers):
        recorded.append(guid)

    monkeypatch.setattr(appmod, "_full_fetch_download", fake_download)

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": FAKE_KUWO1},
            cookies={"music-token": "valid_token"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "", "data": None}

    assert recorded == []
    assert FAKE_KUWO1 not in appmod._full_fetch_tasks


def test_scenario2_fav_autobind_enabled_registers_task(monkeypatch):
    """场景 2：开关开启时，favorite-track/create 注册下载任务，断言 guid 正确。"""
    monkeypatch.setitem(CONF, "fav_auto_bind", True)
    # 门禁独立于边听边存开关：即使边听边存关闭，只要 fav_auto_bind 开启仍可注册
    monkeypatch.setitem(CONF, "tee_save_enabled", False)
    recorded = []

    async def fake_download(guid, headers):
        recorded.append(guid)

    monkeypatch.setattr(appmod, "_full_fetch_download", fake_download)

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": FAKE_KUWO1},
            cookies={"music-token": "valid_token"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "", "data": None}

    assert recorded == [FAKE_KUWO1]


def test_scenario3_fav_autobind_skips_when_cached(monkeypatch):
    """场景 3：开关开启且本地已有缓存文件（monkeypatch find_cache_file 返回路径）时不重复注册。"""
    monkeypatch.setitem(CONF, "fav_auto_bind", True)
    monkeypatch.setattr(appmod, "find_cache_file", lambda guid: "/fake/path/music.flac")
    recorded = []

    async def fake_download(guid, headers):
        recorded.append(guid)

    monkeypatch.setattr(appmod, "_full_fetch_download", fake_download)

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": FAKE_KUWO1},
            cookies={"music-token": "valid_token"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "", "data": None}

    assert recorded == []
    assert appmod._full_fetch_tasks == {}


def test_scenario4_playlist_add_track_registered_for_online_only(monkeypatch):
    """场景 4：playlist/add-track 在线条目不再注册下载（issue #120：自动播放
    列表与显式保存同协议无法区分）；推荐/官方分支照旧不触发。"""
    monkeypatch.setitem(CONF, "fav_auto_bind", True)
    recorded = []

    async def fake_download(guid, headers):
        recorded.append(guid)

    monkeypatch.setattr(appmod, "_full_fetch_download", fake_download)

    with TestClient(app) as client:
        # 分支 a：自建歌单添加在线歌曲 -> 意图不可证实，不触发下载注册
        fake_guid = fake_official_guid(FAKE_KUWO1)
        resp = client.post(
            "/music/api/v1/playlist/add-track",
            json={"guid": PL_CUSTOM, "trackGUIDs": [fake_guid]},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "", "data": None}
        assert recorded == []
        # 歌单内容本身照常保存
        bucket = appmod.load_playlist_tracks("user-a")[PL_CUSTOM]
        assert [it["guid"] for it in bucket] == [FAKE_KUWO1]

        recorded.clear()

        # 分支 b：推荐歌单（只读分支提前 return 吸收请求） -> 不触发下载注册
        rec_guid = dailyrec.daily_playlist_guid("20260924", "user-a")
        resp_rec = client.post(
            "/music/api/v1/playlist/add-track",
            json={"guid": rec_guid, "trackGUIDs": [fake_official_guid(FAKE_KUWO2)]},
        )
        assert resp_rec.status_code == 200
        assert resp_rec.json() == {"code": 0, "msg": "", "data": None}
        assert recorded == []

        # 分支 c：自建歌单添加纯官方歌曲 -> 透传官方，不触发下载注册
        resp_off = client.post(
            "/music/api/v1/playlist/add-track",
            json={"guid": PL_CUSTOM, "trackGUIDs": ["official-track-999"]},
        )
        assert resp_off.status_code == 200
        assert resp_off.json() == {"code": 0, "msg": "", "data": None}
        assert recorded == []


def test_scenario5_conf_default_and_env_watch_keys():
    """场景 5：CONF 默认值 fav_auto_bind 为 False；_ENV_WATCH_KEYS 含新键。"""
    assert "FNMUSIC_FAV_AUTO_BIND" in _ENV_WATCH_KEYS
    assert _ENV_WATCH_KEYS["FNMUSIC_FAV_AUTO_BIND"] == ("fav_auto_bind", "bool")
    assert CONF.get("fav_auto_bind") is False or CONF["fav_auto_bind"] is False
    # 2.5.2a：官方绑定等待与切歌续传并行数纳入热重载
    assert _ENV_WATCH_KEYS["FNMUSIC_OFFICIAL_BIND_TIMEOUT_S"] == ("official_bind_timeout_s", "bind_timeout")
    assert _ENV_WATCH_KEYS["FNMUSIC_TEE_HANDOFF_MAX"] == ("tee_handoff_max", "tee_handoff_max")


def test_fav_autobind_cooldown_and_dedup(monkeypatch):
    """进阶测试：失败冷却期内跳过注册；在途未完成任务去重。"""
    monkeypatch.setitem(CONF, "fav_auto_bind", True)
    now = time.monotonic()
    appmod._full_fetch_failed[FAKE_KUWO1] = now
    recorded = []

    async def fake_download(guid, headers):
        recorded.append(guid)

    monkeypatch.setattr(appmod, "_full_fetch_download", fake_download)

    with TestClient(app) as client:
        # 在冷却期内：跳过注册
        resp = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": FAKE_KUWO1},
            cookies={"music-token": "valid_token"},
        )
        assert resp.status_code == 200
        assert recorded == []

        # 冷却期过期后：恢复注册
        appmod._full_fetch_failed[FAKE_KUWO1] = now - _FULL_FETCH_COOLDOWN_S - 1.0
        resp2 = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": FAKE_KUWO1},
            cookies={"music-token": "valid_token"},
        )
        assert resp2.status_code == 200
        assert recorded == [FAKE_KUWO1]


def test_fav_autobind_hot_reload(tmp_path, monkeypatch):
    """热重载测试：修改 .env 中 FNMUSIC_FAV_AUTO_BIND，热重载自动同步到 CONF。"""
    env_file = str(tmp_path / ".env")
    with open(env_file, "w", encoding="utf-8") as f:
        f.write("FNMUSIC_FAV_AUTO_BIND=true\n")

    monkeypatch.setitem(CONF, "fav_auto_bind", False)
    changed = apply_env_hot_reload(env_file)
    assert "fav_auto_bind" in changed
    assert CONF["fav_auto_bind"] is True


# === issue #120：官方客户端把整份搜索结果批量 add-track 进自动创建的播放列表 ===
# 该协议与用户显式"保存到歌单"完全同构且无从取证区分；修复后 add-track
# 一律不触发自动下载/绑定意图（歌单内容照常保存），显式收藏不受影响。


def test_issue120_playlist_batch_add_never_downloads(monkeypatch):
    """批量 add-track 30 首（自动播放列表形态）→ 零下载注册、零绑定意图；
    歌单条目照常落盘且不带 bind=pending。"""
    monkeypatch.setitem(CONF, "fav_auto_bind", True)
    recorded = []

    async def fake_download(guid, headers):
        recorded.append(guid)

    monkeypatch.setattr(appmod, "_full_fetch_download", fake_download)

    batch = [f"online:kuwo:{228908 + i}" for i in range(30)]
    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/playlist/add-track",
            json={"guid": PL_CUSTOM, "trackGUIDs": [fake_official_guid(g) for g in batch]},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "", "data": None}

    assert recorded == []
    assert appmod._full_fetch_tasks == {}
    assert appmod._bind_pending == {}
    bucket = appmod.load_playlist_tracks("user-a")[PL_CUSTOM]
    assert [it["guid"] for it in bucket] == batch
    assert all("bind" not in it for it in bucket)


def test_issue120_explicit_save_intent_still_downloads(monkeypatch):
    """意图判定证实为用户显式保存（可取证判据出现时的通路）→ 自动下载与
    bind=pending 登记照旧；该通路只有判定函数返回 explicit_save 才启用。"""
    monkeypatch.setitem(CONF, "fav_auto_bind", True)
    recorded = []

    async def fake_download(guid, headers):
        recorded.append(guid)

    monkeypatch.setattr(appmod, "_full_fetch_download", fake_download)

    async def explicit_intent(request, user_guid, playlist_guid):
        return appmod.PLAYLIST_INTENT_EXPLICIT_SAVE

    monkeypatch.setattr(appmod, "_playlist_download_intent", explicit_intent)

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/playlist/add-track",
            json={"guid": PL_CUSTOM, "trackGUIDs": [fake_official_guid(FAKE_KUWO1)]},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "", "data": None}

    assert recorded == [FAKE_KUWO1]
    intent = appmod._bind_pending.get(FAKE_KUWO1, {}).get("user-a")
    assert intent and PL_CUSTOM in intent["playlists"]
    bucket = appmod.load_playlist_tracks("user-a")[PL_CUSTOM]
    assert bucket[0]["bind"] == "pending"


def test_issue120_favorite_create_unaffected(monkeypatch):
    """显式收藏是单曲语义明确的操作：自动下载照旧，不受歌单意图判定影响。"""
    monkeypatch.setitem(CONF, "fav_auto_bind", True)
    monkeypatch.setitem(CONF, "tee_save_enabled", False)
    recorded = []

    async def fake_download(guid, headers):
        recorded.append(guid)

    monkeypatch.setattr(appmod, "_full_fetch_download", fake_download)

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": FAKE_KUWO1},
            cookies={"music-token": "valid_token"},
        )
        assert resp.status_code == 200

    assert recorded == [FAKE_KUWO1]
    assert appmod._bind_pending.get(FAKE_KUWO1, {}).get("user-a", {}).get("fav") is True
