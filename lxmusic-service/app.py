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
import sys
import time
from pathlib import Path
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 仓库根：复用 proxy/fnlog
from proxy.fnlog import diag as fn_diag  # noqa: E402
from proxy.fnlog import setup_logging  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("fnmusic.lxmusic")
# 本地文件日志（logs/lxmusic.log，按天滚动保留 3 天）；失败静默降级为仅 stdout
setup_logging("fnmusic.lxmusic", "lxmusic")

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
    "url_cache_ttl": float(os.environ.get("LX_URL_CACHE_TTL", "600.0")),
    "url_cache_neg_ttl": float(os.environ.get("LX_URL_CACHE_NEG_TTL", "60.0")),
    "url_cache_max": int(os.environ.get("LX_URL_CACHE_MAX", "512")),
    "probe_timeout": float(os.environ.get("LX_PROBE_TIMEOUT", "5.0")),
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
# 播放直链缓存: (canonical_id, tier) -> {"data": dict | None, "ts": float}；data=None 为失败负缓存
_URL_CACHE: dict[tuple[str, str], dict] = {}
# 同键在途解析合并：key -> Future（与 musicdl SingleFlight 同型的 shielded-future 去重）
_URL_INFLIGHT: dict[tuple[str, str], asyncio.Future] = {}
# 源代计数：切源/重导入脚本时自增，代间在途解析的回写据此丢弃，防止串源
_URL_CACHE_GEN = 0
_STATS = {"searches": 0, "url_resolutions": 0, "url_cache_hits": 0, "errors": 0}

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


def _url_cache_reset() -> None:
    """清空播放直链缓存并推进源代计数：切源/重导入脚本后旧直链一律作废，防止串源。"""
    _URL_CACHE.clear()
    global _URL_CACHE_GEN
    _URL_CACHE_GEN += 1


async def _refresh_cached_url(client: httpx.AsyncClient, data: dict) -> dict | None:
    """对过期的缓存直链做 Range 前缀探活续期。

    仅请求 CDN 少量前缀字节、不消耗按次计量的音源解析额度；仍可用则返回
    缓存条目（上游重定向时更新最终 URL 与总大小），失效返回 None。"""
    url = data.get("url")
    if not url:
        return None
    try:
        ok, final_url, _ct, size = await probe_url(client, url, data.get("headers") or {})
    except Exception:
        return None
    if not ok:
        return None
    refreshed = dict(data)
    if final_url and final_url != url:
        refreshed["url"] = final_url
    if size:
        refreshed["file_size"] = size
    return refreshed


async def _resolve_url_cached(
    client: httpx.AsyncClient,
    src: str,
    item: dict,
    quality: str = "lossless",
    budget: float = 20.0,
    fresh: bool = False,
) -> dict | None:
    """带成功缓存/失败负缓存/同键在途合并的播放直链解析。

    - 成功结果按「曲目 + 音质档」缓存 url_cache_ttl 秒，有效期内零额度复用；
    - TTL 到期先探活旧链续期（不耗音源额度），失效才重新解析；
    - 失败负缓存 url_cache_neg_ttl 秒，短时间内同键重复请求直接快速失败；
    - fresh=True 显式旁路缓存强制重新解析（代理重试路径确认旧链失效后使用）；
    - 同键并发共享一次实际解析；源代计数变化后代间结果不回写。"""
    canonical_id = item.get("id") or f"lx:{src}:{item.get('songmid', '')}"
    tiers = _quality_tiers(quality)
    key = (canonical_id, tiers[0] if tiers else "standard")
    gen = _URL_CACHE_GEN
    deadline = time.monotonic() + max(float(budget), 1.0)

    if not fresh:
        entry = _URL_CACHE.get(key)
        if entry is not None:
            # ts 用单调钟：NTP 回拨会把墙钟缓存任意拉长，续发过期直链
            age = time.monotonic() - float(entry.get("ts") or 0.0)
            data = entry.get("data")
            if data is None:
                if age <= CONF["url_cache_neg_ttl"]:
                    fn_diag(logger, "url_cache", op="neg_hit", song=str(canonical_id)[:60],
                            quality=str(tiers[0] if tiers else "standard"))
                    return None
            elif age <= CONF["url_cache_ttl"]:
                _STATS["url_cache_hits"] += 1
                fn_diag(logger, "url_cache", op="hit", song=str(canonical_id)[:60],
                        quality=str(tiers[0] if tiers else "standard"))
                return data
            else:
                refreshed = await _refresh_cached_url(client, data)
                if refreshed is not None:
                    if _URL_CACHE_GEN == gen:
                        entry["data"] = refreshed
                        entry["ts"] = time.monotonic()
                    _STATS["url_cache_hits"] += 1
                    fn_diag(logger, "url_cache", op="refreshed", song=str(canonical_id)[:60],
                            quality=str(tiers[0] if tiers else "standard"))
                    return refreshed

    remaining = deadline - time.monotonic()
    return await _resolve_url_inflight(client, src, item, quality, max(remaining, 1.0), key, gen)


async def _resolve_url_inflight(
    client: httpx.AsyncClient,
    src: str,
    item: dict,
    quality: str,
    budget: float,
    key: tuple[str, str],
    gen: int,
) -> dict | None:
    """实际解析执行段：同键在途合并，失败/取消正确清理在途状态供后续重试。"""
    existing = _URL_INFLIGHT.get(key)
    if existing is not None:
        # issue #45：同键并发跟随者共享领队的实际解析，不再重复消耗音源额度
        fn_diag(logger, "url_inflight", op="join", song=str(key[0])[:60], quality=str(key[1]))
        return await asyncio.shield(existing)

    fut = asyncio.get_running_loop().create_future()
    _URL_INFLIGHT[key] = fut
    try:
        result = await resolve_and_probe(client, src, item, quality, budget=budget)
    except BaseException as exc:
        if not fut.done():
            # 领队被取消时不能把 CancelledError 设给跟随者：跟随者的任务并未被
            # 取消，收到 CancelledError 会被 asyncio 标记为 cancelled 而无辜死掉，
            # 换成普通错误让它们走各自的重试路径
            fut.set_exception(
                exc if not isinstance(exc, asyncio.CancelledError)
                else RuntimeError("inflight leader cancelled")
            )
            # Mark observed even without followers; awaiting followers still receive it.
            fut.exception()
        raise
    finally:
        _URL_INFLIGHT.pop(key, None)

    # issue #45：留档每次真实上游解析（领队），与 hit/join 对比可核对额度消耗
    fn_diag(logger, "url_resolve", op="ok" if result else "none",
            song=str(key[0])[:60], quality=str(key[1]))
    if _URL_CACHE_GEN == gen:
        if len(_URL_CACHE) >= CONF["url_cache_max"]:
            # 容量超限清理最早的一半（与 _cache_put 同策略）
            for old in sorted(_URL_CACHE, key=lambda k: _URL_CACHE[k]["ts"])[: len(_URL_CACHE) // 2]:
                _URL_CACHE.pop(old, None)
        _URL_CACHE[key] = {"data": result, "ts": time.monotonic()}
    if not fut.done():
        fut.set_result(result)
    return result


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
    now = time.time()
    if h.get("half_open"):
        # 半开试探在途：超过一个试探周期仍未回填（解析任务被取消等）自动放行
        # 新试探，避免熔断器被一次未落地的试探永久卡死
        if now - h.get("half_open_since", now) > _CHAIN_RETRY_SECONDS:
            h["half_open"] = False
        else:
            return False
    open_until = h.get("open_until", 0)
    if open_until and now < open_until:
        # half-open 试探
        if now >= h.get("next_try", 0):
            h["half_open"] = True
            h["half_open_since"] = now
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
            "half_open": bool(v.get("half_open")),
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

    async def run(self, scope: str, keyword: str, factory, subkey: str = ""):
        fut = asyncio.get_running_loop().create_future()
        running = self._running
        if (
            running
            and running["scope"] == scope
            and running["keyword"] == keyword
            and running.get("subkey") == subkey
            and not running.get("superseded")
        ):
            running["waiters"].append(fut)
        elif running and running["scope"] == scope and running["keyword"] != keyword:
            running["superseded"] = True
            self._resolve(running, _LX_SUPERSEDED)
            task = running.get("task")
            if task and not task.done():
                task.cancel()
            self._upsert(scope, keyword, subkey, factory, fut, front=True)
        else:
            self._upsert(scope, keyword, subkey, factory, fut, front=False)
        self._pump()
        return await asyncio.shield(fut)

    def _upsert(self, scope, keyword, subkey, factory, fut, front: bool) -> None:
        for slot in self._queue:
            if slot["scope"] != scope:
                continue
            if slot["keyword"] != keyword:
                self._resolve(slot, _LX_SUPERSEDED)
                slot["keyword"] = keyword
                slot["subkey"] = subkey
                slot["factory"] = factory
                slot["waiters"] = [fut]
            elif slot.get("subkey") == subkey:
                slot["waiters"].append(fut)
            else:
                continue
            if front:
                self._queue.remove(slot)
                self._queue.insert(0, slot)
            return
        slot = {
            "scope": scope,
            "keyword": keyword,
            "subkey": subkey,
            "factory": factory,
            "waiters": [fut],
        }
        self._queue.insert(0, slot) if front else self._queue.append(slot)

    def _pump(self) -> None:
        if self._running is not None or not self._queue:
            return
        slot = self._queue.pop(0)
        job = {
            "scope": slot["scope"],
            "keyword": slot["keyword"],
            "subkey": slot.get("subkey", ""),
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

# 同档位探活失败后的换源上限：多源同时激活时逐个排除已产出坏链的脚本再试，
# 超出按音源脚本数量上限直接降档（每轮仍受 resolver_timeout 与总预算约束）
_MAX_SOURCE_ROUNDS = 3


def _scripts_tried(res: dict) -> list[str]:
    """解析结果里实际参与过的脚本标识（attempts 全部 + 最终产出者），供同档换源排除。

    lxserver 的 excludeApiSources 按脚本 name 或 id（不区分大小写）过滤，二者都传。"""
    names: list[str] = []
    for a in (res.get("attempts") or []):
        if isinstance(a, dict):
            name = str(a.get("name") or "").strip()
            if name:
                names.append(name)
    for key in ("sourceId", "sourceName"):
        name = str(res.get(key) or "").strip()
        if name:
            names.append(name)
    return list(dict.fromkeys(names))


async def resolve_and_probe(
    client: httpx.AsyncClient,
    source: str,
    item: dict,
    quality: str = "lossless",
    budget: float = 20.0,
) -> dict | None:
    """尝试按音质阶梯从 lxserver 获取播放直链，并对结果执行 Range 媒体签名探活。

    熔断按平台独立计数：某一平台（如 tx 只产出加密 mflac、mg 后端全挂）连续失败
    只熔断该平台，不再拖垮其他健康平台的解析（2026-10-10 全局熔断误伤全平台故障）。
    同档位直链探活失败时按 excludeApiSources 排除已试脚本换下一个激活源重试。"""
    src = normalize_source(source)
    track_id = item.get("id") or f"lx:{src}:{item.get('songmid', '')}"
    chain_key = f"user_source:{src}" if src else "user_source"

    if not _chain_available(chain_key):
        logger.warning("lx resolve skipped: %s circuit is open", chain_key)
        return None

    # 反查或合成 songInfo
    song_info = LXSERVER.get_cached_song_info(track_id)
    if not song_info:
        song_info = LXSERVER.synthesize_song_info(track_id, item)

    deadline = time.monotonic() + budget
    tiers = _quality_tiers(quality)
    attempted_tiers: list[str] = []

    for tier in tiers:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        lx_q = _tier_to_lx_quality(tier)
        attempted_tiers.append(tier)
        excluded: list[str] = []
        for _round in range(_MAX_SOURCE_ROUNDS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                async with asyncio.timeout(min(remaining, float(CONF["resolver_timeout"]))):
                    res = await LXSERVER.get_music_url(song_info, lx_q,
                                                       exclude_api_sources=excluded or None)
            except Exception as exc:
                logger.debug("lxserver get_music_url tier %s failed: %s", tier, exc)
                res = None

            if not res or not res.get("url"):
                break  # 服务端无可用源/调用出错：降档

            raw_url = res["url"]
            headers = {}
            # 酷狗或特殊源防盗链头
            if src == "kg":
                headers["Referer"] = "https://www.kugou.com/"
            elif src == "tx":
                headers["Referer"] = "https://y.qq.com/"

            # 媒体魔数探活（共用同一总预算）
            remaining_probe = deadline - time.monotonic()
            if remaining_probe <= 0:
                break
            try:
                async with asyncio.timeout(remaining_probe):
                    ok, final_url, ct, size = await probe_url(client, raw_url, headers)
            except Exception:
                ok = False

            if ok:
                _chain_record_success(chain_key)
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

            # 直链探活失败（过期/加密/HTML 错误页）：排除产出该链的脚本同档换源重试；
            # 服务端确认已无更多候选源时不再轮换，直接降档
            logger.info("probe rejected %s tier=%s ct=%s source=%s url=%.120s", track_id, tier,
                        ct or "", res.get("sourceName") or "", raw_url)
            tried = [n for n in _scripts_tried(res) if n not in excluded]
            excluded.extend(tried)
            if res.get("hasMoreSources") is False or not tried:
                break

    _chain_record_failure(chain_key)
    return None


def _source_supported_platforms(s: dict) -> list[str]:
    """提取单个源的声明平台（兼容 supportedSources/sources 键与 dict 形态），空则视为全平台。"""
    raw_supp = s.get("supportedSources") or s.get("sources") or []
    if isinstance(raw_supp, dict):
        raw_supp = list(raw_supp.keys())
    supported = [normalize_source(str(p)) for p in raw_supp if normalize_source(str(p))]
    return supported or list(SUPPORTED_PLATFORMS)


async def source_capabilities() -> dict[str, dict]:
    """描述各平台的可用性（搜索可用性、播放解析可用性、音质列表）。

    多源同时激活时按全部启用源（status != failed）的平台并集判定。"""
    lx_alive = await LXSERVER.is_alive()
    sources = await LXSERVER.list_custom_sources() if lx_alive else []
    enabled_sources = [s for s in sources if s.get("enable") or s.get("enabled")]

    supported: set[str] = set()
    first_error = ""
    for s in enabled_sources:
        if str(s.get("status") or "ok") == "failed":
            if not first_error:
                first_error = str(s.get("error") or "init error")
            continue
        supported.update(_source_supported_platforms(s))

    caps: dict[str, dict] = {}
    for src in SUPPORTED_PLATFORMS:
        has_playback = bool(lx_alive and enabled_sources and (src in supported))
        reason = ""
        if not lx_alive:
            reason = "lxserver unready"
        elif not enabled_sources:
            reason = "no active custom source"
        elif not supported and first_error:
            reason = f"all active sources failed: {first_error}"
        elif src not in supported:
            reason = f"platform {src} not supported by active source"

        qualitys = ["128k", "320k", "flac"] if has_playback else []
        caps[src] = {
            "search": bool(lx_alive),
            "playback_available": has_playback,
            "qualitys": qualitys,
            "reason": reason,
        }
    return caps


async def describe_user_source() -> dict:
    """描述当前激活的音源（支持多源同时激活）。

    兼容约定：configured/url/source/initialized/last_error 等既有字段取第一个启用源；
    新增 sources 数组（全部启用源）与 active_count。"""
    try:
        sources = await LXSERVER.list_custom_sources()
    except Exception:
        sources = []

    enabled = [s for s in sources if s.get("enable") or s.get("enabled")]

    def _entry(s: dict) -> dict:
        status = str(s.get("status") or "ok")
        return {
            "id": str(s.get("id") or ""),
            "name": str(s.get("name") or ""),
            "version": str(s.get("version") or "1.0.0"),
            "url": str(s.get("sourceUrl") or ""),
            "status": status,
            "error": str(s.get("error") or ""),
            "initialized": status != "failed",
            "platforms": _source_supported_platforms(s),
        }

    active_sources = [_entry(s) for s in enabled]

    if not active_sources:
        return {
            "configured": False,
            "url": "",
            "initialized": False,
            "last_error": "",
            "source": None,
            "sources": [],
            "active_count": 0,
        }

    first_src, first = enabled[0], active_sources[0]
    platforms_desc = {p: {"qualitys": ["128k", "320k", "flac"]} for p in first["platforms"]}

    return {
        "configured": True,
        "url": first_src.get("name") or first_src.get("id") or "",
        "initialized": first["initialized"],
        "last_error": first["error"],
        "source": {
            "name": first_src.get("name", "lx-source"),
            "version": first_src.get("version", "1.0.0"),
            "author": first_src.get("author", ""),
            "platforms": platforms_desc,
            "running": first["initialized"],
            "pid": 0,
            "uptime_s": 3600,
        },
        "sources": active_sources,
        "active_count": len(active_sources),
    }


# ------------------------------------------------------------- FastAPI 生命周期 --

def _env_active_source_urls() -> list[str]:
    """从 LX_SOURCE_LIST 环境变量解析 active 标记的源 URL（多源自愈恢复用）。"""
    raw = os.environ.get("LX_SOURCE_LIST", "").strip()
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    urls: list[str] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("active"):
            continue
        url = str(item.get("url") or "").strip()
        if url:
            urls.append(url)
    return urls


async def _bootstrap_migration():
    for _ in range(20):
        try:
            if await LXSERVER.is_alive():
                break
        except Exception:
            pass
        await asyncio.sleep(0.5)

    try:
        old_dir = Path(os.environ.get("LX_DATA_DIR", "/data/lxmusic"))
        uploads = old_dir / "uploads"
        if uploads.is_dir():
            existing = await LXSERVER.list_custom_sources()
            existing_names = {s.get("name") for s in existing} | {s.get("id") for s in existing}
            for js_file in uploads.glob("*.js"):
                if js_file.name not in existing_names:
                    try:
                        content = js_file.read_text(encoding="utf-8", errors="replace")
                        await LXSERVER.upload_custom_source(js_file.name, content)
                        logger.info("已自动迁移并加载历史自定义源脚本: %s", js_file.name)
                    except Exception as e:
                        logger.warning("迁移脚本 %s 失败: %s", js_file.name, e)

        active = await describe_user_source()
        if not active.get("configured"):
            # 优先按 LX_SOURCE_LIST 的 active 标记恢复多源激活（lxserver 数据卷被清时自愈）
            multi_urls = _env_active_source_urls()
            if multi_urls:
                for url in multi_urls:
                    try:
                        res = await source_set(SourceBody(url=_normalize_legacy_file_url(url)))
                        if isinstance(res, JSONResponse):
                            res = json.loads(res.body)
                        if res.get("ok"):
                            logger.info("已自动激活洛雪源: %s", url)
                        else:
                            logger.warning("自动激活洛雪源失败: %s (%s)", url, res.get("error") or "")
                    except Exception as e:
                        logger.warning("自动激活洛雪源失败: %s: %s", url, e)
            else:
                # 旧单源路径回退：state.json / LX_SOURCE_URL
                source_url = ""
                state_file = old_dir / "state.json"
                if state_file.is_file():
                    try:
                        data = json.loads(state_file.read_text(encoding="utf-8"))
                        source_url = str(data.get("source_url") or "")
                    except Exception:
                        pass
                if not source_url:
                    source_url = os.environ.get("LX_SOURCE_URL", "").strip()
                if source_url:
                    try:
                        await source_set(SourceBody(url=_normalize_legacy_file_url(source_url)))
                        logger.info("已自动激活历史音源: %s", source_url)
                    except Exception as e:
                        logger.warning("自动激活历史音源失败: %s", e)
    except Exception as exc:
        logger.warning("历史数据检查与迁移异常: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 共享一个全局 AsyncClient 用于探活和下载
    app.state.client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
    migration_task = asyncio.create_task(_bootstrap_migration())
    yield
    migration_task.cancel()
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
        subkey = f"{page}:{limit}:{','.join(sorted(wanted))}:{probe_enabled}"
        gate_res = await _SEARCH_GATE.run(scope, kw, _do_search, subkey=subkey)
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
    fresh: int = Query(0),
):
    """解析单曲播放直链。支持音质阶梯降级、媒体魔数探活与成功结果缓存/同键并发合并。

    fresh=1 旁路缓存强制重新解析：代理重试路径确认缓存直链失效后的有界刷新入口。"""
    track_id = (id or guid or "").strip()
    src, identifier = parse_track_id(track_id)
    if not src or not identifier:
        return _err(f"invalid track id: {track_id}", 400)

    _STATS["url_resolutions"] += 1
    canonical_id = f"lx:{src}:{identifier}"
    cached = _cache_get(canonical_id) or {"id": canonical_id, "lx_source": src}
    client = get_http(app)

    try:
        result = await _resolve_url_cached(
            client, src, cached, quality, budget=CONF["url_timeout"], fresh=bool(fresh)
        )
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
    enabled: bool = True  # false 表示停用该源（多源叠加语义下的取消激活）


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


_META_BLOCK_RE = re.compile(r"/\*[*!]([\s\S]*?)\*/")


def _parse_script_head_meta(script: str) -> dict:
    """按 lxserver extractMetadata 契约解析头部块注释（/*! 或 /**）里的 @name/@version。"""
    meta: dict[str, str] = {}
    m = _META_BLOCK_RE.search(script)
    if not m:
        return meta
    block = m.group(1)
    for key in ("name", "version"):
        km = re.search(rf"@{key}\s+(.+)", block)
        if km:
            meta[key] = km.group(1).strip()
    return meta


LXSERVER_DATA_DIR = os.environ.get("LXSERVER_DATA_DIR", "/data/lxserver")


def _normalize_legacy_file_url(url: str) -> str:
    """跨部署形态的 file:// 源路径归一。

    docker（/data = 数据卷）与原生（宿主 sources-data）两种部署形态共用同一份
    sources-data，但 file:// URL 里的挂载路径前缀不同；互切后历史持久化的
    URL（state.json / LX_SOURCE_URL 种子）指向另一形态路径。原路径不存在而
    本形态对应路径存在时改写，其余（含 http(s) 与正常 file://）原样返回。
    """
    if not url.startswith("file://"):
        return url
    path = url[len("file://"):]
    if os.path.exists(path):
        return url
    marker = "/lxmusic/uploads/"
    idx = path.find(marker)
    if idx >= 0:
        data_dir = os.environ.get("LX_DATA_DIR", "/data/lxmusic")
        candidate = f"{data_dir}/uploads/{path[idx + len(marker):]}"
        if os.path.exists(candidate):
            return f"file://{candidate}"
    return url


def _lx_source_fs_path(source_id: str) -> str:
    """lxserver _open 用户源的存储路径（与 lxserver getSourceDir/_open 约定一致）。

    容器内为 /data/lxserver（镜像默认值）；原生部署经 LXSERVER_DATA_DIR 指向宿主数据目录。
    """
    return f"{LXSERVER_DATA_DIR}/users/source/_open/{source_id}"


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
    name = _parse_script_head_meta(body.script).get("name") or body.filename

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
    # 同名脚本重新上传可能被 lxserver 覆盖（换接口/密钥），已缓存直链不再可信
    _url_cache_reset()
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
    """激活/停用自定义源（多源叠加语义：只改目标源启用态，不动其他源）。

    body.enabled=false 表示停用目标源；默认 true 为激活。同一脚本重复添加是正常操作。"""
    url = _normalize_legacy_file_url((body.url or "").strip())
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
            if not body.enabled:
                # 停用：无需重新导入，直接按导入 URL 反查源 id
                existing = await _find_lxserver_source(by_url=url)
                source_id = str((existing or {}).get("id") or "")
                if not source_id or not await LXSERVER.set_source_enabled(source_id, False):
                    return JSONResponse(
                        content={"ok": False, "error": f"未找到要停用的源: {url}", "category": "runtime"},
                        status_code=500,
                    )
            else:
                # http(s) URL：交给 lxserver 下载导入；同脚本已导入过则直接复用
                try:
                    res = await LXSERVER.import_custom_source(url)
                    source_id = str(res.get("id") or "")
                except Exception as exc:
                    if "已存在" not in str(exc):
                        raise
                    existing = await _find_lxserver_source(by_url=url)
                    source_id = str((existing or {}).get("id") or "")
                if not source_id:
                    existing = await _find_lxserver_source(by_url=url)
                    source_id = str((existing or {}).get("id") or "")
                if not source_id or not await LXSERVER.set_source_enabled(source_id, True):
                    return JSONResponse(
                        content={
                            "ok": False,
                            "error": f"导入成功但未找到要激活的源: {source_id or url}",
                            "category": "runtime",
                        },
                        status_code=500,
                    )
        else:
            # file:// URL 或裸 id/名称：取最后一段作为 lxserver 源 id（与上传时返回的 id 一致）
            parsed = urllib.parse.urlparse(url)
            path_part = urllib.parse.unquote(parsed.path) if parsed.scheme else url
            source_id = path_part.rsplit("/", 1)[-1]
            if not await LXSERVER.set_source_enabled(source_id, body.enabled):
                # 列表中无此 id：激活语义下，若本地确有脚本文件（如旧数据卷 /data/lxmusic/uploads），
                # 自动补导入 lxserver 后激活，保证历史配置可继续使用；停用语义不做补导入，
                # 但 file:// 的 basename（用户自己的文件名）不是 lxserver 按 @name 派生的
                # id——先按名称反查，再读本地脚本按 @name 反查真实 id 后停用
                resolved = False
                if not body.enabled:
                    existing = await _find_lxserver_source(by_name=source_id.removesuffix(".js"))
                    if not existing and path_part.startswith("/") and os.path.isfile(path_part):
                        try:
                            with open(path_part, "r", encoding="utf-8", errors="replace") as f:
                                script = f.read()
                        except OSError:
                            script = ""
                        name = _parse_script_head_meta(script).get("name") or ""
                        if name:
                            existing = await _find_lxserver_source(by_name=name)
                    if existing:
                        source_id = str(existing.get("id") or source_id)
                        resolved = True
                if body.enabled and path_part.startswith("/") and os.path.isfile(path_part):
                    with open(path_part, "r", encoding="utf-8", errors="replace") as f:
                        script = f.read()
                    try:
                        res = await LXSERVER.upload_custom_source(source_id, script)
                        source_id = str(res.get("id") or "") or source_id
                        resolved = True
                    except Exception as exc:
                        if "已存在" not in str(exc):
                            raise
                        existing = (
                            await _find_lxserver_source(by_id=source_id)
                            or await _find_lxserver_source(by_name=source_id.removesuffix(".js"))
                        )
                        if existing:
                            source_id = str(existing.get("id") or source_id)
                            resolved = True
                if not resolved or not await LXSERVER.set_source_enabled(source_id, body.enabled):
                    action = "激活" if body.enabled else "停用"
                    return JSONResponse(
                        content={"ok": False,
                                 "error": f"未找到要{action}的源: {source_id}，请先上传或用测试校验源可用性",
                                 "category": "runtime"},
                        status_code=500,
                    )
    except Exception as exc:
        return JSONResponse(
            content={"ok": False, "error": f"切换源失败: {exc}", "category": "runtime"},
            status_code=500,
        )

    _CHAIN_HEALTH.clear()
    _url_cache_reset()
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
        return JSONResponse(content={"ok": False, "error": str(exc)}, status_code=500)

    _CHAIN_HEALTH.clear()
    _url_cache_reset()
    return {"ok": True, "data": None}
