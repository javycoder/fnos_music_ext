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
    # 多源叠加语义：目标启用、既有激活源保持不变
    states = {s["id"]: s["enabled"] for s in fake_lx.sources}
    assert states.get("MyTest.js") is True
    assert states.get("source1") is True

    # enabled=false：停用目标源，其余不受影响
    res = test_app_client.post("/api/v1/source", json={"url": data["url"], "enabled": False})
    assert res.status_code == 200
    states = {s["id"]: s["enabled"] for s in fake_lx.sources}
    assert states.get("MyTest.js") is False
    assert states.get("source1") is True

    # describe 返回全部启用源列表
    res = test_app_client.get("/api/v1/source")
    src_data = res.json()["data"]
    assert src_data["configured"] is True
    assert src_data["active_count"] == 1
    assert [s["id"] for s in src_data["sources"]] == ["source1"]

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
    """测试通过 HTTP URL 设置源，能够正确导入并激活该源（多源叠加，不动既有源）。"""
    res = test_app_client.post("/api/v1/source", json={"url": "https://example.com/remote_source.js"})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert "https://example.com/remote_source.js" in fake_lx.import_calls
    # 验证目标源已被激活，旧激活源保持不变
    states = {s["id"]: s["enabled"] for s in fake_lx.sources}
    assert states.get("remote_source.js") is True
    assert states.get("source1") is True


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

    ok = asyncio.run(client.set_source_enabled("non_existent_source", True))
    assert ok is False
    # 没有任何 toggle 被执行，旧源保持原样
    assert toggled == []


def test_set_source_enabled_overlay_semantics():
    """set_source_enabled 只改目标源启用态，多源可同时启用。"""
    from lxserver_client import LxServerClient

    client = LxServerClient(base_url="http://test")
    sources = [
        {"id": "source1", "name": "source1", "enabled": True},
        {"id": "source2", "name": "source2", "enabled": False},
    ]
    toggled = []

    async def fake_list():
        return list(sources)

    async def fake_toggle(sid, enable):
        toggled.append((sid, enable))
        for s in sources:
            if s["id"] == sid:
                s["enabled"] = enable
        return True

    client.list_custom_sources = fake_list
    client.toggle_custom_source = fake_toggle

    assert asyncio.run(client.set_source_enabled("source2", True)) is True
    assert toggled == [("source2", True)]
    assert {s["id"]: s["enabled"] for s in sources} == {"source1": True, "source2": True}

    # 支持按名称匹配
    assert asyncio.run(client.set_source_enabled("source1", False)) is True
    assert toggled == [("source2", True), ("source1", False)]


def test_source_capabilities_union_of_enabled_sources():
    """多源同时激活时 capabilities 按启用源平台并集判定。"""
    from app import source_capabilities
    import app as lxapp

    orig_lx = lxapp.LXSERVER
    try:
        class MultiClient:
            async def is_alive(self):
                return True

            async def list_custom_sources(self):
                return [
                    {"id": "a.js", "name": "a", "enabled": True, "supportedSources": ["kw", "kg"]},
                    {"id": "b.js", "name": "b", "enabled": True, "supportedSources": ["wy", "tx"]},
                    {"id": "c.js", "name": "c", "enabled": False, "supportedSources": ["mg"]},
                ]

        lxapp.LXSERVER = MultiClient()
        caps = asyncio.run(source_capabilities())
        assert caps["kw"]["playback_available"] is True
        assert caps["kg"]["playback_available"] is True
        assert caps["wy"]["playback_available"] is True
        assert caps["tx"]["playback_available"] is True
        assert caps["mg"]["playback_available"] is False
        assert "not supported" in caps["mg"]["reason"]
    finally:
        lxapp.LXSERVER = orig_lx


def test_describe_user_source_lists_all_enabled():
    """describe 返回全部启用源（sources 数组 + active_count），兼容字段取第一个。"""
    from app import describe_user_source
    import app as lxapp

    orig_lx = lxapp.LXSERVER
    try:
        class MultiClient:
            async def list_custom_sources(self):
                return [
                    {"id": "a.js", "name": "src-a", "enabled": True, "version": "1.0",
                     "supportedSources": ["kw", "kg"]},
                    {"id": "b.js", "name": "src-b", "enabled": True, "version": "2.0",
                     "supportedSources": ["wy"]},
                    {"id": "c.js", "name": "src-c", "enabled": False, "version": "1.0",
                     "supportedSources": ["mg"]},
                ]

        lxapp.LXSERVER = MultiClient()
        desc = asyncio.run(describe_user_source())
        assert desc["configured"] is True
        assert desc["active_count"] == 2
        assert [s["id"] for s in desc["sources"]] == ["a.js", "b.js"]
        assert [s["platforms"] for s in desc["sources"]] == [["kw", "kg"], ["wy"]]
        assert desc["source"]["name"] == "src-a"
    finally:
        lxapp.LXSERVER = orig_lx


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



def test_env_active_source_urls_parsing(monkeypatch):
    """LX_SOURCE_LIST 环境变量解析：仅取 active 标记项的 URL，非法输入返回空。"""
    from app import _env_active_source_urls

    monkeypatch.setenv("LX_SOURCE_LIST", json.dumps([
        {"name": "a", "url": "https://s/a.js", "active": True},
        {"name": "b", "url": "https://s/b.js", "active": False},
        {"name": "c", "url": "https://s/c.js", "active": True},
        {"name": "d", "url": "", "active": True},
    ]))
    assert _env_active_source_urls() == ["https://s/a.js", "https://s/c.js"]

    monkeypatch.setenv("LX_SOURCE_LIST", "not-json")
    assert _env_active_source_urls() == []
    monkeypatch.setenv("LX_SOURCE_LIST", "")
    assert _env_active_source_urls() == []
    monkeypatch.delenv("LX_SOURCE_LIST", raising=False)
    assert _env_active_source_urls() == []


def test_bootstrap_migration_restores_multi_active_sources(tmp_path, monkeypatch):
    """lxserver 无启用源（数据卷被清）时，按 LX_SOURCE_LIST 的 active 标记叠加恢复多源。"""
    from conftest import FakeLxServerClient

    fake = FakeLxServerClient()
    for s in fake.sources:
        s["enabled"] = False
    orig_lx = lxapp.LXSERVER
    monkeypatch.setattr(lxapp, "LXSERVER", fake)
    monkeypatch.setenv("LX_DATA_DIR", str(tmp_path))  # 无历史 uploads/state.json
    # 隔离仓库根目录可能存在的 .env（_persisted_source_targets 磁盘优先，
    # 命中真实安装配置会让用例测不到环境变量回退路径）
    monkeypatch.setenv("LX_ENV_PATH", str(tmp_path / "absent.env"))
    monkeypatch.setenv("LX_SOURCE_LIST", json.dumps([
        {"name": "a", "url": "https://s/a.js", "active": True},
        {"name": "b", "url": "https://s/b.js", "active": False},
    ]))
    monkeypatch.delenv("LX_SOURCE_URL", raising=False)
    try:
        asyncio.run(lxapp._bootstrap_migration())
    finally:
        lxapp.LXSERVER = orig_lx
    states = {s["id"]: s["enabled"] for s in fake.sources}
    # a.js 经导入 + 激活；既有 source1 保持停用（叠加语义不误开）
    assert states.get("a.js") is True
    assert states.get("source1") is False


def test_bootstrap_migration_falls_back_to_single_url(tmp_path, monkeypatch):
    """旧数据兼容：LX_SOURCE_LIST 无 active 标记时回退 LX_SOURCE_URL 单值激活。"""
    from conftest import FakeLxServerClient

    fake = FakeLxServerClient()
    for s in fake.sources:
        s["enabled"] = False
    orig_lx = lxapp.LXSERVER
    monkeypatch.setattr(lxapp, "LXSERVER", fake)
    monkeypatch.setenv("LX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LX_ENV_PATH", str(tmp_path / "absent.env"))
    monkeypatch.setenv("LX_SOURCE_LIST", json.dumps([
        {"name": "a", "url": "https://s/a.js", "active": False},
    ]))
    monkeypatch.setenv("LX_SOURCE_URL", "file:///x/source1")
    try:
        asyncio.run(lxapp._bootstrap_migration())
    finally:
        lxapp.LXSERVER = orig_lx
    states = {s["id"]: s["enabled"] for s in fake.sources}
    assert states.get("source1") is True
    assert "https://s/a.js" not in fake.import_calls
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
    lxapp._CHAIN_HEALTH.clear()
    yield
    lxapp._URL_CACHE.clear()
    lxapp._URL_INFLIGHT.clear()
    lxapp._CHAIN_HEALTH.clear()


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


@pytest.mark.asyncio
async def test_circuit_breaker_scoped_per_platform(fake_lx, fresh_url_cache):
    """平台级熔断：tx 连续失败只熔断 tx，kw 解析不受牵连。

    2026-10-10 回归：全局 user_source 熔断下 tx/mg 第三方源故障（tx 只产出加密
    mflac、mg 后端全挂）连累 kw/kg/wy 全平台 404，表现为"几乎所有洛雪曲目不可播"。"""
    probe_client = mock_client(_flac_probe_handler)
    fake_lx.url_result = None
    try:
        # 3 首不同 tx 曲目各失败一次（同曲会命中失败负缓存不再打音源）→ 触发 tx 平台熔断
        for i, songmid in enumerate(["001abc", "002def", "003ghi"]):
            r = await lxapp._resolve_url_cached(
                probe_client, "tx", {"id": f"lx:tx:{songmid}", "lx_source": "tx"}, "lossless", 20.0)
            assert r is None
        tx_calls = len(fake_lx.url_calls)
        assert tx_calls == 9  # 3 次解析 × 完整音质阶梯 3 档

        # 第 4 首 tx 曲目被平台熔断直接跳过：不再消耗音源调用
        r4 = await lxapp._resolve_url_cached(
            probe_client, "tx", {"id": "lx:tx:004jkl", "lx_source": "tx"}, "lossless", 20.0)
        assert r4 is None
        assert len(fake_lx.url_calls) == tx_calls

        # kw 不受 tx 熔断影响，正常解析成功
        fake_lx.url_result = {"url": "https://media.test/song.flac", "type": "flac", "sourceName": "test"}
        rw = await lxapp._resolve_url_cached(
            probe_client, "kw", {"id": "lx:kw:5886682", "lx_source": "kw"}, "lossless", 20.0)
        assert rw and rw["url"] == "https://media.test/song.flac"
        assert len(fake_lx.url_calls) == tx_calls + 1
    finally:
        await probe_client.aclose()
        fake_lx.url_script.clear()


@pytest.mark.asyncio
async def test_probe_reject_rotates_to_next_source(fake_lx, fresh_url_cache):
    """同档直链探活失败：excludeApiSources 排除产出坏链的脚本换下一个激活源重试。"""
    probe_client = mock_client(_flac_probe_handler)
    fake_lx.url_script = [
        {"url": "https://media.test/dead.flac", "type": "flac",
         "sourceName": "坏链源", "sourceId": "bad.js", "hasMoreSources": True},
        {"url": "https://media.test/song.flac", "type": "flac",
         "sourceName": "好链源", "sourceId": "good.js"},
    ]
    try:
        r = await lxapp._resolve_url_cached(
            probe_client, "kw", {"id": "lx:kw:5886682", "lx_source": "kw"}, "lossless", 20.0)
        assert r and r["url"] == "https://media.test/song.flac"
        # 第 2 次调用排除了第 1 个产出坏链的脚本（name 与 id 都传，lxserver 按二者过滤）
        assert fake_lx.url_excludes == [None, ["bad.js", "坏链源"]]
    finally:
        await probe_client.aclose()
        fake_lx.url_script.clear()


@pytest.mark.asyncio
async def test_probe_reject_no_more_sources_falls_to_next_tier(fake_lx, fresh_url_cache):
    """服务端确认无更多候选源（hasMoreSources=false）：同档不再轮换，直接降档。"""
    probe_client = mock_client(_flac_probe_handler)
    # 每档都返回探活失败且无更多源；脚本标识不重复计数（tried 已全在 excluded 时停止）
    fake_lx.url_script = [
        {"url": "https://media.test/dead.flac", "type": "flac",
         "sourceName": "唯一源", "sourceId": "only.js", "hasMoreSources": False},
        {"url": "https://media.test/song.flac", "type": "flac", "sourceName": "唯一源"},
    ]
    try:
        r = await lxapp._resolve_url_cached(
            probe_client, "wy", {"id": "lx:wy:1970560262", "lx_source": "wy"}, "lossless", 20.0)
        assert r and r["url"] == "https://media.test/song.flac"
        # lossless 档 1 次调用后即降档（无轮换），high 档成功：共 2 次
        assert [q for _, q in fake_lx.url_calls] == ["flac", "320k"]
    finally:
        await probe_client.aclose()
        fake_lx.url_script.clear()


@pytest.mark.asyncio
async def test_empty_url_response_rotates_to_next_source(fake_lx, fresh_url_cache):
    """脚本"成功"却拿不出直链（200 无 url，如平台未激活的静默空返回）：同档换源重试。"""
    probe_client = mock_client(_flac_probe_handler)
    fake_lx.url_script = [
        {"type": "flac", "sourceName": "空手源", "sourceId": "empty.js", "hasMoreSources": True},
        {"url": "https://media.test/song.flac", "type": "flac", "sourceName": "好链源"},
    ]
    try:
        r = await lxapp._resolve_url_cached(
            probe_client, "kg", {"id": "lx:kg:abc123", "lx_source": "kg"}, "lossless", 20.0)
        assert r and r["url"] == "https://media.test/song.flac"
        assert fake_lx.url_excludes == [None, ["empty.js", "空手源"]]
    finally:
        await probe_client.aclose()
        fake_lx.url_script.clear()


def test_chain_half_open_stuck_guard(fresh_url_cache):
    """半开试探超时未回填（解析被取消等）自动放行新试探，避免平台熔断被永久卡死。"""
    import time as _time
    lxapp._CHAIN_HEALTH["user_source:tx"] = {
        "fails": 5, "open_until": _time.time() + 600, "next_try": 0,
        "half_open": True, "half_open_since": _time.time() - 120,
    }
    # 超过一个试探周期：放行新的半开试探
    assert lxapp._chain_available("user_source:tx") is True
    h = lxapp._CHAIN_HEALTH["user_source:tx"]
    assert h["half_open"] is True and h["half_open_since"] > _time.time() - 5
    # 新试探在途期间（未超时）其他请求被挡
    assert lxapp._chain_available("user_source:tx") is False


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




@pytest.mark.asyncio
async def test_url_inflight_cancelled_leader_does_not_cancel_follower(monkeypatch, fresh_url_cache):
    started = asyncio.Event()
    async def resolve(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(lxapp, "resolve_and_probe", resolve)
    item = {"id": "lx:kw:cancel", "lx_source": "kw"}
    leader = asyncio.create_task(lxapp._resolve_url_cached(None, "kw", item, "lossless", 20))
    await started.wait()
    follower = asyncio.create_task(lxapp._resolve_url_cached(None, "kw", item, "lossless", 20))
    await asyncio.sleep(0)
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    with pytest.raises(RuntimeError, match="leader cancelled"):
        await follower
    assert not follower.cancelled()
    assert not lxapp._URL_INFLIGHT


def test_source_deactivate_filename_differs_from_name(test_app_client, fake_lx, tmp_path, monkeypatch):
    script = tmp_path / "local.js"
    script.write_text("/**\n * @name Real Source\n */")
    calls = []
    async def enabled(source_id, value):
        calls.append((source_id, value))
        return source_id == "real-id"
    async def find(**kwargs):
        return {"id": "real-id"} if kwargs.get("by_name") == "Real Source" else None
    monkeypatch.setattr(lxapp.LXSERVER, "set_source_enabled", enabled)
    monkeypatch.setattr(lxapp, "_find_lxserver_source", find)
    r = test_app_client.post("/api/v1/source", json={"url": script.as_uri(), "enabled": False})
    assert r.status_code == 200, r.text
    assert ("real-id", False) in calls


@pytest.mark.asyncio
async def test_url_inflight_isolated_across_source_generations(monkeypatch, fresh_url_cache):
    started = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    calls = []
    async def resolve(*args, **kwargs):
        n = len(calls)
        calls.append(lxapp._URL_CACHE_GEN)
        started[n].set()
        await release[n].wait()
        return {"url": f"https://media.test/gen-{calls[n]}"}
    monkeypatch.setattr(lxapp, "resolve_and_probe", resolve)
    item = {"id": "lx:kw:gen"}
    old = asyncio.create_task(lxapp._resolve_url_cached(None, "kw", item))
    await started[0].wait()
    lxapp._url_cache_reset()
    new = asyncio.create_task(lxapp._resolve_url_cached(None, "kw", item))
    try:
        await asyncio.wait_for(started[1].wait(), 1)
        release[0].set()
        await old
        assert len(lxapp._URL_INFLIGHT) == 1  # 旧任务不能清理新代任务
        follower = asyncio.create_task(lxapp._resolve_url_cached(None, "kw", item))
        await asyncio.sleep(0)
        release[1].set()
        r, f = await asyncio.gather(new, follower)
        assert r == f == {"url": f"https://media.test/gen-{calls[1]}"}
        assert calls[1] == calls[0] + 1
        assert lxapp._URL_CACHE[(item["id"], "lossless")]["data"] == r
    finally:
        for event in release:
            event.set()
        await asyncio.gather(old, new, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("usable", [False, True])
async def test_expired_url_probe_and_resolution_are_singleflight(monkeypatch, fresh_url_cache, usable):
    item = {"id": "lx:kw:expired"}
    key = (item["id"], "lossless")
    lxapp._URL_CACHE[key] = {"data": {"url": "https://media.test/old"},
                            "ts": lxapp.time.monotonic() - 1000}
    probing, release = asyncio.Event(), asyncio.Event()
    counts = {"probe": 0, "resolve": 0}
    async def refresh(*args):
        counts["probe"] += 1
        probing.set()
        await release.wait()
        return {"url": "https://media.test/old"} if usable else None
    async def resolve(*args, **kwargs):
        counts["resolve"] += 1
        return {"url": "https://media.test/new"}
    monkeypatch.setattr(lxapp, "_refresh_cached_url", refresh)
    monkeypatch.setattr(lxapp, "resolve_and_probe", resolve)
    leader = asyncio.create_task(lxapp._resolve_url_cached(None, "kw", item))
    await probing.wait()
    follower = asyncio.create_task(lxapp._resolve_url_cached(None, "kw", item))
    await asyncio.sleep(0)
    release.set()
    a, b = await asyncio.gather(leader, follower)
    assert a == b
    assert counts == {"probe": 1, "resolve": 0 if usable else 1}


def test_bootstrap_recovers_partial_failure_and_uses_latest_env(tmp_path, monkeypatch):
    from conftest import FakeLxServerClient
    fake = FakeLxServerClient()
    for s in fake.sources:
        s["enabled"] = False
    monkeypatch.setattr(lxapp, "LXSERVER", fake)
    monkeypatch.setenv("LX_DATA_DIR", str(tmp_path / "data"))
    env_path = tmp_path / ".env"
    monkeypatch.setenv("LX_ENV_PATH", str(env_path))
    urls = ["https://s/a.js", "https://s/b.js"]
    def save(active_urls):
        env_path.write_text("LX_SOURCE_LIST='" + json.dumps([
            {"url": u, "active": u in active_urls} for u in urls
        ]) + "'\nLX_SOURCE_URL='" + (active_urls[0] if active_urls else "") + "'\n")
    save(urls)
    original_import = fake.import_custom_source
    async def fail_b(url):
        if url == urls[1]:
            raise RuntimeError("temporarily unavailable")
        return await original_import(url)
    monkeypatch.setattr(fake, "import_custom_source", fail_b)
    asyncio.run(lxapp._bootstrap_migration())
    assert {s["id"] for s in fake.sources if s["enabled"]} == {"a.js"}
    monkeypatch.setattr(fake, "import_custom_source", original_import)
    asyncio.run(lxapp._bootstrap_migration())
    assert {s["id"] for s in fake.sources if s["enabled"]} == {"a.js", "b.js"}
    # 旧进程环境仍保留 A/B；最新磁盘目标只启用 A，随后全部显式关闭。
    monkeypatch.setenv("LX_SOURCE_LIST", json.dumps([{"url": u, "active": True} for u in urls]))
    save([urls[0]])
    asyncio.run(lxapp._bootstrap_migration())
    assert {s["id"] for s in fake.sources if s["enabled"]} == {"a.js"}
    save([])
    asyncio.run(lxapp._bootstrap_migration())
    assert not any(s["enabled"] for s in fake.sources)


def test_bootstrap_failed_new_target_keeps_old_available_source(tmp_path, monkeypatch):
    from conftest import FakeLxServerClient
    fake = FakeLxServerClient()
    monkeypatch.setattr(lxapp, "LXSERVER", fake)
    monkeypatch.setenv("LX_DATA_DIR", str(tmp_path / "data"))
    env = tmp_path / ".env"
    env.write_text('LX_SOURCE_LIST=\'[{"url":"https://s/new.js","active":true}]\'\n')
    monkeypatch.setenv("LX_ENV_PATH", str(env))
    async def fail(*args):
        raise RuntimeError("temporary import failure")
    monkeypatch.setattr(fake, "import_custom_source", fail)
    asyncio.run(lxapp._bootstrap_migration())
    assert fake.sources[0]["enabled"] is True
    assert not fake.toggle_calls
