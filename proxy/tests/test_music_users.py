"""Official music roster, migration and per-user authorization regression tests."""
import importlib
import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from proxy import publicplaylists as public
from proxy import recommend
from proxy.env_merge import parse_env_file

music = importlib.import_module("proxy.app")
SELECTION = [{"provider": "musicbox", "source": "netease", "kind": "chart", "id": "3778678"}]


def test_roster_excludes_deleted_and_reports_failure(tmp_path):
    db = tmp_path / "music.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE user(id INTEGER, guid TEXT, name TEXT, role TEXT, status TEXT)")
        con.executemany("INSERT INTO user VALUES(?,?,?,?,?)", [
            (1, "a", "甲", "admin", "active"),
            (2, "b", "乙", "user", "active"),
            (3, "gone", "删除", "user", "deleted"),
        ])
    assert music.list_music_users(str(db)) == [
        {"guid": "a", "name": "甲", "role": "admin"},
        {"guid": "b", "name": "乙", "role": "user"},
    ]
    with pytest.raises(ValueError, match="不可用"):
        music.list_music_users(str(tmp_path / "missing"))
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM user")
    assert music.list_music_users(str(db)) == []


def test_migration_retry_idempotence_and_new_user_empty(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    original = "FNMUSIC_PUBLIC_PLAYLISTS_SELECTION='" + json.dumps(SELECTION) + "'\nCUSTOM='keep'\n"
    path.write_text(original)
    monkeypatch.setattr(music, "_env_watch_path", lambda: str(path))
    monkeypatch.setattr(music, "list_music_users", lambda: (_ for _ in ()).throw(ValueError("unavailable")))
    with pytest.raises(ValueError):
        music.migrate_public_playlist_users()
    assert path.read_text() == original
    users = [{"guid": "a"}, {"guid": "b"}]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: music.migrate_public_playlist_users(users), range(2)))
    values = dict(parse_env_file(str(path))[0])
    assert values["CUSTOM"] == "keep"
    mapping = json.loads(values["FNMUSIC_PUBLIC_PLAYLISTS_BY_USER"])
    assert mapping == {"a": SELECTION, "b": SELECTION}
    music.migrate_public_playlist_users(users + [{"guid": "new"}])
    assert dict(parse_env_file(str(path))[0]) == values
    conf = {"public_playlists_enabled": True, "public_playlists_by_user": json.dumps(mapping),
            "netease_enabled": True}
    assert public.configured_selection(conf, "new") == []
    assert public.selection_allows(conf, "a", SELECTION[0])
    assert not public.selection_allows(conf, "new", SELECTION[0])
    assert not public.selection_allows(conf, "", SELECTION[0])


def test_private_guid_uses_whole_identity_and_rejects_foreign():
    a, b = "same-prefix-123456-A", "same-prefix-123456-B"
    for kind in ("daily", "liked"):
        guid = recommend.recommend_playlist_guid(kind, user_guid=a)
        assert guid != recommend.recommend_playlist_guid(kind, user_guid=b)
        assert music._recommend_request_owned(a, kind, guid)
        assert not music._recommend_request_owned(b, kind, guid)
        assert not music._recommend_request_owned("", kind, guid)


def test_failed_migration_write_keeps_legacy_for_retry(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    original = "FNMUSIC_PUBLIC_PLAYLISTS_SELECTION='[]'\n"
    path.write_text(original)
    monkeypatch.setattr(music, "_env_watch_path", lambda: str(path))
    writer = music.write_env_atomic
    monkeypatch.setattr(music, "write_env_atomic", lambda *_: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        music.migrate_public_playlist_users([{"guid": "a"}])
    assert path.read_text() == original
    monkeypatch.setattr(music, "write_env_atomic", writer)
    music.migrate_public_playlist_users([{"guid": "a"}])
    assert json.loads(dict(parse_env_file(path)[0])["FNMUSIC_PUBLIC_PLAYLISTS_BY_USER"]) == {"a": []}


def test_legacy_short_identity_cache_is_rebuilt(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path))
    monkeypatch.setenv("FNMUSIC_LLM_DAILY_ENABLED", "false")
    day = recommend.today_key()
    recommend.save_daily_cache("a", day, {
        "day": day, "guid": f"online:playlist:daily:{day}:a",
        "status": "ready", "tracks": [{"guid": "old"}],
    })
    assert recommend.load_daily_cache("a", day) is None


def test_users_endpoint_reports_total_and_query_error(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("FNMUSIC_PUBLIC_PLAYLISTS_SELECTION='[]'\n")
    monkeypatch.setattr(music, "_env_watch_path", lambda: str(path))
    monkeypatch.setattr(music, "CONF", dict(music.CONF))
    monkeypatch.setattr(music, "list_music_users", lambda: [{"guid": "a", "name": "甲", "role": "admin"}])
    monkeypatch.setattr(music, "apply_env_hot_reload",
                        lambda: (_ for _ in ()).throw(AssertionError("unrelated settings must use watcher")))
    status, data = asyncio.run(music._maintenance_request("GET", "/api/maintenance/music-users", {}))
    assert status == 200 and data["total"] == 1
    assert data["public_playlists_by_user"] == {"a": []}
    monkeypatch.setattr(music, "list_music_users", lambda: (_ for _ in ()).throw(ValueError("DB offline")))
    status, data = asyncio.run(music._maintenance_request("GET", "/api/maintenance/music-users", {}))
    assert status == 503 and data["ok"] is False
    assert "users" not in data


def test_daily_strategy_invalidates_primary_and_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path))
    monkeypatch.setenv("FNMUSIC_LLM_DAILY_ENABLED", "false")
    day = recommend.today_key()
    payload = {"day": day, "status": "ready", "tracks": [{"guid": "track"}], "strategy": "source"}
    recommend.save_daily_cache("a", day, payload)
    import shutil
    shutil.copyfile(recommend.cache_path("a", day), recommend.fallback_cache_path("a", day))
    assert recommend.load_daily_cache("a", day)
    assert recommend.load_fallback_daily_cache("a", day)
    monkeypatch.setenv("FNMUSIC_LLM_DAILY_ENABLED", "true")
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "http://test/v1")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "test")
    assert recommend.daily_strategy() == "llm-personal"
    assert recommend.load_daily_cache("a", day) is None
    assert recommend.load_fallback_daily_cache("a", day) is None
