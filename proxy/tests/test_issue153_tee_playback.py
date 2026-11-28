import asyncio
import errno
import os
import shutil
import subprocess
import threading
from pathlib import Path

import httpx
import pytest
from mutagen.id3 import ID3
from starlette.requests import Request

from proxy import app as proxy


GUID = "online:kuwo:issue153"
INFO = {"title": "Test track", "artist": "Test artist", "album": "Test album",
        "cover_url": "https://cover.invalid/art.jpg"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def audio():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    return subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", "-f", "mp3", "pipe:1"],
        check=True, capture_output=True,
    ).stdout


@pytest.fixture(scope="module")
def cover():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    return subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=size=320x320", "-frames:v", "1", "-c:v", "mjpeg", "-f", "image2pipe", "pipe:1"],
        check=True, capture_output=True,
    ).stdout


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    for key, value in {
        "library_dir": str(library), "tee_save_dir": str(library),
        "cache_dir": str(tmp_path / "cache"), "music_db": str(tmp_path / "missing.db"),
        "tee_save_enabled": True, "tee_save_by_user": False, "tee_save_mode": "direct",
        "download_layout": "flat", "auto_cover": True, "lyric_auto_dl": False,
        "library_scan_path": "", proxy._DIRECT_PLAY_CONF_KEYS["musicdl"]: False,
    }.items():
        monkeypatch.setitem(proxy.CONF, key, value)
    monkeypatch.setattr(proxy, "_TEE_STAGED_CACHE", {})
    monkeypatch.setattr(proxy, "_dispatch_official_binding", lambda *args: None)
    monkeypatch.setattr(proxy, "download_cover_bytes", lambda url: pytest.fail("unexpected cover fetch"))
    return library


async def deliver(response, messages, *, spec="2.4", method="GET"):
    async def receive():
        await asyncio.Future()

    async def send(message):
        messages.append(message)

    await response({"type": "http", "method": method, "asgi": {"spec_version": spec}}, receive, send)


def request(range_header=None, *, method="GET", cookie="user=a"):
    headers = [(b"cookie", cookie.encode()), (b"user-agent", b"test-player")]
    if range_header is not None:
        headers.append((b"range", range_header.encode()))
    return Request({
        "type": "http", "method": method, "app": proxy.app,
        "path": "/music/api/v1/track/stream", "query_string": b"guid=online%3Akuwo%3Aissue153",
        "client": ("127.0.0.1", 4321), "headers": headers,
    })


def payload(messages):
    return b"".join(message.get("body", b"") for message in messages)


@pytest.mark.anyio
@pytest.mark.parametrize("spec,status", [("2.0", 200), ("2.4", 206)])
async def test_response_ends_before_waiting_for_cover(audio, cover, monkeypatch, spec, status):
    cover_started = asyncio.Event()
    release_cover = threading.Event()
    loop = asyncio.get_running_loop()
    fetches = []

    def fetch(url):
        fetches.append(url)
        loop.call_soon_threadsafe(cover_started.set)
        assert release_cover.wait(5), "test did not release cover fetch"
        return cover, "image/jpeg"

    monkeypatch.setattr(proxy, "download_cover_bytes", fetch)
    headers = {"content-type": "audio/mpeg"}
    if status == 206:
        headers["content-range"] = f"bytes 0-{len(audio) - 1}/{len(audio)}"
    response = proxy.stream_tee_response(
        httpx.Response(status, content=audio, headers=headers), GUID, None,
        resolved_ext="mp3", pre_info=INFO,
    )
    messages = []
    delivery = asyncio.create_task(deliver(response, messages, spec=spec))
    try:
        await asyncio.wait_for(cover_started.wait(), 4)
        assert payload(messages) == audio
        assert messages[-1]["more_body"] is False, "response still waiting for cover download"
    finally:
        release_cover.set()
        await asyncio.wait_for(delivery, 5)
    assert fetches == [INFO["cover_url"]]
    assert proxy.find_cache_file(GUID)


@pytest.mark.anyio
async def test_range_keeps_original_bytes_after_library_publish(audio, cover, tmp_path, monkeypatch):
    async def no_local(*args):
        return None

    async def upstream(request, guid, range_header, **kwargs):
        headers = {"content-type": "audio/mpeg", "etag": '"original"'}
        body = audio
        if range_header:
            start, end = proxy.parse_http_range(range_header, len(audio))
            headers["content-range"] = f"bytes {start}-{end}/{len(audio)}"
            body = audio[start:end + 1]
        response = httpx.Response(206 if range_header else 200, content=body, headers=headers)
        return response, None, "mp3", INFO, None, b""

    monkeypatch.setattr(proxy, "resolve_local_play", no_local)
    monkeypatch.setattr(proxy, "_open_online_stream", upstream)
    monkeypatch.setattr(proxy, "_source_enabled", lambda guid: True)
    monkeypatch.setattr(proxy, "_register_full_fetch", lambda *args: None)
    monkeypatch.setattr(proxy, "download_cover_bytes", lambda url: (cover, "image/jpeg"))
    initial = await proxy.stream_track(request("bytes=0-999"))
    messages = []
    await deliver(initial, messages)
    assert payload(messages) == audio[:1000]

    part = tmp_path / "original.part"
    part.write_bytes(audio)
    dest, reused = proxy._install_library_audio(str(part), GUID, "mp3",
                                                INFO["title"], INFO["artist"], INFO["album"], INFO)
    assert not reused
    library_bytes = Path(dest).read_bytes()
    assert library_bytes != audio
    for interval in ("bytes=0-999", "bytes=1500-1699", "bytes=-200"):
        start, end = proxy.parse_http_range(interval, len(audio))
        ranged = await proxy.stream_track(request(interval))
        assert ranged.status_code == 206
        assert ranged.headers["content-range"] == f"bytes {start}-{end}/{len(audio)}"
        messages = []
        await deliver(ranged, messages)
        assert payload(messages) == audio[start:end + 1]

    head = await proxy.stream_track(request(method="HEAD"))
    assert head.status_code == 200 and head.headers["content-length"] == str(len(audio))
    assert head.body == b""
    other_client = await proxy.stream_track(request("bytes=0-999", cookie="user=b"))
    assert other_client.headers["content-range"] == f"bytes 0-999/{len(library_bytes)}"
    restart = await proxy.stream_track(request())
    assert restart.headers["content-length"] == str(len(library_bytes))
    assert (await proxy.stream_track(request("bytes=0-999"))).headers["content-range"] == (
        f"bytes 0-999/{len(library_bytes)}"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("status,content_range,etag", [
    (200, None, '"original"'),
    (206, "bytes 101-200/4096", '"original"'),
    (206, "bytes 100-200/8192", '"original"'),
    (206, "bytes 100-200/4096", '"changed"'),
    (206, "bytes 100-200/4096", 'W/"original"'),
    (206, "bytes 100-200/4096", None),
])
async def test_pinned_range_rejects_incompatible_upstream(monkeypatch, status, content_range, etag):
    replies = []

    async def no_local(*args):
        return None

    async def upstream(*args, **kwargs):
        class Audio(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"A" * 101

        if not replies:
            response = httpx.Response(206, content=b"A" * 101, headers={
                "content-range": "bytes 0-100/4096", "etag": '"original"',
            })
        else:
            headers = {}
            if content_range:
                headers["content-range"] = content_range
            if etag:
                headers["etag"] = etag
            response = httpx.Response(status, stream=Audio(), headers=headers)
        replies.append(response)
        return response, None, "mp3", INFO, None, b""

    monkeypatch.setattr(proxy, "resolve_local_play", no_local)
    monkeypatch.setattr(proxy, "_open_online_stream", upstream)
    monkeypatch.setattr(proxy, "_source_enabled", lambda guid: True)
    monkeypatch.setattr(proxy, "_register_full_fetch", lambda *args: None)
    await deliver(await proxy.stream_track(request("bytes=0-100")), [])
    continuation = await proxy.stream_track(request("bytes=100-200"))
    assert continuation.status_code == 409
    assert b"range_identity_unavailable" in continuation.body
    assert replies[-1].is_closed


def test_pinned_bounded_range_rejects_truncated_unknown_total():
    first = httpx.Response(206, headers={"content-range": "bytes 0-100/*"})
    playback = proxy._online_playback_identity(first)
    truncated = httpx.Response(206, headers={"content-range": "bytes 100-150/*"})
    complete = httpx.Response(206, headers={"content-range": "bytes 100-200/*"})
    final = httpx.Response(206, headers={"content-range": "bytes 100-150/151"})

    assert not proxy._matches_online_playback(truncated, "bytes=100-200", playback)
    assert proxy._matches_online_playback(complete, "bytes=100-200", playback)
    assert proxy._matches_online_playback(final, "bytes=100-200", playback)


def test_direct_does_not_publish_before_cover_embedding(audio, cover, isolated, tmp_path, monkeypatch):
    part = tmp_path / "original.part"
    part.write_bytes(audio)
    part.chmod(0o640)
    visible = []

    def fetch(url):
        visible.extend(isolated.rglob("*.mp3"))
        return cover, "image/jpeg"

    monkeypatch.setattr(proxy, "download_cover_bytes", fetch)
    dest, reused = proxy._install_library_audio(str(part), GUID, "mp3",
                                                INFO["title"], INFO["artist"], INFO["album"], INFO)
    assert not visible, "official filename exposed before embedding cover"
    assert not reused and not part.exists()
    assert os.stat(dest).st_mode & 0o777 == 0o640
    tags = ID3(dest)
    assert tags.getall("APIC")[0].data == cover
    assert tags["TIT2"].text == [INFO["title"]]
    subprocess.run(["ffmpeg", "-v", "error", "-i", dest, "-f", "null", "-"],
                   check=True, capture_output=True)
    assert not list(isolated.rglob("*.part"))


@pytest.mark.parametrize("link_error,expected", [
    (None, "reused"),
    (errno.EOPNOTSUPP, "copied"),
    (errno.EACCES, "failed"),
])
def test_direct_publication_does_not_overwrite_racing_file(audio, cover, isolated, tmp_path, monkeypatch,
                                                          link_error, expected):
    part = tmp_path / "original.part"
    part.write_bytes(audio)
    part.chmod(0o640)
    link_attempts = []
    original_link = os.link
    user_bytes = b"audio owned by another task"

    def link(work, dest):
        link_attempts.append(dest)
        assert ID3(work).getall("APIC")[0].data == cover
        if link_error is not None:
            raise OSError(link_error, "mocked link error")
        Path(dest).write_bytes(user_bytes)
        original_link(work, dest)

    monkeypatch.setattr(proxy.os, "link", link)
    monkeypatch.setattr(proxy, "download_cover_bytes", lambda url: (cover, "image/jpeg"))

    def install():
        return proxy._install_library_audio(str(part), GUID, "mp3",
                                            INFO["title"], INFO["artist"], INFO["album"], INFO)

    if expected == "failed":
        with pytest.raises(PermissionError):
            install()
        assert part.read_bytes() == audio
        assert not list(isolated.rglob("*.mp3"))
    else:
        dest, reused = install()
        assert not part.exists()
        if expected == "reused":
            assert reused and Path(dest).read_bytes() == user_bytes
        else:
            assert not reused and ID3(dest).getall("APIC")[0].data == cover
            assert os.stat(dest).st_mode & 0o777 == 0o640
    assert len(link_attempts) == 1
    assert not list(isolated.rglob("*.part"))
