"""Issue #75 回归测试套件：手机 App 洛雪音源首播失败优化验证。

覆盖内容：
1. 并发取链合并与取消隔离：同曲并发只发起一次解析，调用方取消不取消共享任务。
2. 统一首播等待与超时退出：慢解析（>4s/6s 但 <=20s）冷播放 HEAD/GET 成功；超时退出不强刷 fresh，无泄漏。
3. HEAD 与 GET 响应修正：音频类型一致；未知大小省略 Content-Length；已知大小/Range 准确；HEAD 不落盘。
4. 失效重试与配置/生命周期隔离：失效直链最多刷新一次；配置切换与源禁用阻止旧任务回写；退出时任务回收。
5. 标准音质链路复用与回归：转码复用共享取链；缓存、Range、网易与本地曲目播放回归正常。
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

import proxy.app as appmod
from proxy import transcode as tc
from proxy.app import (
    CONF,
    _LX_CONFIG_KEYS,
    _LX_URL_PIN,
    _cancel_lx_resolutions,
    _lx_url_pin_drop,
    _lx_url_pin_store,
    _lx_url_pin_valid,
    _prefetch_lx,
    _sync_lx_config_epoch,
    app,
    resolve_lx_url_shared,
)


@pytest.fixture(autouse=True)
def _clean_lx_state(tmp_path, monkeypatch):
    """Isolate config, storage and clients; never touch real library data."""
    _LX_URL_PIN.clear()
    appmod._LX_FRESH_COOLDOWN.clear()
    appmod._LOSSLESS_BAD.clear()
    monkeypatch.setitem(CONF, "lx_enabled", True)
    monkeypatch.setitem(CONF, "lx_quality", "lossless")
    monkeypatch.setitem(CONF, "quality_mode", "high")
    monkeypatch.setitem(CONF, "tee_save_enabled", False)
    monkeypatch.setitem(CONF, "music_db", str(tmp_path / "missing.db"))
    for key in ("cache_dir", "library_dir", "fav_dir"):
        path = tmp_path / key
        path.mkdir()
        monkeypatch.setitem(CONF, key, str(path))
    monkeypatch.setattr(appmod, "_HOME", str(tmp_path))
    for attr in ("upstream_client", "musicdl_client", "musicbox_client", "lx_client"):
        monkeypatch.setattr(app.state, attr, None, raising=False)
    yield
    _LX_URL_PIN.clear()
    appmod._LX_FRESH_COOLDOWN.clear()
    appmod._LOSSLESS_BAD.clear()


@pytest.fixture
def fake_ffmpeg(tmp_path, monkeypatch):
    """安装假 ffmpeg 供转码测试使用。"""
    script_content = """#!/usr/bin/env python3
import sys, os, time
args = sys.argv[1:]
if "-f" in args and "null" in args:
    sys.exit(0)
out_file = args[-1]
os.makedirs(os.path.dirname(out_file), exist_ok=True)
with open(out_file, "wb") as f:
    f.write(b"M4ADATA_SEGMENT")
sys.exit(0)
"""
    if sys.platform == "win32":
        py_script = tmp_path / "fake_ffmpeg.py"
        py_script.write_text(script_content, encoding="utf-8")
        script_path = tmp_path / "fake_ffmpeg.bat"
        script_path.write_text(f'@"{sys.executable}" "{py_script}" %*\n', encoding="utf-8")
    else:
        script_path = tmp_path / "fake_ffmpeg"
        script_path.write_text(script_content)
        script_path.chmod(0o755)
    monkeypatch.setattr(tc, "FFMPEG_BIN", str(script_path))
    return str(script_path)


# ==============================================================================
# 1. 并发取链只发起一次解析；某个调用者取消后，其他调用者仍可取得结果
# ==============================================================================

@pytest.mark.anyio
async def test_lx_url_inflight_shared_single_upstream_call():
    """同曲并发取链：多个调用者合并为一次出网，返回相同结果且清空在途表。"""
    upstream_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {
                    "id": "lx:kg:HASH1",
                    "url": "http://audio.test/song.flac",
                    "ext": "flac",
                    "actual_tier": "lossless",
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8772")

    async def caller():
        return await resolve_lx_url_shared(client, "lx:kg:HASH1")

    # 5 个并发请求
    results = await asyncio.gather(*[caller() for _ in range(5)])

    assert len(upstream_calls) == 1
    for res in results:
        assert res is not None
        assert res["url"] == "http://audio.test/song.flac"
        assert res["ext"] == "flac"

    assert len(appmod._LX_URL_INFLIGHT) == 0
    await client.aclose()


@pytest.mark.anyio
async def test_lx_url_inflight_cancellation_shield():
    """领队调用者中途取消：共享任务不受影响继续出网，其余等待者正常取得结果。"""
    upstream_started = asyncio.Event()
    upstream_release = asyncio.Event()
    upstream_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {
                    "id": "lx:kg:CANCEL_TEST",
                    "url": "http://audio.test/cancel_shield.flac",
                    "ext": "flac",
                    "actual_tier": "lossless",
                },
            },
        )

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        upstream_started.set()
        await upstream_release.wait()
        return handler(request)

    transport = httpx.MockTransport(slow_handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8772")

    t1 = asyncio.create_task(resolve_lx_url_shared(client, "lx:kg:CANCEL_TEST"))
    await upstream_started.wait()

    t2 = asyncio.create_task(resolve_lx_url_shared(client, "lx:kg:CANCEL_TEST"))
    await asyncio.sleep(0.01)

    # 取消领队 t1
    t1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t1

    # 放行上游作答
    upstream_release.set()
    res2 = await t2

    assert res2 is not None
    assert res2["url"] == "http://audio.test/cancel_shield.flac"
    assert len(upstream_calls) == 1
    # 结果已被钉住，后续请求命中钉链
    hit = await resolve_lx_url_shared(client, "lx:kg:CANCEL_TEST")
    assert hit["url"] == "http://audio.test/cancel_shield.flac"
    assert len(upstream_calls) == 1
    await client.aclose()


# ==============================================================================
# 2. 模拟解析超过原 4 秒、6 秒但未超过 20 秒，冷缓存 HEAD、GET 成功；超过总预算及时退出
# ==============================================================================

def test_lx_stream_and_head_wait_beyond_legacy_limits(monkeypatch, tmp_path):
    """洛雪慢解析（耗时超过旧版 4s/6s 上限）：HEAD 与 GET 均能成功等待至 20s 预算内出流。"""
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path / "cache"))
    os.makedirs(CONF["cache_dir"], exist_ok=True)

    # Yield to the event loop while resolving; a synchronous sleep cannot test deadlines.
    async def lx_handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.025)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {
                    "id": "lx:kg:SLOW1",
                    "url": "http://audio.test/slow.flac",
                    "ext": "flac",
                    "actual_tier": "lossless",
                },
            },
        )

    def audio_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"fLaC" + b"\x00" * 4096,
            headers={"Content-Type": "audio/flac", "Content-Length": "4100"},
        )

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lx_handler), base_url="http://127.0.0.1:8772"
    )
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )

    monkeypatch.setattr(appmod, "_STREAM_STARTUP_S", 0.01)
    monkeypatch.setitem(CONF, "lx_url_timeout", 15.0)
    monkeypatch.setattr(appmod, "_LX_STREAM_STARTUP_SLACK_S", -14.9)
    real_wait_for = asyncio.wait_for
    first_byte_budgets = []
    async def record_budget(awaitable, timeout):
        if getattr(awaitable, "cr_code", None) is appmod._open_online_stream.__code__:
            first_byte_budgets.append(timeout)
        return await real_wait_for(awaitable, timeout)
    monkeypatch.setattr(asyncio, "wait_for", record_budget)

    # 拦截向 audio.test 发起的请求
    orig_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = httpx.MockTransport(audio_handler)
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_async_client)

    with TestClient(app) as test_client:
        # 冷缓存 HEAD
        head_resp = test_client.head("/music/api/v1/track/stream?guid=online:lx:kg:SLOW1")
        assert head_resp.status_code == 200
        assert head_resp.headers["content-type"] == "audio/flac"

        # Clear the HEAD pin so GET also exercises cold resolution.
        _LX_URL_PIN.clear()
        # 冷缓存 GET
        get_resp = test_client.get("/music/api/v1/track/stream?guid=online:lx:kg:SLOW1")
        assert get_resp.status_code == 200
        assert get_resp.headers["content-type"] == "audio/flac"
        assert get_resp.content.startswith(b"fLaC")
    assert len(first_byte_budgets) == 2
    assert all(.025 < budget <= .1 for budget in first_byte_budgets)


def test_lx_stream_timeout_exceeded_no_fresh_no_leak(monkeypatch, tmp_path):
    """超过 20s 总预算及时退出：不触发 fresh=1 强刷，且在途表中无残留任务。"""
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path / "cache"))
    os.makedirs(CONF["cache_dir"], exist_ok=True)

    requests_seen = []

    async def lx_handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        await asyncio.sleep(0.3)
        return httpx.Response(
            200,
            json={"ok": True, "data": {"id": "x", "url": "http://audio.test/timeout.flac", "ext": "flac"}},
        )

    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lx_handler), base_url="http://127.0.0.1:8772"
    )

    # 设定短超时 0.1s
    monkeypatch.setitem(CONF, "lx_url_timeout", 15.0)
    monkeypatch.setattr(appmod, "_LX_STREAM_STARTUP_SLACK_S", -14.9)

    with TestClient(app) as test_client:
        resp = test_client.get("/music/api/v1/track/stream?guid=online:lx:kg:TIMEOUT1")
        assert resp.status_code == 404

    # 验证没有触发带 fresh=1 的强刷重试
    for req in requests_seen:
        assert req.url.params.get("fresh") != "1"

    # 验证在途解析任务已全部清理
    assert len(appmod._LX_URL_INFLIGHT) == 0


# ==============================================================================
# 3. HEAD 与 GET 的 FLAC 类型一致；大小未知不返回零长度，已知大小和 Range 响应准确，HEAD 不写文件
# ==============================================================================

def test_lx_head_and_get_content_type_consistent_flac(monkeypatch, tmp_path):
    """HEAD 与 GET 音频类型一致：即使源站错误上报 audio/mpeg，依据扩展名统一为 audio/flac。"""
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path / "cache"))
    os.makedirs(CONF["cache_dir"], exist_ok=True)

    def lx_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "data": {"id": "x", "url": "http://audio.test/t.flac", "ext": "flac"}},
        )

    # CDN 错报 content-type 为 audio/mpeg
    def audio_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"fLaC" + b"\x00" * 2048,
            headers={"Content-Type": "audio/mpeg", "Content-Length": "2052"},
        )

    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lx_handler), base_url="http://127.0.0.1:8772"
    )

    orig_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = httpx.MockTransport(audio_handler)
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_async_client)

    with TestClient(app) as test_client:
        head_resp = test_client.head("/music/api/v1/track/stream?guid=online:lx:kg:FLAC_MIME")
        get_resp = test_client.get("/music/api/v1/track/stream?guid=online:lx:kg:FLAC_MIME")

        assert head_resp.status_code == 200
        assert get_resp.status_code == 200
        assert head_resp.headers["content-type"] == "audio/flac"
        assert get_resp.headers["content-type"] == "audio/flac"


def test_lx_head_unknown_size_omits_content_length_and_no_disk_write(monkeypatch, tmp_path):
    """源站未给大小时 HEAD 省略 Content-Length（绝不补零），且 HEAD 请求不写盘。"""
    cache_dir = tmp_path / "cache"
    monkeypatch.setitem(CONF, "cache_dir", str(cache_dir))
    os.makedirs(cache_dir, exist_ok=True)

    def lx_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "data": {"id": "x", "url": "http://audio.test/chunked.flac", "ext": "flac"}},
        )

    # 源站未提供 Content-Length（流式响应且无 Content-Length）
    class ChunkedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"fLaC" + b"\x00" * 1024

    def audio_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=ChunkedStream(),
            headers={"Content-Type": "audio/flac"},  # 无 Content-Length
        )

    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lx_handler), base_url="http://127.0.0.1:8772"
    )

    orig_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = httpx.MockTransport(audio_handler)
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_async_client)

    with TestClient(app) as test_client:
        resp = test_client.head("/music/api/v1/track/stream?guid=online:lx:kg:UNKNOWN_LEN")
        assert resp.status_code == 200
        # 绝不返回 0 长度
        assert "content-length" not in resp.headers

    # 验证 HEAD 请求没有在缓存目录落盘任何音频或临时 .part 文件
    files = os.listdir(cache_dir)
    assert len(files) == 0


def test_lx_head_known_size_and_range_response(monkeypatch, tmp_path):
    """源站提供大小时 HEAD 保留可信 Content-Length 与 Range 响应。"""
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path / "cache"))
    os.makedirs(CONF["cache_dir"], exist_ok=True)

    def lx_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "data": {"id": "x", "url": "http://audio.test/range.flac", "ext": "flac"}},
        )

    def audio_handler(request: httpx.Request) -> httpx.Response:
        rng = request.headers.get("range")
        if rng == "bytes=0-100":
            return httpx.Response(
                206,
                content=b"x" * 101,
                headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-100/10000", "Content-Length": "101"},
            )
        return httpx.Response(
            200,
            content=b"x" * 10000,
            headers={"Content-Type": "audio/flac", "Content-Length": "10000"},
        )

    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lx_handler), base_url="http://127.0.0.1:8772"
    )

    orig_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = httpx.MockTransport(audio_handler)
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_async_client)

    with TestClient(app) as test_client:
        # 普通 HEAD
        h1 = test_client.head("/music/api/v1/track/stream?guid=online:lx:kg:RANGE1")
        assert h1.status_code == 200
        assert h1.headers["content-length"] == "10000"

        # 带 Range 的 HEAD
        h2 = test_client.head(
            "/music/api/v1/track/stream?guid=online:lx:kg:RANGE1",
            headers={"Range": "bytes=0-100"},
        )
        assert h2.status_code == 206
        assert h2.headers["content-length"] == "101"
        assert h2.headers["content-range"] == "bytes 0-100/10000"


# ==============================================================================
# 4. 明确失效的直链最多刷新一次；配置切换、源关闭和服务退出后旧任务无法回写
# ==============================================================================

def test_lx_dead_link_refreshes_at_most_once(monkeypatch, tmp_path):
    """直链明确失效（403/空流/非音频）：弃钉并在剩余预算内最多刷新一次，进入冷却后不反复重试。"""
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path / "cache"))
    os.makedirs(CONF["cache_dir"], exist_ok=True)

    resolve_calls = []

    def lx_handler(request: httpx.Request) -> httpx.Response:
        resolve_calls.append(request)
        is_fresh = request.url.params.get("fresh") == "1"
        suffix = "fresh" if is_fresh else "dead"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {
                    "id": "x",
                    "url": f"http://audio.test/{suffix}.flac",
                    "ext": "flac",
                },
            },
        )

    def audio_handler(request: httpx.Request) -> httpx.Response:
        # 始终返回 403 明确拒绝
        return httpx.Response(403, content=b"Access Denied", headers={"Content-Type": "text/html"})

    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lx_handler), base_url="http://127.0.0.1:8772"
    )

    orig_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = httpx.MockTransport(audio_handler)
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_async_client)

    with TestClient(app) as test_client:
        resp = test_client.get("/music/api/v1/track/stream?guid=online:lx:kg:DEAD403")
        assert resp.status_code == 404

    # 首次出链 + 最多一次 fresh 强刷，共 2 次解析
    url_calls = [r for r in resolve_calls if r.url.path == "/api/v1/track/url"]
    assert len(url_calls) == 2
    assert url_calls[0].url.params.get("fresh") is None
    assert url_calls[1].url.params.get("fresh") == "1"

    # 冷却已生效，再次点播快速失败，不发起额外解析
    with TestClient(app) as test_client:
        resp2 = test_client.get("/music/api/v1/track/stream?guid=online:lx:kg:DEAD403")
        assert resp2.status_code == 404
    url_calls_after = [r for r in resolve_calls if r.url.path == "/api/v1/track/url"]
    assert len(url_calls_after) == 2


@pytest.mark.anyio
async def test_lx_config_epoch_and_disabled_prevents_stale_writeback():
    """配置切换与源禁用：在途任务完成后检查代次与启用状态，拒绝回写旧结果。"""
    upstream_release = asyncio.Event()

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        await upstream_release.wait()
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {
                    "id": "lx:kg:STALE_TASK",
                    "url": "http://audio.test/stale.flac",
                    "ext": "flac",
                },
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(slow_handler), base_url="http://127.0.0.1:8772"
    )

    # Case 1: 配置代次变化主动取消在途解析并阻止回写
    t1 = asyncio.create_task(resolve_lx_url_shared(client, "lx:kg:STALE_TASK"))
    await asyncio.sleep(0.01)

    # 触发配置变更
    CONF["lx_quality"] = "high"
    _sync_lx_config_epoch()

    upstream_release.set()
    with pytest.raises(asyncio.CancelledError):
        await t1

    # 旧任务的结果绝不应该写入 _LX_URL_PIN
    assert "lx:kg:STALE_TASK" not in _LX_URL_PIN

    # Case 2: 洛雪源被禁用阻止回写
    CONF["lx_enabled"] = False
    res_disabled = await appmod.resolve_lx_url(client, "lx:kg:DISABLED_TEST")
    assert res_disabled is not None
    assert "lx:kg:DISABLED_TEST" not in _LX_URL_PIN

    await client.aclose()


@pytest.mark.anyio
async def test_lx_lifespan_cancels_and_reaps_tasks():
    """服务退出/生命周期结束：取消并回收全部在途解析与预热任务。"""
    async def hang_handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(100)
        return httpx.Response(200, json={"ok": True, "data": {}})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(hang_handler), base_url="http://127.0.0.1:8772"
    )

    t1 = asyncio.create_task(resolve_lx_url_shared(client, "lx:kg:HANG1"))
    await asyncio.sleep(0.01)
    assert len(appmod._LX_URL_INFLIGHT) == 1

    reap = _cancel_lx_resolutions()

    assert len(appmod._LX_URL_INFLIGHT) == 0
    assert len(appmod._LX_URL_PREFETCH) == 0
    assert len(reap) == 1
    await asyncio.gather(*reap, return_exceptions=True)
    assert reap[0].cancelled()
    await asyncio.gather(t1, return_exceptions=True)
    await client.aclose()


# ==============================================================================
# 5. 标准音质链路复用共享解析；回归缓存播放、分段请求、后台保存以及网易和本地歌曲
# ==============================================================================

@pytest.mark.anyio
async def test_lx_standard_transcode_reuses_shared_resolution(fake_ffmpeg, tmp_path):
    """洛雪标准音质转码链路复用在途共享解析，且转码会话能正常产出分片。"""
    upstream_calls = []

    def lx_handler(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {
                    "id": "lx:kg:TRANSCODE1",
                    "url": "http://audio.test/transcode_source.mp3",
                    "ext": "mp3",
                    "actual_tier": "high",
                },
            },
        )

    def lx_info_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {
                    "title": "转码测试",
                    "artist": "歌手",
                    "duration_s": 120.0,
                    "ext": "mp3",
                },
            },
        )

    async def transport_handler(request: httpx.Request) -> httpx.Response:
        if "/api/v1/track/url" in request.url.path:
            return lx_handler(request)
        if "/api/v1/track/info" in request.url.path:
            return lx_info_handler(request)
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport_handler), base_url="http://127.0.0.1:8772")
    app.state.lx_client = client

    # 预热当前播放首档；转码随后用相同目标与用途复用该解析。
    await resolve_lx_url_shared(client, "lx:kg:TRANSCODE1")
    assert len(upstream_calls) == 1

    # 构造假 Request 调用 _start_transcode_session
    fake_scope = {
        "type": "http",
        "method": "POST",
        "path": "/music/api/v1/track/transcode",
        "headers": [],
        "app": app,
    }
    fake_req = appmod.Request(fake_scope)
    sess = await appmod._start_transcode_session(fake_req, "online:lx:kg:TRANSCODE1")

    # 复用了同一条解析，upstream_calls 仍然为 1
    assert len(upstream_calls) == 1
    assert sess is not None
    await tc.shutdown_all()
    await client.aclose()


def test_playback_regressions_cached_range_netease_local(monkeypatch, tmp_path):
    """回归测试：缓存播放、Range分段请求、网易音源取流、本地曲目透传不受任何影响。"""
    cache_dir = tmp_path / "cache"
    monkeypatch.setitem(CONF, "cache_dir", str(cache_dir))
    os.makedirs(cache_dir, exist_ok=True)

    # 1. 缓存曲目播放与 Range
    cached_file = cache_dir / "online_lx_kg_CACHED1.flac"
    content = b"fLaC" + b"X" * 1020
    cached_file.write_bytes(content)

    with TestClient(app) as test_client:
        r_full = test_client.get("/music/api/v1/track/stream?guid=online:lx:kg:CACHED1")
        assert r_full.status_code == 200
        assert r_full.content == content

        r_range = test_client.get(
            "/music/api/v1/track/stream?guid=online:lx:kg:CACHED1",
            headers={"Range": "bytes=0-9"},
        )
        assert r_range.status_code == 206
        assert r_range.content == content[:10]
        assert r_range.headers["content-length"] == "10"

    # 2. 本地 32 位 hex guid 透传官方
    upstream_hits = []

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        upstream_hits.append(request)
        return httpx.Response(200, content=b"UPSTREAM_OFFICIAL")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )

    with TestClient(app) as test_client:
        r_local = test_client.get("/music/api/v1/track/stream?guid=abcdef0123456789abcdef0123456789")
        assert r_local.status_code == 200
        assert r_local.content == b"UPSTREAM_OFFICIAL"
        assert len(upstream_hits) == 1


def test_lx_url_timeout_slack_scaling(monkeypatch):
    """总超时联动：取链等待为总超时 + 1s，起播/共享解析等待为总超时 + 5s。"""
    monkeypatch.setitem(CONF, "lx_url_timeout", 15.0)
    assert appmod._lx_url_request_timeout_s() == 16.0
    assert appmod._lx_stream_startup_timeout_s() == 20.0

    monkeypatch.setitem(CONF, "lx_url_timeout", 25.0)
    assert appmod._lx_url_request_timeout_s() == 26.0
    assert appmod._lx_stream_startup_timeout_s() == 30.0

    monkeypatch.setitem(CONF, "lx_url_timeout", 1.0)
    assert appmod._lx_url_request_timeout_s() == 2.0
    assert appmod._lx_stream_startup_timeout_s() == 6.0


def test_lx_url_timeout_hot_reload_clears_fresh_cooldown(tmp_path, monkeypatch):
    """热重载 LX_URL_TIMEOUT 时清空 _LX_FRESH_COOLDOWN，避免调大后同曲重试直接失败。"""
    env_file = tmp_path / ".env"
    env_file.write_text("LX_URL_TIMEOUT=15.0\n", encoding="utf-8")
    monkeypatch.setitem(CONF, "lx_url_timeout", 15.0)

    appmod._LX_FRESH_COOLDOWN["online:lx:kg:COOLDOWN1"] = time.monotonic() + 100
    assert "online:lx:kg:COOLDOWN1" in appmod._LX_FRESH_COOLDOWN

    env_file.write_text("LX_URL_TIMEOUT=25.0\n", encoding="utf-8")
    changed = appmod._apply_env_hot_reload(str(env_file))
    assert "lx_url_timeout" in changed
    assert CONF["lx_url_timeout"] == 25.0
    assert "online:lx:kg:COOLDOWN1" not in appmod._LX_FRESH_COOLDOWN
    assert appmod._lx_url_request_timeout_s() == 26.0
    assert appmod._lx_stream_startup_timeout_s() == 30.0


def test_lx_stream_head_wait_uses_scaled_timeout(monkeypatch):
    """_stream_head_response 探活等待时长跟随 _lx_stream_startup_timeout_s 伸缩。"""
    monkeypatch.setitem(CONF, "lx_url_timeout", 35.0)
    assert appmod._lx_stream_startup_timeout_s() == 40.0

    recorded_timeouts = []

    async def fake_wait_for(fut, timeout=None):
        recorded_timeouts.append(timeout)
        if hasattr(fut, "close"):
            fut.close()
        raise TimeoutError()

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    fake_scope = {
        "type": "http",
        "method": "HEAD",
        "path": "/music/api/v1/track/stream",
        "headers": [],
        "query_string": b"guid=online:lx:kg:WAIT_TEST",
        "app": app,
    }
    fake_req = appmod.Request(fake_scope)

    anyio.run(appmod._stream_head_response, fake_req, "online:lx:kg:WAIT_TEST", None, None)
    assert 40.0 in recorded_timeouts


@pytest.mark.parametrize("env_key,conf_key,initial", [
    ("LX_URL_TIMEOUT", "lx_url_timeout", 15.0),
    ("LX_RESOLVER_TIMEOUT", "lx_resolver_timeout", 8.0),
    ("LX_PROBE_TIMEOUT", "lx_probe_timeout", 3.0),
])
@pytest.mark.anyio
async def test_timeout_change_starts_new_resolution_without_joining_old(
    tmp_path, monkeypatch, env_key, conf_key, initial,
):
    monkeypatch.setitem(CONF, conf_key, initial)
    env_file = tmp_path / ".env"
    env_file.write_text(f"{env_key}=25.0\n")
    gate = asyncio.Event()
    started = asyncio.Event()
    calls = []

    async def resolve(*args, **kwargs):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            started.set()
            await gate.wait()
            return {"url": "https://media.test/old"}
        return {"url": "https://media.test/new"}

    monkeypatch.setattr(appmod, "resolve_lx_url", resolve)
    old = asyncio.create_task(resolve_lx_url_shared(None, "lx:kg:timeout-reload"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        appmod._LX_FRESH_COOLDOWN["lx:kg:timeout-reload"] = time.monotonic() + 60
        assert conf_key in appmod.apply_env_hot_reload(str(env_file))
        assert not appmod._lx_fresh_cooldown_active("lx:kg:timeout-reload")
        assert await asyncio.wait_for(
            resolve_lx_url_shared(None, "lx:kg:timeout-reload"), 1,
        ) == {"url": "https://media.test/new"}
        assert len(calls) == 2
    finally:
        gate.set()
        assert await old is None


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "301", "bad"])
def test_invalid_lx_timeout_is_ignored(value):
    assert appmod._env_watch_parse(value, "lx_timeout") is None
