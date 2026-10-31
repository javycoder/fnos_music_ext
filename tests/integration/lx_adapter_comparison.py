#!/usr/bin/env python3
"""tests/integration/lx_adapter_comparison.py
洛雪原生链接 vs 项目适配流程对比 + 有界前缀解码的独立辅助模块。

用途：
    现有 tests/integration/lx_source_matrix.py 只验证真实 lxserver 原生 URL 与
    1KiB 前缀，报告明确 decode/adapter 未测。本模块补齐两件事，但仍是候选级证据：
    1. 真实调用当前 lxmusic-service/app.py 的 app._attempt_pinned（内部走
       LXSERVER.get_music_url + app.probe_url），得到「项目适配」结果；
    2. 对同一上游直链取得 <=64KiB 媒体前缀，用 ffmpeg pipe 解码最多 0.25 秒
       单声道 16kHz s16le PCM，作为「前缀可解码」证据。

安全边界：
    - 原生 API 客户端只允许调用者传入的精确 loopback 管理 origin；媒体访问
      完全另走 PinnedMediaTransport，绝不把普通 HTTPX 放宽到 127/私网。
    - 媒体目标只允许公共 IPv4：AF_INET 解析一次、逐个检查 is_global、TCP
      固定到该地址；HTTPS 仍用原始 hostname 做 SNI 与证书校验；禁用环境代理；
      拒绝 userinfo/私网/回环/元数据/IPv6；重定向逐跳重新检查并有上限。
    - 报告不记录 URL、signed query、Cookie/Authorization、管理口令或错误正文。
    - 解码只走 pipe 协议，硬超时 <=2 秒且不超过剩余 deadline；取消时终止并
      回收进程组；stderr 只保留受控分类与字节数。

质量语义（不要越界解读）：
    - lxserver 的 type 只是请求回显；app 已用 resolution_quality 区分。实际质量
      没有实测证据，始终记录 unknown。
    - 原生可取链但项目拒绝「有损冒充 lossless」属于正确拒绝，只标
      candidate_disposition=adapter_honest_reject，不判项目缺陷、不判整源失效。
    - prefix_decode 只说明前缀能解出 PCM，不宣称客户端播放成功或整曲可播。
    - 单条候选失败不产生 source_verdict；调用方必须继续其它候选/档位。

当前状态：
    本模块尚未接入 lx_source_matrix CLI，由 worker 显式传入源码快照路径、已上传
    的严格 sourceId、原平台、SongInfo、目标档和同一个剩余 deadline 后调用
    compare_candidate。测试替身只允许出现在原生 API 边界与本机拨号注入点。
"""

from __future__ import annotations

import asyncio
import atexit
from dataclasses import dataclass
import hashlib
import http.client
import importlib.util
import ipaddress
import logging
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import socket
import ssl
import sys
import tempfile
import threading
import time
from types import ModuleType
from typing import Any, Callable, Mapping, Sequence
import urllib.parse

import httpx

# ----------------------------------------------------------------- 常量 --

MAX_PREFIX_BYTES = 64 * 1024          # 只接收且只保留 64KiB 媒体前缀
MAX_REDIRECTS = 3                     # 媒体重定向最多跟随 3 次
MEDIA_HOP_TIMEOUT_S = 12.0            # 单跳媒体请求上限，仍受总 deadline 约束
MIN_PREFIX_BYTES = 256                # 低于该长度不足以尝试解码
MAX_PCM_BYTES = 8192                  # 0.25s * 16000Hz * 单声道 * 2 字节
PCM_SAMPLE_RATE = 16000
PCM_CHANNELS = 1
DECODE_MAX_SECONDS = 0.25
DECODE_HARD_TIMEOUT_S = 2.0
MIN_DECODE_BUDGET_S = 0.3
MAX_DECODE_STDERR_BYTES = 4096
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

SUPPORTED_PLATFORMS = ("kw", "kg", "tx", "wy", "mg")

# 允许从调用方透传到媒体请求的头；Cookie/Authorization/Host 一律丢弃。
_SAFE_REQUEST_HEADERS = ("user-agent", "referer")

# 允许暴露给 app.probe_url 的真实响应头，保留状态判定所需信息。
logger = logging.getLogger("fnmusic.lx_adapter_comparison")


# ----------------------------------------------------------------- 异常 --

class ComparisonConfigError(ValueError):
    """调用参数不可信/不完整；抛出即表示没有执行任何网络访问。"""


class MediaBlocked(RuntimeError):
    """媒体目标被安全策略拒绝；reason 是受控枚举，不含 URL。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class MediaFetchFailed(RuntimeError):
    """媒体读取在传输层失败（超时/连接/TLS/协议）；reason 是受控枚举。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


# ----------------------------------------------------------------- 数据 --

@dataclass(frozen=True)
class MediaResponse:
    """一次受限媒体读取的结果；prefix 可能有界截断，状态是服务器真实状态。"""

    status: int
    content_type: str
    content_encoding: str
    content_length: int | None
    content_range: str
    prefix: bytes
    hops: int
    media_host: str

    def pass_headers(self) -> dict[str, str]:
        """只把 app 探活需要的真实响应头交给 HTTPX。"""
        headers: dict[str, str] = {}
        if self.content_type:
            headers["content-type"] = self.content_type
        if self.content_encoding:
            headers["content-encoding"] = self.content_encoding
        if self.content_length is not None:
            headers["content-length"] = str(self.content_length)
        if self.content_range:
            headers["content-range"] = self.content_range
        return headers

    def evidence(self) -> dict[str, Any]:
        """报告证据：不含 URL、路径、query、Cookie 或错误正文。"""
        return {
            "status": self.status,
            "content_type": self.content_type,
            "content_encoding": self.content_encoding,
            "content_length": self.content_length,
            "content_range": self.content_range,
            "prefix_bytes": len(self.prefix),
            "prefix_sha256": hashlib.sha256(self.prefix).hexdigest(),
            "prefix_sha256_12": hashlib.sha256(self.prefix).hexdigest()[:12],
            "hops": self.hops,
            "media_host": self.media_host,
        }


@dataclass(frozen=True)
class CandidateComparisonRequest:
    """一次候选级对比所需输入；所有步骤共用同一个绝对 deadline。"""

    app_source_path: str
    management_origin: str
    admin_password: str
    source_id: str
    platform: str
    song_info: dict
    quality: str
    deadline: float
    app_sha256: str = ""                                  # 可选：可信快照哈希
    ffmpeg_path: str | None = None                        # 缺省走 PATH 查找
    resolver: Callable[[str, int], Sequence[str]] | None = None  # 仅测试注入
    dial: Callable[[tuple[str, int], float], socket.socket] | None = None  # 仅测试注入
    ssl_context: ssl.SSLContext | None = None             # 仅测试注入自签 CA


# --------------------------------------------------------- URL 与地址校验 --

def _parse_media_url(url: str) -> tuple[str, str, int, str]:
    """解析媒体 URL；返回 (scheme, host, port, request-target)。"""
    try:
        parts = urllib.parse.urlsplit(str(url).strip())
    except ValueError as exc:
        raise MediaBlocked("invalid_url") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise MediaBlocked("unsupported_scheme")
    if parts.username is not None or parts.password is not None:
        raise MediaBlocked("userinfo_rejected")
    host = parts.hostname
    if not host:
        raise MediaBlocked("missing_host")
    # urlsplit 会剥掉方括号；hostname 里还有冒号就说明是 IPv6 字面量。
    if ":" in host:
        raise MediaBlocked("ipv6_rejected")
    try:
        port = parts.port
    except ValueError as exc:
        raise MediaBlocked("invalid_port") from exc
    if port is None:
        port = 443 if scheme == "https" else 80
    if not 0 < port < 65536:
        raise MediaBlocked("invalid_port")
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return scheme, host, port, target


def _is_public_ipv4(text: str) -> bool:
    """只认全局可路由 IPv4；回环/私网/链路本地/元数据/保留网段全部拒绝。"""
    try:
        address = ipaddress.ip_address(str(text).strip())
    except ValueError:
        return False
    if address.version != 4:
        return False
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return False
    return bool(address.is_global)


def _remaining(deadline: float) -> float:
    return float(deadline) - time.monotonic()


def _require_remaining(deadline: float) -> float:
    left = _remaining(deadline)
    if left <= 0:
        raise MediaFetchFailed("deadline_exhausted")
    return left


def _system_resolve_ipv4(host: str, port: int, deadline: float) -> list[str]:
    """系统 DNS 只在守护线程里跑，且被剩余 deadline 上限约束。"""
    left = _require_remaining(deadline)
    result: queue.Queue = queue.Queue(maxsize=1)

    def lookup():
        try:
            infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
            result.put((True, [item[4][0] for item in infos]))
        except Exception as exc:  # noqa: BLE001 - 只回传类型，不记录原文
            result.put((False, exc))

    threading.Thread(target=lookup, daemon=True).start()
    try:
        ok, value = result.get(timeout=left)
    except queue.Empty as exc:
        raise MediaFetchFailed("dns_timeout") from exc
    _require_remaining(deadline)
    if not ok:
        raise MediaFetchFailed("dns_failed")
    return list(value)


def _resolve_public_ipv4(
    host: str,
    port: int,
    deadline: float,
    resolver: Callable[[str, int], Sequence[str]] | None,
) -> str:
    """解析域名并在拨号前逐个校验；返回要固定到的唯一 IPv4 字面量。"""
    if resolver is None:
        addresses = _system_resolve_ipv4(host, port, deadline)
    else:
        _require_remaining(deadline)
        try:
            addresses = list(resolver(host, port))
        except MediaBlocked:
            raise
        except Exception as exc:  # noqa: BLE001 - 注入 resolver 的失败也只留类型
            raise MediaFetchFailed("dns_failed") from exc
    if not addresses:
        raise MediaFetchFailed("dns_empty")
    pinned = ""
    for raw in addresses:
        text = str(raw).strip()
        if not _is_public_ipv4(text):
            raise MediaBlocked("private_or_reserved_ip")
        if not pinned:
            pinned = text
    if not pinned:
        raise MediaFetchFailed("dns_empty")
    return pinned


def _dial_checked(
    dial: Callable[[tuple[str, int], float], socket.socket] | None,
    pinned_ip: str,
    port: int,
    timeout: float,
) -> socket.socket:
    """拨号只接受已校验的 IPv4 字面量；生产路径额外核对真实对端地址。"""
    if dial is not None:
        # 测试注入点：只允许离线用例把 TCP 改拨本机服务，不能进入生产路径。
        return dial((pinned_ip, port), timeout)
    try:
        sock = socket.create_connection((pinned_ip, port), timeout=timeout)
    except TimeoutError as exc:
        raise MediaFetchFailed("socket_timeout") from exc
    except OSError as exc:
        raise MediaFetchFailed("connection_failed") from exc
    try:
        peer = sock.getpeername()
    except OSError as exc:
        sock.close()
        raise MediaFetchFailed("connection_failed") from exc
    if str(peer[0]) != pinned_ip:
        sock.close()
        # 解析校验过的地址与实际 TCP 对端不一致，说明连接没有被固定。
        raise MediaBlocked("peer_mismatch")
    return sock


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Host 头保留原域名，TCP 只连已校验的 IPv4。"""

    def __init__(self, host: str, port: int, *, connect_ip: str, dial, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._connect_ip = connect_ip
        self._dial = dial
        # http.client 在 __init__ 里把 socket.create_connection 设为实例属性，
        # 必须显式改回本连接，否则会再次做系统解析。
        self._create_connection = self._pinned_create_connection

    def _pinned_create_connection(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        value = self.timeout if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout
        return _dial_checked(self._dial, self._connect_ip, self.port, float(value))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS 用 self.host 做 SNI/证书校验，TCP 仍固定到已校验 IPv4。"""

    def __init__(self, host: str, port: int, *, connect_ip: str, dial, timeout: float, context: ssl.SSLContext):
        super().__init__(host, port, timeout=timeout, context=context)
        self._connect_ip = connect_ip
        self._dial = dial
        self._create_connection = self._pinned_create_connection

    def _pinned_create_connection(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        value = self.timeout if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout
        return _dial_checked(self._dial, self._connect_ip, self.port, float(value))


def _request_headers(extra: Mapping[str, str] | None, max_prefix_bytes: int) -> dict[str, str]:
    """构造媒体请求头：Range 由本模块固定，只透传 UA/Referer。"""
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept-Encoding": "identity",
        "Range": f"bytes=0-{max_prefix_bytes - 1}",
        "Connection": "close",
    }
    for key, value in (extra or {}).items():
        lowered = str(key).lower()
        if lowered not in _SAFE_REQUEST_HEADERS or value is None:
            continue
        canonical = "User-Agent" if lowered == "user-agent" else "Referer"
        headers[canonical] = str(value)[:2048]
    return headers


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def fetch_media_prefix(
    url: str,
    *,
    deadline: float,
    headers: Mapping[str, str] | None = None,
    max_prefix_bytes: int = MAX_PREFIX_BYTES,
    max_redirects: int = MAX_REDIRECTS,
    resolver: Callable[[str, int], Sequence[str]] | None = None,
    dial: Callable[[tuple[str, int], float], socket.socket] | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> MediaResponse:
    """受限读取媒体前缀：公共 IPv4 + 固定 TCP + 逐跳重定向 + 总 deadline。

    非 2xx 也返回真实状态和必要响应头（prefix 为空），让上层自己判定；传输层
    失败抛 MediaFetchFailed，安全策略拒绝抛 MediaBlocked。
    """
    if not isinstance(max_prefix_bytes, int) or isinstance(max_prefix_bytes, bool):
        raise ValueError("max_prefix_bytes 必须是整数")
    if not 1 <= max_prefix_bytes <= MAX_PREFIX_BYTES:
        raise ValueError("max_prefix_bytes 必须在 1..65536 之间")
    if not isinstance(max_redirects, int) or max_redirects < 0:
        raise ValueError("max_redirects 必须是非负整数")

    current = str(url)
    hops = 0
    while True:
        remaining = _require_remaining(deadline)
        scheme, host, port, target = _parse_media_url(current)
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            if not _is_public_ipv4(host):
                raise MediaBlocked("private_or_reserved_ip")
            pinned = host
        else:
            pinned = _resolve_public_ipv4(host, port, deadline, resolver)

        timeout = max(0.05, min(remaining, MEDIA_HOP_TIMEOUT_S))
        context = ssl_context if ssl_context is not None else ssl.create_default_context()
        if scheme == "https":
            conn: http.client.HTTPConnection = _PinnedHTTPSConnection(
                host, port, connect_ip=pinned, dial=dial, timeout=timeout, context=context,
            )
        else:
            conn = _PinnedHTTPConnection(host, port, connect_ip=pinned, dial=dial, timeout=timeout)
        try:
            conn.request("GET", target, headers=_request_headers(headers, max_prefix_bytes))
            response = conn.getresponse()
            status = int(response.status)
            content_type = (response.getheader("Content-Type") or "").strip()
            content_encoding = (response.getheader("Content-Encoding") or "").strip()
            content_length = _int_or_none(response.getheader("Content-Length"))
            content_range = (response.getheader("Content-Range") or "").strip()

            if status in (301, 302, 303, 307, 308):
                location = (response.getheader("Location") or "").strip()
                try:
                    response.read(2048)
                except Exception:  # noqa: BLE001 - 重定向体不需要
                    pass
                if not location or len(location) > 4096:
                    raise MediaFetchFailed("redirect_invalid")
                if hops >= max_redirects:
                    raise MediaFetchFailed("too_many_redirects")
                current = urllib.parse.urljoin(current, location)
                hops += 1
                continue

            body = b""
            if 200 <= status < 300:
                try:
                    body = response.read(max_prefix_bytes)
                except (TimeoutError, socket.timeout) as exc:
                    reason = "deadline_exhausted" if _remaining(deadline) <= 0 else "socket_timeout"
                    raise MediaFetchFailed(reason) from exc
                _require_remaining(deadline)
            return MediaResponse(
                status=status,
                content_type=content_type,
                content_encoding=content_encoding,
                content_length=content_length,
                content_range=content_range,
                prefix=body,
                hops=hops,
                media_host=host,
            )
        except (MediaBlocked, MediaFetchFailed):
            raise
        except ssl.SSLError as exc:
            raise MediaFetchFailed("tls_failed") from exc
        except (TimeoutError, socket.timeout) as exc:
            reason = "deadline_exhausted" if _remaining(deadline) <= 0 else "socket_timeout"
            raise MediaFetchFailed(reason) from exc
        except http.client.HTTPException as exc:
            raise MediaFetchFailed("protocol_error") from exc
        except (ValueError, UnicodeError) as exc:
            # 非法请求行/请求头/主机字段同样只回受控原因，不输出原始文本。
            raise MediaFetchFailed("invalid_request") from exc
        except OSError as exc:
            raise MediaFetchFailed("connection_failed") from exc
        finally:
            conn.close()


# ----------------------------------------------------- HTTPX 适配 transport --

class _PrefixStream(httpx.AsyncByteStream):
    """把有界前缀作为流交给 HTTPX；不伪造服务器状态。"""

    def __init__(self, data: bytes):
        self._data = data

    async def __aiter__(self):
        if self._data:
            yield self._data

    async def aclose(self) -> None:
        return None


class PinnedMediaTransport(httpx.AsyncBaseTransport):
    """只让 app.probe_url 通过受限 transport 读取媒体。

    调用方不能把这个 transport 用在管理请求上；管理 origin 必须仍由普通
    loopback 客户端负责。
    """

    def __init__(
        self,
        *,
        deadline: float,
        resolver: Callable[[str, int], Sequence[str]] | None = None,
        dial: Callable[[tuple[str, int], float], socket.socket] | None = None,
        ssl_context: ssl.SSLContext | None = None,
        max_prefix_bytes: int = MAX_PREFIX_BYTES,
    ):
        self.deadline = float(deadline)
        self.resolver = resolver
        self.dial = dial
        self.ssl_context = ssl_context
        self.max_prefix_bytes = max_prefix_bytes
        self.last_response: MediaResponse | None = None
        self.last_error = ""
        self.last_error_kind = ""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            response = await asyncio.to_thread(
                fetch_media_prefix,
                str(request.url),
                deadline=self.deadline,
                headers=dict(request.headers),
                max_prefix_bytes=self.max_prefix_bytes,
                resolver=self.resolver,
                dial=self.dial,
                ssl_context=self.ssl_context,
            )
        except MediaBlocked as exc:
            self.last_response = None
            self.last_error = exc.reason
            self.last_error_kind = "blocked"
            raise
        except MediaFetchFailed as exc:
            self.last_response = None
            self.last_error = exc.reason
            self.last_error_kind = "failed"
            raise
        self.last_response = response
        self.last_error = ""
        self.last_error_kind = ""
        return httpx.Response(
            response.status,
            headers=response.pass_headers(),
            stream=_PrefixStream(response.prefix),
        )

    async def aclose(self) -> None:
        return None


# --------------------------------------------------------------- 前缀解码 --

class _DecodeStatus:
    """解码报告的受控状态字符串。"""

    DECODED = "decoded"
    NEEDS_MORE_DATA = "needs_more_data"
    INCONCLUSIVE = "inconclusive"
    NOT_TESTED = "not_tested"
    TIMED_OUT = "timed_out"
    OUTPUT_CAPPED = "output_capped"


_TRUNCATION_CLASSES = frozenset(
    {"container_moov_missing", "invalid_or_incomplete_data", "truncated_input"}
)


def _classify_stderr(raw: bytes) -> str:
    """只提取受控分类；原文不进入报告。"""
    text = raw.decode("utf-8", "replace").lower()
    markers = (
        ("moov atom not found", "container_moov_missing"),
        ("invalid data found when processing input", "invalid_or_incomplete_data"),
        ("partial file", "truncated_input"),
        ("unexpected eof", "truncated_input"),
        ("end of file", "truncated_input"),
        ("truncated", "truncated_input"),
        ("does not contain any stream", "no_audio_stream"),
        ("unknown decoder", "codec_unsupported"),
        ("decoder not found", "codec_unsupported"),
        ("unsupported codec", "codec_unsupported"),
    )
    for marker, code in markers:
        if marker in text:
            return code
    return "decode_error"


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    """终止整个进程组并回收；任何路径都不能留后台 ffmpeg。"""
    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=1.0)
    except (TimeoutError, asyncio.TimeoutError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


def _decode_argv(ffmpeg_path: str) -> list[str]:
    """只允许 pipe 输入输出；不联网、不打开任何外部路径。"""
    return [
        ffmpeg_path,
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-protocol_whitelist", "pipe",
        "-i", "pipe:0",
        "-vn",
        "-sn",
        "-dn",
        "-t", f"{DECODE_MAX_SECONDS:g}",
        "-ac", str(PCM_CHANNELS),
        "-ar", str(PCM_SAMPLE_RATE),
        "-f", "s16le",
        "pipe:1",
    ]


async def decode_prefix(
    prefix: bytes | bytearray,
    *,
    deadline: float,
    ffmpeg_path: str | None = None,
) -> dict:
    """用 ffmpeg pipe 有界解码 <=64KiB 前缀，输出最多 0.25s 单声道 16kHz PCM。

    输入缺失/过短：needs_more_data（不能断言整曲不可播）。
    无 ffmpeg 或剩余 deadline 不足：not_tested。
    超时：timed_out，并终止进程组；取消：清理后继续抛出 CancelledError。
    """
    if not isinstance(prefix, (bytes, bytearray)):
        raise TypeError("prefix 必须是 bytes")
    data = bytes(prefix)
    if len(data) > MAX_PREFIX_BYTES:
        raise ValueError("prefix 超过 65536 字节上限")

    report: dict[str, Any] = {
        "scope": "prefix_decode",
        "status": _DecodeStatus.NOT_TESTED,
        "reason": "",
        "input_bytes": len(data),
        "pcm_bytes": 0,
        "sample_rate_hz": PCM_SAMPLE_RATE,
        "channels": PCM_CHANNELS,
        "duration_ms": 0,
        "max_seconds": DECODE_MAX_SECONDS,
        "stderr_bytes": 0,
        "stderr_class": "",
        "client_playback": "not_tested",
        "full_track": "unknown",
    }
    if len(data) < MIN_PREFIX_BYTES:
        report.update(status=_DecodeStatus.NEEDS_MORE_DATA, reason="prefix_too_small")
        return report

    remaining = _remaining(deadline)
    if remaining <= MIN_DECODE_BUDGET_S:
        report["reason"] = "deadline_insufficient"
        return report

    executable = ffmpeg_path or shutil.which("ffmpeg")
    if not executable or not Path(executable).is_file():
        report["reason"] = "ffmpeg_missing"
        return report

    budget = min(DECODE_HARD_TIMEOUT_S, remaining)
    workdir = tempfile.mkdtemp(prefix="lx-decode-")
    env = {
        "PATH": os.environ.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin",
        "LC_ALL": "C",
        "LANG": "C",
        "TZ": "UTC",
        "HOME": workdir,
    }
    proc: asyncio.subprocess.Process | None = None
    stdout = bytearray()
    stderr = bytearray()
    capped = asyncio.Event()
    tasks: list[asyncio.Task] = []
    watchers: list[asyncio.Task] = []
    timed_out = False

    async def feed() -> None:
        assert proc is not None and proc.stdin is not None
        try:
            proc.stdin.write(data)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    async def read_stdout() -> None:
        assert proc is not None and proc.stdout is not None
        while True:
            try:
                chunk = await proc.stdout.read(8192)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            room = MAX_PCM_BYTES + 1 - len(stdout)
            if room <= 0:
                capped.set()
                return
            stdout.extend(chunk[:room])
            if len(stdout) > MAX_PCM_BYTES:
                capped.set()
                return

    async def read_stderr() -> None:
        assert proc is not None and proc.stderr is not None
        while len(stderr) < MAX_DECODE_STDERR_BYTES:
            try:
                chunk = await proc.stderr.read(1024)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            stderr.extend(chunk[: MAX_DECODE_STDERR_BYTES - len(stderr)])

    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *_decode_argv(executable),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except (OSError, ValueError):
            report["reason"] = "ffmpeg_spawn_failed"
            return report

        tasks = [
            asyncio.create_task(feed()),
            asyncio.create_task(read_stdout()),
            asyncio.create_task(read_stderr()),
        ]
        all_done = asyncio.ensure_future(asyncio.gather(*tasks, return_exceptions=True))
        capped_wait = asyncio.create_task(capped.wait())
        watchers = [all_done, capped_wait]
        done, _pending = await asyncio.wait(
            {all_done, capped_wait},
            timeout=budget,
            return_when=asyncio.FIRST_COMPLETED,
        )
        timed_out = not done
        if timed_out or capped.is_set():
            await _terminate_process(proc)
        else:
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.5)
            except (TimeoutError, asyncio.TimeoutError):
                await _terminate_process(proc)
    except asyncio.CancelledError:
        if proc is not None:
            await _terminate_process(proc)
        raise
    finally:
        for task in watchers + tasks:
            if not task.done():
                task.cancel()
        if watchers or tasks:
            await asyncio.gather(*watchers, *tasks, return_exceptions=True)
        if proc is not None and proc.returncode is None:
            await _terminate_process(proc)
        shutil.rmtree(workdir, ignore_errors=True)

    returncode = proc.returncode if proc is not None else None
    pcm = bytes(stdout[:MAX_PCM_BYTES])
    stderr_class = _classify_stderr(bytes(stderr))
    report["stderr_bytes"] = len(stderr)
    report["stderr_class"] = stderr_class

    if timed_out:
        report.update(status=_DecodeStatus.TIMED_OUT, reason="ffmpeg_timeout")
    elif capped.is_set() or len(stdout) > MAX_PCM_BYTES:
        report.update(
            status=_DecodeStatus.OUTPUT_CAPPED,
            reason="output_exceeded_cap",
            pcm_bytes=len(pcm),
        )
    elif returncode != 0:
        reason = stderr_class if stderr_class != "decode_error" else "ffmpeg_exit_nonzero"
        status = (
            _DecodeStatus.NEEDS_MORE_DATA
            if stderr_class in _TRUNCATION_CLASSES
            else _DecodeStatus.INCONCLUSIVE
        )
        report.update(status=status, reason=reason)
    elif not pcm:
        report.update(status=_DecodeStatus.INCONCLUSIVE, reason="empty_pcm")
    else:
        frames = len(pcm) / float(PCM_SAMPLE_RATE * PCM_CHANNELS * 2)
        report.update(
            status=_DecodeStatus.DECODED,
            reason="",
            pcm_bytes=len(pcm),
            duration_ms=int(min(DECODE_MAX_SECONDS * 1000, round(frames * 1000))),
        )
    return report


# ------------------------------------------------------- 可信 app.py 快照 --

_ENV_KEYS = (
    "FNMUSIC_LOG_DIR",
    "LX_DATA_DIR",
    "LX_ENV_PATH",
    "LXSERVER_URL",
    "LXSERVER_ADMIN_PASSWORD",
)
_SNAPSHOT_MARKERS = (
    "async def _attempt_pinned(",
    "async def probe_url(",
    "def _media_signature(",
    "LXSERVER = LxServerClient(",
)
_SNAPSHOT_SIBLINGS = (
    ("lxserver_client.py", "class LxServerClient"),
    ("playback_policy.py", "def plan_steps"),
)
_MODULE_CACHE: dict[tuple[str, int, int, str], ModuleType] = {}
_MODULE_TEMPDIRS: list[str] = []


def snapshot_sha256(path: str | Path) -> str:
    """可信快照的 sha256；只读文件，不导入。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_snapshot(path: Path) -> None:
    """拒绝玩具/残缺快照：必须含真实 app 入口且同目录有真实依赖模块。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ComparisonConfigError("快照不可读") from exc
    for marker in _SNAPSHOT_MARKERS:
        if marker not in text:
            raise ComparisonConfigError("快照缺少真实 app 入口")
    for filename, marker in _SNAPSHOT_SIBLINGS:
        sibling = path.parent / filename
        if not sibling.is_file():
            raise ComparisonConfigError("快照缺少同目录依赖模块")
        try:
            if marker not in sibling.read_text(encoding="utf-8"):
                raise ComparisonConfigError("同目录依赖模块不是真实实现")
        except OSError as exc:
            raise ComparisonConfigError("同目录依赖模块不可读") from exc
    repo_root = path.parent.parent
    if not (repo_root / "proxy" / "fnlog.py").is_file():
        raise ComparisonConfigError("快照不在包含 proxy 的仓库快照内")


def load_app_module(app_source_path: str, expected_sha256: str = "") -> ModuleType:
    """从显式传入的绝对快照路径导入 app.py，并隔离宿主日志/数据目录。

    导入只写临时目录；不默认使用镜像或 sys.path 里已有的旧 app。重复调用相同
    快照会命中缓存，避免反复创建 FastAPI 实例。
    """
    path = Path(app_source_path)
    if not path.is_absolute():
        raise ComparisonConfigError("快照路径必须是绝对路径")
    if not path.is_file():
        raise ComparisonConfigError("快照文件不存在")
    sha = snapshot_sha256(path)
    if expected_sha256 and sha != str(expected_sha256).strip().lower():
        raise ComparisonConfigError("快照 sha256 不匹配")
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size, sha)
    cached = _MODULE_CACHE.get(key)
    if cached is not None:
        return cached

    _validate_snapshot(path)
    tempdir = tempfile.mkdtemp(prefix="lx-adapter-compare-")
    _MODULE_TEMPDIRS.append(tempdir)
    module_name = f"_lx_adapter_snapshot_{sha[:16]}"
    old_env = {name: os.environ.get(name) for name in _ENV_KEYS}
    os.environ.update(
        {
            "FNMUSIC_LOG_DIR": os.path.join(tempdir, "logs"),
            "LX_DATA_DIR": os.path.join(tempdir, "data"),
            "LX_ENV_PATH": os.path.join(tempdir, "absent.env"),
            "LXSERVER_URL": "http://127.0.0.1:9",
            "LXSERVER_ADMIN_PASSWORD": "unused-diagnostic-placeholder",
        }
    )
    # app.py 以 `from lxserver_client import ...` 方式引用同目录模块，
    # 以及 import proxy.fnlog；导入期需要这两个目录可导入。
    sys.path.insert(0, str(path.parent))
    sys.path.insert(0, str(path.parent.parent))
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ComparisonConfigError("快照无法加载")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - 报告只留类型，不留路径/正文
            sys.modules.pop(module_name, None)
            raise ComparisonConfigError(f"快照导入失败: {type(exc).__name__}") from None
    finally:
        for name, value in old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    _MODULE_CACHE[key] = module
    return module


def reset_app_module_cache() -> None:
    """清理已导入快照与临时目录；测试和 worker 可在结束时调用。"""
    for key in list(_MODULE_CACHE):
        module = _MODULE_CACHE.pop(key, None)
        name = getattr(module, "__name__", "")
        if name:
            sys.modules.pop(name, None)
    while _MODULE_TEMPDIRS:
        shutil.rmtree(_MODULE_TEMPDIRS.pop(), ignore_errors=True)


atexit.register(reset_app_module_cache)


# ---------------------------------------------------------- 适配流程对比 --

_STAGE_RE = re.compile(r"stage=([a-z_]+)")
_STAGE_WHITELIST = frozenset(
    {
        "probe_reject",
        "probe_format_unknown",
        "quality_above_target",
        "quality_format_mismatch",
        "quality_lossless_above_target",
        "source_id_mismatch",
    }
)
_HONEST_REJECT_STAGES = frozenset(
    {
        "quality_format_mismatch",
        "quality_above_target",
        "quality_lossless_above_target",
        "probe_format_unknown",
    }
)
_LOSSY_MAGICS = frozenset({"mp3", "aac"})


class _StageCapture(logging.Handler):
    """只抓 app 日志里的 stage= 受控标记，不保留日志正文。"""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.stages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - 日志格式化失败不能影响主流程
            return
        for match in _STAGE_RE.finditer(message):
            stage = match.group(1)
            if stage in _STAGE_WHITELIST:
                self.stages.append(stage)

    def last_stage(self) -> str:
        return self.stages[-1] if self.stages else ""


class _RecordingNativeClient:
    """记录原生 response 的委托客户端；只供本模块读取 type/sourceId/url。"""

    def __init__(self, client):
        self._client = client
        self.last_response: dict | None = None
        self.error_type = ""

    async def get_music_url(self, *args, **kwargs):
        try:
            response = await self._client.get_music_url(*args, **kwargs)
        except Exception as exc:
            self.error_type = type(exc).__name__
            raise
        self.last_response = dict(response) if isinstance(response, dict) else None
        return response


def validate_management_origin(origin: str) -> str:
    """只接受精确的 loopback IPv4 管理 origin；拒绝域名、HTTPS、路径和 userinfo。"""
    try:
        parts = urllib.parse.urlsplit(str(origin).strip())
    except ValueError as exc:
        raise ComparisonConfigError("管理 origin 无法解析") from exc
    if parts.scheme != "http":
        raise ComparisonConfigError("管理 origin 只允许 http")
    if parts.username is not None or parts.password is not None:
        raise ComparisonConfigError("管理 origin 不允许 userinfo")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ComparisonConfigError("管理 origin 不允许路径/query/fragment")
    host = parts.hostname
    if not host:
        raise ComparisonConfigError("管理 origin 缺少主机名")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ComparisonConfigError("管理 origin 必须是 loopback IPv4 字面量") from exc
    if address.version != 4 or not address.is_loopback:
        raise ComparisonConfigError("管理 origin 必须指向 loopback")
    try:
        port = parts.port
    except ValueError as exc:
        raise ComparisonConfigError("管理 origin 端口非法") from exc
    if not port:
        raise ComparisonConfigError("管理 origin 必须显式指定端口")
    return f"http://{host}:{port}"


def _base_report(request: CandidateComparisonRequest) -> dict:
    """报告的固定骨架；所有可变字段都在后续阶段填充。"""
    return {
        "scope": "adapter_candidate_comparison",
        "candidate_only": True,
        "source_verdict": None,
        "deadline_exhausted": False,
        "stages": {
            "deadline": "ok",
            "app_snapshot": "not_loaded",
            "native_resolve": "not_tested",
            "native_prefix": "not_tested",
            "adapter_resolve": "not_tested",
            "adapter_probe": "not_tested",
            "decode": "not_tested",
        },
        "app_source_sha256": "",
        "source_id": str(request.source_id),
        "platform": str(request.platform),
        "quality": {
            "requested_tier": "",
            "requested_lx_quality": "",
            "resolution_quality": None,
            "resolution_lx_quality": None,
            "resolution_quality_is_request_echo": True,
            "actual_quality": None,
            "actual_known": False,
        },
        "native_resolve": {
            "status": "not_tested",
            "echo_lx_quality": "",
            "resolution_quality": None,
            "source_id_matched": False,
            "error_type": "",
        },
        "adapter": {
            "status": "not_tested",
            "stage": "",
            "media_host": "",
            "content_type": "",
            "probe": "",
            "resolution_quality": "",
            "actual_quality": None,
            "actual_known": False,
            "probe_readable": False,
            "error_type": "",
        },
        "native_prefix": {"status": "not_tested", "reason": "", "magic": ""},
        "adapter_prefix": {"status": "not_fetched", "reason": "", "magic": ""},
        "prefix_comparison": {
            "native_prefix_bytes": 0,
            "adapter_prefix_bytes": 0,
            "same_status": None,
            "same_bytes": None,
        },
        "decode": {"native": None, "adapter": None},
        "candidate_disposition": "not_tested",
        "coverage": {
            "native_url": "not_tested",
            "adapter_flow": "not_tested",
            "adapter_probe": "not_tested",
            "prefix_decode": "not_tested",
            "client_playback": "not_tested",
            "actual_quality": "unknown",
        },
        "notes": [],
    }


def _prefix_entry(status: str, reason: str = "", media: MediaResponse | None = None, magic: str = "") -> dict:
    """前缀证据条目：status 是受控字符串，http_status 才是服务器真实状态码。"""
    entry: dict[str, Any] = {"status": status, "reason": reason, "magic": magic}
    if media is not None:
        evidence = media.evidence()
        entry["http_status"] = evidence.pop("status")
        entry.update(evidence)
    return entry


def _quality_disposition(report: dict) -> str:
    """候选级处置：服务错误/超时/正确拒绝/普通拒绝，绝不输出整源结论。"""
    adapter = report["adapter"]
    status = adapter["status"]
    if status == "ok":
        return "adapter_ok"
    if status == "service_error":
        return "inconclusive_service_error"
    if status == "timeout":
        return "inconclusive_timeout"
    if status == "error":
        return "inconclusive"
    stage = adapter["stage"]
    native_status = report["native_prefix"]["status"]
    magic = report["native_prefix"].get("magic") or ""
    if stage in _HONEST_REJECT_STAGES:
        if stage == "quality_format_mismatch" and native_status == "ok" and magic in _LOSSY_MAGICS:
            return "adapter_honest_reject"
        if stage in ("quality_above_target", "quality_lossless_above_target"):
            return "adapter_honest_reject"
        return "adapter_rejected"
    if native_status in ("ok", "http_error"):
        return "adapter_rejected"
    return "inconclusive"


async def compare_candidate(request: CandidateComparisonRequest) -> dict:
    """对比一条候选：真实 app 适配结果 + 原生前缀 + 有界前缀解码。

    调用方必须传入同一个绝对 deadline；本函数不会创建新的 90 秒预算。
    返回候选级报告，不产生 source_verdict；单候选失败不能判整源失效。
    """
    started = time.monotonic()
    report = _base_report(request)
    remaining = float(request.deadline) - started
    if remaining <= 0:
        report["deadline_exhausted"] = True
        report["stages"]["deadline"] = "exhausted"
        report["notes"].append("deadline_exhausted_before_start")
        return report

    origin = validate_management_origin(request.management_origin)
    if not str(request.source_id or "").strip():
        raise ComparisonConfigError("source_id 不能为空")
    platform = str(request.platform or "").strip().lower()
    if platform not in SUPPORTED_PLATFORMS:
        raise ComparisonConfigError("不支持的平台")
    if not isinstance(request.song_info, dict) or not request.song_info:
        raise ComparisonConfigError("song_info 不能为空")
    if not str(request.admin_password or ""):
        raise ComparisonConfigError("管理口令不能为空")
    report["platform"] = platform

    module = load_app_module(request.app_source_path, request.app_sha256)
    report["stages"]["app_snapshot"] = "loaded"
    report["app_source_sha256"] = snapshot_sha256(request.app_source_path)
    try:
        tier = module._normalize_quality(request.quality)
    except ValueError as exc:
        raise ComparisonConfigError("音质档位无效") from exc
    lx_quality = module._tier_to_lx_quality(tier)
    report["quality"]["requested_tier"] = tier
    report["quality"]["requested_lx_quality"] = lx_quality

    native_client = module.LxServerClient(
        base_url=origin,
        admin_password=request.admin_password,
        timeout=max(0.1, min(remaining, 2.0)),
        store_path="",
    )
    # 真实 LxServerClient 逻辑保留，但底层 HTTPX 必须禁用环境代理且只连 loopback。
    native_client._client = httpx.AsyncClient(
        base_url=origin,
        timeout=httpx.Timeout(max(0.1, remaining)),
        trust_env=False,
        follow_redirects=False,
        headers={
            "User-Agent": "fnmusic-adapter-compare/1.0",
            "x-frontend-auth": request.admin_password,
        },
    )
    recorder = _RecordingNativeClient(native_client)
    transport = PinnedMediaTransport(
        deadline=float(request.deadline),
        resolver=request.resolver,
        dial=request.dial,
        ssl_context=request.ssl_context,
    )
    adapter_client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(max(0.1, remaining)),
        trust_env=False,
        follow_redirects=False,
    )
    logger_name = "fnmusic.lxmusic"
    app_logger = logging.getLogger(logger_name)
    capture = _StageCapture()
    app_logger.addHandler(capture)
    old_lxserver = module.LXSERVER
    module.LXSERVER = recorder
    attempt = None
    native_url = ""
    try:
        left = float(request.deadline) - time.monotonic()
        if left <= 0:
            report["deadline_exhausted"] = True
            report["stages"]["deadline"] = "exhausted"
            return report
        track_id = str(
            request.song_info.get("id")
            or f"lx:{platform}:{request.song_info.get('songmid') or ''}"
        )
        script = module.ScriptInfo(
            id=str(request.source_id),
            name=str(request.source_id),
            ready=True,
            platforms=(platform,),
        )
        try:
            attempt = await module._attempt_pinned(
                adapter_client,
                script=script,
                platform=platform,
                song_info=request.song_info,
                resolved_track_id=track_id,
                track_id=track_id,
                quality=tier,
                requested_quality=tier,
                deadline=float(request.deadline),
                slot_deadline=float(request.deadline),
                resolver_timeout=max(0.01, left),
                probe_timeout=max(0.01, left),
                exclude_sources=None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单候选异常不能炸整轮；只留类型
            attempt = None
            report["adapter"]["status"] = "error"
            report["adapter"]["error_type"] = type(exc).__name__
        raw = recorder.last_response or {}
        native_url = str(raw.get("url") or "")
        echo = str(raw.get("type") or "")
        native_resolution = module.from_lx_quality(echo)
        if native_url:
            native_status = "url_present"
        elif recorder.error_type:
            native_status = "error"
        else:
            native_status = "url_absent"
        report["native_resolve"] = {
            "status": native_status,
            "echo_lx_quality": echo,
            "resolution_quality": native_resolution or None,
            "source_id_matched": str(raw.get("sourceId") or "") == str(request.source_id),
            "error_type": recorder.error_type,
        }
        report["stages"]["native_resolve"] = native_status
    finally:
        module.LXSERVER = old_lxserver
        app_logger.removeHandler(capture)
        await adapter_client.aclose()
        await native_client.close()

    stage = capture.last_stage()
    if attempt is not None and attempt.data:
        data = attempt.data
        evidence = dict(data.get("quality_evidence") or {})
        report["adapter"].update(
            status="ok",
            stage=stage,
            media_host=str(evidence.get("media_host") or ""),
            content_type=str(evidence.get("content_type") or ""),
            probe=str(evidence.get("probe") or ""),
            resolution_quality=str(data.get("resolution_quality") or ""),
            probe_readable=bool(evidence.get("probe_readable")),
        )
        report["quality"]["resolution_quality"] = data.get("resolution_quality") or None
        report["quality"]["resolution_lx_quality"] = data.get("resolution_lx_quality") or None
        report["stages"]["adapter_resolve"] = "ok"
        report["stages"]["adapter_probe"] = "ok"
        report["coverage"]["adapter_probe"] = "tested"
    elif attempt is not None:
        if attempt.service_error:
            status = "service_error"
        elif attempt.timeout:
            status = "timeout"
        else:
            status = "rejected"
        report["adapter"].update(status=status, stage=stage)
        report["stages"]["adapter_resolve"] = status
        if status in ("service_error", "timeout"):
            report["stages"]["adapter_probe"] = "not_reached"
        else:
            report["stages"]["adapter_probe"] = "rejected"
    else:
        if report["adapter"]["status"] != "error":
            report["adapter"]["status"] = "not_tested"
        report["stages"]["adapter_resolve"] = report["adapter"]["status"]
    report["coverage"]["adapter_flow"] = "tested" if (attempt is not None or report["adapter"]["status"] == "error") else "not_tested"
    report["coverage"]["native_url"] = "tested" if native_url else "not_tested"

    # 原生前缀：只抓取一次，证据与解码复用同一个 MediaResponse。
    native_media: MediaResponse | None = None
    if native_url:
        try:
            native_media = await asyncio.to_thread(
                fetch_media_prefix,
                native_url,
                deadline=float(request.deadline),
                headers=module._platform_headers(platform),
                resolver=request.resolver,
                dial=request.dial,
                ssl_context=request.ssl_context,
            )
            magic = module._media_signature(native_media.prefix)
            native_status = "ok" if 200 <= native_media.status < 300 else "http_error"
            report["native_prefix"] = _prefix_entry(native_status, "", native_media, magic)
            report["stages"]["native_prefix"] = native_status
        except MediaBlocked as exc:
            report["native_prefix"] = _prefix_entry("blocked", exc.reason)
            report["stages"]["native_prefix"] = "blocked"
        except MediaFetchFailed as exc:
            report["native_prefix"] = _prefix_entry("failed", exc.reason)
            report["stages"]["native_prefix"] = "failed"
        except Exception as exc:  # noqa: BLE001 - 只留类型
            report["native_prefix"] = _prefix_entry("failed", type(exc).__name__)
            report["stages"]["native_prefix"] = "failed"
    else:
        report["native_prefix"] = _prefix_entry("not_tested", "no_native_url")

    # 适配前缀来自 app.probe_url 实际经过的受限 transport。
    adapter_media = transport.last_response
    if adapter_media is not None:
        magic = module._media_signature(adapter_media.prefix)
        adapter_status = "ok" if 200 <= adapter_media.status < 300 else "http_error"
        report["adapter_prefix"] = _prefix_entry(adapter_status, "", adapter_media, magic)
    elif transport.last_error:
        report["adapter_prefix"] = _prefix_entry(
            "blocked" if transport.last_error_kind == "blocked" else "failed",
            transport.last_error,
        )
    else:
        report["adapter_prefix"] = _prefix_entry("not_fetched", "probe_not_reached")

    native_bytes = int(report["native_prefix"].get("prefix_bytes") or 0)
    adapter_bytes = int(report["adapter_prefix"].get("prefix_bytes") or 0)
    report["prefix_comparison"] = {
        "native_prefix_bytes": native_bytes,
        "adapter_prefix_bytes": adapter_bytes,
        "same_status": (
            report["native_prefix"].get("http_status")
            == report["adapter_prefix"].get("http_status")
            if native_bytes and adapter_bytes
            else None
        ),
        "same_bytes": (
            report["native_prefix"].get("prefix_sha256")
            == report["adapter_prefix"].get("prefix_sha256")
            if native_bytes and adapter_bytes
            else None
        ),
    }

    # 解码两侧前缀；共用同一个剩余 deadline，不再新建预算。
    async def decode_side(media: MediaResponse | None, absent_reason: str) -> dict:
        if media is None:
            return {
                "scope": "prefix_decode",
                "status": _DecodeStatus.NOT_TESTED,
                "reason": absent_reason,
                "input_bytes": 0,
                "pcm_bytes": 0,
                "sample_rate_hz": PCM_SAMPLE_RATE,
                "channels": PCM_CHANNELS,
                "duration_ms": 0,
                "max_seconds": DECODE_MAX_SECONDS,
                "stderr_bytes": 0,
                "stderr_class": "",
                "client_playback": "not_tested",
                "full_track": "unknown",
            }
        return await decode_prefix(
            media.prefix,
            deadline=float(request.deadline),
            ffmpeg_path=request.ffmpeg_path,
        )

    # 解码复用已登记的原生前缀；再次取流会浪费总预算，还可能让短链过期
    # 或网络抖动抹掉本轮已取得的证据。适配侧继续使用自身独立的响应。
    report["decode"]["native"] = await decode_side(native_media, "no_native_prefix")
    report["decode"]["adapter"] = await decode_side(adapter_media, "no_adapter_prefix")
    decode_statuses = {
        report["decode"]["native"]["status"],
        report["decode"]["adapter"]["status"],
    }
    if decode_statuses <= {_DecodeStatus.NOT_TESTED}:
        report["stages"]["decode"] = "not_tested"
        report["coverage"]["prefix_decode"] = "not_tested"
    elif _DecodeStatus.DECODED in decode_statuses:
        report["stages"]["decode"] = "ok"
        report["coverage"]["prefix_decode"] = "tested"
    else:
        report["stages"]["decode"] = "inconclusive"
        report["coverage"]["prefix_decode"] = "partial"

    # 适配被拒绝时也要保留同一次上游响应的 type 回显，供编排/降档说明使用；
    # 它仍是请求回显，不是实测档位。
    if report["quality"]["resolution_quality"] is None and report["native_resolve"]["resolution_quality"]:
        report["quality"]["resolution_quality"] = report["native_resolve"]["resolution_quality"]
        report["quality"]["resolution_lx_quality"] = (
            report["native_resolve"]["echo_lx_quality"] or None
        )

    report["candidate_disposition"] = _quality_disposition(report)
    report["notes"] = [
        "candidate_only_result_must_not_mark_whole_source_failed",
        "resolution_quality_is_lxserver_type_echo_not_measurement",
        "prefix_decode_is_not_client_playback_or_full_track",
    ]
    if report["candidate_disposition"] == "adapter_honest_reject":
        report["notes"].append("native_url_reachable_but_project_honestly_rejected_lossy_masquerade")
    if report["coverage"]["prefix_decode"] == "not_tested":
        report["notes"].append("prefix_decode_not_tested")
    return report
