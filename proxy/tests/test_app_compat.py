"""App 端兼容性修复测试：列表鉴权探测降级、在线条目官方形状对齐、
stream HEAD/路径形式 guid、播放事件元数据捕获、请求日志白名单。"""
import json
import logging
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import recommend as dailyrec
from proxy.app import app, CONF, _SEARCH_CACHE
from proxy.takeover import safe_child_log


@pytest.fixture(autouse=True)
def setup_compat_env(tmp_path, monkeypatch):
    _SEARCH_CACHE.clear()
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "play_history"))
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "recommend_cache"))
    fav_dir = str(tmp_path / "online_favorites")
    os.makedirs(fav_dir, exist_ok=True)
    os.makedirs(str(tmp_path / "library"), exist_ok=True)
    monkeypatch.setitem(CONF, "fav_dir", fav_dir)
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(CONF, "library_dir", str(tmp_path / "library"))
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "lx_enabled", False)
    monkeypatch.setitem(CONF, "merge_suggest", False)
    monkeypatch.setitem(CONF, "lyric_field", "data.lyric")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8770",
    )


def _write_fav_file(user: str, items: list) -> None:
    path = os.path.join(CONF["fav_dir"], f"{user}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False)


_OFFICIAL_FAV_ITEM = {
    "guid": "local:101",
    "title": "夜曲",
    "artists": [{"name": "周杰伦", "guid": "local:artist:1"}],
    "album": {"name": "十一月的萧邦", "guid": "local:album:1"},
    "duration": 226000,
    "isFavorite": True,
}


def test_favorite_list_probe_rejected_degrades_to_official():
    """官方收藏已取回成功后 user/me 探测被拒 → 降级仅返回官方列表，不回传鉴权错误。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/favorite-track/list":
            return httpx.Response(
                200,
                json={"code": 0, "msg": "", "data": {"list": [_OFFICIAL_FAV_ITEM], "total": 1, "sort": "favoriteAt,desc"}},
            )
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 99999, "msg": "INVALID TOKEN", "data": None})
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500)), base_url="http://127.0.0.1:8768"
    )
    _write_fav_file("user-a", [{
        "guid": "online:migu:600908",
        "createdAt": 1700000000,
        "track": {"guid": "online:migu:600908", "title": "稻香", "artists": [{"name": "周杰伦"}], "album": {"name": "魔杰座"}, "duration": 223000},
    }])

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/favorite-track/list?page=1&size=100")
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["code"] == 0
        assert [it["guid"] for it in rj["data"]["list"]] == ["local:101"]
        assert rj["data"]["total"] == 1


def test_play_history_list_probe_rejected_degrades_to_official():
    """官方历史已取回成功后探测被拒 → 降级仅返回官方列表，不回传鉴权错误。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/play-history/list":
            return httpx.Response(
                200,
                json={"code": 0, "msg": "", "data": {"list": [dict(_OFFICIAL_FAV_ITEM)], "total": 1}},
            )
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 99999, "msg": "INVALID TOKEN", "data": None})
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500)), base_url="http://127.0.0.1:8768"
    )
    dailyrec.record_online_play("user-a", "online:migu:600908", {"title": "稻香", "artist": "周杰伦"})

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/play-history/list")
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["code"] == 0
        assert [it["guid"] for it in rj["data"]["list"]] == ["local:101"]
        assert rj["data"]["total"] == 1


def test_favorite_list_online_entry_matches_official_shape():
    """在线条目以官方真实条目为结构模板：官方独有字段保留结构，缺失元数据走兜底。"""
    official_item = dict(_OFFICIAL_FAV_ITEM, codec="alac")

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/favorite-track/list":
            return httpx.Response(
                200,
                json={"code": 0, "msg": "", "data": {"list": [official_item], "total": 1, "sort": "favoriteAt,desc"}},
            )
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "data": {"guid": "user-a"}})
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500)), base_url="http://127.0.0.1:8768"
    )
    # 存量快照缺 artist/album/时长来源，验证兜底与字段还原
    _write_fav_file("user-a", [{
        "guid": "online:migu:600908",
        "createdAt": 1700000000,
        "track": {"guid": "online:migu:600908", "title": "稻香", "duration": 223000},
    }])

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/favorite-track/list?page=1&size=100")
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["data"]["total"] == 2
        online_item = rj["data"]["list"][1]
        assert online_item["guid"] == "online:migu:600908"
        assert online_item["title"] == "稻香"
        assert online_item["duration"] == 223000
        assert online_item["artists"][0]["name"] == "未知艺术家"
        assert online_item["album"]["name"] == "未知专辑"
        assert online_item["isFavorite"] is True
        # 官方独有字段继承模板结构（App 端严格解析需要字段集对齐）
        assert online_item["codec"] == "alac"


def test_stream_head_and_path_form_guid():
    """stream 支持 HEAD 探测与路径形式 guid：命中缓存时不再透传上游。"""
    audio = b"FAKE_AUDIO_" * 100
    upstream_calls: list[str] = []

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={"ok": True, "id": "kuwo:228908", "title": "晴天", "artist": "周杰伦", "lyric": ""},
            )
        return httpx.Response(
            200,
            content=audio,
            headers={"Content-Type": "audio/mpeg", "Content-Length": str(len(audio)), "Accept-Ranges": "bytes"},
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        # 先 GET 一次使 tee 缓存落盘
        first = client.get("/music/api/v1/track/stream?guid=online:kuwo:228908")
        assert first.status_code == 200
        assert first.content == audio
        calls_before = len(upstream_calls)

        head = client.head("/music/api/v1/track/stream/online:kuwo:228908")
        assert head.status_code == 200
        assert head.headers.get("content-length") == str(len(audio))
        assert head.content == b""

        got = client.get("/music/api/v1/track/stream/online:kuwo:228908")
        assert got.status_code == 200
        assert got.content == audio
        assert len(upstream_calls) == calls_before


def test_event_report_captures_payload_metadata():
    """track_play 事件 payload 携带的元数据写入历史快照，历史列表合并后可见。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "data": {"guid": "user-a"}})
        if request.url.path == "/music/api/v1/play-history/list":
            return httpx.Response(200, json={"code": 0, "msg": "", "data": {"list": [], "total": 0}})
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500)), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/event/report",
            json={"events": [{
                "eventType": "track_play",
                "payload": {
                    "trackGUID": "online:kuwo:228908",
                    "title": "晴天",
                    "artist": "周杰伦",
                    "album": "叶惠美",
                    "duration": 269000,
                },
            }]},
        )
        assert resp.status_code == 200
        assert resp.json()["code"] == 0

        items = dailyrec.load_online_play_history("user-a")
        assert items and items[-1]["guid"] == "online:kuwo:228908"
        snap = items[-1]["track"]
        assert snap["title"] == "晴天"
        assert snap["artist"] == "周杰伦"
        assert snap["album"] == "叶惠美"
        assert abs(float(snap["duration_s"]) - 269.0) < 0.001

        listed = client.get("/music/api/v1/play-history/list")
        assert listed.status_code == 200
        rj = listed.json()
        entry = next(it for it in rj["data"]["list"] if it.get("guid") == "online:kuwo:228908")
        assert entry["title"] == "晴天"
        assert entry["artists"][0]["name"] == "周杰伦"
        assert entry["duration"] == 269000
        assert entry["isFavorite"] is False


def test_request_log_line(caplog):
    """请求日志固定格式：方法 路径 状态 UA。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/favorite-track/list":
            return httpx.Response(200, json={"code": 0, "msg": "", "data": {"list": [], "total": 0}})
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "data": {"guid": "user-a"}})
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500)), base_url="http://127.0.0.1:8768"
    )

    with caplog.at_level(logging.INFO, logger="fnmusic_proxy"):
        with TestClient(app) as client:
            resp = client.get(
                "/music/api/v1/favorite-track/list",
                headers={"User-Agent": "fnos-music/1.0 (iOS)"},
            )
            assert resp.status_code == 200

    assert any(
        r.message.startswith("client request GET /music/api/v1/favorite-track/list status=200 ua=fnos-music/1.0 (iOS)")
        for r in caplog.records
    )


def test_safe_child_log_allows_request_log_and_degrade():
    """takeover 日志白名单放行请求日志与降级告警的固定格式，其余仍丢弃。"""
    request_line = (
        "2026-09-18 10:00:00,123 [INFO] fnmusic_proxy: "
        "client request GET /music/api/v1/track/stream status=200 ua=fnos-music/1.0 iOS"
    )
    out = safe_child_log(request_line)
    assert out is not None
    assert "client request" in out

    degrade_line = (
        "2026-09-18 10:00:00,123 [WARNING] fnmusic_proxy: "
        "favorite list degraded to official-only: auth probe rejected after official list ok"
    )
    assert safe_child_log(degrade_line) is not None

    assert safe_child_log("2026-09-18 10:00:00,123 [INFO] fnmusic_proxy: secret token abc123") is None
