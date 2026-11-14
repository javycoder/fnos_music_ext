"""Cold playback, protocol, and cancellation regressions for issue #56."""
import asyncio
import importlib
import time

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import Response

proxy = importlib.import_module("proxy.app")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy, "_NETEASE_URL_INFLIGHT", {})
    monkeypatch.setattr(proxy, "_NETEASE_PREFETCH", set())
    monkeypatch.setattr(proxy, "_NETEASE_PREHEATED_PINS", {})
    monkeypatch.setattr(proxy, "_NETEASE_URL_PIN", {})
    monkeypatch.setattr(proxy, "_NETEASE_URL_CONFIG", None)
    monkeypatch.setitem(proxy.CONF, "netease_quality", "lossless")
    monkeypatch.setitem(proxy.CONF, "quality_mode", "high")
    monkeypatch.setitem(proxy.CONF, "netease_enabled", True)
    monkeypatch.setitem(proxy.CONF, "cache_dir", str(tmp_path))


@pytest.mark.anyio
@pytest.mark.parametrize("status", [500, 504])
async def test_proxy_does_not_downgrade_on_transient_failure(status):
    calls = []

    async def handler(request):
        calls.append(request.url.params["quality"])
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mb.test") as client:
        assert await proxy.resolve_netease_url(client, "56") is None
    assert calls == ["lossless"]


@pytest.mark.anyio
async def test_proxy_wait_timeout_then_retry_shares_slow_primary(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(dict(request.url.params))
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"ok": True, "data": {"code": 200, "url": "https://cdn.test/original.flac"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mb.test") as client:
        first = asyncio.create_task(proxy.resolve_netease_url(client, "56"))
        await entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        retry = asyncio.create_task(proxy.resolve_netease_url(client, "56"))
        release.set()
        assert await retry == "https://cdn.test/original.flac"
    assert calls == [{"quality": "lossless"}]


@pytest.mark.anyio
async def test_metadata_preheat_and_stream_share_resolution(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(dict(request.url.params))
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"ok": True, "data": {"code": 200, "url": "https://cdn.test/original.flac"}})

    monkeypatch.setattr(proxy, "find_cache_file", lambda guid: None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mb.test") as client:
        monkeypatch.setattr(proxy, "get_musicbox_client", lambda app: client)
        request = Request({"type": "http", "app": proxy.app, "headers": [], "path": "/", "query_string": b""})
        proxy._prefetch_netease(request, "online:netease:56")
        await entered.wait()
        stream = asyncio.create_task(proxy.resolve_netease_url(client, "56"))
        release.set()
        assert await stream == "https://cdn.test/original.flac"
        await asyncio.gather(*list(proxy._NETEASE_PREFETCH))
    assert len(calls) == 1
    assert not proxy._NETEASE_PREHEATED_PINS


@pytest.mark.anyio
@pytest.mark.parametrize("quality_key", ["netease_quality", "play_quality"])
async def test_quality_change_discards_unused_preheat_but_preserves_playing_pin(monkeypatch, quality_key):
    monkeypatch.setitem(proxy.CONF, "play_quality", None)
    async def handler(request):
        quality = request.url.params["quality"]
        return httpx.Response(200, json={"ok": True, "data": {
            "code": 200, "url": f"https://cdn.test/{quality}.flac"}})

    request = Request({"type": "http", "app": proxy.app, "headers": [], "path": "/", "query_string": b""})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mb.test") as client:
        monkeypatch.setattr(proxy, "get_musicbox_client", lambda app: client)
        proxy._prefetch_netease(request, "online:netease:56")
        await asyncio.gather(*list(proxy._NETEASE_PREFETCH))
        assert "56" in proxy._NETEASE_PREHEATED_PINS
        await proxy.resolve_netease_url(client, "57")
        playing = proxy._NETEASE_URL_PIN["57"]
        monkeypatch.setitem(proxy.CONF, quality_key, "exhigh")
        proxy._sync_netease_quality()
        assert "56" not in proxy._NETEASE_URL_PIN
        assert proxy._NETEASE_URL_PIN["57"] is playing
        assert await proxy.resolve_netease_url(client, "56") == "https://cdn.test/exhigh.flac"


@pytest.mark.anyio
async def test_prefetch_bounded_and_skips_local_audio(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        if len(calls) == 2:
            entered.set()
        await release.wait()
        return httpx.Response(200, json={"ok": True, "data": {"code": 404, "url": None}})

    monkeypatch.setattr(proxy, "find_cache_file", lambda guid: "/cached.flac" if guid.endswith(":1") else None)
    request = Request({"type": "http", "app": proxy.app, "headers": [], "path": "/", "query_string": b""})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mb.test") as client:
        monkeypatch.setattr(proxy, "get_musicbox_client", lambda app: client)
        for song_id in range(1, 10):
            proxy._prefetch_netease(request, f"online:netease:{song_id}")
        await entered.wait()
        assert len(proxy._NETEASE_PREFETCH) == 2
        assert set(calls) == {"/api/v1/song/2/url", "/api/v1/song/3/url"}
        release.set()
        await asyncio.gather(*list(proxy._NETEASE_PREFETCH))


@pytest.mark.anyio
async def test_proxy_total_budget_cancels_endpoint_without_downgrade(monkeypatch):
    cancelled = asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request.url.params["quality"])
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(proxy, "_NETEASE_URL_BUDGET_S", 0.03)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mb.test") as client:
        assert await proxy.resolve_netease_url(client, "56") is None
    assert cancelled.is_set() and calls == ["lossless"]
    assert not proxy._NETEASE_URL_INFLIGHT


@pytest.mark.anyio
async def test_metadata_returns_retained_info_without_lyrics_and_preheats(monkeypatch):
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "data": {"code": 200, "url": "https://cdn.test/a.flac"}})

    async def no_info(*args, **kwargs):
        raise AssertionError("已有可信元数据不应请求详情或歌词")

    async def favorites(*args):
        return set()

    monkeypatch.setattr(proxy, "_online_meta_for_request", lambda *args: {
        "title": "Fixture", "artist": "Artist", "duration_s": 180, "ext": "flac"})
    monkeypatch.setattr(proxy, "_online_info", no_info)
    monkeypatch.setattr(proxy, "_online_favorite_set", favorites)
    request = Request({"type": "http", "app": proxy.app, "headers": [],
                       "path": "/music/api/v1/track/metadata", "query_string": b"guid=online:netease:56"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mb.test") as client:
        monkeypatch.setattr(proxy, "get_musicbox_client", lambda app: client)
        response = await proxy.track_metadata(request)
        assert response.status_code == 200 and b"Fixture" in response.body
        await asyncio.gather(*list(proxy._NETEASE_PREFETCH))
    assert calls == ["/api/v1/song/56/url"]


@pytest.mark.anyio
async def test_get_second_wait_reuses_first_resolution_without_fresh(monkeypatch):
    calls, refreshes = [], []
    wait_for = asyncio.wait_for

    async def scaled_wait(awaitable, timeout):
        return await wait_for(awaitable, 0.025 if timeout == 6.0 else timeout)

    async def handler(request):
        calls.append(dict(request.url.params))
        await asyncio.sleep(0.035)
        return httpx.Response(200, json={"ok": True, "data": {"code": 200, "url": "https://cdn.test/a.flac"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mb.test") as client:
        async def open_stream(request, guid, range_header, fresh_url=False, refresh=False):
            refreshes.append(refresh)
            url = await proxy.resolve_netease_url(client, "56", refresh=refresh)
            assert url
            return (None, None, "flac", None, None, b"audio")

        monkeypatch.setattr(proxy.asyncio, "wait_for", scaled_wait)
        monkeypatch.setattr(proxy, "_open_online_stream", open_stream)
        monkeypatch.setattr(proxy, "_retained_track", lambda *args: (None, None))
        monkeypatch.setattr(proxy, "_register_full_fetch", lambda *args: None)
        monkeypatch.setattr(proxy, "stream_tee_response", lambda *args, **kwargs: Response(b"audio"))
        request = Request({"type": "http", "app": proxy.app, "method": "GET",
                           "headers": [(b"range", b"bytes=100-200")],
                           "path": "/music/api/v1/track/stream", "query_string": b"guid=online:netease:56"})
        response = await proxy.stream_track(request)
        assert response.status_code == 200
    assert refreshes == [False, False]
    assert calls == [{"quality": "lossless"}]


@pytest.mark.anyio
@pytest.mark.parametrize("status", [503, "timeout"])
async def test_cdn_temporary_failure_preserves_pin_without_refresh(monkeypatch, status):
    pin_url = "https://cdn.test/pinned.flac"
    proxy._netease_url_pin_store("56", pin_url, 300)
    proxy._sync_netease_quality()
    monkeypatch.setattr(proxy, "_retained_track", lambda *args: ({"ext": "flac", "duration_s": 180}, None))
    client_type = httpx.AsyncClient
    calls = []

    async def handler(request):
        calls.append(str(request.url))
        if status == "timeout":
            raise httpx.ConnectTimeout("slow", request=request)
        return httpx.Response(status)

    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.MockTransport(handler), **kwargs))
    request = Request({"type": "http", "app": proxy.app, "method": "GET", "headers": [],
                       "path": "/", "query_string": b""})
    error = httpx.ConnectTimeout if status == "timeout" else httpx.HTTPStatusError
    with pytest.raises(error):
        await proxy._open_online_stream(request, "online:netease:56", "bytes=100-200")
    assert calls == [pin_url]
    assert proxy._NETEASE_URL_PIN["56"][1] > time.monotonic()


@pytest.mark.anyio
async def test_proxy_shutdown_cancels_resolution(monkeypatch):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mb.test") as client:
        task = asyncio.create_task(proxy.resolve_netease_url(client, "56"))
        await entered.wait()
        tasks = proxy._cancel_netease_resolutions()
        await asyncio.gather(*tasks, return_exceptions=True)
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cancelled.is_set() and not proxy._NETEASE_URL_INFLIGHT
