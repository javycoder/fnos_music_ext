"""本地封面兜底：官方封面资源缺失时按 cover_guid 反查官方库音频文件提取内嵌封面。

背景：代理落盘的新歌官方扫描器尚未生成封面资源文件（或文件无内嵌图时官方
收录失败），官方 App 列表对该 coverId 请求封面 → 代理官方文件直读未命中 →
透传官方必 400（无效资源 id）。修复：透传前按 guid 反查官方库 audio_file.path，
mutagen 提取内嵌专辑图直接出图。
"""
import importlib
import os
import shutil
import sqlite3
import subprocess

import pytest
from starlette.testclient import TestClient

p = importlib.import_module("proxy.app")

JPEG = b"\xFF\xD8\xFF" + b"fake-jpeg-body" + b"\xFF\xD9"


def make_audio(tmp_path, name, *codec_args):
    """ffmpeg 生成 0.2s 静音样本（无 ffmpeg 环境 skip，与 test_merge 同约定）。"""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    path = str(tmp_path / name)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "0.2", *codec_args, path],
        check=True, capture_output=True,
    )
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("cache_dir", "library_dir", "fav_dir"):
        monkeypatch.setitem(p.CONF, key, str(tmp_path / key))
    monkeypatch.setitem(p.CONF, "tee_save_dir", "")
    monkeypatch.setattr(p, "_TEE_SAVE_DIR_WARNED", False)
    p._LOCAL_COVER_CACHE.clear()
    yield tmp_path
    p._LOCAL_COVER_CACHE.clear()


def make_official_db(tmp_path, cover_guid, track_guid=None, audio_path=None):
    """造一个最小官方库：track(cover_guid) → audio_file(path)。"""
    db = tmp_path / "music.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE track (id INTEGER PRIMARY KEY, guid TEXT, cover_guid TEXT,
            is_audio_file_deleted INTEGER DEFAULT 0, is_admin_deleted INTEGER DEFAULT 0,
            is_cue INTEGER DEFAULT 0, audio_file_id INTEGER);
        CREATE TABLE audio_file (id INTEGER PRIMARY KEY, path TEXT,
            is_physical_file_deleted INTEGER DEFAULT 0);
    """)
    tg = track_guid or ("t" * 32)
    con.execute("INSERT INTO audio_file (id, path) VALUES (1, ?)",
                (audio_path or "/nonexistent/missing.flac",))
    con.execute("INSERT INTO track (guid, cover_guid, audio_file_id) VALUES (?, ?, 1)",
                (tg, cover_guid))
    con.commit()
    con.close()
    return str(db)


def test_local_cover_extracts_embedded_from_official_db(env, monkeypatch):
    """官方封面文件缺失 + 官方库 cover_guid 命中 + 文件有内嵌图 → 直接出图。"""
    cover_guid = "a" * 32
    audio = make_audio(env, "sample.mp3", "-q:a", "9")
    assert p.embed_audio_cover(audio, JPEG, "image/jpeg") is True
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", make_official_db(env, cover_guid, audio_path=audio))
    monkeypatch.setattr(p, "_OFFICIAL_COVER_ROOT_CACHE", None)
    monkeypatch.setenv("FNMUSIC_COVER_DIR", str(env / "no_covers"))  # 官方封面目录不存在

    resp = p._local_cover_response(cover_guid)
    assert resp is not None
    assert resp.body == JPEG
    assert "jpeg" in resp.media_type


def test_local_cover_accepts_track_prefix(env, monkeypatch):
    """track_ 前缀形态的 coverId 同样反查（官方惯例两种形态并存）。"""
    cover_guid = "b" * 32
    audio = make_audio(env, "sample.flac", "-c:a", "flac")
    assert p.embed_audio_cover(audio, JPEG, "image/jpeg") is True
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", make_official_db(env, cover_guid, audio_path=audio))

    assert p._local_cover_response(f"track_{cover_guid}") is not None


def test_local_cover_negative_cache_no_embedded(env, monkeypatch):
    """官方库命中但文件无内嵌图：阴性缓存，二次调用不重复提取。"""
    cover_guid = "c" * 32
    audio = make_audio(env, "bare.mp3", "-q:a", "9")  # 不嵌封面
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", make_official_db(env, cover_guid, audio_path=audio))

    calls = []
    real = p._extract_embedded_cover

    def spy(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(p, "_extract_embedded_cover", spy)
    assert p._local_cover_response(cover_guid) is None
    assert p._local_cover_response(cover_guid) is None
    assert len(calls) == 1  # 阴性缓存生效
    assert p._LOCAL_COVER_CACHE[cover_guid] is None


def test_local_cover_db_missing_returns_none(env, monkeypatch):
    """官方库不可用：返回 None，调用方走原透传链路（不比现状差）。"""
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(env / "missing.db"))
    assert p._local_audio_path_by_cover_guid("d" * 32) is None
    assert p._local_cover_response("d" * 32) is None


def test_local_cover_deleted_row_ignored(env, monkeypatch):
    """已删除行（软删/物理文件删除）不参与反查。"""
    cover_guid = "e" * 32
    db = env / "music.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE track (id INTEGER PRIMARY KEY, guid TEXT, cover_guid TEXT,
            is_audio_file_deleted INTEGER DEFAULT 0, is_admin_deleted INTEGER DEFAULT 0,
            is_cue INTEGER DEFAULT 0, audio_file_id INTEGER);
        CREATE TABLE audio_file (id INTEGER PRIMARY KEY, path TEXT,
            is_physical_file_deleted INTEGER DEFAULT 0);
    """)
    con.execute("INSERT INTO audio_file (id, path) VALUES (1, '/gone.flac')")
    con.execute("INSERT INTO track (guid, cover_guid, audio_file_id) VALUES (?, ?, 1)",
                ("f" * 32, cover_guid))
    con.execute("UPDATE track SET is_audio_file_deleted = 1 WHERE cover_guid = ?", (cover_guid,))
    con.commit()
    con.close()
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(db))
    assert p._local_audio_path_by_cover_guid(cover_guid) is None


def test_local_cover_rejects_non_hex(env):
    """非 32hex 的 coverId 不走本地兜底（交回原链路处理）。"""
    assert p._local_cover_response("not-a-guid") is None
    assert p._local_cover_response("") is None


def test_static_cover_endpoint_returns_embedded(env, monkeypatch):
    """端到端：/music/api/v1/static/cover?coverId= 对官方资源缺失的 guid
    不再透传 400，而是内嵌封面直接出图。"""
    from fastapi import FastAPI

    cover_guid = "9" * 32
    audio = make_audio(env, "e2e.mp3", "-q:a", "9")
    assert p.embed_audio_cover(audio, JPEG, "image/jpeg") is True
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", make_official_db(env, cover_guid, audio_path=audio))
    monkeypatch.setattr(p, "_OFFICIAL_COVER_ROOT_CACHE", None)
    monkeypatch.setenv("FNMUSIC_COVER_DIR", str(env / "no_covers"))

    # 官方库不可达的透传链路不能被触发：mock forward 抛错以暴露回归
    async def boom(*a, **k):
        raise AssertionError("unresolved coverId must not be forwarded upstream")

    monkeypatch.setattr(p, "forward_to_upstream", boom)

    app = FastAPI()
    app.router.routes.extend(
        r for r in p.app.router.routes
        if getattr(r, "path", "").startswith("/music/api/v1/static/cover"))
    client = TestClient(app)
    r = client.get(f"/music/api/v1/static/cover?coverId={cover_guid}&size=160")
    assert r.status_code == 200
    assert r.content == JPEG
