from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict

import httpx


_USER_AGENT = "fnmusic-ext (https://github.com/javycoder/fnos_music_ext)"
_MBID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_WIKIDATA_URL = re.compile(r"https?://www\.wikidata\.org/wiki/(Q[1-9][0-9]*)")
_CACHE: OrderedDict[str, tuple[float, tuple[str, frozenset[tuple[str, str]]] | None]] = OrderedDict()
_INFLIGHT: dict[str, asyncio.Task] = {}
_LOCK = asyncio.Lock()
_NEXT_MUSICBRAINZ = 0.0
_LOOKUP_BUDGET_S = 8.0
_POSITIVE_TTL = 86400.0  # 已核验的 ISRC → 歌手映射按天缓存
_NEGATIVE_TTL = 3600.0  # 公共库明确查无：结果稳定，按小时重试
_BUSY_TTL = 60.0  # 应答异常或超时：只对本 ISRC 短暂退避
_UNREACHABLE_TTL = 1800.0  # 连接建立不起来：视为网络不可达，整体暂停查询
_unreachable_until = 0.0


def normalize_isrc(value) -> str:
    code = re.sub(r"[\s-]", "", str(value or "")).upper()
    return code if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}", code) else ""


async def _read_public_json(client: httpx.AsyncClient, url: str, params: dict | None) -> dict:
    if url.startswith("https://musicbrainz.org/"):
        global _NEXT_MUSICBRAINZ
        async with _LOCK:
            await asyncio.sleep(max(0, _NEXT_MUSICBRAINZ - time.monotonic()))
            _NEXT_MUSICBRAINZ = time.monotonic() + 1.1
    async with client.stream("GET", url, params=params) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > 1024 * 1024:
                return {}
    result = json.loads(body)
    return result if isinstance(result, dict) else {}


async def _public_json(client: httpx.AsyncClient, url: str, params: dict | None = None) -> dict:
    try:
        return await _read_public_json(client, url, params)
    except httpx.HTTPStatusError as error:
        if error.response.status_code not in (502, 503, 504):
            raise
        return await _read_public_json(client, url, params)


def _claims(entity: dict, property_id: str) -> set[str]:
    values = set()
    for claim in entity.get("claims", {}).get(property_id, []):
        if not isinstance(claim, dict) or claim.get("rank") == "deprecated":
            continue
        value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(value, str):
            values.add(value)
    return values


async def _find_public_artist(client: httpx.AsyncClient, isrc: str) -> tuple[str, frozenset[tuple[str, str]]] | None:
    recording = await _public_json(client, f"https://musicbrainz.org/ws/2/isrc/{isrc}",
                                   {"fmt": "json", "inc": "artist-credits"})
    recordings = recording.get("recordings")
    if not isinstance(recordings, list) or not recordings:
        return None
    artists, titles = set(), set()
    for row in recordings:
        if not isinstance(row, dict):
            return None
        credits, title = row.get("artist-credit"), row.get("title")
        if not isinstance(credits, list) or len(credits) != 1 or not isinstance(title, str):
            return None
        credit = credits[0]
        artist = credit.get("artist") if isinstance(credit, dict) else None
        mbid = artist.get("id") if isinstance(artist, dict) else None
        if not isinstance(mbid, str) or not _MBID.fullmatch(mbid):
            return None
        artists.add(mbid)
        titles.add(title)
    if len(artists) != 1 or len(titles) != 1:
        return None
    mbid = next(iter(artists))
    artist = await _public_json(client, f"https://musicbrainz.org/ws/2/artist/{mbid}",
                                {"fmt": "json", "inc": "url-rels"})
    if artist.get("id") != mbid:
        return None
    items = set()
    for relation in artist.get("relations", []):
        if isinstance(relation, dict) and relation.get("type") == "wikidata":
            url = relation.get("url")
            resource = url.get("resource", "") if isinstance(url, dict) else ""
            match = _WIKIDATA_URL.fullmatch(resource) if isinstance(resource, str) else None
            if match:
                items.add(match[1])
    if len(items) != 1:
        return None
    item_id = next(iter(items))
    directory = await _public_json(client, f"https://www.wikidata.org/wiki/Special:EntityData/{item_id}.json")
    entity = directory.get("entities", {}).get(item_id, {})
    if _claims(entity, "P434") != {mbid}:
        return None
    verified = set()
    for namespace, property_id, pattern in (
        ("wy", "P10445", r"[1-9][0-9]{0,19}"),
        ("tx", "P10410", r"[A-Za-z0-9]{1,50}"),
    ):
        values = _claims(entity, property_id)
        if len(values) == 1:
            artist_id = next(iter(values))
            if re.fullmatch(pattern, artist_id):
                verified.add((namespace, artist_id))
    return (next(iter(titles)), frozenset(verified)) if verified else None


async def public_artist_ids(isrc: str) -> tuple[str, frozenset[tuple[str, str]]] | None:
    code = normalize_isrc(isrc)
    if not code:
        return None
    now = time.monotonic()
    cached = _CACHE.get(code)
    if cached and cached[0] > now:
        _CACHE.move_to_end(code)
        return cached[1]
    if now < _unreachable_until:
        return None
    task = _INFLIGHT.get(code)
    if task is None:
        if len(_INFLIGHT) >= 4:
            return None
        task = asyncio.create_task(_cache_public_artist(code))
        _INFLIGHT[code] = task
        task.add_done_callback(lambda done: _INFLIGHT.pop(code, None))
    return await asyncio.shield(task)


async def _cache_public_artist(code: str) -> tuple[str, frozenset[tuple[str, str]]] | None:
    global _unreachable_until
    ttl = _NEGATIVE_TTL
    try:
        async with asyncio.timeout(_LOOKUP_BUDGET_S), httpx.AsyncClient(
            timeout=3.5, trust_env=False, headers={"User-Agent": _USER_AGENT},
        ) as client:
            result = await _find_public_artist(client, code)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        # 连不上公共库（被阻断或不可达）时，后续查询不再逐次等满连接超时。
        result, ttl = None, _BUSY_TTL
        _unreachable_until = time.monotonic() + _UNREACHABLE_TTL
    except (httpx.HTTPError, TimeoutError, ValueError, TypeError, AttributeError):
        result, ttl = None, _BUSY_TTL
    _CACHE[code] = (time.monotonic() + (_POSITIVE_TTL if result else ttl), result)
    _CACHE.move_to_end(code)
    while len(_CACHE) > 256:
        _CACHE.popitem(last=False)
    return result
