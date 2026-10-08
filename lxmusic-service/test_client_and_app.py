"""lxmusic-service/test_client_and_app.py
全套契约与单元测试：覆盖字段映射、探活签名、API 契约和容错机制。
"""

from __future__ import annotations

import asyncio

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


def test_source_management_endpoints(test_app_client):
    # GET /api/v1/source
    res = test_app_client.get("/api/v1/source")
    assert res.status_code == 200
    assert res.json()["ok"] is True

    # POST /api/v1/source/upload
    res = test_app_client.post(
        "/api/v1/source/upload",
        json={"filename": "my_src.js", "script": "/* @name MyTest */ console.log(1);"},
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True

    # POST /api/v1/source (激活)
    res = test_app_client.post("/api/v1/source", json={"url": "my_src.js"})
    assert res.status_code == 200
    assert res.json()["ok"] is True

    # DELETE /api/v1/source (清除)
    res = test_app_client.delete("/api/v1/source")
    assert res.status_code == 200
    assert res.json()["ok"] is True


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

