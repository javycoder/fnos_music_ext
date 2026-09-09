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

SERVICE_VERSION = "1.1.0"

CONF = {
    "sources": [s.strip() for s in os.environ.get("LX_SOURCES", "kg,wy,mg,tx,kw").split(",") if s.strip()],
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

_EAPI_KEY = b"#14ljk_!\\]&0U<'("

# id -> {"item": {...}, "ts": float}
_SONG_CACHE: dict[str, dict] = {}
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
        if not isinstance(it, dict):
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
            _cache_put(item)
            items.append(item)
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
        data = _lenient_json(r, f"kg playInfo {fhash}")
        if isinstance(data, dict) and data.get("errcode") == 0 and data.get("url"):
            ext = str(data.get("extName") or "mp3").lower().lstrip(".") or "mp3"
            return {
                "url": str(data["url"]),
                "ext": ext,
                "file_size": int(data.get("fileSize") or 0) or 0,
                "br": int(data.get("bitRate") or 128) * 1000 if int(data.get("bitRate") or 0) < 1000 else int(data.get("bitRate") or 128000),
                "headers": dict(KG_HEADERS),
            }
    except Exception as e:  # noqa: BLE001
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
            data = _lenient_json(r, f"kg trackercdn {fhash}")
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
        if not isinstance(it, dict):
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
            _cache_put(item)
            items.append(item)
    # VIP 候选批量探活，通过（verified）才补进结果
    if vip_candidates and len(items) < limit:
        want = min(len(vip_candidates), limit - len(items) + limit // 2)
        items.extend(await _probe_candidates(client, "wy", vip_candidates[:want], limit - len(items)))
    return items[: limit + limit // 2]


async def wy_resolve_url(client: httpx.AsyncClient, identifier: str, tier: str) -> dict | None:
    # 1. 优先尝试官方 eapi 高音质解析（需 pycryptodome）
    if HAS_CRYPTO:
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
                    timeout=8.0,
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

    # 2. 备用兜底：网易 outer/url 免登录直链（重定向至真实音频 CDN，先做 Range 探测排除 404 HTML）
    try:
        outer_url = f"https://music.163.com/song/media/outer/url?id={identifier}"
        probe_headers = dict(WY_HEADERS)
        probe_headers["Range"] = "bytes=0-1"
        r_probe = await client.get(
            outer_url,
            headers=probe_headers,
            timeout=8.0,
        )
        ct = (r_probe.headers.get("content-type") or "").lower()
        if r_probe.status_code in (200, 206) and "audio" in ct:
            final_url = str(r_probe.url)
            cl = int(r_probe.headers.get("content-length") or 0)
            return {
                "url": final_url,
                "ext": "mp3",
                "file_size": cl,
                "br": 128000,
                "headers": dict(WY_HEADERS),
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("wy outer/url fallback %s failed: %s", identifier, e)

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

    async def _probe_one(it: dict) -> dict | None:
        if not isinstance(it, dict):
            return None
        cid = str(it.get("copyrightId") or it.get("id") or "")
        if not cid:
            return None
        title = str(it.get("songName") or "")
        if any(marker in title for marker in _TRIAL_TITLE_MARKERS):
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
        # 可播性验证：官方解析失败回退溯音咪咕链路，再 Range 探活（含试听碎片防护）
        res = await resolve_and_probe(client, "mg", item)
        if not res:
            return None
        item["verified"] = True
        item["_probe"] = dict(res, ts=time.time(), tier="standard")
        item["file_size"] = res.get("file_size") or 0
        _cache_put(item)
        return item

    candidates = [it for it in raw if isinstance(it, dict)]
    probed = await asyncio.gather(*[_probe_one(it) for it in candidates[:fetch_size]], return_exceptions=True)
    items = []
    for res in probed:
        if isinstance(res, dict):
            items.append(res)
            if len(items) >= limit:
                break
    return items


async def mg_resolve_url(client: httpx.AsyncClient, identifier: str, tier: str) -> dict | None:
    try:
        r = await client.get(
            "https://music.migu.cn/v3/api/music/audio/player_get_song_info",
            params={"copyrightId": identifier, "resourceType": "E", "resourceLevel": tier},
            headers={"User-Agent": UA_PC, "Referer": "https://music.migu.cn/"},
            timeout=8.0,
        )
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
            "ext": "flac" if ext in ("flac", "zq", "sq") else "mp3",
            "file_size": int(data.get("fileSize") or data.get("overdue_size") or 0) or 0,
            "br": int(data.get("bitRate") or 0) or 0,
            "headers": dict(MG_HEADERS),
        }
    except Exception as e:  # noqa: BLE001
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
# 使用第三方链路解析的曲目一律经 Range 探活验证后才对外返回（"搜得到必能播"），
# 对应条目带 verified=true 标记，代理侧据此跳过收费元数据拦截。


def _content_total_size(r: httpx.Response) -> int:
    cr = r.headers.get("content-range") or ""
    if "/" in cr:
        tail = cr.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            return int(tail)
    cl = r.headers.get("content-length") or ""
    return int(cl) if cl.isdigit() else 0


async def probe_url(
    client: httpx.AsyncClient, url: str, headers: "dict | None" = None
) -> "tuple[bool, str, str, int]":
    """直链探活：Range: bytes=0-1 请求，要求 200/206 且非 HTML。

    返回 (ok, final_url, content_type, total_size)。部分 CDN（如酷我）的
    Content-Type 是 application/octet-stream，因此只排除 text/html。
    """
    h = {"User-Agent": UA_PC, "Range": "bytes=0-1"}
    if headers:
        for k, v in headers.items():
            if k.lower() in ("user-agent", "referer"):
                h[k] = v
    try:
        r = await client.get(url, headers=h, timeout=CONF["probe_timeout"])
    except Exception:  # noqa: BLE001
        return False, url, "", 0
    ct = (r.headers.get("content-type") or "").lower()
    ok = r.status_code in (200, 206) and "text/html" not in ct
    return ok, str(r.url), ct, _content_total_size(r) if ok else 0


# 链路熔断器：连续失败达阈值后暂停该链路一段时间，避免每次搜索白等超时
_CHAIN_FAIL_THRESHOLD = 3
_CHAIN_OPEN_SECONDS = 600
_CHAIN_HEALTH: dict[str, dict] = {}


def _chain_available(name: str) -> bool:
    h = _CHAIN_HEALTH.get(name)
    return not (h and h.get("open_until", 0) > time.time())


def _chain_report(name: str, ok: bool) -> None:
    h = _CHAIN_HEALTH.setdefault(name, {"fails": 0, "open_until": 0.0, "breaks": 0})
    if ok:
        h["fails"] = 0
        h["open_until"] = 0.0
        return
    h["fails"] = int(h.get("fails") or 0) + 1
    if h["fails"] >= _CHAIN_FAIL_THRESHOLD:
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
    return (not na or not nb) or na in nb or nb in na


_TIER_TO_NETESE_LEVEL = {"lossless": "lossless", "high": "exhigh", "standard": "standard"}


async def _chain_changqing_kw(client: httpx.AsyncClient, ctx: dict) -> "dict | None":
    """长青 kw：URL 模板 → 302 → 酷我 CDN 直链（实测无损 FLAC）。"""
    from urllib.parse import quote

    level = _TIER_TO_NETESE_LEVEL.get(ctx["tier"], "standard")
    url = f"https://musicapi.haitangw.net/music/kw.php?type=mp3&id={quote(str(ctx['identifier']))}&level={level}"
    ok, final, ct, size = await probe_url(client, url)
    if not ok:
        return None
    lower = (final.split("?")[0] + " " + ct).lower()
    return {
        "url": final,
        "ext": "flac" if "flac" in lower else "mp3",
        "file_size": size,
        "br": 740000 if "flac" in lower else 128000,
        "headers": dict(KW_HEADERS),
    }


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
        data = _lenient_json(r, "suyin mg")
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or int(data.get("code") or 0) != 200:
        return None
    if ctx.get("title") and not _fuzzy_contains(str(data.get("title") or ""), str(ctx["title"])):
        return None
    if ctx.get("artist") and not _fuzzy_contains(str(data.get("singer") or ""), str(ctx["artist"])):
        return None
    url = str(data.get("music_url") or "")
    if not url.startswith(("http://", "https://")):
        return None
    ok, final, ct, size = await probe_url(client, url)
    if not ok:
        return None
    return {
        "url": final,
        "ext": "mp3",
        "file_size": size or int(data.get("size") or 0),
        "br": 320000,
        "headers": dict(MG_HEADERS),
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
        if not _chain_available(link["name"]):
            continue
        ctx = {"identifier": identifier, "tier": tier, "title": title, "artist": artist}
        try:
            result = await asyncio.wait_for(
                link["fn"](client, ctx), timeout=CONF["resolver_timeout"] + CONF["probe_timeout"]
            )
        except Exception:  # noqa: BLE001
            result = None
        if result and result.get("url"):
            _chain_report(link["name"], True)
            return result
        _chain_report(link["name"], False)
    return None


def chain_health_snapshot() -> dict:
    now = time.time()
    return {
        name: {
            "fails": h.get("fails", 0),
            "open": bool(h.get("open_until", 0) > now),
            "breaks": h.get("breaks", 0),
        }
        for name, h in _CHAIN_HEALTH.items()
    }


# ------------------------------------------------------------ 可播性验证 ---

_TRIAL_TITLE_MARKERS = ("(试听)", "（试听）", "试听片段", "片段试听", "试听版")


_TIER_RANK = {"standard": 0, "high": 1, "lossless": 2}


def _fresh_probe(item: "dict | None", want_tier: str = "standard") -> "dict | None":
    """探活缓存未过期且音质不低于请求档位时直接复用（CDN 直链有时效，过期需重新解析）。"""
    if not isinstance(item, dict):
        return None
    p = item.get("_probe")
    if not (isinstance(p, dict) and p.get("url")):
        return None
    if time.time() - p.get("ts", 0) >= CONF["probe_fresh_s"]:
        return None
    if _TIER_RANK.get(str(p.get("tier") or "standard"), 0) < _TIER_RANK.get(want_tier, 0):
        return None
    return {k: v for k, v in p.items() if k not in ("ts", "tier")}


async def resolve_and_probe(client: httpx.AsyncClient, src: str, item: dict, tier: str = "standard") -> "dict | None":
    """搜索期可播性验证：官方解析 → 第三方链路 → Range 探活 + 试听碎片防护。

    返回直链信息（已探活）或 None（不可播）。
    """
    identifier = str(item.get("id") or "").split(":", 2)[-1]
    title = str(item.get("title") or "")
    artist = str(item.get("artist") or "")
    result = None
    try:
        if src == "kg":
            for t in _quality_tiers(tier):
                result = await kg_resolve_url(client, item, identifier, t)
                if result:
                    break
        elif src == "wy":
            result = await wy_resolve_url(client, identifier, tier)
        elif src == "mg":
            result = await mg_resolve_url(client, identifier, "E")
        elif src == "tx":
            result = await resolve_third_party(client, "tx", identifier, tier, title, artist)
        elif src == "kw":
            cached_probe = _fresh_probe(item)
            if cached_probe:
                return cached_probe
            result = await resolve_third_party(client, "kw", identifier, tier, title, artist)
        else:
            return None
    except Exception:  # noqa: BLE001
        result = None
    if not result or not result.get("url"):
        return None

    if not result.get("probed"):
        ok, final, ct, size = await probe_url(client, result["url"], result.get("headers"))
        if not ok:
            return None
        result["url"] = final
        result["file_size"] = size or result.get("file_size") or 0
        result["probed"] = True

    # 试听碎片防护：实际文件明显小于该时长应有的最小体积（128kbps ≈ 16KB/s）时判为片段
    duration_s = float(item.get("duration_s") or 0)
    size = int(result.get("file_size") or 0)
    if duration_s > 60 and 0 < size < duration_s * 16000 * 0.5:
        return None
    return result


def _chunks(seq: list, n: int) -> list:
    return [seq[i : i + n] for i in range(0, len(seq), n)]


async def _probe_candidates(
    client: httpx.AsyncClient, src: str, candidates: list[dict], limit: int
) -> list[dict]:
    """批量并发探活候选曲目，返回通过的条目（附带 verified/_probe 标记）。"""
    passed: list[dict] = []
    for batch in _chunks(candidates, 6):
        results = await asyncio.gather(
            *(resolve_and_probe(client, src, it) for it in batch), return_exceptions=True
        )
        for it, res in zip(batch, results):
            if isinstance(res, dict):
                it["verified"] = True
                it["_probe"] = dict(res, ts=time.time(), tier="standard")
                it["ext"] = res.get("ext") or it.get("ext") or "mp3"
                if res.get("file_size"):
                    it["file_size"] = res["file_size"]
                _cache_put(it)
                passed.append(it)
                if len(passed) >= limit:
                    return passed
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
        if not isinstance(it, dict):
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
    cached = _fresh_probe(item, tier)
    if cached:
        return cached
    return await resolve_third_party(
        client, "tx", identifier, tier, str((item or {}).get("title") or ""), str((item or {}).get("artist") or "")
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
        if not isinstance(it, dict):
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
    cached = _fresh_probe(item, tier)
    if cached:
        return cached
    return await resolve_third_party(
        client, "kw", identifier, tier, str((item or {}).get("title") or ""), str((item or {}).get("artist") or "")
    )


async def kw_resolve_lyric(client: httpx.AsyncClient, item: dict) -> str:
    # 酷我免登录歌词接口已全部失效（2026-09 实测），暂返回空
    return ""


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
            result = _fresh_probe(cached, _quality_tiers(quality)[0])
            if not result:
                for tier in _quality_tiers(quality):
                    result = await kg_resolve_url(client, cached, identifier, tier)
                    if result:
                        break
                if not result:
                    result = await resolve_third_party(
                        client, "kg", identifier, _quality_tiers(quality)[0],
                        str((cached or {}).get("title") or ""), str((cached or {}).get("artist") or ""),
                    )
        elif src == "wy":
            result = await wy_resolve_url(client, identifier, quality)
            if not result:
                result = _fresh_probe(cached, _quality_tiers(quality)[0]) or await resolve_third_party(
                    client, "wy", identifier, _quality_tiers(quality)[0],
                    str((cached or {}).get("title") or ""), str((cached or {}).get("artist") or ""),
                )
        elif src == "mg":
            result = await mg_resolve_url(client, identifier, "E")
            if not result:
                result = _fresh_probe(cached, _quality_tiers(quality)[0]) or await resolve_third_party(
                    client, "mg", identifier, _quality_tiers(quality)[0],
                    str((cached or {}).get("title") or ""), str((cached or {}).get("artist") or ""),
                )
        elif src == "tx":
            result = await tx_resolve_url(client, cached, identifier, _quality_tiers(quality)[0])
        elif src == "kw":
            result = await kw_resolve_url(client, cached, identifier, _quality_tiers(quality)[0])
        else:
            return _err(f"unsupported source: {src}", 400)
    except Exception as e:  # noqa: BLE001
        _STATS["errors"] += 1
        logger.warning("lx url resolve %s failed: %s", track_id, e)
        return _err(f"resolve failed: {e}", 502)

    if not result:
        return _err("no playable url", 404)
    return {"ok": True, "data": {"id": track_id, "quality": quality, **{k: v for k, v in result.items() if k != "probed"}}}


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
        elif src == "tx":
            text = await tx_resolve_lyric(client, identifier)
        elif src == "kw":
            text = await kw_resolve_lyric(client, cached or {})
    except Exception as e:  # noqa: BLE001
        logger.warning("lx lyric %s failed: %s", track_id, e)
    return {"ok": True, "data": {"id": track_id, "lyric": text or ""}}
