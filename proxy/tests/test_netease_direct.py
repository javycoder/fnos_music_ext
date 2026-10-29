import asyncio
import socket
import pytest
import httpx

from proxy.netease_direct import (
    is_allowed_netease_cdn_host,
    is_audio_magic,
    parse_netease_https_candidate,
    upgrade_netease_media_url,
    NeteaseDirectProbeError,
)


def public_dns_resolver(address="93.184.216.34"):
    def resolver(_host, _port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
    return resolver


def private_dns_resolver(address="127.0.0.1"):
    def resolver(_host, _port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
    return resolver


# 1. CDN Host Allowlist Tests
@pytest.mark.parametrize(
    "host,expected",
    [
        ("m7.music.126.net", True),
        ("m8.music.126.net", True),
        ("m701.music.126.net", True),
        ("m801.music.126.net", True),
        ("m10.music.126.net", True),
        ("m7c.music.126.net", False),
        ("M701.MUSIC.126.NET", True),
        ("m801.music.126.net.", True),
        # Disallowed hosts
        ("music.126.net", False),
        ("p1.music.126.net", False),
        ("p2.music.126.net", False),
        ("126.net", False),
        ("evil.126.net", False),
        ("m7.music.126.net.evil.com", False),
        ("sub.m7.music.126.net", False),
        ("example.com", False),
        ("127.0.0.1", False),
        ("", False),
    ],
)
def test_netease_cdn_host_allowlist(host, expected):
    assert is_allowed_netease_cdn_host(host) is expected


def test_extra_allowed_hosts():
    assert is_allowed_netease_cdn_host("cdn.test", extra_allowed_hosts=("cdn.test",)) is True
    assert is_allowed_netease_cdn_host("cdn.test") is False


# 2. Audio Magic Verification Tests
@pytest.mark.parametrize(
    "prefix,expected",
    [
        (b"ID3\x04\x00\x00\x00\x00\x00\x00", True),
        (b"fLaC\x00\x00\x00\x22", True),
        (b"OggS\x00\x02\x00\x00", True),
        (b"RIFF\x24\x00\x00\x00WAVEfmt ", True),
        (b"FORM\x00\x00\x00\x24AIFFCOMM", True),
        (b"\x00\x00\x00\x20ftypM4A ", True),
        (b"MAC \x96\x0f\x00\x00", True),
        (b"\xff\xfb\x90\x44\x00\x00", True),  # MPEG-1 Layer 3
        (b"\xff\xf3\x90\x44\x00\x00", True),  # MPEG-2 Layer 3
        (b"\xff\xf2\x90\x44\x00\x00", True),  # MPEG-2.5 Layer 3
        (b"\xff\xf1\x50\x80\x00\x00", True),  # ADTS AAC
        # Invalid / Error signatures
        (b"<!DOCTYPE html>", False),
        (b"<html><head>", False),
        (b'{"code": 404}', False),
        (b"Error 404", False),
        (b"\x00\x00\x00\x00", False),
        (b"ID", False),
        (b"", False),
    ],
)
def test_audio_magic_verification(prefix, expected):
    assert is_audio_magic(prefix) is expected


# 3. Candidate Parsing & Validation Tests
def test_parse_candidate_existing_https():
    raw = "https://m701.music.126.net/20261028/audio.flac?wsSecret=abc"
    should_probe, url = parse_netease_https_candidate(raw)
    assert should_probe is False
    assert url == raw


def test_parse_candidate_eligible_http():
    raw = "http://m701.music.126.net/20261028/audio.mp3?wsSecret=abc&wsTime=123"
    should_probe, url = parse_netease_https_candidate(raw)
    assert should_probe is True
    assert url == "https://m701.music.126.net/20261028/audio.mp3?wsSecret=abc&wsTime=123"


def test_parse_candidate_eligible_http_with_port_80():
    raw = "http://m801.music.126.net:80/audio.mp3?sig=1"
    should_probe, url = parse_netease_https_candidate(raw)
    assert should_probe is True
    assert url == "https://m801.music.126.net/audio.mp3?sig=1"


@pytest.mark.parametrize(
    "raw",
    [
        "http://m7.music.126.net:8080/audio.mp3",  # non-standard port
        "http://user:pass@m7.music.126.net/audio.mp3",  # userinfo present
        "http://evil.126.net/audio.mp3",  # disallowed subdomain
        "http://126.net/audio.mp3",  # blanket 126.net
        "http://p1.music.126.net/cover.jpg",  # image CDN
        "ftp://m7.music.126.net/audio.mp3",  # invalid scheme
        "http://m7.music.126.net/audio\r\nLocation:evil.mp3",  # control characters
        "http://m7.music.126.net\\@127.0.0.1/a",  # ambiguous url
        "",
    ],
)
def test_parse_candidate_ineligible_preserves_raw(raw):
    should_probe, url = parse_netease_https_candidate(raw)
    assert should_probe is False
    assert url == raw


# 4. End-to-End Upgrade & Probe Tests (Offline Mocking)
@pytest.mark.asyncio
async def test_upgrade_existing_https_skips_probe():
    raw = "https://m701.music.126.net/track.mp3"
    # Even with an empty transport that would fail on request, existing https returns immediately
    res = await upgrade_netease_media_url(raw)
    assert res == raw


@pytest.mark.asyncio
async def test_upgrade_ineligible_returns_original_url():
    raw = "http://unallowed.domain.test/track.mp3"
    res = await upgrade_netease_media_url(raw)
    assert res == raw


@pytest.mark.asyncio
async def test_upgrade_dns_safety_rejection():
    raw = "http://m7.music.126.net/track.mp3"
    with pytest.raises(NeteaseDirectProbeError, match="dns_safety_rejected:resolved_address_not_public"):
        await upgrade_netease_media_url(raw, resolver=private_dns_resolver("127.0.0.1"))


@pytest.mark.asyncio
async def test_upgrade_successful_206_probe():
    raw = "http://m701.music.126.net/song/audio.mp3?wsSecret=test"
    captured_requests = []

    def handler(request: httpx.Request):
        captured_requests.append(request)
        assert "cookie" not in request.headers
        assert "referer" not in request.headers
        assert "mozilla" in request.headers["user-agent"].lower()
        assert request.headers.get("range") == "bytes=0-8191"
        return httpx.Response(
            206,
            headers={
                "Content-Type": "audio/mpeg",
                "Content-Range": "bytes 0-1023/12345678",
                "Content-Length": "1024",
            },
            content=b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 1014,
        )

    transport = httpx.MockTransport(handler)
    res = await upgrade_netease_media_url(
        raw,
        resolver=public_dns_resolver("93.184.216.34"),
        transport=transport,
    )
    assert res == "https://m701.music.126.net/song/audio.mp3?wsSecret=test"
    assert len(captured_requests) == 1
    assert captured_requests[0].url == "https://m701.music.126.net/song/audio.mp3?wsSecret=test"


@pytest.mark.asyncio
async def test_upgrade_successful_200_stream_probe_stops_early():
    raw = "http://m801.music.126.net/song/audio.flac"

    def handler(request: httpx.Request):
        # 10 MB payload, client should read only prefix and stop
        body = b"fLaC\x00\x00\x00\x22" + b"\x01" * (10 * 1024 * 1024)
        return httpx.Response(
            200,
            headers={"Content-Type": "audio/flac", "Content-Length": str(len(body))},
            content=body,
        )

    transport = httpx.MockTransport(handler)
    res = await upgrade_netease_media_url(
        raw,
        resolver=public_dns_resolver(),
        transport=transport,
    )
    assert res == "https://m801.music.126.net/song/audio.flac"


@pytest.mark.asyncio
async def test_upgrade_fails_on_http_404():
    raw = "http://m7.music.126.net/track_404.mp3"
    transport = httpx.MockTransport(lambda _req: httpx.Response(404, text="Not Found"))
    with pytest.raises(NeteaseDirectProbeError, match="http_status_404"):
        await upgrade_netease_media_url(
            raw,
            resolver=public_dns_resolver(),
            transport=transport,
        )


@pytest.mark.asyncio
async def test_upgrade_fails_on_invalid_audio_magic():
    raw = "http://m7.music.126.net/track.mp3"
    transport = httpx.MockTransport(lambda _req: httpx.Response(
        200,
        headers={"Content-Type": "audio/mpeg"},
        content=b"THIS_IS_NOT_AUDIO_MAGIC_BYTES_12345",
    ))
    with pytest.raises(NeteaseDirectProbeError, match="invalid_audio_magic"):
        await upgrade_netease_media_url(
            raw,
            resolver=public_dns_resolver(),
            transport=transport,
        )


@pytest.mark.asyncio
async def test_upgrade_fails_on_non_identity_encoding():
    import gzip
    raw = "http://m7.music.126.net/track.mp3"
    transport = httpx.MockTransport(lambda _req: httpx.Response(
        200,
        headers={"Content-Encoding": "gzip"},
        stream=httpx.ByteStream(gzip.compress(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 100)),
    ))
    with pytest.raises(NeteaseDirectProbeError, match="non_identity_encoding:gzip"):
        await upgrade_netease_media_url(
            raw,
            resolver=public_dns_resolver(),
            transport=transport,
        )


@pytest.mark.asyncio
async def test_upgrade_fails_on_probe_timeout():
    raw = "http://m7.music.126.net/track.mp3"

    async def slow_handler(_req):
        await asyncio.sleep(0.5)
        return httpx.Response(200, content=b"ID3\x04\x00" + b"\x00" * 100)

    transport = httpx.MockTransport(slow_handler)
    with pytest.raises(NeteaseDirectProbeError, match="probe_timeout"):
        await upgrade_netease_media_url(
            raw,
            timeout=0.05,
            resolver=public_dns_resolver(),
            transport=transport,
        )


@pytest.mark.asyncio
async def test_upgrade_redirect_within_allowed_cdn_succeeds():
    raw = "http://m7.music.126.net/track.mp3"

    def handler(request: httpx.Request):
        if request.url.host == "m7.music.126.net":
            return httpx.Response(302, headers={"Location": "https://m801.music.126.net/target.mp3"})
        if request.url.host == "m801.music.126.net":
            return httpx.Response(
                206,
                headers={"Content-Range": "bytes 0-109/5000", "Content-Type": "audio/mpeg"},
                content=b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 100,
            )
        return httpx.Response(400)

    transport = httpx.MockTransport(handler)
    res = await upgrade_netease_media_url(
        raw,
        resolver=public_dns_resolver(),
        transport=transport,
    )
    assert res == "https://m801.music.126.net/target.mp3"


@pytest.mark.asyncio
async def test_upgrade_redirect_downgrade_to_http_rejected():
    raw = "http://m7.music.126.net/track.mp3"
    transport = httpx.MockTransport(
        lambda _req: httpx.Response(302, headers={"Location": "http://m801.music.126.net/target.mp3"})
    )
    with pytest.raises(NeteaseDirectProbeError, match="redirect_non_https:http"):
        await upgrade_netease_media_url(
            raw,
            resolver=public_dns_resolver(),
            transport=transport,
        )


@pytest.mark.asyncio
async def test_upgrade_redirect_to_disallowed_host_rejected():
    raw = "http://m7.music.126.net/track.mp3"
    transport = httpx.MockTransport(
        lambda _req: httpx.Response(302, headers={"Location": "https://evil.example.com/target.mp3"})
    )
    with pytest.raises(NeteaseDirectProbeError, match="redirect_disallowed_host:evil.example.com"):
        await upgrade_netease_media_url(
            raw,
            resolver=public_dns_resolver(),
            transport=transport,
        )


@pytest.mark.asyncio
async def test_upgrade_fails_on_206_non_zero_range_start():
    raw = "http://m701.music.126.net/track.mp3"
    transport = httpx.MockTransport(lambda _req: httpx.Response(
        206,
        headers={"Content-Range": "bytes 1024-2047/5000000"},
        content=b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 100,
    ))
    with pytest.raises(NeteaseDirectProbeError, match="invalid_content_range:bytes 1024-2047/5000000"):
        await upgrade_netease_media_url(
            raw,
            resolver=public_dns_resolver(),
            transport=transport,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", ["text/html; charset=utf-8", "application/json", "application/xml"])
async def test_upgrade_fails_on_disallowed_content_type(content_type):
    raw = "http://m701.music.126.net/track.mp3"
    transport = httpx.MockTransport(lambda _req: httpx.Response(
        200,
        headers={"Content-Type": content_type},
        content=b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 100,
    ))
    with pytest.raises(NeteaseDirectProbeError, match=f"invalid_content_type:{content_type.lower()}"):
        await upgrade_netease_media_url(
            raw,
            resolver=public_dns_resolver(),
            transport=transport,
        )


@pytest.mark.asyncio
async def test_upgrade_redirect_clears_cookies():
    raw = "http://m7.music.126.net/track.mp3"
    hop2_captured_headers = []

    def handler(request: httpx.Request):
        if request.url.host == "m7.music.126.net":
            resp = httpx.Response(302, headers={"Location": "https://m801.music.126.net/target.mp3"})
            # Server attempts to plant sensitive cookie
            resp.headers["Set-Cookie"] = "token=sensitive123; Path=/"
            return resp
        if request.url.host == "m801.music.126.net":
            hop2_captured_headers.append(dict(request.headers))
            return httpx.Response(
                206,
                headers={"Content-Range": "bytes 0-100/5000"},
                content=b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 90,
            )
        return httpx.Response(400)

    transport = httpx.MockTransport(handler)
    res = await upgrade_netease_media_url(
        raw,
        resolver=public_dns_resolver(),
        transport=transport,
    )
    assert res == "https://m801.music.126.net/target.mp3"
    assert len(hop2_captured_headers) == 1
    # Hop 2 MUST NOT have any cookie header
    assert "cookie" not in hop2_captured_headers[0]


@pytest.mark.asyncio
async def test_upgrade_streaming_chunks_bounded_memory():
    raw = "http://m701.music.126.net/track.flac"

    async def chunk_stream():
        # First chunk is valid audio magic but large (16KB)
        yield b"fLaC\x00\x00\x00\x22" + b"\x00" * 16380
        # Subsequent chunks should never be read
        yield b"\x00" * 16384

    def handler(_req: httpx.Request):
        return httpx.Response(
            200,
            headers={"Content-Type": "audio/flac"},
            stream=httpx.ByteStream(b"fLaC\x00\x00\x00\x22" + b"\x00" * 16380),
        )

    transport = httpx.MockTransport(handler)
    res = await upgrade_netease_media_url(
        raw,
        resolver=public_dns_resolver(),
        transport=transport,
    )
    assert res == "https://m701.music.126.net/track.flac"


# 5. Integration with proxy.app
import time
from fastapi import FastAPI, Request
from proxy import app as proxy_app
from proxy.direct_play import DirectTargetBindings, FileTargetBindings
from proxy.media_url import MediaURLDecision
import proxy.netease_direct as netease_direct_mod


def make_request(method="GET", path="/music/api/v1/track/stream", headers=None):
    return Request({
        "type": "http", "method": method, "scheme": "https",
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()],
        "server": ("example.test", 443), "client": ("203.0.113.10", 50000),
        "app": FastAPI(),
    })


def _async_val(value):
    async def f():
        return value
    return f()


@pytest.mark.asyncio
async def test_app_resolve_direct_target_netease_upgrades_http_to_https(monkeypatch):
    song_id = "5257138"
    guid = f"online:netease:{song_id}"
    http_url = "http://m801.music.126.net/20261028/audio.flac?wsSecret=xyz"

    async def mock_resolve(_client, _id):
        return http_url

    monkeypatch.setattr(proxy_app, "resolve_netease_url", mock_resolve)
    monkeypatch.setattr(proxy_app, "_NETEASE_URL_PIN", {song_id: (http_url, time.monotonic() + 300.0)})

    def probe_handler(_req):
        return httpx.Response(
            206,
            headers={"Content-Range": "bytes 0-1023/12764517"},
            content=b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 1014,
        )

    real_upgrade = netease_direct_mod.upgrade_netease_media_url

    async def patched_upgrade(raw_url, **kwargs):
        return await real_upgrade(
            raw_url,
            resolver=public_dns_resolver(),
            transport=httpx.MockTransport(probe_handler),
        )

    monkeypatch.setattr(netease_direct_mod, "upgrade_netease_media_url", patched_upgrade)

    req = make_request()
    target = await proxy_app._resolve_direct_target(req, guid, "netease")
    assert target.provider == "netease"
    assert target.platform == "netease"
    assert target.canonical_id == song_id
    assert target.url == "https://m801.music.126.net/20261028/audio.flac?wsSecret=xyz"
    assert target.protocol == "https"


@pytest.mark.asyncio
async def test_app_resolve_direct_target_netease_fails_on_probe_failure(monkeypatch):
    song_id = "4040404"
    guid = f"online:netease:{song_id}"
    http_url = "http://m701.music.126.net/audio.mp3"

    async def mock_resolve(_client, _id):
        return http_url

    monkeypatch.setattr(proxy_app, "resolve_netease_url", mock_resolve)

    def probe_handler(_req):
        return httpx.Response(404, text="Not Found")

    real_upgrade = netease_direct_mod.upgrade_netease_media_url

    async def patched_upgrade(raw_url, **kwargs):
        return await real_upgrade(
            raw_url,
            resolver=public_dns_resolver(),
            transport=httpx.MockTransport(probe_handler),
        )

    monkeypatch.setattr(netease_direct_mod, "upgrade_netease_media_url", patched_upgrade)

    req = make_request()
    with pytest.raises(NeteaseDirectProbeError):
        await proxy_app._resolve_direct_target(req, guid, "netease")


@pytest.mark.asyncio
async def test_app_direct_redirect_response_netease_explicitly_rejects_on_probe_failure(monkeypatch):
    song_id = "4040404"
    guid = f"online:netease:{song_id}"
    http_url = "http://m701.music.126.net/audio.mp3"

    monkeypatch.setitem(proxy_app.CONF, "netease_direct_play", True)
    monkeypatch.setattr(proxy_app, "_DIRECT_TARGET_BINDINGS", DirectTargetBindings())
    monkeypatch.setattr(proxy_app, "_DIRECT_FILE_BINDINGS", FileTargetBindings())
    monkeypatch.setattr(proxy_app, "_source_enabled", lambda _guid: True)
    async def auth(_req): return True
    monkeypatch.setattr(proxy_app, "_strict_direct_play_auth", auth)
    monkeypatch.setattr(proxy_app, "resolve_netease_url", lambda _c, _id: _async_val(http_url))

    def probe_handler(_req):
        return httpx.Response(404, text="Not Found")

    real_upgrade = netease_direct_mod.upgrade_netease_media_url
    monkeypatch.setattr(
        netease_direct_mod,
        "upgrade_netease_media_url",
        lambda raw, **kw: real_upgrade(
            raw, resolver=public_dns_resolver(), transport=httpx.MockTransport(probe_handler)
        ),
    )

    resp = await proxy_app._direct_redirect_response(make_request(), guid)
    assert resp is not None
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_app_direct_redirect_response_netease_success_and_range_reuse(monkeypatch):
    song_id = "5257138"
    guid = f"online:netease:{song_id}"
    http_url = "http://m801.music.126.net/song.flac"

    monkeypatch.setitem(proxy_app.CONF, "netease_direct_play", True)
    bindings = DirectTargetBindings()
    monkeypatch.setattr(proxy_app, "_DIRECT_TARGET_BINDINGS", bindings)
    monkeypatch.setattr(proxy_app, "_DIRECT_FILE_BINDINGS", FileTargetBindings())
    monkeypatch.setattr(proxy_app, "_source_enabled", lambda _guid: True)
    async def auth(_req): return True
    monkeypatch.setattr(proxy_app, "_strict_direct_play_auth", auth)
    monkeypatch.setattr(proxy_app, "resolve_netease_url", lambda _c, _id: _async_val(http_url))
    monkeypatch.setattr(proxy_app, "_NETEASE_URL_PIN", {song_id: (http_url, time.monotonic() + 300)})
    monkeypatch.setattr(proxy_app, "validate_media_url", lambda url, **_: MediaURLDecision(True, host="m801.music.126.net"))

    def probe_handler(_req):
        return httpx.Response(
            206,
            headers={"Content-Range": "bytes 0-103/5000", "Content-Type": "audio/flac"},
            content=b"fLaC\x00\x00\x00\x22" + b"\x00" * 100,
        )

    real_upgrade = netease_direct_mod.upgrade_netease_media_url
    monkeypatch.setattr(
        netease_direct_mod,
        "upgrade_netease_media_url",
        lambda raw, **kw: real_upgrade(
            raw, resolver=public_dns_resolver(), transport=httpx.MockTransport(probe_handler)
        ),
    )

    # Initial GET
    req_initial = make_request("GET")
    resp_initial = await proxy_app._direct_redirect_response(req_initial, guid)
    assert resp_initial.status_code == 302
    assert resp_initial.headers["Location"] == "https://m801.music.126.net/song.flac"

    # Subsequent Range request reuses the target and gets the same HTTPS URL
    req_range = make_request("GET", headers={"Range": "bytes=65536-66559"})
    resp_range = await proxy_app._direct_redirect_response(req_range, guid)
    assert resp_range.status_code == 302
    assert resp_range.headers["Location"] == "https://m801.music.126.net/song.flac"

