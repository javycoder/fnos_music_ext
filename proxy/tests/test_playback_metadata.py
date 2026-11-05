"""Metadata must describe the playback representation, including cold requests."""
import asyncio
import json
import time
from collections import OrderedDict

import httpx
import pytest

from proxy import app as pa


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(pa, "_ONLINE_META", OrderedDict())
    monkeypatch.setattr(pa, "_LX_URL_PIN", {})
    monkeypatch.setattr(pa, "_NETEASE_URL_PIN", {})
    monkeypatch.setattr(pa, "_SEARCH_CACHE", {})
    monkeypatch.setattr(pa, "_DIRECT_TARGET_BINDINGS", pa.DirectTargetBindings())
    monkeypatch.setattr(pa, "_retained_track", lambda *a: (None, None))
    monkeypatch.setattr(pa, "find_cache_file", lambda *a, **k: None)
    monkeypatch.setattr(pa, "read_lyric_cache", lambda *a: None)
    monkeypatch.setitem(pa.CONF, "lx_enabled", True)
    monkeypatch.setitem(pa.CONF, "netease_enabled", True)
    monkeypatch.setitem(pa.CONF, "play_quality", "lossless")
    monkeypatch.setitem(pa.CONF, "cache_dir", str(tmp_path))

    async def no_local(*a):
        return None

    async def no_favorites(*a):
        return set()

    monkeypatch.setattr(pa, "resolve_local_play", no_local)
    monkeypatch.setattr(pa, "_online_favorite_set", no_favorites)


def request(guid="online:lx:kw:format"):
    return pa.Request({"type": "http", "method": "GET", "app": pa.app,
                       "path": "/music/api/v1/track/metadata", "headers": [],
                       "query_string": f"guid={guid}".encode()})


def test_registry_keeps_media_fields_and_re_registration_does_not_erase_them():
    guid = "online:lx:kw:format"
    pa.register_online_meta([{"guid": guid, "title": "Track", "artist": "Artist",
                             "duration_s": 10, "ext": "flac", "file_size": 1234,
                             "audio_spec": {"sampleRate": 96000}}])
    pa.register_online_meta_one(guid, "Track", "Artist", duration_s=10)
    meta = pa._online_meta_for_request(request(), guid)
    assert meta["ext"] == "flac"
    assert meta["file_size"] == 1234
    assert meta["audio_spec"] == {"sampleRate": 96000}


@pytest.mark.anyio
@pytest.mark.parametrize("catalog,actual", [("mp3", "flac"), ("flac", "mp3")])
async def test_duration_fast_path_uses_shared_playback_resolution(monkeypatch, catalog, actual):
    guid = "online:lx:kw:format"
    pa.register_online_meta([{"guid": guid, "title": "Track", "duration_s": 60,
                             "ext": catalog, "audio_spec": {"sampleRate": 192000}}])
    calls = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def resolve(req):
        calls.append(dict(req.url.params))
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"ok": True, "data": {
            "url": "https://media.test/track", "ext": actual, "file_size": 7654}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(resolve),
                                 base_url="http://lx.test") as client:
        monkeypatch.setattr(pa, "get_lx_client", lambda app: client)
        meta_task = asyncio.create_task(pa.track_metadata(request()))
        await entered.wait()
        stream_task = asyncio.create_task(pa.resolve_lx_url_shared(client, "lx:kw:format"))
        await asyncio.sleep(0)
        release.set()
        response, stream = await asyncio.gather(meta_task, stream_task)
        payload = json.loads(response.body)["data"]
        assert stream["ext"] == actual
        assert len(calls) == 1
        for obj in (payload, payload["track"]):
            assert obj["format"] == actual
            spec = obj["audioSpec"]
            assert spec["path"].endswith("." + actual)
            assert spec["format"] == spec["codec"] == spec["container"] == actual
            assert not any(k in spec for k in ("sampleRate", "bitDepth", "bitrate"))


def test_download_representation_does_not_overwrite_playback_spec():
    song = "lx:kw:format"
    play = pa._lx_url_cache_key(song, "lossless", "playback", None, None)
    download = pa._lx_url_cache_key(song, "standard", "download", 4, None)
    pa._lx_url_pin_store(play, {"url": "https://media.test/play", "ext": "flac"})
    pa._lx_url_pin_store(download, {"url": "https://media.test/download", "ext": "mp3"})
    assert play != download
    assert pa._LX_URL_PIN[play][0]["ext"] == "flac"


@pytest.mark.anyio
async def test_cached_file_metadata_skips_online_resolution(monkeypatch, tmp_path):
    import wave
    path = tmp_path / "audio.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0" * 16000)
    monkeypatch.setattr(pa, "find_cache_file", lambda *a, **k: str(path))
    monkeypatch.setattr(pa, "get_lx_client", lambda *a: pytest.fail("cached file must skip LX"))
    response = await pa.track_metadata(request())
    spec = json.loads(response.body)["data"]["audioSpec"]
    assert spec["format"] == "wav"
    assert spec["sampleRate"] == 8000
    assert spec["size"] == path.stat().st_size


@pytest.mark.anyio
@pytest.mark.parametrize("force_mp3", [False, True])
async def test_stream_prefix_corrects_format_without_polluting_download(monkeypatch, force_mp3):
    # The same endpoint can report a stale ext; bytes at offset zero win.
    song = "lx:kw:format"
    capped = "high" if force_mp3 else None
    play = pa._lx_url_cache_key(song, pa._lx_target_quality(capped), "playback", None, capped)
    pa._LX_URL_PIN[play] = ({"url": "https://media.test/test", "ext": "mp3"}, time.monotonic()+60)
    download = pa._lx_url_cache_key(song, "standard", "download", 4, None)
    pa._LX_URL_PIN[download] = ({"url": "https://media.test/dl", "ext": "mp3"}, time.monotonic()+60)
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda req: httpx.Response(
        200, content=b"fLaC"+b"\0"*100, headers={"Content-Type": "audio/mpeg"}))
    monkeypatch.setattr(pa.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    async def selected(*a, **k):
        return pa._LX_URL_PIN[play][0]
    async def info(*a, **k):
        return {"title": "Track"}
    monkeypatch.setattr(pa, "resolve_lx_url_shared", selected)
    monkeypatch.setattr(pa, "_fetch_online_info", info)
    monkeypatch.setattr(pa, "get_lx_client", lambda *a: None)
    opened = await pa._open_online_stream(request(), "online:lx:kw:format", None, force_mp3=force_mp3)
    assert opened[2] == "flac"
    await opened[0].aclose()
    await opened[1].aclose()
    assert pa._LX_URL_PIN[play][0]["ext"] == "flac"
    assert pa._LX_URL_PIN[download][0]["ext"] == "mp3"


@pytest.mark.anyio
async def test_metadata_uses_existing_direct_representation_without_resolving(monkeypatch):
    guid = "online:lx:kw:format"
    pa.register_online_meta([{"guid": guid, "title": "Track", "duration_s": 60, "ext": "mp3"}])
    generation = pa._DIRECT_PLAY_GENERATIONS["lx"]
    target = pa.DirectMediaTarget(
        provider="lx", platform="kw", canonical_id="lx:kw:format",
        url="https://media.test/direct", ext="flac", file_size=1234,
        reuse_deadline=time.monotonic()+60, source_generation=generation)
    pa._DIRECT_TARGET_BINDINGS.put(pa._direct_scope(request()), guid, target, generation, 60)
    monkeypatch.setitem(pa.CONF, pa._DIRECT_PLAY_CONF_KEYS["lx"], True)
    monkeypatch.setattr(pa, "get_lx_client", lambda *a: pytest.fail("bound target must skip resolution"))
    payload = json.loads((await pa.track_metadata(request())).body)["data"]
    assert payload["format"] == "flac"
    assert payload["audioSpec"]["size"] == 1234


@pytest.mark.anyio
async def test_netease_metadata_uses_selected_media_instead_of_catalog(monkeypatch):
    guid = "online:netease:123"
    pa.register_online_meta([{"guid": guid, "title": "Track", "duration_s": 60, "ext": "flac",
                             "audio_spec": {"sampleRate": 96000}}])
    calls = []
    async def selected(client, song_id, **kwargs):
        calls.append(song_id)
        pa._NETEASE_URL_PIN[song_id] = (
            "https://media.test/audio", time.monotonic()+60, {"ext": "mp3"})
        return "https://media.test/audio"
    monkeypatch.setattr(pa, "resolve_netease_url", selected)
    monkeypatch.setattr(pa, "get_musicbox_client", lambda *a: None)
    payload = json.loads((await pa.track_metadata(request(guid))).body)["data"]
    assert calls == ["123"]
    assert payload["format"] == payload["audioSpec"]["format"] == "mp3"
    assert "sampleRate" not in payload["audioSpec"]
