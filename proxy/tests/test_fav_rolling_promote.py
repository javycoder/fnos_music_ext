"""issue #82：边听边存关闭 + 收藏自动绑定开启时，收藏触达的滚动缓存必须转存进曲库。

官方扫描不进 cache 目录：播放落下的试听缓存若只被"已命中文件→调度官方绑定"
短路，绑定必然等待超时。本文件锁定三条修复路径：播放中收藏的流完成入库、
已有完整缓存再收藏的转存、列表读取自愈的转存；并守住元数据缺失不误入库、
并发去重两条边界。
"""
import asyncio
import importlib
import os
import time

import httpx
import pytest

p = importlib.import_module("proxy.app")
gc = importlib.import_module("proxy.cache_gc")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for key in ("cache_dir", "library_dir", "fav_dir", "plt_dir"):
        monkeypatch.setitem(p.CONF, key, str(tmp_path / key))
    monkeypatch.setitem(p.CONF, "music_db", str(tmp_path / "missing.db"))
    monkeypatch.setitem(p.CONF, "tee_save_enabled", False)
    monkeypatch.setitem(p.CONF, "tee_save_dir", "")
    monkeypatch.setitem(p.CONF, "tee_cache_max", 2)
    monkeypatch.setitem(p.CONF, "lyric_auto_dl", False)
    monkeypatch.setitem(p.CONF, "fav_auto_bind", True)
    monkeypatch.setitem(p.CONF, "library_scan_path", "")
    monkeypatch.setattr(p, "_TEE_SAVE_DIR_WARNED", False)
    # 阻止读取真实磁盘上的绑定登记文件
    monkeypatch.setattr(p, "_bind_registry_loaded", True)
    p._full_fetch_tasks.clear()
    p._full_fetch_failed.clear()
    p._bind_pending.clear()
    p._bind_tasks.clear()
    p._bind_retry_last.clear()
    p._tee_active.clear()
    p._tee_handoff_active.clear()
    p._rolling_promote_active.clear()
    p._scan_last_attempt = 0.0
    p._scan_task = None
    for key in ("cache_dir", "library_dir", "fav_dir"):
        os.makedirs(p.CONF[key], exist_ok=True)
    yield
    p._full_fetch_tasks.clear()
    p._full_fetch_failed.clear()
    p._bind_pending.clear()
    p._bind_tasks.clear()
    p._bind_retry_last.clear()
    p._tee_active.clear()
    p._rolling_promote_active.clear()


class Audio(httpx.AsyncByteStream):
    payload = b"A" * 4096

    async def __aiter__(self):
        yield self.payload


async def listen(guid, title="晴天", artist="周杰伦", ext="flac"):
    """完整试听一首歌并消费完整个响应体，触发 stream_tee_response 落盘。"""
    info = {"title": title, "artist": artist, "album": "叶惠美"}
    response = httpx.Response(200, stream=Audio())
    tee_response = p.stream_tee_response(
        response, guid, None, pre_info=info, resolved_ext=ext)
    body = b"".join([chunk async for chunk in tee_response.body_iterator])
    assert body == Audio.payload


def _fake_request():
    return p.Request({"type": "http", "headers": [(b"cookie", b"sid=test")], "app": p.app})


def _record_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(p, "_dispatch_official_binding", lambda *args: calls.append(args))
    return calls


def _save_favorite_snapshot(user, guid, title, artist="周杰伦", album="叶惠美"):
    """与真实收藏接口一致的落盘形状：bind=pending 条目携带曲目快照。"""
    items = [{"guid": guid, "createdAt": int(time.time()), "bind": "pending",
              "track": {"title": title, "artist": artist, "album": album}}]
    assert p.save_online_favorites(user, items)


async def _wait_until(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.01)


def audio_files(directory):
    if not os.path.isdir(directory):
        return []
    return sorted(
        f for f in os.listdir(directory)
        if os.path.splitext(f)[1].lstrip(".").lower() in gc.AUDIO_EXTS
    )


@pytest.mark.anyio
async def test_favorite_during_playback_lands_in_library(monkeypatch):
    """播放中收藏（tee 关）：流完成按入库语义 finalize，文件进曲库而非滚动缓存。"""
    guid = "online:netease:186016"
    p._register_bind_intent(guid, "user-a", {"cookie": "sid=test"}, None)
    calls = _record_dispatch(monkeypatch)
    await listen(guid)
    assert audio_files(p.CONF["library_dir"]) == ["周杰伦 - 晴天.flac"]
    assert audio_files(p.CONF["cache_dir"]) == []
    assert len(calls) == 1
    disp_guid, headers, meta = calls[0]
    assert disp_guid == guid
    assert meta and meta.get("title") == "晴天" and meta.get("artist") == "周杰伦"
    assert os.path.dirname(meta["dest"]) == p.CONF["library_dir"]


@pytest.mark.anyio
async def test_favorite_after_complete_promotes_rolling_cache(monkeypatch):
    """先播完落滚动缓存再收藏：缓存转存进曲库，cache 不留副本，绑定带 meta 调度。"""
    guid = "online:netease:186016"
    await listen(guid)
    cache_dir, library_dir = p.CONF["cache_dir"], p.CONF["library_dir"]
    cached = os.path.join(cache_dir, "online_netease_186016.flac")
    assert os.path.isfile(cached)
    assert audio_files(library_dir) == []
    _save_favorite_snapshot("user-a", guid, "晴天")
    calls = _record_dispatch(monkeypatch)
    p._register_fav_autobind(_fake_request(), guid, "user-a")
    dest = os.path.join(library_dir, "周杰伦 - 晴天.flac")
    await _wait_until(lambda: os.path.isfile(dest))
    assert not os.path.exists(cached)
    assert len(calls) == 1
    disp_guid, headers, meta = calls[0]
    assert disp_guid == guid
    assert meta and meta["dest"] == dest
    assert meta.get("title") == "晴天" and meta.get("artist") == "周杰伦"


@pytest.mark.anyio
async def test_list_self_heal_promotes_rolling_cache(monkeypatch):
    """列表读取自愈：pending 收藏命中滚动缓存时先转存再绑定，不再空等官方收录。"""
    guid = "online:kuwo:228908"
    await listen(guid, title="歌一")
    _save_favorite_snapshot("user-a", guid, "歌一")
    calls = _record_dispatch(monkeypatch)
    p._maybe_retry_official_binds(
        _fake_request(), "user-a", [{"guid": guid, "bind": "pending"}], None)
    dest = os.path.join(p.CONF["library_dir"], "周杰伦 - 歌一.flac")
    await _wait_until(lambda: os.path.isfile(dest))
    assert calls and calls[0][0] == guid
    assert calls[0][2] and calls[0][2]["dest"] == dest


@pytest.mark.anyio
async def test_promote_keeps_cache_when_metadata_missing(monkeypatch):
    """元数据补齐失败：保持缓存形态待自愈重试，不以 unknown 入库、不调度绑定。"""
    guid = "online:kuwo:777"
    cached = os.path.join(p.CONF["cache_dir"], "online_kuwo_777.flac")
    with open(cached, "wb") as f:
        f.write(b"x" * 4096)
    calls = _record_dispatch(monkeypatch)
    p._register_fav_autobind(_fake_request(), guid, "user-a")
    await asyncio.sleep(0.2)
    assert os.path.isfile(cached)
    assert calls == []
    assert audio_files(p.CONF["library_dir"]) == []


@pytest.mark.anyio
async def test_promote_dedup_concurrent_schedules(monkeypatch):
    """同 guid 在途去重：重复收藏/自愈并发触发只转存一次。"""
    guid = "online:netease:186016"
    await listen(guid)
    _save_favorite_snapshot("user-a", guid, "晴天")
    installs = []
    real_install = p._install_library_audio

    def counting_install(*args):
        installs.append(args[1])
        return real_install(*args)

    monkeypatch.setattr(p, "_install_library_audio", counting_install)
    _record_dispatch(monkeypatch)
    p._schedule_rolling_promote(guid, {"cookie": "sid=test"})
    p._schedule_rolling_promote(guid, {"cookie": "sid=test"})
    dest = os.path.join(p.CONF["library_dir"], "周杰伦 - 晴天.flac")
    await _wait_until(lambda: os.path.isfile(dest))
    await asyncio.sleep(0.1)
    assert installs == [guid]
