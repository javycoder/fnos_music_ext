"""musicdl HTTP 服务：把 musicdl 库包装成共享音源 API.

统一曲目 ID 契约: "<source>:<identifier>"，例如 "kuwo:228908"。
两个消费方（fnmusic-ext 代理、music-box）都以该 ID 串通信。
"""
import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

logger = logging.getLogger("musicdl_service")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("musicdl").setLevel(logging.ERROR)

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    curl_requests = None
    HAS_CURL_CFFI = False
    logger.warning("curl_cffi not installed, falling back to standard httpx client")

from musicdl import musicdl  # noqa: E402
# worker 也是从 musicdl-service 目录启动，两个进程共用同一份 search_core
sys.path.insert(0, str(Path(__file__).resolve().parent))
import search_core  # noqa: E402  归一化与媒体履约判据（worker 与 API 共用同一份）
from hardening import AdaptiveTimeout, SearchCache, SourceBreaker, SingleFlight, SourceBulkhead, SourceBusy, SearchProgress  # noqa: E402

# 复用仓库根（容器内 /srv）的 proxy.env_merge 解析 .env；独立运行无仓库结构时退回纯环境变量
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from proxy.env_merge import parse_env_file  # noqa: E402
except ImportError:
    parse_env_file = None

# 统一日志：主进程把诊断落盘到 logs/musicdl.log（脱敏、轮转、保留与 proxy 一致），
# 管理页日志包按文件名自动收录。独立运行无 fnlog 时退回纯 stdout/stderr。
try:
    from proxy.fnlog import redact as _fnlog_redact, setup_logging as _fnlog_setup  # noqa: E402
except ImportError:
    try:
        from fnlog import redact as _fnlog_redact, setup_logging as _fnlog_setup  # noqa: E402
    except ImportError:
        _fnlog_redact = None
        _fnlog_setup = None
if _fnlog_setup is not None:
    _fnlog_setup("musicdl_service", "musicdl")


def _startup_sources() -> "tuple[list[str], str]":
    """解析 MUSICDL_SOURCES 白名单：.env > 进程环境变量 > 内置默认。

    WebUI 保存只写 bind mount 的 /repo/.env 再 supervisorctl restart；restart 继承的
    是容器启动时刻的 supervisord 环境，新白名单只可能出现在 .env 里，所以 .env
    必须优先于（可能陈旧的）环境变量。
    """
    raw = ""
    origin = ""
    if parse_env_file is not None:
        env_path = Path(os.environ.get("FNMUSIC_ENV_FILE", "/repo/.env"))
        try:
            kv, _ = parse_env_file(env_path)
            raw = (dict(kv).get("MUSICDL_SOURCES") or "").strip()
        except Exception:
            raw = ""
        if raw:
            origin = f"env file {env_path}"
    if not raw:
        raw = os.environ.get("MUSICDL_SOURCES", "").strip()
        if raw:
            origin = "process env"
    if not raw:
        raw = "KuwoMusicClient,MiguMusicClient"
        origin = "builtin default"
    sources = [s.strip() for s in raw.split(",") if s.strip()]
    return sources, origin


_CONF_SOURCES, _SOURCES_ORIGIN = _startup_sources()


def _startup_internal_token() -> str:
    token = os.environ.get("FNMUSIC_INTERNAL_TOKEN", "").strip()
    if token or parse_env_file is None:
        return token
    try:
        pairs, _ = parse_env_file(Path(os.environ.get("FNMUSIC_ENV_FILE", "/repo/.env")))
        return str(dict(pairs).get("FNMUSIC_INTERNAL_TOKEN") or "").strip()
    except Exception:
        return ""


_INTERNAL_TOKEN = _startup_internal_token()
_DIRECT_RESOLVE_INFLIGHT: dict[tuple, asyncio.Task] = {}
_DIRECT_SELECTIONS: dict[str, dict] = {}

CONF = {
    "sources": _CONF_SOURCES,
    "search_timeout": float(os.environ.get("MUSICDL_SEARCH_TIMEOUT", "12")),
    "limit_per_source": int(os.environ.get("MUSICDL_LIMIT_PER_SOURCE", "10")),
    "url_ttl": int(os.environ.get("MUSICDL_URL_TTL", "1800")),
    "cache_max": int(os.environ.get("MUSICDL_CACHE_MAX", "3000")),
    "work_dir": os.environ.get("MUSICDL_WORK_DIR", "/tmp/musicdl_outputs"),
    "search_cache_ttl": int(os.environ.get("MUSICDL_SEARCH_CACHE_TTL", "300")),
    "search_cache_max": int(os.environ.get("MUSICDL_SEARCH_CACHE_MAX", "200")),
    "breaker_threshold": int(os.environ.get("MUSICDL_BREAKER_THRESHOLD", "4")),
    "breaker_cooldown": int(os.environ.get("MUSICDL_BREAKER_COOLDOWN", "120")),
    # Network inactivity timeout is separate from the async response deadline.
    "request_timeout": max(0.1, float(os.environ.get("MUSICDL_REQUEST_TIMEOUT", "5"))),
    "neg_cache_ttl": int(os.environ.get("MUSICDL_NEG_TTL", "30")),
    # 自适应熔断/降级
    "slow_degrade_s": float(os.environ.get("MUSICDL_SLOW_DEGRADE_S", "8")),
    # 一页 10 首的上游搜索大约要 4 秒，再留出探活。压到 3 秒时搜索函数还没返回，
    # 进度里一首都没有，界面就是 0 条，并且会把下一次超时继续压在地板上。
    "adaptive_min_timeout": float(os.environ.get("MUSICDL_ADAPTIVE_MIN_TIMEOUT", "8")),
    "search_page_cap": max(1, int(os.environ.get("MUSICDL_SEARCH_PAGE_CAP", "10"))),
    "probe_timeout": max(0.2, float(os.environ.get("MUSICDL_PROBE_TIMEOUT", "2"))),
    "fast_return_items": int(os.environ.get("MUSICDL_FAST_RETURN_ITEMS", "12")),
    "slow_grace_s": float(os.environ.get("MUSICDL_SLOW_GRACE_S", "2")),
}

SEARCH_CACHE = SearchCache(
    ttl=CONF["search_cache_ttl"],
    max_entries=CONF["search_cache_max"],
)
SOURCE_BREAKER = SourceBreaker(
    failure_threshold=CONF["breaker_threshold"],
    cooldown=CONF["breaker_cooldown"],
    slow_threshold_s=CONF["slow_degrade_s"],
)
ADAPTIVE = AdaptiveTimeout(
    base_timeout=CONF["search_timeout"],
    min_timeout=CONF["adaptive_min_timeout"],
    slow_latency=CONF["slow_degrade_s"],
)
SINGLE_FLIGHT = SingleFlight()

# id -> {"item": {...}, "keyword": str, "download_headers": dict, "lyric": str, "ts": float}
_SONG_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_STATS = {"searches": 0, "errors": 0}


def _source_short(client_name: str) -> str:
    return (client_name or "").replace("MusicClient", "").lower()


def _source_mapping() -> dict:
    """Resolve names without guessing capitalization (HTQYY/FiveSing/MyFreeMP3)."""
    names = set(CONF["sources"])
    try:
        from musicdl.modules.sources import MusicClientBuilder
        names.update(MusicClientBuilder.REGISTERED_MODULES)
    except ImportError:
        names.update(getattr(musicdl, "SUPPORTED_MUSIC_SOURCES", []) or [])
    return {alias: name for name in sorted(names)
            for alias in (name.casefold(), _source_short(name).casefold())}


def _normalize_startup_sources(raw: list, registered: set | None = None) -> list:
    """MUSICDL_SOURCES 白名单启动归一：短名/别名 → 注册全名，未知项告警丢弃。

    库层 musicdl.MusicClient 只认全名；短名必须在服务入口解析。
    只对照注册表解析（不把白名单自身当别名），打错的平台名才会被丢弃。"""
    if registered is None:
        try:
            from musicdl.modules.sources import MusicClientBuilder
            registered = set(MusicClientBuilder.REGISTERED_MODULES)
        except ImportError:
            registered = set(getattr(musicdl, "SUPPORTED_MUSIC_SOURCES", []) or [])
    alias = {a: n for n in sorted(registered)
             for a in (n.casefold(), _source_short(n).casefold())}
    out: list = []
    for name in raw:
        resolved = alias.get(name.strip().casefold())
        if resolved is None:
            logger.warning("MUSICDL_SOURCES 忽略未知平台 %r（可用平台见 /sources）", name)
            continue
        if resolved not in out:
            out.append(resolved)
    return out or ["KuwoMusicClient", "MiguMusicClient"]


CONF["sources"] = _normalize_startup_sources(CONF["sources"])

SOURCE_NAMES = _source_mapping()


def _log_worker_line(source: str, text: str) -> None:
    """worker 子进程的诊断行：主进程统一落盘（脱敏），不记录凭证/完整直链。"""
    # Unstructured worker output is intentionally discarded.
    return None


SOURCE_WORKERS = SourceBulkhead(SOURCE_NAMES.values(), on_worker_line=_log_worker_line)


def _source_client(name: str) -> str:
    client = SOURCE_NAMES.get(name.strip().casefold())
    if client is None:
        raise HTTPException(400, f"unknown music source {name!r}; see /sources")
    return client


def _cache_put(item: dict, keyword: str, download_headers: dict, lyric: str):
    with _CACHE_LOCK:
        if len(_SONG_CACHE) >= CONF["cache_max"]:
            # 淘汰最旧的一半
            for k in sorted(_SONG_CACHE, key=lambda k: _SONG_CACHE[k]["ts"])[: len(_SONG_CACHE) // 2]:
                _SONG_CACHE.pop(k, None)
        _SONG_CACHE[item["id"]] = {
            "item": item,
            "keyword": keyword,
            "download_headers": download_headers or {},
            "lyric": lyric or "",
            "ts": time.time(),
        }


def _cache_get(song_id: str):
    with _CACHE_LOCK:
        return _SONG_CACHE.get(song_id)


def _search_payload(source: str, keyword: str, fetch_size: int, limit: int,
                    song_id: str | None, timeout: float) -> dict:
    """构造 worker 任务。worker 内搜索+探活自限到预算的 90%，留尾部给父进程收尾交付。"""
    return {
        "op": "search", "source": source, "keyword": keyword,
        "fetch_size": fetch_size, "limit": limit, "song_id": song_id,
        "budget_s": max(0.2, timeout - min(0.1, timeout * 0.1)) if song_id is None else timeout,
    }


def _note_empty_source_result(source: str, breaker, adaptive) -> None:
    """记录一次「源正常返回但没有可用结果」。

    空结果不是平台失效：可能是关键词不在这个站、也可能是所有候选都被初筛挡掉
    （例如需要后处理受 DRM 保护的流，本服务不支持）。因此不记熔断失败，也不压
    自适应预算——否则一次空搜索会把这个源在冷却期内判成永久故障。
    """
    logger.info("source %s returned no usable results (not counted as failure)", source)


async def _refresh_by_keyword(song_id: str) -> dict | None:
    """URL 过期或下载失败后按缓存的关键词重搜一次，找回同 ID 的曲目。"""
    entry = _cache_get(song_id)
    if not entry or not entry.get("keyword"):
        return None
    try:
        src_client = _source_client(entry["item"].get("source", ""))
        limit = max(CONF["limit_per_source"], 10)
        timeout = ADAPTIVE.timeout_for(src_client)
        entries: list = []
        await SOURCE_WORKERS.run(
            src_client,
            _search_payload(src_client, entry["keyword"], limit, 1, song_id, timeout),
            timeout=timeout,
            on_entry=lambda e: entries.append((e[0], e[1], e[2])),
        )
    except Exception as exc:
        logger.warning("Refresh failed for %s: %s", song_id, exc)
        return None
    for item, headers, lyric in entries:
        _cache_put(item, entry["keyword"], headers, lyric)
        return _cache_get(song_id)
    return None


# --- 损坏流防护（2026-09-29 实测）-----------------------------------------
# kuwo 官方 CDN 的无损档开始交付不可解码字节（文件头仍是合法 fLaC、大小与
# Content-Length 一致，musicdl 库只验 URL/扩展名发现不了），代理侧会把这种
# 流原样落进曲库，官方转码器编到坏点即报"transcoding error"。取流前对无损
# 档做头部解码探针：坏流自动降级到 kuwo 官方 convert_url2 的 mp3 档（实测
# 干净、无需登录）；上层也可以用 quality=mp3 主动要求有损档。
_PROBE_EXTS = {"flac", "wav", "ape", "m4a", "alac", "ogg", "opus"}
_PROBE_BYTES = 3 * 1024 * 1024
_PROBE_MIN_DECODED_S = 1.0
_PROBE_CACHE: dict[str, tuple[float, bool]] = {}
_PROBE_CACHE_LOCK = threading.Lock()
_PROBE_TTL = int(os.environ.get("MUSICDL_PROBE_TTL", "600"))


def ffmpeg_bin() -> str | None:
    return os.environ.get("FNMUSIC_FFMPEG_BIN") or shutil.which("ffmpeg")


def _decode_probe_ok(path: str) -> bool:
    """ffmpeg 解码探针样本文件，以实际解出的时长判断好坏。

    截断的完好样本也能解出数十秒（探针只取头部字节）；从头就坏的样本
    time≈0。报错文本不可用作判据——截断与损坏的报错完全相同。
    若系统未配置/缺少 ffmpeg，无法执行解码校验，放行样本文件返回 True。
    """
    bin_path = ffmpeg_bin()
    if not bin_path:
        return True
    try:
        r = subprocess.run(
            [bin_path, "-nostdin", "-hide_banner", "-i", path, "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
        times = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
        if not times:
            return False
        h, mnt, s = times[-1]
        return int(h) * 3600 + int(mnt) * 60 + float(s) >= _PROBE_MIN_DECODED_S
    except Exception:
        return False


def _head_probe_sync(url: str, headers: dict) -> bool:
    probe_headers = {k: v for k, v in headers.items() if k.lower() != "range"}
    probe_headers["Range"] = f"bytes=0-{_PROBE_BYTES - 1}"
    tmp_path = ""
    try:
        if HAS_CURL_CFFI:
            resp = curl_requests.get(url, headers=probe_headers, impersonate="chrome",
                                     stream=True, timeout=(10, 60))
        else:
            client = httpx.Client(follow_redirects=True, timeout=30)
            resp = client.build_request("GET", url, headers=probe_headers)
            resp = client.send(resp, stream=True)
        buf = b""
        for chunk in resp.iter_content(256 * 1024):
            buf += chunk
            if len(buf) >= _PROBE_BYTES:
                break
        if hasattr(resp, "close"):
            resp.close()
        if not buf:
            return False
        if not ffmpeg_bin():
            # 仍执行不依赖 ffmpeg 的取流检查；缺少 ffmpeg 时无法执行解码校验，
            # 不判定音频损坏或因此强制降为 MP3，只要收到有效数据即放行
            return True
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".probe", delete=False) as f:
            f.write(buf)
            tmp_path = f.name
        return _decode_probe_ok(tmp_path)
    except Exception:
        return False
    finally:
        if tmp_path:
            with suppress(Exception):
                os.unlink(tmp_path)


async def _head_probe(url: str, headers: dict, song_id: str) -> bool:
    with _PROBE_CACHE_LOCK:
        cached = _PROBE_CACHE.get(song_id)
    if cached and (time.time() - cached[0]) < _PROBE_TTL:
        return cached[1]
    ok = await asyncio.to_thread(_head_probe_sync, url, headers)
    with _PROBE_CACHE_LOCK:
        _PROBE_CACHE[song_id] = (time.time(), ok)
    if not ok:
        logger.warning("stream head probe FAILED for %s (%s) — likely corrupt upstream bytes", song_id, url[:120])
    return ok


def _kuwo_force_mp3_url_sync(song_id: str) -> "tuple[str, dict] | None":
    """kuwo 官方 convert_url2 直取 320k mp3 档（无需登录，实测干净）。"""
    try:
        from musicdl.modules.sources.kuwo import KuwoMusicClientUtils
    except Exception:
        return None
    rid = song_id.split(":", 1)[1] if ":" in song_id else song_id
    query = (f"user=0&corp=kuwo&source=kwplayer_ar_5.1.0.0_B_jiakong_vh.apk"
             f"&p2p=1&type=convert_url2&sig=0&format=mp3&rid={rid}")
    try:
        enc = KuwoMusicClientUtils.encryptquery(query)
        if HAS_CURL_CFFI:
            resp = curl_requests.get(f"http://mobi.kuwo.cn/mobi.s?f=kuwo&q={enc}",
                                     headers={"User-Agent": "okhttp/3.10.0"}, timeout=10)
        else:
            with httpx.Client(timeout=10) as client:
                resp = client.get(f"http://mobi.kuwo.cn/mobi.s?f=kuwo&q={enc}",
                                  headers={"User-Agent": "okhttp/3.10.0"})
        m = re.search(r'http[^\s$\"]+', resp.text or "")
        if not m:
            return None
        return m.group(0), {"User-Agent": "okhttp/3.10.0"}
    except Exception as e:
        logger.warning("kuwo force-mp3 resolve failed for %s: %s", song_id, type(e).__name__)
        return None


async def _maybe_downgrade(id: str, entry: dict, url: str, quality: str,
                           range_header: "str | None") -> "tuple[str, dict]":
    """无损档坏流探测与 mp3 降级；返回最终 (url, 源站请求头)。"""
    ext = str(entry["item"].get("ext") or "").lower()
    want_mp3 = str(quality or "").strip().lower() == "mp3"
    if not want_mp3 and ext in _PROBE_EXTS:
        probe_headers = dict(entry.get("download_headers") or {})
        if not await _head_probe(url, probe_headers, id):
            logger.warning("lossless stream corrupt for %s (ext=%s), downgrading to mp3", id, ext)
            want_mp3 = True
    if not want_mp3:
        src_headers = dict(entry.get("download_headers") or {})
    else:
        forced = None
        if str(id).startswith("kuwo:"):
            forced = await asyncio.to_thread(_kuwo_force_mp3_url_sync, id)
            if forced:
                logger.warning("stream for %s downgraded to official mp3 tier", id)
        if forced:
            url, base_headers = forced
        else:
            base_headers = dict(entry.get("download_headers") or {})
        src_headers = base_headers
    if range_header:
        src_headers["Range"] = range_header
    return url, src_headers


async def _reject_unsupported_entry(song_id: str, entry: dict) -> None:
    """透传直链与 /stream 的共同前置检查。

    需要服务端后处理（DRM/play_auth）、非 HTTP 协议或加密容器的条目不能当作
    裸媒体转发。搜索初筛已经过滤过一次，这里在交付路径上再拦一次，避免客户端
    绕过搜索直接用 id 拿到无法播放的链接。
    """
    item = (entry or {}).get("item") or {}
    reason = search_core.media_unsupported_reason(item)
    if reason is None:
        return
    logger.info("refusing unsupported media for %s: %s", song_id, reason)
    raise HTTPException(409, f"unsupported media for {song_id!r}: {reason}")


async def _resolve_entry(song_id: str, auto_refresh: bool = True):
    entry = _cache_get(song_id)
    if entry is None:
        raise HTTPException(404, f"unknown song id {song_id!r}: cache expired or service restarted; re-search keyword and source once, then retry")
    await _reject_unsupported_entry(song_id, entry)
    fresh = entry["item"].get("download_url") and (time.time() - entry["ts"]) < CONF["url_ttl"]
    if fresh:
        return entry
    # URL 可能仍有效，探测一下
    if entry["item"].get("download_url"):
        head_headers = dict(entry.get("download_headers") or {})
        valid = await asyncio.to_thread(_head_probe_sync, entry["item"]["download_url"], head_headers)
        if valid:
            entry["ts"] = time.time()
            return entry
    if auto_refresh:
        refreshed = await _refresh_by_keyword(song_id)
        if refreshed:
            return refreshed
    raise HTTPException(502, f"playable URL expired for {song_id!r}; bounded refresh failed, re-search keyword and source")


def _internal_authorized(x_internal_token: str | None) -> bool:
    supplied = str(x_internal_token or "").strip()
    return bool(_INTERNAL_TOKEN and supplied and hmac.compare_digest(
        supplied.encode("utf-8"), _INTERNAL_TOKEN.encode("utf-8")))


async def _direct_resolve_entry(song_id: str) -> dict:
    """Resolve a cached known ID without opening a foreground audio stream."""
    key = (asyncio.get_running_loop(), str(song_id))
    task = _DIRECT_RESOLVE_INFLIGHT.get(key)
    if task is None or task.done():
        if len(_DIRECT_RESOLVE_INFLIGHT) >= 16:
            raise HTTPException(503, "direct resolution busy")

        async def resolve():
            entry = _cache_get(song_id)
            if entry is None:
                raise HTTPException(404, "unknown song id; re-search required")
            if entry["item"].get("download_url") and time.time() - entry["ts"] < CONF["url_ttl"]:
                return entry
            refreshed = await asyncio.wait_for(_refresh_by_keyword(song_id), timeout=16.0)
            if (not refreshed or not refreshed["item"].get("download_url")
                    or time.time() - refreshed["ts"] >= CONF["url_ttl"]):
                raise HTTPException(502, "direct resolution failed")
            return refreshed

        task = asyncio.create_task(resolve())
        _DIRECT_RESOLVE_INFLIGHT[key] = task

        def finish(done):
            if _DIRECT_RESOLVE_INFLIGHT.get(key) is done:
                _DIRECT_RESOLVE_INFLIGHT.pop(key, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finish)
    return await asyncio.shield(task)


def _fetch_upstream_stream_sync(url: str, headers: dict):
    """通过 curl_cffi 同步流式请求上游音频源（模拟 Chrome TLS 指纹）。"""
    return curl_requests.get(
        url,
        headers=headers,
        impersonate="chrome",
        stream=True,
        timeout=(10, 60),
    )


class _HttpxStreamWrapper:
    """包装 httpx 响应以对齐 curl_cffi 响应接口。"""
    def __init__(self, resp, client):
        self._resp = resp
        self._client = client
        self.status_code = resp.status_code
        self.headers = resp.headers

    def iter_content(self, chunk_size=64 * 1024):
        return self._resp.iter_bytes(chunk_size)

    def close(self):
        try:
            self._resp.close()
        finally:
            self._client.close()


async def _fetch_upstream_stream(url: str, src_headers: dict):
    """请求源站流。优先走 curl_cffi 工作线程，fallback 走 httpx。"""
    src_headers = {k: v for k, v in src_headers.items() if k.lower() != "accept-encoding"}
    src_headers["Accept-Encoding"] = "identity"
    if HAS_CURL_CFFI:
        resp = await asyncio.to_thread(_fetch_upstream_stream_sync, url, src_headers)
    else:
        def _fetch_httpx_sync():
            client = httpx.Client(follow_redirects=True, timeout=30)
            try:
                req = client.build_request("GET", url, headers=src_headers)
                response = client.send(req, stream=True)
                return _HttpxStreamWrapper(response, client)
            except BaseException:
                client.close()
                raise
        resp = await asyncio.to_thread(_fetch_httpx_sync)
    encoding = resp.headers.get("Content-Encoding") or resp.headers.get("content-encoding") or "identity"
    if encoding.strip().lower() != "identity":
        resp.close()
        raise HTTPException(502, "source ignored identity encoding; refusing decoded bytes with encoded length/range")
    return resp


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时打印一次配置摘要
    logger.info("=== musicdl-service configuration ===")
    for k, v in CONF.items():
        logger.info("  %s = %s", k, v)
    logger.info("  MUSICDL_SOURCES origin = %s", _SOURCES_ORIGIN)
    logger.info("  HAS_CURL_CFFI = %s", HAS_CURL_CFFI)
    logger.info("=====================================")

    app.state.http = httpx.AsyncClient(follow_redirects=True, timeout=30)
    try:
        yield
    finally:
        direct_tasks = [task for task in _DIRECT_RESOLVE_INFLIGHT.values()
                        if task.get_loop() is asyncio.get_running_loop()]
        for task in direct_tasks:
            task.cancel()
        if direct_tasks:
            await asyncio.gather(*direct_tasks, return_exceptions=True)
        await SINGLE_FLIGHT.aclose()
        try:
            await SOURCE_WORKERS.aclose()
        finally:
            await app.state.http.aclose()


app = FastAPI(title="musicdl-service", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "sources": CONF["sources"],
        "cache_size": len(_SONG_CACHE),
        "cache_entries": len(SEARCH_CACHE),
        "breaker_open": SOURCE_BREAKER.get_open_sources(),
        "adaptive_timeouts": ADAPTIVE.stats(),
        "workers": SOURCE_WORKERS.stats(),
        "stats": _STATS,
    }


@app.get("/sources")
async def sources():
    registered: list[str] = []
    try:
        from musicdl.modules.sources import MusicClientBuilder

        registered = sorted(MusicClientBuilder.REGISTERED_MODULES.keys())
    except Exception as exc:
        logger.warning("Failed to list registered musicdl sources: %s", exc)
        try:
            from musicdl import musicdl as _mdl

            registered = sorted(getattr(_mdl, "SUPPORTED_MUSIC_SOURCES", []) or [])
        except Exception:
            registered = list(CONF["sources"])

    return {
        "enabled": CONF["sources"],
        "registered": registered,
    }


@app.get("/search")
async def search(
    keyword: str = Query(..., min_length=1),
    limit: int = Query(None, ge=1, le=30),
    sources: str = Query("", description="逗号分隔的源名(短名或全名)，空=用默认白名单"),
):
    if limit is None:
        limit = CONF["limit_per_source"]

    raw_src_list = CONF["sources"]
    if sources:
        raw_src_list = [_source_client(s) for s in sources.split(",") if s.strip()]
    raw_src_list = list(dict.fromkeys(raw_src_list))
    sources_key = ",".join(sorted(raw_src_list))
    cache_key = f"{sources_key}|limit={limit}"
    _STATS["searches"] += 1

    async def _do_search() -> dict:
        # 1. 过滤熔断中的源
        active_src_list = [s for s in raw_src_list if not SOURCE_BREAKER.is_open(s)]

        # 2. 查缓存（命中直接返回）
        cached = SEARCH_CACHE.get(keyword, cache_key)
        # Search results alone cannot serve playback after song-cache eviction.
        if cached is not None and all(_cache_get(item["id"]) for item in cached):
            return {
                "ok": True,
                "cached": True,
                "keyword": keyword,
                "items": cached,
                "errors": {},
            }

        # 全被熔断且无缓存，直接返回空结果（不报错）
        if not active_src_list:
            return {
                "ok": True,
                "cached": False,
                "keyword": keyword,
                "items": [],
                "errors": {s: "circuit breaker open" for s in raw_src_list},
            }

        progress_by_source = {}

        def collect(source):
            progress = progress_by_source.get(source)
            items = []
            for item, headers, lyric in progress.finish() if progress else []:
                _cache_put(item, keyword, headers, lyric)
                items.append(item)
            return items

        async def one(source: str):
            timeout = ADAPTIVE.timeout_for(source)
            started = time.monotonic()
            progress = SearchProgress(limit, started + timeout)
            progress_by_source[source] = progress
            fetch_size = max(limit * 2, 10)
            payload = _search_payload(source, keyword, fetch_size, limit, None, timeout)

            def on_entry(entry):
                # worker 每确认一首即上交：[item, download_headers, lyric]
                progress.append((entry[0], entry[1], entry[2]))

            try:
                result = await SOURCE_WORKERS.run(source, payload, timeout=timeout, on_entry=on_entry)
                progress.partial = bool(result.get("partial"))
                latency = time.monotonic() - started
                items = collect(source)
                if progress.partial:
                    # 已经交出歌就不要再把超时往下压。压到搜不完一页后，下次还是 0 条。
                    if not items:
                        ADAPTIVE.record_failure(source)
                    return source, items, "partial results (probe deadline reached)"
                ADAPTIVE.record_success(source, latency)
                if items:
                    SOURCE_BREAKER.record_success(source, latency)
                else:
                    _note_empty_source_result(source, SOURCE_BREAKER, ADAPTIVE)
                return source, items, None
            except SourceBusy as exc:
                # Admission rejection is not another upstream failure.
                return source, collect(source), str(exc)
            except asyncio.TimeoutError:
                items = collect(source)
                if not items:
                    SOURCE_BREAKER.record_failure(source)
                    ADAPTIVE.record_failure(source)
                suffix = " (partial results)" if items else ""
                return source, items, f"timeout after {timeout:.1f}s{suffix}"
            except asyncio.CancelledError:
                # The aggregate owner collects this closed snapshot on its
                # global/grace deadline, never the still-running worker.
                progress.finish()
                raise
            except Exception as e:  # 单源失败不影响其他源
                _STATS["errors"] += 1
                SOURCE_BREAKER.record_failure(source)
                return source, collect(source), f"{type(e).__name__}: {e}"

        results: list = []
        pending = {asyncio.create_task(one(s), name=s): s for s in active_src_list}
        overall = max(float(CONF["search_timeout"]), 4.0)
        deadline = time.monotonic() + overall
        skipped_fast_return = False
        try:
            grace_limited = False
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                done, still = await asyncio.wait(
                    pending.keys(), timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    pending.pop(task, None)
                    try:
                        results.append(task.result())
                    except Exception as e:  # noqa: BLE001
                        _STATS["errors"] += 1
                        results.append((task.get_name(), [], f"{type(e).__name__}: {e}"))
                if not pending:
                    break
                total_items = sum(len(items) for _, items, _ in results)
                # 结果已足够优质：不再等待慢源，立即返回
                if total_items >= CONF["fast_return_items"]:
                    skipped_fast_return = True
                    break
                # 已有任意源出结果：再给其余源最多 slow_grace_s 秒，避免慢源拖死整页
                if total_items > 0:
                    grace_limited = True
                    deadline = min(deadline, time.monotonic() + CONF["slow_grace_s"])
        finally:
            for task in pending:
                task.cancel()
            if pending:
                cleanup = asyncio.gather(*pending, return_exceptions=True)
                interrupted = False
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        interrupted = True
                if interrupted:
                    raise asyncio.CancelledError
        if pending:
            skip_reason = ("skipped (fast return)" if skipped_fast_return else
                           "skipped (grace elapsed)" if grace_limited else f"timeout after {overall}s")
            for task, src in pending.items():
                items = collect(src)
                results.append((src, items, skip_reason + (" (partial results)" if items else "")))
                # 没等到歌才收紧。已经有结果、或因为别的源够了而提前返回，都不是这个源坏了。
                if not items and not skipped_fast_return and not grace_limited:
                    ADAPTIVE.record_failure(src)
                    SOURCE_BREAKER.record_failure(src)

        all_items = []
        seen_ids = set()
        for _, items, _ in results:
            for item in items:
                if item["id"] not in seen_ids:
                    seen_ids.add(item["id"])
                    all_items.append(item)

        errors = {src: err for src, _, err in results if err}
        for s in raw_src_list:
            if s not in active_src_list:
                errors.setdefault(s, "circuit breaker open")
        # Partial/error results must remain retryable, not become a long-lived
        # apparently complete cache hit with errors silently removed.
        if not errors:
            if all_items:
                SEARCH_CACHE.put(keyword, cache_key, all_items)
            else:
                SEARCH_CACHE.put(keyword, cache_key, [], ttl=CONF["neg_cache_ttl"])

        return {
            "ok": True,
            "cached": False,
            "keyword": keyword,
            "items": all_items,
            "errors": errors,
        }

    return await SINGLE_FLIGHT.run((keyword, cache_key), _do_search)


@app.get("/info")
async def info(id: str = Query(..., min_length=1)):
    entry = _cache_get(id)
    if entry is None:
        raise HTTPException(404, f"song id {id!r} not found in cache: cache expired or service restarted; re-search keyword and source once, then retry")
    item = entry["item"]
    return {
        "ok": True,
        "id": item.get("id"),
        "source": item.get("source"),
        "title": item.get("title"),
        "artist": item.get("artist"),
        "album": item.get("album"),
        "duration_s": item.get("duration_s"),
        "ext": item.get("ext"),
        # 实际协议与交付形态：让上层能在取流之前判断是否需要专门处理
        "protocol": item.get("protocol", ""),
        "delivery": item.get("delivery", ""),
        # 实际品质未知时如实报 unknown，不按扩展名猜 Hi-Res/母带
        "quality": item.get("quality", ""),
        "bitrate": item.get("bitrate", 0),
        "samplerate": item.get("samplerate", 0),
        "file_size": item.get("file_size"),
        "cover_url": item.get("cover_url"),
        "lyric": entry.get("lyric", ""),
    }


@app.get("/stream")
async def stream(
    id: str = Query(...),
    proxy: bool = Query(False, description="true=字节流透传而非302"),
    quality: str = Query("", description="mp3=跳过无损档直接取有损直链（上游无损流损坏时上层降级用）"),
    range_header: str | None = Header(None, alias="Range", description="客户端请求头中的 Range"),
):
    entry = await _resolve_entry(id)
    url = entry["item"].get("download_url")
    if not url:
        raise HTTPException(502, f"no playable url for {id}")
    url, src_headers = await _maybe_downgrade(id, entry, url, quality, range_header)
    if not proxy:
        return RedirectResponse(url, status_code=302)

    try:
        resp = await _fetch_upstream_stream(url, src_headers)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Stream request failed for %s (%s), attempting refresh: %s", id, url, e)
        refreshed_entry = await _refresh_by_keyword(id)
        if refreshed_entry and refreshed_entry["item"].get("download_url"):
            entry = refreshed_entry
            url = entry["item"]["download_url"]
            url, src_headers = await _maybe_downgrade(id, entry, url, quality, range_header)
            resp = await _fetch_upstream_stream(url, src_headers)
        else:
            raise HTTPException(502, f"failed to fetch stream from source for {id}: {e}")

    # 若源站返回 4xx/5xx，尝试刷新一次
    if resp.status_code >= 400:
        if hasattr(resp, "close"):
            resp.close()
        logger.warning("Source returned %s for %s, attempting refresh...", resp.status_code, id)
        refreshed_entry = await _refresh_by_keyword(id)
        if refreshed_entry and refreshed_entry["item"].get("download_url") and refreshed_entry["item"]["download_url"] != url:
            entry = refreshed_entry
            url = entry["item"]["download_url"]
            url, src_headers = await _maybe_downgrade(id, entry, url, quality, range_header)
            resp = await _fetch_upstream_stream(url, src_headers)
            if resp.status_code >= 400:
                status = resp.status_code
                if hasattr(resp, "close"):
                    resp.close()
                raise HTTPException(502, f"source returned {status} for {id} even after refresh")
        else:
            raise HTTPException(502, f"source returned {resp.status_code} for {id}")

    out_headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    for k in ("Content-Type", "Content-Length", "Content-Range"):
        val = resp.headers.get(k) or resp.headers.get(k.lower())
        if val:
            out_headers[k] = val

    def gen():
        try:
            for chunk in resp.iter_content(64 * 1024):
                if chunk:
                    yield chunk
        finally:
            if hasattr(resp, "close"):
                resp.close()

    return StreamingResponse(
        gen(),
        status_code=206 if "Content-Range" in out_headers or resp.status_code == 206 else 200,
        headers=out_headers,
    )


@app.get("/internal/resolve")
async def internal_resolve(
    id: str = Query(..., min_length=1),
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
):
    """Return a protected URL description for proxy-side direct playback."""
    if not _internal_authorized(x_internal_token):
        raise HTTPException(503, "internal authentication unavailable")
    entry = await _direct_resolve_entry(id)
    await _reject_unsupported_entry(id, entry)
    item = entry.get("item") or {}
    url = str(item.get("download_url") or "")
    if not url:
        raise HTTPException(502, "playable URL unavailable")
    now = time.time()
    reuse_deadline = now + max(0.0, float(CONF["url_ttl"]) - max(0.0, now - float(entry.get("ts") or now)))
    description = {
        "ok": True,
        "provider": "musicdl",
        "platform": str(item.get("source") or ""),
        "canonical_id": str(item.get("id") or id),
        "url": url,
        "server_headers": dict(entry.get("download_headers") or {}),
        "protocol": str(item.get("protocol") or ""),
        "ext": str(item.get("ext") or ""),
        "actual_quality": str(item.get("quality") or ""),
        "file_size": item.get("file_size"),
        "resolved_at": now,
        "reuse_deadline": reuse_deadline,
        "upstream_expires_at": None,
        "expiry_source": "policy",
    }
    for selection in list(_DIRECT_SELECTIONS):
        if _DIRECT_SELECTIONS[selection]["reuse_deadline"] <= now:
            _DIRECT_SELECTIONS.pop(selection, None)
    while len(_DIRECT_SELECTIONS) >= 512:
        _DIRECT_SELECTIONS.pop(next(iter(_DIRECT_SELECTIONS)))
    selection = secrets.token_urlsafe(24)
    description["selection_id"] = selection
    _DIRECT_SELECTIONS[selection] = description
    return description


@app.get("/internal/stream")
async def internal_stream(
    selection: str = Query(..., min_length=1),
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
):
    if not _internal_authorized(x_internal_token):
        raise HTTPException(503, "internal authentication unavailable")
    selected = _DIRECT_SELECTIONS.get(selection)
    if selected is None or selected["reuse_deadline"] <= time.time():
        raise HTTPException(409, "selection expired; restart required")
    headers = {key: value for key, value in selected["server_headers"].items() if key.lower() != "range"}
    headers["Accept-Encoding"] = "identity"
    try:
        upstream = await _fetch_upstream_stream(selected["url"], headers)
    except Exception:
        raise HTTPException(502, "selected media unavailable") from None
    if upstream.status_code != 200:
        upstream.close()
        raise HTTPException(502, "selected media unavailable")
    output = {key: value for key, value in upstream.headers.items()
              if key.lower() in ("content-type", "content-length", "content-encoding")}

    def body():
        try:
            yield from upstream.iter_content(256 * 1024)
        finally:
            upstream.close()

    return StreamingResponse(body(), headers=output)
