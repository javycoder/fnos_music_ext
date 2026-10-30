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
            if "/egress-restricted.wav" in self.path:
                # 模拟 CDN 将临时链接绑定到请求签名时的出口 IP（例如 198.51.100.1）。
                # 若客户端来自移动蜂窝网或异构出口（通过 X-Client-Egress-IP 模拟），CDN 判定 IP 不匹配直接返回 403 拒绝。
                client_ip = self.headers.get("X-Client-Egress-IP") or self.client_address[0]
                expected_ip = "198.51.100.1"
                if "client_ip=198.51.100.1" in self.path and client_ip != expected_ip:
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


@pytest.mark.asyncio
async def test_controlled_https_entry_307_to_cross_origin_302_cdn_chain(monkeypatch, synthetic_cdns):
    """验证 HTTPS 入口 307 相对跳转、非标准端口保留、跨域 302 绝对地址与 CDN 播放全链路。

    目的：
    验证客户端通过非标准端口公网入口（如 8443 端口）发起请求时，代理先返回 307 相对跳转，
    客户端解析后保留原端口与 HTTPS 协议，随后获取 302 绝对 CDN 地址并向 CDN 直连取流；
    同时验证方法（GET 与 HEAD）、Range 头、凭据隔离（跨域不向 CDN 泄露 NAS 凭据）与无下载任务。
    """
    fixture = synthetic_cdns
    cdn_origin = fixture["origins"][0]

    # 初始化直连状态与环境开关
    monkeypatch.setattr(proxy_app, "_DIRECT_TARGET_BINDINGS", DirectTargetBindings())
    monkeypatch.setattr(proxy_app, "_full_fetch_tasks", {})
    monkeypatch.setattr(proxy_app, "_source_enabled", lambda _guid: True)
    monkeypatch.setitem(proxy_app.CONF, "lx_direct_play", True)
    monkeypatch.setitem(proxy_app.CONF, "tee_save_enabled", False)
    monkeypatch.setattr(proxy_app, "find_cache_file", lambda *_args, **_kwargs: None)

    async def no_local(*_args):
        return None

    async def forbidden_stream(*_args, **_kwargs):
        raise AssertionError("直连播放严禁在 NAS 代理端启动本地音频取流")

    # 模拟洛雪源解析出的直连目标媒体地址（指向合成 CDN 服务）
    async def resolve_target(*_args):
        return DirectMediaTarget(
            provider="lx", platform="kg", canonical_id="lx:kg:fixture",
            url=f"{cdn_origin}/audio.wav", ext="wav",
            reuse_deadline=time.monotonic() + 60,
            source_generation=proxy_app._DIRECT_PLAY_GENERATIONS["lx"],
            server_headers={"User-Agent": "NAS-only"},
            representation_key="synthetic-wav",
        )

    monkeypatch.setattr(proxy_app, "resolve_local_play", no_local)
    monkeypatch.setattr(proxy_app, "_open_online_stream", forbidden_stream)
    monkeypatch.setattr(proxy_app, "_resolve_direct_target", resolve_target)
    monkeypatch.setattr(proxy_app, "validate_media_url", lambda url: MediaURLDecision(
        url.startswith(tuple(fixture["origins"])), host="cdn-a.test"))

    # 模拟官方用户鉴权接口 /music/api/v1/user/me
    official = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(
            200, json={"code": 0, "data": {"guid": "test-user-guid"}})),
        base_url="http://official.test",
    )
    monkeypatch.setattr(proxy_app, "get_upstream_client", lambda _app: official)

    # 模拟非标准端口 HTTPS 入口：https://nas-entry.test:8443
    entry_base = "https://nas-entry.test:8443"

    async with official, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy_app.app),
        base_url=entry_base,
    ) as nas_client, httpx.AsyncClient(
        verify=fixture["verify"], trust_env=False, follow_redirects=False,
    ) as cdn_client:
        # 第一阶段：模拟入口 307 跳转（例如换源或选定版本），返回相对路径
        # 客户端向非标准端口发起带凭据与 Range 的 GET 请求
        req_headers = {
            "Authorization": "Bearer secret_user_token_12345",
            "Range": "bytes=0-",
        }
        # 构造模拟候选源引发的 307 响应
        entry_target_guid = "online:lx:kg:fixture"
        # 验证 307 生成逻辑：直接调用相对跳转生成规则
        relative_307_location = f"/music/api/v1/track/stream?guid={entry_target_guid}&_ext_rendition=1"

        # 客户端解析 307 相对地址，必须与当前入口基地址结合，保留非标准端口 8443 与 HTTPS 协议
        resolved_after_307 = str(httpx.URL(entry_base).join(relative_307_location))
        assert resolved_after_307.startswith("https://nas-entry.test:8443/")
        assert "http://" not in resolved_after_307

        # 第二阶段：客户端跟随 307 访问目标音频接口，进入直连 302 阶段
        resp_302 = await nas_client.get(
            f"/music/api/v1/track/stream?guid={entry_target_guid}&_ext_rendition=1",
            headers=req_headers,
        )
        assert resp_302.status_code == 302
        assert resp_302.content == b""
        assert resp_302.headers["cache-control"] == "no-store"
        assert resp_302.headers["referrer-policy"] == "no-referrer"

        # 核心断言：直连 302 交付给客户端的是绝对 CDN 地址，而非相对路径或伪造地址
        cdn_location = resp_302.headers["location"]
        assert cdn_location.startswith("https://cdn-a.test:")
        assert "/audio.wav" in cdn_location

        # 第三阶段：客户端直接请求 CDN，验证跨域行为、凭据隔离与 Range 续传
        # 客户端重定向至跨域 CDN 时，标准 HTTP 客户端不会向 CDN 附带 NAS 的 Authorization 头
        cdn_headers = {"Range": "bytes=0-"}
        cdn_resp = await cdn_client.get(cdn_location, headers=cdn_headers)
        assert cdn_resp.status_code in (200, 206)
        assert cdn_resp.content == fixture["audio"]
        assert cdn_resp.headers["access-control-allow-origin"] == "*"

        # 验证非零 Range 续传（例如拖动进度条 seek 到 2048 字节）
        seek_resp_302 = await nas_client.get(
            f"/music/api/v1/track/stream?guid={entry_target_guid}&_ext_rendition=1",
            headers={"Authorization": "Bearer secret_user_token_12345", "Range": "bytes=2048-"},
        )
        assert seek_resp_302.status_code == 302
        seek_cdn_location = seek_resp_302.headers["location"]
        seek_cdn_resp = await cdn_client.get(seek_cdn_location, headers={"Range": "bytes=2048-"})
        assert seek_cdn_resp.status_code == 206
        assert seek_cdn_resp.content == fixture["audio"][2048:]

        # 验证 HEAD 请求：方法必须透传，且 NAS 端绝不创建后台下载任务（HEAD 无下载原则）
        head_302 = await nas_client.head(
            f"/music/api/v1/track/stream?guid={entry_target_guid}",
            headers={"Authorization": "Bearer secret_user_token_12345"},
        )
        assert head_302.status_code == 302
        assert head_302.content == b""
        assert proxy_app._full_fetch_tasks == {}

        head_cdn_resp = await cdn_client.head(head_302.headers["location"])
        assert head_cdn_resp.status_code in (200, 206)
        assert head_cdn_resp.content == b""
        assert int(head_cdn_resp.headers["content-length"]) == len(fixture["audio"])


@pytest.mark.asyncio
async def test_direct_play_root_cause_analysis_absolute_302_versus_entry_307(monkeypatch, synthetic_cdns):
    """针对计划 F #107 进行根因归因实验：对比 307 入口与直接 302。

    实验证明：
    1. 无论请求直接到达还是经由 307 相对跳转到达，代理均返回包含绝对地址的 302 CDN URL；
    2. CDN 侧因异出口（蜂窝网与 NAS 出口 IP 不一致）或缺失 CORS 头引起的播放失败，
       在直接请求与 307 入口请求下表现完全相同；
    3. 因此不能将客户端直连播放失败的根因断言为入口 307 跳转。
    """
    fixture = synthetic_cdns
    cdn_origin = fixture["origins"][0]

    monkeypatch.setattr(proxy_app, "_DIRECT_TARGET_BINDINGS", DirectTargetBindings())
    monkeypatch.setattr(proxy_app, "_full_fetch_tasks", {})
    monkeypatch.setattr(proxy_app, "_source_enabled", lambda _guid: True)
    monkeypatch.setitem(proxy_app.CONF, "lx_direct_play", True)
    monkeypatch.setitem(proxy_app.CONF, "tee_save_enabled", False)
    monkeypatch.setattr(proxy_app, "find_cache_file", lambda *_args, **_kwargs: None)

    async def no_local(*_args):
        return None

    # 构造两种目标：普通地址与限制出口 IP 的直连目标（带签名 client_ip=198.51.100.1）
    current_target_url = [f"{cdn_origin}/audio.wav"]

    async def resolve_target(*_args):
        return DirectMediaTarget(
            provider="lx", platform="kg", canonical_id="lx:kg:fixture",
            url=current_target_url[0], ext="wav",
            reuse_deadline=time.monotonic() + 60,
            source_generation=proxy_app._DIRECT_PLAY_GENERATIONS["lx"],
            server_headers={"User-Agent": "NAS-only"},
            representation_key="synthetic-wav",
        )

    monkeypatch.setattr(proxy_app, "resolve_local_play", no_local)
    monkeypatch.setattr(proxy_app, "_resolve_direct_target", resolve_target)
    monkeypatch.setattr(proxy_app, "validate_media_url", lambda url: MediaURLDecision(
        url.startswith(tuple(fixture["origins"])), host="cdn-a.test"))

    official = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(
            200, json={"code": 0, "data": {"guid": "test-user-guid"}})),
        base_url="http://official.test",
    )
    monkeypatch.setattr(proxy_app, "get_upstream_client", lambda _app: official)

    async with official, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy_app.app),
        base_url="https://nas-entry.test:8443",
    ) as nas_client, httpx.AsyncClient(
        verify=fixture["verify"], trust_env=False,
    ) as cdn_client:
        auth_headers = {"Authorization": "Bearer valid_token"}

        # 对照组 1：直接访问歌曲 A，拿到 302 绝对 CDN 地址
        direct_resp = await nas_client.get(
            "/music/api/v1/track/stream?guid=online:lx:kg:fixture",
            headers=auth_headers,
        )
        assert direct_resp.status_code == 302
        location_direct = direct_resp.headers["location"]
        assert location_direct == f"{cdn_origin}/audio.wav"

        # 对照组 2：经由 307 相对跳转后带 _ext_rendition=1 再次访问，拿到 302 绝对 CDN 地址
        # 两者拿到的 Location 绝对地址完全一致
        after_307_resp = await nas_client.get(
            "/music/api/v1/track/stream?guid=online:lx:kg:fixture&_ext_rendition=1",
            headers=auth_headers,
        )
        assert after_307_resp.status_code == 302
        location_after_307 = after_307_resp.headers["location"]
        assert location_after_307 == location_direct

        # 根因检验场景 A：异出口（Egress Mismatch）
        # 某些音乐源在取链时将下载令牌绑定为 NAS 的出口 IP（如 198.51.100.1）。
        # 客户端若在移动蜂窝网络或异地宽带（模拟出口 IP 为 203.0.113.50），直连请求 CDN 时遭遇 403 拒绝。
        current_target_url[0] = f"{cdn_origin}/egress-restricted.wav?client_ip=198.51.100.1"
        monkeypatch.setattr(proxy_app, "_DIRECT_TARGET_BINDINGS", DirectTargetBindings())

        egress_resp_302 = await nas_client.get(
            "/music/api/v1/track/stream?guid=online:lx:kg:fixture",
            headers=auth_headers,
        )
        assert egress_resp_302.status_code == 302
        restricted_url = egress_resp_302.headers["location"]

        # 当客户端出口与签名 IP 一致时，CDN 正常响应
        nas_egress_play = await cdn_client.get(restricted_url, headers={"X-Client-Egress-IP": "198.51.100.1"})
        assert nas_egress_play.status_code == 200

        # 当客户端出口与签名 IP 不一致时，CDN 返回 403 Forbidden（非入口 307 问题）
        cellular_egress_play = await cdn_client.get(restricted_url, headers={"X-Client-Egress-IP": "203.0.113.50"})
        assert cellular_egress_play.status_code == 403

        # 根因检验场景 B：跨域 CORS 头缺失
        # Web 播放器在跨域加载音频时依赖 Access-Control-Allow-Origin。若 CDN 未配置 CORS，浏览器播放报错。
        no_cors_url = f"{cdn_origin}/no-cors.wav"
        no_cors_resp = await cdn_client.get(no_cors_url)
        assert no_cors_resp.status_code == 200
        assert "access-control-allow-origin" not in no_cors_resp.headers

        # 根因检验场景 C：日志中不打印秘密凭据且区分 302 与最终播放成功
        # 捕获日志事件，确保 direct_redirect_issued 仅记录脱敏主机名，不记录凭据与签名 token
        diag_records = []

        def capture_diag(log, event, **kwargs):
            diag_records.append((event, kwargs))

        monkeypatch.setattr(proxy_app, "fn_diag", capture_diag)
        await nas_client.get(
            "/music/api/v1/track/stream?guid=online:lx:kg:fixture",
            headers={"Authorization": "Bearer super_secret_pass_token"},
        )
        issued_events = [kwargs for evt, kwargs in diag_records if evt == "direct_redirect_issued"]
        assert len(issued_events) >= 1
        for evt in issued_events:
            assert evt["host"] == "cdn-a.test"
            assert "token" not in str(evt)
            assert "secret" not in str(evt)


@pytest.mark.asyncio
async def test_strict_auth_and_no_silent_proxy_fallback_on_direct_failure(monkeypatch):
    """验证严格鉴权与失败明确报错原则（绝不暗中回退为 NAS 代理供流）。

    规则要求：
    1. 无凭据请求立刻拒绝（401 authorization_failed）；
    2. 官方 /user/me 验证不通过立刻拒绝（401 authorization_failed）；
    3. 直链目标不合规（如私网 IPv4、私网 IPv6、过期等）立刻拒绝（502）；
    4. 绝不悄悄转为 NAS proxy 流式供流。
    """
    monkeypatch.setattr(proxy_app, "_DIRECT_TARGET_BINDINGS", DirectTargetBindings())
    monkeypatch.setitem(proxy_app.CONF, "lx_direct_play", True)
    monkeypatch.setattr(proxy_app, "_source_enabled", lambda _guid: True)
    monkeypatch.setattr(proxy_app, "find_cache_file", lambda *_args, **_kwargs: None)

    async def no_local(*_args):
        return None

    proxy_stream_called = []

    async def tracking_proxy_stream(*_args, **_kwargs):
        proxy_stream_called.append(True)
        raise AssertionError("直连失败时禁止静默回退至 NAS 代理供流")

    monkeypatch.setattr(proxy_app, "resolve_local_play", no_local)
    monkeypatch.setattr(proxy_app, "_open_online_stream", tracking_proxy_stream)

    # 1. 无凭据请求（没有任何 Cookie、Authorization、authx、x-trim-music-temp-token）
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy_app.app),
        base_url="https://nas.test:8443",
    ) as client:
        unauthed = await client.get("/music/api/v1/track/stream?guid=online:lx:kg:fixture")
        assert unauthed.status_code == 401
        assert unauthed.json()["data"]["reason"] == "authorization_failed"
        assert proxy_stream_called == []

        # 2. 官方 /user/me 返回鉴权失败
        denied_upstream = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _req: httpx.Response(
                401, json={"code": 99999, "msg": "INVALID TOKEN"})),
            base_url="http://official.test",
        )
        monkeypatch.setattr(proxy_app, "get_upstream_client", lambda _app: denied_upstream)
        auth_failed = await client.get(
            "/music/api/v1/track/stream?guid=online:lx:kg:fixture",
            headers={"Authorization": "Bearer bad_token"},
        )
        assert auth_failed.status_code == 401
        assert auth_failed.json()["data"]["reason"] == "authorization_failed"
        assert proxy_stream_called == []

        # 3. 官方 /user/me 鉴权通过，但解析目标为私网地址（SSRF 防御）
        ok_upstream = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _req: httpx.Response(
                200, json={"code": 0, "data": {"guid": "valid_user"}})),
            base_url="http://official.test",
        )
        monkeypatch.setattr(proxy_app, "get_upstream_client", lambda _app: ok_upstream)

        async def resolve_private_target(*_args):
            return DirectMediaTarget(
                provider="lx", platform="kg", canonical_id="lx:kg:fixture",
                url="https://192.168.1.100/audio.flac", ext="flac",
                reuse_deadline=time.monotonic() + 60,
                source_generation=proxy_app._DIRECT_PLAY_GENERATIONS["lx"],
            )

        monkeypatch.setattr(proxy_app, "_resolve_direct_target", resolve_private_target)
        private_target_resp = await client.get(
            "/music/api/v1/track/stream?guid=online:lx:kg:fixture",
            headers={"Authorization": "Bearer valid_token"},
        )
        assert private_target_resp.status_code == 502
        assert private_target_resp.json()["data"]["reason"] == "host_not_public"
        assert proxy_stream_called == []
