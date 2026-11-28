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
from pathlib import Path
from unittest.mock import AsyncMock, Mock

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
    monkeypatch.setitem(p.CONF, "tee_save_by_user", False)
    monkeypatch.setitem(p.CONF, "tee_save_dir_by_user", "{}")
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


async def listen(asgi_audio, guid, title="晴天", artist="周杰伦", ext="flac"):
    """完整试听并等待 ASGI 响应结束后的入库。"""
    info = {"title": title, "artist": artist, "album": "叶惠美"}
    response = httpx.Response(200, stream=Audio())
    tee_response = p.stream_tee_response(
        response, guid, None, pre_info=info, resolved_ext=ext)
    body = await asgi_audio(tee_response)
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
@pytest.mark.parametrize("per_user", [False, True])
async def test_favorite_during_playback_lands_in_library(monkeypatch, asgi_audio, per_user):
    """播放中收藏（tee 关）：流完成按入库语义 finalize，文件进曲库而非滚动缓存。"""
    guid = "online:netease:186016"
    monkeypatch.setitem(p.CONF, "tee_save_by_user", per_user)
    resolve_user = AsyncMock(return_value="user-a")
    monkeypatch.setattr(p, "_resolve_storage_user", resolve_user)
    scan = Mock()
    monkeypatch.setattr(p, "_schedule_library_scan", scan)
    calls = _record_dispatch(monkeypatch)
    info = {"title": "晴天", "artist": "周杰伦", "album": "叶惠美"}
    response = p.stream_tee_response(
        httpx.Response(200, stream=Audio()), guid, None, pre_info=info, resolved_ext="flac",
        scan_headers_factory=lambda: {"cookie": "sid=playback"},
    )
    def favorite(chunk):
        assert chunk == Audio.payload
        _save_favorite_snapshot("user-a", guid, "晴天")
        p._register_bind_intent(guid, "user-a", {"cookie": "sid=favorite"}, None)

    assert await asgi_audio(response, on_audio=favorite) == Audio.payload
    root = p.tee_save_dir(user_guid="user-a") if per_user else p.CONF["library_dir"]
    assert audio_files(root) == ["周杰伦 - 晴天.flac"]
    assert list(Path(p.CONF["library_dir"]).rglob("*.flac")) == [Path(root) / "周杰伦 - 晴天.flac"]
    assert audio_files(p.CONF["cache_dir"]) == []
    scan.assert_called_once_with({"cookie": "sid=playback"}, user_guid="user-a" if per_user else "")
    assert not p._full_fetch_tasks
    assert resolve_user.await_count == int(per_user)
    assert len(calls) == 1
    disp_guid, headers, meta = calls[0]
    assert disp_guid == guid
    assert headers is None
    assert meta and meta.get("title") == "晴天" and meta.get("artist") == "周杰伦"
    assert os.path.dirname(meta["dest"]) == root


@pytest.mark.anyio
@pytest.mark.parametrize("intent_state", [
    "disabled", "removed", "playlist", "other-user", "unverified", "lookup-error", "mode-changed",
])
async def test_tee_off_playback_rejects_invalid_save_intent(monkeypatch, asgi_audio, intent_state):
    guid = "online:netease:issue156-save-intent"
    user_guid = "user-a"
    if intent_state != "removed":
        _save_favorite_snapshot(user_guid, guid, "晴天")
    playlist = "old-auto-queue" if intent_state == "playlist" else None
    p._register_bind_intent(guid, user_guid, {"cookie": "sid=favorite"}, playlist)
    monkeypatch.setitem(p.CONF, "fav_auto_bind", intent_state != "disabled")
    per_user = intent_state in ("other-user", "unverified", "lookup-error", "mode-changed")
    monkeypatch.setitem(p.CONF, "tee_save_by_user", per_user)

    async def resolve_user(headers):
        if intent_state == "lookup-error":
            raise ValueError("invalid user response")
        if intent_state == "mode-changed":
            monkeypatch.setitem(p.CONF, "tee_save_by_user", False)
            return user_guid
        return "user-b" if intent_state == "other-user" else None

    monkeypatch.setattr(p, "_resolve_storage_user", resolve_user)
    scan = Mock()
    monkeypatch.setattr(p, "_schedule_library_scan", scan)
    calls = _record_dispatch(monkeypatch)
    post_finalize = AsyncMock()
    response = p.stream_tee_response(
        httpx.Response(200, stream=Audio()), guid, None,
        pre_info={"title": "晴天", "artist": "周杰伦"}, resolved_ext="mp3",
        scan_headers_factory=lambda: {"cookie": "sid=playback"}, post_finalize=post_finalize,
    )

    assert await asgi_audio(response) == Audio.payload
    assert not list(Path(p.CONF["library_dir"]).rglob("*.mp3"))
    cached = Path(p.CONF["cache_dir"]) / f"{p.cache_safe_guid(guid)}.mp3"
    assert cached.is_file()
    assert cached.read_bytes() == Audio.payload
    assert not p._full_fetch_tasks
    post_finalize.assert_not_awaited()
    scan.assert_not_called()
    assert calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("per_user", [False, True])
async def test_favorite_after_complete_promotes_rolling_cache(monkeypatch, asgi_audio, per_user):
    """先播完再收藏：共享模式转存，按用户模式复制，均复用现有缓存。"""
    guid = "online:netease:186016"
    monkeypatch.setitem(p.CONF, "tee_save_by_user", per_user)
    monkeypatch.setattr(p, "_ensure_user_save_tasks", {})
    monkeypatch.setattr(p, "_resolve_storage_user", AsyncMock(return_value="user-a"))
    await listen(asgi_audio, guid)
    cache_dir, library_dir = p.CONF["cache_dir"], p.CONF["library_dir"]
    cached = os.path.join(cache_dir, "online_netease_186016.flac")
    assert os.path.isfile(cached)
    assert audio_files(library_dir) == []
    _save_favorite_snapshot("user-a", guid, "晴天")
    calls = _record_dispatch(monkeypatch)
    p._register_fav_autobind(_fake_request(), guid, "user-a")
    if per_user:
        tasks = tuple(p._ensure_user_save_tasks.values())
        assert len(tasks) == 1
        await asyncio.gather(*tasks)
        library_dir = p.tee_save_dir(user_guid="user-a")
    dest = os.path.join(library_dir, "周杰伦 - 晴天.flac")
    await _wait_until(lambda: os.path.isfile(dest))
    assert os.path.exists(cached) == per_user
    assert not p._full_fetch_tasks
    assert len(calls) == 1
    disp_guid, headers, meta = calls[0]
    assert disp_guid == guid
    assert meta and meta["dest"] == dest
    assert meta.get("title") == "晴天" and meta.get("artist") == "周杰伦"


@pytest.mark.anyio
async def test_list_self_heal_promotes_rolling_cache(monkeypatch, asgi_audio):
    """列表读取自愈：pending 收藏命中滚动缓存时先转存再绑定，不再空等官方收录。"""
    guid = "online:kuwo:228908"
    await listen(asgi_audio, guid, title="歌一")
    _save_favorite_snapshot("user-a", guid, "歌一")
    calls = _record_dispatch(monkeypatch)
    await p._maybe_retry_official_binds(
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
async def test_promote_dedup_concurrent_schedules(monkeypatch, asgi_audio):
    """同 guid 在途去重：重复收藏/自愈并发触发只转存一次。"""
    guid = "online:netease:186016"
    await listen(asgi_audio, guid)
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
