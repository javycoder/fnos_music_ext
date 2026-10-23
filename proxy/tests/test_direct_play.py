import time
import asyncio

import pytest
import httpx

from fastapi import FastAPI, Request

from proxy.direct_play import DirectMediaTarget, DirectTargetBindings
from proxy.media_url import MediaURLDecision

from proxy import app as proxy_app


def target(generation=1):
    return DirectMediaTarget(
        provider="lx",
        platform="kg",
        canonical_id="lx:kg:1",
        url="https://cdn.example/a.flac",
        reuse_deadline=time.monotonic() + 60,
        expiry_source="policy",
        source_generation=generation,
        representation_key="flac:1",
    )


def test_bindings_keep_same_scope_and_guid():
    store = DirectTargetBindings()
    item = target()
    store.put("user", "online:lx:kg:1", item, 1, 60)
    assert store.get("user", "online:lx:kg:1").target is item
    assert store.get("other", "online:lx:kg:1") is None


def test_generation_invalidates_range_target():
    store = DirectTargetBindings()
    store.put("user", "online:lx:kg:1", target(1), 1, 60)
    assert store.get("user", "online:lx:kg:1") is not None
    store.put("user", "online:lx:kg:2", target(2), 2, 60)
    assert store.get("user", "online:lx:kg:1") is None


def test_binding_never_extends_target_reuse_deadline(monkeypatch):
    monkeypatch.setattr("proxy.direct_play.time.monotonic", lambda: 100.0)
    item = target()
    item.reuse_deadline = 100.01
    store = DirectTargetBindings()
    binding = store.put("user", "online:lx:kg:1", item, 1, 60)
    assert binding.expires_at == 100.01
    monkeypatch.setattr("proxy.direct_play.time.monotonic", lambda: 100.02)
    assert store.get("user", "online:lx:kg:1") is None


def make_request(method="GET", path="/music/api/v1/track/stream", headers=None):
    return Request({
        "type": "http", "method": method, "scheme": "https",
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()],
        "server": ("example.test", 443), "client": ("203.0.113.10", 50000),
        "app": FastAPI(),
    })


def direct_target():
    item = target(proxy_app._DIRECT_PLAY_GENERATIONS["lx"])
    item.reuse_deadline = time.monotonic() + 120
    return item


def configure_direct(monkeypatch):
    monkeypatch.setitem(proxy_app.CONF, "lx_enabled", True)
    monkeypatch.setitem(proxy_app.CONF, "lx_direct_play", True)
    monkeypatch.setattr(proxy_app, "_DIRECT_TARGET_BINDINGS", DirectTargetBindings(max_entries=8))
    monkeypatch.setattr(proxy_app, "_source_enabled", lambda _guid: True)
    async def auth(_request):
        return True
    monkeypatch.setattr(proxy_app, "_strict_direct_play_auth", auth)
    monkeypatch.setattr(
        proxy_app, "_resolve_direct_target",
        lambda _request, _guid, _source: _async_result(direct_target()))
    monkeypatch.setattr(
        proxy_app, "validate_media_url",
        lambda _url: MediaURLDecision(True, host="cdn.example"))
    monkeypatch.setattr(proxy_app, "_register_background_fetch", lambda *_args, **_kwargs: False)


def _async_result(value):
    async def result():
        return value
    return result()


@pytest.mark.asyncio
async def test_initial_head_returns_302_without_save_task(monkeypatch):
    configure_direct(monkeypatch)
    saved = []
    monkeypatch.setattr(proxy_app, "_register_background_fetch", lambda *_args, **_kwargs: saved.append(True))
    response = await proxy_app._direct_redirect_response(make_request("HEAD"), "online:lx:kg:1")
    assert response.status_code == 302
    assert response.headers["location"] == "https://cdn.example/a.flac"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert saved == []


@pytest.mark.asyncio
async def test_nonzero_range_reuses_bound_target(monkeypatch):
    configure_direct(monkeypatch)
    first = await proxy_app._direct_redirect_response(make_request("GET"), "online:lx:kg:1")
    assert first.status_code == 302
    calls = []
    async def resolve(request, guid, source):
        calls.append((guid, source))
        return direct_target()
    monkeypatch.setattr(proxy_app, "_resolve_direct_target", resolve)
    second = await proxy_app._direct_redirect_response(
        make_request("GET", headers={"Range": "bytes=100-200"}), "online:lx:kg:1")
    assert second.status_code == 302
    assert calls == []


@pytest.mark.asyncio
async def test_nonzero_range_without_identity_is_rejected(monkeypatch):
    configure_direct(monkeypatch)
    response = await proxy_app._direct_redirect_response(
        make_request("GET", headers={"Range": "bytes=100-"}), "online:lx:kg:1")
    assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("guid,setting,provider", [
    ("online:netease:1", "netease_direct_play", "netease"),
    ("online:kuwo:1", "musicdl_direct_play", "musicdl"),
    ("online:lx:kg:1", "lx_direct_play", "lx"),
])
async def test_all_providers_select_direct_path(monkeypatch, guid, setting, provider):
    configure_direct(monkeypatch)
    monkeypatch.setitem(proxy_app.CONF, setting, True)
    async def resolve(*_args):
        selected = direct_target()
        selected.provider = provider
        selected.source_generation = proxy_app._DIRECT_PLAY_GENERATIONS[provider]
        return selected
    monkeypatch.setattr(proxy_app, "_resolve_direct_target", resolve)
    response = await proxy_app._direct_redirect_response(make_request(), guid)
    assert response.status_code == 302
    assert response.body == b""


@pytest.mark.asyncio
async def test_range_keeps_direct_target_after_background_cache_appears(monkeypatch):
    configure_direct(monkeypatch)
    guid = "online:lx:kg:1"
    await proxy_app._direct_redirect_response(make_request(), guid)
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("a bound continuation must not consult local files")
    monkeypatch.setattr(proxy_app, "resolve_local_play", forbidden)
    monkeypatch.setattr(proxy_app, "find_cache_file", lambda *_args, **_kwargs: "/cached/rewritten.flac")
    response = await proxy_app.stream_track(make_request(headers={"Range": "bytes=123-"}), guid)
    assert response.status_code == 302


@pytest.mark.asyncio
@pytest.mark.parametrize("expired,disabled", [(True, False), (False, True)])
async def test_old_range_cannot_fall_through_to_another_representation(monkeypatch, expired, disabled):
    configure_direct(monkeypatch)
    guid = "online:lx:kg:1"
    await proxy_app._direct_redirect_response(make_request(), guid)
    if expired:
        monkeypatch.setattr("proxy.direct_play.time.monotonic", lambda: float("inf"))
    if disabled:
        monkeypatch.setitem(proxy_app.CONF, "lx_direct_play", False)
    response = await proxy_app.stream_track(make_request(headers={"Range": "bytes=99-"}), guid)
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_head_cache_has_priority_over_direct_resolver(monkeypatch):
    configure_direct(monkeypatch)
    async def no_local(*_args):
        return None
    async def cached_head(*_args):
        return proxy_app.Response(status_code=200)
    async def forbidden(*_args):
        raise AssertionError("cached HEAD must not resolve a CDN URL")
    monkeypatch.setattr(proxy_app, "resolve_local_play", no_local)
    monkeypatch.setattr(proxy_app, "find_cache_file", lambda *_args, **_kwargs: "/cache/audio.flac")
    monkeypatch.setattr(proxy_app, "_stream_head_response", cached_head)
    monkeypatch.setattr(proxy_app, "_resolve_direct_target", forbidden)
    response = await proxy_app.stream_track(make_request("HEAD"), "online:lx:kg:1")
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", [{}, {"Authorization": "Bearer valid"}, {"Cookie": "session=valid"},
                                       {"X-Trim-Music-Temp-Token": "ticket"}, {"authx": "signature"}])
async def test_strict_auth_requires_confirmed_official_user(monkeypatch, credential):
    def handler(request):
        assert any(request.headers.get(key) for key in (
            "authorization", "cookie", "x-trim-music-temp-token", "authx"))
        return httpx.Response(200, json={"code": 0, "data": {"guid": "test-user"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://official") as client:
        monkeypatch.setattr(proxy_app, "get_upstream_client", lambda _app: client)
        assert await proxy_app._strict_direct_play_auth(make_request(headers=credential)) is bool(credential)


@pytest.mark.asyncio
async def test_auth_is_rechecked_for_bound_ranges(monkeypatch):
    configure_direct(monkeypatch)
    guid = "online:lx:kg:1"
    await proxy_app._direct_redirect_response(make_request(), guid)
    async def denied(*_args):
        return False
    monkeypatch.setattr(proxy_app, "_strict_direct_play_auth", denied)
    response = await proxy_app._direct_redirect_response(make_request(headers={"Range": "bytes=100-"}), guid)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_direct_save_is_deduplicated_and_retains_target(monkeypatch):
    monkeypatch.setitem(proxy_app.CONF, "tee_save_enabled", True)
    monkeypatch.setattr(proxy_app, "find_cache_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(proxy_app, "_full_fetch_tasks", {})
    monkeypatch.setattr(proxy_app, "_full_fetch_failed", {})
    monkeypatch.setattr(proxy_app, "_tee_active", {})
    received = []
    release = asyncio.Event()
    async def download(_guid, _headers, **kwargs):
        received.append(kwargs["direct_target"])
        await release.wait()
    monkeypatch.setattr(proxy_app, "_full_fetch_download", download)
    guid = "online:lx:kg:1"
    selected = direct_target()
    assert proxy_app._register_background_fetch(make_request(), guid, "tee_save_enabled", selected)
    assert not proxy_app._register_background_fetch(make_request(), guid, "tee_save_enabled", selected)
    await asyncio.sleep(0)
    assert received == [selected]
    release.set()
    await asyncio.gather(*proxy_app._full_fetch_tasks.values())


@pytest.mark.asyncio
async def test_unsafe_public_target_rejects_redirect(monkeypatch):
    configure_direct(monkeypatch)
    monkeypatch.setattr(
        proxy_app, "validate_media_url",
        lambda _url: MediaURLDecision(False, "host_not_public", "127.0.0.1"))
    response = await proxy_app._direct_redirect_response(make_request("GET"), "online:lx:kg:1")
    assert response.status_code == 502


@pytest.mark.asyncio
async def test_generation_change_prevents_stale_publication(monkeypatch):
    configure_direct(monkeypatch)
    async def resolve(_request, _guid, _source):
        proxy_app._DIRECT_PLAY_GENERATIONS["lx"] += 1
        return direct_target()
    monkeypatch.setattr(proxy_app, "_resolve_direct_target", resolve)
    response = await proxy_app._direct_redirect_response(make_request("GET"), "online:lx:kg:1")
    assert response.status_code == 409
