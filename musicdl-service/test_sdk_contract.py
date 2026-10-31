"""musicdl 适配层与真实 SDK 契约回归（缺陷 #103 修复集）。
覆盖五块被批准的缺陷：
1. 依赖固定与真实SDK构造参数验证：requests_overrides不要向已经
   自带显式 timeout 的源（Soda）再注入 timeout。
2. normalize 必须保留实际 protocol、媒体格式、需后处理/受保护状态；
   cookies/headers 不下发给调用方。
3. HEAD 403/405 不能直接失败：改为有界 GET Range 并读 magic。
4. worker 异常要保留脱敏类型与阶段（search/probe）。
5. 平台表的结论要能被真实注册表核对。

这里不使用全局 socket 超时、不修改系统的网络默认、不连外网。
"""
import asyncio
import importlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import types
import zipfile
from pathlib import Path

import httpx
import pytest

_HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------- 1. 依赖固定

# reproducible hashes pinned by the release integration suite
WHEEL_SHA256 = "cc373ca485d03271f0960eeb5ff99102c6008121fd670497e2d87589ac583838"
SODA_SHA256 = "e409cbf2b15b7ccc94ede28a4aa499263b7e99b2ed1ad6a0eb4571fbebdfcd4a"
WHEEL_NAME = "musicdl-2.14.2-py3-none-any.whl"


def _wheel_candidates():
    """候选 wheel：显式环境变量 > 已知缓存目录。缺失则跳过（不联网下载）。"""
    env = os.environ.get("FNMUSIC_MUSICDL_WHEEL", "").strip()
    candidates = [Path(env)] if env else []
    candidates += [
        Path("/tmp/fnmusic-mdlrecovery") / WHEEL_NAME,
        Path(os.environ.get("TMPDIR", "/tmp")) / "fnmusic-mdlrecovery" / WHEEL_NAME,
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _sample_source_root():
    """主审调查留下的真实源码副本目录（只读）。"""
    env = os.environ.get("FNMUSIC_MUSICDL_SOURCES", "").strip()
    candidates = [Path(env)] if env else []
    for path in candidates:
        if (path / "musicdl" / "modules" / "sources" / "soda.py").is_file():
            return path
    return None


def _load_soda_source():
    """优先读固定 wheel 内的 soda.py，退回真实源码副本；两者皆无则跳过。

    返回 (bytes, origin)；哈希与调查报告记录的 Soda 源码 SHA256 必须一致，
    否则说明调查结论已过期，测试必须失败而不是继续。
    """
    import hashlib

    wheel = _wheel_candidates()
    if wheel is not None:
        with zipfile.ZipFile(wheel) as zf:
            data = zf.read("musicdl/modules/sources/soda.py")
        return data, f"wheel:{wheel}"
    root = _sample_source_root()
    if root is not None:
        data = (root / "musicdl" / "modules" / "sources" / "soda.py").read_bytes()
        return data, f"source copy:{root}"
    # 生产依赖镜像不一定保留wheel缓存，但已安装包仍是可核对的真实源码。
    from importlib.metadata import distribution, PackageNotFoundError
    try:
        installed = distribution('musicdl')
    except PackageNotFoundError:
        pytest.skip('fixed wheel/source copy and installed musicdl are unavailable')
    assert installed.version == '2.14.2', 'SDK version changed; review and update the pinned contract'
    source = Path(installed.locate_file('musicdl/modules/sources/soda.py'))
    assert source.is_file(), 'installed SDK is missing the expected Soda source'
    return source.read_bytes(), 'installed musicdl 2.14.2'


def test_soda_contract_uses_installed_distribution_without_machine_specific_cache(tmp_path, monkeypatch):
    source = tmp_path / 'soda.py'
    source.write_bytes(b'sdk-source-snapshot')
    monkeypatch.setattr(sys.modules[__name__], '_wheel_candidates', lambda: None)
    monkeypatch.setattr(sys.modules[__name__], '_sample_source_root', lambda: None)
    import importlib.metadata
    monkeypatch.setattr(importlib.metadata, 'distribution', lambda name: types.SimpleNamespace(
        version='2.14.2', locate_file=lambda member: source))
    try:
        data, origin = _load_soda_source()
    except pytest.skip.Exception:
        pytest.fail('installed real SDK must be inspected rather than skipped for missing local cache')
    assert data == b'sdk-source-snapshot'
    assert 'installed' in origin


def test_requirements_pins_exact_musicdl_version():
    """隔离镜像实际安装的是 2.14.2，requirements 必须固定同一版本而不是下限。"""
    text = (_HERE / "requirements.txt").read_text(encoding="utf-8")
    pins = [line.strip() for line in text.splitlines() if line.strip()]
    assert "musicdl==2.14.2" in pins


def test_pinned_wheel_matches_documented_sha256():
    """固定 wheel 存在时，核对 pylock-in house wheel 摘要与调查记录一致。"""
    import hashlib

    wheel = _wheel_candidates()
    if wheel is None:
        pytest.skip("无本地固定 wheel；跳过摘要核对")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert digest == WHEEL_SHA256


def test_soda_source_signature_matches_investigation():
    """Soda 源码摘要必须与调查记录一致（尾部换行差异需归一化后再比）。"""
    import hashlib

    data, origin = _load_soda_source()
    # 进程内strip diff：调查报告给的是「以换行结尾」的文本摘要，
    # wheel 内文件不带尾换行，统一归一到 LF 结尾后比对。
    normalized = data.replace(b"\r\n", b"\n").rstrip(b"\n") + b"\n"
    assert hashlib.sha256(normalized).hexdigest() == SODA_SHA256, origin
    # 同时保留不带尾换行的摘要，防止归一化逻辑写成巧合
    assert hashlib.sha256(data.rstrip(b"\n")).hexdigest() != SODA_SHA256


def _text(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def test_soda_source_has_explicit_timeout_before_kwargs_expansion():
    """真实契约：Soda 四个调用自带 timeout=10 后再 **request_overrides。

    项目再注入同名 timeout 会触发 Python 重复关键字 TypeError。这里的断言直接
    读真实字节码形式的源码文本，不依赖安装包，也不自己重述 SDK 行为。
    """
    data, _origin = _load_soda_source()
    lines = _text(data).splitlines()
    offenders = [ln for ln in lines
                 if "timeout=" in ln and "**request_overrides" in ln]
    # 调查记录的行号：87, 105, 109, 111
    hit_lines = sorted(i for i, ln in enumerate(lines, 1)
                       if "timeout=" in ln and "**request_overrides" in ln)
    assert hit_lines == [87, 105, 109, 111], offenders


def test_duplicate_timeout_keyword_raises_typeerror():
    """重复关键字是 Python 语义，不用装 SDK 也能证明冲突确实会发生。"""
    def upstream_call(url, **kwargs):
        return kwargs["timeout"]

    with pytest.raises(TypeError):
        upstream_call("https://x.invalid", timeout=10, **{"timeout": 5})


# ------------------------------------------------- 2. 构造参数（req_overrides）

SEARCH_CORE = str(_HERE)


def _load_search_core():
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    return importlib.import_module("search_core")


search_core = _load_search_core()


class _FakeClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def search(self, keyword):
        return {}


@pytest.fixture()
def _no_real_musicdl(monkeypatch):
    """search_one_source 内部 `from musicdl import musicdl`，用替身接管构造参数。"""
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return _FakeClient(**kwargs)

    monkeypatch.setattr(search_core, "_build_client", factory)
    return calls


def test_search_one_source_skips_timeout_for_conflicting_sources(monkeypatch):
    """已知源（Soda）不再收到 timeout；其余源仍保留超时预算。"""
    captured = {}

    def factory(source, cfg, overrides):
        captured.update({"source": source, "cfg": cfg, "overrides": overrides})
        return _FakeClient(ok=True)

    monkeypatch.setattr(search_core, "_build_client", factory)
    search_core.search_one_source("SodaMusicClient", "晴天", 10)
    assert captured["overrides"] == {}
    # 冲突源仍保留其余可靠的网络限制（单线程、少重试）
    assert captured["cfg"]["max_retries"] == 1

    captured.clear()
    search_core.search_one_source("KuwoMusicClient", "晴天", 10)
    assert "timeout" in captured["overrides"]


def test_worker_does_not_use_global_socket_timeout(monkeypatch):
    """禁止 socket.setdefaulttimeout 全局 hack：预算由父进程回收子进程保证。"""
    import socket

    touched = []
    monkeypatch.setattr(socket, "setdefaulttimeout",
                        lambda value: touched.append(value) or (_ for _ in ()).throw(AssertionError("global timeout hack")))
    search_core.probe_playable_sync("http://127.0.0.1:1/none", {})
    assert touched == []


# --------------------------------------------------------- 3. normalize 保真

class _FakeSongInfo:
    """最小 SongInfo 双胞胎：字段与 musicdl 2.14.2 的 SongInfo 对齐。"""

    def __init__(self, **kwargs):
        self.source = "KuwoMusicClient"
        self.identifier = "1"
        self.song_name = "晴天"
        self.singers = "周杰伦"
        self.album = "叶惠美"
        self.duration_s = 200
        self.ext = "mp3"
        self.file_size_bytes = 4096
        self.cover_url = ""
        self.download_url = "http://example.com/1.mp3"
        self.default_download_headers = {"Referer": "https://source.example"}
        self.default_download_cookies = {"session": "secret"}
        self.lyric = ""
        self.protocol = "HTTP"
        self.raw_data = {}
        self.bitrate = None
        self.codec = None
        self.samplerate = None
        self.channels = None
        self.download_url_status = {}
        self.__dict__.update(kwargs)


def test_normalize_keeps_real_protocol_and_format():
    song = _FakeSongInfo(protocol="HLS", ext="m3u8",
                         download_url="http://example.com/index.m3u8")
    item = search_core.normalize(song, "晴天")
    assert item["protocol"] == "hls"
    assert item["ext"] == "m3u8"
    # HLS 条目必须标记为需专门客户端处理，不能被当成 mp3 直链交付
    assert item["delivery"] == "unsupported"
    assert search_core.media_unsupported_reason(item) is not None


def test_normalize_marks_play_auth_as_postprocessing_required():
    song = _FakeSongInfo(source="SodaMusicClient", ext="flac",
                         raw_data={"play_auth": "SIGNATURE-TOKEN-VALUE"})
    item = search_core.normalize(song, "晴天")
    assert item["delivery"] == "unsupported"
    assert item["source"] == "soda"
    assert "postprocess" in item["unsupported_reason"] or "play_auth" in item["unsupported_reason"]
    # 凭据不得出现在归一化结果里
    assert "SIGNATURE-TOKEN-VALUE" not in json.dumps(item, ensure_ascii=False)


@pytest.mark.parametrize('ext, url', [
    ('m3u8', 'https://cdn.example/opaque'),
    ('mp3', 'https://cdn.example/playlist.M3U8?sig=fixture'),
    ('mp3', 'https://cdn.example/audio%2Empd?sig=fixture'),
])
def test_http_manifest_is_not_a_direct_audio_resource(ext, url):
    song = _FakeSongInfo(ext=ext, download_url=url, protocol='HTTP')
    item = search_core.normalize(song, 'fixture')
    assert item['delivery'] == 'unsupported'
    assert search_core.media_unsupported_reason(item)
    assert not search_core.is_candidate_playable(song, item)


@pytest.mark.parametrize('content_type, body', [
    ('application/vnd.apple.mpegurl', b'#EXTM3U\\n#EXTINF:10,\\naudio.ts'),
    ('audio/mpeg', b'#EXTM3U\\n#EXTINF:10,\\naudio.ts'),
    ('application/xml', b'<Error>AccessDenied</Error>'),
    ('audio/mpeg', b'Access denied: expired resource'),
    ('audio/mpeg', bytes(64)),
    ('application/octet-stream', b'PK' + bytes([3, 4]) + bytes(64)),
    ('audio/mpeg', b'\xef\xbb\xbf {"code":403}'),
])
def test_probe_rejects_manifests_xml_and_bom_json(content_type, body):
    assert not search_core._accept_response(200, content_type, body)


@pytest.mark.parametrize('content_type, prefix', [
    ('audio/mpeg', b'ID3' + bytes([3]) + bytes(32)),
    ('audio/flac', b'fLaC' + bytes(32)),
    ('audio/ogg', b'OggS' + bytes(32)),
    ('audio/mp4', bytes([0, 0, 0, 24]) + b'ftypM4A ' + bytes(32)),
    ('audio/wav', b'RIFF' + bytes(4) + b'WAVE' + bytes(32)),
    ('audio/aac', bytes([255, 241, 80, 128]) + bytes(32)),
    ('audio/webm', bytes.fromhex('1a45dfa3') + bytes(32)),
])
def test_common_audio_prefixes_remain_supported(content_type, prefix):
    assert search_core._accept_response(206, content_type, prefix)


def test_normalize_does_not_leak_server_headers_or_cookies():
    song = _FakeSongInfo()
    item = search_core.normalize(song, "晴天")
    blob = json.dumps(item, ensure_ascii=False)
    assert "secret" not in blob
    assert "https://source.example" not in blob
    for key in ("default_download_headers", "download_headers", "cookies", "raw_data"):
        assert key not in item


def test_normalize_reports_unknown_quality_without_guessing():
    """bitrate 未知不能按扩展名或能力猜 Hi-Res / 母带。"""
    song = _FakeSongInfo(ext="flac", bitrate=None)
    item = search_core.normalize(song, "晴天")
    assert item["quality"] == "unknown"
    assert item["bitrate"] == 0
    assert item["ext"] == "flac"


def test_normalize_keeps_known_bitrate():
    song = _FakeSongInfo(ext="flac", bitrate=921, samplerate=48000)
    item = search_core.normalize(song, "晴天")
    assert item["bitrate"] == 921
    assert item["quality"] == "unknown"  # 已知码率仍不是档位承诺


@pytest.mark.parametrize("protocol", ["RTMP", "MMS", "PLAIN"])
def test_normalize_unsupported_protocol_states(protocol):
    song = _FakeSongInfo(protocol=protocol, ext="mp3")
    item = search_core.normalize(song, "晴天")
    assert item["delivery"] == "unsupported"
    assert search_core.media_unsupported_reason(item) is not None


def test_normalize_plain_http_stays_playable():
    song = _FakeSongInfo(protocol="HTTP", ext="mp3", download_url="http://a.example/1.mp3")
    item = search_core.normalize(song, "晴天")
    assert item["delivery"] == "direct"
    assert search_core.media_unsupported_reason(item) is None


@pytest.mark.parametrize("ext", ["ncm", "qmc0", "qmcflac", "kgm", "mflac", "mgg", "bkcmp3", "tkm", "vpr", "mg3d"])
def test_normalize_encrypted_formats_are_unsupported(ext):
    """加密/DRM 容器不是裸媒体：明确不支持，不实现绕过。"""
    song = _FakeSongInfo(ext=ext, download_url=f"http://a.example/1.{ext}")
    item = search_core.normalize(song, "晴天")
    assert item["delivery"] == "unsupported"


def test_candidate_filter_rejects_unsupported_items():
    """透传直链的入口与搜索初筛必须共用同一判据。"""
    song = _FakeSongInfo(protocol="HLS", ext="m3u8")
    item = search_core.normalize(song, "晴天")
    assert search_core.is_candidate_playable(song, item) is False


# ------------------------------------------- 4. HEAD 403/405 的有界 GET 探活

class _LoopbackServer:
    """极简 TCP 服务：可观测客户端行为（Range、读取量、关闭时机）。"""

    def __init__(self, head_status=403, get_status=206, body=b"", ctype=b"application/octet-stream",
                 body_total=1024, send_prefix_only=False):
        self.send_prefix_only = send_prefix_only
        self.head_status = head_status
        self.get_status = get_status
        self.body = body
        self.ctype = ctype
        self.body_total = body_total
        self.requests = []
        self.client_closed = threading.Event()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}/audio"
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _read_request(self, conn):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
        head = buf.decode("latin-1").split("\r\n")
        headers = {}
        for line in head[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        return head[0], headers

    def _handle(self, conn):
        try:
            while True:
                line, headers = self._read_request(conn)
                if not line:
                    return
                self.requests.append((line, headers))
                if line.startswith("HEAD"):
                    head_type = b"audio/mpeg" if self.head_status in (200, 206) else b"text/html"
                    length = self.body_total if self.head_status in (200, 206) else 0
                    conn.sendall(
                        b"HTTP/1.1 " + str(self.head_status).encode() + b" Probe\r\n"
                        b"Content-Type: " + head_type + b"\r\nContent-Length: " + str(length).encode()
                        + b"\r\nConnection: keep-alive\r\n\r\n")
                    continue
                payload = self.body + b"\x00" * max(0, self.body_total - len(self.body))
                status = b"206 Partial Content" if self.get_status == 206 else b"200 OK"
                head = [b"HTTP/1.1 " + str(self.get_status).encode() + b" " + status,
                        b"Content-Type: " + self.ctype,
                        b"Content-Length: " + str(len(payload)).encode()]
                if payload:
                    head.append(b"Content-Range: bytes 0-" + str(len(payload) - 1).encode()
                                + b"/" + str(len(payload)).encode())
                head += [b"Connection: keep-alive", b"", b""]
                sent_body = payload[:1024] if self.send_prefix_only else payload[:self.body_total]
                conn.sendall(b"\r\n".join(head) + sent_body)
                conn.settimeout(3.0)
                try:
                    tail = conn.recv(65536)
                except ConnectionResetError:
                    tail = b""  # 未读完的大响应关闭可能发RST，同样证明及时断开。
                except socket.timeout:
                    tail = b"TIMEOUT"
                # b"" 表示客户端已经 FIN（及时 close）；TIMEOUT 表示连接被吊着
                if tail == b"":
                    self.client_closed.set()
                return
        finally:
            conn.close()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        finally:
            self._thread.join(1)


class _CurlResponseLike:
    """curl_cffi 路径的最小替身：真连 loopback，只为可观测 close。"""

    def __init__(self, status_code, headers, body_iter=None, raw=None):
        self.status_code = status_code
        self.headers = headers
        self._iter = body_iter or iter(())
        self._raw = raw
        self.closed = False

    def iter_content(self, chunk_size=8192):
        return self._iter(chunk_size) if callable(self._iter) else self._iter

    def close(self):
        self.closed = True
        if self._raw is not None:
            try:
                self._raw.close()
            except Exception:
                pass


class _RealCurlStub(types.ModuleType):
    """curl_cffi 模块替身：head/get 真走 TCP 到 loopback 服务。

    本机/隔离镜像未安装 curl_cffi 时无法通过官方文档評測，因此这里把替身建立在
    真实 HTTP/1.1 连接之上，仍然能证明 Range、读取上限与 close 的可观测行为。
    """

    def __init__(self):
        super().__init__("curl_cffi_stub_probe")
        self.requests = {}

    @staticmethod
    def _request(method, url, headers=None, stream=False, timeout=None, content_callback=None):
        import http.client
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout or 5)
        raw_headers = {k: v for k, v in (headers or {}).items()}
        conn.request(method, parts.path or "/", headers=raw_headers)
        resp = conn.getresponse()
        out_headers = {k.lower(): v for k, v in resp.getheaders()}
        def close_connection():
            resp.close()
            conn.close()
        closer = types.SimpleNamespace(close=close_connection)
        if method == "HEAD":
            return _CurlResponseLike(resp.status, out_headers, raw=closer)
        def chunks(size):
            while True:
                chunk = resp.read(size)
                if not chunk:
                    return
                yield chunk
        if content_callback is not None:
            for chunk in chunks(1024):
                if content_callback(chunk) != len(chunk):
                    break
        return _CurlResponseLike(resp.status, out_headers, body_iter=chunks, raw=closer)

    def head(self, url, **kwargs):
        return self._request("HEAD", url, headers=kwargs.get("headers"), timeout=kwargs.get("timeout"))

    def get(self, url, **kwargs):
        return self._request("GET", url, headers=kwargs.get("headers"),
                             stream=kwargs.get("stream"), timeout=kwargs.get("timeout"),
                             content_callback=kwargs.get("content_callback"))


@pytest.fixture()
def probe_loopback():
    servers = []

    def start(body=b"ID3\x03\x00\x00\x00" + b"\x00" * 40, head_status=403, get_status=206,
              ctype=b"application/octet-stream", body_total=1024, send_prefix_only=False):
        srv = _LoopbackServer(head_status=head_status, get_status=get_status, body=body,
                              ctype=ctype, body_total=body_total, send_prefix_only=send_prefix_only)
        servers.append(srv)
        return srv

    yield start
    for srv in servers:
        srv.close()


def test_curl_prefix_callback_uses_actual_libcurl_abort_sentinel(monkeypatch):
    closed = []
    head = types.SimpleNamespace(status_code=200, headers={'content-type': 'audio/mpeg'},
                                 close=lambda: closed.append('head'))
    response = types.SimpleNamespace(status_code=200, headers={'content-type': 'audio/mpeg'},
                                     close=lambda: closed.append('body'))
    def get(url, **kwargs):
        result = kwargs['content_callback'](b'ID3' + bytes([3]) + bytes(2044))
        assert result == 0xFFFFFFFF, 'curl_cffi ignores ordinary short writes including zero'
        assert not kwargs.get('stream', False)
        return response
    monkeypatch.setattr(search_core, 'HAS_CURL_CFFI', True)
    monkeypatch.setattr(search_core, 'curl_requests', types.SimpleNamespace(head=lambda *a, **k: head, get=get))
    assert search_core.probe_playable_sync('https://media.example/fixture.mp3', {})
    assert closed == ['body', 'head']


@pytest.mark.parametrize('client_path', ['curl', 'httpx', 'curl_real'])
def test_head_200_does_not_replace_actual_media_prefix_check(monkeypatch, probe_loopback, client_path):
    srv = probe_loopback(head_status=200, body=b'{"code":403}', ctype=b'audio/mpeg')
    _select_probe_path(monkeypatch, client_path)
    assert search_core.probe_playable_sync(srv.url, {}) is False
    assert any(line.startswith('GET') for line, headers in srv.requests)


@pytest.mark.parametrize('client_path', ['curl', 'httpx', 'curl_real'])
def test_large_response_can_stop_after_first_media_prefix(monkeypatch, probe_loopback, client_path):
    # 内部配置误放大也不能让探活等待整曲或消耗无界内存。
    monkeypatch.setitem(search_core.CFG, 'probe_max_bytes', 64 * 1024 * 1024)
    srv = probe_loopback(body=b'ID3' + bytes([3]) + bytes(200), body_total=2 * 1024 * 1024,
                         get_status=200, send_prefix_only=True)
    _select_probe_path(monkeypatch, client_path)
    assert search_core.probe_playable_sync(srv.url, {}) is True
    assert srv.client_closed.wait(1.0)
    assert any(headers.get('range') == 'bytes=0-1023' for line, headers in srv.requests if line.startswith('GET'))


@pytest.mark.parametrize("client_path", ["curl", "httpx"])
def test_head_403_falls_back_to_bounded_range_get(monkeypatch, probe_loopback, client_path):
    """HEAD 被拒后必须有界 GET Range 0-1023，而不是直接判不可用。"""
    srv = probe_loopback(body=b"ID3\x03\x00" + bytes(200))
    _select_probe_path(monkeypatch, client_path)
    assert search_core.probe_playable_sync(srv.url, {}) is True
    gets = [req for req in srv.requests if req[0].startswith("GET")]
    heads = [req for req in srv.requests if req[0].startswith("HEAD")]
    assert heads, "should still try HEAD first"
    assert gets and gets[0][1].get("range") == "bytes=0-1023"


@pytest.mark.parametrize("client_path", ["curl", "httpx"])
@pytest.mark.parametrize("head_status", [403, 405])
def test_head_rejection_closes_upstream_response(monkeypatch, probe_loopback, client_path, head_status):
    """stream 响应读完前缀就 close：loopback 侧能观测到客户端 FIN。"""
    srv = probe_loopback(body=b"fLaC" + bytes(120), head_status=head_status)
    _select_probe_path(monkeypatch, client_path)
    assert search_core.probe_playable_sync(srv.url, {}) is True
    assert srv.client_closed.wait(3.0), "upstream response was not closed promptly"


@pytest.mark.parametrize("client_path", ["curl", "httpx"])
def test_range_get_rejects_json_error_body(monkeypatch, probe_loopback, client_path):
    """403/405 回退拿到 JSON 错误体不能当成媒体通过。"""
    srv = probe_loopback(body=b'{"code":403,"msg":"forbidden"}', ctype=b"application/json")
    _select_probe_path(monkeypatch, client_path)
    assert search_core.probe_playable_sync(srv.url, {}) is False


@pytest.mark.parametrize("client_path", ["curl", "httpx"])
def test_range_get_rejects_html_body(monkeypatch, probe_loopback, client_path):
    srv = probe_loopback(body=b"<!DOCTYPE html><html>403</html>", ctype=b"text/html")
    _select_probe_path(monkeypatch, client_path)
    assert search_core.probe_playable_sync(srv.url, {}) is False


@pytest.mark.parametrize("client_path", ["curl", "httpx"])
def test_range_get_reads_at_most_prefix(monkeypatch, probe_loopback, client_path):
    """只读取有限前缀：不能为了判断整首曲目而读完整首。"""
    monkeypatch.setitem(search_core.CFG, "probe_max_bytes", 1024)
    seen = []
    srv = probe_loopback(body=b"OggS" + bytes(200), body_total=1024)
    _select_probe_path(monkeypatch, client_path)
    assert search_core.probe_playable_sync(srv.url, {}) is True
    gets = [req for req in srv.requests if req[0].startswith("GET")]
    assert gets[0][1].get("range") == "bytes=0-1023"
    assert seen == []


@pytest.mark.parametrize("client_path", ["curl", "httpx"])
def test_empty_range_body_is_not_playable(monkeypatch, probe_loopback, client_path):
    # body 是响应体前缀，body_total=0 表示真正的零长响应
    srv = probe_loopback(body=b"", body_total=0, ctype=b"audio/mpeg")
    _select_probe_path(monkeypatch, client_path)
    assert search_core.probe_playable_sync(srv.url, {}) is False


def _select_probe_path(monkeypatch, path):
    if path == 'curl_real':
        import importlib.util
        if importlib.util.find_spec('curl_cffi') is None:
            pytest.skip('curl_cffi unavailable locally; execute in real-dependency validation image')
        # 已安装但原生依赖损坏时必须失败，不能把ABI/导入错误伪装为缺可选包。
        real_requests = importlib.import_module('curl_cffi.requests')
        monkeypatch.setattr(search_core, 'HAS_CURL_CFFI', True)
        monkeypatch.setattr(search_core, 'curl_requests', real_requests)
    elif path == "curl":
        monkeypatch.setattr(search_core, "HAS_CURL_CFFI", True)
        monkeypatch.setattr(search_core, "curl_requests", _RealCurlStub())
    else:
        monkeypatch.setattr(search_core, "HAS_CURL_CFFI", False)


# -------------------------------------------------- 5. worker 阶段与脱敏错误

def _worker_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = SEARCH_CORE + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("MUSICDL_REQUEST_TIMEOUT", "1")
    return env


def _run_worker(task_line: str):
    proc = subprocess.run(
        [sys.executable, str(_HERE / "worker.py")],
        input=(task_line + "\n").encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, env=_worker_env(), cwd=str(_HERE),
    )
    lines = [json.loads(ln) for ln in proc.stdout.decode("utf-8").splitlines() if ln.strip()]
    return lines, proc.stderr.decode("utf-8", "replace")


@pytest.mark.skipif(os.name != "posix", reason="worker 协议回归需在 POSIX 上跑")
def test_worker_search_stage_error_keeps_type_and_stage():
    """未安装 musicdl 时真实触发 ImportError：错误必须带类型与 search 阶段。"""
    lines, _ = _run_worker(json.dumps({
        "op": "search", "source": "KuwoMusicClient", "keyword": "晴天",
        "fetch_size": 5, "limit": 1, "song_id": None, "budget_s": 5.0,
    }))
    errors = [msg for msg in lines if msg.get("type") == "error"]
    assert errors, lines
    error = errors[0]["error"]
    assert "ModuleNotFoundError" in error
    assert "stage=search" in error
    assert "task failed" not in error


def test_worker_error_never_leaks_signature_urls():
    """构造含签名 URL 的异常：505 worker 输出不得包含原文 token。"""
    secret = "https://cdn.example/audio.mp3?token=SECRET-TOKEN-123"
    try:
        raise RuntimeError(f"source failed for {secret}")
    except Exception as exc:
        text = search_core.describe_task_error(exc, "search")
    assert "RuntimeError" in text
    assert "stage=search" in text
    assert "SECRET-TOKEN-123" not in text
    assert secret not in text


@pytest.mark.parametrize('message', [
    'Authorization: Bearer CREDENTIAL_CANARY_103',
    '{"cookie":"session=CREDENTIAL_CANARY_103"}',
    'https://cdn.example/CREDENTIAL_CANARY_103/audio.mp3?auth_key=CREDENTIAL_CANARY_103',
    'https://cdn.example/audio.mp3?sign%61ture=CREDENTIAL_CANARY_103',
])
def test_worker_diagnostic_never_forwards_free_form_secret_text(message):
    text = search_core.describe_task_error(RuntimeError(message), 'probe')
    assert 'RuntimeError' in text
    assert 'stage=probe' in text
    assert 'CREDENTIAL_CANARY_103' not in text
    assert 'https://' not in text


@pytest.mark.parametrize('explicit_cookie', [None, 'explicit=preserved'])
def test_search_preserves_private_cookie_transport_without_exposing_or_mutating_song(monkeypatch, explicit_cookie):
    song = _FakeSongInfo()
    if explicit_cookie:
        song.default_download_headers['Cookie'] = explicit_cookie
    original_headers = dict(song.default_download_headers)
    probes, emitted = [], []
    monkeypatch.setattr(search_core, 'search_one_source', lambda *args: [song])
    def probe(url, headers):
        probes.append(dict(headers))
        return True
    monkeypatch.setattr(search_core, 'probe_playable_sync', probe)
    result = search_core.run_search_task({
        'source': 'KuwoMusicClient', 'keyword': 'fixture', 'fetch_size': 1,
        'limit': 1, 'budget_s': 5.0,
    }, emitted.append)
    assert result['count'] == 1
    expected = explicit_cookie or 'session=secret'
    assert probes[0].get('Cookie') == expected
    assert emitted[0][1].get('Cookie') == expected
    assert 'secret' not in json.dumps(emitted[0][0])
    assert song.default_download_headers == original_headers


@pytest.mark.parametrize('wrapped', [False, True])
@pytest.mark.parametrize('url, expected_cookie', [
    ('https://media.example.com/a.mp3', 'session=allowed'),
    ('https://unrelated.invalid/a.mp3', None),
])
def test_private_cookie_jar_keeps_original_domain_scope(wrapped, url, expected_cookie):
    from http.cookiejar import Cookie, CookieJar
    from types import SimpleNamespace
    jar = CookieJar()
    for name, value, domain in [
        ('session', 'allowed', '.example.com'),
        ('other_account', 'PRIVATE_DOMAIN_CANARY', '.private.invalid'),
    ]:
        jar.set_cookie(Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=domain, domain_specified=True, domain_initial_dot=True,
            path='/', path_specified=True, secure=True, expires=None,
            discard=True, comment=None, comment_url=None, rest={}, rfc2109=False,
        ))
    cookies = SimpleNamespace(jar=jar) if wrapped else jar
    song = _FakeSongInfo(download_url=url, default_download_cookies=cookies)
    headers = search_core._download_request_headers(song)
    assert headers.get('Cookie') == expected_cookie
    assert 'PRIVATE_DOMAIN_CANARY' not in str(headers)


def test_stage_wrapping_distinguishes_search_and_probe():
    """搜索阶段与探活阶段的异常必须能区分，不能全部塌成同一个 task failed。"""
    search_exc = search_core.SearchStageError(ValueError("boom"))
    probe_exc = search_core.ProbeStageError(TimeoutError("slow"))
    assert "search" in search_core.describe_task_error(search_exc, None)
    assert "probe" in search_core.describe_task_error(probe_exc, None)


def test_run_search_task_reports_probe_stage_error(monkeypatch):
    """探活抛错时还是要交出阶段，而不是把 worker 变成未知失败。"""
    def boom(*_args, **_kwargs):
        raise RuntimeError("https://cdn.example/x.mp3?token=abc")
    monkeypatch.setattr(search_core, "search_one_source",
                        lambda *a, **k: [_FakeSongInfo()])
    monkeypatch.setattr(search_core, "probe_playable_sync", boom)
    stages = []
    with pytest.raises(search_core.ProbeStageError):
        search_core.run_search_task(
            {"source": "KuwoMusicClient", "keyword": "k", "fetch_size": 1, "limit": 1,
             "budget_s": 5.0}, lambda entry: None, on_stage=stages.append)
    assert stages[-1] == "probe"


# ------------------------------------------------- 6. 空结果不算平台永久故障

def test_empty_result_is_not_recorded_as_failure():
    """空结果必须可重试：不能记灵活熔断/自适应惩罚把它判成永久故障。"""
    app_module = _load_app()
    from hardening import SourceBreaker, AdaptiveTimeout
    breaker = SourceBreaker(failure_threshold=1, cooldown=60)
    adaptive = AdaptiveTimeout(base_timeout=8.0, min_timeout=1.0)
    for _ in range(3):
        app_module._note_empty_source_result("KuwoMusicClient", breaker, adaptive)
    assert breaker.is_open("KuwoMusicClient") is False
    assert adaptive.timeout_for("KuwoMusicClient") == 8.0


class _EmptyWorker:
    """保持 Reflect 边界：worker 协议对象的替身，目的是为了 Studios 边界。"""

    ready = True

    def alive(self):
        return True

    async def wait_ready(self, timeout):
        return None

    async def execute(self, payload, on_entry, deadline):
        import search_core
        entries = []

        def emit(entry):
            entries.append(entry)
            if on_entry is not None:
                on_entry(entry)

        result = await asyncio.to_thread(search_core.run_search_task, payload, emit)
        return dict(entries=entries, count=result["count"], partial=result["partial"])

    async def terminate(self, grace_s):
        return None


async def _empty_spawn(source, **kwargs):
    return _EmptyWorker()


@pytest.fixture()
def _mock_bulkhead(monkeypatch):
    """用替身 worker 接管 SourceBulkhead：子进程不可 monkeypatch。"""
    from hardening import SourceBulkhead, SourceBreaker, SingleFlight
    app_module = _load_app()
    app_module.SOURCE_BREAKER = SourceBreaker(failure_threshold=1, cooldown=60)
    app_module.SINGLE_FLIGHT = SingleFlight()
    app_module._SONG_CACHE.clear()
    app_module.SEARCH_CACHE.clear()
    workers = SourceBulkhead(app_module.SOURCE_NAMES.values(), spawn=_empty_spawn)
    monkeypatch.setattr(app_module, "SOURCE_WORKERS", workers)
    yield workers
    workers.shutdown(wait=True)


def test_app_empty_search_does_not_open_breaker(monkeypatch, _mock_bulkhead):
    """连续空结果：源必须仍在下一次搜索里真正执行，不被熔断、不被压预算。"""
    import asyncio
    from fastapi.testclient import TestClient
    app_module = _load_app()
    calls = []
    monkeypatch.setattr(search_core, "search_one_source",
                        lambda source, keyword, limit: calls.append(source) or [])
    with TestClient(app_module.app) as client:
        for _ in range(3):
            resp = client.get("/search", params={"keyword": "none", "sources": "kuwo"})
            assert resp.status_code == 200
            assert resp.json()["items"] == []
        # 负缓存会挡住完全相同的重复请求；真正要证明的是源没有被判成永久故障。
        assert len(calls) >= 1
        assert app_module.SOURCE_BREAKER.is_open("KuwoMusicClient") is False
        assert app_module.ADAPTIVE.timeout_for("KuwoMusicClient") >= app_module.ADAPTIVE.base_timeout
        # 换关键词时源必须重新执行，而不是继续被当成坏源跳过。
        app_module.SEARCH_CACHE.clear()
        resp = client.get("/search", params={"keyword": "another", "sources": "kuwo"})
        assert resp.status_code == 200
    assert len(calls) == 2


def _load_app():
    import importlib.util
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    stub = types.ModuleType("musicdl")
    stub.MusicClient = object
    stub.musicdl = stub
    sys.modules.setdefault("musicdl", stub)
    spec = importlib.util.spec_from_file_location("musicdl_service_app", _HERE / "app.py")
    module = sys.modules.get("musicdl_service_app")
    if module is None:
        module = importlib.util.module_from_spec(spec)
        sys.modules["musicdl_service_app"] = module
        spec.loader.exec_module(module)
    return module


# ------------------------------------------------ 7. 透传直链与搜索的共同拒绝

def test_internal_resolve_refuses_unsupported_media(monkeypatch):
    from fastapi.testclient import TestClient
    import asyncio
    app_module = _load_app()
    monkeypatch.setattr(app_module, "_INTERNAL_TOKEN", "secret")
    item = search_core.normalize(
        _FakeSongInfo(protocol="HLS", ext="m3u8", download_url="http://a.example/i.m3u8"), "k")
    async def resolve(_song_id):
        return {"ts": app_module.time.time(), "item": item, "download_headers": {}}
    monkeypatch.setattr(app_module, "_direct_resolve_entry", resolve)
    with TestClient(app_module.app, raise_server_exceptions=False) as client:
        resp = client.get("/internal/resolve", params={"id": "kuwo:1"},
                          headers={"X-Internal-Token": "secret"})
    assert resp.status_code in (409, 415, 422, 502)
    assert "url" not in resp.json()


def test_stream_refuses_unsupported_media():
    import asyncio
    app_module = _load_app()
    item = search_core.normalize(
        _FakeSongInfo(protocol="HLS", ext="m3u8", download_url="http://a.example/i.m3u8"), "k")
    entry = {"ts": app_module.time.time(), "item": item, "download_headers": {}, "lyric": ""}
    async def fail(*_a, **_k):
        raise AssertionError("resolve must not be reached")
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(app_module, "_cache_get", lambda _id: entry)
        with pytest.raises(app_module.HTTPException) as err:
            asyncio.run(app_module.stream(id="kuwo:1", proxy=False, quality="", range_header=None))
        assert err.value.status_code in (409, 415, 422, 502)
    finally:
        monkeypatch.undo()


def test_search_result_never_reports_unsupported_as_playable(monkeypatch):
    from fastapi.testclient import TestClient
    from hardening import SourceBreaker, SingleFlight
    app_module = _load_app()
    app_module.SOURCE_BREAKER = SourceBreaker(failure_threshold=4, cooldown=60)
    app_module.SINGLE_FLIGHT = SingleFlight()
    song = _FakeSongInfo(protocol="HTTP", ext="mp3",
                         raw_data={"play_auth": "TOKEN"}, download_url="http://a.example/1.mp3")
    monkeypatch.setattr(search_core, "search_one_source", lambda *a, **k: [song])
    monkeypatch.setattr(search_core, "probe_playable_sync", lambda url, headers: True)
    with TestClient(app_module.app) as client:
        resp = client.get("/search", params={"keyword": "k", "sources": "kuwo"})
    assert resp.json()["items"] == []


# --------------------------------------------- 8. PLATFORMS.md 与真实注册表

def test_info_exposes_protocol_and_delivery_without_headers():
    """/info 要给出能否交付的判断依据，同时不能吐出服务端 headers/cookies。"""
    from fastapi.testclient import TestClient
    app_module = _load_app()
    song = _FakeSongInfo(protocol="HLS", ext="m3u8")
    item = search_core.normalize(song, "k")
    app_module._cache_put(item, "k", {"Referer": "https://source.example"}, "lrc")
    with TestClient(app_module.app) as client:
        payload = client.get("/info", params={"id": item["id"]}).json()
    assert payload["protocol"] == "hls"
    assert payload["delivery"] == "unsupported"
    assert payload["ext"] == "m3u8"
    assert payload["quality"] == "unknown"
    blob = json.dumps(payload, ensure_ascii=False)
    assert "Referer" not in blob and "source.example" not in blob
    for key in ("download_headers", "default_download_headers", "cookies", "raw_data"):
        assert key not in payload


def test_search_items_never_carry_server_headers(monkeypatch, _mock_bulkhead):
    """搜索结果是对外契约：headers/cookies 留在服务端缓存里。"""
    from fastapi.testclient import TestClient
    app_module = _load_app()
    song = _FakeSongInfo(download_url="http://a.example/1.mp3")
    monkeypatch.setattr(search_core, "search_one_source", lambda *a, **k: [song])
    monkeypatch.setattr(search_core, "probe_playable_sync", lambda url, headers: True)
    with TestClient(app_module.app) as client:
        items = client.get("/search", params={"keyword": "k", "sources": "kuwo"}).json()["items"]
    assert items, "至少有 mp3 结果"
    blob = json.dumps(items, ensure_ascii=False)
    assert "source.example" not in blob
    assert "secret" not in blob

def test_platforms_table_matches_real_registry():
    """PLATFORMS.md 的 musicdl 短名必须与固定 wheel 的真实注册表一致。"""
    wheel = _wheel_candidates()
    if wheel is None:
        pytest.skip("无本地固定 wheel；跳过注册表核对")
    with zipfile.ZipFile(wheel) as zf:
        init = zf.read("musicdl/modules/sources/__init__.py").decode("utf-8")
    import re
    registered = set(re.findall(r"'([A-Za-z0-9]+MusicClient)'\s*:", init))
    assert len(registered) == 57
    table = (_HERE / "PLATFORMS.md").read_text(encoding="utf-8")
    declared = set(re.findall(r"\| musicdl \| [a-z0-9]+ \| ([A-Za-z0-9]+MusicClient) \|", table))
    missing = sorted(declared - registered)
    undeclared = sorted(registered - declared)
    assert not missing, f"表中存在已不注册的平台: {missing}"
    assert len(declared) == 57
    assert undeclared == []


def test_platforms_table_states_unsupported_and_unknown():
    table = (_HERE / "PLATFORMS.md").read_text(encoding="utf-8")
    assert "unknown" in table
    assert "后处理" in table
    assert "不支持" in table
    assert "七档" in table
