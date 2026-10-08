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

