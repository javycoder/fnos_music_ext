"""Local recommendations: ranking, account isolation and client contracts."""
import asyncio
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import localrecommend as localrec
from proxy import recommend as rec
from proxy.app import CONF, app, fake_official_guid
from proxy.tests.test_recommend import _make_local_db


NOW = 1800000000


@pytest.fixture
def library(tmp_path, monkeypatch):
    db = tmp_path / "music.db"
    _make_local_db(db, n_tracks=50)
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "recommend"))
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "history"))
    monkeypatch.setitem(CONF, "music_db", str(db))
    monkeypatch.setitem(CONF, "recommend_daily", False)
    monkeypatch.setitem(CONF, "recommend_hot", False)
    monkeypatch.setitem(CONF, "netease_my_playlists", False)
    monkeypatch.setitem(CONF, "public_playlists_enabled", False)
    monkeypatch.setattr(localrec.time, "time", lambda: NOW)
    return db


def edit(db, sql, parameters=()):
    with sqlite3.connect(db) as con:
        con.execute(sql, parameters)


def bundle(db, user="u1"):
    return localrec._load_or_build(user, str(db))


def cache_path(user="u1"):
    return rec.os.path.join(rec.recommend_cache_dir(), hashlib.sha256(user.encode()).hexdigest(), "liked.json")


def test_threshold_and_deleted(library):
    edit(library, "UPDATE track SET is_admin_deleted = 1 WHERE id > 19")
    assert bundle(library)["tracks"] == []
    edit(library, "UPDATE track SET is_admin_deleted = 0 WHERE id = 20")
    assert len(bundle(library)["tracks"]) == 20
    edit(library, "UPDATE audio_file SET is_physical_file_deleted = 1 WHERE id = 20")
    assert bundle(library)["tracks"] == []


def test_all_favorites_are_excluded_and_online_is_not(tmp_path, monkeypatch):
    from proxy import app as proxy_app

    db = tmp_path / "large.db"
    _make_local_db(db, n_tracks=240)
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "recommend"))
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "history"))
    monkeypatch.setitem(CONF, "fav_dir", str(tmp_path / "favorites"))
    edit(db, "INSERT INTO favorite_track(user_id, track_id) SELECT 1, id FROM track WHERE id <= 220")
    proxy_app.save_online_favorites("u1", [{
        "guid": "online:wy:one", "track": {"title": "本地歌221", "artist": "歌手221"},
    }])
    rec.record_online_play("u1", "online:wy:one", {"title": "本地歌221", "artist": "歌手221"})
    result = bundle(db)
    assert {track["guid"] for track in result["tracks"]} == {f"lt{index}" for index in range(221, 241)}
    assert len(bundle(db, "u2")["tracks"]) == 20


def test_cache_expiration_and_passive_refresh(library, monkeypatch):
    first = bundle(library)
    calls = []
    original = localrec.select_tracks

    def select(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(localrec, "select_tracks", select)
    monkeypatch.setattr(localrec.time, "time", lambda: NOW + 1799)
    assert bundle(library) == first
    assert not calls
    monkeypatch.setattr(localrec.time, "time", lambda: NOW + 1800)
    second = bundle(library)
    assert len(calls) == 1
    assert second["builtAt"] == NOW + 1800
    assert second["guid"] == first["guid"]
    assert bundle(library) == second
    rec.purge_stale_daily_cache(hashlib.sha256(b"u1").hexdigest(), "20000101")
    assert rec.os.path.exists(cache_path())


def test_favorite_and_deletion_revalidate_cached_songs(library):
    first = bundle(library)
    excluded = first["tracks"][0]["guid"]
    edit(library, "INSERT INTO favorite_track(user_id, track_id) SELECT 1, id FROM track WHERE guid = ?", (excluded,))
    second = bundle(library)
    assert excluded not in {track["guid"] for track in second["tracks"]}
    removed = second["tracks"][0]["guid"]
    edit(library, "UPDATE track SET is_audio_file_deleted = 1 WHERE guid = ?", (removed,))
    assert removed not in {track["guid"] for track in bundle(library)["tracks"]}
    edit(library, "INSERT INTO favorite_track(user_id, track_id) SELECT 1, id FROM track WHERE id > 10")
    assert bundle(library)["tracks"] == []


def test_unknown_shared_and_database_failure_are_closed(library):
    assert bundle(library, "shared")["tracks"] == []
    assert bundle(library, "unknown")["tracks"] == []
    assert bundle(library, "")["tracks"] == []
    assert bundle(library)["tracks"]
    edit(library, "DROP TABLE favorite_track")
    assert bundle(library)["tracks"] == []


def test_concurrent_requests_build_once_and_disk_cache_survives(library, monkeypatch):
    original = localrec.select_tracks
    calls = []

    def select(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(localrec, "select_tracks", select)
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(lambda _: bundle(library), range(6)))
    assert len(calls) == 1
    assert all(result == results[0] for result in results)
    localrec._LOCKS.clear()
    assert bundle(library) == results[0]
    assert len(calls) == 1


def test_official_database_is_read_only(library, monkeypatch):
    original = sqlite3.connect
    connections = []

    def connect(path, **kwargs):
        assert path.endswith("?mode=ro")
        assert kwargs["uri"] is True
        con = original(path, **kwargs)
        connections.append(con)
        return con

    monkeypatch.setattr(localrec.sqlite3, "connect", connect)
    assert bundle(library)["tracks"]
    assert connections


def test_profile_scoring_and_exploration(library):
    edit(library, "INSERT INTO play_history(user_id, track_id, play_count, updated_at) VALUES(1, 1, 40, ?)", (NOW - 10 * 86400,))
    edit(library, "INSERT INTO favorite_track(user_id, track_id) VALUES(1, 2)")
    rows = localrec.read_library(str(library), "u1")
    profile = localrec.build_profile(rows, [], NOW)
    assert profile["artist"]["歌手1"] > 0
    assert profile["artist"]["歌手2"] > 0
    assert localrec._history_weight(40, NOW, NOW) > localrec._history_weight(1, NOW, NOW)
    assert localrec._history_weight(1, NOW, NOW) > localrec._history_weight(1, NOW - 90 * 86400, NOW)
    preferred = dict(rows[2], features=rows[0]["features"])
    neutral = dict(preferred, features=rows[3]["features"])
    assert localrec.score_track(preferred, profile, "u1", "fixed", NOW) > localrec.score_track(neutral, profile, "u1", "fixed", NOW)
    recent = dict(preferred, last_played_at=NOW)
    assert localrec.score_track(recent, profile, "u1", "fixed", NOW) < localrec.score_track(preferred, profile, "u1", "fixed", NOW)
    selected = localrec.select_tracks(rows[2:], profile, "u1", "fixed", NOW)
    assert [track["recommendDimension"] for track in selected] == ["preference"] * 16 + ["explore"] * 4
    assert selected == localrec.select_tracks(rows[2:], profile, "u1", "fixed", NOW)
    assert len({track["guid"] for track in selected}) == 20


def test_missing_metadata_cold_start_and_diversity(library):
    edit(library, "DELETE FROM track_artist")
    edit(library, "UPDATE track SET album_id = NULL, year = NULL, title = ''")
    result = bundle(library)
    assert len(result["tracks"]) == 20
    assert all(track["artists"] and track["album"]["name"] for track in result["tracks"])


def test_diversity_soft_penalties_do_not_prevent_filling(library):
    rows = localrec.read_library(str(library), "u1")
    for index, row in enumerate(rows):
        row["features"]["artist"] = ("dominant",) if index < 30 else (f"rare-{index}",)
        row["features"]["album"] = ("same",) if index < 30 else (f"album-{index}",)
    profile = localrec.build_profile([], [], NOW)
    selected = localrec.select_tracks(rows, profile, "u1", "fixed", NOW)
    assert len(selected) == 20
    rare_guids = {row["guid"] for row in rows[30:]}
    assert sum(track["guid"] in rare_guids for track in selected) >= 15
    for row in rows:
        row["features"]["artist"] = ("only-artist",)
        row["features"]["album"] = ("only-album",)
    assert len(localrec.select_tracks(rows, profile, "u1", "fixed", NOW)) == 20


def test_exploration_prefers_unplayed_then_oldest(library):
    rows = localrec.read_library(str(library), "u1")
    for index, row in enumerate(rows):
        row["last_played_at"] = NOW - (index + 1) * 86400
    profile = localrec.build_profile([], [], NOW)
    selected = localrec.select_tracks(rows, profile, "u1", "fixed", NOW)
    used = {track["guid"] for track in selected[:16]}
    expected = [row["guid"] for row in reversed(rows) if row["guid"] not in used][:4]
    assert [track["guid"] for track in selected[16:]] == expected


def test_corrupt_cache_rebuilds(library):
    bundle(library)
    with open(cache_path(), "w", encoding="utf-8") as cache_file:
        cache_file.write("{")
    assert len(bundle(library)["tracks"]) == 20
    with open(cache_path(), encoding="utf-8") as cache_file:
        assert len(json.load(cache_file)["tracks"]) == 20


def test_online_history_influences_profile(library):
    rows = localrec.read_library(str(library), "u1")
    profile = localrec.build_profile(rows, [{
        "title": "另一首", "artist": "歌手1", "genre": "rock", "playedAt": NOW,
    }], NOW)
    assert profile["artist"]["歌手1"] == 1
    assert profile["genre"]["rock"] == 1


@pytest.fixture
def client(library, monkeypatch):
    state = {"user": "u1", "writes": []}

    def handler(request):
        if request.method != "GET":
            state["writes"].append(request.url.path)
        if request.url.path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": state["user"]}})
        if request.url.path.endswith("/playlist/list"):
            return httpx.Response(200, json={"code": 0, "data": {"list": [{"guid": "official", "name": "官方"}], "total": 1}})
        if request.url.path.endswith("/static/cover"):
            return httpx.Response(200, content=b"\xff\xd8\xffcover", headers={"content-type": "image/jpeg"})
        return httpx.Response(200, json={"code": 0, "data": None})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://unix")
    monkeypatch.setattr(app.state, "upstream_client", upstream, raising=False)
    with TestClient(app) as test_client:
        yield test_client, state
    asyncio.run(upstream.aclose())


def test_client_list_detail_batch_pagination_and_readonly(client):
    test_client, state = client
    listing = test_client.get("/music/api/v1/playlist/list").json()["data"]
    assert listing["total"] == 2
    card = listing["list"][0]
    assert card["name"] == "猜你喜欢"
    guid = card["guid"]
    fake = fake_official_guid(guid)
    assert card["trackCount"] == 20
    for identifier in (guid, fake):
        detail = test_client.get("/music/api/v1/playlist/detail", params={"guid": identifier}).json()
        assert detail["data"]["trackCount"] == 20
        batch = test_client.get("/music/api/v1/playlist/batch-detail", params={"guids": identifier}).json()
        assert batch["data"]["list"][0]["trackCount"] == 20
        page = test_client.get("/music/api/v1/track/playlist-detail/list", params={"playlistGUID": identifier, "size": 7, "page": 2}).json()["data"]
        assert page["total"] == 20 and len(page["list"]) == 7
        all_tracks = test_client.get("/music/api/v1/track/playlist-detail/list", params={"playlistGUID": identifier, "size": -1}).json()["data"]
        assert len(all_tracks["list"]) == 20
    for action in ("add-track", "remove-track", "delete"):
        assert test_client.post(f"/music/api/v1/playlist/{action}", json={"guid": fake, "trackGUIDs": ["lt1"]}).json()["code"] == 0
    assert not state["writes"]


def test_client_cross_user_and_shared_never_serve_cache(client):
    test_client, state = client
    guid = rec.recommend_playlist_guid("liked", user_guid="u1")
    assert test_client.get("/music/api/v1/playlist/detail", params={"guid": guid}).json()["data"]["trackCount"] == 20
    for user in ("u2", "shared", ""):
        state["user"] = user
        response = test_client.get("/music/api/v1/track/playlist-detail/list", params={"playlistGUID": guid}).json()
        assert response["data"]["list"] == []
        assert test_client.get("/music/api/v1/playlist/detail", params={"guid": guid}).json()["data"]["trackCount"] == 0
        batch = test_client.get("/music/api/v1/playlist/batch-detail", params={"guids": guid}).json()
        assert batch["data"]["list"][0]["trackCount"] == 0
        if user in ("shared", ""):
            cards = test_client.get("/music/api/v1/playlist/list").json()["data"]["list"]
            assert all(card["name"] != "猜你喜欢" for card in cards)


def test_playlist_cover_and_no_cover_fallback(client, library, monkeypatch):
    from proxy import app as proxy_app

    monkeypatch.setattr(proxy_app, "_official_cover_file_response", lambda *args, **kwargs: None)
    test_client, state = client
    guid = rec.recommend_playlist_guid("liked", user_guid="u1")
    response = test_client.get("/music/api/v1/static/cover", params={"coverId": guid})
    assert response.content == b"\xff\xd8\xffcover"
    assert response.headers["content-type"] == "image/jpeg"
    state["user"] = "u2"
    assert test_client.get("/music/api/v1/static/cover", params={"coverId": guid}).status_code == 404
    own_guid = rec.recommend_playlist_guid("liked", user_guid="u2")
    assert test_client.get("/music/api/v1/static/cover", params={"coverId": own_guid}).status_code == 200
    state["user"] = "u1"
    edit(library, "UPDATE track SET cover_guid = NULL")
    with open(cache_path(), "w", encoding="utf-8") as cache_file:
        cache_file.write("{}")
    assert test_client.get("/music/api/v1/static/cover", params={"coverId": guid}).status_code == 200


def test_order_independent_of_other_recommendation_switches(client, monkeypatch):
    from proxy import app as proxy_app

    original = proxy_app._peek_daily_bundle

    async def peek(request, user_guid, kind="daily"):
        if kind == "liked":
            return await original(request, user_guid, kind)
        result = rec.empty_daily_bundle(user_guid, kind)
        result["tracks"] = [{"guid": "online:wy:test", "title": "test"}]
        return result

    monkeypatch.setattr(proxy_app, "_peek_daily_bundle", peek)
    monkeypatch.setitem(CONF, "recommend_daily", True)
    monkeypatch.setitem(CONF, "recommend_hot", True)
    test_client, _ = client
    cards = test_client.get("/music/api/v1/playlist/list").json()["data"]["list"]
    assert [rec.online_playlist_kind(card["guid"]) for card in cards[:3]] == ["daily", "liked", "hot"]


def test_async_loader(library):
    result = asyncio.run(localrec.get_bundle("u1", str(library)))
    assert len(result["tracks"]) == 20


def test_cache_hit_bypasses_read_library(library, monkeypatch):
    """缓存有效命中路径只做轻量校验，不触发 read_library 全库扫描。"""
    first = bundle(library)
    assert len(first["tracks"]) == 20
    assert rec.os.path.exists(cache_path())

    calls = []
    def fail_read_library(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("read_library should not be called when cache is valid")

    monkeypatch.setattr(localrec, "read_library", fail_read_library)
    cached_result = bundle(library)
    assert len(calls) == 0
    assert cached_result == first


def test_cache_invalidation_on_favorite_and_deletion(library, monkeypatch):
    """当缓存中某曲目被收藏或删除后，缓存定向校验失效，重新全量构建并剔除失效曲目。"""
    first = bundle(library)
    assert len(first["tracks"]) == 20

    target_guid = first["tracks"][0]["guid"]
    calls = []
    orig_read_library = localrec.read_library
    def counted_read_library(*args, **kwargs):
        calls.append(args)
        return orig_read_library(*args, **kwargs)

    monkeypatch.setattr(localrec, "read_library", counted_read_library)
    edit(library, "INSERT INTO favorite_track(user_id, track_id) SELECT 1, id FROM track WHERE guid = ?", (target_guid,))

    second = bundle(library)
    assert len(calls) == 1
    assert target_guid not in {t["guid"] for t in second["tracks"]}
    assert len(second["tracks"]) == 20

    calls.clear()
    delete_guid = second["tracks"][0]["guid"]
    edit(library, "UPDATE track SET is_audio_file_deleted = 1 WHERE guid = ?", (delete_guid,))

    third = bundle(library)
    assert len(calls) == 1
    assert delete_guid not in {t["guid"] for t in third["tracks"]}
    assert len(third["tracks"]) == 20
