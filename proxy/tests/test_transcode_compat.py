"""v2.6.2 标准音质兼容专项：真转码会话、App 下载仿真、透传回归、.ref 标签自愈。

conftest 已全局禁用真 ffmpeg；需要转码路径的用例把 tc.FFMPEG_BIN 指到
tests 内置的假 ffmpeg 脚本（FAKE_FFMPEG_MODE 控制行为：ok/ok_delay/fail/hang）。
"""
import asyncio
import json
import os
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import proxy.app as appmod
from proxy import transcode as tc
from proxy.app import CONF, app

FAKE_KUWO = "online:kuwo:228908"
LOCAL_HEX = "abcdef0123456789abcdef0123456789"

# 假 ffmpeg：hls 模式按 -hls_segment_filename 产 init/分片；普通模式把
# 最后一个参数当输出文件写一段数据。行为由 FAKE_FFMPEG_MODE 控制。
FAKE_FFMPEG_SRC = '''#!/usr/bin/env python3
import os, sys, time
args = sys.argv[1:]
mode = os.environ.get("FAKE_FFMPEG_MODE", "ok")
if mode == "fail":
    sys.exit(1)
if mode == "hang":
    time.sleep(60)
    sys.exit(0)
if mode == "ok_delay":
    time.sleep(0.5)
if "-hls_segment_filename" in args:
    pattern = args[args.index("-hls_segment_filename") + 1]
    segdir = os.path.dirname(os.path.abspath(pattern))
    os.makedirs(segdir, exist_ok=True)
    with open(os.path.join(segdir, "init.mp4"), "wb") as f:
        f.write(b"ftypinit" * 4)
    n = int(os.environ.get("FAKE_FFMPEG_SEGMENTS", "40"))
    for i in range(n):
        with open(os.path.join(segdir, "%05d.m4s" % i), "wb") as f:
            f.write(b"moofseg" + str(i).encode())
    with open(args[-1], "w") as f:
        f.write("#EXTM3U\\n")
    sys.exit(0)
out = args[-1]
os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
with open(out, "wb") as f:
    f.write(b"M4ADATA" * 50)
sys.exit(0)
'''

SENTINEL = {"code": 0, "msg": "ok", "passthrough": True}


def wait_for(pred, timeout: float = 8.0):
    """轮询直到 pred() 为真；返回该真值，超时返回 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = pred()
        if result:
            return result
        time.sleep(0.05)
    return pred() or None


@pytest.fixture
def fake_ffmpeg(tmp_path, monkeypatch):
    """装一个假 ffmpeg；用 mode 参数控制脚本行为（默认 ok）。"""
    def _install(mode: str = "ok", segments: int = 40):
        script = tmp_path / "fake_ffmpeg"
        script.write_text(FAKE_FFMPEG_SRC)
        script.chmod(0o755)
        monkeypatch.setattr(tc, "FFMPEG_BIN", str(script))
        monkeypatch.setenv("FAKE_FFMPEG_MODE", mode)
        monkeypatch.setenv("FAKE_FFMPEG_SEGMENTS", str(segments))
        return str(script)
    return _install


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离目录 + 各源 mock 上游；upstream 记录往返供透传断言。"""
    appmod._SEARCH_CACHE.clear()
    appmod._full_fetch_tasks.clear()
    appmod._full_fetch_failed.clear()
    appmod._tee_active.clear()
    appmod._tee_handoff_active.clear()
    appmod._bind_pending.clear()
    appmod._bind_tasks.clear()
    appmod._bind_retry_last.clear()
    appmod._FAKE_GUID_REVERSE.clear()
    appmod._DL_TASKS.clear()
    appmod._TAGS_LOOKUP_MISS.clear()

    dirs = {}
    for key in ("cache", "library", "online_favorites", "playlist_tracks", "play_history"):
        dirs[key] = str(tmp_path / key)
        os.makedirs(dirs[key], exist_ok=True)
    monkeypatch.setitem(CONF, "cache_dir", dirs["cache"])
    monkeypatch.setitem(CONF, "library_dir", dirs["library"])
    monkeypatch.setitem(CONF, "fav_dir", dirs["online_favorites"])
    monkeypatch.setitem(CONF, "plt_dir", dirs["playlist_tracks"])
    monkeypatch.setitem(CONF, "tee_save_dir", dirs["library"])
    monkeypatch.setitem(CONF, "music_db", str(tmp_path / "music.db"))
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "lx_enabled", False)
    monkeypatch.setitem(CONF, "tee_save_enabled", False)
    monkeypatch.setitem(CONF, "tee_cache_max", 2)
    monkeypatch.setitem(CONF, "lyric_field", "data.lyric")
    monkeypatch.setitem(CONF, "search_list_path", "data.list")
    monkeypatch.setitem(CONF, "online_limit", 30)
    monkeypatch.setitem(CONF, "musicdl_url", "http://127.0.0.1:8768")
    monkeypatch.setitem(CONF, "transcode_enabled", True)
    monkeypatch.setitem(CONF, "transcode_bitrate", "128k")
    monkeypatch.setitem(CONF, "transcode_hls_time", 10.0)
    monkeypatch.setitem(CONF, "transcode_max_sessions", 2)
    monkeypatch.setitem(CONF, "transcode_ttl_s", 90.0)
    monkeypatch.setitem(CONF, "transcode_cache_max_mb", 512)
    monkeypatch.setitem(CONF, "transcode_dl_bitrate", "320k")
    monkeypatch.setitem(CONF, "fav_auto_bind", False)
    monkeypatch.setitem(CONF, "trace_forward", False)
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", dirs["play_history"])
    monkeypatch.setattr(appmod, "_HOME", str(tmp_path))

    upstream_calls: list[dict] = []

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except Exception:
                body = None
        upstream_calls.append({
            "method": request.method,
            "path": request.url.path,
            "query": str(request.url.query),
            "json": body,
        })
        return httpx.Response(200, json=SENTINEL)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix")

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json={
                "ok": True, "id": "kuwo:228908", "source": "kuwo",
                "title": "晴天", "artist": "周杰伦", "album": "叶惠美",
                "duration_s": 269, "ext": "mp3",
            })
        return httpx.Response(404)

    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8770")
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"ok": False})),
        base_url="http://127.0.0.1:8772")

    yield {"dirs": dirs, "upstream_calls": upstream_calls, "tmp": str(tmp_path)}

    appmod._full_fetch_tasks.clear()
    appmod._full_fetch_failed.clear()
    appmod._tee_active.clear()
    appmod._tee_handoff_active.clear()
    appmod._bind_pending.clear()
    appmod._bind_tasks.clear()
    appmod._bind_retry_last.clear()
    appmod._DL_TASKS.clear()
    appmod._TAGS_LOOKUP_MISS.clear()


def seed_cached_audio(guid: str, data: bytes = b"MP3DATA" * 200, ext: str = "mp3") -> str:
    """往测试曲库放一个已落库文件并记 .ref，模拟已下载完成的在线曲。"""
    path = os.path.join(CONF["library_dir"], f"周杰伦 - 晴天.{ext}")
    with open(path, "wb") as f:
        f.write(data)
    appmod.remember_media_path(guid, path)
    return path


# === transcode 模块：playlist 合成 ===

def test_playlist_text_floor_count_and_tail():
    sess = tc.Session(guid="g", directory="/tmp/x", duration_s=269.9, hls_time=10.0, declared_count=26)
    text = tc.playlist_text(sess)
    assert "#EXT-X-VERSION:7" in text
    assert '#EXT-X-MAP:URI="init.mp4"' in text
    assert "#EXT-X-ENDLIST" in text
    assert text.count("#EXTINF:") == 26
    assert "00000.m4s" in text and "00025.m4s" in text
    assert "00026.m4s" not in text          # floor：宁可少一片不越界
    assert "#EXTINF:19.900," in text        # 尾片 = 269.9 - 10*25


def test_declared_count_edges():
    assert tc._declared_count(0, 10) == 1
    assert tc._declared_count(269.9, 10) == 26
    assert tc._declared_count(10, 10) == 1
    assert tc._declared_count(240, 10) == 24


def test_valid_segment_name():
    assert tc.valid_segment_name("init.mp4")
    assert tc.valid_segment_name("00000.m4s")
    assert not tc.valid_segment_name("0000.m4s")
    assert not tc.valid_segment_name("00000.m4s.exe")
    assert not tc.valid_segment_name("preset.m3u8")
    assert not tc.valid_segment_name("../escape")


# === transcode 模块：会话生命周期（假 ffmpeg） ===
# 会话 watcher/事件绑定首个事件循环，一条用例的异步步骤必须在同一个
# asyncio.run 里跑完，不能跨 run 复用 Session。

def test_ensure_session_completes_and_reuses_cache(tmp_path, fake_ffmpeg):
    fake_ffmpeg("ok")
    src = str(tmp_path / "in.mp3")
    with open(src, "wb") as f:
        f.write(b"x" * 64)
    root = str(tmp_path / "cache")

    async def flow():
        sess = await tc.ensure_session(FAKE_KUWO, src, 269.9, root=root)
        assert sess is not None and sess.status in ("starting", "running")
        assert await _wait_event(sess)
        assert sess.status == "done"
        directory = sess.directory
        assert os.path.isfile(os.path.join(directory, "init.mp4"))
        assert json.load(open(os.path.join(directory, tc.STATE_NAME)))["status"] == "done"
        assert FAKE_KUWO not in tc._SESSIONS
        # 完整缓存复用：不再起新进程（proc 为 None 即未 spawn）
        again = await tc.ensure_session(FAKE_KUWO, src, 269.9, root=root)
        assert again is not None and again.status == "done"
        assert again.proc is None
        return directory, (await tc.ensure_session(FAKE_KUWO, src, 269.9, root=root)).directory

    first, third = asyncio.run(flow())
    assert first == third


def test_ensure_session_failure_cleans_directory(tmp_path, fake_ffmpeg):
    fake_ffmpeg("fail")
    src = str(tmp_path / "in.mp3")
    with open(src, "wb") as f:
        f.write(b"x" * 64)
    root = str(tmp_path / "cache")

    async def flow():
        sess = await tc.ensure_session(FAKE_KUWO, src, 100.0, root=root)
        assert sess is not None
        assert await _wait_event(sess)
        assert sess.status == "failed"
        return sess.directory

    directory = asyncio.run(flow())
    assert not os.path.exists(directory)


def test_ensure_session_without_ffmpeg_returns_none(tmp_path):
    # conftest 默认 FFMPEG_BIN=None：回落单分片桩，不炸
    src = str(tmp_path / "in.mp3")
    with open(src, "wb") as f:
        f.write(b"x" * 64)
    assert asyncio.run(tc.ensure_session(FAKE_KUWO, src, 100.0, root=str(tmp_path))) is None


def test_wait_file_returns_path_and_none_on_failure(tmp_path, fake_ffmpeg):
    fake_ffmpeg("fail")
    src = str(tmp_path / "in.mp3")
    with open(src, "wb") as f:
        f.write(b"x" * 64)
    root = str(tmp_path / "cache")
    existing = str(tmp_path / "exists.bin")
    with open(existing, "wb") as f:
        f.write(b"1")

    async def flow():
        sess = await tc.ensure_session(FAKE_KUWO, src, 100.0, root=root)
        assert await _wait_event(sess)
        missing = os.path.join(sess.directory, "00000.m4s")
        return await tc.wait_file(missing, sess, timeout=0.5), await tc.wait_file(existing, None, timeout=1)

    no, yes = asyncio.run(flow())
    assert no is None
    assert yes == existing


def test_quit_session_kills_and_removes_halfway(tmp_path, fake_ffmpeg):
    fake_ffmpeg("hang")
    src = str(tmp_path / "in.mp3")
    with open(src, "wb") as f:
        f.write(b"x" * 64)
    root = str(tmp_path / "cache")

    async def flow():
        sess = await tc.ensure_session(FAKE_KUWO, src, 100.0, root=root)
        assert sess is not None and sess.status == "running"
        await tc.quit_session(FAKE_KUWO)
        assert await _wait_event(sess)
        assert sess.status == "aborted"
        return sess.directory

    directory = asyncio.run(flow())
    assert not os.path.exists(directory)


def test_ttl_reap_kills_expired_and_sweeps_orphans(tmp_path, fake_ffmpeg):
    fake_ffmpeg("hang")
    src = str(tmp_path / "in.mp3")
    with open(src, "wb") as f:
        f.write(b"x" * 64)
    root = str(tmp_path / "cache")

    async def flow():
        sess = await tc.ensure_session(FAKE_KUWO, src, 100.0, root=root)
        sess.last_beat = time.monotonic() - 99999
        tc._reap_expired(root=root, ttl_s=90)   # TTL 杀进程；目录由 watcher 清理
        assert await _wait_event(sess)
        assert sess.status == "aborted"
        return sess.directory

    directory = asyncio.run(flow())
    assert not os.path.exists(directory)
    # 无主半成品目录（模拟崩溃残留）也被清掉
    orphan = os.path.join(root, "hls", "online_kuwo_orphan")
    os.makedirs(orphan, exist_ok=True)
    with open(os.path.join(orphan, "00000.m4s.tmp"), "wb") as f:
        f.write(b"half")
    assert tc._reap_expired(root=root, ttl_s=90) >= 1
    assert not os.path.exists(orphan)


def _mk_done_cache(root: str, guid: str, size: int, age_s: float) -> str:
    directory = tc.session_dir(root, guid)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, tc.INIT_NAME), "wb") as f:
        f.write(b"i" * min(size, 16))
    with open(os.path.join(directory, "00000.m4s"), "wb") as f:
        f.write(b"s" * max(0, size - 16))
    state = {"guid": guid, "status": "done", "duration_s": 100, "hls_time": 10,
             "declared_count": 10, "bitrate": "128k", "started_ts": 0, "done_ts": 0}
    spath = os.path.join(directory, tc.STATE_NAME)
    with open(spath, "w") as f:
        json.dump(state, f)
    old = time.time() - age_s
    os.utime(spath, (old, old))
    return directory


def test_enforce_quota_evicts_lru_oldest_first(tmp_path):
    root = str(tmp_path / "cache")
    old = _mk_done_cache(root, "g-old", 10000, age_s=3600)
    mid = _mk_done_cache(root, "g-mid", 10000, age_s=600)
    new = _mk_done_cache(root, "g-new", 10000, age_s=60)
    # 总量 ~30KB，配额 21KB：只淘汰最旧的 10KB 即回到配额内
    asyncio.run(tc._enforce_quota(root=root, max_bytes=21000))
    assert not os.path.exists(old)
    assert os.path.exists(mid) and os.path.exists(new)


async def _wait_event(sess: tc.Session, timeout: float = 8.0) -> bool:
    try:
        await asyncio.wait_for(sess.exit_event.wait(), timeout)
        return True
    except asyncio.TimeoutError:
        return False


# === App 播放侧：真转码 HLS ===

def test_hls_transcode_full_flow(env, fake_ffmpeg):
    fake_ffmpeg("ok_delay", segments=40)
    with TestClient(app) as client:
        resp = client.post("/music/api/v1/track/transcode", json={"guid": FAKE_KUWO})
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

        body = client.get(f"/music/api/v1/track/hls/{FAKE_KUWO}/preset.m3u8").text
        assert "#EXT-X-VERSION:7" in body
        assert '#EXT-X-MAP:URI="init.mp4"' in body
        assert "00000.m4s" in body and "#EXT-X-ENDLIST" in body

        init = client.get(f"/music/api/v1/track/hls/{FAKE_KUWO}/init.mp4")
        assert init.status_code == 200
        assert init.content.startswith(b"ftypinit")
        assert init.headers["content-type"].startswith("audio/mp4")

        seg = client.get(f"/music/api/v1/track/hls/{FAKE_KUWO}/00000.m4s")
        assert seg.status_code == 200
        assert seg.headers["content-type"].startswith("video/iso.segment")

        assert client.get(f"/music/api/v1/track/hls/{FAKE_KUWO}/evil.mp4").status_code == 404

        hb = client.post("/music/api/v1/track/transcode/heartbeat", json={"guid": FAKE_KUWO})
        assert hb.status_code == 200 and hb.json()["code"] == 0
        assert wait_for(lambda: FAKE_KUWO not in tc._SESSIONS)  # 假 ffmpeg 退出转正
        quit_resp = client.post("/music/api/v1/track/transcode/quit", json={"guid": FAKE_KUWO})
        assert quit_resp.status_code == 200


def test_hls_stub_fallback_without_ffmpeg(env):
    with TestClient(app) as client:
        body = client.get(f"/music/api/v1/track/hls/{FAKE_KUWO}/preset.m3u8").text
        assert "track/stream" in body and "#EXT-X-ENDLIST" in body
        assert client.get(f"/music/api/v1/track/hls/{FAKE_KUWO}/00000.m4s").status_code == 404
        resp = client.post("/music/api/v1/track/transcode", json={"guid": FAKE_KUWO})
        assert resp.json()["status"] == "success"


# === App 下载侧：prepare → status → file → delete（协议形状对齐官方实测） ===

def test_download_standard_transcode_flow(env, fake_ffmpeg):
    fake_ffmpeg("ok")
    seed_cached_audio(FAKE_KUWO, b"FLACDATA" * 500, ext="flac")
    with TestClient(app) as client:
        prep = client.post("/music/api/v1/download/track/transcode/prepare",
                           json={"trackGUID": FAKE_KUWO, "quality": "standard"})
        assert prep.status_code == 200
        data = prep.json()["data"]
        assert data["status"] == "success" and data["downloadId"]  # 官方形状：downloadId

        did = data["downloadId"]

        def _ready():
            st = client.get("/music/api/v1/download/track/transcode/status",
                            params={"downloadId": did}).json()["data"]
            return st if st["status"] == "ready" else None
        st = wait_for(_ready)
        assert st is not None
        assert st["percent"] == 100 and st["downloadId"] == did

        file_resp = client.get("/music/api/v1/download/track/transcode/file",
                               params={"downloadId": did})
        assert file_resp.status_code == 200
        assert file_resp.content.startswith(b"M4ADATA")          # 假 ffmpeg 的产物
        assert file_resp.headers["content-type"].startswith("audio/mpeg")

        head = client.head("/music/api/v1/download/track/transcode/file",
                           params={"downloadId": did})
        assert head.status_code == 200

        rng = client.get("/music/api/v1/download/track/transcode/file",
                         params={"downloadId": did}, headers={"Range": "bytes=0-6"})
        assert rng.status_code == 206
        assert len(rng.content) == 7

        dele = client.post("/music/api/v1/download/track/transcode/delete",
                           json={"downloadId": did})
        assert dele.json()["data"] == {"downloadId": did, "deleted": True}
        # 任务已删：同 downloadId 再问 → 透传官方（sentinel）
        st2 = client.get("/music/api/v1/download/track/transcode/status",
                         params={"downloadId": did})
        assert st2.json() == SENTINEL


def test_download_standard_mp3_source_served_as_is(env, fake_ffmpeg):
    """源已是 mp3：标准档不做无意义重编码，直接供原文件（内容零损失）。"""
    fake_ffmpeg("fail")   # 假 ffmpeg fail——一旦被调用测试即失败
    payload = b"ID3MP3SRC" * 200
    seed_cached_audio(FAKE_KUWO, payload, ext="mp3")
    with TestClient(app) as client:
        did = client.post("/music/api/v1/download/track/transcode/prepare",
                          json={"guid": FAKE_KUWO, "quality": "standard"}).json()["data"]["downloadId"]

        def _ready():
            st = client.get("/music/api/v1/download/track/transcode/status",
                            params={"downloadId": did}).json()["data"]
            return st if st["status"] == "ready" else None
        assert wait_for(_ready)
        file_resp = client.get("/music/api/v1/download/track/transcode/file",
                               params={"downloadId": did})
        assert file_resp.content == payload
        assert file_resp.headers["content-type"].startswith("audio/mpeg")


def test_download_original_serves_cached_file(env, fake_ffmpeg):
    fake_ffmpeg("ok")
    payload = b"ORIGINALBYTES" * 100
    seed_cached_audio(FAKE_KUWO, payload, ext="flac")
    with TestClient(app) as client:
        did = client.post("/music/api/v1/download/track/transcode/prepare",
                          json={"guid": FAKE_KUWO, "quality": "original"}).json()["data"]["downloadId"]

        def _ready():
            st = client.get("/music/api/v1/download/track/transcode/status",
                            params={"downloadId": did}).json()["data"]
            return st if st["status"] == "ready" else None
        assert wait_for(_ready)
        file_resp = client.get("/music/api/v1/download/track/transcode/file",
                               params={"downloadId": did})
        assert file_resp.content == payload      # 原始档：不转码，直接供原文件


def test_download_standard_fails_without_ffmpeg(env):
    seed_cached_audio(FAKE_KUWO, b"FLACDATA" * 100, ext="flac")
    with TestClient(app) as client:
        did = client.post("/music/api/v1/download/track/transcode/prepare",
                          json={"guid": FAKE_KUWO}).json()["data"]["downloadId"]

        def _failed():
            st = client.get("/music/api/v1/download/track/transcode/status",
                            params={"downloadId": did}).json()["data"]
            return st if st["status"] == "failed" else None
        st = wait_for(_failed)
        assert st is not None and st["errmsg"]
        file_resp = client.get("/music/api/v1/download/track/transcode/file",
                               params={"downloadId": did})
        assert file_resp.status_code == 404


def test_download_fails_when_source_dead_and_no_cache(env, fake_ffmpeg):
    fake_ffmpeg("ok")
    guid = "online:migu:600902000006889366"   # musicdl mock 只答 /info，流 404
    with TestClient(app) as client:
        did = client.post("/music/api/v1/download/track/transcode/prepare",
                          json={"guid": guid}).json()["data"]["downloadId"]

        def _failed():
            st = client.get("/music/api/v1/download/track/transcode/status",
                            params={"downloadId": did}).json()["data"]
            return st if st["status"] == "failed" else None
        assert wait_for(_failed, timeout=15)


# === 透传回归：本地/未知 guid 一律原样转发官方 ===

def test_local_and_unknown_guid_passthrough(env):
    calls = env["upstream_calls"]
    with TestClient(app) as client:
        # 本地 guid（32-hex，官方曲目）
        assert client.get(f"/music/api/v1/track/hls/{LOCAL_HEX}/preset.m3u8").json() == SENTINEL
        assert client.get(f"/music/api/v1/track/hls/{LOCAL_HEX}/00000.m4s").json() == SENTINEL
        assert client.post("/music/api/v1/track/transcode", json={"guid": LOCAL_HEX}).json() == SENTINEL
        assert client.post("/music/api/v1/track/transcode/heartbeat",
                           json={"guid": LOCAL_HEX}).json() == SENTINEL
        # 下载端点：本地 guid prepare / 未知 taskGuid 全部透传
        assert client.get("/music/api/v1/download/track/transcode/prepare",
                          params={"guid": LOCAL_HEX}).json() == SENTINEL
        assert client.post("/music/api/v1/download/track/transcode/status",
                           json={"taskGuid": "unknown-task"}).json() == SENTINEL
        assert client.get("/music/api/v1/download/track/transcode/file",
                          params={"taskGuid": "unknown-task"}).json() == SENTINEL
        assert client.post("/music/api/v1/download/track/transcode/delete",
                           json={"taskGuid": "unknown-task"}).json() == SENTINEL

    seen = {(c["method"], c["path"]) for c in calls}
    assert ("GET", "/music/api/v1/track/hls/" + LOCAL_HEX + "/preset.m3u8") in seen
    assert ("GET", "/music/api/v1/track/hls/" + LOCAL_HEX + "/00000.m4s") in seen
    assert ("POST", "/music/api/v1/track/transcode") in seen
    assert ("POST", "/music/api/v1/track/transcode/heartbeat") in seen
    assert ("GET", "/music/api/v1/download/track/transcode/prepare") in seen
    assert ("POST", "/music/api/v1/download/track/transcode/status") in seen
    assert ("GET", "/music/api/v1/download/track/transcode/file") in seen
    assert ("POST", "/music/api/v1/download/track/transcode/delete") in seen
    # query 原样保留
    prep_call = next(c for c in calls if c["path"].endswith("/download/track/transcode/prepare"))
    assert "guid=" + LOCAL_HEX in prep_call["query"]


def test_forward_get_retries_on_connection_error(env, monkeypatch):
    attempts = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("stale uds connection")
        return httpx.Response(200, json=SENTINEL)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(flaky), base_url="http://unix")
    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/track/hls/{LOCAL_HEX}/preset.m3u8")
        assert resp.status_code == 200
        assert resp.json() == SENTINEL
    assert attempts["n"] == 2      # 连接级失败重试一次后成功


def test_forward_post_does_not_retry(env):
    attempts = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("boom")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(flaky), base_url="http://unix")
    with TestClient(app) as client:
        with pytest.raises(httpx.ConnectError):
            client.post("/music/api/v1/track/transcode", json={"guid": LOCAL_HEX})
    assert attempts["n"] == 1      # 非幂等方法不重试


# === .ref 自愈：已落库优先（标签查 music.db） ===

def make_library_db(db_path: str, tracks: list[dict]) -> None:
    con = sqlite3.connect(db_path)
    con.executescript("""
        DROP TABLE IF EXISTS track; DROP TABLE IF EXISTS artist;
        DROP TABLE IF EXISTS track_artist; DROP TABLE IF EXISTS album;
        DROP TABLE IF EXISTS audio_file;
        CREATE TABLE track (id INTEGER PRIMARY KEY, guid TEXT, title TEXT,
                            year INTEGER, album_id INTEGER, audio_file_id INTEGER);
        CREATE TABLE artist (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE track_artist (track_id INTEGER, artist_id INTEGER);
        CREATE TABLE album (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE audio_file (id INTEGER PRIMARY KEY, path TEXT);
    """)
    for t in tracks:
        con.execute("INSERT OR IGNORE INTO artist (id, name) VALUES (?, ?)", (t["artist_id"], t["artist"]))
        con.execute("INSERT OR IGNORE INTO album (id, name) VALUES (?, ?)", (t["album_id"], t["album"]))
        con.execute("INSERT INTO audio_file (id, path) VALUES (?, ?)", (t["file_id"], t["path"]))
        con.execute(
            "INSERT INTO track (id, guid, title, year, album_id, audio_file_id)"
            " VALUES (?, ?, ?, 2020, ?, ?)",
            (t["id"], t["guid"], t["title"], t["album_id"], t["file_id"]))
        con.execute("INSERT INTO track_artist (track_id, artist_id) VALUES (?, ?)",
                    (t["id"], t["artist_id"]))
    con.commit()
    con.close()


def write_play_snapshot(guid: str, title: str, artist: str, album: str = "") -> None:
    path = os.path.join(os.environ["FNMUSIC_PLAY_HISTORY_DIR"], "user-a.json")
    snap = {"guid": guid, "title": title, "artist": artist}
    if album:
        snap["album"] = {"name": album}
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"items": [{"guid": guid, "track": snap}]}, f, ensure_ascii=False)


def test_find_cache_file_self_heals_ref_via_tags(env):
    audio = os.path.join(env["dirs"]["library"], "周杰伦 - 晴天.mp3")
    with open(audio, "wb") as f:
        f.write(b"MP3DATA" * 100)
    make_library_db(str(env["tmp"]) + "/music.db", [{
        "id": 10, "guid": "official-1", "title": "晴天", "artist": "周杰伦",
        "artist_id": 1, "album": "叶惠美", "album_id": 1, "file_id": 7, "path": audio,
    }])
    write_play_snapshot(FAKE_KUWO, "晴天", "周杰伦", "叶惠美")

    # .ref 缺失（根因场景）：标签兜底找回并自愈 .ref
    assert appmod.find_cache_file(FAKE_KUWO) == audio
    assert os.path.exists(appmod.media_ref_path(FAKE_KUWO))
    # 自愈后走 recall，路径不变
    assert appmod.find_cache_file(FAKE_KUWO) == audio


def test_find_cache_file_ignores_guid_without_snapshot(env):
    audio = os.path.join(env["dirs"]["library"], "路人甲 - 无关歌曲.flac")
    with open(audio, "wb") as f:
        f.write(b"F" * 64)
    make_library_db(str(env["tmp"]) + "/music.db", [{
        "id": 11, "guid": "official-2", "title": "无关歌曲", "artist": "路人甲",
        "artist_id": 2, "album": "", "album_id": 2, "file_id": 8, "path": audio,
    }])
    # 库里有歌但没有该 guid 的播放快照 → 不误配
    assert appmod.find_cache_file(FAKE_KUWO) is None
    assert appmod.find_cache_file(FAKE_KUWO) is None   # 阴性缓存下重复调用稳定


def test_library_file_by_tags_matching_rules(env):
    a = os.path.join(env["dirs"]["library"], "a.mp3")
    b = os.path.join(env["dirs"]["library"], "b.mp3")
    for p in (a, b):
        with open(p, "wb") as f:
            f.write(b"X" * 64)
    make_library_db(str(env["tmp"]) + "/music.db", [
        {"id": 20, "guid": "o-a", "title": "Sunny Day", "artist": "Artist A",
         "artist_id": 10, "album": "AlbumA", "album_id": 10, "file_id": 20, "path": a},
        {"id": 21, "guid": "o-b", "title": "sunny day", "artist": "artist a",
         "artist_id": 10, "album": "AlbumB", "album_id": 11, "file_id": 21, "path": b},
    ])
    # 精确未命中 → NOCASE 兜底命中；album 参与收紧
    assert appmod.library_file_by_tags("Sunny Day", "Artist A", "AlbumB") == b
    assert appmod.library_file_by_tags("sunny day", "artist a") == b      # 最新一条优先
    assert appmod.library_file_by_tags("Sunny Day", "别的歌手") is None
    assert appmod.library_file_by_tags("", "Artist A") is None            # 缺 title 不查

    # 磁盘文件不存在的悬空记录不算命中
    make_library_db(str(env["tmp"]) + "/music.db", [
        {"id": 22, "guid": "o-c", "title": "Gone", "artist": "Artist A",
         "artist_id": 10, "album": "", "album_id": 10, "file_id": 22, "path": "/nonexistent/x.mp3"},
    ])
    assert appmod.library_file_by_tags("Gone", "Artist A") is None


def test_remember_media_path_roundtrip(env):
    audio = os.path.join(env["dirs"]["library"], "歌 - 曲.flac")
    with open(audio, "wb") as f:
        f.write(b"F" * 32)
    appmod.remember_media_path(FAKE_KUWO, audio)
    stem = appmod.recalled_media_stem(FAKE_KUWO)
    assert stem == audio[:-len(".flac")]       # 记词干不记扩展名
