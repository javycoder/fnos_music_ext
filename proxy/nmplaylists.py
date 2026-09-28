"""网易账号歌单：音乐页注入网易自建歌单（只读虚拟歌单，FNMUSIC_NETEASE_MY_PLAYLISTS）。

链路：playlist/list 注入摘要卡片（内存 5 分钟 / 失败冷却 2 分钟，musicbox 不可用
回落磁盘缓存）→ detail / track 端点按 nm guid 拉可播曲目（每歌单内存 10 分钟 +
磁盘 24 小时，列表注入后串行后台预取）→ 播放 / 取链 / 歌词 / 曲目封面全部走
既有 online:netease 链路，本模块不碰取流。

歌单 guid 自包含网易歌单 id（online:playlist:nm:<id>），无需反解表即可定位；
卡片封面 coverId 与曲目 guid 的伪装沿用确定性 md5（与 app.fake_official_guid
同公式，盐前缀改动必须两处同步）。磁盘缓存形状对齐 recommend bundle
（{"playlist": ..., "tracks": [...]} / {"items": [...]}），重启后由
ensure_registry_warm 扫描 nm_playlists_cache 顺带重建 fake→real 反查映射。

网易登录是实例级账号：所有 fnOS 用户看到同一批歌单（同热门推荐，无按用户
隔离）；在音乐页对这类歌单加歌 / 移歌 / 删除均被 proxy 只读吸收，不回写网易。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from uuid import uuid4

import httpx

try:
    from . import recommend as dailyrec
except ImportError:  # uvicorn --app-dir proxy
    import recommend as dailyrec  # type: ignore

logger = logging.getLogger("proxy.nmplaylists")

NM_GUID_PREFIX = "online:playlist:nm:"

_SUMMARY_TTL_S = 300.0        # 摘要内存缓存
_FAIL_COOLDOWN_S = 120.0      # 拉取失败（含未登录）冷却：期内不再请求 musicbox
_TRACKS_TTL_S = 600.0         # 曲目内存缓存
_TRACKS_LRU_MAX = 8           # 最多驻留 N 个歌单的曲目
_DISK_TTL_S = 24 * 3600.0     # 磁盘缓存有效期（回落与重启恢复窗口）
_FETCH_TIMEOUT_S = 10.0
_TRACKS_FETCH_TIMEOUT_S = 60.0
_PERSIST_MIN_INTERVAL_S = 5.0
_MAX_PLAYLISTS = 100          # 注入歌单数上限（与上游 limit 一致）
_MAX_TRACKS = 1000            # 每歌单曲目上限（批量详情单次调用上限）

_HEX32_RE = re.compile(r"[0-9a-f]{32}")

# 进程内状态（测试经 reset_for_test 清理）
_summaries: "dict | None" = None      # {"cards": [...], "saved_at": 单调钟}
_fail_until: float = 0.0
_tracks_cache: "dict[str, tuple[list, float]]" = {}
_tracks_order: list[str] = []         # LRU 顺序（旧 → 新）
_load_tasks: "dict[str, asyncio.Task]" = {}
_prefetch_tasks: "set[asyncio.Task]" = set()
_cover_urls: "dict[int, str]" = {}
_disk_cards: "list | None" = None     # 磁盘摘要（懒加载，进程内缓存）
_persist_last: "dict[str, float]" = {}


def home_dir() -> str:
    env = (os.environ.get("FNMUSIC_HOME") or "").strip()
    if env:
        return env
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def cache_dir() -> str:
    return os.environ.get("FNMUSIC_NM_PLAYLISTS_DIR") or os.path.join(home_dir(), "nm_playlists_cache")


# --------------------------------------------------------------- guid 与伪装 --

def nm_fake_guid(real_guid: str) -> str:
    """与 app.fake_official_guid 同公式（含 "fnmusic-ext::" 盐前缀），改动须两处同步。

    仅用于在 app 上下文之外构造封面假 id（app 侧注入时仍会用自身的
    fake_official_guid 注册反查映射，结果一致）。
    """
    return hashlib.md5(f"fnmusic-ext::{real_guid}".encode()).hexdigest()


def nm_playlist_guid(playlist_id: int) -> str:
    return f"{NM_GUID_PREFIX}{int(playlist_id)}"


def is_nm_playlist_guid(guid: str | None) -> bool:
    return str(guid or "").startswith(NM_GUID_PREFIX)


def nm_playlist_id_from_guid(guid: str | None) -> int:
    s = str(guid or "")
    if not s.startswith(NM_GUID_PREFIX):
        return 0
    tail = s[len(NM_GUID_PREFIX):]
    if not tail.isdigit():
        return 0
    return int(tail)


def playlist_id_from_cover_request(resolved_guid: str | None, raw_param: str | None) -> int:
    """封面请求反解歌单 id：优先已反解的 nm guid；否则按裸 32-hex 假 id 查登记。

    覆盖重启后 _FAKE_GUID_REVERSE 尚未重建、客户端持旧假 coverId 直取的窗口。
    """
    pid = nm_playlist_id_from_guid(resolved_guid)
    if pid:
        return pid
    raw = str(raw_param or "")
    fake = raw[6:] if raw.startswith("track_") else raw
    if not _HEX32_RE.fullmatch(fake or ""):
        return 0
    for card in _iter_known_cards():
        if str(card.get("coverId") or "") == f"track_{fake}":
            return nm_playlist_id_from_guid(card.get("guid"))
        if nm_fake_guid(str(card.get("guid") or "")) == fake:
            return nm_playlist_id_from_guid(card.get("guid"))
    return 0


# --------------------------------------------------------------- 磁盘缓存 --

def _summaries_disk_path() -> str:
    return os.path.join(cache_dir(), "summaries.json")


def _tracks_disk_path(playlist_id: int) -> str:
    return os.path.join(cache_dir(), f"pl-{int(playlist_id)}.json")


def _atomic_write_json(path: str, payload) -> bool:
    parent = os.path.dirname(path) or "."
    part = f"{path}.{uuid4().hex[:8]}.part"
    try:
        os.makedirs(parent, exist_ok=True)
        with open(part, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(part, path)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return True
    except Exception as e:
        logger.warning("failed to write %s: %s", path, e)
        if os.path.exists(part):
            try:
                os.remove(part)
            except Exception:
                pass
        return False


def _throttled(key: str) -> bool:
    now = time.monotonic()
    if now - _persist_last.get(key, 0.0) < _PERSIST_MIN_INTERVAL_S:
        return False
    _persist_last[key] = now
    return True


def _persist_summaries(cards: list, covers: dict) -> None:
    if not _throttled("summaries"):
        return
    payload = {
        "version": 1,
        "savedAt": int(time.time()),
        # "items" 形状让 ensure_registry_warm 的既有扫描分支顺带登记歌单假 id
        "items": cards,
        "covers": {str(k): v for k, v in covers.items()},
    }
    _atomic_write_json(_summaries_disk_path(), payload)


def _persist_tracks(playlist_id: int, tracks: list, card: dict | None) -> None:
    if not _throttled(f"pl-{int(playlist_id)}"):
        return
    payload = {
        "version": 1,
        "savedAt": int(time.time()),
        # 形状对齐 recommend bundle：{"playlist": {...}, "tracks": [...]}
        "playlist": card or {"guid": nm_playlist_guid(playlist_id)},
        "tracks": tracks,
    }
    _atomic_write_json(_tracks_disk_path(playlist_id), payload)


def _load_disk_summaries() -> list:
    """磁盘摘要（24h 内有效）→ 卡片列表；同时补全 _cover_urls。无效返回 []。"""
    global _disk_cards
    if _disk_cards is not None:
        return _disk_cards
    cards: list = []
    try:
        with open(_summaries_disk_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        saved = data.get("savedAt") if isinstance(data, dict) else 0
        fresh = isinstance(saved, (int, float)) and time.time() - saved < _DISK_TTL_S
        if fresh and isinstance(data.get("items"), list):
            for c in data["items"]:
                if isinstance(c, dict) and nm_playlist_id_from_guid(c.get("guid")):
                    cards.append(dict(c))
            for k, v in (data.get("covers") or {}).items():
                try:
                    _cover_urls.setdefault(int(k), str(v))
                except (TypeError, ValueError):
                    continue
    except Exception:
        pass
    _disk_cards = cards
    return cards


def _load_disk_tracks(playlist_id: int) -> "list | None":
    """磁盘曲目（24h 内有效）；无文件 / 过期 / 损坏返回 None。"""
    try:
        with open(_tracks_disk_path(playlist_id), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    saved = data.get("savedAt")
    if not (isinstance(saved, (int, float)) and time.time() - saved < _DISK_TTL_S):
        return None
    tracks = data.get("tracks")
    if isinstance(tracks, list):
        return tracks
    return None


# --------------------------------------------------------------- 摘要与卡片 --

def _card_from_row(row: dict) -> "dict | None":
    try:
        pid = int(row.get("playlist_id") or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    guid = nm_playlist_guid(pid)
    try:
        created = int(row.get("created_at") or 0)
        updated = int(row.get("updated_at") or 0)
        count = int(row.get("track_count") or 0)
    except (TypeError, ValueError):
        created, updated, count = 0, 0, 0
    card = {
        "guid": guid,
        "name": str(row.get("name") or ""),
        "coverId": "track_" + nm_fake_guid(guid),
        "createdAt": created or int(time.time()),
        "updatedAt": updated or int(time.time()),
        "trackCount": count,
        "isDaily": False,
    }
    cover_url = str(row.get("cover_url") or "")
    if cover_url:
        _cover_urls[pid] = cover_url
    return card


def _iter_known_cards() -> list:
    """内存摘要 + 磁盘摘要（懒加载）合并视图，供反查。"""
    cards = list((_summaries or {}).get("cards") or [])
    seen = {str(c.get("guid") or "") for c in cards}
    for c in _load_disk_summaries():
        if str(c.get("guid") or "") not in seen:
            cards.append(c)
    return cards


async def peek_summaries(client: httpx.AsyncClient) -> list:
    """网易账号自建歌单卡片（热门推荐与官方歌单之间注入用）。

    成功 → 内存 TTL 5 分钟 + 落盘；失败 / 未登录 → 冷却 2 分钟并回落磁盘
    （24h 内）；任何异常都只返回 []，绝不影响官方列表响应。
    """
    global _summaries, _fail_until, _disk_cards
    now = time.monotonic()
    cached = _summaries
    if cached and now - cached["saved_at"] < _SUMMARY_TTL_S:
        return list(cached["cards"])
    if now < _fail_until:
        return list((cached or {}).get("cards") or [])
    try:
        r = await client.get(
            "/api/v1/user/playlists", params={"limit": _MAX_PLAYLISTS}, timeout=_FETCH_TIMEOUT_S
        )
        if r.status_code != 200:
            raise RuntimeError(f"http {r.status_code}")
        data = r.json()
    except Exception as e:
        logger.debug("nm summaries fetch failed: %s", e)
        _fail_until = now + _FAIL_COOLDOWN_S
        return list(_load_disk_summaries())
    if not (isinstance(data, dict) and data.get("ok") is True):
        # 未登录等结构化错误：静默跳过 + 冷却
        logger.debug("nm summaries not ok: %.120s", data)
        _fail_until = now + _FAIL_COOLDOWN_S
        return list((cached or {}).get("cards") or [])
    try:
        uid = int(data.get("account_uid") or 0)
    except (TypeError, ValueError):
        uid = 0
    rows = [r for r in (data.get("data") or []) if isinstance(r, dict)]
    if uid <= 0:
        # 拿不到账号 id 无法判定自建归属：宁可不注入，不把收藏的他人歌单当自建泄露
        logger.debug("nm summaries missing account_uid, skip")
        return list((cached or {}).get("cards") or [])
    cards: list = []
    new_covers: dict = {}
    for row in rows:
        try:
            owner = int(row.get("user_id") or 0)
        except (TypeError, ValueError):
            owner = 0
        if owner != uid:
            continue  # 收藏的他人歌单不注入
        card = _card_from_row(row)
        if card:
            cards.append(card)
            pid = nm_playlist_id_from_guid(card["guid"])
            if pid and _cover_urls.get(pid):
                new_covers[pid] = _cover_urls[pid]
    _fail_until = 0.0
    _summaries = {"cards": cards, "saved_at": now}
    _disk_cards = None  # 内存新数据已覆盖，磁盘视图按需重读
    if cards:
        _persist_summaries(cards, new_covers)
    return list(cards)


def card_for(playlist_id: int) -> "dict | None":
    """详情端点用：按 id 取卡片（内存 → 磁盘）。未知歌单返回 None。"""
    pid = int(playlist_id)
    want = nm_playlist_guid(pid)
    for card in (_summaries or {}).get("cards") or []:
        if str(card.get("guid") or "") == want:
            return dict(card)
    for card in _load_disk_summaries():
        if str(card.get("guid") or "") == want:
            return dict(card)
    return None


def cover_url_for(playlist_id: int) -> str:
    """歌单封面直链（内存 → 磁盘摘要）；未知返回空串（封面端点据此 404）。"""
    pid = int(playlist_id)
    url = _cover_urls.get(pid)
    if url is not None:
        return url
    _load_disk_summaries()
    return _cover_urls.get(pid, "")


# --------------------------------------------------------------- 曲目 --

async def _fetch_tracks(client: httpx.AsyncClient, playlist_id: int, build_track) -> list:
    pid = int(playlist_id)
    key = str(pid)
    try:
        r = await client.get(
            f"/api/v1/user/playlists/{pid}/tracks", timeout=_TRACKS_FETCH_TIMEOUT_S
        )
        if r.status_code != 200:
            raise RuntimeError(f"http {r.status_code}")
        data = r.json()
    except Exception as e:
        logger.debug("nm tracks fetch failed (%s): %s", pid, e)
        disk = _load_disk_tracks(pid)
        return [dict(t) for t in disk] if disk is not None else []
    if not (isinstance(data, dict) and data.get("ok") is True):
        logger.debug("nm tracks not ok (%s)", pid)
        disk = _load_disk_tracks(pid)
        return [dict(t) for t in disk] if disk is not None else []
    tracks: list = []
    for row in data.get("data") or []:
        if not isinstance(row, dict):
            continue
        candidate = dailyrec._musicbox_recommend_item(row)
        if not candidate.get("id"):
            continue
        try:
            track = build_track(candidate)
        except Exception as e:
            logger.debug("nm track build failed (%s): %s", pid, e)
            continue
        if isinstance(track, dict) and track.get("guid"):
            tracks.append(track)
    _tracks_cache[key] = (tracks, time.monotonic() + _TRACKS_TTL_S)
    if key in _tracks_order:
        _tracks_order.remove(key)
    _tracks_order.append(key)
    while len(_tracks_order) > _TRACKS_LRU_MAX:
        oldest = _tracks_order.pop(0)
        _tracks_cache.pop(oldest, None)
    _persist_tracks(pid, tracks, card_for(pid))
    return [dict(t) for t in tracks]


async def load_tracks(client: httpx.AsyncClient, playlist_id: int, build_track) -> list:
    """歌单可播曲目（VO 形态，real guid）；同歌单并发请求合并为一次抓取。"""
    pid = int(playlist_id)
    if pid <= 0:
        return []
    key = str(pid)
    hit = _tracks_cache.get(key)
    if hit and time.monotonic() < hit[1]:
        return [dict(t) for t in hit[0]]
    existing = _load_tasks.get(key)
    if existing is not None and not existing.done():
        return await asyncio.shield(existing)
    task = asyncio.get_running_loop().create_task(_fetch_tracks(client, pid, build_track))
    _load_tasks[key] = task
    try:
        return await asyncio.shield(task)
    finally:
        _load_tasks.pop(key, None)


def cached_tracks(playlist_id: int) -> "list | None":
    """同步取已缓存曲目（不触发抓取）；未缓存返回 None。"""
    hit = _tracks_cache.get(str(int(playlist_id)))
    if hit and time.monotonic() < hit[1]:
        return list(hit[0])
    disk = _load_disk_tracks(int(playlist_id))
    return [dict(t) for t in disk] if disk is not None else None


def schedule_prefetch(client: httpx.AsyncClient, build_track, cards: list) -> None:
    """列表注入后串行后台预取各歌单曲目（防大歌单首开等批量详情 + 可播过滤）。

    串行而非并发：对网易接口温和（风控意识与推荐链一致），预取本就不赶时间。
    """
    pending: list[int] = []
    now = time.monotonic()
    for card in cards or []:
        pid = nm_playlist_id_from_guid(card.get("guid") if isinstance(card, dict) else "")
        if pid <= 0:
            continue
        hit = _tracks_cache.get(str(pid))
        if hit and now < hit[1]:
            continue
        if pid not in pending:
            pending.append(pid)
    if not pending:
        return

    async def _runner() -> None:
        for pid in pending:
            try:
                await load_tracks(client, pid, build_track)
            except Exception as e:
                logger.debug("nm prefetch failed (%s): %s", pid, e)

    try:
        task = asyncio.get_running_loop().create_task(_runner())
    except RuntimeError:
        return
    _prefetch_tasks.add(task)
    task.add_done_callback(_prefetch_tasks.discard)


def reset_for_test() -> None:
    """清空全部进程内状态（测试隔离用）。"""
    global _summaries, _fail_until, _disk_cards
    _summaries = None
    _fail_until = 0.0
    _disk_cards = None
    _tracks_cache.clear()
    _tracks_order.clear()
    _cover_urls.clear()
    _persist_last.clear()
    for task in list(_load_tasks.values()) + list(_prefetch_tasks):
        if not task.done():
            task.cancel()
    _load_tasks.clear()
