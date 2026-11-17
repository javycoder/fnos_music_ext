"""在线歌单搜索编排。

只查询当前启用音源里的公开歌单：每个已初始化洛雪脚本声明的平台查一次，
网易音源或网易歌单常驻开启时再查网易。结果按用户登记发现授权，不自动订阅。

分页与发现方式参考 Zhuoershang 的提交 7b19f067、7ed971db、f6e13102、931cfc66，
但授权不跨用户，后续页使用独立游标，超时的源不会挡住其他源。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from uuid import uuid4

try:
    from . import publicplaylists as pubpl
except ImportError:  # uvicorn --app-dir proxy
    import publicplaylists as pubpl  # type: ignore

logger = logging.getLogger("proxy.playlistsearch")

TOTAL_BUDGET_S = 5.0
GRACE_AFTER_FIRST_S = 2.0
GRANT_TTL_S = 2 * 3600.0
GRANT_GLOBAL_MAX = 512
GRANT_USER_MAX = 128
SESSION_TTL_S = 120.0
SESSION_MAX = 64
POOL_MAX = 500
MAX_PAGES_PER_ROUND = 4

FetchPage = Callable[..., Awaitable[dict]]
ResolvePlatforms = Callable[[float], Awaitable[tuple[list[str], bool]]]


@dataclass(frozen=True)
class Target:
    provider: str
    source: str


@dataclass
class Found:
    entry: dict
    card: dict
    identity: tuple
    summary: dict


@dataclass
class PageFetch:
    page: int
    items: list[dict]
    raw_count: int
    has_more: bool
    exhaust: bool = False
    repeated: bool = False


@dataclass
class Cursor:
    target: Target
    index: int
    next_page: int = 1
    exhausted: bool = False
    seen: set[str] = field(default_factory=set)
    empty_streak: int = 0
    completed: list[PageFetch] = field(default_factory=list)


@dataclass
class Session:
    key: str
    items: list[Found] = field(default_factory=list)
    cursors: list[Cursor] = field(default_factory=list)
    interrupted: bool = False
    lx_confirmed: bool = False
    created: float = field(default_factory=time.monotonic)
    generation: int = 1
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class PageResult:
    cards: list[dict] = field(default_factory=list)
    founds: list[Found] = field(default_factory=list)
    pool_length: int = 0
    open_more: bool = False
    interrupted: bool = False


@dataclass
class _Grant:
    user_digest: str
    identity: tuple
    entry: dict
    summary: dict
    expires: float


_sessions: OrderedDict[str, Session] = OrderedDict()
_grants: OrderedDict[tuple, _Grant] = OrderedDict()
_grant_lock = asyncio.Lock()
_io_tasks: set[asyncio.Task] = set()


def sources_configured(conf: dict) -> bool:
    return bool(
        conf.get("netease_enabled")
        or conf.get("netease_playlist_resident")
        or conf.get("lx_enabled")
    )


def empty_result() -> PageResult:
    return PageResult()


def parse_positive_int(raw: object, default: int, upper: int) -> int:
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if value < 1:
        return default
    return min(value, upper)


def canonical_identity(entry: dict) -> tuple:
    provider = str(entry.get("provider") or "")
    source = str(entry.get("source") or "")
    identifier = str(entry.get("id") or "").strip()
    if provider in ("netease", "musicbox") or (provider == "lx" and source == "wy"):
        return ("netease", identifier)
    return (provider, source, identifier)


def source_allowed(conf: dict, entry: dict) -> bool:
    provider = str(entry.get("provider") or "")
    source = str(entry.get("source") or "")
    if provider in ("netease", "musicbox"):
        return bool(conf.get("netease_enabled") or conf.get("netease_playlist_resident"))
    if provider != "lx" or not conf.get("lx_enabled"):
        return False
    selected = [str(item) for item in (conf.get("lx_sources") or [])]
    return not selected or source in selected


def lx_platforms_from_capabilities(payload: dict, whitelist: list[str]) -> list[str]:
    rows = payload.get("sources") if isinstance(payload, dict) else None
    available: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("playback_available"):
            continue
        identifier = str(row.get("id") or "").strip()
        if identifier and identifier not in available:
            available.append(identifier)
    if whitelist:
        allowed = set(available)
        return [item for item in whitelist if item in allowed]
    return available


def configured_targets(conf: dict, platforms: list[str]) -> list[Target]:
    targets: list[Target] = []
    if conf.get("netease_enabled") or conf.get("netease_playlist_resident"):
        targets.append(Target("netease", "netease"))
    if conf.get("lx_enabled"):
        selected = [str(item) for item in (conf.get("lx_sources") or [])]
        for platform in platforms:
            if platform and (not selected or platform in selected):
                targets.append(Target("lx", platform))
    return targets


def _user_digest(user_guid: str) -> str:
    return hashlib.sha256(str(user_guid).encode()).hexdigest()


def _user_ok(user_guid: str) -> bool:
    guid = str(user_guid or "").strip()
    return bool(guid) and guid != "shared"


def _session_key(user_guid: str, keyword: str, size: int, conf: dict) -> str:
    payload = {
        "user": _user_digest(user_guid),
        "keyword": keyword.casefold(),
        "size": size,
        "netease": bool(conf.get("netease_enabled")),
        "resident": bool(conf.get("netease_playlist_resident")),
        "lx": bool(conf.get("lx_enabled")),
        "whitelist": [str(item) for item in (conf.get("lx_sources") or [])],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _safe_int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def online_bounds(page: int, size: int, official_total: int) -> tuple[int, int]:
    start = max(0, (page - 1) * size - max(0, official_total))
    end = max(0, page * size - max(0, official_total))
    return start, end


def _entry_from_item(target: Target, item: dict) -> dict | None:
    identifier = str(item.get("id") or "").strip()
    name = str(item.get("name") or "").strip()
    if not identifier or not name:
        return None
    kind = str(item.get("kind") or "playlist")
    if kind not in ("playlist", "chart"):
        kind = "playlist"
    provider = "netease" if target.provider == "netease" else "lx"
    source = "netease" if provider == "netease" else target.source
    return {
        "provider": provider,
        "source": source,
        "kind": kind,
        "id": identifier,
        "name": name,
        "cover_url": str(item.get("cover_url") or ""),
        "track_count": _safe_int(item.get("track_count")),
        "updated_at": _safe_int(item.get("updated_at")),
    }


def _found_from_item(target: Target, item: dict) -> Found | None:
    entry = _entry_from_item(target, item)
    if entry is None:
        return None
    summary = {
        "name": entry.pop("name"),
        "cover_url": entry.pop("cover_url"),
        "track_count": entry.pop("track_count"),
        "updated_at": entry.pop("updated_at"),
    }
    card = pubpl.remember_discovery_summary(entry, summary)
    if card is None:
        return None
    return Found(entry=entry, card=card, identity=canonical_identity(entry), summary=summary)


def _seen_ids(cursor: Cursor) -> set[str]:
    seen = set(cursor.seen)
    for fetch in cursor.completed:
        for item in fetch.items:
            identifier = str(item.get("id") or "").strip()
            if identifier:
                seen.add(identifier)
    return seen


def _interpret(cursor: Cursor, payload: dict, page: int) -> PageFetch:
    raw_items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        raise RuntimeError("invalid playlist page")
    raw_count = payload.get("raw_count")
    if not isinstance(raw_count, int):
        raw_count = len(raw_items)
    has_more = bool(payload.get("has_more")) if isinstance(payload, dict) and "has_more" in payload else raw_count > 0
    items = []
    ids: list[str] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        identifier = str(raw.get("id") or "").strip()
        if identifier:
            ids.append(identifier)
        items.append(raw)
    seen = _seen_ids(cursor)
    repeated = bool(ids) and all(identifier in seen for identifier in ids)
    fresh = [item for item in items if str(item.get("id") or "").strip() not in seen]
    exhaust = raw_count <= 0 or not ids or repeated or not has_more
    if not fresh and not exhaust:
        cursor.empty_streak += 1
        exhaust = cursor.empty_streak >= 2
    elif fresh:
        cursor.empty_streak = 0
    return PageFetch(
        page=page, items=items, raw_count=raw_count, has_more=has_more,
        exhaust=exhaust, repeated=repeated,
    )


def _staged_count(session: Session) -> int:
    seen = {item.identity for item in session.items}
    count = len(session.items)
    for cursor in session.cursors:
        for fetch in cursor.completed:
            for item in fetch.items:
                entry = _entry_from_item(cursor.target, item)
                if entry is None:
                    continue
                identity = canonical_identity(entry)
                if identity in seen:
                    continue
                seen.add(identity)
                count += 1
    return count


def _commit(session: Session, generation: int) -> None:
    if session.generation != generation:
        return
    seen = {item.identity for item in session.items}
    staged: list[tuple[int, int, int, Found]] = []
    for cursor in session.cursors:
        for fetch in cursor.completed:
            for rank, item in enumerate(fetch.items):
                identifier = str(item.get("id") or "").strip()
                if identifier:
                    cursor.seen.add(identifier)
                found = _found_from_item(cursor.target, item)
                if found is not None:
                    staged.append((cursor.index, fetch.page, rank, found))
            if fetch.exhaust or fetch.repeated:
                cursor.exhausted = True
            cursor.next_page = fetch.page + 1
        cursor.completed = []
    for _index, _page, _rank, found in sorted(staged, key=lambda row: row[:3]):
        if found.identity in seen or len(session.items) >= POOL_MAX:
            continue
        seen.add(found.identity)
        session.items.append(found)
    if len(session.items) >= POOL_MAX:
        for cursor in session.cursors:
            cursor.exhausted = True


def _touch_session(key: str, session: Session) -> None:
    _sessions.pop(key, None)
    _sessions[key] = session
    while len(_sessions) > SESSION_MAX:
        _sessions.popitem(last=False)


def _get_session(key: str, page: int) -> Session:
    now = time.monotonic()
    current = _sessions.get(key)
    if current is not None and now - current.created > SESSION_TTL_S:
        current.generation += 1
        current = None
    if current is not None and page == 1 and current.interrupted:
        current.generation += 1
        current = None
    if current is None:
        current = Session(key=key)
    _touch_session(key, current)
    return current


def _align_cursors(session: Session, targets: list[Target]) -> None:
    wanted = {(target.provider, target.source): index for index, target in enumerate(targets)}
    for cursor in session.cursors:
        key = (cursor.target.provider, cursor.target.source)
        if key in wanted:
            cursor.index = wanted[key]
        else:
            cursor.exhausted = True
    existing = {(cursor.target.provider, cursor.target.source) for cursor in session.cursors}
    for target in targets:
        if (target.provider, target.source) not in existing:
            session.cursors.append(Cursor(
                target=target,
                index=wanted[(target.provider, target.source)],
            ))


async def _worker(
    cursor: Cursor,
    session: Session,
    generation: int,
    keyword: str,
    needed_end: int,
    deadline: float,
    grace: dict,
    fetch_page: FetchPage,
) -> None:
    while session.generation == generation and not cursor.exhausted:
        now = time.monotonic()
        if now >= deadline:
            return
        grace_deadline = grace.get("deadline")
        if isinstance(grace_deadline, float) and now >= grace_deadline:
            return
        if len(cursor.completed) >= MAX_PAGES_PER_ROUND:
            return
        if _staged_count(session) >= needed_end and cursor.completed:
            return
        remaining = deadline - now
        if isinstance(grace_deadline, float):
            remaining = min(remaining, grace_deadline - now)
        if remaining <= 0.05:
            return
        page = cursor.next_page + len(cursor.completed)
        try:
            payload = await fetch_page(cursor.target, keyword, page, remaining)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info(
                "playlist source page failed: provider=%s source=%s page=%s error=%s",
                cursor.target.provider, cursor.target.source, page, type(exc).__name__,
            )
            return
        if session.generation != generation:
            return
        try:
            fetch = _interpret(cursor, payload, page)
        except Exception:
            return
        cursor.completed.append(fetch)
        if any(_entry_from_item(cursor.target, item) for item in fetch.items):
            if grace.get("deadline") is None:
                grace["deadline"] = min(deadline, time.monotonic() + GRACE_AFTER_FIRST_S)
        if fetch.exhaust or fetch.repeated:
            return


async def load_page(
    *,
    conf: dict,
    user_guid: str,
    keyword: str,
    page: int,
    size: int,
    official_total: int,
    deadline: float,
    resolve_platforms: ResolvePlatforms,
    fetch_page: FetchPage,
) -> PageResult:
    if not _user_ok(user_guid) or not str(keyword or "").strip() or not sources_configured(conf):
        return empty_result()
    keyword = str(keyword).strip()[:120]
    start, end = online_bounds(page, size, official_total)
    if end <= start:
        return PageResult(open_more=sources_configured(conf))
    key = _session_key(user_guid, keyword, size, conf)
    session = _get_session(key, page)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return PageResult(interrupted=True)
    try:
        await asyncio.wait_for(session.lock.acquire(), timeout=remaining)
    except TimeoutError:
        return PageResult(open_more=True, interrupted=True)
    try:
        if session.key != key or session.generation == 0:
            return empty_result()
        platforms: list[str] = []
        if conf.get("lx_enabled") and not session.lx_confirmed:
            budget = deadline - time.monotonic()
            if budget > 0.05:
                platforms, confirmed = await resolve_platforms(budget)
                session.lx_confirmed = confirmed
            else:
                confirmed = False
        elif conf.get("lx_enabled"):
            confirmed = True
            platforms = [cursor.target.source for cursor in session.cursors if cursor.target.provider == "lx"]
        else:
            confirmed = True
            session.lx_confirmed = True
        targets = configured_targets(conf, platforms if conf.get("lx_enabled") else [])
        if conf.get("lx_enabled") and session.lx_confirmed:
            targets = configured_targets(conf, platforms)
        _align_cursors(session, targets)
        generation = session.generation
        if len(session.items) < end:
            grace: dict = {"deadline": None}
            workers = [
                asyncio.create_task(_worker(
                    cursor, session, generation, keyword, end, deadline, grace, fetch_page,
                ))
                for cursor in session.cursors
                if not cursor.exhausted
            ]
            try:
                while workers:
                    now = time.monotonic()
                    if now >= deadline:
                        break
                    grace_deadline = grace.get("deadline")
                    if isinstance(grace_deadline, float) and now >= grace_deadline:
                        break
                    timeout = min(deadline - now, 0.1)
                    if isinstance(grace_deadline, float):
                        timeout = min(timeout, max(0.0, grace_deadline - now))
                    _done, pending = await asyncio.wait(
                        workers, timeout=max(0.0, timeout), return_when=asyncio.FIRST_COMPLETED,
                    )
                    workers = list(pending)
                    if not workers:
                        break
            finally:
                for task in workers:
                    if not task.done():
                        task.cancel()
                if workers:
                    await asyncio.gather(*workers, return_exceptions=True)
            _commit(session, generation)
        short = len(session.items) < end
        unfinished = any(not cursor.exhausted for cursor in session.cursors)
        lx_pending = bool(conf.get("lx_enabled") and not session.lx_confirmed)
        interrupted = short and (unfinished or lx_pending or time.monotonic() >= deadline)
        session.interrupted = interrupted
        window = session.items[start:end]
        open_more = (unfinished or lx_pending) and not interrupted
        return PageResult(
            cards=[item.card for item in window],
            founds=window,
            pool_length=len(session.items),
            open_more=open_more,
            interrupted=interrupted,
        )
    finally:
        session.lock.release()


async def fetch_lx_platforms(client, whitelist: list[str], timeout: float) -> tuple[list[str], bool]:
    try:
        response = await client.get("/api/v1/public-playlists/capabilities", timeout=max(0.05, timeout))
    except Exception as exc:
        logger.info("playlist search capabilities failed: %s", type(exc).__name__)
        return [], False
    if response.status_code != 200:
        return [], False
    try:
        payload = response.json()
    except Exception:
        return [], False
    if not isinstance(payload, dict) or payload.get("ok") is False:
        return [], False
    return lx_platforms_from_capabilities(payload, [str(item) for item in whitelist]), True


async def fetch_http(client, target: Target, keyword: str, page: int, timeout: float) -> dict:
    params = {
        "source": target.source,
        "channel": "search",
        "keyword": keyword,
        "page": page,
        "limit": 20,
    }
    budget_ms = max(1, int(timeout * 1000))
    if target.provider == "netease":
        params["timeout_ms"] = budget_ms
    else:
        params["timeout_s"] = round(max(0.05, timeout), 3)
    response = await client.get(
        "/api/v1/public-playlists", params=params, timeout=max(0.05, timeout),
    )
    if response.status_code != 200:
        raise RuntimeError(f"playlist search http {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise RuntimeError("invalid playlist search payload")
    return payload


def _grant_path() -> str:
    root = os.environ.get("FNMUSIC_PLAYLIST_SEARCH_DIR") or os.path.join(pubpl.home_dir(), "playlist_search")
    return os.path.join(root, "discovery-grants.json")


def _summary_for_disk(summary: dict) -> dict:
    return {
        "name": str(summary.get("name") or ""),
        "track_count": _safe_int(summary.get("track_count")),
        "updated_at": _safe_int(summary.get("updated_at")),
    }


def _write_grants(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    part = f"{path}.{uuid4().hex[:8]}.part"
    with open(part, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(part, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _export_grants() -> dict:
    now = time.time()
    rows = []
    for grant in _grants.values():
        if grant.expires <= now:
            continue
        rows.append({
            "user": grant.user_digest,
            "identity": list(grant.identity),
            "entry": grant.entry,
            "summary": _summary_for_disk(grant.summary),
            "expires": grant.expires,
        })
    return {"version": 1, "grants": rows}


async def _persist_grants() -> None:
    path = _grant_path()
    payload = _export_grants()
    await asyncio.to_thread(_write_grants, path, payload)


def _evict_grants(now: float) -> None:
    expired = [key for key, grant in _grants.items() if grant.expires <= now]
    for key in expired:
        _grants.pop(key, None)
    counts: dict[str, int] = {}
    for grant in _grants.values():
        counts[grant.user_digest] = counts.get(grant.user_digest, 0) + 1
    for user, count in list(counts.items()):
        while count > GRANT_USER_MAX:
            for key, grant in list(_grants.items()):
                if grant.user_digest == user:
                    _grants.pop(key, None)
                    count -= 1
                    break
    while len(_grants) > GRANT_GLOBAL_MAX:
        _grants.popitem(last=False)


async def grant_delivered(user_guid: str, founds: list[Found]) -> None:
    if not _user_ok(user_guid) or not founds:
        return
    digest = _user_digest(user_guid)
    now = time.time()
    async with _grant_lock:
        for found in founds:
            summary = _summary_for_disk(found.summary)
            _grants[(digest, found.identity)] = _Grant(
                user_digest=digest,
                identity=found.identity,
                entry=dict(found.entry),
                summary=summary,
                expires=now + GRANT_TTL_S,
            )
            _grants.move_to_end((digest, found.identity))
        _evict_grants(now)
        try:
            await _persist_grants()
        except Exception as exc:
            logger.warning("playlist discovery grant persist failed: %s", type(exc).__name__)


def discovery_allows(user_guid: str, entry: dict, conf: dict) -> bool:
    if not _user_ok(user_guid) or not isinstance(entry, dict) or not source_allowed(conf, entry):
        return False
    grant = _grants.get((_user_digest(user_guid), canonical_identity(entry)))
    if grant is None:
        return False
    if grant.expires <= time.time():
        _grants.pop((_user_digest(user_guid), canonical_identity(entry)), None)
        return False
    return True


def restore_discovered(register_guid) -> list[str]:
    path = _grant_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return []
    except Exception as exc:
        logger.warning("playlist discovery grant file rejected: %s", type(exc).__name__)
        return []
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("grants"), list):
        logger.warning("playlist discovery grant file rejected")
        return []
    now = time.time()
    restored: list[str] = []
    _grants.clear()
    for row in payload["grants"]:
        if not isinstance(row, dict):
            continue
        entry = row.get("entry")
        summary = row.get("summary")
        identity = row.get("identity")
        expires = row.get("expires")
        digest = str(row.get("user") or "")
        if (not digest or not isinstance(entry, dict) or not isinstance(summary, dict)
                or not isinstance(identity, list) or not isinstance(expires, (int, float))):
            continue
        if expires <= now or expires > now + GRANT_TTL_S + 60:
            continue
        identity_key = tuple(str(part) for part in identity)
        if identity_key != canonical_identity(entry):
            continue
        clean_summary = _summary_for_disk(summary)
        if not clean_summary["name"]:
            continue
        grant = _Grant(
            user_digest=digest, identity=identity_key, entry=dict(entry),
            summary=clean_summary, expires=float(expires),
        )
        _grants[(digest, identity_key)] = grant
        pubpl.remember_discovery_summary(entry, clean_summary)
        try:
            register_guid(pubpl.playlist_guid(entry))
        except Exception:
            logger.warning("playlist discovery guid restore failed")
            continue
        restored.append(pubpl.playlist_guid(entry))
    _evict_grants(now)
    return restored


async def shutdown() -> None:
    tasks = {task for task in _io_tasks if not task.done()}
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _io_tasks.clear()


def reset_for_test() -> None:
    for session in _sessions.values():
        session.generation += 1
    _sessions.clear()
    _grants.clear()
    for task in list(_io_tasks):
        if not task.done():
            task.cancel()
    _io_tasks.clear()
