"""Per-user directory, reference and organize isolation."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from proxy import app as runtime
from proxy import organize
from proxy import userstorage
from proxy.userstorage import (
    auto_user_dir_name,
    auto_user_root,
    effective_user_root,
    is_auto_user_dir_name,
    load_user_dir_names,
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


def test_user_dir_names_use_username_and_owner_code(monkeypatch):
    """目录名 = 用户名.归属码；用户名只给人看，归属码取 GUID 摘要前 8 位。"""
    code = user_storage_key
    assert load_user_dir_names([
        {"guid": "user-a", "name": "张三", "role": "admin"},
        {"guid": "user-b", "name": "李 四", "role": "user"},
        {"guid": "user-c", "name": "", "role": "user"},
        {"guid": "user-d", "name": "../坏/名字", "role": "user"},
        {"guid": "shared", "name": "公共", "role": "user"},
    ], organize.safe_part) == {
        "user-a": f"张三.{code('user-a')[:8]}", "user-b": f"李 四.{code('user-b')[:8]}",
        "user-c": f"user.{code('user-c')[:8]}", "user-d": f"_坏_名字.{code('user-d')[:8]}",
    }
    # 两个在用用户的 8 位归属码相同：双方一起加长到 12 位，目录名仍不相同
    real = userstorage.user_dir_code
    monkeypatch.setattr(userstorage, "user_dir_code",
                        lambda guid, length=8: "deadbeef" if length == 8 else real(guid, length))
    assert load_user_dir_names([{"guid": "user-a", "name": "甲"}, {"guid": "user-b", "name": "乙"}],
                               organize.safe_part) == {
        "user-a": f"甲.{real('user-a', 12)}", "user-b": f"乙.{real('user-b', 12)}",
    }


def test_auto_user_root_reuses_owned_dir_until_named_dir_exists(tmp_path):
    """期望名目录存在就用它；否则按归属码沿用旧摘要目录；都没有才返回期望名。"""
    library = tmp_path / "library"
    users_dir = library / "users"
    digest = user_storage_key("user-a")
    named = users_dir / f"张三.{digest[:8]}"
    # 表里没有该用户：回退「user.归属码」，仍能按码找回旧摘要目录
    assert auto_user_dir_name("user-a") == f"user.{digest[:8]}"
    assert auto_user_root("user-a", str(library)) == str(users_dir / f"user.{digest[:8]}")
    legacy = users_dir / digest
    legacy.mkdir(parents=True)
    assert auto_user_root("user-a", str(library)) == str(legacy)
    userstorage.set_user_dir_names({"user-a": named.name})
    assert auto_user_dir_name("user-a") == named.name
    assert auto_user_root("user-a", str(library)) == str(legacy)
    legacy.rename(named)
    assert auto_user_root("user-a", str(library)) == str(named)
    code_b = user_storage_key("user-b")[:8]
    (users_dir / f"李四.{code_b}").symlink_to(named)
    # 符号链接不算归属目录：user-b 找不到自己的目录，返回期望名
    assert auto_user_root("user-b", str(library)) == str(users_dir / f"user.{code_b}")
    assert is_auto_user_dir_name(digest) and is_auto_user_dir_name(named.name)
    assert is_auto_user_dir_name(f"李四.{digest[:12]}") and is_auto_user_dir_name("a." + digest)
    assert not is_auto_user_dir_name("张三") and not is_auto_user_dir_name("a.1234567")


def test_expected_user_directory_cannot_redirect_saves_or_references(tmp_path, monkeypatch):
    library = tmp_path / "library"
    users_dir = library / "users"
    digest = user_storage_key("user-a")
    legacy = users_dir / digest
    legacy.mkdir(parents=True)
    named = users_dir / f"张三.{digest[:8]}"
    other = users_dir / user_storage_key("user-b")
    other.mkdir()
    named.symlink_to(other, target_is_directory=True)
    userstorage.set_user_dir_names({"user-a": named.name})
    cache = tmp_path / "cache"
    cache.mkdir()
    ref = cache / f"u_{digest}__song.ref"
    ref.write_text(str(legacy / "song"), encoding="utf-8")
    monkeypatch.setitem(runtime.CONF, "tee_save_by_user", True)
    monkeypatch.setitem(runtime.CONF, "tee_save_dir", str(library))
    monkeypatch.setitem(runtime.CONF, "tee_save_dir_by_user", "{}")

    assert auto_user_root("user-a", str(library)) == str(legacy)
    assert str(named) not in runtime.iter_cleanup_media_dirs()
    assert userstorage.repair_user_refs(str(cache), str(library), [{"guid": "user-a"}]) == 0
    assert ref.read_text(encoding="utf-8") == str(legacy / "song")
    legacy.rmdir()
    with pytest.raises(ValueError, match="符号链接"):
        auto_user_root("user-a", str(library))


def test_effective_user_root_prefers_custom_mapping_then_auto_root(tmp_path):
    """保存入口取目录：有自定义目录用自定义；否则走 auto_user_root（期望名目录或归属目录）。"""
    library = tmp_path / "library"
    users_dir = library / "users"
    digest = user_storage_key("user-a")
    legacy = users_dir / digest
    legacy.mkdir(parents=True)
    conf = {"tee_save_dir_by_user": {}, "cache_dir": str(tmp_path / "cache")}
    # 旧摘要目录还在、用户名表为空：按归属码沿用，不新建目录
    assert effective_user_root(conf, "user-a", str(library)) == str(legacy)
    userstorage.set_user_dir_names({"user-a": f"张三.{digest[:8]}"})
    assert effective_user_root(conf, "user-a", str(library)) == str(legacy)
    assert auto_user_root("user-a", str(library)) == str(legacy)
    legacy.rename(users_dir / f"张三.{digest[:8]}")
    assert effective_user_root(conf, "user-a", str(library)) == str(users_dir / f"张三.{digest[:8]}")
    conf["tee_save_dir_by_user"] = {"user-a": str(tmp_path / "custom")}
    assert effective_user_root(conf, "user-a", str(library)) == str(tmp_path / "custom")


def test_app_uses_named_user_dirs_and_keeps_organize_consistent(tmp_path, monkeypatch):
    """清理、整理、保存链路对同一用户给出同一个自动目录，且都认「用户名.归属码」目录。"""
    library = tmp_path / "library"
    cache = tmp_path / "cache"
    cache.mkdir()
    digest = user_storage_key("user-a")
    named = library / "users" / f"张三.{digest[:8]}"
    named.mkdir(parents=True)
    for key, value in {"tee_save_by_user": True, "tee_save_dir": str(library),
                       "tee_save_dir_by_user": "{}", "cache_dir": str(cache)}.items():
        monkeypatch.setitem(runtime.CONF, key, value)
    monkeypatch.setattr(runtime, "detect_library_dir", lambda: str(library))
    userstorage.set_user_dir_names({"user-a": named.name})
    roots = [os.path.realpath(path) for path in runtime.iter_cleanup_media_dirs()]
    assert os.path.realpath(str(library)) not in roots and os.path.realpath(str(named)) in roots
    assert runtime._organize_target("user-a")[0] == os.path.realpath(str(named))
    assert runtime.tee_save_dir(user_guid="user-a") == str(named)
    source = named / "song.flac"
    source.write_bytes(b"bytes")
    runtime.remember_media_path("song", str(source), user_guid="user-a")
    assert runtime.find_saved_user_file("song", "user-a") == str(source)


@pytest.mark.asyncio
async def test_startup_loads_user_dir_names_once_and_tolerates_unreadable_db(monkeypatch):
    """启动时读一次官方库用户表并冻结；读不到时表为空并回退「user.归属码」，不抛异常。"""
    calls = []

    def read_users(db_path=""):
        calls.append(db_path)
        return [{"guid": "user-a", "name": "张三", "role": "admin"}]

    monkeypatch.setattr(runtime, "list_music_users", read_users)
    loaded = await runtime._load_user_dir_names_on_startup()
    assert loaded == [{"guid": "user-a", "name": "张三", "role": "admin"}] and len(calls) == 1
    assert auto_user_dir_name("user-a") == f"张三.{user_storage_key('user-a')[:8]}"

    def unreadable(db_path=""):
        raise ValueError("飞牛音乐数据库不可用")

    monkeypatch.setattr(runtime, "list_music_users", unreadable)
    userstorage.set_user_dir_names({})
    assert await runtime._load_user_dir_names_on_startup() == []
    assert auto_user_dir_name("user-a") == f"user.{user_storage_key('user-a')[:8]}"


def test_migration_renames_legacy_dir_rewrites_refs_and_keeps_file_bytes(tmp_path):
    """旧摘要目录改名为「用户名.归属码」；音频与歌词字节不变，歌曲引用指向新路径。"""
    library = tmp_path / "library"
    cache = tmp_path / "cache"
    cache.mkdir()
    digest = user_storage_key("user-a")
    digest_b = user_storage_key("user-b")
    legacy = library / "users" / digest / "周杰伦" / "叶惠美"
    legacy.mkdir(parents=True)
    audio = legacy / "晴天.flac"
    audio.write_bytes(b"flac-bytes")
    lyric = legacy / "晴天.lrc"
    lyric.write_text("[00:00.00]晴天", encoding="utf-8")
    ref = cache / f"u_{digest}__song.ref"
    ref.write_text(str(legacy / "晴天"), encoding="utf-8")
    # 表里没有 user-b：它的目录与引用必须保持原样
    other = library / "users" / digest_b / "歌手"
    other.mkdir(parents=True)
    ref_b = cache / f"u_{digest_b}__other.ref"
    ref_b.write_text(str(other / "歌"), encoding="utf-8")

    userstorage.set_user_dir_names({"user-a": f"张三.{digest[:8]}"})
    result = userstorage.migrate_user_dirs(str(library), str(cache), [{"guid": "user-a", "name": "张三"}])

    named = library / "users" / f"张三.{digest[:8]}"
    assert not legacy.parent.parent.exists()
    assert named.joinpath("周杰伦", "叶惠美", "晴天.flac").read_bytes() == b"flac-bytes"
    assert named.joinpath("周杰伦", "叶惠美", "晴天.lrc").read_text(encoding="utf-8") == "[00:00.00]晴天"
    assert ref.read_text(encoding="utf-8") == str(named / "周杰伦" / "叶惠美" / "晴天")
    assert ref_b.read_text(encoding="utf-8") == str(other / "歌") and other.is_dir()
    assert [item["guid"] for item in result["migrated"]] == ["user-a"]
    assert result["conflicts"] == [] and result["rewritten_refs"] == 1


def test_migration_follows_user_rename_and_is_idempotent(tmp_path):
    """用户改名后按归属码找回旧名目录并改名；重复执行不再有任何动作。"""
    library = tmp_path / "library"
    cache = tmp_path / "cache"
    cache.mkdir()
    digest = user_storage_key("user-a")
    old = library / "users" / f"张三.{digest[:8]}" / "专辑"
    old.mkdir(parents=True)
    old.joinpath("歌.flac").write_bytes(b"song")
    userstorage.set_user_dir_names({"user-a": f"李四.{digest[:8]}"})
    users = [{"guid": "user-a", "name": "李四"}]

    first = userstorage.migrate_user_dirs(str(library), str(cache), users)

    renamed = library / "users" / f"李四.{digest[:8]}"
    assert not old.parent.exists()
    assert renamed.joinpath("专辑", "歌.flac").read_bytes() == b"song"
    assert len(first["migrated"]) == 1 and first["conflicts"] == []
    assert userstorage.migrate_user_dirs(str(library), str(cache), users) == {
        "migrated": [], "conflicts": [], "deferred": [], "rewritten_refs": 0}


def test_migration_leaves_conflicts_and_deleted_users_untouched(tmp_path):
    """目标名已被占用、同一用户有多个归属目录、用户已删除：一律不动文件，只记冲突。"""
    library = tmp_path / "library"
    cache = tmp_path / "cache"
    cache.mkdir()
    users_dir = library / "users"
    code_a = user_storage_key("user-a")[:8]
    legacy_a = users_dir / user_storage_key("user-a")
    legacy_a.mkdir(parents=True)
    legacy_a.joinpath("a.flac").write_bytes(b"a")
    # 目标名被一个普通文件占用
    (users_dir / f"张三.{code_a}").write_text("not a directory")
    # 用户已删除：目录保持原样
    gone = users_dir / user_storage_key("user-gone")
    gone.mkdir()
    gone.joinpath("gone.flac").write_bytes(b"gone")
    # 另一个在用用户有两个归属目录，无法判断该用哪个
    digest_c = user_storage_key("user-c")
    code_c = digest_c[:8]
    (users_dir / digest_c).mkdir()
    (users_dir / f"旧名.{code_c}").mkdir()
    userstorage.set_user_dir_names({"user-a": f"张三.{code_a}", "user-c": f"李四.{code_c}"})

    result = userstorage.migrate_user_dirs(str(library), str(cache), [
        {"guid": "user-a", "name": "张三"}, {"guid": "user-c", "name": "李四"}])

    assert legacy_a.joinpath("a.flac").read_bytes() == b"a"
    assert (users_dir / f"张三.{code_a}").read_text(encoding="utf-8") == "not a directory"
    assert gone.joinpath("gone.flac").read_bytes() == b"gone"
    assert (users_dir / digest_c).is_dir() and (users_dir / f"旧名.{code_c}").is_dir()
    assert not (users_dir / f"李四.{code_c}").exists()
    assert result["migrated"] == [] and result["rewritten_refs"] == 0
    assert sorted(result["conflicts"]) == sorted(
        [str(legacy_a), str(users_dir / digest_c), str(users_dir / f"旧名.{code_c}")])


def test_migration_leaves_reference_on_original_file_when_destination_is_occupied(tmp_path):
    library = tmp_path / "library"
    cache = tmp_path / "cache"
    cache.mkdir()
    digest = user_storage_key("user-a")
    legacy = library / "users" / digest
    named = library / "users" / f"张三.{digest[:8]}"
    legacy.mkdir(parents=True)
    named.mkdir()
    (legacy / "song.flac").write_bytes(b"original song")
    (named / "song.flac").write_bytes(b"different song")
    ref = cache / f"u_{digest}__song.ref"
    ref.write_text(str(legacy / "song"), encoding="utf-8")
    userstorage.set_user_dir_names({"user-a": named.name})

    result = userstorage.migrate_user_dirs(str(library), str(cache), [{"guid": "user-a"}])

    assert result["conflicts"] == [str(legacy)]
    assert result["rewritten_refs"] == 0
    assert ref.read_text(encoding="utf-8") == str(legacy / "song")


def test_migration_repairs_stale_refs_after_interrupted_run(tmp_path):
    """目录已是新名而引用还指旧路径（上次中途退出）：只修引用，不动文件。"""
    library = tmp_path / "library"
    cache = tmp_path / "cache"
    cache.mkdir()
    digest = user_storage_key("user-a")
    named_rel = os.path.join("users", f"张三.{digest[:8]}", "叶惠美", "晴天")
    audio = library.joinpath(named_rel).with_suffix(".flac")
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"x")
    ref = cache / f"u_{digest}__song.ref"
    ref.write_text(os.path.join(str(library), "users", digest, "叶惠美", "晴天"), encoding="utf-8")
    userstorage.set_user_dir_names({"user-a": f"张三.{digest[:8]}"})

    result = userstorage.migrate_user_dirs(str(library), str(cache), [{"guid": "user-a", "name": "张三"}])

    assert result["migrated"] == [] and result["rewritten_refs"] == 1
    assert ref.read_text(encoding="utf-8") == os.path.join(str(library), named_rel)
    assert audio.read_bytes() == b"x"


@pytest.mark.asyncio
async def test_startup_migration_records_result_and_requests_library_scan(tmp_path, monkeypatch):
    """启动迁移：改名、改写引用、请求官方扫描；结果进内存状态与主目录记录文件。"""
    library = tmp_path / "library"
    cache = tmp_path / "cache"
    home = tmp_path / "home"
    for directory in (cache, home):
        directory.mkdir()
    digest = user_storage_key("user-a")
    legacy = library / "users" / digest
    legacy.mkdir(parents=True)
    legacy.joinpath("song.flac").write_bytes(b"s")
    ref = cache / f"u_{digest}__song.ref"
    ref.write_text(str(legacy / "song"), encoding="utf-8")
    # 第二个用户同样改名：两人用的是同一套扫描配置，扫描只应请求一次
    legacy_b = library / "users" / user_storage_key("user-b")
    legacy_b.mkdir(parents=True)
    for key, value in {"tee_save_dir": str(library), "tee_save_dir_by_user": "{}",
                       "cache_dir": str(cache), "tee_save_by_user": True,
                       "scan_admin_username": "admin", "scan_admin_password": "secret"}.items():
        monkeypatch.setitem(runtime.CONF, key, value)
    monkeypatch.setattr(runtime, "detect_library_dir", lambda: str(library))
    monkeypatch.setattr(runtime, "_HOME", str(home))
    monkeypatch.setattr(runtime, "list_music_users", lambda db_path="": [
        {"guid": "user-a", "name": "张三", "role": "admin"}, {"guid": "user-b", "name": "李四", "role": "user"}])
    monkeypatch.setattr(runtime, "_scan_config", lambda user_guid="": ("/music/api/v1/shared-library/scan", "{}"))
    requested = []
    monkeypatch.setattr(runtime, "_schedule_library_scan",
                        lambda headers, user_guid="": requested.append((headers, user_guid)))
    monkeypatch.setattr(runtime, "_USER_DIR_MIGRATION", {})
    # 官方曲库没有登记这两个目录下的歌曲：改名不需要飞牛音乐配合
    monkeypatch.setattr(runtime, "_official_audio_bindings", lambda old_name, new_name="": (set(), set()))

    await runtime._migrate_user_dirs_on_startup(await runtime._load_user_dir_names_on_startup())

    named = library / "users" / f"张三.{digest[:8]}"
    assert named.joinpath("song.flac").read_bytes() == b"s" and not legacy.exists()
    assert ref.read_text(encoding="utf-8") == str(named / "song")
    assert (library / "users" / f"李四.{user_storage_key('user-b')[:8]}").is_dir()
    assert requested == [(None, "user-a")]
    status = runtime._user_storage_status([])["dir_migration"]
    assert [item["guid"] for item in status["migrated"]] == ["user-a", "user-b"]
    assert status["conflicts"] == [] and status["deferred"] == [] and status["scan_requested"] is True
    saved = json.loads((home / "user_dir_migration.json").read_text(encoding="utf-8"))
    assert [item["to"] for item in saved["records"]] == [item["to"] for item in status["migrated"]]


@pytest.mark.asyncio
async def test_startup_migration_reports_manual_scan_without_credentials(tmp_path, monkeypatch):
    """扫描路径需要用户凭证时，启动时发不出扫描请求：不排入任务，状态标记为需要手动扫描。"""
    library = tmp_path / "library"
    home = tmp_path / "home"
    home.mkdir()
    legacy = library / "users" / user_storage_key("user-a")
    legacy.mkdir(parents=True)
    for key, value in {"tee_save_dir": str(library), "tee_save_dir_by_user": "{}",
                       "cache_dir": str(tmp_path / "cache"), "tee_save_by_user": True,
                       "scan_admin_username": "", "scan_admin_password": ""}.items():
        monkeypatch.setitem(runtime.CONF, key, value)
    monkeypatch.setattr(runtime, "detect_library_dir", lambda: str(library))
    monkeypatch.setattr(runtime, "_HOME", str(home))
    monkeypatch.setattr(runtime, "list_music_users",
                        lambda db_path="": [{"guid": "user-a", "name": "张三", "role": "user"}])
    monkeypatch.setattr(runtime, "_scan_config", lambda user_guid="": ("/other/scan", "{}"))
    requested = []
    monkeypatch.setattr(runtime, "_schedule_library_scan",
                        lambda headers, user_guid="": requested.append((headers, user_guid)))
    monkeypatch.setattr(runtime, "_USER_DIR_MIGRATION", {})
    monkeypatch.setattr(runtime, "_official_audio_bindings", lambda old_name, new_name="": (set(), set()))

    await runtime._migrate_user_dirs_on_startup(await runtime._load_user_dir_names_on_startup())

    assert requested == []
    status = runtime._user_storage_status([])["dir_migration"]
    assert [item["guid"] for item in status["migrated"]] == ["user-a"]
    assert status["scan_requested"] is False


def test_migration_defers_or_rolls_back_when_official_library_cannot_follow(tmp_path):
    """旧目录的歌在官方曲库有登记：飞牛音乐没运行就推迟改名；改名后官方库没跟着换路径就改回原名。"""
    library = tmp_path / "library"
    users_dir = library / "users"
    digest_a = user_storage_key("user-a")
    digest_b = user_storage_key("user-b")
    legacy_a = users_dir / digest_a
    legacy_b = users_dir / digest_b
    for legacy in (legacy_a, legacy_b):
        legacy.mkdir(parents=True)
        legacy.joinpath("song.flac").write_bytes(b"bytes")
    userstorage.set_user_dir_names({"user-a": f"张三.{digest_a[:8]}", "user-b": f"李四.{digest_b[:8]}"})
    users = [{"guid": "user-a", "name": "张三"}, {"guid": "user-b", "name": "李四"}]

    class Official:
        """测试用的官方曲库保护回调：记录调用，按预设回答。"""

        def __init__(self, rename_ok, follow_ok):
            self.rename_ok, self.follow_ok, self.calls = rename_ok, follow_ok, []

        def can_rename(self, old_path):
            self.calls.append(("can_rename", old_path))
            return self.rename_ok

        def followed(self, old_path, new_path):
            self.calls.append(("followed", old_path, new_path))
            return self.follow_ok

    # 飞牛音乐没运行：不改名，记入 deferred，不算冲突
    result = userstorage.migrate_user_dirs(str(library), "", users, official=Official(False, True))
    assert result == {"migrated": [], "conflicts": [], "deferred": [str(legacy_a), str(legacy_b)], "rewritten_refs": 0}
    assert legacy_a.is_dir() and legacy_b.is_dir()

    # 改名后官方库没跟着更新：改回原名，文件原样保留，记入冲突
    official = Official(True, False)
    result = userstorage.migrate_user_dirs(str(library), "", users, official=official)
    assert result["migrated"] == [] and result["deferred"] == []
    assert sorted(result["conflicts"]) == sorted([str(legacy_a), str(legacy_b)])
    assert legacy_a.joinpath("song.flac").read_bytes() == b"bytes"
    assert legacy_b.is_dir() and not (users_dir / f"张三.{digest_a[:8]}").exists()
    assert ("followed", str(legacy_a), str(users_dir / f"张三.{digest_a[:8]}")) in official.calls

    # 官方库跟着更新了：改名成立
    result = userstorage.migrate_user_dirs(str(library), "", users, official=Official(True, True))
    assert [item["guid"] for item in result["migrated"]] == ["user-a", "user-b"]
    assert not legacy_a.exists() and (users_dir / f"张三.{digest_a[:8]}").joinpath("song.flac").is_file()


def test_official_bindings_follow_rename_only_while_music_service_runs(tmp_path, monkeypatch):
    """官方曲库登记查询：有登记且飞牛音乐没运行时推迟改名；改名后登记没换到新路径就判定未跟随。"""
    db_path = tmp_path / "music.db"
    library = tmp_path / "library"
    legacy = library / "users" / user_storage_key("user-a")
    named = library / "users" / f"张三.{user_storage_key('user-a')[:8]}"
    fresh = library / "users" / user_storage_key("user-b")
    with sqlite3.connect(db_path) as con:
        con.execute("CREATE TABLE audio_file (id INTEGER PRIMARY KEY, path TEXT UNIQUE, is_physical_file_deleted INTEGER)")
        con.execute("CREATE TABLE track (id INTEGER PRIMARY KEY, audio_file_id INTEGER, is_audio_file_deleted INTEGER)")
        con.executemany("INSERT INTO audio_file (path, is_physical_file_deleted) VALUES (?, ?)", [
            (str(legacy / "专辑" / "歌.flac"), 0), (str(legacy / "歌_2.flac"), 0),
            # 已标记丢失的登记、别的目录里的同名歌曲、别的用户目录：都不算这个目录的登记
            (str(legacy / "已丢失.flac"), 1), (str(library / "download" / "歌.flac"), 0),
            (str(library / "users" / (legacy.name + "x") / "歌.flac"), 0),
        ])
    monkeypatch.setitem(runtime.CONF, "music_db", str(db_path))
    reachable = {"value": False}
    monkeypatch.setattr(runtime, "_official_music_reachable", lambda: reachable["value"])

    bindings = runtime._OfficialLibraryBindings(wait_s=0)
    # 这个目录登记了 2 首歌
    assert runtime._official_audio_bindings(legacy.name, named.name) == (
        {("专辑/歌.flac", 1, None), ("歌_2.flac", 2, None)}, set())
    assert bindings.can_rename(str(fresh)) is True       # 没有登记：不需要飞牛音乐配合
    assert bindings.can_rename(str(legacy)) is False     # 有登记、飞牛音乐没运行：推迟
    reachable["value"] = True
    assert bindings.can_rename(str(legacy)) is True
    assert bindings.followed(str(legacy), str(named)) is False   # 登记还在旧路径：未跟随
    # 官方库没跟随过一次：本次启动不再尝试其它有登记的目录；没有登记的目录不受影响
    assert bindings.can_rename(str(legacy)) is False and bindings.can_rename(str(fresh)) is True

    # 官方库把登记换到新路径（目录监听的原位更新）：同一批歌曲，只是路径前缀变了
    following = runtime._OfficialLibraryBindings(wait_s=0)
    assert following.can_rename(str(legacy))
    with sqlite3.connect(db_path) as con:
        con.execute("UPDATE audio_file SET path = ? || substr(path, ?) WHERE is_physical_file_deleted = 0 AND path >= ? AND path < ?",
                    (str(named) + "/", len(str(legacy)) + 2, str(legacy) + "/", str(legacy) + "0"))
    assert following.followed(str(legacy), str(named)) is True

    # 官方库读不了：无法确认，按未确认处理
    monkeypatch.setitem(runtime.CONF, "music_db", str(tmp_path / "missing.db"))
    unknown = runtime._OfficialLibraryBindings(wait_s=0)
    assert unknown.can_rename(str(legacy)) is False
    assert unknown.followed(str(legacy), str(named)) is False


@pytest.mark.asyncio
async def test_startup_migration_rolls_back_when_official_library_does_not_follow(tmp_path, monkeypatch):
    """官方曲库没跟着改名：启动迁移把目录改回原名，记入冲突，不请求扫描。"""
    library = tmp_path / "library"
    home = tmp_path / "home"
    home.mkdir()
    legacy = library / "users" / user_storage_key("user-a")
    legacy.mkdir(parents=True)
    legacy.joinpath("song.flac").write_bytes(b"s")
    for key, value in {"tee_save_dir": str(library), "tee_save_dir_by_user": "{}",
                       "cache_dir": str(tmp_path / "cache"), "tee_save_by_user": True,
                       "scan_admin_username": "admin", "scan_admin_password": "secret"}.items():
        monkeypatch.setitem(runtime.CONF, key, value)
    monkeypatch.setattr(runtime, "detect_library_dir", lambda: str(library))
    monkeypatch.setattr(runtime, "_HOME", str(home))
    monkeypatch.setattr(runtime, "list_music_users",
                        lambda db_path="": [{"guid": "user-a", "name": "张三", "role": "admin"}])
    monkeypatch.setattr(runtime, "_scan_config", lambda user_guid="": ("/music/api/v1/shared-library/scan", "{}"))
    monkeypatch.setattr(runtime, "_official_music_reachable", lambda: True)
    # 官方曲库登记了 1 首歌，改名后仍留在旧路径；等待时间设为 0，用例不必真的等 20 秒
    real_bindings = runtime._OfficialLibraryBindings
    monkeypatch.setattr(runtime, "_OfficialLibraryBindings", lambda: real_bindings(wait_s=0))
    monkeypatch.setattr(runtime, "_official_audio_bindings",
                        lambda old_name, new_name="": ({("song.flac", 1, 1)}, set()))
    requested = []
    monkeypatch.setattr(runtime, "_schedule_library_scan",
                        lambda headers, user_guid="": requested.append((headers, user_guid)))
    monkeypatch.setattr(runtime, "_USER_DIR_MIGRATION", {})

    await runtime._migrate_user_dirs_on_startup(await runtime._load_user_dir_names_on_startup())

    assert legacy.joinpath("song.flac").read_bytes() == b"s"
    assert not (library / "users" / f"张三.{user_storage_key('user-a')[:8]}").exists()
    assert requested == []
    status = runtime._user_storage_status([])["dir_migration"]
    assert status["migrated"] == [] and status["conflicts"] == [str(legacy)] and status["deferred"] == []
    saved = json.loads((home / "user_dir_migration.json").read_text(encoding="utf-8"))
    assert [item["path"] for item in saved["conflicts"]] == [str(legacy)]


@pytest.mark.parametrize("recreated_track", [False, True])
def test_migration_requires_same_file_and_track_ids_after_rename(tmp_path, monkeypatch, recreated_track):
    db_path = tmp_path / "music.db"
    users_dir = tmp_path / "library" / "users"
    digest = user_storage_key("user-a")
    legacy = users_dir / digest
    named = users_dir / f"张三.{digest[:8]}"
    with sqlite3.connect(db_path) as con:
        con.execute("CREATE TABLE audio_file (id INTEGER PRIMARY KEY, path TEXT, is_physical_file_deleted INTEGER)")
        con.execute("CREATE TABLE track (id INTEGER PRIMARY KEY, audio_file_id INTEGER, is_audio_file_deleted INTEGER)")
        con.execute("INSERT INTO audio_file VALUES (3, ?, 0)", (str(legacy / "song.flac"),))
        con.execute("INSERT INTO track VALUES (10, 3, 0)")
    monkeypatch.setitem(runtime.CONF, "music_db", str(db_path))
    monkeypatch.setattr(runtime, "_official_music_reachable", lambda: True)
    bindings = runtime._OfficialLibraryBindings(wait_s=0)
    assert bindings.can_rename(str(legacy))
    with sqlite3.connect(db_path) as con:
        con.execute("UPDATE audio_file SET path = ?", (str(named / "song.flac"),))
        if recreated_track:
            con.execute("UPDATE track SET id = 11")
    assert bindings.followed(str(legacy), str(named)) is not recreated_track


@pytest.mark.asyncio
async def test_library_scan_accepts_admin_session_without_user_credentials(monkeypatch):
    """管理员扫描路径用管理员会话登录：没有用户请求头也排入扫描；其它情况保持必须有请求头。"""
    ran = []

    async def fake_worker(initial_wait, user_guid=""):
        ran.append(user_guid)

    monkeypatch.setattr(runtime, "_run_scan_worker", fake_worker)
    monkeypatch.setattr(runtime, "_scan_config", lambda user_guid="": (runtime._ADMIN_SCAN_PATH, "{}"))
    monkeypatch.setitem(runtime.CONF, "scan_admin_username", "admin")
    monkeypatch.setitem(runtime.CONF, "scan_admin_password", "secret")
    try:
        runtime._schedule_library_scan(None, user_guid="user-a")
        await asyncio.sleep(0)
        assert ran == ["user-a"]
        ran.clear()
        runtime._scan_state_by_user.clear()
        # 未配置管理员账号：没有请求头就不排入
        monkeypatch.setitem(runtime.CONF, "scan_admin_password", "")
        runtime._schedule_library_scan(None, user_guid="user-b")
        await asyncio.sleep(0)
        assert ran == []
        # 非管理员扫描路径：即使配置了管理员账号，没有请求头也不排入
        monkeypatch.setitem(runtime.CONF, "scan_admin_password", "secret")
        monkeypatch.setattr(runtime, "_scan_config", lambda user_guid="": ("/other/scan", "{}"))
        runtime._schedule_library_scan(None, user_guid="user-c")
        await asyncio.sleep(0)
        assert ran == []
    finally:
        runtime._scan_state_by_user.clear()
