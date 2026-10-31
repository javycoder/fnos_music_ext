"""tests/integration/test_lx_adapter_comparison.py
离线严格测试：洛雪原生链接 vs 项目适配流程对比、受限公网媒体传输与有界前缀解码。

覆盖范围：
1. 媒体传输安全：私网/回环/元数据/IPv6/userinfo 拒绝且真实 TCP trap 收不到连接；
   DNS 只解析一次并把 TCP 固定到已校验公网 IPv4；逐跳重定向重新校验；
   环境代理不生效；HTTPS 保留原始 hostname 的 SNI 与证书校验。
2. 真实 app.py 流程：用受控 loopback 原生 API 假后端驱动 app._attempt_pinned，
   验证 sourceId 校验、type 回显只当解析档、有损冒充无损被正确拒绝、
   内部服务错误不判脚本失效、单候选失败不判整源死。
3. 有界前缀解码：ffmpeg pipe 输入、0.25 秒单声道 16kHz s16le、输出上限、
   超时/取消进程组回收、stderr 不输出原文、缺前缀/缺 moov 只判 needs_more_data。
4. 失败/边界：deadline 共用、非 loopback 管理 origin 拒绝、不可信快照拒绝、
   报告不出现 URL/密钥/Cookie/错误正文。

注意：本机没有 ffmpeg 时，真实解码成功用例会 skip；进程控制与边界用例使用
测试用假 ffmpeg 脚本，仍会执行。第三方脚本、外网和项目构建一律不执行。
"""

from __future__ import annotations

import asyncio
import datetime
import io
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import sys
import threading
import time
import wave

import pytest

# 确保能导入 tests/integration 下的模块
INTEGRATION_DIR = Path(__file__).resolve().parent
REPO_ROOT = INTEGRATION_DIR.parents[1]
if str(INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(INTEGRATION_DIR))

import lx_adapter_comparison as lac  # noqa: E402

APP_SOURCE = REPO_ROOT / "lxmusic-service" / "app.py"
GLOBAL_TEST_IP = "93.184.216.34"  # 保留给测试的示例公网 IPv4；不会真的连它
TEST_ADMIN_TOKEN = "test-admin-token-not-a-secret"
REAL_FFMPEG = shutil.which("ffmpeg")

requires_ffmpeg = pytest.mark.skipif(
    REAL_FFMPEG is None,
    reason="本机没有 ffmpeg：真实前缀解码成功路径未执行（仅进程控制/边界用假二进制）",
)


# ==============================================================================
# 1. 通用测试基础设施：合成音频、假后端、陷阱监听、TLS 材料
# ==============================================================================

def _wav_bytes(seconds: float = 1.0, rate: int = 8000) -> bytes:
    """生成单声道 16bit PCM WAV；只用于测试，不构建项目。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def _fake_mp3_bytes(size: int = 2000) -> bytes:
    """构造 ID3 魔数前缀；只用于魔数/拒绝路径，不冒充真实 MP3 解码。"""
    return b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" + b"\x00" * size


def _quiet_server(*args, **kwargs):
    """构造不打印请求日志的 HTTPServer 子类。"""
    from http.server import ThreadingHTTPServer

    class _Quiet(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):  # noqa: D401
            """客户端超时/断开是预期路径，不向测试输出打印 traceback。"""
            return

    return _Quiet(*args, **kwargs)


class _FakeBackend:
    """受控的 loopback「原生 API + 媒体」假后端。

    这里只替身 lxserver HTTP 边界，不替身 app.py 的解析、校验或探活结果：
    对比流程仍然真实执行 app._attempt_pinned 与 app.probe_url。
    """

    def __init__(self):
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.api_status = 200
        self.api_delay = 0.0
        self.echo_lx_quality = "128k"
        self.returned_source_id = "fakesource"
        self.media_url_override = ""
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self._lock = threading.Lock()
        self.wav = _wav_bytes(1.0)
        self.routes["/stream.wav"] = (200, {"Content-Type": "audio/wav"}, self.wav)
        self.routes["/lossy.mp3"] = (200, {"Content-Type": "audio/mpeg"}, _fake_mp3_bytes())
        self.routes["/big.wav"] = (
            200,
            {"Content-Type": "audio/wav"},
            self.wav + b"\x00" * (256 * 1024),
        )
        self.server = _quiet_server(("127.0.0.1", 0), self._handler())
        self.server.backend = self
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def media_url(self, path: str = "/stream.wav") -> str:
        """媒体 URL 使用不会解析到私网的域名；测试注入 resolver 返回公网 IP。"""
        if self.media_url_override:
            return self.media_url_override
        return f"http://media.example:{self.port}{path}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=2.0)

    # ------------------------------------------------------------ HTTP 处理 --
    def _handler(self):
        backend = self

        class Handler(__import__("http.server", fromlist=["BaseHTTPRequestHandler"]).BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # noqa: D401
                return

            def _record(self):
                wanted = {"cookie", "authorization", "referer", "user-agent", "range"}
                headers = {
                    key.lower(): value
                    for key, value in self.headers.items()
                    if key.lower() in wanted
                }
                with backend._lock:
                    backend.requests.append((self.command, self.path, headers))

            def do_POST(self):
                self._record()
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                if backend.api_delay:
                    time.sleep(backend.api_delay)
                if backend.api_status != 200:
                    payload = b'{"error":"backend-unavailable"}'
                    self.send_response(backend.api_status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                try:
                    request = json.loads(body.decode("utf-8") or "{}")
                except Exception:
                    request = {}
                response = {
                    "url": backend.media_url("/stream.wav"),
                    "type": backend.echo_lx_quality,
                    "sourceId": backend.returned_source_id,
                    "sourceName": "fake-native",
                    "requestedQuality": request.get("quality", ""),
                }
                payload = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._record()
                path = self.path.split("?", 1)[0]
                route = backend.routes.get(path)
                if route is None:
                    payload = b"not found"
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                status, headers, body = route
                if status in (301, 302, 303, 307, 308):
                    self.send_response(status)
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path == "/slow.wav":
                    time.sleep(float(headers.get("X-Test-Delay", "0.8")))
                self.send_response(status)
                for key, value in headers.items():
                    if key.lower() == "x-test-delay":
                        continue
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


class _TrapServer:
    """记录是否收到 TCP 连接的陷阱监听；用于证明被拒绝目标根本没有拨号。"""

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.05)
        self.port = self._sock.getsockname()[1]
        self.hits: list[float] = []
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.hits.append(time.monotonic())
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=1.0)


@pytest.fixture()
def backend():
    """每个测试独立的受控原生 API + 媒体假后端。"""
    server = _FakeBackend()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture()
def trap():
    """真实 TCP 陷阱监听。"""
    holder = _TrapServer()
    try:
        yield holder
    finally:
        holder.close()


def _resolver_for(ip: str = GLOBAL_TEST_IP, calls: list | None = None):
    """构造只返回指定公网 IPv4 的 resolver；记录调用次数以证明只解析一次。"""
    def resolver(host: str, port: int):
        if calls is not None:
            calls.append((host, port))
        return [ip]

    return resolver


def _dial_to(port: int, calls: list | None = None, host: str = "127.0.0.1"):
    """把已固定的 TCP 目标改拨到本机测试服务；这是离线测试的唯一拨号替身。"""
    def dial(address, timeout):
        if calls is not None:
            calls.append(address)
        return socket.create_connection((host, port), timeout=timeout)

    return dial


def _deadline(seconds: float = 6.0) -> float:
    return time.monotonic() + seconds


def _wait_file(path: Path, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _make_fake_ffmpeg(tmp_path: Path, name: str, body: str) -> Path:
    """生成测试用假 ffmpeg shell 脚本；不是构建项目产物。"""
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


# ==============================================================================
# 2. 媒体传输安全：私网/元数据/IPv6/userinfo/二次解析/重定向/代理/TLS
# ==============================================================================

class TestMediaTransportGuard:
    """受限媒体传输只允许公共 IPv4，且实际 TCP 必须固定到已校验地址。"""

    @pytest.mark.parametrize(
        "url_factory",
        [
            lambda trap: f"http://127.0.0.1:{trap.port}/x",
            lambda trap: "http://10.0.0.8/x",
            lambda trap: "http://192.168.1.10/x",
            lambda trap: "http://169.254.169.254/latest/meta-data",
            lambda trap: "http://100.64.0.1/x",
            lambda trap: "http://198.18.0.1/x",
            lambda trap: f"http://[::1]:{trap.port}/x",
            lambda trap: f"http://user:pass@media.example:{trap.port}/x",
            lambda trap: "ftp://media.example/x",
        ],
    )
    def test_disallowed_targets_never_open_tcp(self, trap, url_factory):
        """私网/回环/元数据/IPv6/userinfo/非 http(s) 在拨号前拒绝，陷阱零连接。"""
        url = url_factory(trap)
        with pytest.raises((lac.MediaBlocked, lac.MediaFetchFailed)):
            lac.fetch_media_prefix(url, deadline=_deadline(2.0))
        assert trap.hits == [], f"被拒绝目标仍然拨号: {url}"

    def test_domain_resolving_to_private_is_blocked_before_connect(self, trap):
        """安全 DNS 查询本身不算连接固定：解析结果非公网时必须拒绝拨号。"""
        url = f"http://media.example:{trap.port}/x"
        resolver = _resolver_for("127.0.0.1")
        with pytest.raises(lac.MediaBlocked) as info:
            lac.fetch_media_prefix(url, deadline=_deadline(2.0), resolver=resolver)
        assert info.value.reason in ("private_or_reserved_ip", "dns_non_public")
        assert trap.hits == []

    def test_mixed_public_private_dns_answer_is_rejected(self, trap):
        """DNS 同时返回公网和私网地址时整次拒绝，避免挑选攻击者可控地址。"""
        def resolver(host, port):
            return [GLOBAL_TEST_IP, "10.1.2.3"]

        with pytest.raises(lac.MediaBlocked):
            lac.fetch_media_prefix(
                f"http://media.example:{trap.port}/x",
                deadline=_deadline(2.0),
                resolver=resolver,
            )
        assert trap.hits == []

    def test_resolves_once_and_pins_tcp_address(self, backend, monkeypatch):
        """只解析一次；TCP 实际收到的是已校验 IPv4，而不是再次解析域名。"""
        calls: list = []
        dial_calls: list = []
        resolver = _resolver_for(GLOBAL_TEST_IP, calls)
        result = lac.fetch_media_prefix(
            backend.media_url("/stream.wav"),
            deadline=_deadline(3.0),
            resolver=resolver,
            dial=_dial_to(backend.port, dial_calls),
        )
        assert result.status == 200
        assert result.prefix.startswith(b"RIFF")
        assert len(calls) == 1, f"resolver 被调用 {len(calls)} 次，存在二次解析"
        assert dial_calls and dial_calls[0][0] == GLOBAL_TEST_IP

    def test_redirect_is_rechecked_per_hop_and_records_hop_count(self, backend):
        """重定向逐跳重新解析/校验，并只记录跳数，不记录 Location 或 URL。"""
        backend.routes["/redir-audio"] = (
            302,
            {"Location": f"http://b.example:{backend.port}/stream.wav"},
            b"",
        )
        hosts: list = []
        def resolver(host, port):
            hosts.append(host)
            return [GLOBAL_TEST_IP]

        result = lac.fetch_media_prefix(
            f"http://a.example:{backend.port}/redir-audio",
            deadline=_deadline(3.0),
            resolver=resolver,
            dial=_dial_to(backend.port),
        )
        assert result.status == 200
        assert hosts == ["a.example", "b.example"]
        assert result.hops == 1
        evidence = json.dumps(result.evidence())
        assert "Location" not in evidence and "redir-audio" not in evidence

    def test_redirect_to_private_is_blocked(self, backend):
        """重定向指向元数据地址时拒绝，且不会对私网目标拨号。"""
        backend.routes["/redir-private"] = (
            302,
            {"Location": "http://169.254.169.254/latest/meta-data"},
            b"",
        )
        dial_calls: list = []
        with pytest.raises(lac.MediaBlocked):
            lac.fetch_media_prefix(
                f"http://media.example:{backend.port}/redir-private",
                deadline=_deadline(3.0),
                resolver=_resolver_for(),
                dial=_dial_to(backend.port, dial_calls),
            )
        assert len(dial_calls) == 1

    def test_redirect_limit_enforced(self, backend):
        """超过重定向上限时明确失败，不能无限跟跳。"""
        for index in range(6):
            backend.routes[f"/chain/{index}"] = (
                302,
                {"Location": f"/chain/{index + 1}"},
                b"",
            )
        with pytest.raises(lac.MediaFetchFailed) as info:
            lac.fetch_media_prefix(
                f"http://media.example:{backend.port}/chain/0",
                deadline=_deadline(5.0),
                resolver=_resolver_for(),
                dial=_dial_to(backend.port),
                max_redirects=3,
            )
        assert info.value.reason == "too_many_redirects"

    def test_environment_proxy_is_not_used(self, backend, monkeypatch, trap):
        """媒体读取禁用环境代理：代理陷阱零连接，直连测试服务正常拿到前缀。"""
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{trap.port}")
        monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{trap.port}")
        monkeypatch.setenv("ALL_PROXY", f"http://127.0.0.1:{trap.port}")
        result = lac.fetch_media_prefix(
            backend.media_url("/stream.wav"),
            deadline=_deadline(3.0),
            resolver=_resolver_for(),
            dial=_dial_to(backend.port),
        )
        assert result.prefix.startswith(b"RIFF")
        assert trap.hits == []

    def test_prefix_is_bounded_to_64kib(self, backend):
        """服务端返回 200KB 时只保留前 64KiB 前缀。"""
        result = lac.fetch_media_prefix(
            backend.media_url("/big.wav"),
            deadline=_deadline(3.0),
            resolver=_resolver_for(),
            dial=_dial_to(backend.port),
        )
        assert result.status == 200
        assert len(result.prefix) == lac.MAX_PREFIX_BYTES

    def test_request_headers_filtered_to_safe_allowlist(self, backend):
        """只转发 UA/Referer，Cookie/Authorization 不进入媒体请求。"""
        result = lac.fetch_media_prefix(
            backend.media_url("/stream.wav"),
            deadline=_deadline(3.0),
            resolver=_resolver_for(),
            dial=_dial_to(backend.port),
            headers={
                "User-Agent": "test-agent",
                "Referer": "https://example.test/",
                "Cookie": "session=secret",
                "Authorization": "Bearer secret",
            },
        )
        assert result.status == 200
        request_headers = backend.requests[-1][2]
        assert request_headers.get("referer") == "https://example.test/"
        assert request_headers.get("user-agent") == "test-agent"
        assert "cookie" not in request_headers
        assert "authorization" not in request_headers

    def test_deadline_bounds_slow_media_fetch(self, backend):
        """媒体读取共用调用者 deadline：慢响应在预算内失败，不重置为长超时。"""
        backend.routes["/slow.wav"] = (
            200,
            {"Content-Type": "audio/wav", "X-Test-Delay": "1.2"},
            backend.wav,
        )
        started = time.monotonic()
        with pytest.raises(lac.MediaFetchFailed):
            lac.fetch_media_prefix(
                backend.media_url("/slow.wav"),
                deadline=_deadline(0.35),
                resolver=_resolver_for(),
                dial=_dial_to(backend.port),
            )
        assert time.monotonic() - started < 1.5

    def test_tls_preserves_sni_and_verifies_certificate(self, tmp_path):
        """HTTPS 固定公网 IPv4，但 SNI 与证书校验仍使用原始 hostname。"""
        pytest.importorskip("cryptography")
        tls = _TlsServer(tmp_path)
        try:
            client_context = ssl.create_default_context(cafile=str(tls.cert_path))
            result = lac.fetch_media_prefix(
                f"https://media.example:{tls.port}/stream.wav",
                deadline=_deadline(3.0),
                resolver=_resolver_for(),
                dial=_dial_to(tls.port),
                ssl_context=client_context,
            )
            assert result.prefix.startswith(b"RIFF")
            assert tls.sni_names == ["media.example"]

            # 证书只对 media.example 有效：换 hostname 必须校验失败，证明不是跳过校验。
            with pytest.raises(lac.MediaFetchFailed) as info:
                lac.fetch_media_prefix(
                    f"https://other.example:{tls.port}/stream.wav",
                    deadline=_deadline(3.0),
                    resolver=_resolver_for(),
                    dial=_dial_to(tls.port),
                    ssl_context=client_context,
                )
            assert info.value.reason == "tls_failed"
            assert tls.sni_names[-1] == "other.example"
        finally:
            tls.close()


class _TlsServer:
    """本地自签 TLS 服务；记录 SNI 名称以验证 HTTPS transport 行为。"""

    def __init__(self, tmp_path: Path):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        from http.server import BaseHTTPRequestHandler

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "media.example")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName("media.example")]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        self.cert_path = tmp_path / "cert.pem"
        key_path = tmp_path / "key.pem"
        self.cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )

        body = _wav_bytes(0.5)

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        self.server = _quiet_server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.sni_names: list[str] = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.cert_path), str(key_path))

        def sni_callback(sock, server_name, ctx):
            self.sni_names.append(server_name)
            return None

        context.sni_callback = sni_callback
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=2.0)


# ==============================================================================
# 3. 有界前缀解码：ffmpeg pipe、输出/时间/取消/错误脱敏
# ==============================================================================

class TestPrefixDecode:
    """前缀解码只证明前缀可解，不宣称整曲或客户端播放成功。"""

    @requires_ffmpeg
    def test_real_ffmpeg_decodes_synthetic_wav_prefix(self):
        """本机 ffmpeg 处理自己生成的合成 WAV：0.25s 单声道 16kHz s16le。"""
        prefix = _wav_bytes(1.0)
        report = asyncio.run(lac.decode_prefix(prefix, deadline=_deadline(4.0)))
        assert report["scope"] == "prefix_decode"
        assert report["status"] == "decoded"
        assert report["sample_rate_hz"] == 16000
        assert report["channels"] == 1
        assert 0 < report["pcm_bytes"] <= lac.MAX_PCM_BYTES
        assert report["duration_ms"] <= 250
        assert report["client_playback"] == "not_tested"

    @requires_ffmpeg
    def test_real_ffmpeg_marks_invalid_prefix_inconclusive(self):
        """无效前缀不得断言整曲不可播，只允许 inconclusive/needs_more_data。"""
        report = asyncio.run(
            lac.decode_prefix(b"not-audio" * 200, deadline=_deadline(4.0))
        )
        assert report["status"] in ("inconclusive", "needs_more_data")
        assert report["client_playback"] == "not_tested"

    def test_missing_ffmpeg_is_not_tested(self, monkeypatch):
        """找不到 ffmpeg 时明确 not_tested，不能猜可播性。"""
        monkeypatch.setattr(lac.shutil, "which", lambda name: None)
        report = asyncio.run(
            lac.decode_prefix(_wav_bytes(0.2), deadline=_deadline(2.0), ffmpeg_path=None)
        )
        assert report["status"] == "not_tested"
        assert report["reason"] == "ffmpeg_missing"

    def test_insufficient_deadline_is_not_tested(self):
        """deadline 剩余不足时明确 not_tested，不启动解码。"""
        report = asyncio.run(
            lac.decode_prefix(_wav_bytes(0.2), deadline=time.monotonic() + 0.05)
        )
        assert report["status"] == "not_tested"
        assert report["reason"] == "deadline_insufficient"

    def test_prefix_too_large_is_rejected(self):
        """只接收 <=64KiB 前缀；超限直接拒绝，防止无界输入。"""
        with pytest.raises(ValueError):
            asyncio.run(
                lac.decode_prefix(
                    b"\x00" * (lac.MAX_PREFIX_BYTES + 1), deadline=_deadline(2.0)
                )
            )

    def test_empty_prefix_needs_more_data(self):
        """空前缀只判 needs_more_data，不能断言不可播。"""
        report = asyncio.run(lac.decode_prefix(b"", deadline=_deadline(2.0)))
        assert report["status"] == "needs_more_data"
        assert report["reason"] == "prefix_too_small"

    def test_moov_missing_maps_to_needs_more_data(self, tmp_path):
        """前缀缺 moov 是数据不足，不是整曲不可播；stderr 原文不外泄。"""
        fake = _make_fake_ffmpeg(
            tmp_path,
            "ffmpeg-moov",
            "echo 'moov atom not found' >&2\nexit 1\n",
        )
        report = asyncio.run(
            lac.decode_prefix(_fake_mp3_bytes(), deadline=_deadline(2.0), ffmpeg_path=str(fake))
        )
        assert report["status"] == "needs_more_data"
        assert report["reason"] == "container_moov_missing"
        assert "moov atom not found" not in json.dumps(report)

    def test_decode_timeout_kills_process_group(self, tmp_path):
        """解码硬超时（<=2s 且不超过 deadline）后终止并回收子进程。"""
        fake = _make_fake_ffmpeg(
            tmp_path,
            "ffmpeg-slow",
            'echo $$ > "$(dirname "$0")/pid"\nsleep 30\n',
        )
        pid_file = tmp_path / "pid"
        started = time.monotonic()
        report = asyncio.run(
            lac.decode_prefix(_wav_bytes(0.2), deadline=_deadline(1.0), ffmpeg_path=str(fake))
        )
        assert report["status"] == "timed_out"
        assert time.monotonic() - started < 3.0
        assert _wait_file(pid_file)
        pid = int(pid_file.read_text().strip())
        assert not _pid_alive(pid), "超时后子进程未被回收"

    def test_decode_cancellation_terminates_process(self, tmp_path):
        """取消解码任务必须终止进程组并回收，不能留下后台 ffmpeg。"""
        fake = _make_fake_ffmpeg(
            tmp_path,
            "ffmpeg-cancel",
            'echo $$ > "$(dirname "$0")/pid"\nsleep 30\n',
        )
        pid_file = tmp_path / "pid"

        async def scenario():
            task = asyncio.create_task(
                lac.decode_prefix(
                    _wav_bytes(0.2), deadline=_deadline(10.0), ffmpeg_path=str(fake)
                )
            )
            assert await _async_wait_file(pid_file)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        pid = int(pid_file.read_text().strip())
        assert not _pid_alive(pid), "取消后子进程未被回收"

    def test_decode_output_capped(self, tmp_path):
        """假 ffmpeg 输出 100KB 时只保留有界 PCM，并标记 output_capped。"""
        fake = _make_fake_ffmpeg(tmp_path, "ffmpeg-big", "head -c 100000 /dev/zero\n")
        report = asyncio.run(
            lac.decode_prefix(_wav_bytes(0.2), deadline=_deadline(2.0), ffmpeg_path=str(fake))
        )
        assert report["status"] == "output_capped"
        assert report["pcm_bytes"] <= lac.MAX_PCM_BYTES

    def test_decode_stderr_never_returned(self, tmp_path):
        """stderr 只保留受控分类和字节数，不输出原文。"""
        marker = "SECRET-STDERR-MARKER-123"
        fake = _make_fake_ffmpeg(
            tmp_path,
            "ffmpeg-error",
            f"echo '{marker}' >&2\nexit 1\n",
        )
        report = asyncio.run(
            lac.decode_prefix(_wav_bytes(0.2), deadline=_deadline(2.0), ffmpeg_path=str(fake))
        )
        assert report["status"] in ("inconclusive", "needs_more_data")
        assert marker not in json.dumps(report)
        assert report["stderr_bytes"] > 0

    def test_stub_pcm_pipeline_bounds(self, tmp_path):
        """测试替身 ffmpeg 写出 8000 字节 PCM：验证输出上限/时长换算（非真实解码）。"""
        fake = _make_fake_ffmpeg(tmp_path, "ffmpeg-pcm", "head -c 8000 /dev/zero\n")
        report = asyncio.run(
            lac.decode_prefix(_wav_bytes(0.2), deadline=_deadline(2.0), ffmpeg_path=str(fake))
        )
        assert report["status"] == "decoded"
        assert report["pcm_bytes"] == 8000
        assert report["duration_ms"] == 250

    def test_decode_argv_is_pipe_only(self):
        """解码命令只允许 pipe 输入输出，且输出参数有界（无 ffmpeg 也能校验）。"""
        argv = lac._decode_argv("/usr/local/bin/ffmpeg")
        assert "pipe:0" in argv and "pipe:1" in argv
        index = argv.index("-protocol_whitelist")
        assert argv[index + 1] == "pipe"
        assert all("http" not in part for part in argv)
        assert lac.DECODE_HARD_TIMEOUT_S <= 2.0
        assert lac.DECODE_MAX_SECONDS == 0.25
        assert lac.PCM_SAMPLE_RATE == 16000
        assert lac.PCM_CHANNELS == 1
        assert lac.MAX_PCM_BYTES == 8192


async def _async_wait_file(path: Path, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists():
            return True
        await asyncio.sleep(0.02)
    return False


# ==============================================================================
# 4. 真实 app.py 适配流程对比（受控 loopback 原生 API 假后端）
# ==============================================================================

def _request(backend: _FakeBackend, **overrides) -> lac.CandidateComparisonRequest:
    values = {
        "app_source_path": str(APP_SOURCE),
        "app_sha256": lac.snapshot_sha256(APP_SOURCE),
        "management_origin": f"http://127.0.0.1:{backend.port}",
        "admin_password": TEST_ADMIN_TOKEN,
        "source_id": "fakesource",
        "platform": "kw",
        "song_info": {
            "source": "kw",
            "songmid": "abc123",
            "name": "测试歌曲",
            "singer": "测试歌手",
        },
        "quality": "standard",
        "deadline": _deadline(6.0),
        "resolver": _resolver_for(),
        "dial": _dial_to(backend.port),
    }
    values.update(overrides)
    return lac.CandidateComparisonRequest(**values)


class TestAdapterComparison:
    """真实执行 app._attempt_pinned 与 app.probe_url，只替身原生 API 边界。"""

    def _run(self, request):
        return asyncio.run(lac.compare_candidate(request))

    def test_success_flow_uses_real_app_parse_and_probe(self, backend):
        """成功用例：真实 app 解析 + 真实探活 + 受限 transport + 候选级结论。"""
        report = self._run(_request(backend))
        assert report["scope"] == "adapter_candidate_comparison"
        assert report["adapter"]["status"] == "ok"
        assert report["native_resolve"]["status"] == "url_present"
        assert report["native_resolve"]["source_id_matched"] is True
        assert report["native_prefix"]["status"] == "ok"
        assert report["adapter_prefix"]["status"] == "ok"
        assert report["prefix_comparison"]["same_bytes"] is True
        assert report["candidate_disposition"] == "adapter_ok"
        assert report["source_verdict"] is None
        assert report["quality"]["actual_quality"] is None
        assert report["quality"]["actual_known"] is False
        assert report["quality"]["resolution_quality"] == "standard"
        assert report["quality"]["resolution_quality_is_request_echo"] is True
        assert report["coverage"]["adapter_flow"] == "tested"

        if REAL_FFMPEG is None:
            assert report["decode"]["native"]["status"] == "not_tested"
            assert report["decode"]["adapter"]["status"] == "not_tested"
        else:
            assert report["decode"]["native"]["status"] == "decoded"
            assert report["decode"]["adapter"]["status"] == "decoded"
            assert report["decode"]["native"]["scope"] == "prefix_decode"

        serialized = json.dumps(report, ensure_ascii=False)
        # 成功路径必须真的经过受限 transport 读媒体，而不是只信 app 返回的 url。
        assert any(method == "GET" for method, _path, _headers in backend.requests)
        for forbidden in (
            TEST_ADMIN_TOKEN,
            "http://",
            "/stream.wav",
            "x-frontend-auth",
            "Cookie",
            str(APP_SOURCE),
        ):
            assert forbidden not in serialized

    def test_native_prefix_is_fetched_once_and_reused_for_decode(self, backend, monkeypatch):
        """原生侧已取到前缀后，解码只复用该响应，不再消耗媒体请求与总预算。"""
        fetch = lac.fetch_media_prefix
        prefixes = []
        decoded = []

        def fetch_once_per_side(*args, **kwargs):
            # 第一份来自真实适配探活，第二份来自原生对比；第三次已无验证用途。
            if len(prefixes) >= 2:
                raise lac.MediaFetchFailed("deadline_exhausted")
            response = fetch(*args, **kwargs)
            prefixes.append(response.prefix)
            return response

        async def record_decode(prefix, **kwargs):
            decoded.append(prefix)
            return {"status": "decoded"}

        monkeypatch.setattr(lac, "fetch_media_prefix", fetch_once_per_side)
        monkeypatch.setattr(lac, "decode_prefix", record_decode)
        report = self._run(_request(backend))
        assert report["candidate_disposition"] == "adapter_ok"
        assert len(prefixes) == 2
        assert decoded == [prefixes[1], prefixes[0]]
        media_requests = [row for row in backend.requests if row[1] == "/stream.wav"]
        assert len(media_requests) == 2

    def test_app_exception_is_contained_as_inconclusive(self, backend, monkeypatch):
        """真实 app 入口抛异常时只记受控类型，不炸整轮、不判整源死。"""
        module = lac.load_app_module(str(APP_SOURCE), lac.snapshot_sha256(APP_SOURCE))

        async def boom(*args, **kwargs):
            raise RuntimeError("boom-secret-detail")

        monkeypatch.setattr(module, "_attempt_pinned", boom)
        report = self._run(_request(backend))
        assert report["adapter"]["status"] == "error"
        assert report["adapter"]["error_type"] == "RuntimeError"
        assert report["candidate_disposition"] == "inconclusive"
        assert report["source_verdict"] is None
        assert "boom-secret-detail" not in json.dumps(report, ensure_ascii=False)

    def test_source_id_mismatch_is_rejected_without_source_verdict(self, backend):
        """上游返回别的 sourceId 时项目拒绝，且不据此判整源失效。"""
        backend.returned_source_id = "other-source"
        report = self._run(_request(backend))
        assert report["adapter"]["status"] == "rejected"
        assert report["adapter"]["stage"] == "source_id_mismatch"
        assert report["candidate_disposition"] == "adapter_rejected"
        assert report["source_verdict"] is None
        assert report["adapter_prefix"]["status"] == "not_fetched"

    def test_lossy_media_with_flac_echo_is_honest_rejection(self, backend):
        """原生可取链但项目拒绝有损冒充无损：正确拒绝，不当项目缺陷。"""
        backend.echo_lx_quality = "flac"
        backend.routes["/stream.wav"] = (200, {"Content-Type": "audio/mpeg"}, _fake_mp3_bytes())
        report = self._run(_request(backend, quality="lossless"))
        assert report["native_resolve"]["status"] == "url_present"
        assert report["native_prefix"]["status"] == "ok"
        assert report["native_prefix"]["magic"] == "mp3"
        assert report["adapter"]["status"] == "rejected"
        assert report["adapter"]["stage"] == "quality_format_mismatch"
        assert report["candidate_disposition"] == "adapter_honest_reject"
        assert report["quality"]["resolution_quality"] == "lossless"
        assert report["quality"]["actual_quality"] is None
        assert report["source_verdict"] is None

    def test_quality_above_target_is_rejected(self, backend):
        """请求 standard 却回显 flac：解析档高于上限，项目拒绝该表示。"""
        backend.echo_lx_quality = "flac"
        report = self._run(_request(backend, quality="standard"))
        assert report["adapter"]["status"] == "rejected"
        assert report["adapter"]["stage"] == "quality_above_target"
        assert report["candidate_disposition"] == "adapter_honest_reject"

    def test_missing_type_echo_stays_unknown(self, backend):
        """type 缺失时解析档与实际档都保持未知，不按扩展名回填。"""
        backend.echo_lx_quality = ""
        report = self._run(_request(backend))
        assert report["adapter"]["status"] == "ok"
        assert report["quality"]["resolution_quality"] is None
        assert report["quality"]["resolution_lx_quality"] is None
        assert report["quality"]["actual_quality"] is None
        assert report["quality"]["actual_known"] is False

    def test_internal_service_error_is_not_script_failure(self, backend):
        """lxserver 503 属于内部服务故障，不写脚本失败、不判整源死。"""
        backend.api_status = 503
        report = self._run(_request(backend))
        assert report["adapter"]["status"] == "service_error"
        assert report["candidate_disposition"] == "inconclusive_service_error"
        assert report["source_verdict"] is None
        assert report["native_resolve"]["status"] == "error"

    def test_shared_deadline_not_reset(self, backend):
        """解析共用调用者 deadline：接口慢时按剩余预算收尾，不会重置为长预算。"""
        backend.api_delay = 0.7
        started = time.monotonic()
        report = self._run(_request(backend, deadline=_deadline(0.45)))
        elapsed = time.monotonic() - started
        # app 把内部超时归为 service_error，把 asyncio 预算到期归为 timeout；两者都不是脚本失败。
        assert report["adapter"]["status"] in ("timeout", "service_error")
        assert report["candidate_disposition"] in (
            "inconclusive_timeout",
            "inconclusive_service_error",
        )
        assert report["source_verdict"] is None
        assert elapsed < 3.0, f"疑似重置了总预算，实际耗时 {elapsed:.2f}s"

    def test_exhausted_deadline_makes_no_request(self, backend):
        """deadline 已耗尽时直接 not_tested，不导入 app、不发请求。"""
        report = self._run(_request(backend, deadline=time.monotonic() - 1.0))
        assert report["candidate_disposition"] == "not_tested"
        assert report["deadline_exhausted"] is True
        assert backend.requests == []

    @pytest.mark.parametrize(
        "origin",
        [
            "http://192.168.1.10:8005",
            "http://127.0.0.1",
            "http://[::1]:8005",
            "https://127.0.0.1:8005",
            "http://user:pass@127.0.0.1:8005",
            "http://127.0.0.1:8005/api/music/url",
        ],
    )
    def test_management_origin_must_be_exact_loopback(self, backend, origin):
        """原生 API 客户端只允许精确的本实例 loopback 管理 origin。"""
        with pytest.raises(lac.ComparisonConfigError):
            self._run(_request(backend, management_origin=origin))
        assert backend.requests == []

    def test_valid_management_origin_normalized(self):
        """合法 loopback origin 规范化后返回，带端口。"""
        assert lac.validate_management_origin("http://127.0.0.1:8005/") == "http://127.0.0.1:8005"
        assert lac.validate_management_origin("http://127.0.0.2:18005") == "http://127.0.0.2:18005"

    def test_untrusted_snapshot_rejected(self, tmp_path, backend):
        """没有真实 app 关键入口的玩具文件不能当快照导入。"""
        toy = tmp_path / "app.py"
        toy.write_text("def probe_url():\n    return True\n", encoding="utf-8")
        with pytest.raises(lac.ComparisonConfigError):
            asyncio.run(
                lac.compare_candidate(
                    _request(backend, app_source_path=str(toy), app_sha256="")
                )
            )
        with pytest.raises(lac.ComparisonConfigError):
            lac.load_app_module(str(toy))

    def test_snapshot_hash_mismatch_rejected(self, backend):
        """显式传入的 sha256 不匹配时拒绝导入。"""
        with pytest.raises(lac.ComparisonConfigError):
            asyncio.run(
                lac.compare_candidate(_request(backend, app_sha256="0" * 64))
            )

    def test_relative_snapshot_path_rejected(self):
        """快照必须是绝对路径，避免误用镜像内旧 app。"""
        with pytest.raises(lac.ComparisonConfigError):
            lac.load_app_module("lxmusic-service/app.py")
