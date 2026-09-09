"""lxmusic（洛雪音乐源）集成测试：healthz / 搜索聚合 / online:lx:* GUID 流解析与 tee 缓存。"""
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy.app import (
    app,
    CONF,
    _SEARCH_CACHE,
    fetch_lx_search,
    resolve_lx_url,
    get_version,
    is_playable_online_track,
)


@pytest.fixture(autouse=True)
def setup_lx_env(tmp_path, monkeypatch):
    _SEARCH_CACHE.clear()
    cache_dir = str(tmp_path / "cache")
    library_dir = str(tmp_path / "library")
    fav_dir = str(tmp_path / "online_favorites")
    os.makedirs(library_dir, exist_ok=True)
    os.makedirs(fav_dir, exist_ok=True)
    monkeypatch.setitem(CONF, "cache_dir", cache_dir)
    monkeypatch.setitem(CONF, "library_dir", library_dir)
    monkeypatch.setitem(CONF, "fav_dir", fav_dir)
    monkeypatch.setitem(CONF, "search_list_path", "data.list")
    monkeypatch.setitem(CONF, "online_limit", 30)
    monkeypatch.setitem(CONF, "netease_search_limit", 50)
    monkeypatch.setitem(CONF, "merge_suggest", False)
    monkeypatch.setitem(CONF, "lyric_field", "data.lyric")
    monkeypatch.setitem(CONF, "musicdl_enabled", False)
    monkeypatch.setitem(CONF, "netease_enabled", False)
    monkeypatch.setitem(CONF, "lx_enabled", True)
    monkeypatch.setitem(CONF, "lx_search_limit", 20)
    monkeypatch.setitem(CONF, "lx_quality", "lossless")
    monkeypatch.setitem(CONF, "netease_wait_s", 2.5)
    monkeypatch.setitem(CONF, "search_cache_ttl", 300.0)
    monkeypatch.setitem(CONF, "late_page_wait_s", 5.0)
    yield
    # 恢复全局 app.state，避免 mock 客户端泄漏到其它测试文件
    for attr in ("upstream_client", "musicdl_client", "musicbox_client", "lx_client"):
        setattr(app.state, attr, None)


def _lx_handler_factory(calls=None):
    def lx_handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True, "service": "fnmusic-lxmusic"})
        if request.url.path == "/api/v1/search":
            assert request.url.params.get("keyword") == "晴天"
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "items": [
                        {
                            "id": "lx:kg:ABCDEF1234567890",
                            "lx_source": "kg",
                            "title": "晴天",
                            "artist": "周杰伦",
                            "album": "叶惠美",
                            "duration_s": 269.0,
                            "ext": "flac",
                            "cover_url": "https://img.test/kg.jpg",
                            "file_size": 28936190,
                        },
                        {
                            "id": "lx:wy:186016",
                            "lx_source": "wy",
                            "title": "晴天 (Live)",
                            "artist": "周杰伦",
                            "album": "",
                            "duration_s": 301.2,
                            "ext": "mp3",
                            "cover_url": "",
                            "file_size": 0,
                        },
                    ],
                    "errors": {},
                },
            )
        if request.url.path == "/api/v1/track/url":
            assert request.url.params.get("id") == "lx:kg:ABCDEF1234567890"
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "id": "lx:kg:ABCDEF1234567890",
                        "url": "http://audio.test/kg_track.flac",
                        "ext": "flac",
                        "br": 999000,
                        "file_size": 28936190,
                        "headers": {"User-Agent": "Mozilla/5.0"},
                    },
                },
            )
        if request.url.path == "/api/v1/track/info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "id": "lx:kg:ABCDEF1234567890",
                        "lx_source": "kg",
                        "title": "晴天",
                        "artist": "周杰伦",
                        "album": "叶惠美",
                        "duration_s": 269.0,
                        "ext": "flac",
                        "file_size": 28936190,
                        "cover_url": "https://img.test/kg.jpg",
                    },
                },
            )
        if request.url.path == "/api/v1/track/lyric":
            return httpx.Response(
                200,
                json={"ok": True, "data": {"id": "lx:kg:ABCDEF1234567890", "lyric": "[00:00.00]晴天 - 周杰伦\n"}},
            )
        return httpx.Response(404)

    return lx_handler


# =========================================================================
# 1. fetch_lx_search 映射
# =========================================================================
@pytest.mark.anyio
async def test_fetch_lx_search_mapping():
    client = httpx.AsyncClient(transport=httpx.MockTransport(_lx_handler_factory()), base_url="http://127.0.0.1:8772")
    items = await fetch_lx_search(client, "晴天", 20)
    assert isinstance(items, list) and len(items) == 2
    first = items[0]
    assert first["id"] == "lx:kg:ABCDEF1234567890"
    assert first["source"] == "lx"
    assert first["lx_source"] == "kg"
    assert first["title"] == "晴天"
    assert first["artist"] == "周杰伦"
    assert first["ext"] == "flac"
    assert first["duration_s"] == 269.0
    await client.aclose()


@pytest.mark.anyio
async def test_fetch_lx_search_verified_vip_passes():
    """verified 条目（服务端已探活实证）绕过收费元数据拦截。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "items": [
                    {
                        "id": "lx:kw:228908",
                        "lx_source": "kw",
                        "title": "晴天",
                        "artist": "周杰伦",
                        "album": "叶惠美",
                        "duration_s": 269.0,
                        "ext": "flac",
                        "cover_url": "",
                        "file_size": 38210000,
                        "pay_type": 1,  # VIP 元数据保留
                        "verified": True,  # 但已探活实证可播
                    },
                    {
                        "id": "lx:wy:999999",
                        "lx_source": "wy",
                        "title": "晴天 (未验证VIP)",
                        "artist": "周杰伦",
                        "album": "",
                        "duration_s": 269.0,
                        "ext": "mp3",
                        "cover_url": "",
                        "file_size": 0,
                        "fee": 1,  # VIP 且未 verified → 仍被拦截
                    },
                ],
                "errors": {},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8772")
    items = await fetch_lx_search(client, "晴天", 20)
    assert [it["id"] for it in items] == ["lx:kw:228908"]
    assert items[0]["verified"] is True  # 字段透传
    await client.aclose()


# =========================================================================
# 1b. is_playable_online_track：verified 可播性实证语义
# =========================================================================
def test_is_playable_online_track_verified_semantics():
    base = {"id": "lx:kw:228908", "title": "晴天", "artist": "周杰伦", "duration_s": 269.0}

    # 未 verified 的 VIP/付费条目：保持原有拦截（回归）
    assert is_playable_online_track({**base, "pay_type": 3}) is False
    assert is_playable_online_track({**base, "fee": 1}) is False
    assert is_playable_online_track({**base, "price": 8}) is False

    # verified 条目：服务端已 Range 探活实证可播，跳过收费元数据拦截
    assert is_playable_online_track({**base, "pay_type": 3, "verified": True}) is True
    assert is_playable_online_track({**base, "fee": 1, "verified": True}) is True

    # verified 只豁免收费检查；真不可播的防线全部保留
    assert is_playable_online_track({**base, "pay_type": 3, "verified": True, "title": "晴天 (试听)"}) is False
    assert is_playable_online_track({**base, "verified": True, "is_trial": True}) is False
    assert is_playable_online_track({**base, "verified": True, "url": "https://x/404/error.html"}) is False
    assert is_playable_online_track({**base, "verified": True, "playable": False}) is False

    # 免费条目行为不变
    assert is_playable_online_track({**base}) is True


@pytest.mark.anyio
async def test_resolve_lx_url_downgrade():
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        if request.url.params.get("quality") == "lossless":
            return httpx.Response(200, json={"ok": True, "data": {"id": "x", "url": ""}})
        return httpx.Response(
            200,
            json={"ok": True, "data": {"id": "x", "url": "http://audio.test/320.mp3", "ext": "mp3"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8772")
    res = await resolve_lx_url(client, "lx:wy:186016")
    assert res and res["url"] == "http://audio.test/320.mp3"
    assert hits["n"] == 2
    await client.aclose()


@pytest.mark.anyio
async def test_resolve_lx_url_all_fail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8772")
    assert await resolve_lx_url(client, "lx:mg:600902") is None
    await client.aclose()


# =========================================================================
# 2. healthz 包含 lxmusic 状态
# =========================================================================
def test_ext_healthz_includes_lxmusic():
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_lx_handler_factory()), base_url="http://127.0.0.1:8772"
    )

    with TestClient(app) as client:
        resp = client.get("/_ext/healthz")
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["lxmusic"] == "ok"
        assert rj["ok"] is True
        assert rj["version"] == get_version()


def test_ext_healthz_lxmusic_disabled(monkeypatch):
    monkeypatch.setitem(CONF, "lx_enabled", False)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0})

    def fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(fail_handler), base_url="http://127.0.0.1:8772"
    )

    with TestClient(app) as client:
        rj = client.get("/_ext/healthz").json()
        assert rj["lxmusic"] == "disabled"


# =========================================================================
# 3. 搜索聚合：online:lx:<source>:<id> GUID
# =========================================================================
def test_search_merge_lx_items():
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "OK",
                "data": {
                    "list": [
                        {"guid": "local:101", "title": "晴天", "artist": "周杰伦", "album": "叶惠美"},
                    ],
                    "total": 1,
                },
            },
        )

    def fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(fail_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(fail_handler), base_url="http://127.0.0.1:8770"
    )
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_lx_handler_factory()), base_url="http://127.0.0.1:8772"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?keyword=晴天&size=50")
        assert resp.status_code == 200
        data = resp.json()
        guid_set = {item.get("guid") for item in data["data"]["list"]}
        # 上游本地曲库条目保留
        assert "local:101" in guid_set
        # lx 条目合并进列表（与上游 (title, artist) 重复的 kg 条目被去重，Live 版保留）
        assert "online:lx:wy:186016" in guid_set


# =========================================================================
# 4. stream_track lx 分支：直链透传 + tee 落盘 + 缓存回放
# =========================================================================
def test_stream_track_lx_direct_stream_and_cache(monkeypatch, tmp_path):
    audio_content = b"KG_TRACKERCDN_FLAC_TEST_PAYLOAD_" * 80
    content_len = str(len(audio_content))

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Should not hit upstream")

    def direct_stream_handler(request: httpx.Request) -> httpx.Response:
        if "audio.test" in str(request.url):
            assert request.headers.get("user-agent") == "Mozilla/5.0"
            return httpx.Response(
                200,
                content=audio_content,
                headers={
                    "Content-Type": "audio/mpeg",
                    "Content-Length": content_len,
                    "Accept-Ranges": "bytes",
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_lx_handler_factory()), base_url="http://127.0.0.1:8772"
    )

    orig_async_client_init = httpx.AsyncClient.__init__

    def mock_client_init(self, *args, **kwargs):
        if "base_url" not in kwargs and not kwargs.get("transport"):
            kwargs["transport"] = httpx.MockTransport(direct_stream_handler)
        orig_async_client_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", mock_client_init)

    guid = "online:lx:kg:ABCDEF1234567890"
    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/track/stream?guid={guid}")
        assert resp.status_code == 200
        assert resp.content == audio_content
        # lx url 接口声明 ext=flac，覆盖源站 audio/mpeg
        assert resp.headers.get("content-type") == "audio/flac"

        saved_file = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.flac")
        assert os.path.exists(saved_file)
        with open(saved_file, "rb") as f:
            assert f.read() == audio_content

        # 二次播放：缓存命中 206 Range 回放，不再请求远端
        resp2 = client.get(f"/music/api/v1/track/stream?guid={guid}", headers={"Range": "bytes=0-9"})
        assert resp2.status_code == 206
        assert resp2.content == audio_content[:10]


def test_stream_track_lx_unavailable_404():
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def no_url_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/track/url":
            return httpx.Response(200, json={"ok": False, "error": "no playable url"})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(no_url_handler), base_url="http://127.0.0.1:8772"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream?guid=online:lx:kg:notfound")
        assert resp.status_code == 404
        assert resp.json()["code"] == 404


# =========================================================================
# 5. 歌词 / 元数据：lx 分支
# =========================================================================
def test_lyric_list_from_lxmusic():
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_lx_handler_factory()), base_url="http://127.0.0.1:8772"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/lyric/list?trackGUID=online:lx:kg:ABCDEF1234567890")
        assert resp.status_code == 200
        content = resp.json()["data"]["list"][0]["content"]
        assert "晴天 - 周杰伦" in content

        resp2 = client.get("/music/api/v1/track/metadata?guid=online:lx:kg:ABCDEF1234567890")
        assert resp2.status_code == 200
        track = resp2.json()["data"]["track"]
        assert track["guid"] == "online:lx:kg:ABCDEF1234567890"
        assert track["title"] == "晴天"
        assert track["audioSpec"]["format"] == "flac"


def test_search_without_netease_merges_lx_and_musicdl(monkeypatch):
    """无 NetEase 时前台 wait/gather 必须正确合并 musicdl + lx（回归 gather 解包 bug）。"""
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "netease_enabled", False)
    monkeypatch.setitem(CONF, "lx_enabled", True)
    monkeypatch.setitem(CONF, "netease_wait_s", 0.5)
    monkeypatch.setitem(CONF, "search_timeout", 4.0)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if "search/track" in request.url.path:
            return httpx.Response(
                200,
                json={"code": 0, "data": {"list": [{"guid": "local-1", "title": "本地晴天"}], "total": 1}},
            )
        return httpx.Response(200, json={"code": 0, "data": None})

    def mdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search":
            return httpx.Response(
                200,
                json={"items": [{"id": "migu:1", "source": "migu", "title": "晴天", "artist": "周杰伦", "duration_s": 200, "ext": "mp3"}]},
            )
        return httpx.Response(200, json={"ok": True})

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler), base_url="http://unix")
    app.state.musicdl_client = httpx.AsyncClient(transport=httpx.MockTransport(mdl_handler), base_url="http://127.0.0.1:8768")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        base_url="http://127.0.0.1:8770",
    )
    app.state.lx_client = httpx.AsyncClient(transport=httpx.MockTransport(_lx_handler_factory()), base_url="http://127.0.0.1:8772")

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track", params={"keyword": "晴天", "page": 1, "size": 50})
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("code") == 0
        guids = [str(x.get("guid")) for x in (body.get("data") or {}).get("list") or []]
        assert "local-1" in guids
        assert any(g.startswith("online:lx:") for g in guids)
        assert any("migu" in g for g in guids)
