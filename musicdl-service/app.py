"""musicdl HTTP 服务：把 musicdl 库包装成共享音源 API.

统一曲目 ID 契约: "<source>:<identifier>"，例如 "kuwo:228908"。
两个消费方（fnmusic-ext 代理、music-box）都以该 ID 串通信。
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager

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
from hardening import AdaptiveTimeout, SearchCache, SourceBreaker, SingleFlight

CONF = {
    "sources": [
        s.strip()
        for s in os.environ.get(
            "MUSICDL_SOURCES", "KuwoMusicClient,MiguMusicClient"
        ).split(",")
        if s.strip()
    ],
    "search_timeout": float(os.environ.get("MUSICDL_SEARCH_TIMEOUT", "12")),
    "limit_per_source": int(os.environ.get("MUSICDL_LIMIT_PER_SOURCE", "10")),
    "url_ttl": int(os.environ.get("MUSICDL_URL_TTL", "1800")),
    "cache_max": int(os.environ.get("MUSICDL_CACHE_MAX", "3000")),
    "work_dir": os.environ.get("MUSICDL_WORK_DIR", "/tmp/musicdl_outputs"),
    "search_cache_ttl": int(os.environ.get("MUSICDL_SEARCH_CACHE_TTL", "300")),
    "search_cache_max": int(os.environ.get("MUSICDL_SEARCH_CACHE_MAX", "200")),
    "breaker_threshold": int(os.environ.get("MUSICDL_BREAKER_THRESHOLD", "4")),
    "breaker_cooldown": int(os.environ.get("MUSICDL_BREAKER_COOLDOWN", "120")),
    "search_workers": int(os.environ.get("MUSICDL_SEARCH_WORKERS", "6")),
    "neg_cache_ttl": int(os.environ.get("MUSICDL_NEG_TTL", "30")),
    # 自适应熔断/降级
    "slow_degrade_s": float(os.environ.get("MUSICDL_SLOW_DEGRADE_S", "8")),
    "adaptive_min_timeout": float(os.environ.get("MUSICDL_ADAPTIVE_MIN_TIMEOUT", "3")),
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
SOURCE_EXECUTOR = ThreadPoolExecutor(
    max_workers=CONF["search_workers"],
    thread_name_prefix="srcsearch",
)

# id -> {"item": {...}, "keyword": str, "download_headers": dict, "lyric": str, "ts": float}
_SONG_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_STATS = {"searches": 0, "errors": 0}


def _source_short(client_name: str) -> str:
    return (client_name or "").replace("MusicClient", "").lower()


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


def _search_one_source(source: str, keyword: str, limit: int) -> list:
    """单个源搜索（工作线程内执行，阻塞）。"""
    client = musicdl.MusicClient(
        music_sources=[source],
        init_music_clients_cfg={
            source: {
                "search_size_per_source": max(limit, 5),
                "work_dir": CONF["work_dir"],
            },
        },
    )
    result = client.search(keyword=keyword)
    return list(result.values())[0] if result else []


def _normalize(song, keyword: str) -> dict:
    src = _source_short(getattr(song, "source", "") or "")
    sid = str(getattr(song, "identifier", "") or "")
    return {
        "id": f"{src}:{sid}",
        "source": src,
        "title": getattr(song, "song_name", "") or "",
        "artist": getattr(song, "singers", "") or "",
        "album": getattr(song, "album", "") or "",
        "duration_s": getattr(song, "duration_s", 0) or 0,
        "ext": getattr(song, "ext", "") or "mp3",
        "file_size": getattr(song, "file_size_bytes", 0) or 0,
        "cover_url": getattr(song, "cover_url", "") or "",
        "download_url": getattr(song, "download_url", "") or "",
    }


TRIAL_MARKERS = (
    "(试听)",
    "（试听）",
    "试听片段",
    "片段试听",
    "试听版",
    "[试听]",
    "【试听】",
    "- 试听",
    " - 试听",
)


def _is_candidate_playable(song, item: dict) -> bool:
    """初筛：检查 identifier、标题试听标记、download_url 存在且非错误页。"""
    sid = item.get("id") or ""
    if not sid or sid.endswith(":"):
        return False
    title = str(item.get("title") or "")
    if not title:
        return False
    if any(marker in title for marker in TRIAL_MARKERS):
        return False
    url = str(item.get("download_url") or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return False
    if "404/error.html" in url or "error.html" in url:
        return False
    return True


def _probe_playable_sync(url: str, headers: dict) -> bool:
    """探测直链是否有效且真实可播放（过滤无效/试听/404直链/HTML错误页）。"""
    if not url or not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    if "404/error.html" in url or "error.html" in url:
        return False
    hdrs = dict(headers or {})
    if HAS_CURL_CFFI:
        try:
            r = curl_requests.head(
                url,
                headers=hdrs,
                impersonate="chrome",
                allow_redirects=True,
                timeout=5,
            )
            if r.status_code == 405:
                hdrs["Range"] = "bytes=0-1"
                r = curl_requests.get(
                    url,
                    headers=hdrs,
                    impersonate="chrome",
                    allow_redirects=True,
                    stream=True,
                    timeout=5,
                )
            if r.status_code in (200, 206):
                ct = (r.headers.get("content-type") or "").lower()
                if "text/html" in ct:
                    return False
                return True
            return False
        except Exception:
            return False
    else:
        try:
            with httpx.Client(follow_redirects=True, timeout=5.0) as cx:
                r = cx.head(url, headers=hdrs)
                if r.status_code == 405:
                    hdrs["Range"] = "bytes=0-1"
                    r = cx.get(url, headers=hdrs)
                if r.status_code in (200, 206):
                    ct = (r.headers.get("content-type") or "").lower()
                    if "text/html" in ct:
                        return False
                    return True
                return False
        except Exception:
            return False


_head_probe_sync = _probe_playable_sync


async def _refresh_by_keyword(song_id: str) -> dict | None:
    """URL 过期或下载失败后按缓存的关键词重搜一次，找回同 ID 的曲目。"""
    entry = _cache_get(song_id)
    if not entry or not entry.get("keyword"):
        return None
    src_short = entry["item"].get("source", "")
    src_client = src_short.capitalize() + "MusicClient"
    try:
        limit = max(CONF["limit_per_source"], 10)
        loop = asyncio.get_running_loop()
        songs = await asyncio.wait_for(
            loop.run_in_executor(
                SOURCE_EXECUTOR, _search_one_source, src_client, entry["keyword"], limit
            ),
            timeout=ADAPTIVE.timeout_for(src_client),
        )
    except Exception:
        return None
    for song in songs:
        item = _normalize(song, entry["keyword"])
        if item["id"] == song_id:
            if not _is_candidate_playable(song, item):
                continue
            headers = getattr(song, "default_download_headers", {}) or {}
            valid = await asyncio.to_thread(_probe_playable_sync, item["download_url"], headers)
            if not valid:
                continue
            _cache_put(
                item,
                entry["keyword"],
                headers,
                str(getattr(song, "lyric", "") or ""),
            )
            return _cache_get(song_id)
    return None


async def _resolve_entry(song_id: str, auto_refresh: bool = True):
    entry = _cache_get(song_id)
    if entry is None:
        raise HTTPException(404, f"unknown song id {song_id!r}, search it first")
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
        entry = await _refresh_by_keyword(song_id) or entry
    return entry


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
    def __init__(self, resp):
        self._resp = resp
        self.status_code = resp.status_code
        self.headers = resp.headers

    def iter_content(self, chunk_size=64 * 1024):
        return self._resp.iter_bytes(chunk_size)

    def close(self):
        self._resp.close()


async def _fetch_upstream_stream(url: str, src_headers: dict):
    """请求源站流。优先走 curl_cffi 工作线程，fallback 走 httpx。"""
    if HAS_CURL_CFFI:
        return await asyncio.to_thread(_fetch_upstream_stream_sync, url, src_headers)
    else:
        def _fetch_httpx_sync():
            client = httpx.Client(follow_redirects=True, timeout=30)
            req = client.build_request("GET", url, headers=src_headers)
            resp = client.send(req, stream=True)
            return _HttpxStreamWrapper(resp)
        return await asyncio.to_thread(_fetch_httpx_sync)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时打印一次配置摘要
    logger.info("=== musicdl-service configuration ===")
    for k, v in CONF.items():
        logger.info("  %s = %s", k, v)
    logger.info("  HAS_CURL_CFFI = %s", HAS_CURL_CFFI)
    logger.info("=====================================")

    app.state.http = httpx.AsyncClient(follow_redirects=True, timeout=30)
    try:
        yield
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
        raw_src_list = [
            s if s.endswith("MusicClient") else s.capitalize() + "MusicClient"
            for s in sources.split(",")
            if s.strip()
        ]
    sources_key = ",".join(sorted(raw_src_list))
    _STATS["searches"] += 1

    async def _do_search() -> dict:
        # 1. 过滤熔断中的源
        active_src_list = [s for s in raw_src_list if not SOURCE_BREAKER.is_open(s)]

        # 2. 查缓存（命中直接返回）
        cached = SEARCH_CACHE.get(keyword, sources_key)
        if cached is not None:
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

        async def one(source: str):
            loop = asyncio.get_running_loop()
            timeout = ADAPTIVE.timeout_for(source)
            started = time.monotonic()
            fetch_size = max(limit * 2, 10)
            try:
                songs = await asyncio.wait_for(
                    loop.run_in_executor(
                        SOURCE_EXECUTOR, _search_one_source, source, keyword, fetch_size
                    ),
                    timeout=timeout,
                )
                latency = time.monotonic() - started
                ADAPTIVE.record_success(source, latency)

                async def _probe_one(s) -> dict | None:
                    it = _normalize(s, keyword)
                    if not _is_candidate_playable(s, it):
                        return None
                    hdrs = getattr(s, "default_download_headers", {}) or {}
                    ok = await asyncio.to_thread(_probe_playable_sync, it["download_url"], hdrs)
                    if not ok:
                        return None
                    return it

                candidates = [s for s in songs if s]
                probed = await asyncio.gather(
                    *[_probe_one(s) for s in candidates[:fetch_size]],
                    return_exceptions=True,
                )
                items = []
                for s, res in zip(candidates[:fetch_size], probed):
                    if isinstance(res, dict) and res.get("id"):
                        _cache_put(
                            res,
                            keyword,
                            getattr(s, "default_download_headers", {}) or {},
                            str(getattr(s, "lyric", "") or ""),
                        )
                        items.append(res)
                        if len(items) >= limit:
                            break
                if items:
                    # 慢响应（超过 slow_degrade_s）计入降级失败，持续慢源会被熔断
                    SOURCE_BREAKER.record_success(source, latency)
                return source, items, None
            except asyncio.TimeoutError:
                SOURCE_BREAKER.record_failure(source)
                ADAPTIVE.record_failure(source)
                return source, [], f"timeout after {timeout:.1f}s"
            except Exception as e:  # 单源失败不影响其他源
                _STATS["errors"] += 1
                SOURCE_BREAKER.record_failure(source)
                return source, [], f"{type(e).__name__}: {e}"

        results: list = []
        pending = {asyncio.create_task(one(s), name=s): s for s in active_src_list}
        overall = max(float(CONF["search_timeout"]), 4.0)
        deadline = time.monotonic() + overall
        skipped_fast_return = False
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
                deadline = min(deadline, time.monotonic() + CONF["slow_grace_s"])
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            skip_reason = "skipped (fast return)" if skipped_fast_return else f"timeout after {overall}s"
            for task, src in pending.items():
                results.append((src, [], skip_reason))
                # 被放弃的慢源：收紧自适应超时；若是全局超时放弃，同时计入熔断失败
                ADAPTIVE.record_failure(src)
                if not skipped_fast_return:
                    SOURCE_BREAKER.record_failure(src)

        all_items = []
        seen_ids = set()
        for _, items, _ in results:
            for item in items:
                if item["id"] not in seen_ids:
                    seen_ids.add(item["id"])
                    all_items.append(item)

        if all_items:
            SEARCH_CACHE.put(keyword, sources_key, all_items)
        else:
            SEARCH_CACHE.put(keyword, sources_key, [], ttl=CONF["neg_cache_ttl"])

        errors = {src: err for src, _, err in results if err}
        # 被熔断跳过的源也在 errors 中体现
        for s in raw_src_list:
            if s not in active_src_list:
                errors.setdefault(s, "circuit breaker open")

        return {
            "ok": True,
            "cached": False,
            "keyword": keyword,
            "items": all_items,
            "errors": errors,
        }

    return await SINGLE_FLIGHT.run(f"{keyword}|{sources_key}", _do_search)


@app.get("/info")
async def info(id: str = Query(..., min_length=1)):
    entry = _cache_get(id)
    if entry is None:
        raise HTTPException(404, f"song id {id!r} not found in cache")
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
        "file_size": item.get("file_size"),
        "cover_url": item.get("cover_url"),
        "lyric": entry.get("lyric", ""),
    }


@app.get("/stream")
async def stream(
    id: str = Query(...),
    proxy: bool = Query(False, description="true=字节流透传而非302"),
    range_header: str | None = Header(None, alias="Range", description="客户端请求头中的 Range"),
):
    entry = await _resolve_entry(id)
    url = entry["item"].get("download_url")
    if not url:
        raise HTTPException(502, f"no playable url for {id}")
    if not proxy:
        return RedirectResponse(url, status_code=302)

    src_headers = dict(entry.get("download_headers") or {})
    if range_header:
        src_headers["Range"] = range_header

    try:
        resp = await _fetch_upstream_stream(url, src_headers)
    except Exception as e:
        logger.warning("Stream request failed for %s (%s), attempting refresh: %s", id, url, e)
        refreshed_entry = await _refresh_by_keyword(id)
        if refreshed_entry and refreshed_entry["item"].get("download_url"):
            entry = refreshed_entry
            url = entry["item"]["download_url"]
            src_headers = dict(entry.get("download_headers") or {})
            if range_header:
                src_headers["Range"] = range_header
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
            src_headers = dict(entry.get("download_headers") or {})
            if range_header:
                src_headers["Range"] = range_header
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
