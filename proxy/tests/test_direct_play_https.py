import io
import math
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from proxy import app as proxy_app
from proxy.direct_play import DirectMediaTarget, DirectTargetBindings
from proxy.media_url import MediaURLDecision


@pytest.fixture
def synthetic_cdns(tmp_path, monkeypatch):
    if not shutil.which("openssl"):
        pytest.skip("openssl required for the isolated HTTPS fixture")
    certificate = tmp_path / "certificate.pem"
    private_key = tmp_path / "private-key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(private_key), "-out", str(certificate), "-days", "1",
        "-subj", "/CN=cdn-a.test",
        "-addext", "subjectAltName=DNS:cdn-a.test,DNS:cdn-b.test",
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    audio_buffer = io.BytesIO()
    with wave.open(audio_buffer, "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"".join(struct.pack("<h", int(5000 * math.sin(index * 0.3)))
                                   for index in range(8000)))
    audio_bytes = audio_buffer.getvalue()
    alternative_buffer = io.BytesIO()
    with wave.open(alternative_buffer, "wb") as alternative:
        alternative.setparams((1, 1, 8000, 0, "NONE", "not compressed"))
        alternative.writeframes(bytes(int(128 + 40 * math.sin(index * 0.3))
                                      for index in range(8000)))
    alternative_bytes = alternative_buffer.getvalue()
    traffic = []

    class MediaHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_HEAD(self):
            self.deliver()

        def do_GET(self):
            self.deliver()

        def deliver(self):
            if (self.path == "/expired.wav"
                    or (self.path == "/requires-headers.wav" and (
                        self.headers.get("User-Agent") != "NAS-only"
                        or self.headers.get("Referer") != "https://private.test"))):
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                traffic.append((self.command, self.path, 0))
                return
            if self.path == "/redirect.wav":
                self.send_response(302)
                self.send_header("Location", f"https://cdn-b.test:{second.server_port}/audio.wav")
                self.send_header("Content-Length", "0")
                self.end_headers()
                traffic.append((self.command, self.path, 0))
                return
            if self.path in ("/chain.wav", "/downgrade.wav"):
                self.send_response(302)
                destination = (f"https://cdn-a.test:{first.server_port}/redirect.wav"
                               if self.path == "/chain.wav" else
                               "http://cdn-b.test/audio.wav")
                self.send_header("Location", destination)
                self.send_header("Content-Length", "0")
                self.end_headers()
                traffic.append((self.command, self.path, 0))
                return
            if self.path == "/slow.wav":
                time.sleep(0.05)
            selected_audio = alternative_bytes if self.path == "/alternative.wav" else audio_bytes
            range_value = None if self.path == "/ignore-range.wav" else self.headers.get("Range")
            offset = int(range_value.split("=", 1)[1].split("-", 1)[0]) if range_value else 0
            body = selected_audio[offset:]
            self.send_response(206 if range_value else 200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(body)))
            if self.path != "/no-cors.wav":
                self.send_header("Access-Control-Allow-Origin", "*")
            if range_value:
                self.send_header("Content-Range", f"bytes {offset}-{len(selected_audio) - 1}/{len(selected_audio)}")
            self.end_headers()
            if self.command == "GET":
                if self.path == "/disconnect.wav":
                    body = body[:100]
                    self.close_connection = True
                self.wfile.write(body)
            traffic.append((self.command, self.path, len(body) if self.command == "GET" else 0))

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, private_key)
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), MediaHandler) for _ in range(2)]
    first, second = servers
    threads = []
    for server in servers:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)
    real_getaddrinfo = socket.getaddrinfo

    def local_dns(host, *args, **kwargs):
        if host in ("cdn-a.test", "cdn-b.test", b"cdn-a.test", b"cdn-b.test"):
            host = "127.0.0.1"
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", local_dns)
    try:
        yield {
            "origins": [f"https://cdn-a.test:{first.server_port}", f"https://cdn-b.test:{second.server_port}"],
            "verify": ssl.create_default_context(cafile=str(certificate)),
            "audio": audio_bytes, "alternative": alternative_bytes, "traffic": traffic,
        }
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin_index", [0, 1])
async def test_real_https_audio_and_seek_without_nas_audio_body(monkeypatch, synthetic_cdns, origin_index):
    fixture = synthetic_cdns
    monkeypatch.setattr(proxy_app, "_DIRECT_TARGET_BINDINGS", DirectTargetBindings())
    monkeypatch.setattr(proxy_app, "_full_fetch_tasks", {})
    monkeypatch.setattr(proxy_app, "_source_enabled", lambda _guid: True)
    monkeypatch.setitem(proxy_app.CONF, "lx_direct_play", True)
    monkeypatch.setitem(proxy_app.CONF, "tee_save_enabled", False)
    monkeypatch.setattr(proxy_app, "find_cache_file", lambda *_args, **_kwargs: None)

    async def no_local(*_args):
        return None

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("direct playback must not open a NAS audio stream")

    async def resolve(*_args):
        return DirectMediaTarget(
            provider="lx", platform="kg", canonical_id="lx:kg:fixture",
            url=fixture["origins"][origin_index] + "/audio.wav", ext="wav",
            reuse_deadline=time.monotonic() + 60,
            source_generation=proxy_app._DIRECT_PLAY_GENERATIONS["lx"],
            server_headers={"User-Agent": "NAS-only", "Referer": "https://private.test"},
            representation_key="synthetic-wav",
        )

    monkeypatch.setattr(proxy_app, "resolve_local_play", no_local)
    monkeypatch.setattr(proxy_app, "_open_online_stream", forbidden)
    monkeypatch.setattr(proxy_app, "_resolve_direct_target", resolve)
    monkeypatch.setattr(proxy_app, "validate_media_url", lambda url: MediaURLDecision(
        url.startswith(tuple(fixture["origins"])), host="isolated-fixture"))
    official = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(
            200, json={"code": 0, "data": {"guid": "fixture-user"}})),
        base_url="http://official.test")
    monkeypatch.setattr(proxy_app, "get_upstream_client", lambda _app: official)
    async with official, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy_app.app), base_url="http://nas.test",
        headers={"Authorization": "Bearer fixture"},
    ) as nas, httpx.AsyncClient(verify=fixture["verify"], trust_env=False, follow_redirects=True) as cdn:
        path = "/music/api/v1/track/stream/online:lx:kg:fixture"
        head = await nas.head(path)
        assert head.status_code == 302 and head.content == b""
        assert (await cdn.head(head.headers["location"])).content == b""
        initial = await nas.get(path)
        assert initial.status_code == 302 and initial.content == b""
        assert initial.headers["location"] == head.headers["location"]
        assert "referer" not in initial.headers and "user-agent" not in initial.headers
        audio = await cdn.get(initial.headers["location"])
        assert audio.content == fixture["audio"]
        with wave.open(io.BytesIO(audio.content), "rb") as decoded:
            assert len(decoded.readframes(decoded.getnframes())) == 16000
        continuation = await nas.get(path, headers={"Range": "bytes=2048-"})
        assert continuation.status_code == 302
        seek = await cdn.get(continuation.headers["location"], headers={"Range": "bytes=2048-"})
        assert seek.status_code == 206 and seek.content == fixture["audio"][2048:]
        redirected = await cdn.get(fixture["origins"][0] + "/redirect.wav")
        assert redirected.content == fixture["audio"] and len(redirected.history) == 1
        assert (await cdn.get(fixture["origins"][0] + "/expired.wav")).status_code == 403
    assert proxy_app._full_fetch_tasks == {}
    assert sum(size for _, _, size in fixture["traffic"]) == 3 * len(fixture["audio"]) - 2048


@pytest.mark.asyncio
async def test_controlled_cdn_compatibility_failures_remain_visible(synthetic_cdns):
    fixture = synthetic_cdns
    origin = fixture["origins"][0]
    async with httpx.AsyncClient(verify=fixture["verify"], trust_env=False) as client:
        restricted = await client.get(origin + "/requires-headers.wav")
        assert restricted.status_code == 403
        authorized = await client.get(origin + "/requires-headers.wav", headers={
            "User-Agent": "NAS-only", "Referer": "https://private.test",
        })
        assert authorized.content == fixture["audio"]
        ignored = await client.get(origin + "/ignore-range.wav", headers={"Range": "bytes=2048-"})
        assert ignored.status_code == 200 and ignored.content == fixture["audio"]
        assert "content-range" not in ignored.headers
        no_cors = await client.get(origin + "/no-cors.wav")
        assert no_cors.content == fixture["audio"]
        assert "access-control-allow-origin" not in no_cors.headers
        chain = await client.get(origin + "/chain.wav", follow_redirects=True)
        assert chain.content == fixture["audio"] and len(chain.history) == 2
        downgrade = await client.get(origin + "/downgrade.wav")
        assert downgrade.status_code == 302
        assert downgrade.headers["location"].startswith("http://")
        assert not proxy_app.validate_media_url(downgrade.headers["location"]).ok
        started = time.monotonic()
        slow = await client.get(origin + "/slow.wav")
        assert time.monotonic() - started >= 0.05
        assert slow.content == fixture["audio"]
        with pytest.raises(httpx.RemoteProtocolError):
            await client.get(origin + "/disconnect.wav")
        alternative = await client.get(origin + "/alternative.wav")
        assert alternative.content == fixture["alternative"] != fixture["audio"]
        with wave.open(io.BytesIO(alternative.content), "rb") as decoded:
            assert decoded.getsampwidth() == 1 and decoded.getnframes() == 8000
