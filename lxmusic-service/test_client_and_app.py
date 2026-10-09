"""lxmusic-service/test_client_and_app.py
全套契约与单元测试：覆盖字段映射、探活签名、API 契约和容错机制。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from conftest import lxapp, mock_client
from lxserver_client import (
    extract_track_identifier,
    map_lxserver_song,
    normalize_source,
    parse_interval_to_seconds,
)


# ------------------------------------------------------------- 映射函数单测 --

def test_normalize_source():
    assert normalize_source("kugou") == "kg"
    assert normalize_source("KG") == "kg"
    assert normalize_source("163") == "wy"
    assert normalize_source("netease") == "wy"
    assert normalize_source("kuwo") == "kw"
    assert normalize_source("migu") == "mg"
    assert normalize_source("qq") == "tx"
    assert normalize_source("unknown") == ""
    assert normalize_source(None) == ""


def test_parse_interval_to_seconds():
    assert parse_interval_to_seconds("05:24") == 324
    assert parse_interval_to_seconds("00:45") == 45
    assert parse_interval_to_seconds("01:02:03") == 3723
    assert parse_interval_to_seconds(180) == 180
    assert parse_interval_to_seconds(180.5) == 180
    assert parse_interval_to_seconds("") == 0
    assert parse_interval_to_seconds(None) == 0


def test_map_lxserver_song_kuwo():
    raw = {
        "name": "海阔天空",
        "singer": "Beyond",
        "source": "kw",
        "songmid": "5886682",
        "albumName": "乐与怒",
        "interval": "05:24",
        "img": "https://img.test/pic.jpg",
        "types": [{"type": "128k"}, {"type": "320k"}, {"type": "flac"}],
    }
    item = map_lxserver_song(raw)
    assert item["id"] == "lx:kw:5886682"
    assert item["lx_source"] == "kw"
    assert item["title"] == "海阔天空"
    assert item["artist"] == "Beyond"
    assert item["album"] == "乐与怒"
    assert item["duration_s"] == 324
    assert item["ext"] == "flac"
    assert item["cover_url"] == "https://img.test/pic.jpg"
    assert "flac" in item["types"]


def test_map_lxserver_song_kugou_hash():
    raw = {
        "name": "泡沫",
        "singer": "邓紫棋",
        "source": "kg",
        "hash": "F52899ABCDEF",
        "albumName": "X.P.X",
        "interval": "04:18",
        "types": [{"type": "320k", "hash": "F52899ABCDEF"}],
    }
    item = map_lxserver_song(raw)
    assert item["id"] == "lx:kg:f52899abcdef"
    assert item["hash"] == "f52899abcdef"
    assert item["ext"] == "mp3"


def test_map_lxserver_song_migu_copyright():
    raw = {
        "name": "稻香",
        "singer": "周杰伦",
        "source": "mg",
        "copyrightId": "60054701983",
        "interval": 223,
    }
    item = map_lxserver_song(raw)
    assert item["id"] == "lx:mg:60054701983"
    assert item["copyrightId"] == "60054701983"
    assert item["duration_s"] == 223


# ------------------------------------------------------------- 探活与魔数单测 --

def test_media_signature():
    assert lxapp._media_signature(b"fLaC\x00\x00") == "flac"
    assert lxapp._media_signature(b"ID3\x04\x00") == "mp3"
    assert lxapp._media_signature(b"OggS\x00\x02") == "ogg"
    assert lxapp._media_signature(b"RIFF\x00\x00\x00\x00WAVE") == "wav"
    assert lxapp._media_signature(b"\x00\x00\x00 ftypM4A ") == "m4a"
    assert lxapp._media_signature(b"\xff\xf1\x50\x80") == "aac"
    assert lxapp._media_signature(b"<html>error</html>") == ""


@pytest.mark.asyncio
async def test_probe_url_flac_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            206,
            headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-4095/30000000"},
            content=b"fLaC" + b"\x00" * 4092,
        )

    client = mock_client(handler)
    try:
        ok, final_url, ct, size = await lxapp.probe_url(client, "https://media.test/track.flac")
        assert ok is True
        assert ct == "audio/flac"
        assert size == 30000000
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_probe_url_html_rejection():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"<html>Denied</html>")

    client = mock_client(handler)
    try:
        ok, _, _, _ = await lxapp.probe_url(client, "https://media.test/track.mp3")
        assert ok is False
    finally:
        await client.aclose()


# ------------------------------------------------------------- API 契约测试 --

def test_healthz_endpoint(test_app_client):
    res = test_app_client.get("/healthz")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["service"] == "fnmusic-lxmusic"
    assert "capabilities" in data
    assert "user_source" in data
    assert "charts" in data
    assert data["user_source"]["configured"] is True


def test_search_endpoint(test_app_client):
    res = test_app_client.get("/api/v1/search?q=海阔天空&sources=kw&limit=10")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert isinstance(data["items"], list)
    assert len(data["items"]) > 0
    item = data["items"][0]
    assert item["id"] == "lx:kw:5886682"
    assert item["title"] == "海阔天空"
    assert item["artist"] == "Beyond"


def test_recommend_endpoint(test_app_client):
    res = test_app_client.get("/api/v1/recommend?sources=kw&limit=5")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert isinstance(data["items"], list)


def test_track_info_endpoint(test_app_client):
    # 未缓存条目返回合成数据
    res = test_app_client.get("/api/v1/track/info?id=lx:kw:5886682")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["data"]["id"] == "lx:kw:5886682"
    assert data["data"]["lx_source"] == "kw"


def test_track_lyric_endpoint(test_app_client):
    res = test_app_client.get("/api/v1/track/lyric?id=lx:kw:5886682")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert "[00:00.00]" in data["data"]["lyric"]


def test_source_management_endpoints(test_app_client, fake_lx):
    # GET /api/v1/source
    res = test_app_client.get("/api/v1/source")
    assert res.status_code == 200
    assert res.json()["ok"] is True

    # POST /api/v1/source/upload —— 返回 lxserver 真实存储路径（id 由 @name 派生）
    res = test_app_client.post(
        "/api/v1/source/upload",
        json={"filename": "my_src.js", "script": "/*!\n * @name MyTest\n */\nconsole.log(1);"},
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True
    data = res.json()["data"]
    assert data["url"] == "file:///data/lxserver/users/source/_open/MyTest.js"
    assert data["meta"]["name"] == "MyTest"

    # 同一脚本重复上传：复用既有源，不报错（多源列表的常规操作）
    res = test_app_client.post(
        "/api/v1/source/upload",
        json={"filename": "my_src.js", "script": "/*!\n * @name MyTest\n */\nconsole.log(1);"},
    )
    assert res.status_code == 200
    assert res.json()["data"]["url"] == data["url"]

    # POST /api/v1/source（激活上传返回的 file:// URL）
    res = test_app_client.post("/api/v1/source", json={"url": data["url"]})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    # 单源语义：目标启用、其余禁用
    states = {s["id"]: s["enabled"] for s in fake_lx.sources}
    assert states.get("MyTest.js") is True
    assert states.get("source1") is False

    # DELETE /api/v1/source (清除)
    res = test_app_client.delete("/api/v1/source")
    assert res.status_code == 200
    assert res.json()["ok"] is True


def test_source_set_activates_unknown_local_file_by_import(test_app_client, fake_lx, tmp_path):
    """旧数据卷路径（如 /data/lxmusic/uploads）不在 lxserver 列表时自动补导入再激活。"""
    script_path = tmp_path / "legacy_src.js"
    script_path.write_text("/*!\n * @name LegacySrc\n */\nconsole.log(1);", encoding="utf-8")
    res = test_app_client.post("/api/v1/source", json={"url": f"file://{script_path}"})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    # 已按派生 id 导入并激活
    states = {s["id"]: s["enabled"] for s in fake_lx.sources}
    assert states.get("LegacySrc.js") is True


def test_source_set_rejects_missing_source(test_app_client):
    res = test_app_client.post("/api/v1/source", json={"url": "file:///nonexistent/nope.js"})
    assert res.status_code == 500
    assert "未找到要激活的源" in res.json()["error"]


def test_lxserver_upload_sends_json_contract():
    """上传必须发 JSON {filename, content}（lxserver 端点 JSON.parse，multipart 会被拒）。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"success": True, "id": "x.js", "metadata": {"name": "x"}})

    from lxserver_client import LxServerClient

    client = LxServerClient(base_url="http://test", admin_password="pw")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    res = asyncio.run(client.upload_custom_source("x.js", "console.log(1)"))
    assert captured["content_type"].startswith("application/json")
    assert captured["body"] == {"filename": "x.js", "content": "console.log(1)"}
    assert res["id"] == "x.js"


def test_lxserver_admin_resp_semantics():
    """管理端点失败语义：success=false / HTTP 500 抛 RuntimeError 带服务端文案。"""
    from lxserver_client import LxServerClient

    def fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "error": '源 "x" 已存在于 [open]'})

    client = LxServerClient(base_url="http://test", admin_password="pw")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(fail_handler), base_url="http://test")
    with pytest.raises(RuntimeError, match="已存在"):
        asyncio.run(client.upload_custom_source("x.js", "s"))

    def server_error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"success": False, "error": "boom"})

    client2 = LxServerClient(base_url="http://test", admin_password="pw")
    client2._client = httpx.AsyncClient(transport=httpx.MockTransport(server_error_handler), base_url="http://test")
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(client2.toggle_custom_source("x.js", True))


def test_lxserver_toggle_sends_enabled_key():
    """toggle 必须用 enabled 键（lxserver 端点不认识 enable，会退化成布尔翻转）。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"success": True, "enabled": True})

    from lxserver_client import LxServerClient

    client = LxServerClient(base_url="http://test", admin_password="pw")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    assert asyncio.run(client.toggle_custom_source("MyTest.js", True)) is True
    assert captured["body"] == {"id": "MyTest.js", "enabled": True}


def test_track_url_resolution(test_app_client):
    # 模拟探测成功的直链返回
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            206,
            headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-4095/30000000"},
            content=b"fLaC" + b"\x00" * 4092,
        )

    # 替换全局 client 进行探活
    orig_client = lxapp.app.state.client
    lxapp.app.state.client = mock_client(handler)
    try:
        res = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless")
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["data"]["id"] == "lx:kw:5886682"
        assert data["data"]["ext"] == "flac"
        assert data["data"]["actual_tier"] == "lossless"
        assert "headers" in data["data"]
    finally:
        lxapp.app.state.client = orig_client


def test_verify_source_json_format_guard():
    from verify_source import _looks_like_json_source

    assert _looks_like_json_source('{"api": "https://test.com"}') is True
    assert _looks_like_json_source('[{"api": 1}]') is True
    assert _looks_like_json_source("/* @name test */ console.log(1);") is False


def test_source_set_http_url_activates_target(test_app_client, fake_lx):
    """测试通过 HTTP URL 设置源，能够正确导入并激活该源（单源生效）。"""
    res = test_app_client.post("/api/v1/source", json={"url": "https://example.com/remote_source.js"})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert "https://example.com/remote_source.js" in fake_lx.import_calls
    # 验证目标源已被激活，旧源已被禁用
    states = {s["id"]: s["enabled"] for s in fake_lx.sources}
    assert states.get("remote_source.js") is True
    assert states.get("source1") is False


def test_activate_nonexistent_source_does_not_disable_existing():
    """激活不存在的目标源时，不应当误将当前运行中的源全部禁用。"""
    from lxserver_client import LxServerClient

    client = LxServerClient(base_url="http://test")
    # 模拟已有激活源
    sources = [
        {"id": "source1", "name": "source1", "enabled": True},
        {"id": "source2", "name": "source2", "enabled": False},
    ]
    toggled = []

    async def fake_list():
        return list(sources)

    async def fake_toggle(sid, enable):
        toggled.append((sid, enable))
        return True

    client.list_custom_sources = fake_list
    client.toggle_custom_source = fake_toggle

    ok = asyncio.run(client.activate_single_source("non_existent_source"))
    assert ok is False
    # 没有任何 toggle 被执行，旧源保持原样
    assert toggled == []


def test_source_capabilities_reflects_failed_status():
    """当激活源状态为 failed 时，playback_available 应为 False 并给出错误原因。"""
    from app import source_capabilities
    import app as lxapp

    orig_lx = lxapp.LXSERVER
    try:
        class FailedClient:
            async def is_alive(self):
                return True

            async def list_custom_sources(self):
                return [{
                    "id": "bad.js",
                    "name": "bad",
                    "enabled": True,
                    "status": "failed",
                    "error": "script crashed",
                    "supportedSources": ["kw"],
                }]

        lxapp.LXSERVER = FailedClient()
        caps = asyncio.run(source_capabilities())
        for plat, info in caps.items():
            assert info["playback_available"] is False
            assert "failed" in info["reason"]
    finally:
        lxapp.LXSERVER = orig_lx


# ------------------------------------------------- 播放直链缓存与并发合并 --
# issue #45：同曲同音质重复解析治理——成功缓存、失败负缓存、探活续期、
# 同键在途合并、fresh 旁路与切源失效。

def _flac_probe_handler(request: httpx.Request) -> httpx.Response:
    """探活 mock：dead 链接模拟直链过期后 CDN 拒绝，其余返回合法 flac 前缀。"""
    if "dead" in str(request.url):
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"<html>gone</html>")
    return httpx.Response(
        206,
        headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-4095/30000000"},
        content=b"fLaC" + b"\x00" * 4092,
    )


@pytest.fixture
def fresh_url_cache():
    """隔离模块级直链缓存/在途/熔断状态。"""
    lxapp._URL_CACHE.clear()
    lxapp._URL_INFLIGHT.clear()
    lxapp._CHAIN_HEALTH.pop("user_source", None)
    yield
    lxapp._URL_CACHE.clear()
    lxapp._URL_INFLIGHT.clear()


def test_track_url_success_cache_reuses_resolution(test_app_client, fresh_url_cache, fake_lx):
    """同曲同音质连续请求：第二次命中缓存，音源仅解析一次（issue #45 主诉求）。"""
    orig_client = lxapp.app.state.client
    lxapp.app.state.client = mock_client(_flac_probe_handler)
    try:
        r1 = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless")
        r2 = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless")
    finally:
        lxapp.app.state.client = orig_client
    assert r1.status_code == 200 and r2.status_code == 200
    assert len(fake_lx.url_calls) == 1
    assert r1.json()["data"]["url"] == r2.json()["data"]["url"] == "https://media.test/song.flac"


@pytest.mark.asyncio
async def test_track_url_concurrent_requests_merge_inflight(fake_lx, fresh_url_cache):
    """同键并发请求共享一次实际解析（在途 Future 合并），不放大音源调用量。"""
    gate = asyncio.Event()
    fake_lx.url_gate = gate
    probe_client = mock_client(_flac_probe_handler)
    item = {"id": "lx:kw:5886682", "lx_source": "kw"}
    leader = asyncio.create_task(lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0))
    while not fake_lx.url_calls:
        await asyncio.sleep(0.01)
    followers = [
        asyncio.create_task(lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0))
        for _ in range(2)
    ]
    await asyncio.sleep(0.05)
    assert len(fake_lx.url_calls) == 1  # 等待期间未新增解析
    gate.set()
    results = await asyncio.gather(leader, *followers)
    assert len(fake_lx.url_calls) == 1
    assert all(r["url"] == results[0]["url"] for r in results)
    assert lxapp._URL_INFLIGHT == {}
    await probe_client.aclose()


@pytest.mark.asyncio
async def test_track_url_ttl_expiry_renews_via_probe(fake_lx, fresh_url_cache):
    """TTL 到期先探活旧链续期（零额度消耗），不重新解析；续期后缓存刷新继续命中。"""
    probe_client = mock_client(_flac_probe_handler)
    item = {"id": "lx:kw:5886682", "lx_source": "kw"}
    key = ("lx:kw:5886682", "lossless")
    r1 = await lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0)
    assert len(fake_lx.url_calls) == 1
    lxapp._URL_CACHE[key]["ts"] -= lxapp.CONF["url_cache_ttl"] + 1.0
    r2 = await lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0)
    assert len(fake_lx.url_calls) == 1  # 旧链探活通过 → 续期，不消耗解析额度
    assert r2["url"] == r1["url"]
    r3 = await lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0)
    assert len(fake_lx.url_calls) == 1
    assert r3["url"] == r1["url"]
    await probe_client.aclose()


@pytest.mark.asyncio
async def test_track_url_ttl_expiry_dead_url_reresolves(fake_lx, fresh_url_cache):
    """TTL 到期且旧链探活失败：重新解析并覆盖缓存。"""
    probe_client = mock_client(_flac_probe_handler)
    item = {"id": "lx:kw:5886682", "lx_source": "kw"}
    key = ("lx:kw:5886682", "lossless")
    r1 = await lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0)
    assert len(fake_lx.url_calls) == 1
    entry = lxapp._URL_CACHE[key]
    entry["data"] = {**entry["data"], "url": "https://media.test/dead.flac"}
    entry["ts"] -= lxapp.CONF["url_cache_ttl"] + 1.0
    r2 = await lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0)
    assert len(fake_lx.url_calls) == 2
    assert r2["url"] == "https://media.test/song.flac"
    await probe_client.aclose()


def test_track_url_negative_cache_and_fresh_bypass(test_app_client, fresh_url_cache, fake_lx):
    """解析失败写负缓存：短时间内重复请求不再打音源；fresh=1 旁路强制重新解析。

    一次失败解析会走完整音质阶梯（flac→320k→128k 共 3 次调低档调用）。"""
    fake_lx.url_result = None
    r1 = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless")
    r2 = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless")
    assert r1.status_code == 404 and r2.status_code == 404
    assert len(fake_lx.url_calls) == 3  # 第二次请求负缓存命中，未再走阶梯

    r3 = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless&fresh=1")
    assert r3.status_code == 404
    assert len(fake_lx.url_calls) == 6  # fresh 旁路负缓存，重新走一遍阶梯

    # 音源恢复后 fresh 重取成功并覆盖负缓存，后续请求恢复命中
    fake_lx.url_result = {"url": "https://media.test/song.flac", "type": "flac", "sourceName": "test"}
    orig_client = lxapp.app.state.client
    lxapp.app.state.client = mock_client(_flac_probe_handler)
    try:
        r4 = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless&fresh=1")
        assert r4.status_code == 200
        r5 = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless")
        assert r5.status_code == 200
    finally:
        lxapp.app.state.client = orig_client
    assert len(fake_lx.url_calls) == 7  # 成功解析在首档即命中，仅 +1


def test_track_url_quality_isolation(test_app_client, fresh_url_cache, fake_lx):
    """不同音质档各自解析与缓存，同音质命中缓存（音质变化不串缓存）。"""
    orig_client = lxapp.app.state.client
    lxapp.app.state.client = mock_client(_flac_probe_handler)
    try:
        assert test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless").status_code == 200
        assert test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless").status_code == 200
        assert test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=high").status_code == 200
        assert test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=high").status_code == 200
    finally:
        lxapp.app.state.client = orig_client
    assert [q for _, q in fake_lx.url_calls] == ["flac", "320k"]


def test_track_url_cache_cleared_on_source_change(test_app_client, fresh_url_cache, fake_lx):
    """切换音源后直链缓存清空：后续请求重新解析，杜绝串源。"""
    orig_client = lxapp.app.state.client
    lxapp.app.state.client = mock_client(_flac_probe_handler)
    try:
        assert test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless").status_code == 200
        assert len(fake_lx.url_calls) == 1
        res = test_app_client.post("/api/v1/source", json={"url": "file:///data/lxserver/users/source/_open/source1"})
        assert res.status_code == 200 and res.json()["ok"] is True
        assert lxapp._URL_CACHE == {}
        assert test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless").status_code == 200
        assert len(fake_lx.url_calls) == 2
    finally:
        lxapp.app.state.client = orig_client


@pytest.mark.asyncio
async def test_url_inflight_leader_failure_cleans_up(monkeypatch, fresh_url_cache):
    """领头解析异常传播给等待者，在途状态清理且不写缓存（含负缓存），随后可重试。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def flaky_resolve(client, src, item, quality="lossless", budget=20.0):
        started.set()
        await release.wait()
        raise RuntimeError("boom")

    monkeypatch.setattr(lxapp, "resolve_and_probe", flaky_resolve)
    probe_client = mock_client(_flac_probe_handler)
    item = {"id": "lx:kw:5886682", "lx_source": "kw"}
    leader = asyncio.create_task(lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0))
    await started.wait()
    follower = asyncio.create_task(lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0))
    await asyncio.sleep(0.05)
    release.set()
    with pytest.raises(RuntimeError, match="boom"):
        await leader
    with pytest.raises(RuntimeError, match="boom"):
        await follower
    assert lxapp._URL_INFLIGHT == {}
    assert lxapp._URL_CACHE == {}

    async def ok_resolve(client, src, item, quality="lossless", budget=20.0):
        return {"id": item.get("id"), "url": "https://media.test/song.flac"}

    monkeypatch.setattr(lxapp, "resolve_and_probe", ok_resolve)
    r = await lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0)
    assert r["url"] == "https://media.test/song.flac"
    await probe_client.aclose()


