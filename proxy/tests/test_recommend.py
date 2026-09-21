"""Tests for daily recommend, play-history merge, and LLM config gating."""
import asyncio
import json
import os
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import recommend as dailyrec
from proxy.app import CONF, _DAILY_TASKS, _SEARCH_CACHE, app, _conf_log_value, fake_official_guid
from proxy.app import resolve_real_guid


@pytest.fixture(autouse=True)
def setup_recommend_env(tmp_path, monkeypatch):
    _SEARCH_CACHE.clear()
    _DAILY_TASKS.clear()
    dailyrec._LAST_DAILY_INFO.clear()
    rec_dir = str(tmp_path / "recommend_cache")
    hist_dir = str(tmp_path / "play_history")
    fav_dir = str(tmp_path / "online_favorites")
    cache_dir = str(tmp_path / "cache")
    os.makedirs(fav_dir, exist_ok=True)
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", rec_dir)
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", hist_dir)
    monkeypatch.setitem(CONF, "fav_dir", fav_dir)
    monkeypatch.setitem(CONF, "cache_dir", cache_dir)
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.delenv("FNMUSIC_LLM_API_KEY", raising=False)
    monkeypatch.delenv("FNMUSIC_LLM_BASE_URL", raising=False)
    # lxmusic 客户端默认 mock（404），防止每日推荐链路在测试中触达真实 127.0.0.1:8772
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8772",
    )


def _auth_user(guid="user-rec-1"):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": guid}})
        if path.endswith("/playlist/list"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"list": [{"guid": "localpl", "name": "牛一", "coverId": "c1", "createdAt": 1, "updatedAt": 1}], "total": 1}},
            )
        if path.endswith("/playlist/batch-detail"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"list": [{"guid": "localpl", "trackCount": 3}]}},
            )
        if path.endswith("/play-history/list"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"list": [{"guid": "local-track-1", "title": "本地"}], "total": 1}},
            )
        if path.endswith("/event/report"):
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": None})
        if "search/track" in path:
            return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
        return httpx.Response(200, json={"code": 0, "data": None})
    return handler


def test_infer_language_and_guid():
    assert dailyrec.infer_language("晴天", "周杰伦") == "中文"
    assert dailyrec.infer_language("夜に駆ける", "YOASOBI") == "日语"
    assert dailyrec.infer_language("Dynamite", "BTS 방탄소년단") == "韩语"
    assert dailyrec.infer_language("Shape of You", "Ed Sheeran") == "英语"
    guid = dailyrec.daily_playlist_guid("20260831", "user-1")
    assert dailyrec.is_daily_playlist_guid(guid)
    assert "20260831" in guid


def test_parse_llm_json_fenced_and_object():
    raw = """```json
    [{"title": "七里香", "artist": "周杰伦", "dimension": "artist", "reason": "同歌手"}]
    ```"""
    items = dailyrec.parse_llm_recommendations(raw)
    assert items[0]["title"] == "七里香"
    wrapped = json.dumps({"songs": [{"title": "海阔天空", "artist": "Beyond", "dimension": "genre"}]})
    items2 = dailyrec.parse_llm_recommendations(wrapped)
    assert items2[0]["artist"] == "Beyond"


def test_conf_log_redacts_secrets():
    assert _conf_log_value("llm_api_key", "sk-secret") == "***"
    assert _conf_log_value("musicdl_url", "http://127.0.0.1:8768") == "http://127.0.0.1:8768"


def test_playlist_list_injects_fallback_when_llm_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "items": [{
                        "id": "migu:1",
                        "source": "migu",
                        "title": "晴天",
                        "artist": "周杰伦",
                        "album": "叶惠美",
                        "duration_s": 269,
                        "ext": "mp3",
                    }],
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(_auth_user()), base_url="http://unix")
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8770",
    )
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        assert resp.status_code == 200
        names = [x.get("name") for x in resp.json()["data"]["list"]]
        assert "牛一" in names
        assert any("每日推荐" in str(n) for n in names)
        first = resp.json()["data"]["list"][0]
        assert first["isDaily"] is True
        assert dailyrec.is_daily_playlist_guid(first["guid"])
        assert dailyrec.today_key() in first["guid"]


def test_playlist_list_drops_yesterday_daily(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    yesterday = dailyrec.daily_playlist_guid("20200101", "user-rec-1")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": "user-rec-1"}})
        if path.endswith("/playlist/list"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "list": [
                            {"guid": yesterday, "name": "每日推荐 01-01", "coverId": "old", "createdAt": 1, "updatedAt": 1},
                            {"guid": "localpl", "name": "牛一", "coverId": "c1", "createdAt": 1, "updatedAt": 1},
                        ],
                        "total": 2,
                    },
                },
            )
        return httpx.Response(200, json={"code": 0, "data": None})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "items": [{
                        "id": "migu:1",
                        "source": "migu",
                        "title": "晴天",
                        "artist": "周杰伦",
                        "duration_s": 269,
                        "ext": "mp3",
                    }],
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://unix")
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8770",
    )
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        names = [x.get("name") for x in resp.json()["data"]["list"]]
        guids = [x.get("guid") for x in resp.json()["data"]["list"]]
        assert yesterday not in guids
        assert sum(1 for n in names if "每日推荐" in str(n)) == 1
        assert dailyrec.today_key() in str(guids[0])


def test_playlist_list_injects_daily_when_enabled(monkeypatch, tmp_path):
    # 大模型链路仅当网易音源未启用时生效（新策略），此处关闭网易以覆盖 LLM 注入端到端流程
    monkeypatch.setitem(CONF, "netease_enabled", False)
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "items": [{
                        "id": "migu:1",
                        "source": "migu",
                        "title": "晴天",
                        "artist": "周杰伦",
                        "album": "叶惠美",
                        "duration_s": 269,
                        "ext": "mp3",
                    }],
                },
            )
        return httpx.Response(404)

    def llm_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {
                        "content": json.dumps([
                            {"title": "晴天", "artist": "周杰伦", "dimension": "artist", "genre": "流行", "language": "中文", "type": "抒情", "reason": "同歌手"},
                            {"title": "七里香", "artist": "周杰伦", "dimension": "genre", "genre": "流行", "language": "中文", "type": "流行", "reason": "曲风"},
                        ])
                    }
                }]
            },
        )

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(_auth_user()), base_url="http://unix")
    app.state.musicdl_client = httpx.AsyncClient(transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8770",
    )
    app.state.llm_client = httpx.AsyncClient(transport=httpx.MockTransport(llm_handler), base_url="http://127.0.0.1:9")

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert body["data"]["total"] >= 2
        first = body["data"]["list"][0]
        assert first["isDaily"] is True
        assert dailyrec.is_daily_playlist_guid(first["guid"])
        assert first["trackCount"] >= 1

        detail = client.get(f"/music/api/v1/playlist/detail?guid={first['guid']}")
        assert detail.json()["data"]["guid"] == first["guid"]

        tracks = client.get(f"/music/api/v1/track/playlist-detail/list?playlistGUID={first['guid']}&page=1&size=50")
        tj = tracks.json()
        assert tj["code"] == 0
        assert tj["data"]["total"] >= 1
        assert resolve_real_guid(tj["data"]["list"][0]["guid"]).startswith("online:")
        assert isinstance(tj["data"]["list"][0]["artists"], list)

        batch = client.get(f"/music/api/v1/playlist/batch-detail?guids={first['guid']},localpl")
        bj = batch.json()
        assert any(dailyrec.is_daily_playlist_guid(str(x.get("guid"))) for x in bj["data"]["list"])


def test_playlist_unauth_passthrough(monkeypatch):
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "sk-test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 99999, "msg": "INVALID TOKEN", "data": None})

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://unix")
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        assert resp.json()["code"] == 99999


def test_event_report_records_online_play(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "ph"))
    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(_auth_user()), base_url="http://unix")
    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/event/report",
            json={"events": [{"eventType": "track_play", "occurredAt": 1, "payload": {"trackGUID": "online:migu:1"}}]},
        )
        assert resp.json()["code"] == 0
        items = dailyrec.load_online_play_history("user-rec-1")
        assert items[-1]["guid"] == "online:migu:1"


def test_play_history_merges_online(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "ph"))
    dailyrec.record_online_play("user-rec-1", "online:migu:99", {"title": "在线歌", "artist": "歌手"})
    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(_auth_user()), base_url="http://unix")
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/play-history/list")
        body = resp.json()
        guids = [x["guid"] for x in body["data"]["list"]]
        assert fake_official_guid("online:migu:99") in guids
        assert "local-track-1" in guids
        assert body["data"]["total"] == 2


def test_read_local_recent_tracks(tmp_path):
    db = tmp_path / "music.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE user (id INTEGER PRIMARY KEY, guid TEXT);
        CREATE TABLE track (id INTEGER PRIMARY KEY, guid TEXT, title TEXT, year INTEGER, album_id INTEGER);
        CREATE TABLE album (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE artist (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE track_artist (track_id INTEGER, artist_id INTEGER);
        CREATE TABLE genre (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE track_genre (track_id INTEGER, genre_id INTEGER);
        CREATE TABLE play_history (id INTEGER PRIMARY KEY, user_id INTEGER, track_id INTEGER, play_count INTEGER, updated_at TEXT);
        INSERT INTO user VALUES (1, 'u1');
        INSERT INTO album VALUES (1, '叶惠美');
        INSERT INTO track VALUES (1, 'tg1', '晴天', 2003, 1);
        INSERT INTO artist VALUES (1, '周杰伦');
        INSERT INTO track_artist VALUES (1, 1);
        INSERT INTO genre VALUES (1, '流行');
        INSERT INTO track_genre VALUES (1, 1);
        INSERT INTO play_history VALUES (1, 1, 1, 3, '2026-08-31 09:00:00+08:00');
        """
    )
    con.commit()
    con.close()
    rows = dailyrec.read_local_recent_tracks(str(db), "u1", 20)
    assert rows[0]["title"] == "晴天"
    assert "周杰伦" in rows[0]["artist"]
    assert rows[0]["language"] == "中文"


def test_purge_stale_daily_cache_keeps_today_only(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "rc"))
    user = "user-rec-1"
    folder = os.path.join(dailyrec.recommend_cache_dir(), dailyrec._safe_user_name(user))
    os.makedirs(folder, exist_ok=True)
    open(os.path.join(folder, "20260830.json"), "w").write("{}")            # 旧格式（无前缀）
    open(os.path.join(folder, "daily-20260831.json"), "w").write("{}")
    open(os.path.join(folder, "hot-20260831.json"), "w").write("{}")
    open(os.path.join(folder, "daily-20260901.json"), "w").write("{}")
    dailyrec.purge_stale_daily_cache(user, "20260831")
    assert not os.path.exists(os.path.join(folder, "20260830.json"))        # 旧格式一并清理
    assert not os.path.exists(os.path.join(folder, "daily-20260901.json"))
    assert os.path.exists(os.path.join(folder, "daily-20260831.json"))
    assert os.path.exists(os.path.join(folder, "hot-20260831.json"))


def test_collect_exclude_sets_from_favorites():
    guids, tas = dailyrec.collect_exclude_sets(
        [{"guid": "online:migu:1", "track": {"title": "晴天", "artist": "周杰伦"}}],
        [{"guid": "local-1", "title": "七里香", "artist": "周杰伦"}],
    )
    assert "online:migu:1" in guids
    assert dailyrec.identity_key("晴天", "周杰伦") in tas
    assert dailyrec.identity_key("七里香", "周杰伦") in tas


@pytest.mark.anyio
async def test_daily_fills_twenty_online_and_skips_favorites(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "rc"))

    recs = [
        {"title": "已收藏", "artist": "收藏歌手", "dimension": "artist", "reason": "should skip"},
    ] + [
        {"title": f"新歌{i}", "artist": f"歌手{i}", "dimension": "genre", "reason": "n"}
        for i in range(30)
    ]

    def llm_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(recs)}}]},
        )

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/search":
            return httpx.Response(404)
        kw = request.url.params.get("keyword") or ""
        if "已收藏" in kw or "收藏歌手" in kw:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "items": [{
                        "id": "migu:fav1",
                        "source": "migu",
                        "title": "已收藏",
                        "artist": "收藏歌手",
                        "duration_s": 200,
                        "ext": "mp3",
                    }],
                },
            )
        import re
        m = re.search(r"(\d+)", kw)
        i = int(m.group(1)) if m else 99
        return httpx.Response(
            200,
            json={
                "ok": True,
                "items": [{
                    "id": f"migu:{i}",
                    "source": "migu",
                    "title": f"新歌{i}",
                    "artist": f"歌手{i}",
                    "duration_s": 200,
                    "ext": "mp3",
                }],
            },
        )

    from proxy.app import build_online_track

    yesterday = os.path.join(dailyrec.recommend_cache_dir(), dailyrec._safe_user_name("u-fill"), "20200101.json")
    os.makedirs(os.path.dirname(yesterday), exist_ok=True)
    with open(yesterday, "w") as f:
        f.write("{}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(llm_handler), base_url="http://127.0.0.1:9") as llm_client, \
            httpx.AsyncClient(transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768") as mdl, \
            httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
                base_url="http://127.0.0.1:8770",
            ) as mb:
        payload = await dailyrec.get_or_build_daily(
            user_guid="u-fill",
            musicdl_client=mdl,
            musicbox_client=mb,
            llm_http=llm_client,
            build_track=build_online_track,
            netease_enabled=False,
            favorite_items=[{
                "guid": "online:migu:fav1",
                "track": {"title": "已收藏", "artist": "收藏歌手"},
            }],
        )
    assert payload["status"] == "ready"
    assert len(payload["tracks"]) == 20
    titles = [str(t.get("title")) for t in payload["tracks"]]
    guids = [str(t.get("guid")) for t in payload["tracks"]]
    assert "已收藏" not in titles
    assert "online:migu:fav1" not in guids
    assert not os.path.exists(yesterday)
    cached = dailyrec.load_daily_cache("u-fill", dailyrec.today_key())
    assert cached is not None
    assert len(cached["tracks"]) == 20


def test_healthz_includes_llm_flag():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/healthz"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"code": 99999})

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://unix")
    app.state.musicdl_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8768")
    app.state.musicbox_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8770")
    with TestClient(app) as client:
        resp = client.get("/_ext/healthz")
        assert "llm" in resp.json()
        assert resp.json()["llm"] in ("disabled", "enabled")
        rec = resp.json()["recommend"]
        assert rec["mode"] == "source-native"
        assert rec["netease"] is True
        assert isinstance(rec["recent"], dict)


@pytest.mark.anyio
async def test_get_or_build_daily_uses_llm_when_netease_disabled(monkeypatch):
    """网易音源未启用且配置了 LLM 时走大模型，不能被 fallback 竞速取消。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", "/nonexistent.db")
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "test-key")
    monkeypatch.setenv("FNMUSIC_LLM_MODEL", "gpt-test")
    llm_calls = {"n": 0}

    def llm_handler(request: httpx.Request) -> httpx.Response:
        llm_calls["n"] += 1
        content = json.dumps([
            {"title": f"LLM歌{i}", "artist": f"LLM歌手{i}", "dimension": "llm", "reason": "test"}
            for i in range(30)
        ])
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        kw = request.url.params.get("keyword") or ""
        title = kw.split()[-1] if kw else "x"
        return httpx.Response(
            200,
            json={"items": [{"id": f"migu:{abs(hash(kw)) % 100000}", "source": "migu", "title": title, "artist": "A", "duration_s": 180, "ext": "mp3"}]},
        )

    from proxy.app import build_online_track

    async with httpx.AsyncClient(transport=httpx.MockTransport(llm_handler), base_url="http://127.0.0.1:9") as llm_client, \
            httpx.AsyncClient(transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768") as mdl, \
            httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url="http://127.0.0.1:8770") as mb:
        payload = await dailyrec.get_or_build_daily(
            user_guid="u-llm-first",
            musicdl_client=mdl,
            musicbox_client=mb,
            llm_http=llm_client,
            build_track=build_online_track,
            netease_enabled=False,
        )
    assert llm_calls["n"] >= 1
    assert len(payload["tracks"]) >= 1
    assert "llm" in payload["tiers"]
    # 至少部分曲目应来自 LLM 候选标题
    titles = " ".join(str(t.get("title") or "") for t in payload["tracks"])
    assert "LLM歌" in titles


def _mb_detail_rows(n: int, prefix: str, sid_base: int = 90000) -> list[dict]:
    return [
        {
            "song_id": sid_base + i,
            "name": f"{prefix}{i}",
            "artist": f"网易歌手{i % 5}",
            "album_name": f"专辑{i}",
            "album_pic_url": f"http://img/{sid_base + i}.jpg",
            "duration_ms": 210000,
            "has_sq": i % 4 == 0,
            "has_hr": False,
        }
        for i in range(n)
    ]


@pytest.mark.anyio
async def test_daily_prefers_netease_daily_and_skips_llm(tmp_path, monkeypatch):
    """网易启用时：真·每日推荐直接命中（平台 ID 直连），LLM 完全不调用。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "test-key")
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "rc"))
    llm_calls = {"n": 0}

    def llm_handler(request: httpx.Request) -> httpx.Response:
        llm_calls["n"] += 1
        return httpx.Response(500)

    def mb_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/recommend/songs":
            assert request.url.params.get("limit") == str(dailyrec.NETEASE_DAILY_LIMIT)
            return httpx.Response(200, json={"ok": True, "data": _mb_detail_rows(25, "网易日推"), "logged_in": True})
        return httpx.Response(404, json={"ok": False})

    from proxy.app import build_online_track

    async with httpx.AsyncClient(transport=httpx.MockTransport(llm_handler), base_url="http://127.0.0.1:9") as llm_client, \
            httpx.AsyncClient(transport=httpx.MockTransport(mb_handler), base_url="http://127.0.0.1:8770") as mb:
        payload = await dailyrec.get_or_build_daily(
            user_guid="u-netease-daily",
            musicdl_client=None,
            musicbox_client=mb,
            llm_http=llm_client,
            build_track=build_online_track,
            netease_enabled=True,
            favorite_items=[{"guid": "online:netease:90000", "track": {"title": "网易日推0", "artist": "网易歌手0"}}],
        )
    assert payload["tiers"] == ["netease-daily"]
    assert payload["status"] == "ready"
    assert len(payload["tracks"]) == dailyrec.PLAYLIST_SIZE
    assert all(str(t["guid"]).startswith("online:netease:") for t in payload["tracks"])
    titles = [str(t.get("title")) for t in payload["tracks"]]
    assert "网易日推0" not in titles  # 已收藏跳过
    assert "网易日推1" in titles
    assert llm_calls["n"] == 0  # 网易启用时绝不调 LLM
    summary = dailyrec.last_recommend_summary().get("u-netease-daily:daily")
    assert summary and summary["tiers"] == ["netease-daily"]


@pytest.mark.anyio
async def test_hot_playlist_uses_netease_charts(tmp_path, monkeypatch):
    """热门推荐歌单：网易热歌榜免登录可用，封面取第一首有封面的歌。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "rc"))

    def mb_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/recommend/songs":
            return httpx.Response(
                200,
                json={"ok": False, "error": {"type": "not_logged_in", "message": "未登录或登录已过期"}},
            )
        if request.url.path == "/api/v1/toplist":
            assert request.url.params.get("index") == str(dailyrec.NETEASE_TOPLIST_INDEX)
            return httpx.Response(200, json={"ok": True, "data": _mb_detail_rows(25, "热歌榜", sid_base=70000), "index": 3})
        return httpx.Response(404, json={"ok": False})

    from proxy.app import build_online_track

    async with httpx.AsyncClient(transport=httpx.MockTransport(mb_handler), base_url="http://127.0.0.1:8770") as mb:
        payload = await dailyrec.get_or_build_daily(
            user_guid="u-netease-charts",
            musicdl_client=None,
            musicbox_client=mb,
            llm_http=None,
            build_track=build_online_track,
            netease_enabled=True,
            kind="hot",
        )
    assert payload["kind"] == "hot"
    assert payload["tiers"] == ["netease-charts"]
    assert payload["guid"].startswith("online:playlist:hot:")
    assert payload["playlist"]["name"] == "热门推荐"
    assert len(payload["tracks"]) == dailyrec.PLAYLIST_SIZE
    assert all(str(t["guid"]).startswith("online:netease:") for t in payload["tracks"])
    # 封面 = 第一个有 cover_url 的歌（第一首在列）
    assert payload["playlist"]["coverId"] == payload["tracks"][0]["guid"]
    assert str(payload["tracks"][0].get("cover_url") or "").startswith("http://img/70000")


@pytest.mark.anyio
async def test_daily_skips_charts_when_not_logged_in(tmp_path, monkeypatch):
    """未登录时每日推荐不再借用榜单（榜单归热门歌单），无 LLM/音源则为空。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "rc"))
    paths = []

    def mb_handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/v1/recommend/songs":
            return httpx.Response(
                200,
                json={"ok": False, "error": {"type": "not_logged_in", "message": "未登录或登录已过期"}},
            )
        if request.url.path == "/api/v1/toplist":
            return httpx.Response(200, json={"ok": True, "data": _mb_detail_rows(25, "热歌榜", sid_base=70000), "index": 3})
        return httpx.Response(404, json={"ok": False})

    from proxy.app import build_online_track

    async with httpx.AsyncClient(transport=httpx.MockTransport(mb_handler), base_url="http://127.0.0.1:8770") as mb:
        payload = await dailyrec.get_or_build_daily(
            user_guid="u-daily-no-charts",
            musicdl_client=None,
            musicbox_client=mb,
            llm_http=None,
            build_track=build_online_track,
            netease_enabled=True,
        )
    assert "/api/v1/toplist" not in paths  # daily 链不再请求榜单
    assert payload["kind"] == "daily"
    assert payload["tracks"] == []
    assert payload["tiers"] == []


@pytest.mark.anyio
async def test_hot_uses_lx_charts_when_musicbox_unavailable(tmp_path, monkeypatch):
    """musicbox 全挂时热门歌单降级 lxmusic 免登录榜单；全无封面则 coverId 指向歌单自身。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "rc"))

    def lx_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/recommend":
            items = [
                {
                    "id": f"lx:kg:HASH{i}",
                    "lx_source": "kg",
                    "title": f"榜单歌{i}",
                    "artist": f"酷狗歌手{i}",
                    "album": "",
                    "duration_s": 200,
                    "ext": "mp3",
                    "cover_url": "",
                }
                for i in range(25)
            ]
            return httpx.Response(200, json={"ok": True, "items": items, "errors": {}})
        return httpx.Response(404, json={"ok": False})

    from proxy.app import build_online_track

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url="http://127.0.0.1:8770") as mb, \
            httpx.AsyncClient(transport=httpx.MockTransport(lx_handler), base_url="http://127.0.0.1:8772") as lx:
        payload = await dailyrec.get_or_build_daily(
            user_guid="u-lx-charts",
            musicdl_client=None,
            musicbox_client=mb,
            llm_http=None,
            build_track=build_online_track,
            netease_enabled=True,
            lx_client=lx,
            lx_enabled=True,
            kind="hot",
        )
    assert payload["kind"] == "hot"
    assert payload["tiers"] == ["lx-charts"]
    assert len(payload["tracks"]) == dailyrec.PLAYLIST_SIZE
    assert all(str(t["guid"]).startswith("online:lx:kg:") for t in payload["tracks"])
    # lx 榜单曲目 cover_url 全为空：无可用封面，coverId 落到歌单自身 guid
    assert payload["playlist"]["coverId"] == payload["guid"]


# ------------------------------------------------ 推荐歌单封面取曲

def test_pick_playlist_cover_track():
    tracks = [
        {"guid": "online:a:1", "cover_url": ""},
        {"guid": "online:a:2", "cover_url": f"https://{dailyrec.KW_TEXT_COVER_HOST}/pic?rid=1"},  # 酷我文本页假封面
        {"guid": "online:a:3", "cover_url": "https://img.example/3.jpg"},
        {"guid": "online:a:4", "cover_url": "https://img.example/4.jpg"},
    ]
    assert dailyrec.pick_playlist_cover_track(tracks)["guid"] == "online:a:3"
    assert dailyrec.pick_playlist_cover_track([]) is None
    assert dailyrec.pick_playlist_cover_track(None) is None
    assert dailyrec.pick_playlist_cover_track([{"guid": "x", "cover_url": ""}]) is None
    assert dailyrec.pick_playlist_cover_track(
        [{"guid": "x", "cover_url": ""}, {"guid": "y", "cover_url": "https://a/1.jpg"}]
    )["guid"] == "y"


def test_recommend_playlist_guid_kinds():
    day = "20260921"
    daily = dailyrec.recommend_playlist_guid("daily", day, "user-x")
    hot = dailyrec.recommend_playlist_guid("hot", day, "user-x")
    assert daily.startswith("online:playlist:daily:20260921:")
    assert hot.startswith("online:playlist:hot:20260921:")
    assert dailyrec.online_playlist_kind(daily) == "daily"
    assert dailyrec.online_playlist_kind(hot) == "hot"
    assert dailyrec.online_playlist_kind("online:migu:1") == ""
    assert dailyrec.is_recommend_playlist_guid(daily) and dailyrec.is_recommend_playlist_guid(hot)
    assert not dailyrec.is_recommend_playlist_guid("online:migu:1")


@pytest.mark.anyio
async def test_daily_cover_skips_coverless_tracks(tmp_path, monkeypatch):
    """构建出的歌单封面 = 第一个有封面直链的歌（第一首无封面时跳到第二首）。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "rc"))

    rows = [
        {
            "song_id": 51000, "name": "无封面歌", "artist": "歌手A", "album_name": "专辑A",
            "album_pic_url": "", "duration_ms": 210000, "has_sq": False, "has_hr": False,
        },
    ] + _mb_detail_rows(24, "有封面歌", sid_base=51001)

    def mb_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/recommend/songs"
        return httpx.Response(200, json={"ok": True, "data": rows, "logged_in": True})

    from proxy.app import build_online_track

    async with httpx.AsyncClient(transport=httpx.MockTransport(mb_handler), base_url="http://127.0.0.1:8770") as mb:
        payload = await dailyrec.get_or_build_daily(
            user_guid="u-cover-pick",
            musicdl_client=None,
            musicbox_client=mb,
            llm_http=None,
            build_track=build_online_track,
            netease_enabled=True,
        )
    assert payload["tracks"][0]["title"] == "无封面歌"
    assert not payload["tracks"][0].get("cover_url")
    assert payload["playlist"]["coverId"] == payload["tracks"][1]["guid"]
    assert payload["tracks"][1].get("cover_url")


def test_playlist_list_injects_both_playlists_with_disguised_cover(tmp_path, monkeypatch):
    """双开关开启：每日推荐与热门推荐两条歌单都注入，coverId 伪装为官方 track_+32hex。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(tmp_path / "missing.db"))
    monkeypatch.setitem(CONF, "recommend_daily", True)
    monkeypatch.setitem(CONF, "recommend_hot", True)

    def mb_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/recommend/songs":
            return httpx.Response(200, json={"ok": True, "data": _mb_detail_rows(25, "网易日推"), "logged_in": True})
        if request.url.path == "/api/v1/toplist":
            return httpx.Response(200, json={"ok": True, "data": _mb_detail_rows(25, "热歌榜", sid_base=70000), "index": 3})
        return httpx.Response(404, json={"ok": False})

    app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(_auth_user()), base_url="http://unix")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(mb_handler), base_url="http://127.0.0.1:8770"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url="http://127.0.0.1:8768"
    )
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        assert resp.status_code == 200
        data = resp.json()["data"]
        recs = [it for it in data["list"] if dailyrec.is_recommend_playlist_guid(str(it.get("guid") or ""))]
        assert len(recs) == 2
        assert data["total"] == len(data["list"])
        daily = next(it for it in recs if dailyrec.online_playlist_kind(str(it["guid"])) == "daily")
        hot = next(it for it in recs if dailyrec.online_playlist_kind(str(it["guid"])) == "hot")
        assert "每日推荐" in daily["name"]
        assert hot["name"] == "热门推荐"
        # coverId 伪装为官方形态（track_ + 32hex），且可反解回第一个有封面的歌
        for rec in (daily, hot):
            cover = str(rec["coverId"])
            assert cover.startswith("track_") and len(cover) == 6 + 32
            resolved = resolve_real_guid(cover)
            assert str(resolved).startswith("online:netease:")


@pytest.mark.asyncio
async def test_resolve_recommendations_concurrency_and_throttle(monkeypatch):
    active = 0
    max_active = 0
    search_timestamps = []

    async def mock_search_keyword(keyword, *args, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        search_timestamps.append(time.monotonic())
        try:
            await asyncio.sleep(0.02)
        finally:
            active -= 1
            return [{
                "id": f"netease:{keyword}",
                "source": "netease",
                "title": keyword,
                "artist": "Artist",
                "album": "Album",
                "duration_s": 200,
                "ext": "mp3",
            }]

    monkeypatch.setattr(dailyrec, "_search_keyword", mock_search_keyword)
    monkeypatch.setattr(dailyrec, "RECOMMEND_SEARCH_CONCURRENCY", 2)
    monkeypatch.setattr(dailyrec, "RECOMMEND_SEARCH_INTERVAL", 0.05)

    recs = [{"title": f"Song {i}", "artist": "Artist"} for i in range(6)]
    t0 = time.monotonic()
    results = await dailyrec.resolve_recommendations(
        recs=recs,
        musicdl_client=None,
        musicbox_client=None,
        netease_enabled=True,
        build_track=lambda pick: {"guid": f"online:{pick['id']}", "title": pick["title"], "artist": pick["artist"]},
        limit=6,
    )

    assert max_active <= 2
    assert len(results) == 6
    assert len(search_timestamps) >= 6
    assert time.monotonic() - t0 >= 0.12

