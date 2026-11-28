"""Per-user directory, reference and organize isolation."""
from __future__ import annotations

import asyncio
import os
import sqlite3
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from proxy import app as runtime
from proxy import organize
from proxy.userstorage import (
    effective_user_root,
    merge_user_map,
    parse_user_map,
    user_ref_name,
    user_storage_key,
    validate_effective_roots,
)


def test_user_map_patch_merge_and_ref_name_are_stable():
    existing = parse_user_map('{"user-a": "/vol1/music/a"}', kind="下载目录映射")
    patch = parse_user_map({"user-a": None, "user-b": "/vol1/music/b"}, kind="下载目录映射", patch=True)
    merged = merge_user_map(existing, patch)
    assert "user-a" not in merged
    assert merged["user-b"].endswith(os.path.join("vol1", "music", "b"))
    scans = parse_user_map({"user-b": ""}, kind="曲库重扫映射", patch=True)
    assert scans["user-b"] == ""
    with pytest.raises(ValueError):
        parse_user_map({"user-a": "  "}, kind="下载目录映射")
    with pytest.raises(ValueError):
        parse_user_map({"shared": "/vol1/music/a"}, kind="下载目录映射")
    with pytest.raises(ValueError):
        parse_user_map({"user-a": "relative/music"}, kind="下载目录映射")
    with pytest.raises(ValueError):
        parse_user_map({"user-a": "/vol1/music/../other"}, kind="下载目录映射")
    name = user_ref_name("user-a", "song")
    assert name.startswith("u_") and name.endswith("__song.ref")
    assert user_storage_key("user-a") == user_storage_key("user-a")
    assert user_storage_key("user-a") != user_storage_key("user-b")


def test_effective_roots_reject_overlap_but_allow_distinct_children():
    global_root = os.path.abspath("/vol1/music")
    alice = os.path.join(global_root, "alice")
    bob = os.path.join(global_root, "bob")
    validate_effective_roots({"cache_dir": "/vol1/cache"}, {"a": alice, "b": bob}, global_root)
    with pytest.raises(ValueError):
        validate_effective_roots({"cache_dir": "/vol1/cache"}, {"a": alice, "b": os.path.join(alice, "nested")}, global_root)
    with pytest.raises(ValueError):
        validate_effective_roots({"cache_dir": "/vol1/cache"}, {"a": global_root}, global_root)
    with pytest.raises(ValueError):
        validate_effective_roots(
            {"cache_dir": "/vol1/cache"},
            {"a": os.path.join(global_root, "users", "ab" * 32)},
            global_root,
        )


def test_user_save_does_not_reuse_another_users_file(tmp_path, monkeypatch):
    library = tmp_path / "library"
    cache = tmp_path / "cache"
    library.mkdir()
    cache.mkdir()
    monkeypatch.setitem(runtime.CONF, "tee_save_by_user", True)
    monkeypatch.setitem(runtime.CONF, "tee_save_dir", str(library))
    monkeypatch.setitem(runtime.CONF, "tee_save_dir_by_user", "{}")
    monkeypatch.setitem(runtime.CONF, "cache_dir", str(cache))
    monkeypatch.setitem(runtime.CONF, "download_layout", "flat")
    monkeypatch.setattr(runtime, "detect_library_dir", lambda: str(library))
    monkeypatch.setattr(runtime, "download_subdir", lambda root, artist="", album="": root)
    song = "online:netease:same"
    owner_root = runtime.tee_save_dir(user_guid="user-a")
    source = os.path.join(owner_root, "same.flac")
    with open(source, "wb") as handle:
        handle.write(b"owner-bytes")
    runtime.remember_media_path(song, source, user_guid="user-a")
    runtime.remember_media_path(song, source)
    assert runtime.find_saved_user_file(song, "user-a") == source
    assert runtime.find_saved_user_file(song, "user-b") is None
    other = runtime.library_media_path(song, "晴天", "flac", artist="周杰伦", user_guid="user-b")
    other_root = runtime.tee_save_dir(user_guid="user-b")
    assert os.path.realpath(other).startswith(os.path.realpath(other_root))
    assert os.path.realpath(other) != os.path.realpath(source)
    with open(source, "rb") as handle:
        assert handle.read() == b"owner-bytes"
    with pytest.raises(runtime.UserStorageError):
        runtime.tee_save_dir(user_guid="shared")
    monkeypatch.setitem(runtime.CONF, "tee_save_by_user", False)
    assert runtime.tee_save_dir(user_guid="user-b") == str(library)


def test_user_save_rejects_overlapping_mappings_before_creating_directories(tmp_path, monkeypatch):
    library = tmp_path / "library"
    cache = tmp_path / "cache"
    library.mkdir()
    cache.mkdir()
    shared = tmp_path / "shared"
    monkeypatch.setitem(runtime.CONF, "tee_save_by_user", True)
    monkeypatch.setitem(runtime.CONF, "tee_save_dir", str(library))
    monkeypatch.setitem(runtime.CONF, "cache_dir", str(cache))
    monkeypatch.setitem(runtime.CONF, "tee_save_dir_by_user", {
        "user-a": str(shared), "user-b": str(shared / "nested"),
    })
    with pytest.raises(runtime.UserStorageError):
        runtime.tee_save_dir(user_guid="user-a")
    assert not shared.exists()


@pytest.fixture
def isolated_playback_storage(tmp_path, monkeypatch):
    library = tmp_path / "library"
    cache = tmp_path / "cache"
    for key, value in {
        "tee_save_enabled": False,
        "tee_save_by_user": True,
        "tee_save_dir": str(library),
        "tee_save_dir_by_user": "{}",
        "cache_dir": str(cache),
        "music_db": str(tmp_path / "missing.db"),
        "fav_auto_bind": True,
        "netease_direct_play": False,
        "lyric_auto_dl": False,
    }.items():
        monkeypatch.setitem(runtime.CONF, key, value)
    monkeypatch.setattr(runtime, "_bind_pending", {})
    monkeypatch.setattr(runtime, "_ensure_user_save_tasks", {})
    monkeypatch.setattr(runtime, "_full_fetch_tasks", {})
    monkeypatch.setattr(runtime, "_schedule_library_scan", Mock())
    monkeypatch.setattr(runtime, "resolve_local_play", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "_tee_metadata_fallback", lambda *args, **kwargs: ("晴天", "周杰伦", "叶惠美"))
    return library, cache


@pytest.mark.asyncio
async def test_tee_off_user_storage_replays_rolling_cache_without_saving(
        isolated_playback_storage, monkeypatch, asgi_audio):
    library, cache = isolated_playback_storage
    resolve_user = AsyncMock(return_value="user-a")
    monkeypatch.setattr(runtime, "_resolve_storage_user", resolve_user)
    guid = "online:netease:issue156-user-storage"
    payload = b"M" * 4096
    headers = {"cookie": "sid=test"}
    initial = runtime.stream_tee_response(
        httpx.Response(200, content=payload), guid, None, resolved_ext="mp3",
        scan_headers_factory=lambda: headers,
    )
    assert await asgi_audio(initial) == payload
    assert (cache / f"{runtime.cache_safe_guid(guid)}.mp3").read_bytes() == payload
    assert not list(library.rglob("*.mp3"))
    request = runtime.Request({
        "type": "http", "method": "GET", "path": "/music/api/v1/track/stream",
        "query_string": f"guid={guid}".encode(), "headers": [(b"cookie", b"sid=test")],
        "app": runtime.app,
    })
    replay = await runtime.stream_track(request)
    copy_tasks = tuple(runtime._ensure_user_save_tasks.values())
    assert b"".join([chunk async for chunk in replay.body_iterator]) == payload
    await asyncio.gather(*copy_tasks)
    assert not list(library.rglob("*.mp3"))
    assert not copy_tasks and not runtime._full_fetch_tasks
    resolve_user.assert_not_awaited()
    runtime._schedule_library_scan.assert_not_called()


@pytest.mark.asyncio
async def test_user_copy_rechecks_tee_after_identity_lookup(isolated_playback_storage, monkeypatch):
    library, cache = isolated_playback_storage
    guid = "online:netease:issue156-inflight-user"
    cache.mkdir()
    (cache / f"{runtime.cache_safe_guid(guid)}.mp3").write_bytes(b"M" * 4096)
    monkeypatch.setitem(runtime.CONF, "tee_save_enabled", True)

    async def resolve_user(headers):
        monkeypatch.setitem(runtime.CONF, "tee_save_enabled", False)
        return "user-a"

    monkeypatch.setattr(runtime, "_resolve_storage_user", resolve_user)
    runtime._ensure_user_saved(guid, {"cookie": "sid=test"})
    tasks = tuple(runtime._ensure_user_save_tasks.values())
    assert len(tasks) == 1
    await asyncio.gather(*tasks)
    assert not list(library.rglob("*.mp3"))
    assert not runtime._full_fetch_tasks
    runtime._schedule_library_scan.assert_not_called()


def test_organize_selects_one_user_root_and_cleanup_skips_public_parent(tmp_path, monkeypatch):
    library = tmp_path / "library"
    cache = tmp_path / "cache"
    library.mkdir()
    cache.mkdir()
    monkeypatch.setitem(runtime.CONF, "tee_save_by_user", True)
    monkeypatch.setitem(runtime.CONF, "tee_save_dir", str(library))
    monkeypatch.setitem(runtime.CONF, "tee_save_dir_by_user", "{}")
    monkeypatch.setitem(runtime.CONF, "cache_dir", str(cache))
    monkeypatch.setitem(runtime.CONF, "download_layout", "artist")
    with pytest.raises(ValueError, match="必须选择"):
        runtime._organize_target()
    auto = effective_user_root(runtime.CONF, "user-a", str(library))
    os.makedirs(auto)
    root, layout = runtime._organize_target("user-a")
    assert root == os.path.realpath(auto)
    assert layout == "artist"
    assert os.path.realpath(library) not in runtime.iter_cleanup_media_dirs()
    assert os.path.realpath(auto) in [os.path.realpath(path) for path in runtime.iter_cleanup_media_dirs()]
    other = library / "users" / user_storage_key("user-b")
    other.mkdir(parents=True)
    (other / "kept.flac").write_bytes(b"b")
    assert os.path.realpath(other) != root


def test_extension_metadata_reads_user_reference(tmp_path):
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"x")
    ref_name = user_ref_name("user-a", "songguid")
    ref = tmp_path / ref_name
    runtime_stub = type("Runtime", (), {
        "_ONLINE_META": {"songguid": {"artist": "周杰伦", "album": "叶惠美"}},
        "cache_safe_guid": lambda self, value: value,
    })()
    artist, album = organize._extension_metadata(
        runtime_stub, str(audio), [(str(ref), str(audio.with_suffix("")))])
    assert (artist, album) == ("周杰伦", "叶惠美")


def test_official_track_match_uses_path_when_tags_are_identical(tmp_path, monkeypatch):
    db_path = tmp_path / "music.db"
    with sqlite3.connect(db_path) as con:
        con.executescript("""
            CREATE TABLE track (
                id INTEGER PRIMARY KEY, guid TEXT, title TEXT, album_id INTEGER,
                audio_file_id INTEGER, is_audio_file_deleted INTEGER, is_admin_deleted INTEGER,
                is_cue INTEGER
            );
            CREATE TABLE artist (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE track_artist (track_id INTEGER, artist_id INTEGER);
            CREATE TABLE album (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE audio_file (
                id INTEGER PRIMARY KEY, path TEXT, size INTEGER, is_physical_file_deleted INTEGER
            );
        """)
        con.execute("INSERT INTO artist (id, name) VALUES (1, '周杰伦')")
        con.execute("INSERT INTO album (id, name) VALUES (1, '叶惠美')")
        con.execute("INSERT INTO audio_file (id, path, size, is_physical_file_deleted) VALUES (1, '/music/a/song.flac', 10, 0)")
        con.execute("INSERT INTO audio_file (id, path, size, is_physical_file_deleted) VALUES (2, '/music/b/song.flac', 10, 0)")
        con.execute("INSERT INTO track VALUES (10, 'guid-a', '晴天', 1, 1, 0, 0, 0)")
        con.execute("INSERT INTO track VALUES (11, 'guid-b', '晴天', 1, 2, 0, 0, 0)")
        con.execute("INSERT INTO track_artist VALUES (10, 1)")
        con.execute("INSERT INTO track_artist VALUES (11, 1)")
    monkeypatch.setitem(runtime.CONF, "music_db", str(db_path))
    assert runtime.official_track_guid_by_tags("晴天", "周杰伦", "叶惠美", "/music/b/song.flac") == "guid-b"
    missing = runtime._lookup_official_track_guid(
        "晴天", "周杰伦", "叶惠美", "/music/missing.flac", allow_tag_fallback=False)
    assert missing[0] is None
