"""lxmusic-service/app.py
fnmusic-ext 洛雪音源适配服务 (基于 lxserver 后端)。

契约说明：
1. 保持对 proxy 和 WebUI 的既有 10 个 HTTP API 契约完全不变；
2. 内部通过 lxserver_client 调用容器内本地运行的 lxserver (端口 8005)；
3. 本模块负责曲目 ID (lx:<src>:<id>) 映射、音质降档、Range 魔数探活 (probe_url)、
   熔断防护 (_chain_*)、搜索并发门闸 (LxSearchGate) 以及旧版状态兼容。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import json
import logging
import os
import re
import time
from typing import Any
import urllib.parse

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
import httpx
from pydantic import BaseModel

from lxserver_client import (
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_LXSERVER_URL,
    LxServerClient,
    SUPPORTED_PLATFORMS,
    map_lxserver_song,
    normalize_source,
    parse_interval_to_seconds,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("fnmusic.lxmusic")

SERVICE_VERSION = "2.7.0"
UA_PC = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

# 配置字典
CONF: dict[str, Any] = {
    "sources": [s.strip() for s in os.environ.get("LX_SOURCES", "kg,wy,mg,kw,tx").split(",") if s.strip()],
    "search_timeout": float(os.environ.get("LX_SEARCH_TIMEOUT", "12.0")),
    "limit_per_source": int(os.environ.get("LX_LIMIT_PER_SOURCE", "20")),
    "url_timeout": float(os.environ.get("LX_URL_TIMEOUT", "20.0")),
    "resolver_timeout": float(os.environ.get("LX_RESOLVER_TIMEOUT", "12.0")),
    "cache_max": int(os.environ.get("LX_CACHE_MAX", "2000")),
    "cache_ttl": float(os.environ.get("LX_CACHE_TTL", "604800.0")),
    "probe_timeout": float(os.environ.get("LX_PROBE_TIMEOUT", "5.0")),
    "probe_fresh_s": float(os.environ.get("LX_PROBE_FRESH_S", "900.0")),
    "search_probe": bool(os.environ.get("FNMUSIC_SEARCH_PROBE", "").lower() in ("1", "true", "yes", "on")),
    "data_dir": os.environ.get("LX_DATA_DIR", "/data/lxmusic"),
}

# 全局客户端实例
LXSERVER = LxServerClient(
    base_url=os.environ.get("LXSERVER_URL", DEFAULT_LXSERVER_URL),
    admin_password=os.environ.get("LXSERVER_ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD),
    timeout=CONF["resolver_timeout"],
)

# 内存曲目缓存: id -> {"item": dict, "ts": float}
_SONG_CACHE: dict[str, dict] = {}
_STATS = {"searches": 0, "url_resolutions": 0, "errors": 0}

# 探活前缀字节数
_PROBE_BYTES = 4096

_SEARCH_PROBE_ENABLED: ContextVar[bool | None] = ContextVar("lx_search_probe_enabled", default=None)


def is_probe_enabled() -> bool:
    v = _SEARCH_PROBE_ENABLED.get()
    if v is not None:
        return v
    return bool(CONF.get("search_probe", False))


def parse_track_id(track_id: str) -> tuple[str, str]:
    """解析 "lx:<source>:<identifier>" -> ("kg", "<identifier>")；异常时返回 ("", "")."""
    parts = (track_id or "").strip().split(":", 2)
    if len(parts) == 3 and parts[0] == "lx":
        src = normalize_source(parts[1])
        if src and parts[2]:
            return src, parts[2]
    if len(parts) == 2:
        src = normalize_source(parts[0])
        if src and parts[1]:
            return src, parts[1]
    return "", ""


def _cache_put(item: dict) -> None:
    track_id = item.get("id")
    if not track_id:
        return
    if len(_SONG_CACHE) >= CONF["cache_max"]:
        # 清理最早的一半
        for k in sorted(_SONG_CACHE, key=lambda k: _SONG_CACHE[k]["ts"])[: len(_SONG_CACHE) // 2]:
            _SONG_CACHE.pop(k, None)
    _SONG_CACHE[track_id] = {"item": item, "ts": time.time()}


def _cache_get(track_id: str) -> dict | None:
    entry = _SONG_CACHE.get(track_id)
    if not entry:
        return None
    if time.time() - entry["ts"] > CONF["cache_ttl"]:
        _SONG_CACHE.pop(track_id, None)
        return None
    return entry["item"]


def _quality_tiers(quality: str) -> list[str]:
    q = (quality or "").strip().lower()
    if q in ("lossless", "flac", "sq", "hires", "hr", "master"):
        return ["lossless", "high", "standard"]
    if q in ("high", "320", "exhigh", "hq"):
        return ["high", "standard"]
    return ["standard"]


def _tier_to_lx_quality(tier: str) -> str:
    if tier == "lossless":
        return "flac"
    if tier == "high":
        return "320k"
    return "128k"


# ------------------------------------------------------------- 媒体流探活 --

def _media_signature(body: bytes) -> str:
    """签名嗅探：通过前缀魔数识别音频格式。"""
    if body.startswith(b"fLaC"):
        return "flac"
    if body.startswith(b"ID3"):
        return "mp3"
    if body.startswith(b"OggS"):
        return "ogg"
    if len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WAVE":
        return "wav"
    if len(body) >= 12 and body[4:8] == b"ftyp":
        return "m4a"
    if len(body) >= 4 and body[0] == 0xFF:
        if body[1] & 0xF6 == 0xF0:  # ADTS AAC
            return "aac"
        if (
            body[1] & 0xE0 == 0xE0
            and body[1] & 6
            and body[2] & 0xF0 not in (0, 0xF0)
            and body[2] & 12 != 12
        ):
            return "mp3"
    return ""


def _content_total_size(resp: httpx.Response) -> int:
    cr = resp.headers.get("content-range") or ""
    if "/" in cr:
        try:
            return int(cr.split("/")[-1])
        except ValueError:
            pass
    try:
        return int(resp.headers.get("content-length") or 0)
    except ValueError:
        return 0


async def probe_url(
    client: httpx.AsyncClient,
    url: str,
    headers: dict | None = None,
) -> tuple[bool, str, str, int]:
    """有界流式前缀探活。返回 (是否有效, 最终URL, MIME/扩展名, 总大小)。"""
    h = {k: v for k, v in (headers or {}).items() if k.lower() not in ("range", "accept-encoding")}
    h.setdefault("User-Agent", UA_PC)
    h.update({"Range": f"bytes=0-{_PROBE_BYTES - 1}", "Accept-Encoding": "identity"})
    try:
        async with asyncio.timeout(CONF["probe_timeout"]):
            curr_url = url
            for _ in range(6):  # 最多 5 次重定向
                async with client.stream(
                    "GET", curr_url, headers=h, follow_redirects=False, timeout=CONF["probe_timeout"]
                ) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        location = r.headers.get("location")
                        if not location:
                            return False, str(r.url), "", 0
                        target = r.url.join(location)
                        if target.scheme not in ("http", "https"):
                            return False, str(r.url), "", 0
                        if target.host != r.url.host:
                            h = {
                                k: v
                                for k, v in h.items()
                                if k.lower() not in ("authorization", "cookie", "host")
                            }
                        curr_url = str(target)
                        continue

                    ct = (r.headers.get("content-type") or "").lower()
                    if r.status_code not in (200, 206):
                        return False, str(r.url), ct, 0
                    if (
                        ct.startswith("text/")
                        or "json" in ct
                        or "xml" in ct
                        or r.headers.get("content-encoding", "identity") != "identity"
                    ):
                        return False, str(r.url), ct, 0

                    prefix = bytearray()
                    if r.is_stream_consumed:
                        prefix.extend(r.content[:_PROBE_BYTES])
                    else:
                        async for chunk in r.aiter_raw():
                            prefix.extend(chunk[: _PROBE_BYTES - len(prefix)])
                            ext = _media_signature(prefix)
                            if ext or len(prefix) >= _PROBE_BYTES:
                                break
                    ext = _media_signature(prefix)
                    return (
                        bool(ext),
                        str(r.url),
                        (f"audio/{ext}" if ext else ct),
                        _content_total_size(r) if ext else 0,
                    )
            return False, curr_url, "", 0
    except Exception as exc:
        logger.debug("probe_url exception for %s: %s", url, exc)
        return False, url, "", 0


# ------------------------------------------------------------- 熔断器机制 --

_CHAIN_FAIL_THRESHOLD = 3
_CHAIN_OPEN_SECONDS = 600
_CHAIN_RETRY_SECONDS = 30
_CHAIN_HEALTH: dict[str, dict] = {}


def _chain_available(name: str) -> bool:
    h = _CHAIN_HEALTH.get(name)
    if not h:
        return True
    if h.get("half_open"):
        return False
    open_until = h.get("open_until", 0)
    now = time.time()
    if open_until and now < open_until:
        # half-open 试探
        if now >= h.get("next_try", 0):
            h["half_open"] = True
            return True
        return False
    return True


def _chain_record_success(name: str) -> None:
    _CHAIN_HEALTH.pop(name, None)


def _chain_record_failure(name: str) -> None:
    now = time.time()
    h = _CHAIN_HEALTH.setdefault(name, {"fails": 0, "open_until": 0, "next_try": 0, "half_open": False})
    if h.get("half_open"):
        h["half_open"] = False
        h["open_until"] = now + _CHAIN_OPEN_SECONDS
        h["next_try"] = now + _CHAIN_RETRY_SECONDS
        return
    h["fails"] += 1
    if h["fails"] >= _CHAIN_FAIL_THRESHOLD:
        h["open_until"] = now + _CHAIN_OPEN_SECONDS
        h["next_try"] = now + _CHAIN_RETRY_SECONDS


def chain_health_snapshot() -> dict[str, dict]:
    now = time.time()
    result = {}
    for k, v in list(_CHAIN_HEALTH.items()):
        open_until = v.get("open_until", 0)
        result[k] = {
            "fails": v.get("fails", 0),
            "state": "open" if open_until > now else "closed",
            "open_seconds_left": max(0, int(open_until - now)),
        }
    return result


# ------------------------------------------------------------- 搜索门闸 --

class _LxSuperseded:
    pass


_LX_SUPERSEDED = _LxSuperseded()


class LxSearchGate:
    """同时只搜一个关键词。新关键词自动取消前一个旧任务，防止多源并发阻塞。"""

    def __init__(self):
        self._epoch = 0
        self._running: dict | None = None
        self._queue: list[dict] = []

    def reset(self) -> None:
        self._epoch += 1
        job = self._running
        self._running = None
        queued = self._queue
        self._queue = []
        if job and job.get("task") and not job["task"].done():
            job["task"].cancel()
        for slot in queued:
            self._resolve(slot, _LX_SUPERSEDED)
        if job:
            self._resolve(job, _LX_SUPERSEDED)

    def _resolve(self, slot: dict, value: Any) -> None:
        for w in slot.get("waiters", []):
            if not w.done():
                w.set_result(value)

    async def run(self, scope: str, keyword: str, factory):
        fut = asyncio.get_running_loop().create_future()
        running = self._running
        if running and running["scope"] == scope and running["keyword"] == keyword and not running.get("superseded"):
            running["waiters"].append(fut)
        elif running and running["scope"] == scope and running["keyword"] != keyword:
            running["superseded"] = True
            self._resolve(running, _LX_SUPERSEDED)
            task = running.get("task")
            if task and not task.done():
                task.cancel()
            self._upsert(scope, keyword, factory, fut, front=True)
        else:
            self._upsert(scope, keyword, factory, fut, front=False)
        self._pump()
        return await asyncio.shield(fut)

    def _upsert(self, scope, keyword, factory, fut, front: bool) -> None:
        for slot in self._queue:
            if slot["scope"] != scope:
                continue
            if slot["keyword"] != keyword:
                self._resolve(slot, _LX_SUPERSEDED)
                slot["keyword"] = keyword
                slot["factory"] = factory
                slot["waiters"] = [fut]
            else:
                slot["waiters"].append(fut)
            if front:
                self._queue.remove(slot)
                self._queue.insert(0, slot)
            return
        slot = {"scope": scope, "keyword": keyword, "factory": factory, "waiters": [fut]}
        self._queue.insert(0, slot) if front else self._queue.append(slot)

    def _pump(self) -> None:
        if self._running is not None or not self._queue:
            return
        slot = self._queue.pop(0)
        job = {
            "scope": slot["scope"],
            "keyword": slot["keyword"],
            "factory": slot["factory"],
            "waiters": slot["waiters"],
            "superseded": False,
            "epoch": self._epoch,
            "task": None,
        }
        self._running = job

        async def _runner():
            val = None
            try:
                val = await job["factory"]()
            except asyncio.CancelledError:
                val = _LX_SUPERSEDED
            except Exception as e:
                val = e
            finally:
                if self._running is job:
                    self._running = None
                self._resolve(job, _LX_SUPERSEDED if job.get("superseded") else val)
                self._pump()

        job["task"] = asyncio.create_task(_runner())


_SEARCH_GATE = LxSearchGate()


# ------------------------------------------------------------- 核心解析逻辑 --

async def resolve_and_probe(
    client: httpx.AsyncClient,
    source: str,
    item: dict,
    quality: str = "lossless",
    budget: float = 20.0,
) -> dict | None:
    """尝试按音质阶梯从 lxserver 获取播放直链，并对结果执行 Range 媒体签名探活。"""
    src = normalize_source(source)
    track_id = item.get("id") or f"lx:{src}:{item.get('songmid', '')}"

    if not _chain_available("user_source"):
        logger.warning("lx resolve skipped: user_source circuit is open")
        return None

    # 反查或合成 songInfo
    song_info = LXSERVER.get_cached_song_info(track_id)
    if not song_info:
        song_info = LXSERVER.synthesize_song_info(track_id, item)

    tiers = _quality_tiers(quality)
    attempted_tiers: list[str] = []

    for tier in tiers:
        lx_q = _tier_to_lx_quality(tier)
        attempted_tiers.append(tier)
        try:
            async with asyncio.timeout(min(budget, CONF["resolver_timeout"])):
                res = await LXSERVER.get_music_url(song_info, lx_q)
        except Exception as exc:
            logger.debug("lxserver get_music_url tier %s failed: %s", tier, exc)
            res = None

        if not res or not res.get("url"):
            continue

        raw_url = res["url"]
        headers = {}
        # 酷狗或特殊源防盗链头
        if src == "kg":
            headers["Referer"] = "https://www.kugou.com/"
        elif src == "tx":
            headers["Referer"] = "https://y.qq.com/"

        # 媒体魔数探活
        ok, final_url, ct, size = await probe_url(client, raw_url, headers)
        if ok:
            _chain_record_success("user_source")
            ext = "flac" if "flac" in ct else ("mp3" if "mp3" in ct else "mp3")
            return {
                "id": track_id,
                "url": final_url,
                "ext": ext,
                "file_size": size,
                "actual_tier": tier,
                "resolver": "lxserver",
                "headers": headers,
                "br": 320000 if tier == "high" else (960000 if tier == "lossless" else 128000),
                "attempted_tiers": attempted_tiers,
            }
        else:
            logger.debug("probe_url rejected stream for %s at tier %s: ct=%s", track_id, tier, ct)

    _chain_record_failure("user_source")
    return None


async def source_capabilities() -> dict[str, dict]:
    """描述各平台的可用性（搜索可用性、播放解析可用性、音质列表）。"""
    lx_alive = await LXSERVER.is_alive()
    sources = await LXSERVER.list_custom_sources() if lx_alive else []
    # 查找是否有启用的自定义源
    active_source = None
    for s in sources:
        if s.get("enable") or s.get("enabled"):
            active_source = s
            break

    caps: dict[str, dict] = {}
    for src in SUPPORTED_PLATFORMS:
        has_playback = bool(lx_alive and active_source)
        reason = ""
        if not lx_alive:
            reason = "lxserver unready"
        elif not active_source:
            reason = "no active custom source"

        qualitys = ["128k", "320k", "flac"] if has_playback else []
        caps[src] = {
            "search": bool(lx_alive),
            "playback_available": has_playback,
            "qualitys": qualitys,
            "reason": reason,
        }
    return caps


async def describe_user_source() -> dict:
    """描述当前激活的音源。保持与原 SourceManager.describe() 结构兼容。"""
    try:
        sources = await LXSERVER.list_custom_sources()
    except Exception:
        sources = []

    active = None
    for s in sources:
        if s.get("enable") or s.get("enabled"):
            active = s
            break

    if not active:
        return {
            "configured": False,
            "url": "",
            "initialized": False,
            "last_error": "",
            "source": None,
        }

    platforms_desc = {}
    for p in SUPPORTED_PLATFORMS:
        platforms_desc[p] = {"qualitys": ["128k", "320k", "flac"]}

    return {
        "configured": True,
        "url": active.get("name") or active.get("id") or "",
        "initialized": True,
        "last_error": "",
        "source": {
            "name": active.get("name", "lx-source"),
            "version": active.get("version", "1.0.0"),
            "author": active.get("author", ""),
            "platforms": platforms_desc,
            "running": True,
            "pid": 0,
            "uptime_s": 3600,
        },
    }


# ------------------------------------------------------------- FastAPI 生命周期 --

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 共享一个全局 AsyncClient 用于探活和下载
    app.state.client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
    yield
    await app.state.client.aclose()
    await LXSERVER.close()


app = FastAPI(title="fnmusic-lxmusic", version=SERVICE_VERSION, lifespan=lifespan)


def get_http(request_or_app=None) -> httpx.AsyncClient:
    state = getattr(request_or_app, "state", None) or getattr(app, "state", None)
    if state is not None:
        client = getattr(state, "client", None)
        if client is not None and not client.is_closed:
            return client
        client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
        state.client = client
        return client
    return httpx.AsyncClient(timeout=10.0, follow_redirects=True)


def _err(msg: str, code: int = 404) -> JSONResponse:
    return JSONResponse(content={"ok": False, "error": msg}, status_code=code)


# ------------------------------------------------------------- 外部 HTTP 端点 --

@app.get("/healthz")
async def healthz():
    user_src = await describe_user_source()
    caps = await source_capabilities()
    return {
        "ok": True,
        "service": "fnmusic-lxmusic",
        "version": SERVICE_VERSION,
        "sources": CONF["sources"],
        "user_source": user_src,
        "circuit": chain_health_snapshot(),
        "capabilities": caps,
        "charts": ["kg", "kw", "wy"],
    }


@app.get("/api/v1/search")
async def search_tracks(
    keyword: str = Query("", alias="keyword"),
    q: str = Query("", alias="q"),
    sources: str = Query(""),
    limit: int = Query(0),
    page: int = Query(1),
    probe: int = Query(0),
    scope: str = Header(default="global", alias="X-Fnmusic-Scope"),
):
    """跨平台聚合搜索。支持防抖门闸与逐曲探活。"""
    kw = (keyword or q or "").strip()
    if not kw:
        return {"ok": True, "items": [], "errors": {}, "stats": dict(_STATS)}

    if limit <= 0:
        limit = CONF["limit_per_source"]

    wanted_raw = [s.strip() for s in (sources or "").split(",") if s.strip()]
    wanted = [normalize_source(s) for s in wanted_raw] if wanted_raw else CONF["sources"]
    wanted = [s for s in wanted if s in SUPPORTED_PLATFORMS]

    probe_enabled = bool(probe == 1 or is_probe_enabled())
    probe_token = _SEARCH_PROBE_ENABLED.set(probe_enabled)

    async def _do_search():
        _STATS["searches"] += 1
        items: list[dict] = []
        errors: dict[str, str] = {}
        client = get_http(app)

        async def _search_one(src: str):
            try:
                # 若 limit > 20，计算所需 pages
                pages = max(1, (limit + 19) // 20)
                sub_items = await LXSERVER.search(kw, source=src, page=page, pages=pages, limit=limit)
                return src, sub_items, None
            except Exception as e:
                return src, [], str(e)

        tasks = [_search_one(src) for src in wanted]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, Exception):
                continue
            src, sub_items, err = res
            if err:
                errors[src] = err
                continue
            for it in sub_items:
                _cache_put(it)
                if probe_enabled:
                    # 逐曲探活模式
                    probe_res = await resolve_and_probe(client, src, it, quality="standard", budget=4.0)
                    if probe_res:
                        it["verified"] = True
                        it["validation_status"] = "verified"
                        items.append(it)
                    else:
                        it["verified"] = False
                        it["validation_status"] = "candidate"
                else:
                    it["verified"] = False
                    it["validation_status"] = "candidate"
                    items.append(it)

        caps = await source_capabilities()
        return {
            "ok": True,
            "items": items[: limit * len(wanted)],
            "errors": errors,
            "stats": dict(_STATS),
            "capabilities": caps,
        }

    try:
        gate_res = await _SEARCH_GATE.run(scope, kw, _do_search)
        if gate_res is _LX_SUPERSEDED:
            return {"ok": True, "items": [], "superseded": True}
        if isinstance(gate_res, Exception):
            return _err(f"search failed: {gate_res}", 500)
        return gate_res
    finally:
        _SEARCH_PROBE_ENABLED.reset(probe_token)


@app.get("/api/v1/recommend")
async def recommend_charts(limit: int = Query(0), sources: str = Query("")):
    """免登录榜单推荐 (kg TOP500 / kw 飙升榜 / wy 新歌榜)。"""
    if limit <= 0:
        limit = CONF["limit_per_source"]

    wanted_raw = [s.strip() for s in (sources or "").split(",") if s.strip()]
    wanted = [normalize_source(s) for s in wanted_raw] if wanted_raw else ["kg", "kw", "wy"]
    wanted = [s for s in wanted if s in ("kg", "kw", "wy")]

    items: list[dict] = []
    errors: dict[str, str] = {}

    # 榜单映射
    chart_ids = {
        "kg": "8888",   # TOP500
        "kw": "93",     # 飙升榜
        "wy": "3779629",# 新歌榜
    }

    for src in wanted:
        bid = chart_ids.get(src, "")
        try:
            sub = await LXSERVER.get_leaderboard_list(src, bid, page=1)
            for it in sub:
                _cache_put(it)
                items.append(it)
        except Exception as e:
            errors[src] = str(e)

    _STATS["errors"] += len(errors)
    return {"ok": True, "items": items[:limit], "errors": errors, "charts": ["kg", "kw", "wy"]}


@app.get("/api/v1/track/url")
async def track_url(
    id: str = Query("", alias="id"),
    guid: str = Query("", alias="guid"),
    quality: str = Query("lossless"),
):
    """解析单曲播放直链。支持音质阶梯降级与媒体魔数探活。"""
    track_id = (id or guid or "").strip()
    src, identifier = parse_track_id(track_id)
    if not src or not identifier:
        return _err(f"invalid track id: {track_id}", 400)

    _STATS["url_resolutions"] += 1
    canonical_id = f"lx:{src}:{identifier}"
    cached = _cache_get(canonical_id) or {"id": canonical_id, "lx_source": src}
    client = get_http(app)

    try:
        result = await resolve_and_probe(client, src, cached, quality, budget=CONF["url_timeout"])
    except Exception as e:
        _STATS["errors"] += 1
        logger.warning("lx url resolve %s failed: %s", track_id, e)
        return _err(f"resolve failed: {e}", 502)

    if not result:
        return _err("no playable url", 404)

    return {
        "ok": True,
        "data": {
            "id": track_id,
            "quality": quality,
            **{k: v for k, v in result.items() if k != "probed"},
        },
    }


@app.get("/api/v1/track/info")
async def track_info(id: str = Query("", alias="id"), guid: str = Query("", alias="guid")):
    track_id = (id or guid or "").strip()
    src, identifier = parse_track_id(track_id)
    if not src:
        return _err(f"invalid track id: {track_id}", 400)

    canonical_id = f"lx:{src}:{identifier}"
    cached = _cache_get(canonical_id)
    if cached:
        return {"ok": True, "data": cached}

    # 合成默认信息
    sinfo = LXSERVER.synthesize_song_info(canonical_id)
    mapped = map_lxserver_song(sinfo, fallback_source=src)
    return {"ok": True, "data": mapped}


@app.get("/api/v1/track/lyric")
async def track_lyric(id: str = Query("", alias="id"), guid: str = Query("", alias="guid")):
    track_id = (id or guid or "").strip()
    src, identifier = parse_track_id(track_id)
    if not src:
        return _err(f"invalid track id: {track_id}", 400)

    canonical_id = f"lx:{src}:{identifier}"
    sinfo = LXSERVER.get_cached_song_info(canonical_id)
    if not sinfo:
        cached = _cache_get(canonical_id) or {}
        sinfo = LXSERVER.synthesize_song_info(canonical_id, cached)

    text = ""
    try:
        lyric_data = await LXSERVER.get_lyric(sinfo)
        if lyric_data:
            text = lyric_data.get("lyric") or lyric_data.get("lrc") or ""
    except Exception as exc:
        logger.warning("lx lyric %s failed: %s", track_id, exc)

    return {"ok": True, "data": {"id": track_id, "lyric": text or ""}}


# ------------------------------------------------------------- 自定义音源管理 --

class SourceBody(BaseModel):
    url: str = ""
    script: str = ""


@app.get("/api/v1/source")
async def source_info():
    user_src = await describe_user_source()
    return {"ok": True, "data": user_src, "circuit": chain_health_snapshot()}


@app.post("/api/v1/source/verify")
async def source_verify(body: SourceBody):
    """端到端试运行自定义源（不改变当前激活源）。"""
    from verify_source import verify_url

    url = (body.url or "").strip()
    if not url.startswith(("http://", "https://", "file://")):
        return _err("url 必须以 http:// 、https:// 或 file:// 开头", 400)
    try:
        report = await asyncio.wait_for(verify_url(url), timeout=120.0)
    except asyncio.TimeoutError:
        return _err("校验超时 (120s)", 504)
    except Exception as exc:
        return JSONResponse(
            content={"ok": False, "data": {"category": "internal", "message": f"校验失败: {exc}"}}
        )
    return {"ok": bool(report.get("ok")), "data": report}


class UploadBody(BaseModel):
    filename: str
    script: str


def _lx_source_fs_path(source_id: str) -> str:
    """lxserver _open 用户源的容器内存储路径（与 lxserver getSourceDir/_open 约定一致）。"""
    return f"/data/lxserver/users/source/_open/{source_id}"


async def _find_lxserver_source(*, by_id: str = "", by_name: str = "", by_url: str = "") -> dict | None:
    """在 lxserver 已有源列表中查找（按 id / 名称 / 导入 URL 任一命中）。"""
    try:
        sources = await LXSERVER.list_custom_sources()
    except Exception:
        return None
    for s in sources:
        if by_id and str(s.get("id") or "") == by_id:
            return s
    for s in sources:
        name = str(s.get("name") or "")
        if by_name and (name == by_name or str(s.get("id") or "") == by_name):
            return s
        if by_url and str(s.get("sourceUrl") or "") == by_url:
            return s
    return None


@app.post("/api/v1/source/upload")
async def source_upload(body: UploadBody):
    """落盘并导入上传的自定义源脚本。

    lxserver 以脚本 @name 生成唯一 id 并落盘到 _open 目录；同一脚本重复上传时
    lxserver 报"已存在"，此时复用已有源（多源列表场景下再次添加同名脚本是正常操作）。
    """
    # 提取脚本头部元数据（lxserver 返回的 metadata 为准，此处作兜底与匹配用）
    match = re.search(r"@name\s+([^\r\n]+)", body.script)
    name = match.group(1).strip() if match else body.filename

    try:
        res = await LXSERVER.upload_custom_source(body.filename, body.script)
    except Exception as exc:
        if "已存在" not in str(exc):
            return JSONResponse(
                content={"ok": False, "error": f"上传到 lxserver 失败: {exc}", "category": "download"},
                status_code=500,
            )
        existing = await _find_lxserver_source(by_name=name)
        if not existing:
            return JSONResponse(
                content={"ok": False, "error": f"上传到 lxserver 失败: {exc}", "category": "download"},
                status_code=500,
            )
        res = {"id": existing.get("id"), "metadata": {
            "name": existing.get("name") or name,
            "version": existing.get("version") or "1.0.0",
        }}

    source_id = str(res.get("id") or "") or name
    meta = res.get("metadata") or {}
    path = _lx_source_fs_path(source_id)
    return {
        "ok": True,
        "data": {
            "path": path,
            "url": f"file://{path}",
            "meta": {
                "name": meta.get("name") or name,
                "version": meta.get("version") or "1.0.0",
            },
        },
    }


@app.post("/api/v1/source")
async def source_set(body: SourceBody):
    """切换激活自定义源。第一期保持单源语义：激活目标源，其余禁用。"""
    url = (body.url or "").strip()
    if not url and body.script:
        # 直接传入脚本内容：先上传再激活
        try:
            res = await LXSERVER.upload_custom_source("custom_source.js", body.script)
            url = f"file://{_lx_source_fs_path(str(res.get('id') or 'custom_source.js'))}"
        except Exception as exc:
            return JSONResponse(
                content={"ok": False, "error": f"上传脚本失败: {exc}", "category": "download"},
                status_code=500,
            )

    if not url:
        return _err("url 不能为空", 400)

    try:
        source_id = ""
        if url.startswith(("http://", "https://")):
            # http(s) URL：交给 lxserver 下载导入；同脚本已导入过则直接复用
            try:
                res = await LXSERVER.import_custom_source(url)
                source_id = str(res.get("id") or "")
            except Exception as exc:
                if "已存在" not in str(exc):
                    raise
                existing = await _find_lxserver_source(by_url=url)
                source_id = str((existing or {}).get("id") or "")
        else:
            # file:// URL 或裸 id/名称：取最后一段作为 lxserver 源 id（与上传时返回的 id 一致）
            parsed = urllib.parse.urlparse(url)
            path_part = urllib.parse.unquote(parsed.path) if parsed.scheme else url
            source_id = path_part.rsplit("/", 1)[-1]
            if not await LXSERVER.activate_single_source(source_id):
                # 列表中无此 id：若本地确有脚本文件（如旧数据卷 /data/lxmusic/uploads），
                # 自动补导入 lxserver 后激活，保证历史配置可继续使用
                imported = False
                if path_part.startswith("/") and os.path.isfile(path_part):
                    with open(path_part, "r", encoding="utf-8", errors="replace") as f:
                        script = f.read()
                    try:
                        res = await LXSERVER.upload_custom_source(source_id, script)
                        source_id = str(res.get("id") or "") or source_id
                        imported = True
                    except Exception as exc:
                        if "已存在" not in str(exc):
                            raise
                        existing = (
                            await _find_lxserver_source(by_id=source_id)
                            or await _find_lxserver_source(by_name=source_id.removesuffix(".js"))
                        )
                        if existing:
                            source_id = str(existing.get("id") or source_id)
                            imported = True
                if not imported or not await LXSERVER.activate_single_source(source_id):
                    return JSONResponse(
                        content={"ok": False,
                                 "error": f"未找到要激活的源: {source_id}，请先上传或用测试校验源可用性",
                                 "category": "runtime"},
                        status_code=500,
                    )
    except Exception as exc:
        return JSONResponse(
            content={"ok": False, "error": f"激活源失败: {exc}", "category": "runtime"},
            status_code=500,
        )

    _CHAIN_HEALTH.pop("user_source", None)
    user_src = await describe_user_source()
    return {"ok": True, "data": user_src}


@app.delete("/api/v1/source")
async def source_clear():
    """停用全部自定义源。"""
    try:
        sources = await LXSERVER.list_custom_sources()
        for s in sources:
            sid = str(s.get("id") or "")
            if sid:
                await LXSERVER.toggle_custom_source(sid, False)
    except Exception as exc:
        logger.warning("source_clear error: %s", exc)

    _CHAIN_HEALTH.pop("user_source", None)
    return {"ok": True, "data": None}
