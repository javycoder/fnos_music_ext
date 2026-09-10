"""Offline LX reliability regressions; every upstream uses MockTransport."""
import asyncio
import time
from collections import Counter

import httpx
import pytest
from fastapi.testclient import TestClient

from test_app import lxapp, setup_http  # reuse the isolated app and autouse fixture


@pytest.mark.skipif(not lxapp.HAS_CRYPTO, reason="pycryptodome not installed")
@pytest.mark.parametrize("path,payload,expected", [
    ("/api/song/enhance/player/url", {"ids": [1], "br": 128000},
     "fa90b329e9614f79e79598f37dc2edb430f8378d2a2796338f0bfdeaef824a22975cda9d96d79e6dc4a59218cdb8199f431875ae1d73405baa720dd56d6adf08ee33c9b7f8f525d258cd0157817b905f87bdc594c57b8752269cb04b64528f296f1e76e1a558f234725ab15ff01dc73e6aa3b102fbe7296ab0db9ea5c46ad12b"),
    ("https://interface3.music.163.com/eapi/song/enhance/player/url",
     {"header": {"os": "pc"}, "ids": [186016], "title": "晴天", "br": 999000},
     "fa90b329e9614f79e79598f37dc2edb430f8378d2a2796338f0bfdeaef824a2206ec7397cb8529217ff9ab0a36f101b05a40c026e630cf4fcf81cebcc046406f6d6caf766395c828d7f724a931804b4958eb6323aafe093fbd1daaefecd38a9c93fdfbbecd03e522b70dd503680acc4e8b643d56273ee9699ece5da523d7306651993814593bc1136995790902e26b4b4ecbfeaef1ba88a015f5de13b972c03fe32dcece68b174aea7528529bd9ef417"),
])
def test_eapi_independent_musicdl_vectors(path, payload, expected):
    # Generated locally from musicdl 2.13.6 modules/utils/neteaseutils.py,
    # EapiCryptoUtils.encryptparams (cryptography backend), NOT LX's own key,
    # serializer, digest, or decryptor. No reference cookies/client code executed.
    assert lxapp._eapi_params(path, payload) == expected


@pytest.mark.parametrize("official_body", [None, {"play_url": "https://media.test/error"}])
def test_real_mg_search_fallback_then_guid_cache(official_body):
    calls = Counter()

    def handler(req):
        calls[req.url.host] += 1
        if req.url.host == "c.music.migu.cn":
            return httpx.Response(200, json={"songs": [{
                "copyrightId": "42", "songName": "晴天", "singers": [{"name": "周杰伦"}],
                "albums": [{"albumName": "叶惠美"}], "length": 269000,
                "lrcUrl": "https://lyric.test/42", "toneFlags": [{"toneType": "SQ"}],
            }]})
        if req.url.host == "music.migu.cn":
            return httpx.Response(200, json={"data": official_body or {}})
        if req.url.host == "api.xcvts.cn":
            assert req.url.params["gm"] == "晴天 周杰伦"
            return httpx.Response(200, json={"code": 200, "title": "晴天", "singer": "周杰伦",
                                           "music_url": "https://media.test/song", "br": 320000})
        if req.url.path == "/error":
            return httpx.Response(200, json={"error": "denied"})
        assert req.url.host == "media.test"
        assert req.headers["referer"] == lxapp.MG_HEADERS["Referer"]
        return httpx.Response(206, content=b"ID3", headers={"Content-Range": "bytes 0-2/9000000"})

    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(lxapp.app) as client:
        data = client.get("/api/v1/search", params={"q": "晴天", "sources": "mg"}).json()
        assert data["errors"] == {}
        item, = data["items"]
        assert item["id"] == "lx:mg:42"
        assert item["album"] == "叶惠美" and item["lrc_url"].endswith("/42")
        assert item["verified"] and item["ext"] == "mp3"
        assert item["completeness"] == "unknown"
        assert item["_probe"]["actual_tier"] == "high"
        before = calls.copy()
        result = client.get("/api/v1/track/url", params={"guid": "migu:42", "quality": "high"})
        assert result.status_code == 200
        assert result.json()["data"]["headers"] == lxapp.MG_HEADERS
        assert calls == before  # no resolver call and no duplicate probe
        assert calls["api.xcvts.cn"] == 1
        assert calls["media.test"] == (2 if official_body else 1)


@pytest.mark.parametrize("ct,body,ok", [
    ("application/octet-stream", b"fLaC" + b"\0" * 40, True),
    ("application/octet-stream", b"ID3", True),
    ("audio/mpeg", b'{"error":"forbidden"}', False),
    ("audio/mpeg", b"not authorized", False),
    ("audio/mpeg", b"", False),
    ("application/octet-stream", b"<html>failure</html>", False),
    ("application/json", b"ID3", False),
    ("text/plain", b"ID3", False),
    ("application/octet-stream", b"OggS" + b"\0" * 20, True),
    ("application/octet-stream", b"RIFF\0\0\0\0WAVE", True),
])
def test_probe_requires_media_signature(ct, body, ok):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, headers={"Content-Type": ct}, content=body)
        )) as client:
            assert (await lxapp.probe_url(client, "https://media.test/a"))[0] is ok
    asyncio.run(run())


@pytest.mark.parametrize("prefix,expected", [(b"ID3", True), (b"x" * 1024, False)])
def test_range_ignored_read_is_bounded_and_closed(prefix, expected):
    class HugeStream(httpx.AsyncByteStream):
        reads = 0
        closed = False

        async def __aiter__(self):
            for _ in range(100000):
                self.reads += 1
                yield prefix + b"\0" * 1024

        async def aclose(self):
            self.closed = True

    stream = HugeStream()

    def handler(req):
        assert req.headers["Range"] == "bytes=0-4095"
        assert req.headers["X-Media-Token"] == "test-token"
        return httpx.Response(200, stream=stream, headers={"Content-Length": "1000000000"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await lxapp.probe_url(client, "https://media.test/a", {"X-Media-Token": "test-token"})
            assert result[0] is expected
    asyncio.run(run())
    assert stream.closed
    assert stream.reads <= 4  # never drains an ignored-range full song/error page


def test_quality_downgrade_and_actual_tier_cache(monkeypatch):
    calls = Counter()

    async def official(client, item, identifier, tier):
        calls[tier] += 1
        return {"url": "https://media.test/" + tier, "br": 128000,
                "headers": {"Referer": "https://required.test/", "X-Media-Token": "ok"}}

    def handler(req):
        calls["probe"] += 1
        assert req.headers["X-Media-Token"] == "ok"
        if req.url.path != "/standard":
            return httpx.Response(200, json={"error": "unavailable quality"})
        return httpx.Response(206, content=b"ID3", headers={"Content-Range": "bytes 0-2/9000000"})

    monkeypatch.setattr(lxapp, "kg_resolve_url", official)
    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(lxapp.app) as client:
        for quality in ("lossless", "lossless", "high", "standard"):
            response = client.get("/api/v1/track/url", params={"id": "lx:kg:42", "quality": quality})
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["actual_tier"] == "standard"
            assert data["quality"] == quality  # old requested-quality API contract
            assert data["completeness"] == "unknown"
        assert calls == {"lossless": 1, "high": 1, "standard": 1, "probe": 3}


def test_trial_metadata_and_expired_url_reresolution(monkeypatch):
    calls = Counter()
    state = {"trial": False}

    async def official(client, identifier, tier):
        calls["resolve"] += 1
        return {"url": "https://media.test/" + str(calls["resolve"]), "br": 128000,
                "freeTrialInfo": {"start": 0, "end": 30} if state["trial"] else None}

    def handler(req):
        calls["probe"] += 1
        # Small valid media is not proof of a trial OR proof of completeness.
        return httpx.Response(206, content=b"ID3", headers={"Content-Range": "bytes 0-2/400000"})

    monkeypatch.setattr(lxapp, "mg_resolve_url", official)
    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    lxapp._cache_put({"id": "lx:mg:42", "duration_s": 269})
    with TestClient(lxapp.app) as client:
        params = {"id": "lx:mg:42", "quality": "standard"}
        first = client.get("/api/v1/track/url", params=params).json()["data"]
        assert first["completeness"] == "unknown"
        assert client.get("/api/v1/track/url", params=params).json()["data"]["url"] == first["url"]
        lxapp._SONG_CACHE["lx:mg:42"]["item"]["_probe"]["ts"] = 0
        second = client.get("/api/v1/track/url", params=params).json()["data"]
        assert second["url"] != first["url"]
        lxapp._SONG_CACHE["lx:mg:42"]["item"]["_probe"]["ts"] = 0
        state["trial"] = True
        assert client.get("/api/v1/track/url", params=params).status_code == 404
        assert calls == {"resolve": 3, "probe": 2}


def test_five_sources_shared_deadline_preserves_partial_and_cleans_children(monkeypatch):
    started, cleaned = set(), set()

    async def quick(client, keyword, limit):
        return [{"id": "lx:wy:quick"}]

    async def error(client, keyword, limit):
        raise RuntimeError("source failed")

    async def slow(src, client, keyword, limit):
        async def child():
            started.add(src)
            try:
                await asyncio.sleep(10)
            finally:
                cleaned.add(src)
        if src == "kg":
            lxapp._publish({"id": "lx:kg:partial"})
        await asyncio.gather(child(), child())
        return []

    from functools import partial
    monkeypatch.setattr(lxapp, "_SEARCHERS", {
        "kg": partial(slow, "kg"), "wy": quick, "mg": error,
        "tx": partial(slow, "tx"), "kw": partial(slow, "kw"),
    })
    # Test-only tx registration: not a new live provider.
    monkeypatch.setattr(lxapp, "THIRD_PARTY_CHAIN", [
        {"name": "test_only", "platforms": {"tx", "kw"}, "fn": None}])
    monkeypatch.setitem(lxapp.CONF, "search_timeout", 0.08)
    with TestClient(lxapp.app) as client:
        start = time.monotonic()
        response = client.get("/api/v1/search", params={"q": "a", "sources": "kg,wy,mg,tx,kw,kg"})
        elapsed = time.monotonic() - start
    data = response.json()
    assert elapsed < 0.5
    assert {item["id"] for item in data["items"]} == {"lx:wy:quick", "lx:kg:partial"}
    assert data["errors"] == {"kg": "search deadline exceeded", "mg": "source failed",
                              "tx": "search deadline exceeded", "kw": "search deadline exceeded"}
    assert started == cleaned == {"kg", "tx", "kw"}


def test_circuit_misses_do_not_open_and_half_open_is_exclusive(monkeypatch):
    calls = Counter()
    state = {"fail": False, "block": False}

    async def chain(client, ctx):
        calls["n"] += 1
        if state["block"]:
            await asyncio.sleep(10)
        if state["fail"]:
            raise httpx.ConnectError("transport down")
        return None  # healthy song miss

    monkeypatch.setattr(lxapp, "THIRD_PARTY_CHAIN", [
        {"name": "local", "platforms": {"kw"}, "fn": chain}])

    async def run():
        client = lxapp.app.state.http
        for _ in range(5):
            assert await lxapp.resolve_third_party(client, "kw", "missing") is None
        assert lxapp._CHAIN_HEALTH["local"]["fails"] == 0
        state["fail"] = True
        for _ in range(5):
            await lxapp.resolve_third_party(client, "kw", "a")
        assert calls["n"] == 8
        assert lxapp.chain_health_snapshot()["local"]["open"]
        lxapp._CHAIN_HEALTH["local"]["open_until"] = time.time() - 1
        state["block"] = True
        first = asyncio.create_task(lxapp.resolve_third_party(client, "kw", "a"))
        while calls["n"] == 8:
            await asyncio.sleep(0)
        await asyncio.gather(*(lxapp.resolve_third_party(client, "kw", "a") for _ in range(5)))
        assert calls["n"] == 9
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        assert not lxapp._CHAIN_HEALTH["local"].get("half_open")
        state.update(block=False, fail=False)
        await lxapp.resolve_third_party(client, "kw", "missing")
        assert lxapp.chain_health_snapshot()["local"]["state"] == "closed"
    asyncio.run(run())


def test_real_mg_search_deadline_cancels_media_children(monkeypatch):
    closed = []

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(10)
            yield b"ID3"

        async def aclose(self):
            closed.append(True)

    def handler(req):
        if req.url.host == "c.music.migu.cn":
            return httpx.Response(200, json={"songs": [
                {"copyrightId": str(i), "songName": "Song"} for i in range(2)]})
        if req.url.host == "music.migu.cn":
            return httpx.Response(200, json={"data": {
                "play_url": "https://media.test/" + req.url.params["copyrightId"]}})
        if req.url.path == "/0":
            return httpx.Response(206, content=b"ID3")
        return httpx.Response(200, stream=SlowStream())

    monkeypatch.setitem(lxapp.CONF, "search_timeout", 0.08)
    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(lxapp.app) as client:
        response = client.get("/api/v1/search", params={"q": "Song", "sources": "mg"})
        data = response.json()
    assert [it["id"] for it in data["items"]] == ["lx:mg:0"]
    assert data["errors"] == {"mg": "search deadline exceeded"}
    assert closed == [True]


@pytest.mark.skipif(not lxapp.HAS_CRYPTO, reason="pycryptodome not installed")
def test_real_wy_eapi_trial_rejected_then_quality_downgrade():
    calls = Counter()

    def handler(req):
        if req.url.host == "interface3.music.163.com":
            from urllib.parse import parse_qs
            encrypted = parse_qs(req.content.decode())["params"][0]
            assert all(c in "0123456789abcdef" for c in encrypted)
            calls["eapi"] += 1
            return httpx.Response(200, json={"data": [{
                "url": "https://media.test/trial" if calls["eapi"] == 1 else "https://media.test/full",
                "freeTrialInfo": {"start": 0, "end": 30} if calls["eapi"] == 1 else None,
                "type": "mp3", "br": 320000,
            }]})
        assert req.url.path == "/full"  # never probe explicit trial URL
        calls["probe"] += 1
        return httpx.Response(206, content=b"ID3")

    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(lxapp.app) as client:
        for _ in range(2):
            response = client.get("/api/v1/track/url", params={"guid": "wy:42", "quality": "lossless"})
            assert response.status_code == 200
            assert response.json()["data"]["actual_tier"] == "high"
            assert response.json()["data"]["completeness"] == "unknown"
    assert calls == {"eapi": 2, "probe": 1}


def test_capability_health_disabled_third_party(monkeypatch):
    with TestClient(lxapp.app) as client:
        health = client.get("/healthz").json()
        assert not health["capabilities"]["tx"]["playback_available"]
        assert health["capabilities"]["tx"]["reason"] == "no_resolver_registered"
        assert set(health["chains"]) == {"suyin_mg", "changqing_kw"}
        monkeypatch.setitem(lxapp.CONF, "third_party", False)
        health = client.get("/healthz").json()
        assert health["capabilities"]["mg"]["playback_available"]
        assert not health["capabilities"]["kw"]["playback_available"]
        assert health["capabilities"]["kw"]["reason"] == "third_party_disabled"
        assert all(chain["state"] == "disabled" for chain in health["chains"].values())


@pytest.mark.parametrize("loop", [False, True])
def test_redirect_bodies_never_read_and_hops_bounded(loop):
    streams = []
    class RedirectBody(httpx.AsyncByteStream):
        reads = 0
        closed = False
        async def __aiter__(self):
            self.reads += 1
            yield b"x" * (65 * 1024 * 1024)
        async def aclose(self):
            self.closed = True
    def handler(req):
        if not loop and req.url.path == "/final":
            return httpx.Response(206, content=b"ID3")
        stream = RedirectBody()
        streams.append(stream)
        return httpx.Response(302, headers={"Location": "/loop" if loop else "/final"}, stream=stream)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            result = await lxapp.probe_url(client, "https://media.test/start")
            assert result[0] is not loop
    asyncio.run(run())
    assert len(streams) == (6 if loop else 1)
    assert all(s.reads == 0 and s.closed for s in streams)


def test_kg_rejected_playinfo_continues_tracker():
    calls = Counter()
    def handler(req):
        calls[req.url.host] += 1
        if req.url.host == "m.kugou.com":
            return httpx.Response(200, json={"errcode": 0, "url": "https://media.test/dead"})
        if "trackercdn" in req.url.host:
            return httpx.Response(200, json={"code": 0, "url": "https://media.test/good", "bitRate": 128})
        if req.url.path == "/dead":
            return httpx.Response(200, json={"error": "gone"})
        return httpx.Response(206, content=b"ID3")
    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(lxapp.app) as client:
        response = client.get("/api/v1/track/url", params={"id": "lx:kg:42", "quality": "standard"})
    assert response.status_code == 200
    assert response.json()["data"]["url"].endswith("/good")
    assert calls["trackercdnbj.kugou.com"] == 1 and calls["media.test"] == 2


@pytest.mark.skipif(not lxapp.HAS_CRYPTO, reason="pycryptodome not installed")
def test_wy_trial_eapi_continues_outer_same_tier():
    calls = Counter()
    def handler(req):
        calls[req.url.host] += 1
        if req.url.host == "interface3.music.163.com":
            return httpx.Response(200, json={"data": [{"url": "https://media.test/trial",
                "freeTrialInfo": {"end": 30}, "br": 128000}]})
        assert "outer/url" in req.url.path
        return httpx.Response(206, content=b"ID3")
    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(lxapp.app) as client:
        response = client.get("/api/v1/track/url", params={"id": "lx:wy:42", "quality": "standard"})
    assert response.status_code == 200
    assert calls == {"interface3.music.163.com": 1, "music.163.com": 1}


def test_silent_official_downgrade_pursues_better_chain_and_caches(monkeypatch):
    calls = Counter()
    async def official(client, identifier, tier):
        calls["official_" + tier] += 1
        return {"url": "https://media.test/standard", "br": 128000}
    async def chain(client, ctx):
        calls["chain_" + ctx["tier"]] += 1
        if ctx["tier"] == "high":
            return {"url": "https://media.test/high", "br": 320000}
        return None
    monkeypatch.setattr(lxapp, "mg_resolve_url", official)
    monkeypatch.setattr(lxapp, "THIRD_PARTY_CHAIN", [{"name": "test", "platforms": {"mg"}, "fn": chain}])
    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(206, content=b"ID3")))
    with TestClient(lxapp.app) as client:
        for _ in range(2):
            response = client.get("/api/v1/track/url", params={"id": "lx:mg:42", "quality": "lossless"})
            assert response.status_code == 200
            assert response.json()["data"]["actual_tier"] == "high"
            assert response.json()["data"]["resolver"] == "test"
    assert calls == {"official_lossless": 1, "chain_lossless": 1, "official_high": 1, "chain_high": 1}


@pytest.mark.parametrize("source", ["kg", "wy", "mg", "kw"])
@pytest.mark.parametrize("failure,expected", [(503, 502), (429, 502), ("connect", 502), (404, 404), ("json", 404)])
def test_resolution_exhaustion_status(source, failure, expected):
    def handler(req):
        if failure == "connect":
            raise httpx.ConnectError("offline fixture", request=req)
        if failure == "json":
            return httpx.Response(200, json={"error": "song missing"})
        return httpx.Response(failure)
    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(lxapp.app) as client:
        response = client.get("/api/v1/track/url", params={"id": f"lx:{source}:42", "quality": "standard"})
    assert response.status_code == expected


def test_failed_upgrade_not_cached_as_exhausted(monkeypatch):
    calls = Counter()
    async def official(client, identifier, tier):
        calls[tier] += 1
        return {"url": "https://media.test/standard", "br": 128000}
    async def chain(client, ctx):
        raise httpx.ConnectError("upgrade temporarily unavailable")
    monkeypatch.setattr(lxapp, "mg_resolve_url", official)
    monkeypatch.setattr(lxapp, "THIRD_PARTY_CHAIN", [{"name": "test", "platforms": {"mg"}, "fn": chain}])
    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(206, content=b"ID3")))
    with TestClient(lxapp.app) as client:
        for _ in range(2):
            response = client.get("/api/v1/track/url", params={"id": "lx:mg:42", "quality": "lossless"})
            assert response.status_code == 200  # keep the valid downgrade
            data = response.json()["data"]
            assert data["actual_tier"] == "standard"
            assert "lossless" not in data["attempted_tiers"]
    assert calls["lossless"] == 2  # retry incomplete upgrade, don't lie about exhaustion


@pytest.mark.parametrize("caller_cancel", [False, True])
def test_upgrade_budget_retains_media_but_caller_cancel_propagates(monkeypatch, caller_cancel):
    calls = Counter()
    started = asyncio.Event()

    async def chain(client, ctx):
        calls["upgrade"] += 1
        started.set()
        try:
            await asyncio.sleep(10)
        finally:
            await asyncio.sleep(0)  # cleanup is awaited, not abandoned
            calls["cleaned"] += 1

    def handler(req):
        if req.url.host == "music.migu.cn":
            return httpx.Response(200, json={"data": {
                "play_url": "https://media.test/standard", "bitRate": 128000}})
        return httpx.Response(206, content=b"ID3")

    monkeypatch.setattr(lxapp, "THIRD_PARTY_CHAIN", [
        {"name": "test", "platforms": {"mg"}, "fn": chain}])
    monkeypatch.setitem(lxapp.CONF, "url_timeout", 0.03)
    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    if caller_cancel:
        async def run():
            item = {"id": "lx:mg:42"}
            task = asyncio.create_task(lxapp.resolve_and_probe(
                lxapp.app.state.http, "mg", item, "lossless", budget=10))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert "_probe" not in item
            assert calls["cleaned"] == 1
        asyncio.run(run())
    else:
        with TestClient(lxapp.app) as client:
            for _ in range(2):
                response = client.get("/api/v1/track/url", params={
                    "id": "lx:mg:42", "quality": "lossless"})
                assert response.status_code == 200
                data = response.json()["data"]
                assert data["actual_tier"] == "standard"
                assert data["completeness"] == "unknown"
                assert data["attempted_tiers"] == []
                assert data["url"] == "https://media.test/standard"
            assert calls == {"upgrade": 2, "cleaned": 2}
    assert not lxapp._CHAIN_HEALTH.get("test", {}).get("half_open")
