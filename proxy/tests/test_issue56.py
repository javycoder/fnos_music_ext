"""Cold playback, protocol, and cancellation regressions for issue #56."""
import asyncio
import importlib
import json
import sys
import threading
import time
import types
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import Response

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "musicbox-service"))
import playback

proxy = importlib.import_module("proxy.app")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(playback, "_tasks", {})
    monkeypatch.setattr(playback, "_cache", {})
    monkeypatch.setattr(playback, "_api", None)
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
@pytest.mark.parametrize("fresh", [False, True])
async def test_service_shared_resolution_survives_caller_cancel(monkeypatch, fresh):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def request(song_id, quality, generation):
        calls.append((song_id, quality))
        entered.set()
        await release.wait()
        return {"code": 200, "url": "https://cdn.test/original.flac", "expi": 100}

    monkeypatch.setattr(playback, "_request", request)
    first = asyncio.create_task(playback.get_song_url_async(56, "lossless", fresh=fresh))
    await entered.wait()
    second = asyncio.create_task(playback.get_song_url_async(56, "lossless", fresh=fresh))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert (await second)["url"].endswith("original.flac")
    assert calls == [(56, "lossless")]
    assert not playback._tasks


@pytest.mark.anyio
async def test_service_reset_rejects_old_result(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(*args):
        entered.set()
        await release.wait()
        return {"code": 200, "url": "https://cdn.test/old.flac"}

    monkeypatch.setattr(playback, "_request", request)
    task = asyncio.create_task(playback.get_song_url_async(56, "lossless"))
    await entered.wait()
    playback.reset()
    release.set()
    with pytest.raises(playback.UpstreamError, match="login state"):
        await task
    assert not playback._cache


def test_old_login_generation_cannot_merge_response_cookies(monkeypatch):
    class Jar:
        def load(self):
            raise AssertionError("旧登录态不得读取或写入 cookie")

    monkeypatch.setattr(playback, "_api", types.SimpleNamespace(cookie_jar=Jar()))
    playback._merge_cookies(playback._generation - 1, httpx.Cookies({"MUSIC_U": "fixture"}))


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["timeout", "network", "payload"])
async def test_service_errors_are_not_negative_cached(monkeypatch, failure):
    async def request(*args):
        if failure == "timeout":
            raise httpx.ReadTimeout("slow")
        if failure == "network":
            raise httpx.ConnectError("offline")
        raise ValueError("malformed")

    monkeypatch.setattr(playback, "_request", request)
    expected = playback.ResolutionTimeout if failure == "timeout" else playback.UpstreamError
    with pytest.raises(expected):
        await playback.get_song_url_async(56, "lossless")
    assert not playback._cache


@pytest.mark.anyio
async def test_service_budget_cancels_work(monkeypatch):
    cancelled = asyncio.Event()

    async def request(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(playback, "BUDGET_S", 0.03)
    monkeypatch.setattr(playback, "_request", request)
    with pytest.raises(playback.ResolutionTimeout):
        await playback.get_song_url_async(56, "lossless")
    assert cancelled.is_set() and not playback._tasks


@pytest.mark.anyio
async def test_service_wire_encoding_and_global_lock_independence(monkeypatch):
    import netease_ext
    module = types.ModuleType("NEMbox.api")
    payloads, requests = [], []
    module.BASE_URL = "https://music.163.com"
    module.EAPI_BASE_URL = "https://interface.music.163.com"

    def encode(path, payload):
        payloads.append(payload)
        return {"params": "encrypted+payload"}

    module.eapi_encrypt = encode
    module.encrypted_request = lambda params: {"params": "fallback", "encSecKey": "rsa"}
    module.eapi_response_decrypt = lambda value: {"code": 200, "data": []}
    module.music_quality_to_level = lambda quality: quality
    module.level_to_encode_type = lambda quality: "flac"
    monkeypatch.setitem(sys.modules, "NEMbox.api", module)
    monkeypatch.setattr(playback, "_snapshot", lambda generation: ({"__csrf": "fixture"}, {}, []))
    client_type = httpx.AsyncClient

    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"code": 200, "data": []})
        return httpx.Response(200, json={"code": 200, "data": [{"code": 200, "url": "https://cdn.test/a.flac"}]})

    monkeypatch.setattr(playback.httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.MockTransport(handler), **kwargs))
    with netease_ext._api_lock:
        result = await asyncio.wait_for(playback.get_song_url_async(56, "lossless"), 1)
    assert result["url"].endswith(".flac")
    assert payloads[0]["level"] == "lossless"
    assert payloads[0]["ids"] == "[56]"
    assert parse_qs(requests[0].content.decode()) == {"params": ["encrypted+payload"]}
    assert parse_qs(requests[1].content.decode()) == {"params": ["fallback"], "encSecKey": ["rsa"]}


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
async def test_service_cli_only_for_unavailable_protocol(monkeypatch):
    calls = []

    async def request(*args):
        raise playback.CapabilityError()

    async def cli(song_id, quality):
        calls.append((song_id, quality))
        return {"code": 200, "url": "https://cdn.test/fallback.flac"}

    monkeypatch.setattr(playback, "_request", request)
    monkeypatch.setattr(playback, "_cli", cli)
    await playback.get_song_url_async(56, "lossless")
    assert calls == [(56, "lossless")]

    async def timeout(*args):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(playback, "_request", timeout)
    with pytest.raises(playback.ResolutionTimeout):
        await playback.get_song_url_async(57, "lossless")
    assert len(calls) == 1


@pytest.mark.anyio
async def test_service_fresh_inflight_takes_precedence_over_cached_success(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def request(*args):
        calls.append(True)
        if len(calls) > 1:
            entered.set()
            await release.wait()
        return {"code": 200, "url": f"https://cdn.test/{len(calls)}.flac"}

    monkeypatch.setattr(playback, "_request", request)
    await playback.get_song_url_async(56, "lossless")
    fresh = asyncio.create_task(playback.get_song_url_async(56, "lossless", fresh=True))
    await entered.wait()
    ordinary = asyncio.create_task(playback.get_song_url_async(56, "lossless"))
    await asyncio.sleep(0)
    release.set()
    assert (await fresh)["url"] == (await ordinary)["url"] == "https://cdn.test/2.flac"
    assert len(calls) == 2


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
async def test_service_distinct_qualities_resolve_concurrently(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def request(song_id, quality, generation):
        calls.append((song_id, quality))
        if len(calls) == 2:
            entered.set()
        await release.wait()
        return {"code": 200, "url": f"https://cdn.test/{song_id}-{quality}.flac"}

    monkeypatch.setattr(playback, "_request", request)
    tasks = [asyncio.create_task(playback.get_song_url_async(56, quality))
             for quality in ("lossless", "exhigh")]
    await asyncio.wait_for(entered.wait(), 1)
    release.set()
    results = await asyncio.gather(*tasks)
    assert [item["url"] for item in results] == [
        "https://cdn.test/56-lossless.flac", "https://cdn.test/56-exhigh.flac"]


@pytest.mark.anyio
async def test_service_success_expiry_and_negative_cache_separate(monkeypatch):
    now = [100.0]
    calls = []

    async def request(song_id, quality, generation):
        calls.append(song_id)
        if song_id == 57:
            return {"code": 404, "url": None}
        return {"code": 200, "url": f"https://cdn.test/{len(calls)}.flac", "expi": 100}

    # Patch only playback's clock, not the event loop's clock.
    monkeypatch.setattr(playback, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(playback, "_request", request)
    assert (await playback.get_song_url_async(56, "lossless"))["url"].endswith("1.flac")
    now[0] = 139
    assert (await playback.get_song_url_async(56, "lossless"))["url"].endswith("1.flac")
    now[0] = 141
    assert (await playback.get_song_url_async(56, "lossless"))["url"].endswith("2.flac")
    await playback.get_song_url_async(57, "lossless")
    await playback.get_song_url_async(57, "lossless")
    assert calls == [56, 56, 57]


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
