"""lxmusic（洛雪音乐源）集成测试：healthz / 搜索聚合 / online:lx:* GUID 流解析与 tee 缓存。"""
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy.app import (
    fake_official_guid,
    app,
    CONF,
    _SEARCH_CACHE,
    _LX_URL_PIN,
    _LX_MEDIA_UA,
    _LX_FRESH_COOLDOWN,
    _lx_media_headers,
    _lx_fresh_cooldown_mark,
    fetch_lx_search,
    resolve_lx_url,
    get_version,
    is_playable_online_track,
    resolve_real_guid,
)


@pytest.fixture(autouse=True)
def setup_lx_env(tmp_path, monkeypatch):
    _SEARCH_CACHE.clear()
    _LX_URL_PIN.clear()
    _LX_FRESH_COOLDOWN.clear()
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
    monkeypatch.setitem(CONF, "search_debounce_s", 0.0)
    monkeypatch.setitem(CONF, "search_deep_page", True)
    monkeypatch.setitem(CONF, "search_deep_max_pages", 10)
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
async def test_fetch_lx_search_verified_vip_passes(monkeypatch):
    """verified 条目（服务端已探活实证）绕过收费元数据拦截（逐曲探活开启时）。"""
    monkeypatch.setitem(CONF, "search_probe", True)

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


@pytest.mark.anyio
async def test_fetch_lx_search_probe_disabled_allows_unverified_vip(monkeypatch):
    """逐曲探活未开启时，lx 候选曲目跳过收费拦截直出（由客户端播放时按需解析）。"""
    monkeypatch.setitem(CONF, "search_probe", False)

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
                        "pay_type": 1,
                        "verified": False,
                    },
                ],
                "errors": {},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8772")
    items = await fetch_lx_search(client, "晴天", 20)
    assert len(items) == 1
    assert items[0]["id"] == "lx:kw:228908"
    assert items[0]["verified"] is False
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


@pytest.mark.anyio
async def test_resolve_lx_url_fresh_param_passthrough():
    """fresh=True 时请求带 fresh=1 旁路 lxmusic 缓存；默认不带该参数（issue #45）。"""
    seen: "list[dict]" = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({
            "id": request.url.params.get("id"),
            "quality": request.url.params.get("quality"),
            "fresh": request.url.params.get("fresh"),
        })
        return httpx.Response(
            200,
            json={"ok": True, "data": {"id": "x", "url": "http://audio.test/a.flac", "ext": "flac"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8772")
    res = await resolve_lx_url(client, "lx:kg:ABCDEF1234567890", fresh=True)
    assert res and res["url"] == "http://audio.test/a.flac"
    n_fresh = len(seen)
    # 首次解析已落钉链，清掉后才能再次打到 lxmusic 验证默认不带 fresh=1
    _LX_URL_PIN.pop("lx:kg:ABCDEF1234567890", None)
    res2 = await resolve_lx_url(client, "lx:kg:ABCDEF1234567890")
    assert res2 and res2["url"] == "http://audio.test/a.flac"
    assert n_fresh >= 1
    assert all(p["fresh"] == "1" for p in seen[:n_fresh])
    assert all(p["fresh"] is None for p in seen[n_fresh:])
    await client.aclose()


@pytest.mark.anyio
async def test_resolve_lx_url_pin_consistent_within_ttl():
    """钉链（2.8.0i）：TTL 内重复解析零出网且 rendition 一致。

    窗口型播放内核按 1MB 窗口各自重解析；同曲换 rendition（如 320k MP3 换
    48k AAC）会让带偏移的续拉错位断流，表象即"播到一半跳歌"。"""
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(
            200,
            json={"ok": True, "data": {"id": "x", "url": f"http://audio.test/v{hits['n']}.flac", "ext": "flac"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8772")
    first = await resolve_lx_url(client, "lx:kg:PINTEST1")
    second = await resolve_lx_url(client, "lx:kg:PINTEST1")
    third = await resolve_lx_url(client, "lx:kg:PINTEST1")
    assert first and second and third
    assert second["url"] == third["url"] == first["url"] == "http://audio.test/v1.flac"
    assert hits["n"] == 1
    # 钉链返回副本：调用方改动不得污染钉链
    second["url"] = "mutated"
    assert (await resolve_lx_url(client, "lx:kg:PINTEST1"))["url"] == "http://audio.test/v1.flac"
    await client.aclose()


@pytest.mark.anyio
async def test_resolve_lx_url_max_quality_bypasses_lossless_pin_and_downgrades():
    """max_quality=high 时旁路无损钉链，且请求 quality=high。"""
    hits = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("quality")
        hits.append(q)
        if q == "lossless":
            return httpx.Response(200, json={"ok": True, "data": {"id": "x", "url": "http://audio.test/lossless.flac", "ext": "flac", "actual_tier": "lossless"}})
        return httpx.Response(200, json={"ok": True, "data": {"id": "x", "url": "http://audio.test/320.mp3", "ext": "mp3", "actual_tier": "high"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8772")
    first = await resolve_lx_url(client, "lx:kg:PINTEST_DOWN")
    assert first and first["ext"] == "flac"
    assert hits == ["lossless"]

    # 再次解析但指定 max_quality="high"：必须旁路无损钉链，重新请求 high
    downgraded = await resolve_lx_url(client, "lx:kg:PINTEST_DOWN", max_quality="high")
    assert downgraded and downgraded["ext"] == "mp3"
    assert downgraded["url"] == "http://audio.test/320.mp3"
    assert hits == ["lossless", "high"]
    await client.aclose()


@pytest.mark.anyio
async def test_resolve_lx_url_pin_expiry_re_resolves():
    """钉链过期：下一次解析重新出链并续钉（lxmusic 探活续期/重解析的入口）。"""
    import time as _time

    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(
            200,
            json={"ok": True, "data": {"id": "x", "url": f"http://audio.test/v{hits['n']}.flac", "ext": "flac"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8772")
    assert await resolve_lx_url(client, "lx:kg:PINTEST2")
    assert hits["n"] == 1
    _LX_URL_PIN["lx:kg:PINTEST2"] = (_LX_URL_PIN["lx:kg:PINTEST2"][0], _time.monotonic() - 1)
    refreshed = await resolve_lx_url(client, "lx:kg:PINTEST2")
    assert hits["n"] == 2
    assert refreshed["url"] == "http://audio.test/v2.flac"
    # 过期后重解析的结果已续钉
    again = await resolve_lx_url(client, "lx:kg:PINTEST2")
    assert again["url"] == "http://audio.test/v2.flac" and hits["n"] == 2
    await client.aclose()


@pytest.mark.anyio
async def test_lx_pin_drop_and_fresh_bypass():
    """弃钉后重新出链；fresh=True 始终旁路钉链强制重解析。"""
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(
            200,
            json={"ok": True, "data": {"id": "x", "url": f"http://audio.test/v{hits['n']}.flac", "ext": "flac"}},
        )

    from proxy.app import _lx_url_pin_drop

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8772")
    assert await resolve_lx_url(client, "lx:kg:PINTEST3")
    _lx_url_pin_drop("lx:kg:PINTEST3")
    after = await resolve_lx_url(client, "lx:kg:PINTEST3")
    assert hits["n"] == 2 and after["url"] == "http://audio.test/v2.flac"
    forced = await resolve_lx_url(client, "lx:kg:PINTEST3", fresh=True)
    assert hits["n"] == 3 and forced["url"] == "http://audio.test/v3.flac"
    await client.aclose()


@pytest.mark.anyio
async def test_stream_open_failure_drops_lx_pin(monkeypatch):
    """取流打不开钉住的直链（连接拒绝）→ 弃钉，重试旁路缓存重新出链。"""
    import proxy.app as proxy_app

    resolve_hits = {"n": 0}

    def lx_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/track/url":
            resolve_hits["n"] += 1
            return httpx.Response(
                200,
                json={"ok": True, "data": {"id": "x", "url": "http://127.0.0.1:1/dead.flac", "ext": "flac"}},
            )
        return httpx.Response(404)

    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lx_handler), base_url="http://127.0.0.1:8772"
    )

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream?guid=online:lx:kg:PINDEAD1")
    assert resp.status_code == 404
    # 首轮钉链打开失败弃钉 + fresh 重试解析：至少两次出链；终态钉链已清
    assert resolve_hits["n"] >= 2
    assert "lx:kg:PINDEAD1" not in _LX_URL_PIN


def test_stream_track_lx_retry_passes_fresh_url(monkeypatch):
    """取流重试（首次打开失败后）必须旁路 lxmusic 缓存：fresh_url 依次为 False、True。"""
    import proxy.app as proxy_app

    opened_fresh: "list[bool]" = []

    async def fake_open(request, guid, range_header, force_mp3=False, fresh_url=False, refresh=False):
        opened_fresh.append(fresh_url)
        return None

    async def fake_recover(request, guid, entry):
        return True

    monkeypatch.setattr(proxy_app, "_open_online_stream", fake_open)
    monkeypatch.setattr(proxy_app, "_recover_source", fake_recover)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_lx_handler_factory()), base_url="http://127.0.0.1:8772"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream?guid=online:lx:kg:ABCDEF1234567890")
    assert resp.status_code == 404
    assert opened_fresh == [False, True]


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
        assert fake_official_guid("online:lx:wy:186016") in guid_set


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
        assert track["guid"] == fake_official_guid("online:lx:kg:ABCDEF1234567890")
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
        assert any(resolve_real_guid(g).startswith("online:lx:") for g in guids)
        assert any("migu" in resolve_real_guid(g) for g in guids)


def test_search_track_deep_pagination_lx_page_passthrough():
    """深分页对洛雪源透传 page 页号：翻页越过首屏池时按 page=2、3 增量取页。"""
    pages_seen: list[int] = []

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})

    def lx_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/v1/search":
            return httpx.Response(404, json={"ok": False})
        page = int(request.url.params.get("page") or 1)
        pages_seen.append(page)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "items": [
                    {
                        "id": f"lx:kg:{page:02d}{i:03d}",
                        "lx_source": "kg",
                        "title": f"晴天{page}-{i}",
                        "artist": "周杰伦",
                        "album": "叶惠美",
                        "duration_s": 269.0,
                        "ext": "flac",
                        "cover_url": "",
                        "file_size": 1000,
                    }
                    for i in range(20)
                ],
                "errors": {},
            },
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lx_handler), base_url="http://127.0.0.1:8772"
    )

    with TestClient(app) as client:
        p1 = client.get("/music/api/v1/search/track?q=晴天&page=1&size=20").json()["data"]
        p2 = client.get("/music/api/v1/search/track?q=晴天&page=2&size=20").json()["data"]
        p3 = client.get("/music/api/v1/search/track?q=晴天&page=3&size=20").json()["data"]
    # 每页都拿满 20 条在线；page=2/3 由深分页增量触发（首屏聚合不发 page 参数）；
    # 末页响应后的预取可能多取一轮更深的页，属预期行为
    assert len(p1["list"]) == 20 and p1["total"] == 20
    assert len(p2["list"]) == 20 and p2["total"] == 40
    assert len(p3["list"]) == 20 and p3["total"] == 60
    assert pages_seen[0] == 1
    assert pages_seen == sorted(pages_seen)
    assert {2, 3}.issubset(set(pages_seen))
    # 跨页零重复
    g1 = {str(i["guid"]) for i in p1["list"]}
    g2 = {str(i["guid"]) for i in p2["list"]}
    g3 = {str(i["guid"]) for i in p3["list"]}
    assert not (g1 & g2) and not (g1 & g3) and not (g2 & g3)


# ----------------------------------------------------- issue #54：UA 一致与刷新有界 --

def test_lx_media_headers_ua_fallback():
    """issue #54：解析结果 headers 为空时兜底探活同款 UA；自带 UA 不被覆盖，
    非 Referer/UA 的头不透传。出流/转码各调用点共用该构建。"""
    assert _lx_media_headers(None) == {"User-Agent": _LX_MEDIA_UA}
    assert _lx_media_headers({"headers": {}}) == {"User-Agent": _LX_MEDIA_UA}
    assert _lx_media_headers({"headers": {"User-Agent": "Custom/1.0",
                                          "Referer": "https://x/",
                                          "Range": "bytes=0-"}}) == {
        "User-Agent": "Custom/1.0", "Referer": "https://x/"}


@pytest.mark.anyio
async def test_resolve_lx_url_fresh_cooldown_bounds_reparse():
    """issue #54：fresh 冷却窗内的强制刷新不出网快速失败（额度有界化），
    冷却到期恢复出网；非 fresh 路径不受冷却影响。"""
    import time as _time

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={
            "ok": True,
            "data": {"id": "lx:kw:CD54", "url": "https://audio.test/cd54.flac",
                     "ext": "flac", "headers": {}},
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://lxmusic-test")
    try:
        ok = await resolve_lx_url(client, "lx:kw:CD54", fresh=True)
        assert ok is not None and calls["n"] == 1

        _lx_fresh_cooldown_mark("lx:kw:CD54")
        blocked = await resolve_lx_url(client, "lx:kw:CD54", fresh=True)
        assert blocked is None
        assert calls["n"] == 1  # 冷却窗内未出网

        # 非 fresh 路径不受冷却影响（钉链/成功缓存照常）
        non_fresh = await resolve_lx_url(client, "lx:kw:CD54", fresh=False)
        assert non_fresh is not None  # 命中钉链或重新出网均可，但不能被冷却拦截
        pre = calls["n"]

        _LX_FRESH_COOLDOWN["lx:kw:CD54"] = _time.monotonic() - 1.0
        recovered = await resolve_lx_url(client, "lx:kw:CD54", fresh=True)
        assert recovered is not None and calls["n"] > pre  # 冷却到期恢复出网
    finally:
        await client.aclose()


# =========================================================================
# issue #49：播放元数据透传 + 在线曲目元数据 LRU
# =========================================================================
@pytest.mark.anyio
async def test_resolve_lx_url_meta_passthrough():
    """meta 元数据透传：songInfo 反查全冷（重启后点播存量歌单）时把真实标签
    带给 lxmusic 兜底合成，关键词型源脚本按歌名歌手取链不再拿到"未知曲目"。"""
    seen: "list[dict]" = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(
            200,
            json={"ok": True, "data": {"id": "x", "url": "http://audio.test/a.flac", "ext": "flac"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8772")
    meta = {"title": "晴天", "artist": "周杰伦", "album": "叶惠美",
            "duration_s": 269.3, "cover_url": "https://img.test/x.jpg"}
    res = await resolve_lx_url(client, "lx:kw:COLDMETA1", meta=meta)
    assert res and res["url"] == "http://audio.test/a.flac"
    p = seen[0]
    assert p.get("title") == "晴天"
    assert p.get("artist") == "周杰伦"
    assert p.get("album") == "叶惠美"
    assert p.get("cover") == "https://img.test/x.jpg"
    assert p.get("duration") == "269.3"
    # 不带 meta 的解析不追加任何标签参数
    _LX_URL_PIN.pop("lx:kw:COLDMETA1", None)
    seen.clear()
    await resolve_lx_url(client, "lx:kw:COLDMETA1")
    assert "title" not in seen[0]
    _LX_URL_PIN.pop("lx:kw:COLDMETA1", None)
    await client.aclose()


def test_register_online_meta_lru_and_play_fallback(monkeypatch):
    """下发出口登记 guid→元数据；播放兜底链先内存搜索会话、miss 后 LRU。"""
    import proxy.app as proxy_app
    from proxy.app import _ONLINE_META, _online_meta_get, register_online_meta

    _ONLINE_META.clear()
    items = [
        {"id": "lx:kg:HASH1", "source": "lx", "title": "自由的你",
         "artist": "G.E.M.邓紫棋", "album": "新专辑", "duration_s": 296,
         "cover_url": "https://img.test/1.jpg"},
        {"id": "wy:186016", "source": "lx", "title": "未知曲目", "artist": "未知歌手"},
    ]
    added = register_online_meta(items)
    assert added == 1  # 未知标题条目跳过
    meta = _online_meta_get("online:lx:kg:HASH1")
    assert meta["title"] == "自由的你"
    assert meta["duration_s"] == 296

    # LRU 兜底：内存搜索会话 miss 时用 LRU（歌单/收藏/历史播放场景）
    monkeypatch.setattr(proxy_app, "_retained_track", lambda request, guid: (None, None))
    got = proxy_app._online_meta_for_request(object(), "online:lx:kg:HASH1")
    assert got and got["title"] == "自由的你"
    # 未登记的 guid 无兜底
    assert proxy_app._online_meta_for_request(object(), "online:lx:kg:ABSENT") is None

    # 内存会话命中优先于 LRU（数据更新）
    monkeypatch.setattr(
        proxy_app, "_retained_track",
        lambda request, guid: ({"title": "会话名", "artist": "会话歌手", "duration_s": 10}, None),
    )
    got2 = proxy_app._online_meta_for_request(object(), "online:lx:kg:HASH1")
    assert got2["title"] == "会话名"
    _ONLINE_META.clear()


def test_register_online_meta_capacity_eviction(monkeypatch):
    """LRU 容量上限：超限淘汰最旧，重新登记视为最新。"""
    import proxy.app as proxy_app
    from proxy.app import _ONLINE_META, register_online_meta_one

    _ONLINE_META.clear()
    monkeypatch.setattr(proxy_app, "_ONLINE_META_MAX", 3)
    for i in range(5):
        register_online_meta_one(f"online:lx:kw:{i}", f"t{i}", "a")
    assert len(_ONLINE_META) == 3
    assert "online:lx:kw:0" not in _ONLINE_META
    assert _ONLINE_META["online:lx:kw:4"]["title"] == "t4"
    # 重新登记视为最新，不挤掉自己也不淘汰错位
    register_online_meta_one("online:lx:kw:2", "t2-new", "a")
    assert _ONLINE_META["online:lx:kw:2"]["title"] == "t2-new"
    assert "online:lx:kw:1" not in _ONLINE_META  # 2 移到末尾后 1 成为最旧被淘汰
    _ONLINE_META.clear()


@pytest.mark.asyncio
async def test_mobile_cancelled_resolve_rejoins_same_cold_lx_task(monkeypatch):
    import asyncio
    import proxy.app as pa
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def slow(*args, **kwargs):
        calls.append(kwargs)
        started.set()
        await release.wait()
        return {"url": "http://audio.test/pinned.mp3", "ext": "mp3"}
    monkeypatch.setattr(pa, "resolve_lx_url", slow)
    first = asyncio.create_task(pa.resolve_lx_url_shared(None, "lx:kw:75"))
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(pa.resolve_lx_url_shared(None, "lx:kw:75"))
    await asyncio.sleep(0)
    release.set()
    result = await second
    assert result["url"].endswith("pinned.mp3")
    assert len(calls) == 1
    await asyncio.sleep(0)
    assert not pa._LX_URL_INFLIGHT


@pytest.mark.asyncio
async def test_shared_lx_scopes_quality_fresh_and_bounds_work(monkeypatch):
    import asyncio
    import proxy.app as pa
    release = asyncio.Event()
    calls = []
    async def slow(*args, **kwargs):
        calls.append(kwargs)
        await release.wait()
        return {"url": "http://audio.test/a.mp3"}
    monkeypatch.setattr(pa, "resolve_lx_url", slow)
    tasks = [asyncio.create_task(pa.resolve_lx_url_shared(None, "lx:kw:75", **scope))
             for scope in ({}, {"max_quality": "high"}, {"fresh": True})]
    tasks += [asyncio.create_task(pa.resolve_lx_url_shared(None, f"lx:kw:{n}"))
              for n in range(20)]
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(pa._LX_URL_INFLIGHT) == 16
    release.set()
    results = await asyncio.gather(*tasks)
    assert len(calls) == 16
    assert all(result for result in results[:3])
    assert sum(result is None for result in results) == 7


@pytest.mark.asyncio
async def test_lx_source_change_cannot_publish_old_resolution_or_pin(monkeypatch):
    import asyncio
    import proxy.app as pa
    started, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # A backend can finish while cancellation is already in flight.
            pass
        return httpx.Response(200, json={"ok": True, "data": {
            "url": "http://audio.test/old.mp3", "ext": "mp3"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://lx.test") as client:
        old = asyncio.create_task(pa.resolve_lx_url_shared(client, "lx:kw:75"))
        await started.wait()
        monkeypatch.setitem(CONF, "lx_source_url", "https://source.test/new.js")
        pa._sync_lx_config_epoch()
        release.set()
        await asyncio.gather(old, return_exceptions=True)
        await asyncio.sleep(0)
        assert "lx:kw:75" not in pa._LX_URL_PIN
        assert not pa._LX_URL_INFLIGHT


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected_fresh", [(TimeoutError, False), (httpx.ConnectError, True)])
async def test_lx_stream_retry_only_refreshes_confirmed_link_failure(monkeypatch, failure, expected_fresh):
    import proxy.app as pa
    monkeypatch.setattr(pa, "resolve_local_play", _no_local)
    monkeypatch.setattr(pa, "find_cache_file", lambda *a, **k: None)
    monkeypatch.setattr(pa, "_retained_track", lambda *a: (None, None))
    flags = []
    async def open_stream(request, guid, range_header, **kwargs):
        flags.append(kwargs["fresh_url"])
        raise failure("simulated")
    monkeypatch.setattr(pa, "_open_online_stream", open_stream)
    request = pa.Request({"type": "http", "method": "GET", "path": "/music/api/v1/track/stream",
                          "query_string": b"guid=online:lx:kw:75", "headers": [], "app": app})
    await pa.stream_track(request)
    assert flags == [False, expected_fresh]


async def _no_local(*args, **kwargs):
    return None


def test_lx_config_epoch_can_be_invalidated_without_running_loop(monkeypatch):
    import proxy.app as pa
    pa._sync_lx_config_epoch()
    pa._LX_URL_PIN["lx:kw:75"] = ({"url": "old"}, 999999999999)
    monkeypatch.setitem(CONF, "lx_source_url", "https://source.test/changed.js")
    pa._sync_lx_config_epoch()
    assert not pa._LX_URL_PIN


@pytest.mark.asyncio
async def test_lx_prefetch_is_bounded_shared_and_shutdown_drained(monkeypatch):
    import asyncio
    import proxy.app as pa
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def slow(*args, **kwargs):
        calls.append(args[1])
        started.set()
        await release.wait()
        return {"url": "http://audio.test/a.mp3"}
    monkeypatch.setattr(pa, "resolve_lx_url", slow)
    monkeypatch.setattr(pa, "find_cache_file", lambda *a, **k: None)
    monkeypatch.setattr(pa, "_online_meta_for_request", lambda *a: None)
    monkeypatch.setattr(pa, "get_lx_client", lambda *a: None)
    request = pa.Request({"type": "http", "headers": [], "app": app})
    for n in range(10):
        pa._prefetch_lx(request, f"online:lx:kw:{n}")
    assert len(pa._LX_URL_PREFETCH) == 2
    await started.wait()
    joined = asyncio.create_task(pa.resolve_lx_url_shared(None, "lx:kw:0"))
    await asyncio.sleep(0)
    drained = pa._cancel_lx_resolutions()
    await asyncio.gather(*drained, joined, return_exceptions=True)
    assert calls.count("lx:kw:0") == 1
    assert not pa._LX_URL_INFLIGHT and not pa._LX_URL_PREFETCH
