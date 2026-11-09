"""Per-user directory, reference and organize isolation."""
from __future__ import annotations

import os
import sqlite3

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
