"""lxmusic HTTP 服务：洛雪音乐 (LX Music) 风格免登录音源 API.

统一曲目 ID 契约: "lx:<source>:<identifier>"，例如：
  - "lx:kg:<filehash>"   酷狗（trackercdn hash 解析直链）
  - "lx:wy:<song_id>"    网易云（eapi 解析直链）
  - "lx:mg:<copyrightId>" 咪咕（player_get_song_info 解析直链）

设计目标：全部免登录、无需任何账号 Cookie 即可搜索 + 高音质直链解析，
供 fnmusic-ext 代理（以及其它消费方）以统一的 REST 契约调用。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger("lxmusic_service")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

try:
    from Crypto.Cipher import AES  # pycryptodome

    HAS_CRYPTO = True
except ImportError:
    AES = None  # type: ignore
    HAS_CRYPTO = False
    logger.warning("pycryptodome not installed, wy eapi resolution disabled")

SERVICE_VERSION = "1.0.0"

CONF = {
    "sources": [s.strip() for s in os.environ.get("LX_SOURCES", "kg,wy,mg").split(",") if s.strip()],
    "search_timeout": float(os.environ.get("LX_SEARCH_TIMEOUT", "12")),
    "limit_per_source": int(os.environ.get("LX_LIMIT_PER_SOURCE", "20")),
    "url_timeout": float(os.environ.get("LX_URL_TIMEOUT", "10")),
    "cache_max": int(os.environ.get("LX_CACHE_MAX", "2000")),
    "cache_ttl": int(os.environ.get("LX_CACHE_TTL", "1800")),
}

# 支持的音源别名归一化
_SOURCE_ALIASES = {
    "kg": "kg",
    "kugou": "kg",
    "wy": "wy",
    "netease": "wy",
    "163": "wy",
    "mg": "mg",
    "migu": "mg",
}

# 各源直链需要携带的额外请求头（供代理透传给 CDN）
KG_HEADERS = {"User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36"}
WY_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://music.163.com/"}
MG_HEADERS = {"User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36", "Referer": "https://m.music.migu.cn/"}

_EAPI_KEY = b"#14ljk_!\\]&0U<'("

UA_PC = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
UA_MOBILE = "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"

# id -> {"item": {...}, "ts": float}
_SONG_CACHE: dict[str, dict] = {}
_STATS = {"searches": 0, "url_resolutions": 0, "errors": 0}


def normalize_source(raw: str) -> str:
    return _SOURCE_ALIASES.get((raw or "").strip().lower(), "")


def parse_track_id(track_id: str) -> "tuple[str, str]":
    """解析 "lx:<source>:<identifier>" -> ("kg", "<identifier>")；异常时返回 ("", "")."""
    parts = (track_id or "").strip().split(":", 2)
    if len(parts) == 3 and parts[0] == "lx":
        src = normalize_source(parts[1])
        if src and parts[2]:
            return src, parts[2]
    # 兼容 "kg:xxx" / "wy:xxx" 形式
    if len(parts) == 2:
        src = normalize_source(parts[0])
        if src and parts[1]:
            return src, parts[1]
    return "", ""


def _cache_put(item: dict) -> None:
    if not item.get("id"):
        return
    if len(_SONG_CACHE) >= CONF["cache_max"]:
        for k in sorted(_SONG_CACHE, key=lambda k: _SONG_CACHE[k]["ts"])[: len(_SONG_CACHE) // 2]:
            _SONG_CACHE.pop(k, None)
    _SONG_CACHE[item["id"]] = {"item": item, "ts": time.time()}


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
    if q in ("lossless", "flac", "sq", "hires", "hr"):
        return ["lossless", "high", "standard"]
    if q in ("high", "320", "exhigh", "hq"):
        return ["high", "standard"]
    return ["standard"]


def _kg_hash_for_quality(item: dict, tier: str) -> str:
    sq = str(item.get("hash_sq") or "")
    hq = str(item.get("hash_hq") or "")
    std = str(item.get("hash") or item.get("id", "").split(":")[-1] or "")
    if tier == "lossless":
        return sq or hq or std
    if tier == "high":
        return hq or std
    return std or hq or sq


# ------------------------------------------------------------------ 酷狗 kg ---

async def kg_search(client: httpx.AsyncClient, keyword: str, limit: int) -> list[dict]:
    r = await client.get(
        "http://mobilecdn.kugou.com/api/v3/search/song",
        params={
            "keyword": keyword,
            "format": "json",
            "page": 1,
            "pagesize": max(limit, 5),
            "showtype": 1,
        },
        headers={"User-Agent": UA_MOBILE},
    )
    r.raise_for_status()
    data = r.json()
    raw = ((data or {}).get("data") or {}).get("info") or []
    items = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        fhash = str(it.get("hash") or "")
        if not fhash:
            continue
        singer = str(it.get("singername") or "")
        title = str(it.get("songname") or it.get("filename") or "").replace(f"{singer} - ", "")
        sq = str(it.get("sqhash") or "")
        hq = str(it.get("hqhash") or "")
        duration_ms = int(it.get("duration") or 0)  # v3 接口 duration 为毫秒
        cover = str(it.get("origin_cover") or it.get("img") or "").replace("{size}", "480")
        item = {
            "id": f"lx:kg:{fhash}",
            "lx_source": "kg",
            "title": title,
            "artist": singer,
            "album": str(it.get("album_name") or ""),
            "duration_s": duration_ms / 1000.0,
            "ext": "flac" if sq else "mp3",
            "cover_url": cover,
            "file_size": int(sq and it.get("sq_size") or it.get("filesize") or 0) or 0,
            "lyric": "",
            "hash": fhash,
            "hash_hq": hq,
            "hash_sq": sq,
            "mixsongid": str(it.get("mixsongid") or ""),
        }
        _cache_put(item)
        items.append(item)
    return items


async def kg_resolve_url(
    client: httpx.AsyncClient, item: dict | None, identifier: str, tier: str
) -> dict | None:
    fhash = _kg_hash_for_quality(item or {"hash": identifier}, tier)
    if not fhash:
        return None
    last_err = None
    for host in ("https://trackercdnbj.kugou.com", "http://trackercdn.kugou.com"):
        try:
            r = await client.get(
                f"{host}/v1/url",
                params={"hash": fhash, "pid": 1, "appid": 1010, "behavior": "play"},
                headers={"User-Agent": UA_MOBILE},
            )
            data = r.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
        if isinstance(data, dict) and data.get("code") == 0 and data.get("url"):
            return {
                "url": str(data["url"]),
                "ext": str(data.get("ext") or "mp3").lower().lstrip(".") or "mp3",
                "file_size": int(data.get("file_size") or 0) or 0,
                "br": int((data.get("bitRate") or data.get("bitrate") or 0) or 0),
                "headers": dict(KG_HEADERS),
            }
    if last_err:
        raise last_err
    return None


async def kg_resolve_lyric(client: httpx.AsyncClient, item: dict) -> str:
    duration_ms = int(float(item.get("duration_s") or 0) * 1000)
    r = await client.get(
        "https://krcs.kugou.com/search",
        params={
            "ver": 1,
            "man": "yes",
            "client": "mobi",
            "keyword": f"{item.get('title','')} {item.get('artist','')}".strip(),
            "duration": duration_ms,
            "hash": item.get("hash") or "",
        },
        headers={"User-Agent": UA_MOBILE},
    )
    candidates = ((r.json() or {}).get("candidates") or [])
    if not candidates:
        return ""
    cand = candidates[0]
    r2 = await client.get(
        "http://lyrics.kugou.com/download",
        params={
            "ver": 1,
            "client": "pc",
            "id": cand.get("id"),
            "accesskey": cand.get("accesskey"),
            "fmt": "lrc",
            "charset": "utf8",
        },
        headers={"User-Agent": UA_PC},
    )
    content = (r2.json() or {}).get("content") or ""
    if not content:
        return ""
    try:
        return base64.b64decode(content).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------------ 网易 wy ---

def _eapi_params(eapi_path: str, payload: dict) -> str:
    """网易 eapi 参数加密（AES-ECB + MD5 摘要，与 LX Music 源一致）。"""
    import json as _json

    text = _json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    message = f"nobody{eapi_path}use{text}md5Encrypt"
    digest = hashlib.md5(message.encode("utf-8")).hexdigest()
    data = f"{eapi_path}-36cd479b6b5-{text}-36cd479b6b5-{digest}".encode("utf-8")
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    cipher = AES.new(_EAPI_KEY, AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(data)).decode()


_WY_EAPI_HEADER = {
    "osver": "",
    "deviceId": "",
    "appver": "9.1.15",
    "versioncode": "140",
    "mobilename": "",
    "buildver": "",
    "resolution": "1920x1080",
    "__nonce": "",
    "os": "pc",
    "countrycode": "",
    "MUSIC_U": "",
}


async def wy_search(client: httpx.AsyncClient, keyword: str, limit: int) -> list[dict]:
    r = await client.post(
        "https://music.163.com/api/search/get/web",
        data={"s": keyword, "type": 1, "offset": 0, "limit": max(limit, 5), "total": "true"},
        headers={
            "User-Agent": UA_PC,
            "Referer": "https://music.163.com/",
            "Cookie": "os=pc; appver=9.1.15",
        },
    )
    r.raise_for_status()
    songs = ((r.json() or {}).get("result") or {}).get("songs") or []
    items = []
    for it in songs:
        if not isinstance(it, dict):
            continue
        sid = str(it.get("id") or "")
        if not sid:
            continue
        artists = it.get("artists") or []
        artist = " / ".join(
            str(a.get("name") or "") for a in artists if isinstance(a, dict)
        )
        album = it.get("album") or {}
        item = {
            "id": f"lx:wy:{sid}",
            "lx_source": "wy",
            "title": str(it.get("name") or ""),
            "artist": artist,
            "album": str(album.get("name") or "") if isinstance(album, dict) else "",
            "duration_s": int(it.get("duration") or 0) / 1000.0,
            "ext": "mp3",
            "cover_url": str(album.get("picUrl") or "") if isinstance(album, dict) else "",
            "file_size": 0,
            "lyric": "",
            "song_id": sid,
        }
        _cache_put(item)
        items.append(item)
    return items


async def wy_resolve_url(client: httpx.AsyncClient, identifier: str, tier: str) -> dict | None:
    if not HAS_CRYPTO:
        raise RuntimeError("pycryptodome not installed: wy eapi unavailable")
    br_map = {"lossless": 999000, "high": 320000, "standard": 128000}
    brs = [br_map[t] for t in _quality_tiers(tier) if t in br_map]
    eapi_path = "/api/song/enhance/player/url"
    for br in brs:
        try:
            r = await client.post(
                "https://interface3.music.163.com/eapi/song/enhance/player/url",
                data={"params": _eapi_params(eapi_path, {"header": dict(_WY_EAPI_HEADER), "ids": [int(identifier)], "br": br})},
                headers={
                    "User-Agent": UA_PC,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cookie": "os=pc; appver=9.1.15; osver=Microsoft-Windows-10",
                },
            )
            data = (r.json() or {}).get("data") or []
        except Exception:  # noqa: BLE001
            continue
        for entry in data:
            if isinstance(entry, dict) and entry.get("url"):
                return {
                    "url": str(entry["url"]),
                    "ext": "mp3",
                    "file_size": int(entry.get("size") or 0) or 0,
                    "br": int(entry.get("br") or 0) or br,
                    "headers": dict(WY_HEADERS),
                }
    return None


async def wy_resolve_lyric(client: httpx.AsyncClient, identifier: str) -> str:
    r = await client.get(
        "https://music.163.com/api/song/lyric",
        params={"id": identifier, "lv": 1, "tv": -1},
        headers={"User-Agent": UA_PC, "Referer": "https://music.163.com/", "Cookie": "os=pc"},
    )
    lrc = (r.json() or {}).get("lrc") or {}
    return str(lrc.get("lyric") or "")


# ------------------------------------------------------------------ 咪咕 mg ---

async def mg_search(client: httpx.AsyncClient, keyword: str, limit: int) -> list[dict]:
    r = await client.get(
        "https://c.music.migu.cn/MIGUM2.0/v1.0/content/search_all.do",
        params={"text": keyword, "pageNo": 1, "pageSize": max(limit, 5), "resource": 1},
        headers={"User-Agent": UA_MOBILE, "Referer": "https://m.music.migu.cn/"},
    )
    r.raise_for_status()
    data = r.json() or {}
    raw = data.get("songs") or (data.get("songResultData") or {}).get("result") or []
    items = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        cid = str(it.get("copyrightId") or it.get("id") or "")
        if not cid:
            continue
        singers = it.get("singers") or []
        artist = " / ".join(str(s.get("name") or "") for s in singers if isinstance(s, dict))
        album = it.get("albums") or []
        album_name = str(album[0].get("albumName") or album[0].get("name") or "") if album and isinstance(album[0], dict) else ""
        covers = it.get("albumMaterialList") or []
        cover = str((covers[0] or {}).get("coverUrl") or "") if covers else ""
        tones = {str(t.get("toneType") or "").upper() for t in (it.get("toneFlags") or []) if isinstance(t, dict)}
        length_ms = int(it.get("length") or 0)
        item = {
            "id": f"lx:mg:{cid}",
            "lx_source": "mg",
            "title": str(it.get("songName") or ""),
            "artist": artist,
            "album": album_name,
            "duration_s": length_ms / 1000.0,
            "ext": "flac" if tones & {"SQ", "ZQ", "ZQ24"} else "mp3",
            "cover_url": cover,
            "file_size": 0,
            "lyric": "",
            "lrc_url": str(it.get("lrcUrl") or ""),
            "copyright_id": cid,
        }
        _cache_put(item)
        items.append(item)
    return items


async def mg_resolve_url(client: httpx.AsyncClient, identifier: str, tier: str) -> dict | None:
    r = await client.get(
        "https://music.migu.cn/v3/api/music/audio/player_get_song_info",
        params={"copyrightId": identifier, "resourceType": "E", "resourceLevel": tier},
        headers={"User-Agent": UA_PC, "Referer": "https://music.migu.cn/"},
    )
    data = (r.json() or {}).get("data") or {}
    url = str(data.get("play_url") or data.get("url") or "")
    if not url or url == "https://music.migu.cn/404/error.html":
        return None
    ext = str(data.get("format_type") or "mp3").lower().lstrip(".") or "mp3"
    return {
        "url": url,
        "ext": "flac" if ext in ("flac", "zq", "sq") else "mp3",
        "file_size": int(data.get("fileSize") or data.get("overdue_size") or 0) or 0,
        "br": int(data.get("bitRate") or 0) or 0,
        "headers": dict(MG_HEADERS),
    }


async def mg_resolve_lyric(client: httpx.AsyncClient, item: dict) -> str:
    lrc_url = str(item.get("lrc_url") or "")
    if not lrc_url:
        return ""
    r = await client.get(lrc_url, headers={"User-Agent": UA_MOBILE})
    return r.text or ""


# --------------------------------------------------------------------- app ---

_SEARCHERS = {"kg": kg_search, "wy": wy_search, "mg": mg_search}


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    created = False
    if getattr(fastapi_app.state, "http", None) is None:
        fastapi_app.state.http = httpx.AsyncClient(
            timeout=httpx.Timeout(CONF["search_timeout"], connect=5.0),
            follow_redirects=True,
        )
        created = True
    try:
        yield
    finally:
        if created:
            await fastapi_app.state.http.aclose()


app = FastAPI(title="fnmusic-lxmusic", version=SERVICE_VERSION, lifespan=lifespan)


def get_http(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "http", None)
    if client is None:
        client = httpx.AsyncClient(timeout=CONF["search_timeout"], follow_redirects=True)
        fastapi_app.state.http = client
    return client


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "service": "fnmusic-lxmusic",
        "version": SERVICE_VERSION,
        "sources": CONF["sources"],
        "eapi": HAS_CRYPTO,
    }


def _err(msg: str, code: int = 404) -> JSONResponse:
    return JSONResponse(content={"ok": False, "error": msg}, status_code=code)


@app.get("/api/v1/search")
async def search(
    keyword: str = Query("", alias="keyword"),
    q: str = Query("", alias="q"),
    limit: int = Query(0),
    sources: str = Query(""),
):
    kw = (keyword or q or "").strip()
    if not kw:
        return _err("keyword required", 400)
    _STATS["searches"] += 1
    if limit <= 0:
        limit = CONF["limit_per_source"]
    wanted_raw = [s.strip() for s in (sources or "").split(",") if s.strip()]
    wanted = [normalize_source(s) for s in wanted_raw]
    wanted = [s for s in wanted if s] or CONF["sources"]

    client = get_http(app)
    tasks = {}
    for src in wanted:
        fn = _SEARCHERS.get(src)
        if fn is None:
            continue
        tasks[src] = asyncio.create_task(fn(client, kw, limit))

    items: list[dict] = []
    errors: dict[str, str] = {}
    for src, task in tasks.items():
        try:
            items.extend(await asyncio.wait_for(task, timeout=max(CONF["search_timeout"], 8.0) * 2))
        except Exception as e:  # noqa: BLE001
            _STATS["errors"] += 1
            errors[src] = str(e)
            logger.warning("lx search %s failed: %s", src, e)

    return {"ok": True, "items": items, "errors": errors, "stats": dict(_STATS)}


@app.get("/api/v1/track/url")
async def track_url(
    id: str = Query("", alias="id"),
    guid: str = Query("", alias="guid"),
    quality: str = Query("lossless"),
):
    track_id = (id or guid or "").strip()
    src, identifier = parse_track_id(track_id)
    if not src or not identifier:
        return _err(f"invalid track id: {track_id}", 400)
    _STATS["url_resolutions"] += 1
    cached = _cache_get(track_id)
    client = get_http(app)
    try:
        if src == "kg":
            result = None
            for tier in _quality_tiers(quality):
                result = await kg_resolve_url(client, cached, identifier, tier)
                if result:
                    break
        elif src == "wy":
            result = await wy_resolve_url(client, identifier, quality)
        elif src == "mg":
            result = await mg_resolve_url(client, identifier, "E")
        else:
            return _err(f"unsupported source: {src}", 400)
    except Exception as e:  # noqa: BLE001
        _STATS["errors"] += 1
        logger.warning("lx url resolve %s failed: %s", track_id, e)
        return _err(f"resolve failed: {e}", 502)

    if not result:
        return _err("no playable url", 404)
    return {"ok": True, "data": {"id": track_id, "quality": quality, **result}}


@app.get("/api/v1/track/info")
async def track_info(id: str = Query("", alias="id"), guid: str = Query("", alias="guid")):
    track_id = (id or guid or "").strip()
    src, identifier = parse_track_id(track_id)
    if not src:
        return _err(f"invalid track id: {track_id}", 400)
    cached = _cache_get(track_id)
    if cached:
        return {"ok": True, "data": cached}
    return {"ok": True, "data": {"id": track_id, "source": "lx", "lx_source": src, "title": "", "artist": "", "album": "", "duration_s": 0, "ext": "mp3", "file_size": 0, "cover_url": "", "lyric": ""}}


@app.get("/api/v1/track/lyric")
async def track_lyric(id: str = Query("", alias="id"), guid: str = Query("", alias="guid")):
    track_id = (id or guid or "").strip()
    src, identifier = parse_track_id(track_id)
    if not src:
        return _err(f"invalid track id: {track_id}", 400)
    cached = _cache_get(track_id) or {}
    client = get_http(app)
    text = ""
    try:
        if src == "kg":
            text = await kg_resolve_lyric(client, cached or {"hash": identifier, "title": "", "artist": "", "duration_s": 0})
        elif src == "wy":
            text = await wy_resolve_lyric(client, identifier)
        elif src == "mg":
            text = await mg_resolve_lyric(client, cached)
    except Exception as e:  # noqa: BLE001
        logger.warning("lx lyric %s failed: %s", track_id, e)
    return {"ok": True, "data": {"id": track_id, "lyric": text or ""}}
