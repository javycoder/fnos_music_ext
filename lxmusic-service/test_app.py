"""lxmusic-service API 测试：搜索/解析管线走用户自定义源替身，内置平台接口走 MockTransport。

锁定行为：
- 统一曲目 ID 契约 "lx:<source>:<identifier>"
- 播放解析唯一通道 = 用户源 musicUrl（quality 档位映射 + 降级）
- capabilities 门控（无源/平台未声明/熔断打开）
- /api/v1/source 管理端点、healthz、榜单、歌词
"""
from __future__ import annotations

import base64 as _b64
import sys
import time
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FakeRuntime, lxapp, mock_client
from source_runtime import SourceError

# ------------------------------------------------------------------ 搜索固定响应 ---

_KW_RS_BODY = (
    "{'abslist':["
    "{'MUSICRID':'MUSIC_228908','SONGNAME':'晴天','ARTIST':'周杰伦','ALBUM':'叶惠美',"
    "'DURATION':269,'PAY':1,'web_albumpic_short':'120/85/1/4091887608.jpg',"
    "'payInfo':{'cannotOnlinePlay':'0','cannotDownload':'1'}},"
    "{'MUSICRID':'MUSIC_111222','SONGNAME':'晴天 (DJ版)','ARTIST':'路人','DURATION':130,'PAY':0,"
    "'payInfo':{'cannotOnlinePlay':'1'}}"
    "]}"
)


def _media_handler(total=38210000, ext="flac"):
    def handler(request: httpx.Request) -> httpx.Response:
        if "media.test" in str(request.url):
            return httpx.Response(
                206,
                headers={"Content-Type": f"audio/x-{ext}", "Content-Range": f"bytes 0-1/{total}"},
                content=b"fLaC" if ext == "flac" else b"ID3",
            )
        return httpx.Response(404)

    return handler


def _kw_media_handler(media_total=38210000):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "search.kuwo.cn" in url:
            assert request.url.params.get("all") == "晴天"
            return httpx.Response(200, text=_KW_RS_BODY)
        if "media.test" in url:
            return httpx.Response(
                206,
                headers={"Content-Type": "audio/x-flac", "Content-Range": f"bytes 0-1/{media_total}"},
                content=b"fLaC",
            )
        return httpx.Response(404)

    return handler


# ------------------------------------------------------------------ ID 契约 ---

def test_normalize_sources_startup():
    """LX_SOURCES 启动归一：别名→规范代码、去重、非法丢弃、全非法回退默认。"""
    assert lxapp._normalize_sources(["kugou", "kuwo", "kugou"]) == ["kg", "kw"]
    assert lxapp._normalize_sources(["qq", "tx"]) == ["tx"]
    assert lxapp._normalize_sources(["bogus", ""]) == ["kg", "wy", "mg", "kw"]
    assert lxapp._normalize_sources([]) == ["kg", "wy", "mg", "kw"]
    # 模块加载时 CONF["sources"] 必然是归一结果（别名若不归一会被 _SEARCHERS 静默跳过）
    assert lxapp.CONF["sources"] == lxapp._normalize_sources(lxapp.CONF["sources"])
    assert all(src in lxapp._SEARCHERS for src in lxapp.CONF["sources"])


def test_parse_track_id():
    assert lxapp.parse_track_id("lx:kg:ABC123") == ("kg", "ABC123")
    assert lxapp.parse_track_id("lx:wy:186016") == ("wy", "186016")
    assert lxapp.parse_track_id("lx:mg:600902") == ("mg", "600902")
    assert lxapp.parse_track_id("lx:tx:0039MnYb0qxYhV") == ("tx", "0039MnYb0qxYhV")
    assert lxapp.parse_track_id("lx:kw:228908") == ("kw", "228908")
    assert lxapp.parse_track_id("kg:HASH2") == ("kg", "HASH2")
    assert lxapp.parse_track_id("lx:xx:1") == ("", "")
    assert lxapp.parse_track_id("garbage") == ("", "")
    assert lxapp.parse_track_id("") == ("", "")
    # 冒号在 identifier 内不算分隔符
    assert lxapp.parse_track_id("lx:wy:a:b:c") == ("wy", "a:b:c")


def test_quality_tiers_mapping():
    assert lxapp._quality_tiers("lossless") == ["lossless", "high", "standard"]
    assert lxapp._quality_tiers("flac") == ["lossless", "high", "standard"]
    assert lxapp._quality_tiers("high") == ["high", "standard"]
    assert lxapp._quality_tiers("320") == ["high", "standard"]
    assert lxapp._quality_tiers("standard") == ["standard"]
    assert lxapp._quality_tiers("") == ["standard"]


# ------------------------------------------------------------------ capabilities ---

def test_capabilities_without_source(isolated):
    caps = lxapp.source_capabilities()
    assert set(caps) == {"kg", "wy", "mg", "tx", "kw"}
    assert all(not c["playback_available"] for c in caps.values())
    assert all(not c["search_available"] for c in caps.values())
    assert all(c["reason"] == "no_source_configured" for c in caps.values())
    assert all(c["qualitys"] == [] for c in caps.values())


def test_capabilities_init_failed_when_configured(isolated):
    isolated.last_error = "init: boom"
    isolated.active_url = "https://src.test/1.js"
    caps = lxapp.source_capabilities()
    assert all(c["reason"] == "source_init_failed" for c in caps.values())


def test_capabilities_declared_platforms_only(isolated):
    isolated._runtime = FakeRuntime(platforms={"kw": ["128k", "320k", "flac"], "tx": ["128k"]})
    caps = lxapp.source_capabilities()
    assert caps["kw"]["playback_available"] is True
    assert caps["kw"]["search_available"] is True
    assert caps["kw"]["qualitys"] == ["128k", "320k", "flac"]
    assert caps["tx"]["playback_available"] is True
    for src in ("kg", "wy", "mg"):
        assert caps[src]["playback_available"] is False
        assert caps[src]["search_available"] is False
        assert caps[src]["reason"] == "platform_not_supported"


def test_capabilities_circuit_open(isolated):
    isolated._runtime = FakeRuntime()  # 默认声明 kw
    lxapp._CHAIN_HEALTH["user_source"] = {"fails": 0, "open_until": time.time() + 60, "breaks": 1}
    caps = lxapp.source_capabilities()
    # 已声明平台被熔断拦截；未声明平台仍报 platform_not_supported
    assert caps["kw"]["reason"] == "source_circuit_open"
    assert caps["kw"]["playback_available"] is False
    assert caps["kw"]["search_available"] is False
    assert caps["kg"]["reason"] == "platform_not_supported"


def test_runtime_override_does_not_replace_manager(isolated):
    original = lxapp.SOURCE_MANAGER
    fake = FakeRuntime(platforms={"tx": ["128k"]})
    token = lxapp._RUNTIME_OVERRIDE.set(fake)
    try:
        assert lxapp.SOURCE_MANAGER is original
        assert lxapp.current_runtime() is fake
    finally:
        lxapp._RUNTIME_OVERRIDE.reset(token)
    assert lxapp._RUNTIME_OVERRIDE.get() is None


# ------------------------------------------------------------------ 搜索：用户源管线 ---

def test_search_kw_verified_via_user_source(isolated):
    rt = FakeRuntime()  # 默认 kw + flac 直链
    isolated._runtime = rt
    lxapp.app.state.http = mock_client(_kw_media_handler())

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "晴天", "sources": "kw"})
        assert resp.status_code == 200
        rj = resp.json()
        # cannotOnlinePlay=1 剔除；VIP 曲经用户源解析+探活通过后保留
        ids = [it["id"] for it in rj["items"]]
        assert ids == ["lx:kw:228908"]
        item = rj["items"][0]
        assert item["verified"] is True
        assert item["ext"] == "flac"
        assert item["file_size"] == 38210000
        assert item["cover_url"].startswith("https://img1.kuwo.cn/star/albumcover/")
        assert len(rt.calls) == 1
        assert rt.calls[0]["quality"] == "128k"  # standard 档 → 脚本 128k
        assert rt.calls[0]["info"]["rid"] == "228908"

        # track/url 复用探活缓存（15 分钟内不再回源）
        resp2 = client.get("/api/v1/track/url", params={"id": "lx:kw:228908", "quality": "lossless"})
        assert resp2.status_code == 200
        data = resp2.json()["data"]
        assert data["url"].endswith(".flac")
        assert data["ext"] == "flac"
        assert len(rt.calls) == 1  # 缓存命中，未再调脚本


def test_search_kw_quality_downgrade_when_flac_undeclared(isolated):
    rt = FakeRuntime(platforms={"kw": ["128k"]})
    isolated._runtime = rt
    lxapp.app.state.http = mock_client(_kw_media_handler())

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/url", params={"id": "lx:kw:228908", "quality": "lossless"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        # 脚本只声明 128k：lossless/high 档无匹配质量，降档到 standard
        assert [c["quality"] for c in rt.calls] == ["128k"]
        assert data["ext"] == "flac"  # 探活实证的实际容器
    cached = lxapp._cache_get("lx:kw:228908")
    assert cached["_probe"]["attempted_tiers"] == ["lossless", "high", "standard"]
    # 降档完成后 lossless 请求可复用
    assert lxapp._fresh_probe(cached, "lossless") is not None


def test_search_wy_vip_excluded_when_source_rejects(isolated):
    rt = FakeRuntime(platforms={"wy": ["128k"]}, resolver=SourceError("resolve", "no url"))
    isolated._runtime = rt
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "music.163.com/api/search" in url:
            calls["n"] += 1
            return httpx.Response(
                200,
                json={
                    "result": {
                        "songs": [
                            {"id": 777888, "name": "晴天", "artists": [{"name": "周杰伦"}],
                             "duration": 269000, "fee": 1},
                            {"id": 186016, "name": "晴天", "artists": [{"name": "周杰伦"}],
                             "duration": 269000, "fee": 0},
                        ]
                    }
                },
            )
        return httpx.Response(404)

    lxapp.app.state.http = mock_client(handler)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "晴天", "sources": "wy"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert [it["id"] for it in items] == ["lx:wy:186016"]  # VIP 曲未混入
    assert calls["n"] == 1
    # 单次失败不应熔断
    snap = lxapp.chain_health_snapshot()
    assert snap["user_source"]["fails"] == 1 and snap["user_source"]["open"] is False


def test_search_platform_not_declared_zero_requests(isolated):
    isolated._runtime = FakeRuntime(platforms={"kw": ["128k"]})  # tx 未声明
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(404)

    lxapp.app.state.http = mock_client(handler)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "晴天", "sources": "tx"})
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["items"] == []
        assert rj["errors"]["tx"] == "platform_not_supported"
    assert hits["n"] == 0  # 能力门控在发起搜索前拦截


def test_search_without_source_returns_reason(isolated):
    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "晴天", "sources": "kw"})
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["items"] == []
        assert rj["errors"]["kw"] == "no_source_configured"


def test_search_kw_partial_media_rejected(isolated):
    """声称 400KB 的试听片段（269s 的歌）：空实体无媒体签名，探活剔除。"""
    isolated._runtime = FakeRuntime()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "search.kuwo.cn" in url:
            return httpx.Response(200, text=_KW_RS_BODY)
        if "media.test" in url:
            return httpx.Response(
                206,
                headers={"Content-Type": "audio/x-flac", "Content-Range": "bytes 0-1/400000"},
            )
        return httpx.Response(404)

    lxapp.app.state.http = mock_client(handler)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "晴天", "sources": "kw"})
        assert resp.status_code == 200
        assert resp.json()["items"] == []


# ------------------------------------------------------------------ track/url ---

def test_track_url_invalid_id():
    with TestClient(lxapp.app) as client:
        assert client.get("/api/v1/track/url", params={"id": "bogus"}).status_code == 400
        assert client.get("/api/v1/track/url", params={"id": "lx:xx:1"}).status_code == 400


def test_track_url_no_source_404_with_reason(isolated):
    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/url", params={"id": "lx:kw:228908"})
        assert resp.status_code == 404
        assert resp.json()["error"] == "no_source_configured"


def test_track_url_dead_media_link_404_without_circuit_trip(isolated):
    """脚本返回了直链但媒体 404：解析"干净失败"，不算源故障。"""
    isolated._runtime = FakeRuntime(resolver=lambda i, q, p: "https://dead.test/x.flac")
    lxapp.app.state.http = mock_client(_media_handler())

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/url", params={"id": "lx:kw:228908"})
        assert resp.status_code == 404
    snap = lxapp.chain_health_snapshot()
    assert snap["user_source"]["fails"] == 0


def test_track_url_source_error_returns_502(isolated):
    isolated._runtime = FakeRuntime(resolver=SourceError("resolve", "script exploded"))

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/url", params={"id": "lx:kw:228908"})
        assert resp.status_code == 502
        assert "resolve failed" in resp.json()["error"]


def test_track_url_music_info_carries_platform_keys(isolated):
    rt = FakeRuntime(platforms={"kg": ["128k", "320k", "flac"], "kw": ["128k"]})
    isolated._runtime = rt
    lxapp.app.state.http = mock_client(_kw_media_handler())

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/url", params={"id": "lx:kg:KGHASH1", "quality": "standard"})
        assert resp.status_code == 200
    info = rt.calls[0]["info"]
    assert info["songmid"] == "KGHASH1"
    assert info["hash"] == "KGHASH1"
    assert info["source"] == "kg"


# ------------------------------------------------------------------ 熔断 ---

def test_circuit_opens_after_consecutive_failures(isolated):
    rt = FakeRuntime(resolver=SourceError("resolve", "boom"))
    isolated._runtime = rt

    async def run():
        http = mock_client(lambda r: httpx.Response(404))
        try:
            for _ in range(5):
                try:
                    await lxapp.resolve_and_probe(
                        http, "kw", {"id": "lx:kw:1", "title": "t", "duration_s": 200}
                    )
                except lxapp.ChainTransportError:
                    pass
        finally:
            await http.aclose()

    import asyncio

    asyncio.run(run())
    assert len(rt.calls) == 3  # 第 4、5 次被熔断跳过
    snap = lxapp.chain_health_snapshot()
    assert snap["user_source"]["open"] is True
    assert snap["user_source"]["breaks"] == 1
    assert snap["user_source"]["state"] == "open"


def test_circuit_recovery_on_open_expiry(isolated):
    lxapp._CHAIN_HEALTH["user_source"] = {"fails": 0, "open_until": time.time() - 1, "breaks": 1}
    isolated._runtime = FakeRuntime()

    async def run():
        http = mock_client(_media_handler())
        try:
            return await lxapp.resolve_and_probe(
                http, "kw", {"id": "lx:kw:9", "title": "t", "duration_s": 200}
            )
        finally:
            await http.aclose()

    import asyncio

    result = asyncio.run(run())
    assert result is not None and result["resolver"] == "user_source"
    snap = lxapp.chain_health_snapshot()
    assert snap["user_source"]["state"] == "closed"  # half_open 试探成功后闭合


# ------------------------------------------------------------------ healthz / source 端点 ---

def test_healthz_reports_user_source(isolated):
    isolated._runtime = FakeRuntime(platforms={"kw": ["128k", "320k", "flac"]})
    with TestClient(lxapp.app) as client:
        rj = client.get("/healthz").json()
    assert rj["ok"] is True
    assert rj["version"] == "2.0.0"
    assert rj["user_source"]["initialized"] is True
    assert rj["user_source"]["source"]["platforms"]["kw"]["qualitys"] == ["128k", "320k", "flac"]
    assert rj["capabilities"]["kw"]["playback_available"] is True
    assert "user_source" in rj["circuit"]
    assert rj["charts"] == ["kg", "kw", "wy"]


def test_source_endpoints_manage(isolated, tmp_path):
    isolated.state_path = tmp_path / "state.json"
    isolated.script_cache = tmp_path / "source.js"

    with TestClient(lxapp.app) as client:
        r = client.get("/api/v1/source")
        assert r.status_code == 200
        assert r.json()["data"]["configured"] is False
        assert "circuit" in r.json()

        # 非法协议
        r = client.post("/api/v1/source", json={"url": "ftp://bad"})
        assert r.status_code == 400

        # 激活失败：分类错误返回 400
        r = client.post("/api/v1/source", json={"url": "https://bad-source/1.js"})
        assert r.status_code == 400
        body = r.json()
        assert body["ok"] is False
        assert body["category"] == "download"

        # 激活成功
        r = client.post("/api/v1/source", json={"url": "https://src.test/good.js"})
        assert r.status_code == 200
        assert r.json()["data"]["url"] == "https://src.test/good.js"

        # 停用并清除持久化状态
        isolated.state_path.write_text("{}", encoding="utf-8")
        isolated.script_cache.write_text("/*x*/", encoding="utf-8")
        r = client.delete("/api/v1/source")
        assert r.status_code == 200
        assert not isolated.state_path.exists()
        assert not isolated.script_cache.exists()
        assert isolated.shut_down == 1


def test_source_verify_endpoint(isolated, monkeypatch):
    calls = {}

    async def fake_verify(url):
        calls["url"] = url
        return {"ok": True, "url": url, "category": "", "message": "",
                "meta": {"name": "x"}, "platforms": ["kw"]}

    stub = types.ModuleType("verify_source")
    stub.verify_url = fake_verify
    monkeypatch.setitem(sys.modules, "verify_source", stub)

    with TestClient(lxapp.app) as client:
        r = client.post("/api/v1/source/verify", json={"url": "https://s/1.js"})
        assert r.status_code == 200
        rj = r.json()
        assert rj["ok"] is True
        assert rj["data"]["platforms"] == ["kw"]
        assert calls["url"] == "https://s/1.js"

        r2 = client.post("/api/v1/source/verify", json={"url": "notaurl"})
        assert r2.status_code == 400


# ------------------------------------------------------------------ 歌词 ---

def test_track_lyric_tx_base64_decode():
    lrc = "[00:01.00]晴天 - 周杰伦\n[00:05.30]故事的小黄花&#58;"
    encoded = _b64.b64encode(lrc.encode()).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("songmid") == "0039MnYb0qxYhV"
        return httpx.Response(200, json={"retcode": 0, "lyric": encoded})

    lxapp.app.state.http = mock_client(handler)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/lyric", params={"id": "lx:tx:0039MnYb0qxYhV"})
        assert resp.status_code == 200
        text = resp.json()["data"]["lyric"]
        assert text.startswith("[00:01.00]晴天 - 周杰伦")
        assert text.endswith("故事的小黄花:")  # &#58; → :


def test_track_lyric_kw_returns_empty():
    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/lyric", params={"id": "lx:kw:228908"})
        assert resp.status_code == 200
        assert resp.json()["data"]["lyric"] == ""


# ------------------------------------------------------------------ 榜单推荐 ---

def test_kg_chart_free_direct_and_vip_via_user_source(isolated):
    isolated._runtime = FakeRuntime(platforms={"kg": ["128k", "320k", "flac"]})

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "m.kugou.com/rank/info/" in url:
            assert request.url.params.get("page") == "1", "rank/info 不带 page 时 songs.list 为空"
            assert request.url.params.get("rankid") == "8888"
            return httpx.Response(
                200,
                json={
                    "songs": {
                        "list": [
                            {
                                "hash": "KGFREE1", "sqhash": "KGSQ1", "320hash": "KGHQ1",
                                "songname": "榜单歌A", "authors": [{"author_name": "歌手A", "author_id": 1}],
                                "duration": 210, "pay_type": 0, "price": 0, "pkg_price": 0,
                                "album_sizable_cover": "http://imge.kugou.com/stdmusic/{size}/a.jpg",
                                "sqfilesize": 25000000,
                            },
                            {
                                "hash": "VIPHASH", "songname": "榜单歌VIP",
                                "authors": [{"author_name": "歌手V", "author_id": 2}],
                                "duration": 200, "pay_type": 3,
                            },
                        ]
                    }
                },
            )
        if "media.test" in url:
            return httpx.Response(
                206,
                headers={"Content-Type": "audio/x-flac", "Content-Range": "bytes 0-1/28936190"},
                content=b"fLaC",
            )
        return httpx.Response(500)

    lxapp.app.state.http = mock_client(handler)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/recommend", params={"limit": 5, "sources": "kg"})
    assert resp.status_code == 200
    rj = resp.json()
    assert rj["ok"] is True
    assert rj["errors"] == {}
    assert [it["id"] for it in rj["items"]] == ["lx:kg:KGFREE1", "lx:kg:VIPHASH"]
    free, vip = rj["items"]
    assert free["duration_s"] == 210  # rank 接口秒制
    assert free["ext"] == "flac" and "{size}" not in free["cover_url"]
    assert vip["verified"] is True  # VIP 经用户源解析+探活
    assert vip["pay_type"] == 3


def test_wy_chart_vip_routes_to_probe(isolated):
    isolated._runtime = FakeRuntime(platforms={"wy": ["128k"]}, resolver=SourceError("resolve", "reject"))

    def handler(request: httpx.Request) -> httpx.Response:
        if "personalized/newsong" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "id": 1,
                            "song": {"id": "3425638996", "name": "新歌免费",
                                     "artists": [{"name": "歌手D", "id": 9}],
                                     "album": {"name": "专辑D", "picUrl": "http://img/d.jpg"},
                                     "duration": 211686, "fee": 0},
                        },
                        {
                            "id": 2,
                            "song": {"id": "3425638997", "name": "新歌VIP",
                                     "artists": [{"name": "歌手E", "id": 10}],
                                     "album": {"name": "专辑E", "picUrl": ""},
                                     "duration": 200000, "fee": 1},
                        },
                    ]
                },
            )
        return httpx.Response(500)

    lxapp.app.state.http = mock_client(handler)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/recommend", params={"limit": 5, "sources": "wy"})
    assert resp.status_code == 200
    rj = resp.json()
    assert [it["id"] for it in rj["items"]] == ["lx:wy:3425638996"]
    assert rj["items"][0]["duration_s"] == 211.686


def test_recommend_aggregates_with_early_stop(isolated):
    isolated._runtime = FakeRuntime(platforms={"kg": ["128k"], "wy": ["128k"]})
    calls = {"kg": 0, "wy": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "m.kugou.com/rank/info/" in url:
            calls["kg"] += 1
            rows = [
                {"hash": f"KGN{i}", "songname": f"聚合歌{i}",
                 "authors": [{"author_name": f"聚合歌手{i}", "author_id": i}],
                 "duration": 200, "pay_type": 0}
                for i in range(3)
            ]
            return httpx.Response(200, json={"songs": {"list": rows}})
        if "personalized/newsong" in url:
            calls["wy"] += 1
            return httpx.Response(200, json={"result": []})
        return httpx.Response(500)

    lxapp.app.state.http = mock_client(handler)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/recommend", params={"limit": 3, "sources": "kg,wy"})
    assert resp.status_code == 200
    rj = resp.json()
    assert len(rj["items"]) == 3
    assert calls == {"kg": 1, "wy": 0}  # kg 凑满后未再触达 wy


# ------------------------------------------------------------------ 探活器/纯函数 ---

def test_probe_url_rejects_html():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "html.test" in url:
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text="<html>404</html>")
        if "audio.test" in url:
            return httpx.Response(
                206,
                headers={"Content-Type": "application/octet-stream", "Content-Range": "bytes 0-1/1000"},
                content=b"ID3",
            )
        if "redir.test" in url:
            return httpx.Response(302, headers={"Location": "https://audio.test/x.mp3"})
        return httpx.Response(500)

    import asyncio

    async def run():
        http = mock_client(handler)
        try:
            ok_html, _, _, _ = await lxapp.probe_url(http, "https://html.test/x")
            ok_audio, final, ct, size = await lxapp.probe_url(http, "https://audio.test/x.mp3")
            ok_redir, final_r, _, _ = await lxapp.probe_url(http, "https://redir.test/x")
            return ok_html, ok_audio, final, ct, size, ok_redir, final_r
        finally:
            await http.aclose()

    ok_html, ok_audio, final, ct, size, ok_redir, final_r = asyncio.run(run())
    assert ok_html is False
    assert ok_audio is True and size == 1000
    assert ok_redir is True and final_r.endswith("x.mp3")


def test_fresh_probe_tier_and_expiry():
    base = {"url": "https://cdn.test/a.flac", "ext": "flac", "headers": {},
            "probed": True, "validation_status": "media_verified"}
    # standard 缓存 → lossless 请求拒绝（需重新解析高音质）
    item = {"_probe": dict(base, ts=time.time(), actual_tier="standard")}
    assert lxapp._fresh_probe(item, "lossless") is None
    # 降档完成后可复用
    item = {"_probe": dict(base, ts=time.time(), actual_tier="standard",
                           attempted_tiers=["lossless", "high", "standard"])}
    assert lxapp._fresh_probe(item, "lossless") is not None
    # standard 缓存 → standard 请求复用
    got = lxapp._fresh_probe({"_probe": dict(base, ts=time.time(), actual_tier="standard")}, "standard")
    assert got and got["url"].endswith(".flac") and "ts" not in got
    # lossless 缓存 → standard 请求也可复用（音质只高不低）
    item = {"_probe": dict(base, ts=time.time(), actual_tier="lossless")}
    assert lxapp._fresh_probe(item, "standard") is not None
    # 过期缓存拒绝
    item = {"_probe": dict(base, ts=time.time() - lxapp.CONF["probe_fresh_s"] - 1, actual_tier="lossless")}
    assert lxapp._fresh_probe(item, "lossless") is None
    # 无缓存 / 非 dict / 试听
    assert lxapp._fresh_probe({}, "standard") is None
    assert lxapp._fresh_probe(None, "standard") is None
    assert lxapp._fresh_probe({"_probe": dict(base, ts=time.time(), actual_tier="lossless"),
                               "trial": "1"}, "standard") is None


def test_title_relevance_ranking():
    r = lxapp._title_relevance
    assert r("晴天", "晴天 周杰伦") == 1  # 原版
    assert r("晴天 (KTV版伴奏)", "晴天 周杰伦") == 1
    assert r("晴天周杰伦串烧版", "晴天 周杰伦") == 1
    assert r("超好听晴天周杰伦remix", "晴天 周杰伦") == 2
    assert r("志明与春娇+晴天+双截棍", "晴天 周杰伦") == 3
    assert r("花海", "晴天 周杰伦") == 3
    assert r("晴天", "晴天") == 0
    assert r("任意", "") == 3


def test_lenient_pydict_parses_python_literal():
    class _FakeResp:
        text = "{'abslist':[{'MUSICRID':'MUSIC_1','SONGNAME':'A'}]}"

        def json(self):
            raise ValueError("not json")

    parsed = lxapp._lenient_pydict(_FakeResp(), "kw")
    assert parsed == {"abslist": [{"MUSICRID": "MUSIC_1", "SONGNAME": "A"}]}
