"""lxmusic-service/app.py
fnmusic-ext 洛雪音源适配服务 (基于 lxserver 后端)。

契约说明：
1. 既有播放/搜索/源管理接口保持兼容；源管理新增 POST /api/v1/source/reorder，
   GET /api/v1/source 增加 policy 字段（自动切换、平台优选、稳定脚本顺序）；
2. 内部通过 lxserver_client 调用容器内本地运行的 lxserver (端口 8005)；
3. 本模块负责曲目 ID (lx:<src>:<id>) 映射、Range 魔数探活 (probe_url)、
   熔断防护 (_chain_*)、搜索并发门闸 (LxSearchGate) 以及旧版状态兼容；
4. 播放编排由 playback_policy 负责：音质 -> 脚本顺序 -> 同脚本原平台/其他平台，
   跨平台候选必须通过歌名/歌手/版本/时长可信匹配。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import math
import json
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
import urllib.parse

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
import httpx
from pydantic import BaseModel, Field

from lxserver_client import (
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_LXSERVER_URL,
    LxServerClient,
    SUPPORTED_PLATFORMS,
    is_lxserver_unreachable,
    map_lxserver_song,
    normalize_source,
    parse_interval_to_seconds,
    SourceOperationBusy,
    serialize_source_operation,
)
from playback_policy import (
    CandidateFinder,
    PolicySettings,
    build_source_catalog,
    from_lx_quality,
    media_ext_from_content_type,
    media_ext_from_url,
    merge_reorder,
    merge_source_order,
    normalize_quality,
    ordered_scripts,
    plan_steps,
    quality_ladder,
    read_policy_settings,
    ScriptInfo,
    to_lx_quality,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 仓库根：复用 proxy/fnlog
from proxy.fnlog import diag as fn_diag  # noqa: E402
from proxy.fnlog import setup_logging, redact  # noqa: E402
from proxy.env_merge import parse_env_file  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("fnmusic.lxmusic")
# 本地文件日志（logs/lxmusic.log，按天滚动保留 3 天）；失败静默降级为仅 stdout
setup_logging("fnmusic.lxmusic", "lxmusic")

SERVICE_VERSION = "2.7.0"
HEALTH_SNAPSHOT_TIMEOUT = 1.5  # 小于 proxy 的约 2 秒探活预算
SOURCE_INIT_CONFIRM_TIMEOUT = 10.0
SOURCE_INIT_POLL_INTERVAL = 0.25
UA_PC = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

# 配置字典
CONF: dict[str, Any] = {
    "sources": [s.strip() for s in os.environ.get("LX_SOURCES", "kg,wy,mg,kw,tx").split(",") if s.strip()],
    "search_timeout": float(os.environ.get("LX_SEARCH_TIMEOUT", "12.0")),
    "limit_per_source": int(os.environ.get("LX_LIMIT_PER_SOURCE", "20")),
    # issue #55：失败路径预算收紧——首播点播 5-7s 才出声的主因是解析失败吃满
    # 大预算才降档/换源（单轮 resolver 12s + 探活 5s 叠加远超播放器起播耐心）。
    # 成功路径本就在 1-3s 内返回，不受影响；收紧的是"坏链/慢源"的失败上限。
    "url_timeout": float(os.environ.get("LX_URL_TIMEOUT", "15.0")),
    "resolver_timeout": float(os.environ.get("LX_RESOLVER_TIMEOUT", "8.0")),
    "cache_max": int(os.environ.get("LX_CACHE_MAX", "2000")),
    "cache_ttl": float(os.environ.get("LX_CACHE_TTL", "604800.0")),
    "url_cache_ttl": float(os.environ.get("LX_URL_CACHE_TTL", "600.0")),
    "url_cache_neg_ttl": float(os.environ.get("LX_URL_CACHE_NEG_TTL", "60.0")),
    "url_cache_max": int(os.environ.get("LX_URL_CACHE_MAX", "512")),
    "probe_timeout": float(os.environ.get("LX_PROBE_TIMEOUT", "3.0")),
    "data_dir": os.environ.get("LX_DATA_DIR", "/data/lxmusic"),
}

_TIMEOUT_ENV_KEYS: tuple[tuple[str, str, float], ...] = (
    ("url_timeout", "LX_URL_TIMEOUT", 15.0),
    ("resolver_timeout", "LX_RESOLVER_TIMEOUT", 8.0),
    ("probe_timeout", "LX_PROBE_TIMEOUT", 3.0),
)
_PERSISTED_ENV_CACHE: "tuple[tuple[str, int, int], dict[str, str]] | None" = None


def _persisted_env_paths() -> list[Path]:
    configured_path = os.environ.get("LX_ENV_PATH")
    if configured_path:
        return [Path(configured_path)]
    return [
        Path(os.environ.get("WEBUI_REPO_DIR", "/repo")) / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]


def _persisted_env_values() -> dict[str, str]:
    global _PERSISTED_ENV_CACHE
    for path in _persisted_env_paths():
        try:
            stat = path.stat()
            fingerprint = (str(path), stat.st_mtime_ns, stat.st_size)
            if _PERSISTED_ENV_CACHE and _PERSISTED_ENV_CACHE[0] == fingerprint:
                return dict(_PERSISTED_ENV_CACHE[1])
            entries, _ = parse_env_file(path)
            values = dict(entries)
            _PERSISTED_ENV_CACHE = (fingerprint, values)
            return dict(values)
        except OSError:
            continue
    return dict(os.environ)


def _reload_timeout_settings() -> dict[str, float]:
    values = _persisted_env_values()
    changed = False
    for conf_key, env_key, default in _TIMEOUT_ENV_KEYS:
        raw = values.get(env_key, os.environ.get(env_key, str(default)))
        try:
            parsed = float(str(raw).strip())
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(parsed) or not 1.0 <= parsed <= 300.0:
            continue
        if CONF[conf_key] != parsed:
            CONF[conf_key] = parsed
            changed = True
    if changed:
        global _URL_CACHE_GEN
        for key, entry in list(_URL_CACHE.items()):
            if entry.get("data") is None:
                _URL_CACHE.pop(key, None)
        _CHAIN_HEALTH.clear()
        _URL_CACHE_GEN += 1
    return {key: float(CONF[key]) for key, _, _ in _TIMEOUT_ENV_KEYS}

# 全局客户端实例
LXSERVER = LxServerClient(
    base_url=os.environ.get("LXSERVER_URL", DEFAULT_LXSERVER_URL),
    admin_password=os.environ.get("LXSERVER_ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD),
    timeout=CONF["resolver_timeout"],
    # songInfo 磁盘持久化（issue #49）：重启后榜单/搜索反查缓存不丢，
    # 关键词型源脚本冷启动仍能按真实歌名歌手取链
    store_path=os.path.join(CONF["data_dir"], "songinfo_store.json"),
)

# 内存曲目缓存: id -> {"item": dict, "ts": float}
_SONG_CACHE: dict[str, dict] = {}
# 播放直链缓存: (canonical_id, tier) -> {"data": dict | None, "ts": float}；data=None 为失败负缓存
_URL_CACHE: dict[tuple[str, str], dict] = {}
# 同键在途解析合并：key -> Future（与 musicdl SingleFlight 同型的 shielded-future 去重）
_URL_INFLIGHT: dict[tuple[int, str, str, bool], asyncio.Future] = {}
# 源代计数：切源/重导入脚本时自增，代间在途解析的回写据此丢弃，防止串源
_URL_CACHE_GEN = 0
_STATS = {"searches": 0, "url_resolutions": 0, "url_cache_hits": 0, "errors": 0}

# 跨平台候选：每平台 3 个、搜索并发 2、结果缓存 60 秒（播放策略模块实现）
_CANDIDATES = CandidateFinder()
# 三项策略的最近快照签名。策略变化时推进源代并失效候选缓存，阻止旧任务回写。
_POLICY_SIGNATURE: tuple | None = None
_SOURCE_ORDER_FILENAME = "source_order.json"

# 探活前缀字节数
_PROBE_BYTES = 4096

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
    """清空播放直链缓存并推进源代计数：切源/重导入脚本后旧直链一律作废，防止串源。

    候选搜索缓存同样依赖脚本顺序与配置代，必须一起失效，避免排序后仍用旧候选。
    """
    _URL_CACHE.clear()
    global _URL_CACHE_GEN
    _URL_CACHE_GEN += 1
    finder = globals().get("_CANDIDATES")
    if finder is not None:
        finder.invalidate()


async def _refresh_cached_url(
    client: httpx.AsyncClient, data: dict, probe_timeout: float | None = None,
) -> dict | None:
    """对过期的缓存直链做 Range 前缀探活续期。

    仅请求 CDN 少量前缀字节、不消耗按次计量的音源解析额度；仍可用则返回
    缓存条目（上游重定向时更新最终 URL 与总大小），失效返回 None。"""
    url = data.get("url")
    if not url:
        return None
    # issue #54：续期探活与续期结果共用同一 UA；空 headers 的旧缓存条目借续期
    # 补上探活 UA，避免续期成功后播放仍拿不到 UA 被拒 403
    headers = dict(data.get("headers") or {})
    headers.setdefault("User-Agent", UA_PC)
    try:
        ok, final_url, _ct, size = await probe_url(client, url, headers, timeout=probe_timeout)
    except Exception:
        return None
    if not ok:
        return None
    refreshed = dict(data)
    refreshed["headers"] = headers
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
    budget: float | None = None,
    fresh: bool = False,
    timeout_settings: "dict[str, float] | None" = None,
) -> dict | None:
    """带成功缓存/失败负缓存/同键在途合并的播放直链解析。

    - 成功结果按「曲目 + 音质档」缓存 url_cache_ttl 秒，有效期内零额度复用；
    - TTL 到期先探活旧链续期（不耗音源额度），失效才重新解析；
    - 失败负缓存 url_cache_neg_ttl 秒，短时间内同键重复请求直接快速失败；
    - fresh=True 显式旁路缓存强制重新解析（代理重试路径确认旧链失效后使用）；
    - 同键并发共享一次实际解析；源代计数变化后代间结果不回写。"""
    canonical_id = item.get("id") or f"lx:{src}:{item.get('songmid', '')}"
    settings = timeout_settings or _reload_timeout_settings()
    tiers = _quality_tiers(quality)
    key = (canonical_id, tiers[0] if tiers else "standard")
    gen = _URL_CACHE_GEN
    effective_budget = settings["url_timeout"] if budget is None else float(budget)
    deadline = time.monotonic() + max(effective_budget, 1.0)

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

    remaining = deadline - time.monotonic()
    return await _resolve_url_inflight(
        client, src, item, quality, max(remaining, 1.0), key, gen, fresh, settings)


async def _resolve_url_inflight(
    client: httpx.AsyncClient,
    src: str,
    item: dict,
    quality: str,
    budget: float,
    key: tuple[str, str],
    gen: int,
    fresh: bool = False,
    timeout_settings: "dict[str, float] | None" = None,
) -> dict | None:
    """探活及解析整体合并；不同源代和强制刷新不共享旧探活任务。"""
    inflight_key = (gen, *key, fresh)
    existing = _URL_INFLIGHT.get(inflight_key)
    if existing is not None:
        # issue #45：同键并发跟随者共享领队的实际解析，不再重复消耗音源额度
        fn_diag(logger, "url_inflight", op="join", song=str(key[0])[:60], quality=str(key[1]))
        return await asyncio.shield(existing)

    fut = asyncio.get_running_loop().create_future()
    _URL_INFLIGHT[inflight_key] = fut
    settings = timeout_settings or _reload_timeout_settings()
    try:
        deadline = time.monotonic() + budget
        result = None
        refreshed_hit = False
        if not fresh:
            entry = _URL_CACHE.get(key)
            if entry and entry.get("data") is not None:
                remaining = deadline - time.monotonic()
                try:
                    async with asyncio.timeout(max(remaining, 0.001)):
                        result = await _refresh_cached_url(
                            client, entry["data"], settings["probe_timeout"])
                except TimeoutError:
                    result = None
                refreshed_hit = result is not None
        if not refreshed_hit:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                result = await resolve_and_probe(
                    client, src, item, quality,
                    budget=remaining,
                    resolver_timeout=settings["resolver_timeout"],
                    probe_timeout=settings["probe_timeout"],
                    generation=gen,
                )
        else:
            _STATS["url_cache_hits"] += 1
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
        if _URL_INFLIGHT.get(inflight_key) is fut:
            _URL_INFLIGHT.pop(inflight_key, None)

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


def _normalize_quality(quality: str) -> str:
    """统一档位名；共享模块异常时回退到播放默认 lossless。"""
    try:
        return str(normalize_quality(quality) or "lossless")
    except Exception:
        return "lossless"


def _quality_tiers(quality: str) -> list[str]:
    """目标档严格向下阶梯；洛雪旧 high 由共享模块归一为 exhigh（320k）。"""
    try:
        tiers = [str(tier) for tier in quality_ladder(quality) if str(tier)]
    except Exception:
        tiers = []
    return tiers or ["standard"]


def _tier_to_lx_quality(tier: str) -> str:
    try:
        return str(to_lx_quality(tier) or "128k")
    except Exception:
        return "128k"


# 兼容旧代理契约的 actual_tier 粗粒度：无损及以上都按 lossless，320k/192k 按 high。
def _legacy_tier(quality: str) -> str:
    if quality in ("lossless", "hires", "jymaster", "sky"):
        return "lossless"
    if quality in ("exhigh", "higher"):
        return "high"
    return "standard"


_QUALITY_BITRATE = {
    "standard": 128000,
    "higher": 192000,
    "exhigh": 320000,
    "lossless": 960000,
    "hires": 2304000,
    "jymaster": 2304000,
    "sky": 2304000,
}


# ------------------------------------------------------------- 播放策略配置 --

def _policy_settings() -> PolicySettings:
    """读取三项 env 策略；.env 热更新逐键覆盖进程环境，缺失键继续用进程环境。"""
    values = dict(os.environ)
    values.update(_persisted_env_values())
    return read_policy_settings(values)


def _sync_policy_generation(settings: PolicySettings) -> None:
    """策略变化时推进源代：清空旧直链、候选缓存和熔断，旧在途结果不得回写。"""
    global _POLICY_SIGNATURE
    signature = (
        bool(settings.auto_switch),
        bool(settings.platform_preferred),
        tuple(settings.source_order),
    )
    if _POLICY_SIGNATURE is not None and signature != _POLICY_SIGNATURE:
        _url_cache_reset()
        _CHAIN_HEALTH.clear()
        fn_diag(logger, "policy", op="generation_bump")
    _POLICY_SIGNATURE = signature


def _source_order_path() -> Path:
    return Path(str(CONF["data_dir"])) / _SOURCE_ORDER_FILENAME


def _as_source_id_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    ids: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        sid = item.strip()
        if sid and sid not in ids:
            ids.append(sid)
    return ids


def _read_source_order_file() -> dict:
    """读取排序持久文件；损坏/缺失静默当作空，不阻断播放。"""
    try:
        raw = json.loads(_source_order_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"order": [], "env_order": []}
    if not isinstance(raw, dict):
        return {"order": [], "env_order": []}
    return {
        "order": _as_source_id_list(raw.get("order")),
        "env_order": _as_source_id_list(raw.get("env_order")),
    }


def _write_source_order_file(order: list[str], env_order: list[str]) -> None:
    """原子写入排序文件：tmp + os.replace，避免半截 JSON。"""
    path = _source_order_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "order": [str(item) for item in order if str(item)],
        "env_order": [str(item) for item in env_order if str(item)],
    }
    fd, tmp = tempfile.mkstemp(prefix=".source-order-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _effective_source_order(
    catalog_ids: list[str],
    env_order: tuple[str, ...] | list[str],
    *,
    persist_cleanup: bool = False,
) -> list[str]:
    """合并 env、排序文件与当前源目录，得到完整稳定顺序。

    规则：env 是用户配置基线；若 env 与文件记录的 env 快照一致，文件里的
    最新 POST 排序优先；env 被改写则 env 覆盖旧文件。删除的 ID 在合并时
    丢弃；`persist_cleanup=True` 时把清理后的顺序写回磁盘（默认只在管理
    接口/状态查询时写，播放路径不写盘）。
    """
    stored = _read_source_order_file()
    file_order = stored["order"]
    env_list = [str(item) for item in (env_order or []) if str(item)]
    if env_list:
        if file_order and env_list == stored["env_order"]:
            chosen = file_order
        else:
            # env 可能只列激活脚本：按槽位合并，保留停用脚本的历史位置。
            base = file_order or merge_source_order(catalog_ids, [])
            chosen = merge_reorder(base, env_list)
    else:
        chosen = file_order
    merged = merge_source_order(catalog_ids, chosen)
    if persist_cleanup and merged != file_order:
        try:
            _write_source_order_file(merged, env_list)
        except OSError as exc:
            logger.warning("写入洛雪脚本排序失败: %s", exc)
    return merged


def _policy_payload(sources: list[dict], *, persist_cleanup: bool = False) -> dict:
    """给 WebUI 源管理返回统一策略视图（自动切换、平台优选、稳定顺序、配置代）。"""
    settings = _policy_settings()
    _sync_policy_generation(settings)
    catalog = build_source_catalog(sources)
    order = _effective_source_order(
        [script.id for script in catalog],
        settings.source_order,
        persist_cleanup=persist_cleanup,
    )
    scripts = ordered_scripts(catalog, order)
    active = [script.id for script in scripts if script.ready]
    return {
        "auto_switch": bool(settings.auto_switch),
        "platform_preferred": bool(settings.platform_preferred),
        "source_order": order,
        "active_source_order": active,
        "generation": _URL_CACHE_GEN,
    }


def _resolve_policy_snapshot(
    sources: list[dict],
) -> tuple[PolicySettings, list[ScriptInfo], list[ScriptInfo]]:
    """播放前的一次性策略快照：策略 env -> 源目录 -> 稳定顺序。"""
    settings = _policy_settings()
    _sync_policy_generation(settings)
    catalog = build_source_catalog(sources)
    order = _effective_source_order(
        [script.id for script in catalog], settings.source_order,
    )
    return settings, catalog, ordered_scripts(catalog, order)


class SourceStateQueryError(RuntimeError):
    """源管理列表未能提供可信快照，不代表脚本失效，也不应写负缓存或熔断。"""


async def _policy_source_list(deadline: float) -> list[dict]:
    """在总预算内读取源列表；超时/内部服务故障上浮为服务错误，不触发脚本熔断。"""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SourceStateQueryError("读取洛雪源列表前预算已耗尽")
    try:
        async with asyncio.timeout(min(remaining, 2.0)):
            return await LXSERVER.list_custom_sources()
    except TimeoutError as exc:
        logger.warning("读取洛雪源列表超时，播放解析放弃")
        raise SourceStateQueryError("读取洛雪源列表超时") from exc
    except Exception as exc:
        logger.warning("读取洛雪源列表失败，播放解析放弃: %s", exc)
        detail = redact(str(exc)).strip() or type(exc).__name__
        raise SourceStateQueryError(f"lxserver 源状态查询失败: {detail}"[:1000]) from exc


def _platform_headers(platform: str) -> dict[str, str]:
    """探活与实际播放共用同一请求头；Referer 按实际来源平台补齐。"""
    headers = {"User-Agent": UA_PC}
    if platform == "kg":
        headers["Referer"] = "https://www.kugou.com/"
    elif platform == "tx":
        headers["Referer"] = "https://y.qq.com/"
    return headers


def _exclude_others(script_id: str, catalog: list[ScriptInfo]) -> list[str] | None:
    """排除其他脚本的稳定 ID，把上游自动切换固定到当前脚本。

    只传 ID、不传 name：脚本名可能重复，按名字排除会把当前脚本一起排除。
    不改任何脚本的全局启用状态；只影响本次 /api/music/url 请求。
    """
    others: list[str] = []
    for script in catalog:
        if script.id != script_id and script.id not in others:
            others.append(script.id)
    return others or None


def _chain_key(script_id: str, platform: str, generation: int) -> str:
    """熔断按脚本 + 平台 + 配置代隔离，不能用平台全局键误伤其他脚本。"""
    return f"policy:{int(generation)}:{script_id}:{platform}"


@dataclass
class _AttemptResult:
    """一次固定脚本/平台的解析结果分类。"""

    data: dict | None = None
    failure: bool = False
    timeout: bool = False
    service_error: bool = False


def _candidate_song_info(candidate: dict) -> dict:
    """候选曲目 -> lxserver songInfo；搜索阶段已缓存原始对象时直接复用。"""
    track_id = str(candidate.get("id") or "")
    info = LXSERVER.get_cached_song_info(track_id) if track_id else None
    if info:
        return info
    return LXSERVER.synthesize_song_info(track_id, candidate)


async def _search_candidates(platform: str, keyword: str, limit: int) -> list[dict]:
    """候选搜索入口：直接走 lxserver 搜索，不占用全局 LxSearchGate。"""
    return await LXSERVER.search(keyword, source=platform, limit=limit)


async def _attempt_pinned(
    client: httpx.AsyncClient,
    *,
    script: ScriptInfo,
    platform: str,
    song_info: dict,
    resolved_track_id: str,
    track_id: str,
    quality: str,
    requested_quality: str,
    deadline: float,
    resolver_timeout: float,
    probe_timeout: float,
    exclude_sources: list[str] | None,
) -> _AttemptResult:
    """固定脚本、固定平台、固定档位解析一次，并做同请求头 Range 探活。"""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _AttemptResult(timeout=True)
    lx_quality = _tier_to_lx_quality(quality)
    headers = _platform_headers(platform)
    per_call_timeout = max(0.01, min(remaining, resolver_timeout))
    try:
        async with asyncio.timeout(per_call_timeout):
            response = await LXSERVER.get_music_url(
                song_info,
                lx_quality,
                exclude_api_sources=exclude_sources,
                request_timeout=per_call_timeout,
            )
    except TimeoutError:
        # 单次解析超时由外层继续下一个脚本/档位；预算耗尽与取消都不能算脚本失败。
        return _AttemptResult(timeout=True)
    except Exception as exc:
        if is_lxserver_unreachable(exc):
            logger.warning("lxserver 播放解析内部故障: %s", exc)
            return _AttemptResult(service_error=True)
        logger.debug("lxserver get_music_url 失败 script=%s platform=%s: %s",
                     script.id, platform, exc)
        return _AttemptResult(failure=True)

    if not response or not response.get("url"):
        # 200 空 url（脚本静默空返回）按该脚本/平台失败处理，交由计划换脚本。
        return _AttemptResult(failure=True)

    raw_url = str(response["url"])
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _AttemptResult(timeout=True)
    try:
        async with asyncio.timeout(max(0.01, min(remaining, probe_timeout))):
            ok, final_url, content_type, size = await probe_url(
                client, raw_url, headers,
                timeout=max(0.01, min(remaining, probe_timeout)),
            )
    except Exception:
        ok = False
        final_url, content_type, size = raw_url, "", 0

    if not ok:
        logger.info("探活拒绝 %s quality=%s ct=%s script=%s platform=%s url=%.120s",
                    track_id, quality, content_type or "", script.id, platform, raw_url)
        return _AttemptResult(failure=True)

    # 魔数探活已确认格式；MIME/最终 URL 都识别不了时宁可失败，也不硬写 mp3。
    ext = media_ext_from_content_type(content_type) or media_ext_from_url(final_url)
    if not ext:
        logger.warning("探活通过但无法识别媒体格式 %s ct=%r url=%.120s",
                       track_id, content_type, final_url)
        return _AttemptResult(failure=True)

    reported_lx = str(response.get("type") or "").strip().lower()
    actual_quality = from_lx_quality(reported_lx) or quality
    actual_lx_quality = reported_lx or lx_quality
    try:
        upstream_br = int(response.get("br") or 0)
    except (TypeError, ValueError):
        upstream_br = 0
    br = upstream_br or _QUALITY_BITRATE.get(actual_quality, 128000)
    fallback_reason = ""
    if actual_quality != requested_quality:
        fallback_reason = (
            f"requested {requested_quality}; resolved {actual_quality} "
            f"({actual_lx_quality})"
        )
    return _AttemptResult(data={
        "id": track_id,
        "url": final_url,
        "ext": ext,
        "file_size": size,
        "actual_tier": _legacy_tier(actual_quality),
        "actual_quality": actual_quality,
        "requested_quality": requested_quality,
        "actual_lx_quality": actual_lx_quality,
        "quality_evidence": {
            "probe": ext,
            "content_type": content_type or "",
            "reported_lx_quality": reported_lx or None,
            "source": "lxserver",
        },
        "fallback_reason": fallback_reason,
        "resolved_track_id": resolved_track_id,
        "source_platform": platform,
        "source_script": script.id,
        "source_script_name": script.name,
        "resolver": "lxserver",
        "headers": dict(headers),
        "br": br,
    })


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
    timeout: float | None = None,
) -> tuple[bool, str, str, int]:
    """有界流式前缀探活。返回 (是否有效, 最终URL, MIME/扩展名, 总大小)。"""
    h = {k: v for k, v in (headers or {}).items() if k.lower() not in ("range", "accept-encoding")}
    h.setdefault("User-Agent", UA_PC)
    h.update({"Range": f"bytes=0-{_PROBE_BYTES - 1}", "Accept-Encoding": "identity"})
    try:
        probe_timeout = float(CONF["probe_timeout"] if timeout is None else timeout)
        async with asyncio.timeout(probe_timeout):
            curr_url = url
            for _ in range(6):  # 最多 5 次重定向
                async with client.stream(
                    "GET", curr_url, headers=h, follow_redirects=False, timeout=probe_timeout
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

def _track_song_info(track_id: str, item: dict) -> dict:
    """反查或合成原曲目的 songInfo；关键词型脚本依赖真实歌名歌手。"""
    song_info = LXSERVER.get_cached_song_info(track_id)
    if not song_info:
        song_info = LXSERVER.synthesize_song_info(track_id, item)
    return song_info


async def resolve_and_probe(
    client: httpx.AsyncClient,
    source: str,
    item: dict,
    quality: str = "lossless",
    budget: float = 20.0,
    resolver_timeout: float | None = None,
    probe_timeout: float | None = None,
    generation: int | None = None,
) -> dict | None:
    """按「音质 -> 脚本顺序 -> 同脚本原平台/其他平台」执行一次总预算解析。

    与旧实现的区别：
    - 计划由 playback_policy.plan_steps 一次生成，不再有每档最多三脚本的第二套梯度；
    - 目标档位遍历完所有就绪脚本后才降档，跨平台候选先经过可信匹配；
    - 每次请求用 excludeApiSources 排除其他脚本，只固定当前脚本，不改全局启用态；
    - 探活/搜索/降档共用同一个 deadline；取消、预算耗尽、lxserver 内部故障不计熔断；
    - 熔断键是脚本 + 平台 + 配置代，单个脚本坏链不会误伤同平台其他脚本。
    """
    src = normalize_source(source)
    if not src:
        return None
    active_generation = _URL_CACHE_GEN if generation is None else generation
    effective_resolver_timeout = float(
        CONF["resolver_timeout"] if resolver_timeout is None else resolver_timeout
    )
    effective_probe_timeout = float(
        CONF["probe_timeout"] if probe_timeout is None else probe_timeout
    )
    deadline = time.monotonic() + max(float(budget), 0.001)
    target_quality = _normalize_quality(quality)
    track_id = item.get("id") or f"lx:{src}:{item.get('songmid', '')}"

    # 源列表本身也是内部服务调用：失败/超时上浮为 503，不写负缓存、不记脚本熔断。
    sources = await _policy_source_list(deadline)
    settings, catalog, scripts = _resolve_policy_snapshot(sources)
    steps = plan_steps(
        _quality_tiers(target_quality),
        scripts,
        src,
        auto_switch=settings.auto_switch,
        platform_preferred=settings.platform_preferred,
    )
    if not steps:
        logger.debug("lx resolve no plan: source=%s quality=%s scripts=%d",
                     src, target_quality, len(scripts))
        return None

    original_info = _track_song_info(track_id, item)
    scripts_by_id = {script.id: script for script in scripts}
    # 跨平台候选按「脚本+档位」首次需要时并发预取；缓存 60 秒，后续档位零搜索。
    prefetched: dict[tuple[str, str], dict[str, list[dict]]] = {}
    attempted_tiers: list[str] = []
    failed_steps: set[tuple[str, str]] = set()
    budget_exhausted = False

    for step in steps:
        if active_generation != _URL_CACHE_GEN:
            return None
        if step.quality not in attempted_tiers:
            attempted_tiers.append(step.quality)
        if deadline - time.monotonic() <= 0:
            budget_exhausted = True
            break
        chain_key = _chain_key(step.script_id, step.platform, active_generation)
        if not _chain_available(chain_key):
            continue

        if step.platform == src:
            attempts: list[tuple[str, dict]] = [(track_id, original_info)]
        else:
            prefetch_key = (step.script_id, step.quality)
            if prefetch_key not in prefetched:
                script = scripts_by_id.get(step.script_id)
                others = [p for p in (script.platforms if script else ()) if p != src]
                prefetched[prefetch_key] = await _CANDIDATES.find_many(
                    _search_candidates, item, others, deadline, active_generation,
                )
            attempts = []
            for candidate in prefetched[prefetch_key].get(step.platform) or []:
                candidate_id = str(candidate.get("id") or "")
                if candidate_id:
                    attempts.append((candidate_id, _candidate_song_info(candidate)))
        if not attempts:
            continue

        exclude_sources = _exclude_others(step.script_id, catalog)
        step_failed = False
        for resolved_id, song_info in attempts:
            attempt = await _attempt_pinned(
                client,
                script=scripts_by_id[step.script_id],
                platform=step.platform,
                song_info=song_info,
                resolved_track_id=resolved_id,
                track_id=track_id,
                quality=step.quality,
                requested_quality=target_quality,
                deadline=deadline,
                resolver_timeout=effective_resolver_timeout,
                probe_timeout=effective_probe_timeout,
                exclude_sources=exclude_sources,
            )
            if attempt.service_error:
                # 内部服务故障上浮为 503：不写负缓存、不写脚本熔断，也不继续消耗预算。
                raise SourceStateQueryError("lxserver 播放解析内部故障")
            if attempt.data:
                _chain_record_success(chain_key)
                result = dict(attempt.data)
                result["attempted_tiers"] = list(attempted_tiers)
                return result
            if attempt.timeout:
                # 单次超时只是本脚本/本候选慢；预算耗尽时结束整个计划，不记熔断。
                if deadline - time.monotonic() <= 0:
                    budget_exhausted = True
                    break
                continue
            step_failed = True
        if step_failed:
            failed_steps.add((step.script_id, step.platform))
        if budget_exhausted:
            break

    if active_generation == _URL_CACHE_GEN and not budget_exhausted:
        for script_id, platform in failed_steps:
            _chain_record_failure(_chain_key(script_id, platform, active_generation))
    return None


def _source_supported_platforms(s: dict) -> list[str]:
    """提取单个源的声明平台（兼容 supportedSources/sources 键与 dict 形态），空则视为全平台。"""
    raw_supp = s.get("supportedSources") or s.get("sources") or []
    if isinstance(raw_supp, dict):
        raw_supp = list(raw_supp.keys())
    supported = [normalize_source(str(p)) for p in raw_supp if normalize_source(str(p))]
    return supported or list(SUPPORTED_PLATFORMS)


def _describe_source_snapshot(sources: list[dict]) -> dict:
    """配置启用与运行就绪分离；缺失状态表示尚未获得初始化结果，而非成功。"""
    enabled = [s for s in (sources or []) if isinstance(s, dict) and (s.get("enable") or s.get("enabled"))]
    active_sources = []
    for s in enabled:
        sid = str(s.get("id") or "")
        raw_url = str(s.get("sourceUrl") or "").strip()
        resolved_url = raw_url or (f"file://{_lx_source_fs_path(sid)}" if sid else "")
        status = str(s.get("status") or "loading")
        active_sources.append({
            "id": sid,
            "name": str(s.get("name") or ""),
            "version": str(s.get("version") or "1.0.0"),
            "url": resolved_url,
            "status": status,
            "error": redact(str(s.get("error") or "")),
            "initialized": status == "success",
            "platforms": _source_supported_platforms(s),
        })
    ready = sum(s["initialized"] for s in active_sources)
    failed = sum(s["status"] == "failed" for s in active_sources)
    pending = len(active_sources) - ready - failed
    if not active_sources:
        state = "unconfigured"
    elif ready:
        state = "ready" if ready == len(active_sources) else "degraded"
    else:
        state = "loading" if pending else "failed"
    result = {
        "configured": bool(active_sources),
        "url": "",
        "initialized": ready > 0,
        "last_error": "; ".join(f"{s['name'] or s['id']}: {s['error']}"
                                for s in active_sources if s["error"]),
        "source": None,
        "sources": active_sources,
        "active_count": len(active_sources),
        "state": state,
        "ready_count": ready,
        "failed_count": failed,
        "pending_count": pending,
    }
    if active_sources:
        first_src, first = enabled[0], active_sources[0]
        # 保留旧首源字段的身份；整体 initialized 则按所有明确成功源聚合。
        result["url"] = first_src.get("name") or first_src.get("id") or ""
        result["source"] = {
            "name": first_src.get("name", "lx-source"),
            "version": first_src.get("version", "1.0.0"),
            "author": first_src.get("author", ""),
            "platforms": {p: {"qualitys": ["128k", "320k", "flac"]} for p in first["platforms"]},
            "running": first["initialized"],
            "pid": 0,
            "uptime_s": 0,
        }
    return result


def _source_capabilities_snapshot(sources: list[dict], *, alive: bool = True, error: str = "") -> dict[str, dict]:
    enabled = [s for s in (sources or []) if isinstance(s, dict) and (s.get("enable") or s.get("enabled"))]
    supported: set[str] = set()
    for s in enabled:
        if s.get("status") == "success":
            supported.update(_source_supported_platforms(s))
    failed = [s for s in enabled if s.get("status") == "failed"]
    caps = {}
    for src in SUPPORTED_PLATFORMS:
        available = bool(alive and src in supported)
        if not alive:
            reason = error or "lxserver unready"
        elif not enabled:
            reason = "no active custom source"
        elif len(failed) == len(enabled):
            reason = "all active sources failed: " + redact(str(failed[0].get("error") or "init error"))
        elif not supported:
            reason = "active sources initializing"
        elif not available:
            reason = f"platform {src} not supported by active source"
        else:
            reason = ""
        caps[src] = {"search": alive, "playback_available": available,
                     "qualitys": ["128k", "320k", "flac"] if available else [], "reason": reason}
    return caps


async def source_capabilities() -> dict[str, dict]:
    """仅明确初始化成功的启用源提供播放能力。"""
    try:
        alive = await LXSERVER.is_alive()
        sources = await LXSERVER.list_custom_sources() if alive else []
        return _source_capabilities_snapshot(sources, alive=alive)
    except Exception as exc:
        return _source_capabilities_snapshot([], alive=False, error=_source_query_error(exc))


async def _query_source_list() -> list[dict]:
    try:
        return await LXSERVER.list_custom_sources()
    except Exception as exc:
        raise SourceStateQueryError(_source_query_error(exc)) from exc


async def describe_user_source() -> dict:
    # 请求失败必须由调用方作为服务故障处理，不能伪装成“没有配置源”。
    return _describe_source_snapshot(await _query_source_list())


def _source_query_error(exc: Exception) -> str:
    detail = redact(str(exc)).strip() or type(exc).__name__
    if isinstance(exc, SourceStateQueryError):
        return detail
    return f"lxserver 源状态查询失败: {detail}"[:1000]


def _source_query_response(exc: Exception) -> JSONResponse:
    error = _source_query_error(exc)
    return JSONResponse(status_code=503, content={
        "ok": False, "error": error, "category": "service_unavailable",
        "stage": "lxmusic_to_lxserver",
        "data": {"state": "unavailable", "initialized": False, "last_error": error},
    })


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


def _persisted_source_targets() -> list[str] | None:
    """最新 .env 为准；缺少可读的配置才回退进程环境，None 表示旧单源配置。"""
    from proxy.env_merge import parse_env_file

    configured_path = os.environ.get("LX_ENV_PATH")
    paths = [Path(configured_path)] if configured_path else [
        Path(os.environ.get("WEBUI_REPO_DIR", "/repo")) / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]
    values = dict(os.environ)
    from_disk = False
    for path in paths:
        try:
            with path.open("r", encoding="utf-8"):
                pass
            entries, _ = parse_env_file(path)
            values = dict(entries)
            from_disk = True
            break
        except OSError:
            continue
    try:
        items = json.loads(values.get("LX_SOURCE_LIST") or "[]")
    except (ValueError, TypeError):
        return None
    if not isinstance(items, list):
        return None
    if not items and "LX_SOURCE_LIST" in values and not values.get("LX_SOURCE_URL"):
        return []
    if not any(isinstance(i, dict) and "active" in i for i in items):
        if from_disk:
            url = str(values.get("LX_SOURCE_URL") or "").strip()
            return [_normalize_legacy_file_url(url)] if url else []
        return None
    urls = [str(i.get("url") or "").strip() for i in items
            if isinstance(i, dict) and i.get("active") and i.get("url")]
    # 兼容此前 .env 写入无激活列表但保留旧单源指针的情况。
    if not urls and not from_disk and values.get("LX_SOURCE_URL"):
        urls = [str(values["LX_SOURCE_URL"]).strip()]
    return list(dict.fromkeys(_normalize_legacy_file_url(url) for url in urls if url))


async def _reconcile_source_targets(urls: list[str]) -> None:
    """按持久化目标逐项恢复，并关闭未在目标集合中的启用源。"""
    target_ids: set[str] = set()
    all_ok = True
    for url in urls:
        latest = _persisted_source_targets()
        if latest is not None and latest != urls:
            # 保存配置与启动恢复交错时，不再依据旧快照激活。
            return
        try:
            res = await source_set(SourceBody(url=url))
            if isinstance(res, JSONResponse):
                res = json.loads(res.body)
            if not res.get("ok"):
                all_ok = False
                logger.warning("自动激活洛雪源失败: %s (%s)", url, res.get("error") or "")
            elif res.get("source_id"):
                target_ids.add(str(res["source_id"]))
                continue
        except Exception as exc:
            all_ok = False
            logger.warning("自动激活洛雪源失败: %s: %s", url, exc)
        if url.startswith(("http://", "https://")):
            existing = await _find_lxserver_source(by_url=url)
        else:
            parsed = urllib.parse.urlparse(url)
            source_id = urllib.parse.unquote(parsed.path).rsplit("/", 1)[-1]
            existing = await _find_lxserver_source(by_id=source_id, by_name=source_id.removesuffix(".js"))
            if not existing and os.path.isfile(urllib.parse.unquote(parsed.path)):
                script = Path(urllib.parse.unquote(parsed.path)).read_text(encoding="utf-8", errors="replace")
                existing = await _find_lxserver_source(by_name=_parse_script_head_meta(script).get("name") or "")
        if existing:
            target_ids.add(str(existing.get("id") or ""))
    latest = _persisted_source_targets()
    if not all_ok or (latest is not None and latest != urls):
        return
    for s in await LXSERVER.list_custom_sources():
        sid = str(s.get("id") or "")
        if sid and sid not in target_ids and (s.get("enabled") or s.get("enable")):
            await LXSERVER.set_source_enabled(sid, False)
            _url_cache_reset()
    _CHAIN_HEALTH.clear()


async def _bootstrap_migration():
    # 等待 lxserver 就绪不占用管理锁：启动期（lxserver 慢启动最长约 10s）
    # WebUI 的激活请求不应被启动对账挡成 409。
    for _ in range(20):
        try:
            if await LXSERVER.is_alive():
                break
        except Exception:
            pass
        await asyncio.sleep(0.5)
    await _migrate_persisted_sources()


@serialize_source_operation
async def _migrate_persisted_sources():
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

        targets = _persisted_source_targets()
        if targets is not None:
            await _reconcile_source_targets(targets)
            return
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


@app.exception_handler(SourceStateQueryError)
async def source_query_error_handler(request: Request, exc: SourceStateQueryError):
    return _source_query_response(exc)


@app.exception_handler(SourceOperationBusy)
async def source_operation_busy_handler(request: Request, exc: SourceOperationBusy):
    return JSONResponse(status_code=409, content={
        "ok": False, "error": "源管理操作正在进行，请稍后重试",
        "category": "source_operation_busy", "stage": "lxmusic_to_lxserver",
    })


@app.exception_handler(ValueError)
async def value_error_handler(_request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})


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
    # 两个底层请求并发且有总预算；状态与能力复用同一份源列表。
    error = ""
    sources: list[dict] = []
    try:
        async with asyncio.timeout(HEALTH_SNAPSHOT_TIMEOUT):
            alive, sources = await asyncio.gather(LXSERVER.is_alive(), LXSERVER.list_custom_sources())
        if not alive:
            error = "lxserver unreachable"
    except Exception as exc:
        error = _source_query_error(exc)
    if error:
        user_src = {"state": "unavailable", "initialized": False, "last_error": error}
    else:
        user_src = _describe_source_snapshot(sources)
    result = {
        "ok": not bool(error),
        "service": "fnmusic-lxmusic",
        "version": SERVICE_VERSION,
        "sources": CONF["sources"],
        "user_source": user_src,
        "source_ready": bool(user_src["initialized"]),
        "circuit": chain_health_snapshot(),
        "capabilities": _source_capabilities_snapshot(sources, alive=not bool(error), error=error),
        "charts": ["kg", "kw", "wy"],
    }
    if error:
        result.update(error=error, category="service_unavailable", stage="lxmusic_to_lxserver")
        return JSONResponse(status_code=503, content=result)
    # 源失败是 readiness 问题，不让进程恢复逻辑反复重启健康的服务。
    return result


@app.get("/api/v1/search")
async def search_tracks(
    keyword: str = Query("", alias="keyword"),
    q: str = Query("", alias="q"),
    sources: str = Query(""),
    limit: int = Query(0),
    page: int = Query(1),
    scope: str = Header(default="global", alias="X-Fnmusic-Scope"),
):
    """跨平台聚合搜索。支持防抖门闸。"""
    kw = (keyword or q or "").strip()
    if not kw:
        return {"ok": True, "items": [], "errors": {}, "stats": dict(_STATS)}

    if limit <= 0:
        limit = CONF["limit_per_source"]

    wanted_raw = [s.strip() for s in (sources or "").split(",") if s.strip()]
    wanted = [normalize_source(s) for s in wanted_raw] if wanted_raw else CONF["sources"]
    wanted = [s for s in wanted if s in SUPPORTED_PLATFORMS]

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

    subkey = f"{page}:{limit}:{','.join(sorted(wanted))}"
    gate_res = await _SEARCH_GATE.run(scope, kw, _do_search, subkey=subkey)
    if gate_res is _LX_SUPERSEDED:
        return {"ok": True, "items": [], "superseded": True}
    if isinstance(gate_res, Exception):
        return _err(f"search failed: {gate_res}", 500)
    return gate_res


# 免登录榜单目标：平台 -> (榜单名, bangid 内置回退值)。
# bangid 优先按榜单名从 lxserver boards 接口动态解析（上游换榜 id 不再静默失效），
# boards 不可用或未命中时回退内置值；解析结果缓存 10 分钟（上游 boards 本就 max-age=600）。
_CHART_TARGETS: dict[str, tuple[str, str]] = {
    "kg": ("TOP500", "8888"),
    "kw": ("飙升榜", "93"),
    "wy": ("新歌榜", "3779629"),
}
_CHART_ID_TTL_S = 600.0
_CHART_ID_CACHE: dict[str, tuple[float, str]] = {}


async def _resolve_chart_id(src: str) -> str:
    target, default = _CHART_TARGETS[src]
    now = time.monotonic()
    hit = _CHART_ID_CACHE.get(src)
    if hit and now - hit[0] < _CHART_ID_TTL_S:
        return hit[1]
    bid = default
    try:
        boards = await LXSERVER.get_leaderboard_boards(src)
        for b in boards:
            if not isinstance(b, dict):
                continue
            if target in str(b.get("name") or ""):
                bid = str(b.get("bangid") or b.get("id") or default).strip()
                if "__" in bid:
                    bid = bid.split("__", 1)[1]
                if not bid:
                    bid = default
                break
    except Exception as e:
        logger.debug("leaderboard boards resolve failed for %s: %s", src, e)
    _CHART_ID_CACHE[src] = (now, bid)
    return bid


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

    for src in wanted:
        bid = await _resolve_chart_id(src)
        try:
            sub = await LXSERVER.get_leaderboard_list(src, bid, page=1)
            for it in sub:
                _cache_put(it)
                items.append(it)
        except Exception as e:
            errors[src] = str(e)

    _STATS["errors"] += len(errors)
    return {"ok": True, "items": items[:limit], "errors": errors, "charts": ["kg", "kw", "wy"]}


def _public_source(source: str) -> str:
    src = normalize_source(source)
    if not src or src not in SUPPORTED_PLATFORMS:
        raise ValueError(f"unsupported source {source!r}")
    return src


@app.get("/api/v1/public-playlists/capabilities")
async def public_playlist_capabilities():
    caps = await source_capabilities()
    return {
        "ok": True,
        "provider": "lx",
        "sources": [{
            "id": src,
            "name": {"kw": "酷我", "kg": "酷狗", "tx": "QQ", "wy": "网易", "mg": "咪咕"}.get(src, src.upper()),
            "playback_available": bool(caps.get(src, {}).get("playback_available")),
            "reason": caps.get(src, {}).get("reason") or "",
            "channels": [
                {"id": "top", "name": "热门与分类", "login_required": False},
                {"id": "search", "name": "搜索歌单", "login_required": False},
                {"id": "charts", "name": "排行榜", "login_required": False},
            ],
            "logged_in": False,
        } for src in SUPPORTED_PLATFORMS],
    }


@app.get("/api/v1/public-playlists/categories")
async def public_playlist_categories(source: str = Query(...), kind: str = Query("playlist")):
    src = _public_source(source)
    if kind not in ("playlist", "chart"):
        raise ValueError("kind must be playlist or chart")
    if kind == "chart":
        boards = await LXSERVER.get_leaderboard_boards(src)
        return {"ok": True, "categories": [{"id": "", "name": "全部"}], "tags": boards, "sorts": []}
    data = await LXSERVER.get_song_list_tags(src)
    tags = data.get("tags")
    if isinstance(tags, list):
        data["tags"] = [
            {**tag, "id": str(tag.get("id") or tag.get("name") or ""), "name": str(tag.get("name") or tag.get("id") or "")}
            if isinstance(tag, dict) else {"id": str(tag), "name": str(tag)}
            for tag in tags
        ]
    return {
        "ok": True,
        "categories": data.get("tags") or [],
        "tags": data.get("hotTag") or data.get("hotTags") or [],
        "sorts": data.get("sortList") or [],
    }


@app.get("/api/v1/public-playlists")
async def public_playlists(
    source: str = Query(...),
    channel: str = Query("top"),
    category: str = Query(""),
    sort: str = Query(""),
    keyword: str = Query(""),
    page: int = Query(1, ge=1, le=10000),
    limit: int = Query(30, ge=1, le=50),
    cursor: str = Query(""),
):
    src = _public_source(source)
    if channel == "charts":
        rows = await LXSERVER.get_leaderboard_boards(src)
        items = [LXSERVER.normalize_public_playlist(row, src, "chart") for row in rows if isinstance(row, dict)]
        return {"ok": True, "items": [it for it in items if it["id"]], "total": len(items), "has_more": False}
    if channel == "search":
        # lxserver 原生歌单搜索：SDK 固定页大小（默认 20），limit 不透传
        data = await LXSERVER.get_song_list_search(src, keyword, page)
        raw_items = data.get("list") or []
        items = [LXSERVER.normalize_public_playlist(row, src) for row in raw_items if isinstance(row, dict)]
        try:
            total = int(data.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        try:
            page_limit = max(1, int(data.get("limit") or 20))
        except (TypeError, ValueError):
            page_limit = 20
        return {
            "ok": True,
            "items": [it for it in items if it["id"]],
            "raw_count": len(raw_items),
            "total": total,
            "has_more": bool(raw_items) and (page * page_limit < total if total else True),
        }
    if channel != "top":
        raise ValueError(f"unsupported channel {channel!r}")
    if not sort:
        tags = await LXSERVER.get_song_list_tags(src)
        sorts = tags.get("sortList") or []
        if sorts:
            sort = str(sorts[0].get("id") or "")
    data = await LXSERVER.get_song_lists(src, category, sort, page)
    raw_items = data.get("list") or []
    items = [LXSERVER.normalize_public_playlist(row, src) for row in raw_items if isinstance(row, dict)]
    try:
        total = int(data.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    try:
        page_limit = max(1, int(data.get("limit") or 30))
    except (TypeError, ValueError):
        page_limit = 30
    return {
        "ok": True,
        "items": [it for it in items if it["id"]],
        "total": total,
        "has_more": bool(data.get("has_more")) or (page * page_limit < total if total else bool(raw_items)),
    }


@app.get("/api/v1/public-playlists/{playlist_id}/tracks")
async def public_playlist_tracks(
    playlist_id: str,
    source: str = Query(...),
    kind: str = Query("playlist", pattern="^(playlist|chart)$"),
):
    async with asyncio.timeout(60):
        return await _public_playlist_tracks(playlist_id, source, kind)


async def _public_playlist_tracks(playlist_id: str, source: str, kind: str):
    src = _public_source(source)
    raw_id = urllib.parse.unquote(playlist_id)
    items: list[dict] = []
    truncated = False
    if kind == "chart":
        page = 1
        seen_ids: set[str] = set()
        while len(items) < 1000 and page <= 100:
            rows = await LXSERVER.get_leaderboard_list(src, raw_id, page=page, strict=True)
            fresh = []
            for row in rows:
                identifier = str(row.get("id") or "")
                if identifier and identifier not in seen_ids:
                    seen_ids.add(identifier)
                    fresh.append(row)
            for row in fresh:
                items.append(row)
            # 部分平台忽略 page 并重复返回整榜，此时重复页就是结束条件。
            if not fresh:
                break
            page += 1
        if page > 100 and len(items) < 1000:
            raise RuntimeError("lxserver leaderboard pagination did not finish")
        if len(items) >= 1000:
            truncated = True
            items = items[:1000]
    else:
        page = 1
        hit_limit = False
        while len(items) < 1000:
            data = await LXSERVER.get_song_list_detail(src, raw_id, page=page)
            rows = data.get("list") or []
            for raw in rows:
                item = map_lxserver_song(raw, fallback_source=src)
                LXSERVER.cache_song_info(item["id"], raw)
                _cache_put(item)
                items.append(item)
                if len(items) >= 1000:
                    hit_limit = True
                    break
            try:
                total = int(data["total"]) if data.get("total") is not None else None
                page_limit = max(1, int(data.get("limit") or len(rows) or 1))
            except (TypeError, ValueError):
                total, page_limit = None, max(1, len(rows))
            if not rows and total is not None and len(items) < min(total, 1000):
                raise RuntimeError("lxserver playlist ended before reported total")
            if not rows or (total is not None and len(items) >= total) or (total is None and len(rows) < page_limit):
                break
            page += 1
        truncated = hit_limit and (total is None or total > 1000)
    for item in items:
        _cache_put(item)
    return {"ok": True, "data": items[:1000], "total": len(items), "truncated": truncated}


@app.get("/api/v1/public-playlists/{playlist_id}")
async def public_playlist(playlist_id: str, source: str = Query(...), kind: str = Query("playlist", pattern="^(playlist|chart)$")):
    src = _public_source(source)
    raw_id = urllib.parse.unquote(playlist_id)
    if kind == "chart":
        boards = await LXSERVER.get_leaderboard_boards(src)
        for row in boards:
            if not isinstance(row, dict):
                continue
            summary = LXSERVER.normalize_public_playlist(row, src, "chart")
            if summary["id"] == raw_id:
                return {"ok": True, "data": summary}
        raise ValueError(f"chart {raw_id} not found")
    data = await LXSERVER.get_song_list_detail(src, raw_id, page=1)
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    summary = LXSERVER.normalize_public_playlist({**info, "id": raw_id}, src)
    if not summary["name"]:
        summary["name"] = raw_id
    return {"ok": True, "data": summary}


def _meta_query(title: str, artist: str, album: str, duration: float, cover: str) -> dict:
    """代理透传的曲目元数据查询参数 → synthesize 元数据字典；无有效字段返回 {}。"""
    meta: dict[str, Any] = {}
    if str(title or "").strip():
        meta["title"] = str(title).strip()
    if str(artist or "").strip():
        meta["artist"] = str(artist).strip()
    if str(album or "").strip():
        meta["album"] = str(album).strip()
    try:
        if float(duration) > 0:
            meta["duration_s"] = float(duration)
    except (TypeError, ValueError):
        pass
    if str(cover or "").strip():
        meta["cover_url"] = str(cover).strip()
    return meta


def _ensure_song_info(track_id: str, meta: dict) -> None:
    """播放/详情前的反查兜底（issue #49）：songInfo 反查缺失时代理透传的真实
    标签立即合成并写回存储——关键词型源脚本按歌名歌手取链，冷启动合成"未知
    曲目/未知歌手"必然失败；写回后同曲后续解析/详情零成本命中。已有条目不覆盖。"""
    if not meta or LXSERVER.get_cached_song_info(track_id):
        return
    LXSERVER.cache_song_info(track_id, LXSERVER.synthesize_song_info(track_id, meta))


def _item_from_song_info(track_id: str, src: str) -> "dict | None":
    """songInfo 反查缓存（含磁盘持久层）→ 映射条目，用于映射缓存全冷时重建。"""
    raw = LXSERVER.get_cached_song_info(track_id)
    if not raw:
        return None
    item = map_lxserver_song(raw, fallback_source=src)
    item["id"] = track_id
    return item


@app.get("/api/v1/track/url")
async def track_url(
    id: str = Query("", alias="id"),
    guid: str = Query("", alias="guid"),
    quality: str = Query("lossless"),
    fresh: int = Query(0),
    title: str = Query(""),
    artist: str = Query(""),
    album: str = Query(""),
    duration: float = Query(0),
    cover: str = Query(""),
):
    """解析单曲播放直链。支持音质阶梯降级、媒体魔数探活与成功结果缓存/同键并发合并。

    fresh=1 旁路缓存强制重新解析：代理重试路径确认缓存直链失效后的有界刷新入口。
    title/artist/album/duration/cover（issue #49）：代理在 songInfo 反查缓存
    全冷（重启后点播存量歌单/收藏/历史）时透传曲目真实标签，兜底合成不再产出
    "未知曲目/未知歌手"，关键词型源脚本照常按歌名歌手取链。"""
    track_id = (id or guid or "").strip()
    src, identifier = parse_track_id(track_id)
    if not src or not identifier:
        return _err(f"invalid track id: {track_id}", 400)

    _STATS["url_resolutions"] += 1
    canonical_id = f"lx:{src}:{identifier}"
    _ensure_song_info(canonical_id, _meta_query(title, artist, album, duration, cover))
    cached = _cache_get(canonical_id) or _item_from_song_info(canonical_id, src) \
        or {"id": canonical_id, "lx_source": src}
    client = get_http(app)
    timeout_settings = _reload_timeout_settings()
    # 策略 env 改变时推进配置代并清空旧直链，避免继续复用旧顺序下的解析结果。
    _sync_policy_generation(_policy_settings())

    generation = _URL_CACHE_GEN
    try:
        result = await _resolve_url_cached(
            client, src, cached, quality,
            budget=timeout_settings["url_timeout"], fresh=bool(fresh), timeout_settings=timeout_settings
        )
    except SourceStateQueryError:
        # 内部 lxserver 不可用是服务故障（503），不能伪装成曲目不可播（404/502）。
        raise
    except Exception as e:
        _STATS["errors"] += 1
        logger.warning("lx url resolve %s failed: %s", track_id, e)
        return _err(f"resolve failed: {e}", 502)

    if not result:
        return _err("no playable url", 404)
    if generation != _URL_CACHE_GEN:
        return _err("source changed during resolution", 409)
    tiers = _quality_tiers(quality)
    selection = _URL_CACHE.get((cached.get("id") or canonical_id, tiers[0] if tiers else "standard"))
    remaining = (max(0.0, CONF["url_cache_ttl"] - (time.monotonic() - selection["ts"]))
                 if selection and selection.get("data") == result else 0.0)

    return {
        "ok": True,
        "data": {
            "id": track_id,
            "quality": quality,
            **{k: v for k, v in result.items() if k != "probed"},
            "reuse_for_s": remaining,
            "expiry_source": "policy",
        },
    }


@app.get("/api/v1/track/info")
async def track_info(
    id: str = Query("", alias="id"),
    guid: str = Query("", alias="guid"),
    title: str = Query(""),
    artist: str = Query(""),
    album: str = Query(""),
    duration: float = Query(0),
    cover: str = Query(""),
):
    track_id = (id or guid or "").strip()
    src, identifier = parse_track_id(track_id)
    if not src:
        return _err(f"invalid track id: {track_id}", 400)

    canonical_id = f"lx:{src}:{identifier}"
    _ensure_song_info(canonical_id, _meta_query(title, artist, album, duration, cover))
    cached = _cache_get(canonical_id)
    if not cached:
        # 映射缓存冷但 songInfo 反查在（含磁盘持久层，重启后）：重建映射条目
        cached = _item_from_song_info(canonical_id, src)
        if cached:
            _cache_put(cached)
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

async def _confirm_source_initialization(source_id: str) -> tuple[str, dict, dict | None]:
    """等待目标源而非聚合状态；不把查询失败或待定误当成脚本失败。"""
    deadline = time.monotonic() + SOURCE_INIT_CONFIRM_TIMEOUT
    snapshot = _describe_source_snapshot([])
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return source_id, snapshot, None
        try:
            async with asyncio.timeout(min(2.0, remaining)):
                sources = await _query_source_list()
        except TimeoutError as exc:
            raise SourceStateQueryError("lxserver 源状态查询超时") from exc
        snapshot = _describe_source_snapshot(sources)
        target = next((s for s in sources if str(s.get("id") or "") == source_id), None)
        if target is None:
            target = next((s for s in sources if str(s.get("name") or "") == source_id), None)
        if target:
            source_id = str(target.get("id") or source_id)
            if target.get("enabled") or target.get("enable"):
                if target.get("status") in ("success", "failed"):
                    return source_id, snapshot, target
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return source_id, snapshot, None
        await asyncio.sleep(min(SOURCE_INIT_POLL_INTERVAL, remaining))


class SourceBody(BaseModel):
    url: str = ""
    script: str = ""
    enabled: bool = True  # false 表示停用该源（多源叠加语义下的取消激活）


class ReorderBody(BaseModel):
    """排序接口请求体：稳定脚本 ID 数组（只传 WebUI 可见的激活脚本即可）。"""

    source_ids: list[str] = Field(default_factory=list)


@app.get("/api/v1/source")
async def source_info():
    try:
        sources = await _query_source_list()
        user_src = _describe_source_snapshot(sources)
        policy_payload = _policy_payload(sources, persist_cleanup=True)
    except Exception as exc:
        return _source_query_response(exc)
    return {
        "ok": True,
        "data": user_src,
        "circuit": chain_health_snapshot(),
        "policy": policy_payload,
    }


def _validate_reorder_ids(source_ids: list[str]) -> list[str]:
    ids: list[str] = []
    for raw in source_ids or []:
        sid = str(raw or "").strip()
        if not sid:
            raise ValueError("source_ids 不能包含空值")
        if len(sid) > 200 or any(ch in sid for ch in "\r\n\x00"):
            raise ValueError("source_ids 包含非法脚本 ID")
        if sid in ids:
            raise ValueError(f"重复的脚本 ID: {sid}")
        ids.append(sid)
    return ids


async def _rollback_source_order(previous: list[str]) -> None:
    """上游排序失败/未生效时尽力恢复旧顺序；恢复失败只记录，不覆盖原始错误。"""
    try:
        await LXSERVER.reorder_custom_sources(previous)
    except Exception as exc:
        logger.warning("恢复洛雪脚本原顺序失败: %s", exc)


@app.post("/api/v1/source/reorder")
@serialize_source_operation
async def source_reorder(body: ReorderBody):
    """调整脚本顺序：管理锁串行、上游 sourceIds 持久化、失败回滚。

    只改顺序，不改任何脚本的全局启用状态；成功后排空直链/候选缓存并推进
    配置代，旧在途解析的回写会被 `_URL_CACHE_GEN` 检查丢弃。
    """
    submitted = _validate_reorder_ids(body.source_ids)
    try:
        sources = await _query_source_list()
    except SourceStateQueryError as exc:
        return _source_query_response(exc)
    catalog = build_source_catalog(sources)
    catalog_ids = [script.id for script in catalog]
    if not catalog_ids:
        return _err("没有可排序的自定义源", 400)

    unknown = [sid for sid in submitted if sid not in set(catalog_ids)]
    if unknown:
        raise ValueError(f"未知脚本 ID: {', '.join(unknown)}")

    settings = _policy_settings()
    before = _effective_source_order(catalog_ids, settings.source_order)
    after = merge_reorder(before, submitted)

    try:
        await LXSERVER.reorder_custom_sources(after)
    except Exception as exc:
        if is_lxserver_unreachable(exc):
            return JSONResponse(status_code=503, content={
                "ok": False, "error": "洛雪脚本服务 lxserver 不可连接，请查看运行日志",
                "category": "service_unavailable", "stage": "lxmusic_to_lxserver",
            })
        return JSONResponse(status_code=502, content={
            "ok": False, "error": f"调整脚本顺序失败: {exc}",
            "category": "internal", "stage": "lxmusic_to_lxserver",
        })

    # 上游必须真的按新顺序返回；未生效不写入本地，避免 UI 显示成功但播放仍旧顺序。
    try:
        after_sources = await _query_source_list()
    except Exception as exc:
        await _rollback_source_order(before)
        return JSONResponse(status_code=502, content={
            "ok": False, "error": f"排序后源列表确认失败: {exc}",
            "category": "internal", "stage": "lxmusic_to_lxserver",
        })
    # 只校验当前就绪（激活且初始化成功）脚本的相对顺序：上游是否给停用脚本
    # 重排不影响播放（本地文件保留停用位置），也避免上游异步重载造成误回滚。
    ready_ids = {script.id for script in catalog if script.ready}
    actual_ids = [str(item.get("id") or "") for item in after_sources if isinstance(item, dict)]
    expected = [sid for sid in after if sid in ready_ids]
    actual = [sid for sid in actual_ids if sid in ready_ids]
    if actual != expected:
        logger.warning("上游排序未生效: expected=%s actual=%s", expected, actual)
        await _rollback_source_order(before)
        return JSONResponse(status_code=502, content={
            "ok": False, "error": "上游未按提交顺序生效，已尝试恢复原顺序",
            "category": "internal", "stage": "lxmusic_to_lxserver",
        })

    try:
        _write_source_order_file(after, list(settings.source_order))
    except OSError as exc:
        await _rollback_source_order(before)
        return JSONResponse(status_code=500, content={
            "ok": False, "error": f"保存脚本顺序失败: {exc}",
            "category": "internal", "stage": "lxmusic_to_lxserver",
        })

    _url_cache_reset()
    _CHAIN_HEALTH.clear()
    policy_payload = _policy_payload(after_sources)
    return {
        "ok": True,
        "data": {
            "source_ids": after,
            "active_source_order": policy_payload["active_source_order"],
            "generation": _URL_CACHE_GEN,
            "policy": policy_payload,
        },
    }


@app.post("/api/v1/source/verify")
async def source_verify(body: SourceBody):
    """端到端试运行自定义源（不改变当前激活源）。"""
    from verify_source import verify_url

    url = (body.url or "").strip()
    if not url.startswith(("http://", "https://", "file://")):
        return _err("url 必须以 http:// 、https:// 或 file:// 开头", 400)
    try:
        # asyncio.timeout 保持同一任务执行 verify_url，避免子任务身份破坏管理锁重入。
        async with asyncio.timeout(120.0):
            report = await verify_url(url)
    except SourceOperationBusy:
        raise
    except asyncio.TimeoutError:
        return _err("校验超时 (120s)", 504)
    except Exception as exc:
        if is_lxserver_unreachable(exc):
            logger.error("lxserver 不可连接，校验源失败: %s", exc)
            return JSONResponse(
                content={"ok": False, "data": {
                    "category": "service_unavailable", "stage": "lxmusic_to_lxserver",
                    "message": "洛雪脚本服务 lxserver 不可连接，请查看 lxserver 运行日志"}},
                status_code=503,
            )
        return JSONResponse(
            content={"ok": False, "data": {"category": "internal", "message": f"校验失败: {exc}"}}
        )
    # 服务故障返回 503，不按源失效处理。
    if str(report.get("category") or "") == "service_unavailable":
        return JSONResponse(content={"ok": False, "data": report}, status_code=503)
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
    sources = await _query_source_list()
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
@serialize_source_operation
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
            # 内部通信故障与脚本错误分别归类。
            if is_lxserver_unreachable(exc):
                logger.error("lxserver 不可连接，上传源失败: %s", exc)
                return JSONResponse(
                    content={"ok": False, "error": "洛雪脚本服务 lxserver 不可连接，请查看 lxserver 运行日志",
                             "category": "service_unavailable", "stage": "lxmusic_to_lxserver"},
                    status_code=503,
                )
            return JSONResponse(
                content={"ok": False, "error": f"上传到 lxserver 失败: {exc}", "category": "download"},
                status_code=500,
            )
        try:
            existing = await _find_lxserver_source(by_name=name)
        except SourceStateQueryError as lookup_exc:
            return _source_query_response(lookup_exc)
        except Exception as lookup_exc:
            if not is_lxserver_unreachable(lookup_exc):
                raise
            logger.error("lxserver 不可连接，查询重复上传源失败: %s", lookup_exc)
            return JSONResponse(
                content={"ok": False, "error": "洛雪脚本服务 lxserver 不可连接，请查看 lxserver 运行日志",
                         "category": "service_unavailable", "stage": "lxmusic_to_lxserver"},
                status_code=503,
            )
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
@serialize_source_operation
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
            if is_lxserver_unreachable(exc):
                return JSONResponse(
                    content={"ok": False, "error": "洛雪脚本服务 lxserver 不可连接，请查看 lxserver 运行日志",
                             "category": "service_unavailable", "stage": "lxmusic_to_lxserver"},
                    status_code=503,
                )
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
                # 外部下载由 lxserver 执行；此处 HTTP 异常仅来自内部通信。
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
            if url.startswith("file://"):
                raw = urllib.parse.unquote(url[len("file://"):])
                if os.name == "nt":
                    if raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
                        raw = raw[1:]
                    elif raw.startswith("///") and len(raw) > 4 and raw[4] == ":":
                        raw = raw[3:]
                path_part = raw
            else:
                parsed = urllib.parse.urlparse(url)
                path_part = urllib.parse.unquote(parsed.path) if parsed.scheme else url
            norm_path = path_part.replace("\\", "/")
            source_id = norm_path.rsplit("/", 1)[-1]
            if not await LXSERVER.set_source_enabled(source_id, body.enabled):
                # 列表中无此 id：激活语义下，若本地确有脚本文件（如旧数据卷 /data/lxmusic/uploads），
                # 自动补导入 lxserver 后激活，保证历史配置可继续使用；停用语义不做补导入，
                # 但 file:// 的 basename（用户自己的文件名）不是 lxserver 按 @name 派生的
                # id——先按名称反查，再读本地脚本按 @name 反查真实 id 后停用
                resolved = False
                has_file = os.path.isfile(path_part)
                if not body.enabled:
                    existing = await _find_lxserver_source(by_name=source_id.removesuffix(".js"))
                    if not existing and has_file:
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
                if body.enabled and has_file:
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
    except SourceStateQueryError as exc:
        return _source_query_response(exc)
    except Exception as exc:
        if is_lxserver_unreachable(exc):
            logger.error("lxserver 不可连接，切换源失败: %s", exc)
            return JSONResponse(
                content={"ok": False, "error": "洛雪脚本服务 lxserver 不可连接，请查看 lxserver 运行日志",
                         "category": "service_unavailable", "stage": "lxmusic_to_lxserver"},
                status_code=503,
            )
        return JSONResponse(
            content={"ok": False, "error": f"切换源失败: {exc}", "category": "runtime"},
            status_code=500,
        )

    # 激活必须以目标源的明确初始化结果为准；停用无需等待初始化。
    confirmed_id, user_src, target = source_id, None, None
    if body.enabled:
        confirmed_id, user_src, target = await _confirm_source_initialization(source_id)
        if target is None:
            return JSONResponse(status_code=504, content={
                "ok": False,
                "error": f"源 {source_id} 初始化结果待确认，可稍后重试或查看运行日志",
                "category": "initialization_pending", "stage": "lxserver_init",
                "source_id": confirmed_id,
            })
        if target.get("status") == "failed":
            return JSONResponse(status_code=500, content={
                "ok": False,
                "error": f"源初始化失败: {redact(str(target.get('error') or '')).strip() or 'init error'}",
                "category": "runtime", "stage": "lxserver_init",
                "source_id": confirmed_id,
            })
        source_id = confirmed_id

    _CHAIN_HEALTH.clear()
    _url_cache_reset()
    if user_src is None:
        user_src = await describe_user_source()
    return {"ok": True, "data": user_src, "source_id": source_id}


@app.delete("/api/v1/source")
@serialize_source_operation
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
