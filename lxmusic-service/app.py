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
from contextvars import ContextVar
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

SERVICE_VERSION = "1.1.0"

CONF = {
    "sources": [s.strip() for s in os.environ.get("LX_SOURCES", "kg,wy,mg,kw").split(",") if s.strip()],
    "search_timeout": float(os.environ.get("LX_SEARCH_TIMEOUT", "12")),
    "limit_per_source": int(os.environ.get("LX_LIMIT_PER_SOURCE", "20")),
    "url_timeout": float(os.environ.get("LX_URL_TIMEOUT", "10")),
    "cache_max": int(os.environ.get("LX_CACHE_MAX", "2000")),
    "cache_ttl": int(os.environ.get("LX_CACHE_TTL", "1800")),
    # 第三方解析链路（移植自洛雪社区聚合源 qdy v9.3 链路清单）总开关
    "third_party": os.environ.get("LX_THIRD_PARTY", "1").strip().lower() in ("1", "true", "yes", "on"),
    "resolver_timeout": float(os.environ.get("LX_RESOLVER_TIMEOUT", "4.0")),
    "probe_timeout": float(os.environ.get("LX_PROBE_TIMEOUT", "5.0")),
    # 搜索期 VIP/第三方直链曲目的探活结果有效期（秒）：过期后 track/url 重新解析
    "probe_fresh_s": int(os.environ.get("LX_PROBE_FRESH_S", "900")),
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
    "tx": "tx",
    "qq": "tx",
    "tencent": "tx",
    "kw": "kw",
    "kuwo": "kw",
}

UA_PC = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
UA_MOBILE = "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"

# 各源直链需要携带的额外请求头（供代理透传给 CDN）
KG_HEADERS = {"User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36"}
WY_HEADERS = {"User-Agent": UA_PC, "Referer": "https://music.163.com/"}
MG_HEADERS = {"User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36", "Referer": "https://m.music.migu.cn/"}
TX_HEADERS = {"User-Agent": UA_PC, "Referer": "https://y.qq.com/"}
KW_HEADERS = {"User-Agent": UA_PC}

_EAPI_KEY = b"e82ckenh8dichen8"

# id -> {"item": {...}, "ts": float}
_SONG_CACHE: dict[str, dict] = {}
_RESOLUTION_FAILURES: ContextVar[list | None] = ContextVar("lx_resolution_failures", default=None)


def _record_failure(exc: Exception) -> None:
    failures = _RESOLUTION_FAILURES.get()
    if failures is not None:
        failures.append(str(exc) or type(exc).__name__)


def _check_resolver_status(response: httpx.Response) -> None:
    if response.status_code >= 500 or response.status_code in (408, 429):
        raise ChainTransportError(f"resolver HTTP {response.status_code}")


_SEARCH_PARTIAL: ContextVar[list | None] = ContextVar("lx_search_partial", default=None)


def _publish(item: dict) -> None:
    partial = _SEARCH_PARTIAL.get()
    if partial is not None:
        partial.append(item)


_STATS = {"searches": 0, "url_resolutions": 0, "errors": 0}


def _lenient_json(resp: httpx.Response, tag: str = "") -> dict | list | None:
    """宽容解析 JSON：第三方接口可能返回 Content-Type text/html 但内容为合法 JSON。"""
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        pass
    text = (resp.text or "").strip()
    if not text or (not text.startswith("{") and not text.startswith("[")):
        logger.warning(
            "%s: upstream returned non-JSON body (HTTP %s, CT %s): %.80s",
            tag,
            resp.status_code,
            resp.headers.get("content-type"),
            text,
        )
        return None
    import json as _json
    try:
        return _json.loads(text)
    except Exception as e:  # noqa: BLE001
        logger.warning("%s: json parse failed: %s (%.80s)", tag, e, text)
        return None


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
    # 检索更多条目以便剔除收费/VIP曲目后仍能满足 limit 数量
    fetch_size = max(limit * 3, 20)
    r = await client.get(
        "http://mobilecdn.kugou.com/api/v3/search/song",
        params={
            "keyword": keyword,
            "format": "json",
            "page": 1,
            "pagesize": fetch_size,
            "showtype": 1,
        },
        headers={"User-Agent": UA_MOBILE},
    )
    r.raise_for_status()
    data = r.json()
    raw = ((data or {}).get("data") or {}).get("info") or []
    items = []
    vip_candidates = []
    for it in raw:
        if not isinstance(it, dict) or _explicit_trial(it):
            continue
        fhash = str(it.get("hash") or "")
        if not fhash:
            continue

        # 可播放性过滤：
        # 1. 收费/VIP 曲目不直接丢弃，转入探活队列（直链解析+Range探活通过才返回）
        pay_type = int(it.get("pay_type") or 0)
        is_vip = pay_type != 0 or int(it.get("pkg_price") or 0) != 0 or int(it.get("price") or 0) != 0
        # 2. 排除仅免费试听片段标记 (is_free_part=1) 及 VIP 拦截 (fail_process=4)，真不可播
        if int(it.get("is_free_part") or 0) != 0 or int(it.get("fail_process") or 0) == 4:
            continue

        singer = str(it.get("singername") or "")
        title = str(it.get("songname") or it.get("filename") or "").replace(f"{singer} - ", "")
        # 3. 标题带有试听片段标记的坚决不返回
        if any(marker in title for marker in _TRIAL_TITLE_MARKERS):
            continue
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
            "pay_type": pay_type,
        }
        if is_vip:
            vip_candidates.append(item)
        else:
            item.update(verified=False, validation_status="unverified", completeness="unknown")
            _cache_put(item)
            items.append(item)
            _publish(item)
    # VIP 候选批量探活，通过（verified）才补进结果
    if vip_candidates and len(items) < limit:
        want = min(len(vip_candidates), limit - len(items) + limit // 2)
        items.extend(await _probe_candidates(client, "kg", vip_candidates[:want], limit - len(items)))
    return items[: limit + limit // 2]


async def kg_resolve_url(
    client: httpx.AsyncClient, item: dict | None, identifier: str, tier: str
) -> dict | None:
    fhash = _kg_hash_for_quality(item or {"hash": identifier}, tier)
    if not fhash:
        return None

    # 1. 主接口：m.kugou.com 移动端 playInfo（免登录可用，返回 128k mp3 直链）
    try:
        r = await client.get(
            "http://m.kugou.com/app/i/getSongInfo.php",
            params={"cmd": "playInfo", "hash": fhash},
            headers={"User-Agent": UA_MOBILE},
            timeout=8.0,
        )
        _check_resolver_status(r)
        data = _lenient_json(r, f"kg playInfo {fhash}")
        if isinstance(data, dict) and data.get("errcode") == 0 and data.get("url"):
            ext = str(data.get("extName") or "mp3").lower().lstrip(".") or "mp3"
            candidate = {
                "trial": _explicit_trial(data),
                "url": str(data["url"]),
                "ext": ext,
                "file_size": int(data.get("fileSize") or 0) or 0,
                "br": int(data.get("bitRate") or 0) * 1000 if int(data.get("bitRate") or 0) < 1000 else int(data.get("bitRate") or 0),
                "headers": dict(KG_HEADERS),
            }
            result = await _verify_result(client, candidate, report_transport=True)
            if result:
                return result
    except Exception as e:  # noqa: BLE001
        _record_failure(e)
        logger.warning("kg playInfo %s failed: %s", fhash, e)

    # 2. 备用接口：老版 trackercdn（部分地区/IP 或自建反代可能可用）
    last_err = None
    for host in ("https://trackercdnbj.kugou.com", "http://trackercdn.kugou.com"):
        try:
            r = await client.get(
                f"{host}/v1/url",
                params={"hash": fhash, "pid": 1, "appid": 1010, "behavior": "play"},
                headers={"User-Agent": UA_MOBILE},
                timeout=6.0,
            )
            _check_resolver_status(r)
            data = _lenient_json(r, f"kg trackercdn {fhash}")
        except Exception as e:  # noqa: BLE001
            _record_failure(e)
            last_err = e
            continue
        if isinstance(data, dict) and data.get("code") == 0 and data.get("url"):
            candidate = {
                "trial": _explicit_trial(data),
                "url": str(data["url"]),
                "ext": str(data.get("ext") or "mp3").lower().lstrip(".") or "mp3",
                "file_size": int(data.get("file_size") or 0) or 0,
                "br": _bitrate_bps(data.get("bitRate") or data.get("bitrate")),
                "headers": dict(KG_HEADERS),
            }
            try:
                result = await _verify_result(client, candidate, report_transport=True)
            except (httpx.HTTPError, ChainTransportError, TimeoutError) as exc:
                _record_failure(exc)
                continue
            if result:
                return result
    if last_err:
        logger.warning("kg trackercdn %s last error: %s", fhash, last_err)
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

    from urllib.parse import urlparse

    eapi_path = urlparse(eapi_path).path.replace("/eapi/", "/api/")
    # Match the independent musicdl EapiCryptoUtils wire serialization.
    text = _json.dumps(payload)
    message = f"nobody{eapi_path}use{text}md5forencrypt"
    digest = hashlib.md5(message.encode("utf-8")).hexdigest()
    data = f"{eapi_path}-36cd479b6b5-{text}-36cd479b6b5-{digest}".encode("utf-8")
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    cipher = AES.new(_EAPI_KEY, AES.MODE_ECB)
    return cipher.encrypt(data).hex()


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
    fetch_limit = max(limit * 2, 20)
    r = await client.post(
        "https://music.163.com/api/search/get/web",
        data={"s": keyword, "type": 1, "offset": 0, "limit": fetch_limit, "total": "true"},
        headers={
            "User-Agent": UA_PC,
            "Referer": "https://music.163.com/",
            "Cookie": "os=pc; appver=9.1.15",
        },
    )
    r.raise_for_status()
    songs = ((r.json() or {}).get("result") or {}).get("songs") or []
    items = []
    vip_candidates = []
    for it in songs:
        if not isinstance(it, dict) or _explicit_trial(it):
            continue
        sid = str(it.get("id") or "")
        if not sid:
            continue

        # 可播放性过滤：fee∉(0,8) 的 VIP/付费曲不直接丢弃，转探活队列验证
        fee = int(it.get("fee") or 0)
        is_vip = fee not in (0, 8)

        # 排除无版权（真不可播）
        if it.get("noCopyrightRcmd") is not None and it.get("noCopyrightRcmd") != 0:
            continue

        # 检查 privilege 状态
        priv = it.get("privilege")
        if isinstance(priv, dict):
            priv_fee = int(priv.get("fee", fee))
            if priv_fee not in (0, 8):
                is_vip = True
            if int(priv.get("pl") or 0) <= 0 and int(priv.get("st") or 0) < 0:
                continue
            if priv.get("freeTrialPrivilege") and priv.get("freeTrialPrivilege", {}).get("cannotListenReason"):
                continue

        title = str(it.get("name") or "")
        # 排除标题含试听片段标记
        if any(marker in title for marker in _TRIAL_TITLE_MARKERS):
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
            "fee": fee,
        }
        if is_vip:
            vip_candidates.append(item)
        else:
            item.update(verified=False, validation_status="unverified", completeness="unknown")
            _cache_put(item)
            items.append(item)
            _publish(item)
    # VIP 候选批量探活，通过（verified）才补进结果
    if vip_candidates and len(items) < limit:
        want = min(len(vip_candidates), limit - len(items) + limit // 2)
        items.extend(await _probe_candidates(client, "wy", vip_candidates[:want], limit - len(items)))
    return items[: limit + limit // 2]


async def wy_resolve_url(client: httpx.AsyncClient, identifier: str, tier: str) -> dict | None:
    # 1. 优先尝试官方 eapi 高音质解析（需 pycryptodome）
    if HAS_CRYPTO:
        br_map = {"lossless": 999000, "high": 320000, "standard": 128000}
        brs = [br_map[_quality_tiers(tier)[0]]]
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
                    timeout=8.0,
                )
                _check_resolver_status(r)
                if r.status_code in (401, 403, 404, 410):
                    continue
                data = (r.json() or {}).get("data") or []
            except Exception as exc:  # noqa: BLE001
                _record_failure(exc)
                continue
            for entry in data:
                if isinstance(entry, dict) and entry.get("url"):
                    candidate = {
                        "url": str(entry["url"]),
                        "trial": _explicit_trial(entry),
                        "ext": str(entry.get("type") or "mp3").lower(),
                        "file_size": int(entry.get("size") or 0) or 0,
                        "br": int(entry.get("br") or 0),
                        "headers": dict(WY_HEADERS),
                    }
                    try:
                        result = await _verify_result(client, candidate, report_transport=True)
                    except (httpx.HTTPError, ChainTransportError, TimeoutError) as exc:
                        _record_failure(exc)
                        continue
                    if result:
                        return result

    # Standard-only outer URL; the shared pipeline owns the single probe.
    if tier == "standard":
        return {"url": f"https://music.163.com/song/media/outer/url?id={identifier}",
                "ext": "mp3", "br": 128000, "headers": dict(WY_HEADERS)}
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
    fetch_size = max(limit * 2, 10)
    r = await client.get(
        "https://c.music.migu.cn/MIGUM2.0/v1.0/content/search_all.do",
        params={"text": keyword, "pageNo": 1, "pageSize": fetch_size, "resource": 1},
        headers={"User-Agent": UA_MOBILE, "Referer": "https://m.music.migu.cn/"},
    )
    r.raise_for_status()
    data = r.json() or {}
    raw = data.get("songs") or (data.get("songResultData") or {}).get("result") or []

    def _map_one(it: dict) -> dict | None:
        if not isinstance(it, dict):
            return None
        cid = str(it.get("copyrightId") or it.get("id") or "")
        if not cid:
            return None
        title = str(it.get("songName") or "")
        if _explicit_trial(it) or any(marker in title for marker in _TRIAL_TITLE_MARKERS):
            return None
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
            "title": title,
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
        return item

    candidates = [item for it in raw[:fetch_size] if (item := _map_one(it))]
    return await _probe_candidates(client, "mg", candidates, limit)


async def mg_resolve_url(client: httpx.AsyncClient, identifier: str, tier: str) -> dict | None:
    try:
        r = await client.get(
            "https://music.migu.cn/v3/api/music/audio/player_get_song_info",
            params={"copyrightId": identifier, "resourceType": "E", "resourceLevel": {"lossless": "ZQ", "high": "PQ", "standard": "E"}.get(tier, tier)},
            headers={"User-Agent": UA_PC, "Referer": "https://music.migu.cn/"},
            timeout=8.0,
        )
        _check_resolver_status(r)
        json_obj = _lenient_json(r, f"mg player_get_song_info {identifier}")
        if not isinstance(json_obj, dict):
            return None
        data = json_obj.get("data") or {}
        url = str(data.get("play_url") or data.get("url") or "")
        if not url or url == "https://music.migu.cn/404/error.html":
            return None
        ext = str(data.get("format_type") or "mp3").lower().lstrip(".") or "mp3"
        return {
            "url": url,
            "trial": _explicit_trial(data),
            "ext": "flac" if ext in ("flac", "zq", "sq") else "mp3",
            "file_size": int(data.get("fileSize") or data.get("overdue_size") or 0) or 0,
            "br": _bitrate_bps(data.get("bitRate")),
            "headers": dict(MG_HEADERS),
        }
    except Exception as e:  # noqa: BLE001
        _record_failure(e)
        logger.warning("mg resolve %s failed: %s", identifier, e)
        return None


async def mg_resolve_lyric(client: httpx.AsyncClient, item: dict) -> str:
    lrc_url = str(item.get("lrc_url") or "")
    if not lrc_url:
        return ""
    r = await client.get(lrc_url, headers={"User-Agent": UA_MOBILE})
    return r.text or ""


# --------------------------------------------- 第三方解析链路（移植自洛雪社区聚合源 qdy v9.3） ---
#
# 链路清单于 2026-09-09 逐条实测（qdy v9.3 全部 10 条链路）：
#   [活] 长青kw    musicapi.haitangw.net/music/kw.php   302→酷我CDN，无损FLAC 206 实测 0.5~0.8s
#   [活] 溯音咪咕  api.xcvts.cn/api/music/migu           JSON 返回 music_url(320k)+歌词
#   [死] 星海主    music-api.gdstudio.xyz/api.php        source 枚举收缩(tencent 拒绝)、netease 解析返回空
#   [死] 长青tx/wy 175.27.166.236                       tx 全曲返回 "has not any level"；wy 404
#   [死] 长青kg    music.haitangw.cc                     code 201 error（多 hash/level 验证）
#   [死] 念心      music.nxinxz.com                      404
#   [死] 溯音QQ/163/酷我 oiapi.net                       DNS 不存在
#   [死] 汽水      api.vsaa.cn                           404
#   [死] Huibq/聆川 qdy 脚本内即为占位符（"your_key_here"）
# 已死链路不注册；后续复活时在此追加即可，调度/探活/熔断逻辑无需改动。
#
# 第三方直链经有界媒体签名探测；verified 仅表示媒体前缀有效，
# 不保证完整歌曲、授权状态或未来可用性；completeness 始终保守标记 unknown。


def _content_total_size(r: httpx.Response) -> int:
    cr = r.headers.get("content-range") or ""
    if "/" in cr:
        tail = cr.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            return int(tail)
    # A partial response's Content-Length is NOT the whole file size.
    cl = r.headers.get("content-length") or ""
    return int(cl) if r.status_code == 200 and cl.isdigit() else 0


_PROBE_BYTES = 4096


class ChainTransportError(Exception):
    """Infrastructure failure, unlike a healthy resolver's song miss."""


def _media_signature(body: bytes) -> str:
    """Positive signatures only; MIME and byte counts do not prove a full song."""
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
    if len(body) >= 4 and body[0] == 0xff:
        if body[1] & 0xf6 == 0xf0:  # ADTS AAC
            return "aac"
        if (body[1] & 0xe0 == 0xe0 and body[1] & 6
                and body[2] & 0xf0 not in (0, 0xf0) and body[2] & 12 != 12):
            return "mp3"
    return ""


async def probe_url(
    client: httpx.AsyncClient, url: str, headers: "dict | None" = None,
    *, report_transport: bool = False,
) -> "tuple[bool, str, str, int]":
    """Bounded streaming prefix inspection, even when a CDN ignores Range.

    The tuple remains API-compatible. Success means media prefix verified, not
    complete-song verification. Streams close on success, failure and cancellation.
    """
    h = {k: v for k, v in (headers or {}).items()
         if k.lower() not in ("range", "accept-encoding")}
    h.setdefault("User-Agent", UA_PC)
    h.update({"Range": f"bytes=0-{_PROBE_BYTES - 1}", "Accept-Encoding": "identity"})
    try:
        async with asyncio.timeout(CONF["probe_timeout"]):
            for _ in range(6):  # at most five redirects, one shared deadline
                async with client.stream("GET", url, headers=h, follow_redirects=False,
                                         timeout=CONF["probe_timeout"]) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        location = r.headers.get("location")
                        if not location:
                            return False, str(r.url), "", 0
                        target = r.url.join(location)
                        if target.scheme not in ("http", "https"):
                            return False, str(r.url), "", 0
                        if target.host != r.url.host:
                            h = {k: v for k, v in h.items()
                                 if k.lower() not in ("authorization", "cookie", "host")}
                        url = str(target)
                        # Leaving this context closes the intermediate response
                        # WITHOUT HTTPX's automatic redirect body draining.
                        continue
                    ct = (r.headers.get("content-type") or "").lower()
                    if r.status_code >= 500 or r.status_code in (408, 429):
                        raise ChainTransportError(f"media HTTP {r.status_code}")
                    if r.status_code not in (200, 206):
                        return False, str(r.url), ct, 0
                    if (ct.startswith("text/") or "json" in ct or "xml" in ct
                            or r.headers.get("content-encoding", "identity") != "identity"):
                        return False, str(r.url), ct, 0
                    prefix = bytearray()
                    # aiter_raw avoids decompression/buffering the full response. Slice
                    # each transport chunk and stop as soon as a signature is known.
                    if r.is_stream_consumed:  # in-memory transports (e.g. MockTransport)
                        prefix.extend(r.content[:_PROBE_BYTES])
                    else:
                        async for chunk in r.aiter_raw():
                            prefix.extend(chunk[:_PROBE_BYTES - len(prefix)])
                            ext = _media_signature(prefix)
                            if ext or len(prefix) >= _PROBE_BYTES:
                                break
                    ext = _media_signature(prefix)
                    return bool(ext), str(r.url), (f"audio/{ext}" if ext else ct), _content_total_size(r) if ext else 0
            raise ChainTransportError("media redirect limit exceeded")
    except (httpx.HTTPError, TimeoutError, ChainTransportError) as exc:
        if report_transport:
            raise ChainTransportError(str(exc)) from exc
        return False, url, "", 0


# 链路熔断器：连续失败达阈值后暂停该链路一段时间，避免每次搜索白等超时
_CHAIN_FAIL_THRESHOLD = 3
_CHAIN_OPEN_SECONDS = 600
_CHAIN_HEALTH: dict[str, dict] = {}


def _chain_available(name: str) -> bool:
    h = _CHAIN_HEALTH.get(name)
    return not (h and (h.get("open_until", 0) > time.time() or h.get("half_open")))


def _chain_acquire(name: str) -> bool:
    if not _chain_available(name):
        return False
    h = _CHAIN_HEALTH.get(name)
    if h and h.get("open_until"):
        h["half_open"] = True  # synchronous claim: only one recovery request
    return True


def _chain_report(name: str, ok: bool) -> None:
    h = _CHAIN_HEALTH.setdefault(name, {"fails": 0, "open_until": 0.0, "breaks": 0})
    was_half_open = h.pop("half_open", False)
    if ok:
        h["fails"] = 0
        h["open_until"] = 0.0
        return
    h["fails"] = int(h.get("fails") or 0) + 1
    if was_half_open or h["fails"] >= _CHAIN_FAIL_THRESHOLD:
        h["open_until"] = time.time() + _CHAIN_OPEN_SECONDS
        h["fails"] = 0
        h["breaks"] = int(h.get("breaks") or 0) + 1


def _fuzzy_contains(a: str, b: str) -> bool:
    """宽松匹配（去括号/空格/符号后双向包含），用于搜索式链路防错歌。"""

    def norm(s: str) -> str:
        import re as _re

        s = _re.sub(r"\([^)]*\)", "", s or "")
        s = _re.sub(r"[\s\-—·・]", "", s)
        return s.lower()

    na, nb = norm(a), norm(b)
    return bool(na and nb) and (na in nb or nb in na)


_TIER_TO_NETESE_LEVEL = {"lossless": "lossless", "high": "exhigh", "standard": "standard"}


async def _chain_changqing_kw(client: httpx.AsyncClient, ctx: dict) -> "dict | None":
    """长青 kw：URL 模板 → 302 → 酷我 CDN 直链（实测无损 FLAC）。"""
    from urllib.parse import quote

    level = _TIER_TO_NETESE_LEVEL.get(ctx["tier"], "standard")
    url = f"https://musicapi.haitangw.net/music/kw.php?type=mp3&id={quote(str(ctx['identifier']))}&level={level}"
    # Resolution only. The dispatcher owns the single media verification.
    return {"url": url, "headers": dict(KW_HEADERS)}


async def _chain_suyin_migu(client: httpx.AsyncClient, ctx: dict) -> "dict | None":
    """溯音咪咕：关键词搜索式解析，返回 music_url(320k)。需标题模糊匹配防错歌。"""
    keyword = " ".join(x for x in (ctx.get("title"), ctx.get("artist")) if x).strip()
    if not keyword:
        return None
    try:
        r = await client.get(
            "https://api.xcvts.cn/api/music/migu",
            params={"gm": keyword, "n": 1, "num": 1, "type": "json"},
            headers={"User-Agent": UA_MOBILE},
            timeout=CONF["resolver_timeout"],
        )
        if r.status_code >= 500 or r.status_code in (408, 429):
            raise ChainTransportError(f"resolver HTTP {r.status_code}")
        if r.status_code in (404, 410):
            return None
        r.raise_for_status()
        data = _lenient_json(r, "suyin mg")
        if not isinstance(data, dict):
            raise ChainTransportError("invalid resolver response")
    except httpx.HTTPError as exc:
        raise ChainTransportError(str(exc)) from exc
    if not isinstance(data, dict) or int(data.get("code") or 0) != 200:
        return None
    if ctx.get("title") and not _fuzzy_contains(str(data.get("title") or ""), str(ctx["title"])):
        return None
    if ctx.get("artist") and not _fuzzy_contains(str(data.get("singer") or ""), str(ctx["artist"])):
        return None
    url = str(data.get("music_url") or "")
    if not url.startswith(("http://", "https://")):
        return None
    return {
        "url": url,
        "ext": "mp3",
        "file_size": int(data.get("size") or 0),
        "br": int(data.get("br") or 0),
        "headers": dict(MG_HEADERS),
        "trial": _explicit_trial(data),
    }


THIRD_PARTY_CHAIN: list[dict] = [
    {
        "name": "changqing_kw",
        "platforms": {"kw"},
        "needs_keyword": False,
        "fn": _chain_changqing_kw,
    },
    {
        "name": "suyin_mg",
        "platforms": {"mg"},
        "needs_keyword": True,
        "fn": _chain_suyin_migu,
    },
]


async def resolve_third_party(
    client: httpx.AsyncClient,
    platform: str,
    identifier: str,
    tier: str = "standard",
    title: str = "",
    artist: str = "",
) -> "dict | None":
    """按注册表顺序走第三方链路解析直链；返回结果均已通过探活。"""
    if not CONF["third_party"]:
        return None
    for link in THIRD_PARTY_CHAIN:
        if platform not in link["platforms"]:
            continue
        if link.get("needs_keyword") and not (title or artist):
            continue
        if not _chain_acquire(link["name"]):
            _record_failure(ChainTransportError("resolver circuit open"))
            continue
        recovering = bool(_CHAIN_HEALTH.get(link["name"], {}).get("half_open"))
        ctx = {"identifier": identifier, "tier": tier, "title": title, "artist": artist}
        try:
            result = await asyncio.wait_for(
                link["fn"](client, ctx), timeout=CONF["resolver_timeout"] + CONF["probe_timeout"]
            )
            if result and result.get("url"):
                result = await _verify_result(client, result, report_transport=True)
        except asyncio.CancelledError:
            # Deadline/client cancellation says nothing about provider health.
            _CHAIN_HEALTH.get(link["name"], {}).pop("half_open", None)
            raise
        except Exception as exc:  # transport, timeout, or broken resolver protocol
            _record_failure(exc)
            _chain_report(link["name"], False)
            continue
        # A missing song, mismatch, or rejected media is not a provider outage.
        # A late pre-open request must not close a circuit opened by siblings.
        if recovering or not _CHAIN_HEALTH.get(link["name"], {}).get("open_until"):
            _chain_report(link["name"], True)
        if result:
            result["resolver"] = link["name"]
            result["third_party"] = True
            return result
    return None


def chain_health_snapshot() -> dict:
    now = time.time()
    snapshot = {}
    for link in THIRD_PARTY_CHAIN:
        name = link["name"]
        h = _CHAIN_HEALTH.get(name, {})
        state = ("disabled" if not CONF["third_party"] else
                 "half_open" if h.get("half_open") else
                 "open" if h.get("open_until", 0) > now else
                 "recovery_ready" if h.get("open_until") else "closed")
        snapshot[name] = {"fails": h.get("fails", 0), "open": state == "open",
                          "breaks": h.get("breaks", 0), "state": state,
                          "enabled": CONF["third_party"]}
    return snapshot


def source_capabilities() -> dict:
    result = {}
    for src in _SEARCHERS:
        official = src in ("kg", "wy", "mg")
        links = [link for link in THIRD_PARTY_CHAIN if src in link["platforms"]]
        available = CONF["third_party"] and any(_chain_available(link["name"]) for link in links)
        reason = ("" if official or available else "third_party_disabled" if not CONF["third_party"]
                  else "no_resolver_registered" if not links else "resolver_circuit_open")
        result[src] = {"search_available": True, "playback_available": bool(official or available),
                       "official_resolver": official, "third_party_enabled": CONF["third_party"],
                       "chains": [link["name"] for link in links], "reason": reason,
                       "validation_status": "unverified", "completeness": "unknown"}
    return result


# ------------------------------------------------------------ 可播性验证 ---

_TRIAL_TITLE_MARKERS = ("(试听)", "（试听）", "试听片段", "片段试听", "试听版")


_TIER_RANK = {"standard": 0, "high": 1, "lossless": 2}


def _explicit_trial(data: dict) -> bool:
    """Only explicit clip metadata; fee/VIP and size are not trial proof."""
    for key in ("trial", "is_trial", "isTrial", "is_free_part", "isFreePart"):
        if str(data.get(key, "")).lower() in ("1", "true", "yes"):
            return True
    return bool(data.get("freeTrialInfo") or data.get("trialInfo")
                or data.get("trial_url") or data.get("trialUrl"))


def _bitrate_bps(value: Any) -> int:
    br = int(value or 0)
    return br * 1000 if 0 < br < 1000 else br


def _actual_tier(result: dict) -> str:
    if result.get("ext") in ("flac", "wav", "ape"):
        return "lossless"
    br = int(result.get("br") or 0)
    if br >= 256000:
        return "high"
    return "standard" if br > 0 else "unknown"


async def _verify_result(client: httpx.AsyncClient, result: dict | None,
                         *, report_transport: bool = False) -> dict | None:
    if not result or not result.get("url") or _explicit_trial(result):
        return None
    result = dict(result)
    if not result.get("probed"):
        ok, final, ct, size = await probe_url(client, result["url"], result.get("headers"),
                                            report_transport=report_transport)
        if not ok:
            return None
        result.update(url=final, file_size=size or result.get("file_size") or 0,
                      ext=ct.split("/")[-1], probed=True)
    result.update(validation_status="media_verified", completeness="unknown")
    result["actual_tier"] = _actual_tier(result)
    return result


def _fresh_probe(item: "dict | None", want_tier: str = "standard") -> "dict | None":
    """Reuse actual quality or a completed downgrade for this requested tier."""
    if not isinstance(item, dict) or _explicit_trial(item):
        return None
    p = item.get("_probe")
    if not (isinstance(p, dict) and p.get("url") and p.get("probed")
            and p.get("validation_status") == "media_verified"):
        return None
    if _explicit_trial(p) or (p.get("third_party") and not CONF["third_party"]):
        return None
    if time.time() - p.get("ts", 0) >= CONF["probe_fresh_s"]:
        return None
    want_tier = _quality_tiers(want_tier)[0]
    rank = _TIER_RANK.get(p.get("actual_tier"), -1)
    if rank < _TIER_RANK[want_tier] and want_tier not in p.get("attempted_tiers", []):
        return None
    return {k: v for k, v in p.items() if k not in ("ts", "tier")}


async def _resolve_and_probe(client: httpx.AsyncClient, src: str, item: dict,
                             tier: str = "standard", retained: dict | None = None) -> "dict | None":
    """Shared search/URL pipeline: official -> verify -> fallback -> downgrade."""
    if _explicit_trial(item) or any(m in str(item.get("title") or "") for m in _TRIAL_TITLE_MARKERS):
        return None
    tiers = _quality_tiers(tier)
    cached = _fresh_probe(item, tiers[0])
    if cached:
        return cached
    item.pop("_probe", None)
    item.update(verified=False, validation_status="unverified", completeness="unknown")
    identifier = parse_track_id(str(item.get("id") or ""))[1]
    title, artist = str(item.get("title") or ""), str(item.get("artist") or "")
    attempted = []
    best = None
    failures = _RESOLUTION_FAILURES.get()
    for t in tiers:
        # Once a better known tier is retained, lower tiers cannot improve it.
        if best and _TIER_RANK.get(best["actual_tier"], -1) >= _TIER_RANK[t]:
            break
        before = len(failures or [])
        try:
            if src == "kg":
                raw = await kg_resolve_url(client, item, identifier, t)
            elif src == "wy":
                raw = await wy_resolve_url(client, identifier, t)
            elif src == "mg":
                raw = await mg_resolve_url(client, identifier, t)
            else:
                raw = None
            result = await _verify_result(client, raw, report_transport=True)
        except Exception as exc:
            _record_failure(exc)
            result = None
        if result:
            result["resolver"] = "official"
            if not best or _TIER_RANK.get(result["actual_tier"], -1) > _TIER_RANK.get(best["actual_tier"], -1):
                best = result
                if retained is not None:
                    retained.update(best=best, attempted=attempted)
        if not best or (t != "standard" and _TIER_RANK.get(best["actual_tier"], -1) < _TIER_RANK[t]):
            result = await resolve_third_party(client, src, identifier, t, title, artist)
            if result and (not best or _TIER_RANK.get(result["actual_tier"], -1) > _TIER_RANK.get(best["actual_tier"], -1)):
                best = result
                if retained is not None:
                    retained.update(best=best, attempted=attempted)
        # Infrastructure-interrupted tiers must not be cached as exhausted.
        if len(failures or []) == before:
            attempted.append(t)
    if best:
        best["attempted_tiers"] = attempted
        item["_probe"] = dict(best, ts=time.time(), tier=best["actual_tier"])
        item.update(verified=True, validation_status="media_verified", completeness="unknown")
        _cache_put(item)
    return best


async def resolve_and_probe(client: httpx.AsyncClient, src: str, item: dict,
                            tier: str = "standard", *, budget: float | None = None) -> dict | None:
    failures: list[str] = []
    retained: dict = {}
    token = _RESOLUTION_FAILURES.set(failures)
    try:
        deadline = asyncio.timeout(budget)
        try:
            async with deadline:
                result = await _resolve_and_probe(client, src, item, tier, retained)
        except TimeoutError:
            # Only our resolution budget may return retained media. External
            # caller cancellation remains CancelledError and propagates after
            # the awaited resolver/upgrade has completed cancellation cleanup.
            if not deadline.expired() or not retained.get("best"):
                raise
            result = dict(retained["best"], attempted_tiers=list(retained["attempted"]))
            item["_probe"] = dict(result, ts=time.time(), tier=result["actual_tier"])
            item.update(verified=True, validation_status="media_verified", completeness="unknown")
            _cache_put(item)
        if result is None and failures:
            raise ChainTransportError("resolution infrastructure exhausted: " + failures[-1])
        return result
    finally:
        _RESOLUTION_FAILURES.reset(token)


def _chunks(seq: list, n: int) -> list:
    return [seq[i : i + n] for i in range(0, len(seq), n)]


async def _probe_candidates(
    client: httpx.AsyncClient, src: str, candidates: list[dict], limit: int
) -> list[dict]:
    """批量并发探活候选曲目，返回通过的条目（附带 verified/_probe 标记）。"""
    if limit <= 0:
        return []
    passed: list[dict] = []
    async def one(it):
        res = await resolve_and_probe(client, src, it)
        if res:
            it.update(verified=True, validation_status="media_verified", completeness="unknown",
                      ext=res.get("ext") or it.get("ext") or "mp3",
                      file_size=res.get("file_size") or 0)
            _cache_put(it)
            _publish(it)
            return it
        return None

    for batch in _chunks(candidates, 6):
        tasks = [asyncio.create_task(one(it)) for it in batch]
        try:
            for done in asyncio.as_completed(tasks):
                try:
                    it = await done
                except Exception:
                    continue
                if it:
                    passed.append(it)
                    if len(passed) >= limit:
                        return passed
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    return passed


# ------------------------------------------------------------------ QQ tx ---

TX_SEARCH_BODY = {
    "req_1": {
        "method": "DoSearchForQQMusicDesktop",
        "module": "music.search.SearchCgiService",
        "param": {"search_type": 0, "query": "", "page_num": 1, "num_per_page": 20},
    }
}


async def tx_search(client: httpx.AsyncClient, keyword: str, limit: int) -> list[dict]:
    """QQ 音乐官方免登录搜索（musicu.fcg）。直链无官方免登录路径，全部经第三方链路探活。"""
    fetch_size = max(limit * 2, 20)
    body = {"req_1": {**TX_SEARCH_BODY["req_1"], "param": {**TX_SEARCH_BODY["req_1"]["param"], "query": keyword, "num_per_page": fetch_size}}}
    try:
        r = await client.post(
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
            json=body,
            headers={"User-Agent": UA_PC, "Referer": "https://y.qq.com/"},
        )
        data = r.json() or {}
    except Exception:  # noqa: BLE001
        raise
    songs = ((((data.get("req_1") or {}).get("data") or {}).get("body") or {}).get("song") or {}).get("list") or []
    candidates = []
    for it in songs:
        if not isinstance(it, dict) or _explicit_trial(it):
            continue
        mid = str(it.get("mid") or it.get("songmid") or "")
        title = str(it.get("title") or "")
        if not mid or not title:
            continue
        if any(marker in title for marker in _TRIAL_TITLE_MARKERS):
            continue
        singers = it.get("singer") or []
        album = it.get("album") or {}
        pay = it.get("pay") or {}
        candidates.append(
            {
                "id": f"lx:tx:{mid}",
                "lx_source": "tx",
                "title": title,
                "artist": " / ".join(str(s.get("name") or "") for s in singers if isinstance(s, dict)),
                "album": str(album.get("name") or ""),
                "duration_s": int(it.get("interval") or 0),
                "ext": "mp3",
                "cover_url": (
                    f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album.get('mid')}.jpg"
                    if album.get("mid")
                    else ""
                ),
                "file_size": 0,
                "lyric": "",
                "songmid": mid,
                "pay_type": int(pay.get("pay_play") or 0),
            }
        )
    # tx 直链当前无存活链路 → 探活全部失败返回空；链路注册表新增 tx 链路后自动恢复
    return await _probe_candidates(client, "tx", candidates, limit)


async def tx_resolve_url(
    client: httpx.AsyncClient, item: "dict | None", identifier: str, tier: str
) -> "dict | None":
    return await resolve_and_probe(
        client, "tx", item or {"id": f"lx:tx:{identifier}"}, tier
    )


async def tx_resolve_lyric(client: httpx.AsyncClient, identifier: str) -> str:
    import html as _html

    try:
        r = await client.get(
            "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
            params={
                "songmid": identifier,
                "g_tk": "5381",
                "loginUin": "0",
                "hostUin": "0",
                "format": "json",
                "inCharset": "utf8",
                "outCharset": "utf-8",
                "notice": "0",
                "platform": "yqq",
                "needNewCode": "0",
            },
            headers={"User-Agent": UA_PC, "Referer": "https://y.qq.com/portal/player.html"},
        )
        data = _lenient_json(r, f"tx lyric {identifier}")
        content = str((data or {}).get("lyric") or "")
        if not content:
            return ""
        return _html.unescape(base64.b64decode(content).decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------------ 酷我 kw ---

def _lenient_pydict(resp: httpx.Response, tag: str = "") -> "dict | None":
    """酷我 r.s 老接口返回 Python 字面量风格（单引号），宽容解析。"""
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        pass
    import ast as _ast

    text = (resp.text or "").strip()
    if text.startswith("{"):
        try:
            return _ast.literal_eval(text)
        except Exception as e:  # noqa: BLE001
            logger.warning("%s: py-dict parse failed: %s", tag, e)
    return None


def _title_relevance(title: str, keyword: str) -> int:
    """搜索候选排序：标题与关键词越接近越靠前（r.s 相关度常把原版排在伴奏/翻唱之后）。"""
    import re as _re

    def norm(s: str) -> str:
        s = _re.sub(r"\([^)]*\)|（[^）]*）|\[[^\]]*\]", "", s or "")
        return _re.sub(r"[\s\-—·・&]", "", s).lower()

    t, k = norm(title), norm(keyword)
    if not k:
        return 3
    if t == k:
        return 0
    if k.startswith(t) or t.startswith(k):
        return 1  # 原版：title 即关键词主体（"晴天" ⊂ "晴天 周杰伦"）
    if k in t or t in k:
        return 2
    return 3


async def kw_search(client: httpx.AsyncClient, keyword: str, limit: int) -> list[dict]:
    """酷我官方免登录搜索（r.s 老接口）。直链经长青 kw 链路（无损 FLAC，2026-09 实测存活）探活。"""
    import html as _html

    # r.s 相关度常把原版排在伴奏/翻唱之后，抓取窗口放大再按标题相关度重排
    fetch_size = max(limit * 4, 60)
    r = await client.get(
        "http://search.kuwo.cn/r.s",
        params={
            "all": keyword,
            "ft": "music",
            "itemset": "web_2013",
            "client": "kt",
            "pn": 0,
            "rn": fetch_size,
            "rformat": "json",
            "encoding": "utf8",
        },
        headers={"User-Agent": UA_PC, "Referer": "http://www.kuwo.cn/"},
    )
    r.raise_for_status()
    raw = _lenient_pydict(r, "kw r.s") or {}
    candidates = []
    for it in raw.get("abslist") or []:
        if not isinstance(it, dict) or _explicit_trial(it):
            continue
        rid = str(it.get("MUSICRID") or "").replace("MUSIC_", "").strip()
        if not rid.isdigit():
            continue
        payinfo = it.get("payInfo") or {}
        # cannotOnlinePlay=1 表示无在线播放版权，真不可播，直接剔除
        if str(payinfo.get("cannotOnlinePlay") or "0") == "1":
            continue
        title = _html.unescape(str(it.get("SONGNAME") or "")).replace("\xa0", " ").strip()
        if not title or any(marker in title for marker in _TRIAL_TITLE_MARKERS):
            continue
        cover_short = str(it.get("web_albumpic_short") or "").strip()
        candidates.append(
            {
                "id": f"lx:kw:{rid}",
                "lx_source": "kw",
                "title": title,
                "artist": _html.unescape(str(it.get("ARTIST") or "")).replace("\xa0", " ").strip(),
                "album": _html.unescape(str(it.get("ALBUM") or "")).replace("\xa0", " ").strip(),
                "duration_s": int(it.get("DURATION") or 0),
                "ext": "mp3",
                "cover_url": f"https://img1.kuwo.cn/star/albumcover/{cover_short}" if cover_short else "",
                "file_size": 0,
                "lyric": "",
                "rid": rid,
                "pay_type": int(it.get("PAY") or 0),
            }
        )
    # 标题相关度排序：原版（title≈keyword）优先于伴奏/DJ/翻唱版本
    candidates.sort(key=lambda c: _title_relevance(c["title"], keyword))
    return await _probe_candidates(client, "kw", candidates, limit)


async def kw_resolve_url(
    client: httpx.AsyncClient, item: "dict | None", identifier: str, tier: str
) -> "dict | None":
    return await resolve_and_probe(
        client, "kw", item or {"id": f"lx:kw:{identifier}"}, tier
    )


async def kw_resolve_lyric(client: httpx.AsyncClient, item: dict) -> str:
    # 酷我免登录歌词接口已全部失效（2026-09 实测），暂返回空
    return ""


# ------------------------------------------------------------ 免登录榜单推荐 ---

# 实测存活的免登录榜单（2026-09）：kg 移动端 TOP500 / kw kbang 飙升榜 / wy 新歌速递
_KG_RANK_ID = 8888  # m.kugou.com/rank/info 必须带 page 参数，否则 songs.list 为空
_KW_BANG_ID = 93    # kbangserver.kuwo.cn 免签老接口


async def kg_chart_songs(client: httpx.AsyncClient, limit: int) -> list[dict]:
    """酷狗移动端 TOP500 榜单（免登录）。免费曲直接收录，付费曲探活通过才返回。"""
    try:
        r = await client.get(
            "http://m.kugou.com/rank/info/",
            params={"rankid": _KG_RANK_ID, "page": 1, "json": "true"},
            headers={"User-Agent": UA_MOBILE},
            timeout=10.0,
        )
        r.raise_for_status()
        rows = ((r.json() or {}).get("songs") or {}).get("list") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("kg chart rank %s failed: %s", _KG_RANK_ID, e)
        return []
    items: list[dict] = []
    vip_candidates: list[dict] = []
    for it in rows:
        if not isinstance(it, dict) or _explicit_trial(it):
            continue
        fhash = str(it.get("hash") or "")
        if not fhash:
            continue
        # 榜单曲目几乎全部带 pay_type/fail_process VIP 标记，但实测匿名 playInfo
        # 仍能解析出完整时长直链（2026-09 验证 3.6MB/225s），故不按标记硬过滤，
        # 一律以探活结果为准（与搜索侧"可播性验证取代收费过滤"同一原则）
        if int(it.get("is_free_part") or 0) != 0:
            continue
        singer = " / ".join(
            str(a.get("author_name") or "")
            for a in (it.get("authors") or [])
            if isinstance(a, dict) and a.get("author_name")
        )
        title = str(it.get("songname") or it.get("filename") or "").replace(f"{singer} - ", "")
        if not title or any(marker in title for marker in _TRIAL_TITLE_MARKERS):
            continue
        pay_type = int(it.get("pay_type") or 0)
        is_vip = pay_type != 0 or int(it.get("pkg_price") or 0) != 0 or int(it.get("price") or 0) != 0
        sq = str(it.get("sqhash") or "")
        item = {
            "id": f"lx:kg:{fhash}",
            "lx_source": "kg",
            "title": title,
            "artist": singer,
            "album": "",  # rank 接口不返回专辑名
            "duration_s": int(it.get("duration") or 0),  # rank 接口 duration 单位为秒
            "ext": "flac" if sq else "mp3",
            "cover_url": str(it.get("album_sizable_cover") or "").replace("{size}", "480"),
            "file_size": int(it.get("sqfilesize") or it.get("320filesize") or 0) or 0,
            "lyric": "",
            "hash": fhash,
            "hash_hq": str(it.get("320hash") or ""),
            "hash_sq": sq,
            "mixsongid": str(it.get("album_audio_id") or ""),
            "pay_type": pay_type,
        }
        if is_vip:
            vip_candidates.append(item)
        else:
            item.update(verified=False, validation_status="unverified", completeness="unknown")
            _cache_put(item)
            items.append(item)
            _publish(item)
    if vip_candidates and len(items) < limit:
        items.extend(await _probe_candidates(client, "kg", vip_candidates, limit - len(items)))
    return items[:limit]


async def kw_chart_songs(client: httpx.AsyncClient, limit: int) -> list[dict]:
    """酷我 kbang 免签飙升榜（老接口字段较旧）。酷我直链依赖第三方链路，全部探活。"""
    import html as _html

    try:
        r = await client.get(
            "http://kbangserver.kuwo.cn/ksong.s",
            params={
                "from": "pc",
                "fmt": "json",
                "pn": 0,
                "rn": max(limit * 2, 40),
                "id": _KW_BANG_ID,
                "nc": 1,
            },
            headers={"User-Agent": UA_PC},
            timeout=10.0,
        )
        r.raise_for_status()
        rows = (r.json() or {}).get("musiclist") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("kw bang %s failed: %s", _KW_BANG_ID, e)
        return []
    candidates: list[dict] = []
    for it in rows:
        if not isinstance(it, dict):
            continue
        rid = str(it.get("id") or "")
        if not rid.isdigit():
            continue
        payinfo = it.get("payInfo") or {}
        # cannotOnlinePlay=1 无在线版权；listen_fragment=1 仅有试听片段，均真不可播
        if str(payinfo.get("cannotOnlinePlay") or "0") == "1":
            continue
        if str(payinfo.get("listen_fragment") or "0") == "1":
            continue
        title = _html.unescape(str(it.get("name") or "")).replace("\xa0", " ").strip()
        if not title or any(marker in title for marker in _TRIAL_TITLE_MARKERS):
            continue
        formats = str(it.get("formats") or "")
        fee_type = payinfo.get("feeType") or {}
        candidates.append(
            {
                "id": f"lx:kw:{rid}",
                "lx_source": "kw",
                "title": title,
                "artist": _html.unescape(str(it.get("artist") or "")).replace("\xa0", " ").strip(),
                "album": _html.unescape(str(it.get("album") or "")).replace("\xa0", " ").strip(),
                "duration_s": int(float(it.get("song_duration") or 0)),
                "ext": "flac" if "ALFLAC" in formats else "mp3",
                "cover_url": "",
                "file_size": 0,
                "lyric": "",
                "rid": rid,
                "pay_type": 1 if str(fee_type.get("song") or "0") != "0" else 0,
            }
        )
    return await _probe_candidates(client, "kw", candidates, limit)


async def wy_chart_songs(client: httpx.AsyncClient, limit: int) -> list[dict]:
    """网易新歌速递（免登录）。fee∉(0,8) 的付费曲转探活验证。"""
    try:
        r = await client.get(
            "https://music.163.com/api/personalized/newsong",
            params={"limit": max(limit * 2, 30)},
            headers={
                "User-Agent": UA_PC,
                "Referer": "https://music.163.com/",
                "Cookie": "os=pc; appver=9.1.15",
            },
            timeout=10.0,
        )
        r.raise_for_status()
        rows = (r.json() or {}).get("result") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("wy newsong failed: %s", e)
        return []
    items: list[dict] = []
    vip_candidates: list[dict] = []
    for it in rows:
        if not isinstance(it, dict):
            continue
        song = it.get("song") if isinstance(it.get("song"), dict) else {}
        sid = str(song.get("id") or it.get("id") or "")
        if not sid:
            continue
        name = str(song.get("name") or it.get("name") or "")
        if not name or any(marker in name for marker in _TRIAL_TITLE_MARKERS):
            continue
        artists = song.get("artists") or []
        artist = " / ".join(str(a.get("name") or "") for a in artists if isinstance(a, dict))
        album = song.get("album") or {}
        fee = int(song.get("fee") or 0)
        item = {
            "id": f"lx:wy:{sid}",
            "lx_source": "wy",
            "title": name,
            "artist": artist,
            "album": str(album.get("name") or "") if isinstance(album, dict) else "",
            "duration_s": int(song.get("duration") or 0) / 1000.0,
            "ext": "mp3",
            "cover_url": str(album.get("picUrl") or "") if isinstance(album, dict) else "",
            "file_size": 0,
            "lyric": "",
            "song_id": sid,
            "fee": fee,
        }
        if fee not in (0, 8):
            vip_candidates.append(item)
        else:
            item.update(verified=False, validation_status="unverified", completeness="unknown")
            _cache_put(item)
            items.append(item)
            _publish(item)
    if vip_candidates and len(items) < limit:
        items.extend(await _probe_candidates(client, "wy", vip_candidates, limit - len(items)))
    return items[:limit]


_CHARTERS: dict[str, Any] = {"kg": kg_chart_songs, "kw": kw_chart_songs, "wy": wy_chart_songs}


# --------------------------------------------------------------------- app ---

_SEARCHERS = {"kg": kg_search, "wy": wy_search, "mg": mg_search, "tx": tx_search, "kw": kw_search}


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
        "third_party": CONF["third_party"],
        "chains": chain_health_snapshot(),
        "capabilities": source_capabilities(),
        "charts": sorted(_CHARTERS),
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
    partials = {}
    errors: dict[str, str] = {}
    capabilities = source_capabilities()

    async def run_source(src):
        token = _SEARCH_PARTIAL.set(partials[src])
        try:
            return await _SEARCHERS[src](client, kw, limit)
        finally:
            _SEARCH_PARTIAL.reset(token)

    for src in dict.fromkeys(wanted):
        if src not in _SEARCHERS:
            continue
        if not capabilities[src]["playback_available"]:
            errors[src] = capabilities[src]["reason"]
            continue
        partials[src] = []
        tasks[src] = asyncio.create_task(run_source(src))
    items: list[dict] = []
    try:
        if tasks:
            # One shared budget, below the proxy's default 15s timeout. Never
            # multiply the timeout by source count or wait in insertion order.
            await asyncio.wait(tasks.values(), timeout=max(0.001, min(CONF["search_timeout"], 13.0)))
    finally:
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
    for src, task in tasks.items():
        if task.cancelled():
            errors[src] = "search deadline exceeded"
            results = partials[src]
        elif task.exception() is not None:
            errors[src] = str(task.exception()) or type(task.exception()).__name__
            results = partials[src]
        else:
            results = task.result()
        seen = set()
        for item in results:
            if item.get("id") not in seen:
                items.append(item)
                seen.add(item.get("id"))
                if len(seen) >= limit:
                    break
    _STATS["errors"] += len(errors)
    return {"ok": True, "items": items, "errors": errors, "stats": dict(_STATS),
            "capabilities": capabilities}


@app.get("/api/v1/recommend")
async def recommend_charts(
    limit: int = Query(0),
    sources: str = Query(""),
):
    """免登录榜单推荐：kg TOP500 / kw 飙升榜 / wy 新歌速递，按源顺序聚合凑满 limit。"""
    if limit <= 0:
        limit = CONF["limit_per_source"]
    wanted_raw = [s.strip() for s in (sources or "").split(",") if s.strip()]
    wanted = [normalize_source(s) for s in wanted_raw]
    wanted = [s for s in wanted if s in _CHARTERS] or [s for s in CONF["sources"] if s in _CHARTERS]

    client = get_http(app)
    capabilities = source_capabilities()
    items: list[dict] = []
    errors: dict[str, str] = {}
    for src in dict.fromkeys(wanted):
        if not capabilities.get(src, {}).get("playback_available"):
            errors[src] = capabilities.get(src, {}).get("reason") or "playback unavailable"
            continue
        try:
            chunk = await _CHARTERS[src](client, limit)
        except Exception as e:  # noqa: BLE001
            errors[src] = str(e) or type(e).__name__
            continue
        items.extend(chunk)
        if len(items) >= limit:
            break
    _STATS["errors"] += len(errors)
    return {"ok": True, "items": items[:limit], "errors": errors, "charts": sorted(_CHARTERS)}


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
    canonical_id = f"lx:{src}:{identifier}"
    cached = _cache_get(canonical_id) or {"id": canonical_id, "lx_source": src}
    client = get_http(app)
    try:
        result = await resolve_and_probe(
            client, src, cached, quality, budget=CONF["url_timeout"]
        )
    except Exception as e:  # noqa: BLE001
        _STATS["errors"] += 1
        logger.warning("lx url resolve %s failed: %s", track_id, e)
        return _err(f"resolve failed: {e}", 502)

    if not result:
        return _err(source_capabilities()[src]["reason"] or "no playable url", 404)
    return {"ok": True, "data": {"id": track_id, "quality": quality, **{k: v for k, v in result.items() if k != "probed"}}}


@app.get("/api/v1/track/info")
async def track_info(id: str = Query("", alias="id"), guid: str = Query("", alias="guid")):
    track_id = (id or guid or "").strip()
    src, identifier = parse_track_id(track_id)
    if not src:
        return _err(f"invalid track id: {track_id}", 400)
    cached = _cache_get(f"lx:{src}:{identifier}")
    if cached:
        return {"ok": True, "data": cached}
    return {"ok": True, "data": {"id": track_id, "source": "lx", "lx_source": src, "title": "", "artist": "", "album": "", "duration_s": 0, "ext": "mp3", "file_size": 0, "cover_url": "", "lyric": ""}}


@app.get("/api/v1/track/lyric")
async def track_lyric(id: str = Query("", alias="id"), guid: str = Query("", alias="guid")):
    track_id = (id or guid or "").strip()
    src, identifier = parse_track_id(track_id)
    if not src:
        return _err(f"invalid track id: {track_id}", 400)
    cached = _cache_get(f"lx:{src}:{identifier}") or {}
    client = get_http(app)
    text = ""
    try:
        if src == "kg":
            text = await kg_resolve_lyric(client, cached or {"hash": identifier, "title": "", "artist": "", "duration_s": 0})
        elif src == "wy":
            text = await wy_resolve_lyric(client, identifier)
        elif src == "mg":
            text = await mg_resolve_lyric(client, cached)
        elif src == "tx":
            text = await tx_resolve_lyric(client, identifier)
        elif src == "kw":
            text = await kw_resolve_lyric(client, cached or {})
    except Exception as e:  # noqa: BLE001
        logger.warning("lx lyric %s failed: %s", track_id, e)
    return {"ok": True, "data": {"id": track_id, "lyric": text or ""}}
