"""任务 3.3/3.4 离线单测：歌单文件导入解析（m3u/pls/cue）+ 去重合并。

纯标准库，不触网、不读真实仓库；自定义目录与推荐缓存目录均隔离到 tmp_path。
"""
import importlib.util
import os
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, BASE / "proxy" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


custompl = _load("customplaylists")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_CUSTOM_PLAYLISTS_DIR", str(tmp_path / "custom"))
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "recommend_cache"))
    monkeypatch.setenv("FNMUSIC_HOME", str(tmp_path))
    return tmp_path


def test_parse_m3u_extinf_and_skip_count():
    content = "#EXTM3U\n#EXTINF:123,周杰伦 - 晴天\n/path/晴天.mp3\n\nbadline-noext\n#EXTINF:200, - \n/only.mp3\n"
    tracks, skipped = custompl.parse_playlist_file(content, "a.m3u")
    assert tracks == [
        {"title": "晴天", "artist": "周杰伦"},
        {"title": "badline-noext", "artist": ""},
        {"title": "only", "artist": ""},
    ]
    assert skipped == 1


def test_parse_m3u8_bom_and_cue():
    content = "\ufeff#EXTM3U\n#EXTINF:200,林俊杰 - 江南\n/x/江南.flac\n"
    tracks, skipped = custompl.parse_playlist_file(content, "b.m3u8")
    assert tracks == [{"title": "江南", "artist": "林俊杰"}]
    assert skipped == 0
    cue = 'PERFORMER "周杰伦"\nFILE "x.wav" WAVE\n  TRACK 01 AUDIO\n    TITLE "七里香"\n  TRACK 02 AUDIO\n    TITLE ""\n'
    tracks2, skipped2 = custompl.parse_playlist_file(cue, "c.cue")
    assert tracks2 == [{"title": "七里香", "artist": "周杰伦"}]
    assert skipped2 == 1


def test_parse_pls_titles_and_files():
    content = "[playlist]\nFile1=/x/夜曲.mp3\nTitle1=周杰伦 - 夜曲\nFile2=/x/no.mp3\nNumberOfEntries=2\n"
    tracks, skipped = custompl.parse_playlist_file(content, "d.pls")
    assert tracks == [{"title": "夜曲", "artist": "周杰伦"}, {"title": "no", "artist": ""}]
    assert skipped == 0


def test_parse_empty_and_unknown_ext():
    tracks, skipped = custompl.parse_playlist_file("", "e.m3u")
    assert tracks == [] and skipped == 0
    tracks2, skipped2 = custompl.parse_playlist_file("周杰伦 - 晴天\n", "f.txt")
    assert tracks2 == [{"title": "晴天", "artist": "周杰伦"}]
    assert skipped2 == 0


def test_dedupe_merge_case_insensitive_and_empty():
    base = [{"title": "晴天", "artist": "周杰伦"}, {"title": "", "artist": "x"}]
    extra = [{"title": "晴天", "artist": "周杰伦"}, {"title": "  晴天  ", "artist": "周杰伦 "},
             {"title": "夜曲", "artist": "周杰伦"}]
    assert custompl.dedupe_merge(base, extra) == [
        {"title": "晴天", "artist": "周杰伦"},
        {"title": "夜曲", "artist": "周杰伦"},
    ]


def test_create_list_load_enable_delete(isolated):
    data = custompl.create_definition("我的歌单", [{"title": "晴天", "artist": "周杰伦"}], source="manual")
    pid = data["id"]
    items = custompl.list_definitions()
    assert len(items) == 1 and items[0]["id"] == pid and items[0]["trackCount"] == 1
    loaded = custompl.load_definition(pid)
    assert loaded and loaded["name"] == "我的歌单" and loaded["enabled"] is True
    updated = custompl.set_enabled(pid, False)
    assert updated and updated["enabled"] is False
    guid = custompl.playlist_guid(pid)
    assert custompl.id_from_guid(guid) == pid
    assert custompl.is_custom_playlist_guid(guid)
    assert custompl.delete_definition(pid) is True
    assert custompl.load_definition(pid) is None
    assert custompl.list_definitions() == []


def test_delete_missing_is_idempotent(isolated):
    assert custompl.delete_definition("notexistid1") is True


def test_purge_custom_cache_files(isolated, tmp_path):
    root = tmp_path / "recommend_cache" / "user1"
    root.mkdir(parents=True)
    (root / "custom-abc123-20261103.json").write_text("{}", encoding="utf-8")
    (root / "custom-abc123-20261103.fallback.json").write_text("{}", encoding="utf-8")
    (root / "daily-20261103.json").write_text("{}", encoding="utf-8")
    removed = custompl.purge_custom_cache_files("abc123")
    assert removed == 2
    assert (root / "daily-20261103.json").exists()
    (root / "custom-xyz-20261103.json").write_text("{}", encoding="utf-8")
    removed_all = custompl.purge_custom_cache_files(None)
    assert removed_all == 1
