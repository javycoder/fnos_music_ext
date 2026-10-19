"""本地优先播放专项：在线歌曲统一"匹配本地曲库 → 本地播放 → 失败回退在线"。

覆盖规格要求的行为：
- 每日推荐/公共歌单（磁盘缓存针）首播即命中本地，零在线请求；重启后仍命中。
- 搜索会话/已登记元数据、播放历史快照同规则；metadata/audio-info 用本地规格。
- 大小写/空白/专辑差异可命中；不同歌手、版本文字差异、歌手集合不完整不误配。
- 文件缺失/空文件/损坏音轨/数据库缺失回退在线；故障指纹 5 分钟跳过、文件
  变化后重试；本地中途失败后 Range 偏移不套用到在线文件。
- 标准音质转码本地直喂 ffmpeg；起播失败清理会话换在线输入重建；本地会话
  不触发重复整轨下载；HLS 分片序列不跨音频版本混供。
"""
import asyncio
import json
import os
import re
import sqlite3
import time
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

import proxy.app as appmod
import proxy.localmatch as localmatch
from proxy import transcode as tc
from proxy.app import CONF, app

GUID_KUWO = "online:kuwo:228908"
GUID_WY = "online:netease:99123"
ONLINE_MP3 = b"ID3ONLINEMPDATAX" * 64

# 假 ffmpeg：ARGLOG 记录参数；FAIL_FIRST_ATTEMPT=1 时首次调用失败（模拟本地
# 文件起播失败、第二次在线输入重建成功）；正常按 -hls_segment_filename 产片。
FAKE_FFMPEG_SRC = '''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
log = os.environ.get("FAKE_FFMPEG_ARGLOG", "")
count_file = os.environ.get("FAKE_FFMPEG_COUNT", "")
attempt = 0
if count_file and os.path.exists(count_file):
    try:
        attempt = int(open(count_file).read().strip() or "0")
    except ValueError:
        attempt = 0
if count_file:
    with open(count_file, "w") as f:
        f.write(str(attempt + 1))
if log:
    with open(log, "a") as f:
        f.write(" ".join(args) + "\\n")
if os.environ.get("FAKE_FFMPEG_FAIL_FIRST_ATTEMPT") == "1" and attempt == 0:
    sys.exit(1)
if "-hls_segment_filename" in args:
    pattern = args[args.index("-hls_segment_filename") + 1]
    segdir = os.path.dirname(os.path.abspath(pattern))
    os.makedirs(segdir, exist_ok=True)
    with open(os.path.join(segdir, "init.mp4"), "wb") as f:
        f.write(b"ftypinit")
    n = int(os.environ.get("FAKE_FFMPEG_SEGMENTS", "3"))
    for i in range(n):
        with open(os.path.join(segdir, "%05d.m4s" % i), "wb") as f:
            f.write(b"moofseg" + str(i).encode())
    with open(args[-1], "w") as f:
        f.write("#EXTM3U\\n")
sys.exit(0)
'''


def _make_wav(path, seconds: float) -> bytes:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x01" * int(8000 * seconds))
    with open(path, "rb") as f:
        return f.read()


def _make_db(db_path: str, rows: list[dict]) -> None:
    """rows: {id, guid, title, album, artists, path, suffix, size, duration_ms,
    is_cue, is_audio_file_deleted, is_admin_deleted, is_physical_file_deleted}"""
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE track (id INTEGER PRIMARY KEY, guid TEXT, title TEXT,
          album_id INTEGER, audio_file_id INTEGER, duration_ms INTEGER,
          is_cue INTEGER DEFAULT 0, is_audio_file_deleted INTEGER DEFAULT 0,
          is_admin_deleted INTEGER DEFAULT 0);
        CREATE TABLE audio_file (id INTEGER PRIMARY KEY, path TEXT, suffix TEXT,
          size INTEGER, is_physical_file_deleted INTEGER DEFAULT 0);
        CREATE TABLE album (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE artist (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE track_artist (track_id INTEGER, artist_id INTEGER);
    """)
    albums, artists = {}, {}
    for row in rows:
        af_flags = int(row.get("is_physical_file_deleted") or 0)
        con.execute("INSERT INTO audio_file VALUES (?,?,?,?,?)",
                    (row["id"], row["path"], row.get("suffix") or "wav",
                     row.get("size") or 0, af_flags))
        album_id = None
        if row.get("album"):
            if row["album"] not in albums:
                albums[row["album"]] = len(albums) + 1
                con.execute("INSERT INTO album VALUES (?,?)",
                            (albums[row["album"]], row["album"]))
            album_id = albums[row["album"]]
        con.execute(
            "INSERT INTO track VALUES (?,?,?,?,?,?,?,?,?)",
            (row["id"], row.get("guid") or f"g{row['id']}", row["title"], album_id,
             row["id"], int(row.get("duration_ms") or 0), int(row.get("is_cue") or 0),
             int(row.get("is_audio_file_deleted") or 0),
             int(row.get("is_admin_deleted") or 0)))
        for name in row.get("artists") or []:
            if name not in artists:
                artists[name] = len(artists) + 1
                con.execute("INSERT INTO artist VALUES (?,?)", (artists[name], name))
            con.execute("INSERT INTO track_artist VALUES (?,?)",
                        (row["id"], artists[name]))
    con.commit()
    con.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离目录 + 假官方库 + 在线源 mock（带调用计数）。"""
    for store in (appmod._SEARCH_CACHE, appmod._ONLINE_META, appmod._full_fetch_tasks,
                  appmod._full_fetch_failed, appmod._tee_active, appmod._tee_handoff_active,
                  appmod._DL_TASKS, appmod._TAGS_LOOKUP_MISS):
        store.clear()
    localmatch.reset_caches()

    dirs = {key: str(tmp_path / key) for key in
            ("cache", "library", "favorites", "history", "recommend", "db")}
    for value in dirs.values():
        os.makedirs(value, exist_ok=True)
    monkeypatch.setitem(CONF, "cache_dir", dirs["cache"])
    monkeypatch.setitem(CONF, "library_dir", dirs["library"])
    monkeypatch.setitem(CONF, "fav_dir", dirs["favorites"])
    monkeypatch.setitem(CONF, "tee_save_dir", dirs["library"])
    monkeypatch.setitem(CONF, "music_db", os.path.join(dirs["db"], "music.db"))
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "lx_enabled", False)
    monkeypatch.setitem(CONF, "tee_save_enabled", False)
    monkeypatch.setitem(CONF, "lyric_field", "data.lyric")
    monkeypatch.setitem(CONF, "transcode_enabled", True)
    monkeypatch.setitem(CONF, "transcode_bitrate", "128k")
    monkeypatch.setitem(CONF, "transcode_hls_time", 10.0)
    monkeypatch.setitem(CONF, "transcode_max_sessions", 2)
    monkeypatch.setitem(CONF, "transcode_ttl_s", 90.0)
    monkeypatch.setitem(CONF, "transcode_cache_max_mb", 512)
    monkeypatch.setitem(CONF, "musicdl_url", "http://127.0.0.1:8768")
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", dirs["recommend"])
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", dirs["history"])
    monkeypatch.setenv("FNMUSIC_PUBLIC_PLAYLISTS_DIR", str(tmp_path / "public"))
    monkeypatch.setattr(appmod, "_HOME", str(tmp_path))

    state = {"musicdl": [], "musicbox": []}

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        state["musicdl"].append({"path": request.url.path,
                                 "range": request.headers.get("range")})
        if request.url.path == "/info":
            return httpx.Response(200, json={
                "ok": True, "id": "kuwo:228908", "source": "kuwo",
                "title": "晴天", "artist": "周杰伦", "album": "叶惠美",
                "duration_s": 269, "ext": "mp3", "lyric": ""})
        if request.url.path == "/stream":
            return httpx.Response(200, content=ONLINE_MP3,
                                  headers={"Content-Type": "audio/mpeg"})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"code": 0})),
        base_url="http://unix")
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: (state["musicbox"].append({"path": r.url.path}),
                       httpx.Response(404, json={"ok": False}))[1]),
        base_url="http://127.0.0.1:8770")
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8772")

    state["dirs"] = dirs
    state["tmp"] = str(tmp_path)
    yield state


def _seed_library_song(env, *, title="晴天", artists=("周杰伦",), album="叶惠美",
                       seconds=2.0, name="song", corrupt=False, missing=False):
    """建库+建文件；返回 (path, wav_bytes)。corrupt 写假字节，missing 只留库记录。"""
    db = CONF["music_db"]
    path = os.path.join(env["dirs"]["library"], f"{name}.wav")
    wav_bytes = b""
    if corrupt:
        with open(path, "wb") as f:
            f.write(b"not audio at all" * 64)
    elif not missing:
        wav_bytes = _make_wav(path, seconds)
    size = os.path.getsize(path) if (corrupt or not missing) else 12345
    _make_db(db, [{
        "id": 1, "title": title, "artists": list(artists), "album": album,
        "path": path, "suffix": "wav", "size": size,
        "duration_ms": int(seconds * 1000),
    }])
    return path, wav_bytes


def _write_recommend_cache(env, guid, title="晴天", artist="周杰伦", album="叶惠美"):
    user_dir = os.path.join(env["dirs"]["recommend"], "u1")
    os.makedirs(user_dir, exist_ok=True)
    with open(os.path.join(user_dir, "daily-20260101.json"), "w", encoding="utf-8") as f:
        json.dump({"tracks": [{
            "guid": guid, "title": title, "artist": artist, "album": album,
            "duration_s": 269, "ext": "mp3",
        }]}, f, ensure_ascii=False)


# === 播放链路：本地命中 ===

def test_daily_recommend_first_play_hits_local_without_online(env):
    """每日推荐 guid 首播即命中本地：无收藏/历史/.ref 依赖，在线请求次数为零。"""
    _, wav_bytes = _seed_library_song(env)
    _write_recommend_cache(env, GUID_KUWO)
    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/track/stream?guid={GUID_KUWO}")
    assert resp.status_code == 200
    assert resp.content == wav_bytes
    assert resp.headers["content-type"].startswith("audio/wav")
    assert resp.headers["content-length"] == str(len(wav_bytes))
    assert env["musicdl"] == [] and env["musicbox"] == []


def test_local_hit_survives_restart_via_disk_needle(env):
    """重启后（内存会话/LRU/匹配缓存全空）靠磁盘缓存仍命中本地。"""
    _, wav_bytes = _seed_library_song(env)
    _write_recommend_cache(env, GUID_KUWO)
    with TestClient(app) as client:
        first = client.get(f"/music/api/v1/track/stream?guid={GUID_KUWO}")
    assert first.status_code == 200 and first.content == wav_bytes
    # 模拟重启：清空进程内全部状态，只剩磁盘缓存与曲库
    for store in (appmod._SEARCH_CACHE, appmod._ONLINE_META):
        store.clear()
    localmatch.reset_caches()
    with TestClient(app) as client:
        second = client.get(f"/music/api/v1/track/stream?guid={GUID_KUWO}")
    assert second.status_code == 200 and second.content == wav_bytes
    assert env["musicdl"] == [] and env["musicbox"] == []


def test_play_history_needle_and_range_head(env):
    """播放历史快照作针：GET Range 走本地 206、HEAD 回本地真实大小。"""
    _, wav_bytes = _seed_library_song(env, seconds=3.0)
    with open(os.path.join(env["dirs"]["history"], "u1.json"), "w", encoding="utf-8") as f:
        json.dump({"items": [{
            "guid": GUID_KUWO, "playedAt": time.time(),
            "track": {"guid": GUID_KUWO, "title": "晴天", "artist": "周杰伦",
                      "album": "叶惠美"},
        }]}, f, ensure_ascii=False)
    with TestClient(app) as client:
        head = client.head(f"/music/api/v1/track/stream?guid={GUID_KUWO}")
        rng = client.get(f"/music/api/v1/track/stream?guid={GUID_KUWO}",
                         headers={"Range": "bytes=100-"})
    assert head.status_code == 200
    assert head.headers["content-length"] == str(len(wav_bytes))
    assert rng.status_code == 206
    assert rng.content == wav_bytes[100:]
    assert env["musicdl"] == [] and env["musicbox"] == []


def test_metadata_uses_local_specs_and_skips_online_detail(env):
    """metadata/audio-info 用本地格式/大小/时长；网易直链预取与详情请求跳过。"""
    wav_path, _ = _seed_library_song(env, seconds=2.0)
    appmod.register_online_meta_one(GUID_WY, "晴天", "周杰伦", "叶惠美", 269)
    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/track/metadata?guid={GUID_WY}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["duration"] == 2000
    assert data["audioSpec"]["size"] == os.path.getsize(wav_path)
    assert data["audioSpec"]["format"] == "wav"
    assert env["musicbox"] == []   # 无取链预取、无在线详情
    assert env["musicdl"] == []


def test_search_session_needle_hits_local(env):
    """搜索会话/已登记元数据作针（内存路径）。"""
    _, wav_bytes = _seed_library_song(env)
    appmod.register_online_meta_one(GUID_KUWO, "晴天", "周杰伦", "叶惠美", 269)
    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/track/stream?guid={GUID_KUWO}")
    assert resp.status_code == 200 and resp.content == wav_bytes
    assert env["musicdl"] == [] and env["musicbox"] == []


# === 匹配规则 ===

def test_match_tolerates_case_and_whitespace_but_not_identity(env):
    """大小写/空白/全角可命中；不同歌手、版本文字、歌手集合不完整不误配。"""
    db = CONF["music_db"]
    latin = os.path.join(env["dirs"]["library"], "latin.wav")
    _make_wav(latin, 1.0)
    _make_db(db, [
        {"id": 1, "title": "Hello  World", "artists": ["Jay Chou"],
         "album": "", "path": latin, "suffix": "wav",
         "size": os.path.getsize(latin), "duration_ms": 1000},
    ])
    # 针大小写与空白差异可命中（NFKC+casefold+折叠）
    hit = localmatch.resolve_in_library(db, "hello world", "JAY CHOU")
    assert hit and hit["path"] == latin
    assert localmatch.resolve_in_library(db, " Ｈｅｌｌｏ  world ", "jay chou") is not None
    # 版本文字差异（Live）不命中
    assert localmatch.resolve_in_library(db, "Hello World (Live)", "Jay Chou") is None
    # 不同歌手 / 歌手集合不完整不命中
    assert localmatch.resolve_in_library(db, "Hello World", "Someone Else") is None
    assert localmatch.resolve_in_library(db, "Hello World", "Jay Chou/Other") is None


def test_match_prefers_same_album_then_latest_id(env):
    db = CONF["music_db"]
    a = os.path.join(env["dirs"]["library"], "a.wav")
    b = os.path.join(env["dirs"]["library"], "b.wav")
    _make_wav(a, 1.0)
    _make_wav(b, 1.0)
    _make_db(db, [
        {"id": 1, "title": "晴天", "artists": ["周杰伦"], "album": "",
         "path": a, "suffix": "wav", "size": os.path.getsize(a), "duration_ms": 1000},
        {"id": 2, "title": "晴天", "artists": ["周杰伦"], "album": "叶惠美",
         "path": b, "suffix": "wav", "size": os.path.getsize(b), "duration_ms": 1000},
    ])
    same = localmatch.resolve_in_library(db, "晴天", "周杰伦", "叶惠美")
    assert same["path"] == b and same["select_reason"] == "album"
    # 专辑不匹配任何候选时按官方记录 ID 降序（规格）
    no_album = localmatch.resolve_in_library(db, "晴天", "周杰伦", "无关专辑")
    assert no_album["path"] == b and no_album["select_reason"] == "latest"


def test_cue_and_soft_deleted_and_missing_files_never_match(env):
    db = CONF["music_db"]
    good = os.path.join(env["dirs"]["library"], "good.wav")
    _make_wav(good, 1.0)
    _make_db(db, [
        {"id": 1, "title": "七里香", "artists": ["周杰伦"], "path": good,
         "is_cue": 1, "size": os.path.getsize(good), "duration_ms": 1000},
        {"id": 2, "title": "稻香", "artists": ["周杰伦"], "path": good,
         "is_admin_deleted": 1, "size": os.path.getsize(good), "duration_ms": 1000},
        {"id": 3, "title": "搁浅", "artists": ["周杰伦"], "path": good,
         "is_audio_file_deleted": 1, "size": os.path.getsize(good), "duration_ms": 1000},
        {"id": 4, "title": "夜曲", "artists": ["周杰伦"], "path": good,
         "is_physical_file_deleted": 1, "size": os.path.getsize(good), "duration_ms": 1000},
        {"id": 5, "title": "发如雪", "artists": ["周杰伦"],
         "path": os.path.join(env["dirs"]["library"], "ghost.wav"),
         "size": 100, "duration_ms": 1000},
    ])
    for title in ("七里香", "稻香", "搁浅", "夜曲", "发如雪"):
        assert localmatch.resolve_in_library(db, title, "周杰伦") is None, title


# === 失败回退 ===

def test_corrupt_audio_falls_back_online_with_fault_skip_and_recovery(env):
    """损坏音轨回退在线；5 分钟故障窗口内跳过本地；文件修复（指纹变化）恢复本地。"""
    path, _ = _seed_library_song(env, corrupt=True)
    _write_recommend_cache(env, GUID_KUWO)
    probe_calls = []
    real_probe = localmatch.probe_audio

    def counting_probe(match):
        probe_calls.append(match["path"])
        return real_probe(match)

    with TestClient(app) as client:
        first = client.get(f"/music/api/v1/track/stream?guid={GUID_KUWO}")
        assert first.status_code == 200 and first.content == ONLINE_MP3
        assert [c for c in env["musicdl"] if c["path"] == "/stream"]
        assert localmatch.fault_active(GUID_KUWO, {"path": path,
               "size": os.path.getsize(path), "mtime": os.path.getmtime(path)})
        # 故障窗口内：不再探测本地，直接在线
        monkey_probe = localmatch.probe_audio
        localmatch.probe_audio = counting_probe
        try:
            env["musicdl"].clear()
            second = client.get(f"/music/api/v1/track/stream?guid={GUID_KUWO}")
            assert second.status_code == 200 and second.content == ONLINE_MP3
            assert probe_calls == []   # 未再探测（fault 跳过）
        finally:
            localmatch.probe_audio = monkey_probe
        # 文件修复（大小/修改时间变化）→ 立即允许重试 → 恢复本地优先
        wav_bytes = _make_wav(path, 4.0)
        env["musicdl"].clear()
        third = client.get(f"/music/api/v1/track/stream?guid={GUID_KUWO}")
    assert third.status_code == 200 and third.content == wav_bytes
    assert env["musicdl"] == []


def test_missing_file_and_db_absent_fall_back_online(env):
    """文件缺失与数据库不可用都不阻断：直接在线。"""
    _seed_library_song(env, missing=True)
    _write_recommend_cache(env, GUID_KUWO)
    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/track/stream?guid={GUID_KUWO}")
        assert resp.status_code == 200 and resp.content == ONLINE_MP3
        # 数据库缺失（路径不存在）
        CONF["music_db"] = os.path.join(env["tmp"], "nope", "music.db")
        localmatch.reset_caches()
        resp2 = client.get(f"/music/api/v1/track/stream?guid={GUID_KUWO}")
    assert resp2.status_code == 200 and resp2.content == ONLINE_MP3


def test_stale_local_range_not_applied_to_online(env):
    """本地中途失败后：非零起始 Range 不套用到在线文件（重起播），bytes=0- 放行。"""
    _, wav_bytes = _seed_library_song(env)
    _write_recommend_cache(env, GUID_KUWO)
    with TestClient(app) as client:
        first = client.get(f"/music/api/v1/track/stream?guid={GUID_KUWO}")
        assert first.status_code == 200
        # 模拟本地字节已供出后中途失败（error_hook 语义）：mark + 同指纹故障
        match = {"path": os.path.join(env["dirs"]["library"], "song.wav"),
                 "size": len(wav_bytes), "mtime": os.path.getmtime(
                     os.path.join(env["dirs"]["library"], "song.wav"))}
        localmatch.record_fault(GUID_KUWO, match, "test mid-read")
        env["musicdl"].clear()
        resumed = client.get(f"/music/api/v1/track/stream?guid={GUID_KUWO}",
                             headers={"Range": "bytes=100-"})
    assert resumed.status_code == 200           # 200 全量，而非带错位偏移的 206
    assert resumed.content == ONLINE_MP3
    streamed = [c for c in env["musicdl"] if c["path"] == "/stream"]
    assert streamed and streamed[0]["range"] is None
    # bytes=0- 无害，原样转发在线（不带本地偏移语义）
    assert appmod._sanitize_stale_local_range(GUID_KUWO, "bytes=0-") == "bytes=0-"
    assert appmod._sanitize_stale_local_range(GUID_KUWO, None) is None
    # Once online bytes have been served, later seeks refer to that new layout.
    assert appmod._sanitize_stale_local_range(GUID_KUWO, "bytes=-100") == "bytes=-100"


def test_serve_file_with_range_error_hook_on_truncation(tmp_path):
    """文件中途收缩送不满时触发 error_hook（本地优先据此记故障指纹）。"""
    target = tmp_path / "t.wav"
    target.write_bytes(b"x" * 10000)
    calls = []
    resp = appmod.serve_file_with_range(str(target), None, "audio/wav",
                                        error_hook=lambda: calls.append(1))
    with open(target, "r+b") as f:
        f.truncate(100)

    async def consume():
        return b"".join([chunk async for chunk in resp.body_iterator])

    body = asyncio.run(consume())
    assert len(body) == 100 and calls == [1]


# === 标准音质转码（HLS）===

def _install_fake_ffmpeg(monkeypatch, tmp_path):
    script = tmp_path / "fake_ffmpeg_localfirst"
    script.write_text(FAKE_FFMPEG_SRC)
    script.chmod(0o755)
    monkeypatch.setattr(tc, "FFMPEG_BIN", str(script))
    arglog = tmp_path / "ffmpeg_args.log"
    count = tmp_path / "ffmpeg_count"
    monkeypatch.setenv("FAKE_FFMPEG_ARGLOG", str(arglog))
    monkeypatch.setenv("FAKE_FFMPEG_COUNT", str(count))
    # 在线元数据时长 269s/10s 分片 → 声明 26 片：产 40 片确保覆盖声明清单
    monkeypatch.setenv("FAKE_FFMPEG_SEGMENTS", "40")
    return str(arglog)


def test_transcode_local_file_input_and_no_duplicate_download(env, tmp_path, monkeypatch):
    """本地命中直喂 ffmpeg（输入为本地路径）；本地会话不挂整轨下载。"""
    wav_path, _ = _seed_library_song(env)
    _write_recommend_cache(env, GUID_KUWO)
    arglog = _install_fake_ffmpeg(monkeypatch, tmp_path)
    with TestClient(app) as client:
        post = client.post("/music/api/v1/track/transcode", json={"guid": GUID_KUWO})
        assert post.status_code == 200
        playlist = client.get(f"/music/api/v1/track/hls/{GUID_KUWO}/preset.m3u8")
        assert playlist.status_code == 200
        token = re.search(r'URI="([a-f0-9]{32})__init.mp4"', playlist.text)[1]
        init = client.get(f"/music/api/v1/track/hls/{GUID_KUWO}/{token}__init.mp4")
        assert init.status_code == 200 and init.content.startswith(b"ftypinit")
        seg = client.get(f"/music/api/v1/track/hls/{GUID_KUWO}/{token}__00000.m4s")
        assert seg.status_code == 200
    argv = open(arglog).read()
    assert wav_path in argv and "127.0.0.1:8768" not in argv   # 输入是本地文件
    assert appmod._full_fetch_tasks == {}                      # 不触发重复整轨下载


def test_completed_hls_rendition_stays_pinned_when_local_appears(env, tmp_path, monkeypatch):
    _install_fake_ffmpeg(monkeypatch, tmp_path)
    directory = tc.session_dir(CONF["cache_dir"], GUID_KUWO)
    os.makedirs(directory, exist_ok=True)
    completed = tc.Session(GUID_KUWO, directory, 10, 10, 1, status="done")
    with open(os.path.join(directory, tc.INIT_NAME), "wb") as fp:
        fp.write(b"original-init")
    with open(os.path.join(directory, "00000.m4s"), "wb") as fp:
        fp.write(b"original-online")
    tc._write_state(completed)
    with TestClient(app) as client:
        first = client.get(f"/music/api/v1/track/hls/{GUID_KUWO}/preset.m3u8")
        assert completed.rendition in first.text
        assert tc.playback_session(GUID_KUWO) is not None
        _seed_library_song(env)
        _write_recommend_cache(env, GUID_KUWO)
        appmod.register_online_meta_one(GUID_KUWO, "晴天", "周杰伦", "叶惠美")
        second = client.get(f"/music/api/v1/track/hls/{GUID_KUWO}/preset.m3u8")
        assert completed.rendition in second.text
        segment = client.get(f"/music/api/v1/track/hls/{GUID_KUWO}/{completed.rendition}__00000.m4s")
        assert segment.content == b"original-online"
        client.post("/music/api/v1/track/transcode/quit", json={"guid": GUID_KUWO})
        assert not tc.playback_in_use()
        restarted = client.get(f"/music/api/v1/track/hls/{GUID_KUWO}/preset.m3u8")
        assert completed.rendition not in restarted.text
    assert not env["musicdl"]


def test_transcode_startup_failure_rebuilds_from_online(env, tmp_path, monkeypatch):
    """本地转码起播失败：清理失败会话、记故障，换在线输入重建。"""
    wav_path, _ = _seed_library_song(env)
    _write_recommend_cache(env, GUID_KUWO)
    arglog = _install_fake_ffmpeg(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_FFMPEG_FAIL_FIRST_ATTEMPT", "1")   # 首次（本地）失败
    with TestClient(app) as client:
        post = client.post("/music/api/v1/track/transcode", json={"guid": GUID_KUWO})
        assert post.status_code == 200
        playlist = client.get(f"/music/api/v1/track/hls/{GUID_KUWO}/preset.m3u8")
        assert playlist.status_code == 200
        token = re.search(r'URI="([a-f0-9]{32})__init.mp4"', playlist.text)[1]
        seg = client.get(f"/music/api/v1/track/hls/{GUID_KUWO}/{token}__00000.m4s")
        assert seg.status_code == 200
    argv = open(arglog).read()
    assert wav_path in argv                      # 第一次尝试：本地输入
    assert "/stream" in argv                     # 第二次重建：在线输入
    assert localmatch.fault_active(GUID_KUWO, {
        "path": wav_path, "size": os.path.getsize(wav_path),
        "mtime": os.path.getmtime(wav_path)})


def test_hls_segments_never_mix_audio_versions(env, tmp_path, monkeypatch):
    """本地会话中途失败后：客户端仍续拉旧序列时不供出在线版本分片；重新拉清单后恢复。"""
    song_path, _ = _seed_library_song(env)
    _write_recommend_cache(env, GUID_KUWO)
    arglog = _install_fake_ffmpeg(monkeypatch, tmp_path)
    with TestClient(app) as client:
        playlist = client.get(f"/music/api/v1/track/hls/{GUID_KUWO}/preset.m3u8")
        assert playlist.status_code == 200
        token = re.search(r'URI="([a-f0-9]{32})__init.mp4"', playlist.text)[1]
        seg = client.get(f"/music/api/v1/track/hls/{GUID_KUWO}/{token}__00000.m4s")
        assert seg.status_code == 200            # 本地版本分片已供出
        # 模拟本地会话中途死亡（watcher 清理目录）+ 记文件故障（_on_hls_segment_failure 语义）
        import shutil
        shutil.rmtree(tc.session_dir(CONF["cache_dir"], GUID_KUWO), ignore_errors=True)
        tc._SESSIONS.pop(GUID_KUWO, None)
        localmatch.record_fault(GUID_KUWO, {
            "path": song_path, "size": os.path.getsize(song_path),
            "mtime": os.path.getmtime(song_path)}, "test mid-death")
        # 旧序列续拉：重建的是在线会话（版本不同）→ 分片 404，不混入另一份音频
        retry_seg = client.get(f"/music/api/v1/track/hls/{GUID_KUWO}/{token}__00001.m4s")
        assert retry_seg.status_code == 404
        # 新的播放过程：重新拉 playlist 固定新版本，分片恢复
        playlist2 = client.get(f"/music/api/v1/track/hls/{GUID_KUWO}/preset.m3u8")
        assert playlist2.status_code == 200
        new_token = re.search(r'URI="([a-f0-9]{32})__init.mp4"', playlist2.text)[1]
        assert new_token != token
        old = client.get(f"/music/api/v1/track/hls/{GUID_KUWO}/{token}__00001.m4s")
        assert old.status_code == 404
        token = new_token
        seg2 = client.get(f"/music/api/v1/track/hls/{GUID_KUWO}/{token}__00001.m4s")
        assert seg2.status_code == 200
    argv = open(arglog).read()
    assert "/stream" in argv                     # 重建输入为在线


@pytest.mark.parametrize("origin", ["public", "account", "favorite"])
def test_disk_origins_keep_online_identity_and_avoid_network(env, monkeypatch, origin):
    _, audio = _seed_library_song(env)
    track = {"guid": GUID_KUWO, "title": "晴天", "artists": [{"name": "周杰伦"}],
             "album": {"name": "叶惠美"}}
    if origin == "favorite":
        path = os.path.join(env["dirs"]["favorites"], "u1.json")
        data = {"items": [{"guid": GUID_KUWO, "track": track}]}
    else:
        root = os.path.join(env["tmp"], origin)
        monkeypatch.setenv("FNMUSIC_PUBLIC_PLAYLISTS_DIR" if origin == "public"
                           else "FNMUSIC_NM_PLAYLISTS_DIR", root)
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, "list.json")
        data = {"tracks": [track]}
    with open(path, "w") as fp:
        json.dump(data, fp)
    with TestClient(app) as client:
        response = client.get("/music/api/v1/track/stream", params={"guid": GUID_KUWO})
        metadata = client.get("/music/api/v1/track/audio-info", params={"guid": GUID_KUWO})
    assert response.content == audio
    info = metadata.json()["data"]
    assert appmod.resolve_real_guid(info["guid"]) == GUID_KUWO
    assert info["audioSpec"]["sampleRate"] == 8000
    assert info["audioSpec"]["channel"] == 1
    assert info["audioSpec"]["bitrate"] == 128000
    assert env["musicdl"] == env["musicbox"] == []


def test_all_unicode_candidates_album_priority_and_full_artist_set(env):
    path = os.path.join(env["dirs"]["library"], "unicode.wav")
    _make_wav(path, 1)
    rows = [{"id": i, "title": "  ＳＴＲＡＳＳＥ\u2003世界  ",
             "artists": ["Ａ", "B"], "album": "wanted" if i == 60 else "other",
             "path": path} for i in range(1, 80)]
    _make_db(CONF["music_db"], rows)
    hit = localmatch.resolve_in_library(CONF["music_db"], "straße 世界", "b/a", "wanted")
    assert hit["track_guid"] == "g60"
    assert localmatch.resolve_in_library(CONF["music_db"], "straße 世界", "a") is None
    assert localmatch.resolve_in_library(CONF["music_db"], "straße 世界 Live", "a/b") is None


def test_deleted_cached_hit_rechecks_db_and_missing_file(env):
    path, _ = _seed_library_song(env)
    needle = lambda: ("晴天", "周杰伦", "叶惠美")
    assert localmatch.resolve_guid(GUID_KUWO, needle, CONF["music_db"])
    con = sqlite3.connect(CONF["music_db"])
    con.execute("UPDATE track SET is_admin_deleted=1")
    con.commit()
    con.close()
    assert localmatch.resolve_guid(GUID_KUWO, needle, CONF["music_db"]) is None
    localmatch.invalidate(GUID_KUWO)
    os.unlink(path)
    assert localmatch.resolve_guid(GUID_KUWO, needle, CONF["music_db"]) is None


def test_db_error_does_not_create_negative_cache(env):
    db = CONF["music_db"]
    with open(db, "wb") as fp:
        fp.write(b"broken sqlite")
    needle = lambda: ("晴天", "周杰伦", "叶惠美")
    assert localmatch.resolve_guid(GUID_KUWO, needle, db) is None
    assert GUID_KUWO not in localmatch._MISSES
    os.unlink(db)
    _seed_library_song(env)
    assert localmatch.resolve_guid(GUID_KUWO, needle, db)


def test_empty_unreadable_files_are_excluded(env, monkeypatch):
    path, _ = _seed_library_song(env)
    monkeypatch.setattr(os, "access", lambda *args: False)
    assert localmatch.resolve_in_library(CONF["music_db"], "晴天", "周杰伦") is None
    monkeypatch.undo()
    with open(path, "wb"):
        pass
    assert localmatch._readable_audio_file(path) is None


def test_probe_timeout_and_fingerprint_success_cache(env, monkeypatch):
    path, _ = _seed_library_song(env)
    match = localmatch.resolve_in_library(CONF["music_db"], "晴天", "周杰伦")
    assert localmatch.probe_audio(match)["audio_spec"]["sampleRate"] == 8000
    import mutagen
    monkeypatch.setattr(mutagen, "File", lambda path: pytest.fail("successful probe repeated"))
    assert localmatch.probe_audio(match)
    monkeypatch.setattr(localmatch, "PROBE_TIMEOUT_S", 0.02)
    monkeypatch.setattr(localmatch, "probe_audio", lambda match: time.sleep(0.1))
    assert asyncio.run(localmatch.probe_audio_async(match)) is None
    assert localmatch.PROBE_TIMEOUT_S <= 2


def test_local_response_holds_same_open_file_after_unlink(env):
    path, audio = _seed_library_song(env)
    match = localmatch.resolve_in_library(CONF["music_db"], "晴天", "周杰伦")
    async def run():
        response = await appmod._serve_local_stream(GUID_KUWO, match, "bytes=100-200")
        os.unlink(path)
        return b"".join([chunk async for chunk in response.body_iterator])
    assert asyncio.run(run()) == audio[100:201]


def test_corrupt_head_and_metadata_do_not_claim_local_specs(env):
    _seed_library_song(env, corrupt=True)
    _write_recommend_cache(env, GUID_KUWO)
    with TestClient(app) as client:
        meta = client.get("/music/api/v1/track/metadata", params={"guid": GUID_KUWO})
        head = client.head("/music/api/v1/track/stream", params={"guid": GUID_KUWO})
    assert meta.json()["data"]["audioSpec"]["format"] == "mp3"
    assert not head.headers.get("content-type", "").startswith("audio/wav")


def test_no_ffmpeg_local_manifest_does_not_fetch_info(env, monkeypatch):
    _seed_library_song(env)
    _write_recommend_cache(env, GUID_KUWO)
    monkeypatch.setattr(tc, "FFMPEG_BIN", None)
    with TestClient(app) as client:
        manifest = client.get(f"/music/api/v1/track/hls/{GUID_KUWO}/preset.m3u8")
    assert manifest.status_code == 200 and "#EXTINF:2.000" in manifest.text
    assert env["musicdl"] == env["musicbox"] == []


def test_watcher_records_mid_transcode_failure_before_session_removal(env, monkeypatch):
    path, _ = _seed_library_song(env)
    match = localmatch.resolve_in_library(CONF["music_db"], "晴天", "周杰伦")
    class FailedProcess:
        async def wait(self):
            return 1
    async def run():
        directory = tc.session_dir(CONF["cache_dir"], GUID_KUWO)
        os.makedirs(directory, exist_ok=True)
        sess = tc.Session(GUID_KUWO, directory, 20, 10, 2, src=path,
                          proc=FailedProcess(), src_kind="local", source_fingerprint=match)
        sess.on_failure = lambda failed: appmod._on_hls_segment_failure(GUID_KUWO, failed)
        tc._SESSIONS[GUID_KUWO] = sess
        await tc._watch(sess)
        assert tc.get_session(GUID_KUWO) is None
        assert sess.status == "failed"
    asyncio.run(run())
    assert localmatch.fault_active(GUID_KUWO, match)



def test_disk_multiple_artists_are_not_reduced_to_first_artist(env):
    _, audio = _seed_library_song(env, artists=("A", "B"))
    root = appmod.pubpl.cache_dir()
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "duet.json"), "w") as fp:
        json.dump({"tracks": [{"guid": GUID_KUWO, "title": "晴天", "artist": "A",
                   "artists": [{"name": "B"}, {"name": "A"}], "album": "叶惠美"}]}, fp)
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream", params={"guid": GUID_KUWO})
    assert resp.content == audio
    assert env["musicdl"] == []


def test_miss_expiry_and_new_registration_invalidate(env, monkeypatch):
    _seed_library_song(env)
    needle = lambda: ("new song", "周杰伦", "")
    assert localmatch.resolve_guid(GUID_KUWO, needle, CONF["music_db"]) is None
    assert GUID_KUWO in localmatch._MISSES
    assert localmatch._MISSES[GUID_KUWO] <= time.monotonic() + 30
    appmod.register_online_meta_one(GUID_KUWO, "晴天", "周杰伦", "叶惠美", 2)
    assert GUID_KUWO not in localmatch._MISSES
    assert localmatch.resolve_guid(GUID_KUWO, lambda: ("晴天", "周杰伦", ""), CONF["music_db"])


def test_concurrent_ensure_produces_only_one_session(env, tmp_path, monkeypatch):
    _install_fake_ffmpeg(monkeypatch, tmp_path)
    monkeypatch.setattr(tc, "FFPROBE_BIN", None)
    async def run():
        kwargs = {"root": CONF["cache_dir"]}
        one, two = await asyncio.gather(
            tc.ensure_session(GUID_KUWO, "http://test/source", 20, **kwargs),
            tc.ensure_session(GUID_KUWO, "http://test/source", 20, **kwargs))
        assert one is two
        await one.exit_event.wait()
    asyncio.run(run())
    with open(os.environ["FAKE_FFMPEG_COUNT"]) as fp:
        assert fp.read() == "1"



def test_failed_local_file_cannot_return_via_legacy_ref_cache(env):
    path, _ = _seed_library_song(env, corrupt=True)
    _write_recommend_cache(env, GUID_KUWO)
    appmod.remember_media_path(GUID_KUWO, path)
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream", params={"guid": GUID_KUWO})
    assert resp.content == ONLINE_MP3
    assert appmod.find_cache_file(GUID_KUWO, playback=True) != path


def test_local_library_beats_existing_complete_download(env):
    _, audio = _seed_library_song(env)
    _write_recommend_cache(env, GUID_KUWO)
    cached = os.path.join(CONF["cache_dir"], appmod.cache_safe_guid(GUID_KUWO) + ".mp3")
    with open(cached, "wb") as fp:
        fp.write(ONLINE_MP3)
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream", params={"guid": GUID_KUWO})
    assert resp.content == audio
    assert env["musicdl"] == []


def test_discard_waits_watcher_before_new_directory(env, tmp_path, monkeypatch):
    _install_fake_ffmpeg(monkeypatch, tmp_path)
    monkeypatch.setattr(tc, "FFPROBE_BIN", None)
    class Process:
        returncode = None
        def kill(self):
            self.returncode = -9
    async def run():
        directory = tc.session_dir(CONF["cache_dir"], GUID_KUWO)
        os.makedirs(directory, exist_ok=True)
        sess = tc.Session(GUID_KUWO, directory, 20, 10, 2, proc=Process())
        async def old_cleanup():
            await asyncio.sleep(0.03)
            tc._remove_dir(directory)
            sess.exit_event.set()
        sess.watcher = asyncio.create_task(old_cleanup())
        tc._SESSIONS[GUID_KUWO] = sess
        await tc.discard_session(GUID_KUWO)
        new = await tc.ensure_session(GUID_KUWO, "http://test/source", 20, root=CONF["cache_dir"])
        await new.exit_event.wait()
        assert os.path.isfile(os.path.join(directory, tc.INIT_NAME))
    asyncio.run(run())


@pytest.mark.parametrize("title, artist, expected", [
    ("A B", "C D", True),
    ("  A\t  B  ", "  C\u2003 D  ", True),
    ("AB", "C D", False),
    ("A B", "CD", False),
    ("AB", "CD", False),
])
def test_internal_single_space_is_part_of_title_and_artist_identity(env, title, artist, expected):
    _seed_library_song(env, title="A B", artists=("C D",))
    hit = localmatch.resolve_in_library(CONF["music_db"], title, artist)
    assert (hit is not None) is expected
    assert localmatch.title_key("AB") != localmatch.title_key("A B")
    assert localmatch.split_artist_set("AB") != localmatch.split_artist_set("A B")


def test_registered_vo_preserves_complete_artist_set_for_local_match(env):
    _, audio = _seed_library_song(env, artists=("甲", "乙"))
    appmod.register_online_meta([{
        "guid": GUID_KUWO, "title": "晴天", "album": "叶惠美",
        "artists": [{"name": "甲"}, {"name": "乙"}],
    }])
    with TestClient(app) as client:
        response = client.get("/music/api/v1/track/stream", params={"guid": GUID_KUWO})
    assert response.content == audio
    assert not env["musicdl"]


@pytest.mark.parametrize("failure", ["soft_delete", "db_missing"])
def test_local_layout_range_reset_when_library_becomes_unavailable(env, failure):
    _seed_library_song(env)
    _write_recommend_cache(env, GUID_KUWO)
    with TestClient(app) as client:
        assert client.get("/music/api/v1/track/stream", params={"guid": GUID_KUWO}).status_code == 200
        if failure == "soft_delete":
            with sqlite3.connect(CONF["music_db"]) as con:
                con.execute("UPDATE track SET is_admin_deleted=1")
        else:
            os.unlink(CONF["music_db"])
        env["musicdl"].clear()
        response = client.get("/music/api/v1/track/stream", params={"guid": GUID_KUWO},
                              headers={"Range": "bytes=100-"})
        assert response.content == ONLINE_MP3
        assert [c for c in env["musicdl"] if c["path"] == "/stream"][0]["range"] is None
        env["musicdl"].clear()
        seek = client.get("/music/api/v1/track/stream", params={"guid": GUID_KUWO},
                          headers={"Range": "bytes=50-"})
        # A completed online tee may serve the seek from its cache.
        assert seek.status_code == 206 and seek.content == ONLINE_MP3[50:]
        assert appmod._sanitize_stale_local_range(GUID_KUWO, "bytes=50-") == "bytes=50-"
