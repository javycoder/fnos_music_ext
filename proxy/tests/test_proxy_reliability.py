"""Adversarial proxy tests: synthetic clients, temporary cache only."""
import asyncio
import importlib
import os
import time

import httpx
import pytest
from starlette.requests import Request

p = importlib.import_module("proxy.app")
fake_official_guid = p.fake_official_guid


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    p._SEARCH_CACHE.clear()
    for key in ("cache_dir", "library_dir", "fav_dir"):
        monkeypatch.setitem(p.CONF, key, str(tmp_path / key))
    monkeypatch.setitem(p.CONF, "music_db", str(tmp_path / "missing.db"))
    for key in ("musicdl_enabled", "netease_enabled", "lx_enabled"):
        monkeypatch.setitem(p.CONF, key, True)
    for attr in ("upstream_client", "musicdl_client", "musicbox_client", "lx_client"):
        monkeypatch.setattr(p.app.state, attr, None, raising=False)
    yield
    p._SEARCH_CACHE.clear()


def request(query="", token="a"):
    return Request({"type": "http", "method": "GET", "path": "/music/api/v1/search/track",
                    "query_string": query.encode(), "headers": [(b"authorization", token.encode())],
                    "app": p.app, "scheme": "http", "server": ("test", 80)})


def song(sid, title="Song", duration=200, **kw):
    return {"id": sid, "source": sid.split(":")[0], "title": title,
            "artist": "Artist", "duration_s": duration, **kw}


def session(req, items):
    entry = {"items": items, "pages": {}, "cursor": 0, "keyword": "Song", "ts": time.time(),
             "credentials": p._credential_scope(req)}
    p._SEARCH_CACHE["test"] = entry
    return entry


@pytest.mark.anyio
async def test_livez_never_constructs_dependency_clients(monkeypatch):
    def forbidden(*args):
        pytest.fail("liveness must not touch dependencies")
    for name in ("get_upstream_client", "get_musicdl_client", "get_musicbox_client", "get_lx_client"):
        monkeypatch.setattr(p, name, forbidden)
    assert await p.ext_livez() == {"ok": True, "service": "fnmusic-ext", "pid": os.getpid()}


@pytest.mark.anyio
async def test_health_concurrent_wall_clock_budget(monkeypatch):
    started = []
    async def stalled(req):
        started.append(time.monotonic())
        await asyncio.sleep(30)
    clients = []
    for getter in ("get_upstream_client", "get_musicdl_client", "get_musicbox_client", "get_lx_client"):
        client = httpx.AsyncClient(transport=httpx.MockTransport(stalled), base_url="http://test")
        clients.append(client)
        monkeypatch.setattr(p, getter, lambda app, c=client: c)
    before = time.monotonic()
    result = await p.ext_healthz(request())
    elapsed = time.monotonic() - before
    assert 2.3 < elapsed < 2.65
    assert len(started) == 4 and max(started) - min(started) < .1
    assert not result["ok"] and result["degraded"]
    assert all(x["error"] == "TimeoutError" for x in result["details"].values())
    for client in clients:
        await client.aclose()


@pytest.mark.anyio
async def test_health_one_source_suffices_but_reports_degradation(monkeypatch):
    for getter, status, payload in [("get_upstream_client", 401, {}), ("get_musicdl_client", 200, {"ok": True}),
                                  ("get_musicbox_client", 200, {"ok": False, "reason": "offline"})]:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req, s=status, d=payload: httpx.Response(s, json=d)), base_url="http://test")
        monkeypatch.setattr(p, getter, lambda app, c=client: c)
    monkeypatch.setitem(p.CONF, "lx_enabled", False)
    result = await p.ext_healthz(request())
    assert result["ok"] and result["degraded"]
    assert result["musicbox"] == "fail" and result["lxmusic"] == "disabled"
    assert result["details"]["musicbox"]["dependency"]["reason"] == "offline"


@pytest.mark.parametrize("initial", [1, 3])
def test_online_window_local_first_layout_and_local_duplicates(initial, monkeypatch):
    """本地优先全局布局：在线条目在 items 上连续分页、不重不漏；本地重复由 merge 过滤。"""
    monkeypatch.setitem(p.CONF, "online_limit", 3)
    raw = [song(f"kuwo:{i}", str(i)) for i in range(initial)] + [song(f"netease:{i}", str(i)) for i in range(5)]
    entry = session(request(), p.deduplicate_online_items(raw))
    items = [x["title"] for x in entry["items"]]
    # local_total=0（上游无本地条目）：翻页走完整个在线段（dedup 后同名条目已合并）
    walked: list[str] = []
    page = 1
    while True:
        window = p._online_window(entry, page, 2, local_total=0)
        if not window:
            break
        walked.extend(x["title"] for x in window)
        page += 1
        assert page < 20
    assert walked == items
    # 本地重复过滤不影响窗口本身（过滤发生在 merge_online_tracks）
    envelope = {"data": {"list": [{"title": walked[0], "artist": "Artist"}], "total": 1}}
    window = p._online_window(entry, 1, 2, local_total=0)
    merged = p.merge_online_tracks(envelope, window, selected=True)
    assert [x["title"] for x in merged["data"]["list"]] == walked[0:2]


def test_online_window_local_pages_have_no_online_items():
    """纯本地页（分页区间未触及在线段）在线切片为空；边界页拼接本地尾部与在线头部。"""
    entry = session(request(), [song(f"kuwo:{i}", str(i)) for i in range(5)])
    # 本地 4 条、size 2：第 1-2 页纯本地，第 3 页起是在线段（无拼接边界，整页在线）
    assert p._online_window(entry, 1, 2, local_total=4) == []
    assert p._online_window(entry, 2, 2, local_total=4) == []
    assert [x["title"] for x in p._online_window(entry, 3, 2, local_total=4)] == ["0", "1"]
    assert [x["title"] for x in p._online_window(entry, 4, 2, local_total=4)] == ["2", "3"]
    # 本地不满一页：首页 = 本地 1 条 + 在线 1 条（窗口只给在线头部 1 条）
    assert [x["title"] for x in p._online_window(entry, 1, 2, local_total=1)] == ["0"]
    assert [x["title"] for x in p._online_window(entry, 2, 2, local_total=1)] == ["1", "2"]


def test_strict_identity_alternatives_and_scope(monkeypatch):
    original = song("kuwo:1")
    variants = [song("netease:1"), song("lx:kg:1", duration=230), song("lx:kg:2", title="Song (Live)"),
                song("lx:kg:3", version="remix"), song("lx:kg:4", duration=0)]
    merged = p.deduplicate_online_items([original] + variants)
    assert len(merged) == 5
    assert merged[0]["_alternatives"] == [variants[0]]
    assert p._search_scope(request("page=1&size=2")) == p._search_scope(request("page=2&size=2"))
    assert p._search_scope(request(token="a")) != p._search_scope(request(token="b"))
    old = p._search_scope(request())
    monkeypatch.setitem(p.CONF, "online_sources", "Other")
    assert p._search_scope(request()) != old
    assert p._search_ttl({"items": [original]}) > p._search_ttl({"items": [original], "partial": True}) > p._search_ttl({"items": []})
    assert p._search_ttl({"items": [], "partial": True}) == p._search_ttl({"items": []})


@pytest.mark.anyio
@pytest.mark.parametrize("fast_result", [None, []])
async def test_empty_or_error_first_completion_still_waits_for_song(monkeypatch, fast_result):
    monkeypatch.setitem(p.CONF, "netease_wait_s", .01)
    monkeypatch.setitem(p.CONF, "late_page_wait_s", .15)
    monkeypatch.setitem(p.CONF, "lx_enabled", False)
    async def fast(*args):
        await asyncio.sleep(.02)
        return fast_result
    async def available(*args):
        await asyncio.sleep(.06)
        return {"items": [song("kuwo:1")]}
    monkeypatch.setattr(p, "fetch_musicbox_search", fast)
    monkeypatch.setattr(p, "fetch_musicdl_search", available)
    p.app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})), base_url="http://test")
    response = await p.search_track(request("q=Song"))
    import json
    assert json.loads(response.body)["data"]["list"][0]["guid"] == fake_official_guid("online:kuwo:1")
    for entry in p._SEARCH_CACHE.values():
        await entry["task"]


class Audio(httpx.AsyncByteStream):
    def __init__(self, fail=False):
        self.fail = fail
        self.closed = False
        self.reads = 0
    async def __aiter__(self):
        self.reads += 1
        yield b"x" * 2048
        self.reads += 1
        if self.fail:
            raise httpx.ReadError("connection lost")
        yield b"y" * 2048
    async def aclose(self):
        self.closed = True


@pytest.mark.anyio
async def test_unknown_length_error_never_finalizes(tmp_path):
    audio = Audio(fail=True)
    response = p.stream_tee_response(httpx.Response(200, stream=audio), "online:kuwo:1", None, pre_info=song("kuwo:1"))
    with pytest.raises(httpx.ReadError):
        async for _ in response.body_iterator:
            pass
    assert audio.closed
    assert not list(tmp_path.rglob("*.part")) and not list(tmp_path.rglob("*.mp3"))


@pytest.mark.anyio
async def test_disconnect_closes_stream_without_detached_downloader(tmp_path):
    audio = Audio()
    response = p.stream_tee_response(httpx.Response(200, stream=audio), "online:kuwo:1", None, pre_info={})
    assert audio.reads == 0
    assert await anext(response.body_iterator) == b"x" * 2048
    assert audio.reads == 1
    await response.body_iterator.aclose()
    assert audio.closed and audio.reads == 1
    assert not list(tmp_path.rglob("*.part")) and not list(tmp_path.rglob("*.mp3"))


@pytest.mark.anyio
async def test_prebyte_fallback_and_no_splicing(monkeypatch):
    req = request("guid=online:kuwo:1")
    items = p.deduplicate_online_items([song("kuwo:1"), song("netease:2")])
    session(req, items)
    attempts = []
    streams = []
    async def opened(req, guid, rang):
        attempts.append(guid)
        if guid == "online:kuwo:1":
            raise httpx.ReadError("before first byte")
        audio = Audio(fail=True)
        streams.append(audio)
        resp = httpx.Response(200, stream=audio)
        chunks = resp.aiter_bytes()
        return resp, None, "mp3", {}, chunks, await anext(chunks)
    monkeypatch.setattr(p, "_open_online_stream", opened)
    async def no_recovery(*args):
        return False
    monkeypatch.setattr(p, "_recover_source", no_recovery)
    response = await p.stream_track(req)
    assert attempts == ["online:kuwo:1", "online:netease:2"]
    assert response.status_code == 307
    assert response.headers["location"].startswith("/")  # never absolute
    assert streams[0].closed  # prevalidation connection is not orphaned
    target = httpx.URL(response.headers["location"])
    assert target.params["guid"] == "online:netease:2"
    assert target.params["_ext_rendition"] == "1"
    # The chosen source is explicit in the client URL after session eviction.
    p._SEARCH_CACHE.clear()
    seek = request(str(target.query, "utf-8"))
    seek.scope["headers"].append((b"range", b"bytes=2048-"))
    response = await p.stream_track(seek)
    assert await anext(response.body_iterator) == b"x" * 2048
    with pytest.raises(httpx.ReadError):
        await anext(response.body_iterator)
    assert attempts == ["online:kuwo:1", "online:netease:2", "online:netease:2"]


@pytest.mark.anyio
async def test_fallback_redirect_survives_relayed_https_client(monkeypatch):
    # A wan client (fn Connect relay / port forward) sends Host=<public domain>
    # with X-Forwarded-Proto: https, but behind the gateway unix socket the
    # ASGI scheme stays http and the Host header carries no port. The fallback
    # 307 must stay relative: an absolute Location would downgrade the client
    # to http://<domain>/... which no listener serves on that path.
    req = Request({"type": "http", "method": "GET", "path": "/music/api/v1/track/stream",
                   "query_string": b"guid=online:kuwo:1",
                   "headers": [(b"authorization", b"a"), (b"host", b"relay.example.com"),
                               (b"x-forwarded-proto", b"https")],
                   "app": p.app, "scheme": "http", "server": ("relay.example.com", 443)})
    items = p.deduplicate_online_items([song("kuwo:1"), song("netease:2")])
    session(req, items)

    async def opened(req, guid, rang):
        if guid == "online:kuwo:1":
            raise httpx.ReadError("before first byte")
        audio = Audio()
        resp = httpx.Response(200, stream=audio)
        chunks = resp.aiter_bytes()
        return resp, None, "mp3", {}, chunks, await anext(chunks)

    monkeypatch.setattr(p, "_open_online_stream", opened)

    async def no_recovery(*args):
        return False

    monkeypatch.setattr(p, "_recover_source", no_recovery)
    response = await p.stream_track(req)
    assert response.status_code == 307
    location = response.headers["location"]
    assert location.startswith("/")
    assert "relay.example.com" not in location
    assert not location.startswith(("http://", "https://"))
    target = httpx.URL(location)
    assert target.path == "/music/api/v1/track/stream"
    assert target.params["guid"] == "online:netease:2"
    assert target.params["_ext_rendition"] == "1"


@pytest.mark.anyio
async def test_disabled_alternative_and_seek_never_cross_sources(monkeypatch):
    req = request("guid=online:kuwo:1")
    session(req, p.deduplicate_online_items([song("kuwo:1"), song("netease:2")]))
    monkeypatch.setitem(p.CONF, "netease_enabled", False)
    attempts = []
    async def failed(req, guid, rang):
        attempts.append(guid)
        return None
    async def no_recovery(*args):
        return False
    monkeypatch.setattr(p, "_open_online_stream", failed)
    monkeypatch.setattr(p, "_recover_source", no_recovery)
    assert (await p.stream_track(req)).status_code == 404
    assert attempts == ["online:kuwo:1"]
    attempts.clear()
    monkeypatch.setitem(p.CONF, "netease_enabled", True)
    req.scope["headers"].append((b"range", b"bytes=100-"))
    # Create a fresh Request to avoid cached headers.
    req = Request(req.scope)
    assert (await p.stream_track(req)).status_code == 404
    assert attempts == ["online:kuwo:1"]


@pytest.mark.anyio
async def test_metadata_recovers_backend_after_restart_once(monkeypatch):
    req = request()
    session(req, [song("kuwo:1")])
    calls = []
    restored = False
    async def fetch(req, guid):
        calls.append("info")
        return song("kuwo:1", album="restored") if restored else None
    async def search(*args):
        nonlocal restored
        calls.append("search")
        restored = True
        return {"items": [song("kuwo:1")]}
    monkeypatch.setattr(p, "_fetch_online_info", fetch)
    monkeypatch.setattr(p, "fetch_musicdl_search", search)
    assert (await p._online_info(req, "online:kuwo:1"))["album"] == "restored"
    assert calls == ["info", "search", "info"]
    restored = False
    assert (await p._online_info(req, "online:kuwo:1"))["title"] == "Song"
    assert calls.count("search") == 1


@pytest.mark.anyio
async def test_startup_stall_is_bounded_and_closes_transport(monkeypatch):
    class Stalled(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            await asyncio.sleep(30)
            yield b"never"
        async def aclose(self):
            self.closed = True
    audio = Stalled()
    p.app.state.musicdl_client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, stream=audio)), base_url="http://test")
    before = time.monotonic()
    response = await p.stream_track(request("guid=online:kuwo:1"))
    assert response.status_code == 404
    assert 3.8 < time.monotonic() - before < 4.5
    assert audio.closed


@pytest.mark.anyio
async def test_clean_unknown_length_eof_finalizes(tmp_path):
    audio = Audio()
    response = p.stream_tee_response(httpx.Response(200, stream=audio, headers={"content-type": "audio/mpeg"}),
                                    "online:kuwo:1", None, pre_info=song("kuwo:1"))
    data = b"".join([chunk async for chunk in response.body_iterator])
    assert len(data) == 4096 and audio.closed
    cached = p.find_cache_file("online:kuwo:1")
    assert cached and open(cached, "rb").read() == data
    assert not list(tmp_path.rglob("*.part"))


def test_repeated_empty_page_recovers_without_replacing_prefix(monkeypatch):
    monkeypatch.setitem(p.CONF, "online_limit", 2)
    entry = session(request(), [])
    assert p._online_window(entry, 1, 2, local_total=0) == []
    entry["items"] = [song("kuwo:1")]
    assert p._online_window(entry, 1, 2, local_total=0)[0]["id"] == "kuwo:1"
    entry["items"] += [song("netease:2", title="Late")]
    assert [item["id"] for item in p._online_window(entry, 1, 2, local_total=0)] == ["kuwo:1", "netease:2"]


@pytest.mark.anyio
async def test_late_priority_cannot_replace_published_first_page(monkeypatch):
    monkeypatch.setitem(p.CONF, "netease_wait_s", .01)
    monkeypatch.setitem(p.CONF, "late_page_wait_s", .15)
    monkeypatch.setitem(p.CONF, "online_limit", 1)
    monkeypatch.setitem(p.CONF, "lx_enabled", False)
    async def slow(*args):
        await asyncio.sleep(.07)
        return [song("netease:1"), song("netease:2", title="Late")]
    async def fast(*args):
        return {"items": [song("kuwo:1")]}
    monkeypatch.setattr(p, "fetch_musicbox_search", slow)
    monkeypatch.setattr(p, "fetch_musicdl_search", fast)
    p.app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})), base_url="http://test")
    import json
    first = json.loads((await p.search_track(request("q=Song&page=1&size=1"))).body)
    second = json.loads((await p.search_track(request("q=Song&page=2&size=1"))).body)
    repeated = json.loads((await p.search_track(request("q=Song&page=1&size=1"))).body)
    assert first["data"]["list"][0]["guid"] == fake_official_guid("online:kuwo:1")
    assert second["data"]["list"][0]["guid"] == fake_official_guid("online:netease:2")
    assert repeated["data"]["list"] == first["data"]["list"]
    assert next(iter(p._SEARCH_CACHE.values()))["items"][0]["_alternatives"][0]["id"] == "netease:1"


@pytest.mark.anyio
@pytest.mark.parametrize("content_range,length,cached", [
    ("bytes 0-4095/999999", "4096", False),
    ("bytes 0-4095/*", "4096", False),
    ("bytes 100-4195/4196", "4096", False),
    ("invalid", "4096", False),
    ("bytes 0-4095/4096", "4096", True),
    ("bytes 0-4095/4096", "", True),
    ("bytes 0-4095/4096", "8192", False),
])
async def test_only_full_resource_206_can_finalize(content_range, length, cached, tmp_path):
    audio = Audio()
    headers = {"content-type": "audio/mpeg", "content-range": content_range}
    if length:
        headers["content-length"] = length
    response = p.stream_tee_response(httpx.Response(206, stream=audio, headers=headers),
                                    "online:kuwo:1", "bytes=0-", pre_info=song("kuwo:1"))
    assert len(b"".join([chunk async for chunk in response.body_iterator])) == 4096
    assert bool(p.find_cache_file("online:kuwo:1")) == cached
    assert not list(tmp_path.rglob("*.part"))


@pytest.mark.anyio
async def test_gzip_audio_rejected_before_first_byte(tmp_path):
    import gzip
    packed = gzip.compress(b"x" * 4096)
    seen = []
    def handler(req):
        seen.append(req.headers.get("accept-encoding"))
        return httpx.Response(200, content=packed, headers={"content-type": "audio/mpeg",
            "content-encoding": "gzip", "content-length": str(len(packed))})
    p.app.state.musicdl_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    response = await p.stream_track(request("guid=online:kuwo:1"))
    assert response.status_code == 404 and seen == ["identity"]
    assert not list(tmp_path.rglob("*.mp3"))
