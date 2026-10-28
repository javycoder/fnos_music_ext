"""Optional NEMbox internals for batch detail / lyrics (NetEase-MusicBox)."""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

try:
    import httpx
except ImportError:
    httpx = None

try:
    import requests
except ImportError:
    requests = None

logger = logging.getLogger("musicbox_service.netease_ext")

_api_lock = threading.Lock()
_api_instance = None

# 登录态探测结果缓存：(结果, 探测时刻单调钟)。失败短缓存、成功长缓存，
# 过期时强制重建实例重读磁盘 cookie 再下结论，避免常驻实例揣着过期 cookie。
_login_state_lock = threading.Lock()
_login_state: "tuple[bool, float] | None" = None
_LOGIN_TTL_OK_S = 300.0
_LOGIN_TTL_FAIL_S = 60.0

# 进程内取链结果缓存：(item, 过期时刻单调钟)。网易直链自带过期时间，只做短缓存。
_url_cache_lock = threading.Lock()
_url_cache: "dict[tuple[int, str], tuple[dict, float]]" = {}
_URL_CACHE_TTL_OK_S = 600.0
_URL_CACHE_TTL_FAIL_S = 60.0
_URL_CACHE_MAX = 4096


def _get_api_locked():
    """调用方必须已持有 _api_lock：取实例 + 用实例在同一次持锁内完成。"""
    global _api_instance
    if _api_instance is None:
        from runner import ensure_xdg_dirs

        ensure_xdg_dirs()
        from NEMbox.api import NetEase

        _api_instance = NetEase()
    return _api_instance


def _get_api():
    with _api_lock:
        return _get_api_locked()


def reset_api(reason: str = "") -> None:
    """丢弃常驻 NetEase 实例与相关缓存，下次 _get_api() 重建并重读磁盘 cookie。

    登录由 CLI 子进程完成并写盘，而 NEMbox 的 NetEase 只在构造时读一次盘；
    磁盘 cookie 变化（扫码成功等）后不重建实例，服务进程内就永远是旧登录态。
    """
    global _api_instance, _login_state, _vip_state
    import playback

    playback.reset()
    with _api_lock:
        if _api_instance is not None:
            try:
                _api_instance.session.close()
            except Exception:
                pass
        _api_instance = None
    with _login_state_lock:
        _login_state = None
    with _vip_state_lock:
        _vip_state = None
    with _url_cache_lock:
        _url_cache.clear()
    if reason:
        logger.info("netease api instance reset: %s", reason)


def _map_song_detail(item: dict[str, Any]) -> dict[str, Any]:
    sid = item.get("id") or item.get("song_id")
    song_id = int(sid) if sid is not None else 0
    name = str(item.get("name") or "")
    ar_list = item.get("ar") or item.get("artists") or []
    if isinstance(ar_list, list):
        artist = " / ".join(
            str(a.get("name")) for a in ar_list if isinstance(a, dict) and a.get("name")
        )
    else:
        artist = ""
    al = item.get("al") or item.get("album") or {}
    if isinstance(al, dict):
        album_name = str(al.get("name") or "")
        album_pic_url = str(al.get("picUrl") or al.get("pic_url") or "")
    else:
        album_name = ""
        album_pic_url = ""
    duration_ms = int(item.get("dt") or item.get("duration") or 0)
    try:
        fee = int(item.get("fee") or 0)
    except (TypeError, ValueError):
        fee = 0
    return {
        "song_id": song_id,
        "name": name,
        "artist": artist,
        "album_name": album_name,
        "album_pic_url": album_pic_url,
        "duration_ms": duration_ms,
        "has_sq": bool(item.get("sq")),
        "has_hr": bool(item.get("hr")),
        # issue #57：fee 收费类型（0=免费 1=VIP 4=购买专辑 8=非会员免费低音质），
        # 供行级 needs_vip 标记——无 VIP 账号点 VIP 歌只有试听，标记让用户有预期
        "fee": fee,
    }


_vip_state: "tuple[bool, float] | None" = None
_vip_state_lock = threading.Lock()
_VIP_TTL_S = 300.0


def vip_entitled() -> bool:
    """当前登录账号是否具有 VIP 权益（profile.vipType>0）。短 TTL 缓存。

    匿名/失败一律按无 VIP 处理（保守：宁可多标记，不漏标）。"""
    global _vip_state
    with _vip_state_lock:
        state = _vip_state
    if state is not None and time.monotonic() - state[1] < _VIP_TTL_S:
        return state[0]
    entitled = False
    try:
        with _api_lock:
            api = _get_api_locked()
            info = api.get_account_info()
        profile = (info or {}).get("profile") or {}
        entitled = int(profile.get("vipType") or 0) > 0
    except Exception:
        entitled = False
    with _vip_state_lock:
        _vip_state = (entitled, time.monotonic())
    return entitled


def check_is_logged_in() -> bool:
    global _login_state
    with _login_state_lock:
        state = _login_state
    if state is not None:
        logged, at = state
        if time.monotonic() - at < (_LOGIN_TTL_OK_S if logged else _LOGIN_TTL_FAIL_S):
            return logged
        # 缓存过期：磁盘 cookie 可能已换（重新扫码/在别处登录），先重建实例再探测
        with _login_state_lock:
            _login_state = None
        reset_api("login-state ttl expired")
    try:
        with _api_lock:
            api = _get_api_locked()
            info = api.get_account_info()
        logged = bool(info and (info.get("account") or info.get("profile")))
    except Exception:
        logged = False
    with _login_state_lock:
        _login_state = (logged, time.monotonic())
    return logged


def filter_playable_song_ids(ids: list[int]) -> set[int]:
    """根据真实可播放状态过滤歌曲 ID。

    - 未登录时：使用 api.songs_url 批量获取真实可播状态。凡是 url 为空/404、或者带有 freeTrialInfo（试听片段）且 fee != 0 的曲目，一律过滤掉。
    - 已登录时：如果有账号权限能取到完整真实 url 且非试听，则允许返回；若无权限仍过滤。
    - 只能试听30~45秒片段（带 freeTrialInfo/试听限制）的歌曲，绝不能当作可播放曲目返回。
    """
    if not ids:
        return set()
    with _api_lock:
        try:
            api = _get_api_locked()
            urls_data = api.songs_url(ids)
        except Exception:
            return set()
    if not isinstance(urls_data, list):
        return set()

    logged_in = check_is_logged_in()
    playable_ids: set[int] = set()
    for item in urls_data:
        if not isinstance(item, dict):
            continue
        sid = item.get("id") or item.get("song_id")
        if not sid:
            continue
        try:
            sid_int = int(sid)
        except (ValueError, TypeError):
            continue

        url = item.get("url")
        code = item.get("code")
        fee = item.get("fee", 0)
        free_trial = item.get("freeTrialInfo")

        # 核心铁律：url 为空或 code == 404，坚决过滤
        if not url or not str(url).strip() or code == 404:
            continue
        # 凡是带有 freeTrialInfo（试听片段）且 fee != 0 的曲目，一律过滤掉
        # 并且只能试听片段的歌曲绝不当作可播返回
        if free_trial:
            continue
        # 未登录状态下，收费/VIP/专辑曲目坚决不返回
        if not logged_in and fee != 0 and fee not in (0, 8):
            continue

        playable_ids.add(sid_int)
    return playable_ids


def get_song_url(song_id: int, quality: str, fresh: bool = False) -> "dict[str, Any] | None":
    """进程内解析单曲播放链接（复用常驻实例，免起 CLI 子进程）。

    返回对齐 CLI `song url --json` 的 data 字段（含 url/code/fee 等）；接口
    异常或返回空时返回 None，由调用方降级 CLI（保留结构化错误语义）。
    结果短缓存（成功 600s / 失败 60s，键含音质），reset_api 时随实例一并清空；
    fresh=True 旁路缓存读（仍回写结果），供代理确认旧直链失效后强制重解析。
    """
    key = (int(song_id), str(quality))
    now = time.monotonic()
    with _url_cache_lock:
        hit = _url_cache.get(key)
        if hit and now < hit[1] and not fresh:
            return hit[0]
    try:
        with _api_lock:
            api = _get_api_locked()
            # songs_url 的音质取自全局 Config：临时改写再恢复（CLI cmd_song_url 同款做法）
            from NEMbox.config import Config

            config = Config()
            old_quality = config.get("music_quality")
            config.config.setdefault("music_quality", {})["value"] = quality
            try:
                urls = api.songs_url([int(song_id)])
            finally:
                config.config["music_quality"]["value"] = old_quality
    except Exception:
        return None
    if not isinstance(urls, list) or not urls or not isinstance(urls[0], dict):
        return None
    item = urls[0]
    ok = item.get("code") == 200 and item.get("url")
    with _url_cache_lock:
        if len(_url_cache) >= _URL_CACHE_MAX:
            expire = time.monotonic()
            for k in [k for k, v in _url_cache.items() if v[1] <= expire]:
                del _url_cache[k]
            if key not in _url_cache and len(_url_cache) >= _URL_CACHE_MAX:
                del _url_cache[min(_url_cache, key=lambda k: _url_cache[k][1])]
        _url_cache[key] = (item, time.monotonic() + (_URL_CACHE_TTL_OK_S if ok else _URL_CACHE_TTL_FAIL_S))
    return item


def batch_song_details(ids: list[int]) -> list[dict[str, Any]]:
    """批量歌曲详情（名称/歌手/专辑/封面/时长/有无无损），按请求顺序返回。

    封面与可播性解耦（2.8.0）：详情行是纯展示元数据，绝不能因取链判定的
    可播状态整行丢弃——登录态抖动（NEMbox 任一 cookie 过期即清空整个 jar）、
    VIP 权益波动或风控都会让 songs_url 拿不到完整 url，此前把这些歌的详情
    连同专辑封面一起丢掉，表现为"已登录 VIP 却大面积无封面"。可播过滤只属
    于搜索入口（filter_playable_song_ids），详情接口一律全量返回。
    """
    if not ids:
        return []
    with _api_lock:
        api = _get_api_locked()
        raw_items = api.songs_detail(ids)
    if not raw_items or not isinstance(raw_items, list):
        return []

    detail_map: dict[int, dict[str, Any]] = {}
    for item in raw_items:
        if isinstance(item, dict):
            mapped = _map_song_detail(item)
            if mapped["song_id"]:
                detail_map[mapped["song_id"]] = mapped
    return [detail_map[sid] for sid in ids if sid in detail_map]


def song_detail_raw(song_id: int) -> "dict[str, Any] | None":
    """单曲原生详情（weapi v3 song/detail 原始对象，含 al.picUrl/dt/sq/hr 等）。

    供 /api/v1/song/{id}/info 进程内直取（毫秒级），失败返回 None 由调用方
    降级 CLI 子进程。匿名可用，不经过可播过滤。
    """
    try:
        with _api_lock:
            api = _get_api_locked()
            rows = api.songs_detail([int(song_id)])
    except Exception as e:
        logger.warning("song_detail_raw failed for %s: %s", song_id, type(e).__name__)
        return None
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0]
    return None


def song_lyric_pair(song_id: int) -> dict[str, str]:
    with _api_lock:
        api = _get_api_locked()
        raw_lyric = api.song_lyric(song_id)
        raw_tlyric = api.song_tlyric(song_id)
    lyric_str = "\n".join(str(line) for line in raw_lyric) if isinstance(raw_lyric, list) else ""
    tlyric_str = "\n".join(str(line) for line in raw_tlyric) if isinstance(raw_tlyric, list) else ""
    return {"lyric": lyric_str, "tlyric": tlyric_str}


def search_web_fallback(keyword: str, stype: str = "song", limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    """网易官方 Web 搜索接口降级容错（https://music.163.com/api/search/get/web）。

    当主接口被风控（如 405 操作频繁）或失败时，调用官方备用接口获取歌曲列表。
    offset 为官方接口原生分页参数（0 起始）；CLI search 无 offset 参数，深分页也走这里。
    """
    if not keyword or not keyword.strip():
        return []
    url = "https://music.163.com/api/search/get/web"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://music.163.com",
        "Cookie": "os=pc",
    }
    type_map = {
        "song": 1,
        "album": 10,
        "artist": 100,
        "playlist": 1000,
    }
    data = {
        "s": keyword.strip(),
        "type": str(type_map.get(stype, 1)),
        "limit": str(limit),
        "offset": str(max(0, int(offset or 0))),
    }
    res = None
    try:
        if httpx is not None:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, headers=headers, data=data)
                resp.raise_for_status()
                res = resp.json()
        elif requests is not None:
            resp = requests.post(url, headers=headers, data=data, timeout=10.0)
            resp.raise_for_status()
            res = resp.json()
        else:
            import urllib.parse
            import urllib.request
            encoded = urllib.parse.urlencode(data).encode("utf-8")
            req = urllib.request.Request(url, data=encoded, headers=headers)
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                res = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning("search_web_fallback request failed: %s", e)
        return []

    if not isinstance(res, dict):
        return []
    result = res.get("result")
    if not isinstance(result, dict):
        return []

    songs = result.get("songs")
    if not isinstance(songs, list):
        return []

    out: list[dict[str, Any]] = []
    for s in songs:
        if not isinstance(s, dict):
            continue
        sid = s.get("id") or s.get("song_id")
        if not sid:
            continue
        try:
            sid_int = int(sid)
        except (ValueError, TypeError):
            continue

        name = str(s.get("name") or s.get("title") or "")
        artists = s.get("artists") or []
        if isinstance(artists, list):
            artist = " / ".join(str(a.get("name")) for a in artists if isinstance(a, dict) and a.get("name"))
        else:
            artist = str(s.get("artist") or "")
        album = s.get("album") or {}
        album_name = str(album.get("name") or "") if isinstance(album, dict) else str(s.get("album_name") or "")
        album_pic = str(album.get("picUrl") or album.get("pic_url") or "") if isinstance(album, dict) else ""
        duration_raw = s.get("duration") or 0
        try:
            dur = float(duration_raw)
            if dur > 10000:
                dur = dur / 1000.0
        except (ValueError, TypeError):
            dur = 0.0

        out.append({
            "song_id": sid_int,
            "id": sid_int,
            "song_name": name,
            "title": name,
            "name": name,
            "artist": artist,
            "album_name": album_name,
            "album": album_name,
            # 主接口被风控走本兜底时行内自带封面，免去代理二次补全对详情接口的依赖
            "album_pic_url": album_pic,
            "duration": dur,
            "quality": "lossless",
        })
    return out


def _int_or_zero(val: Any) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _ms_to_epoch_s(val: Any) -> int:
    """网易接口的毫秒时间戳 -> 秒；异常回落当前时间。"""
    n = _int_or_zero(val)
    if n > 0:
        return n // 1000
    return int(time.time())


def _map_playlist_summary(item: dict) -> dict:
    pid = _int_or_zero(item.get("id") or item.get("playlist_id"))
    return {
        "playlist_id": pid,
        "name": str(item.get("name") or ""),
        "cover_url": str(item.get("coverImgUrl") or item.get("cover_img_url") or ""),
        "track_count": _int_or_zero(item.get("trackCount") or item.get("track_count")),
        "created_at": _ms_to_epoch_s(item.get("createTime")),
        "updated_at": _ms_to_epoch_s(item.get("updateTime")),
        "user_id": _int_or_zero(item.get("userId") or item.get("user_id")),
        "special_type": _int_or_zero(item.get("specialType") or item.get("special_type")),
    }


def user_playlists(limit: int = 100) -> "dict[str, Any] | None":
    """当前登录账号的歌单列表（含自建与收藏，自建判定交给调用方按 user_id 过滤）。

    返回 {"uid": 账号id, "playlists": [摘要行]}；未登录/接口失败返回 None。
    """
    if not check_is_logged_in():
        return None
    try:
        with _api_lock:
            api = _get_api_locked()
            info = api.get_account_info()
            uid = 0
            for src in (info.get("account"), info.get("profile")):
                if isinstance(src, dict):
                    uid = _int_or_zero(src.get("id") or src.get("userId"))
                    if uid:
                        break
            if not uid:
                return None
            rows = api.user_playlist(uid, offset=0, limit=limit)
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    playlists = []
    for it in rows:
        if isinstance(it, dict):
            mapped = _map_playlist_summary(it)
            if mapped["playlist_id"] > 0:
                playlists.append(mapped)
    return {"uid": uid, "playlists": playlists}


def playlist_track_ids(playlist_id: int) -> "list[int] | None":
    """歌单曲目 ID 列表（保持网易歌单内顺序）；未登录返回 None，空歌单返回 []。"""
    if not check_is_logged_in():
        return None
    try:
        with _api_lock:
            api = _get_api_locked()
            raw = api.playlist_songlist(int(playlist_id))
    except Exception:
        return None
    if not isinstance(raw, list):
        return []
    ids: list[int] = []
    for it in raw:
        sid = it if isinstance(it, int) else (it.get("id") if isinstance(it, dict) else None)
        sid = _int_or_zero(sid)
        if sid > 0:
            ids.append(sid)
    return ids


def _public_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    from NEMbox.api import BASE_URL

    with _api_lock:
        api = _get_api_locked()
        resp = api._raw_request("GET", f"{BASE_URL}{path}", params or {})
        resp.raise_for_status()
        return _validate_public_response(resp.json())


def _validate_public_response(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("code", 200) != 200:
        raise RuntimeError(f"netease public request failed: code={data.get('code') if isinstance(data, dict) else 'invalid'}")
    return data


def _public_post(path: str, params: dict[str, Any]) -> dict[str, Any]:
    with _api_lock:
        api = _get_api_locked()
        return _validate_public_response(api.request("POST", path, params))


def public_playlist_categories(kind: str = "playlist") -> dict[str, Any]:
    highquality_tags: list[dict[str, str]] = []
    if kind == "highquality":
        data = _public_get("/api/playlist/highquality/tags")
        tags = data.get("tags") if isinstance(data, dict) else []
        rows = []
        for item in tags if isinstance(tags, list) else []:
            tag = (item.get("playlistTag") or item) if isinstance(item, dict) else None
            if isinstance(tag, dict) and tag.get("name"):
                rows.append({"id": str(tag.get("name")), "name": str(tag.get("name"))})
        return {"ok": True, "categories": [{"id": "", "name": "全部"}], "tags": rows}

    data = _public_post("/weapi/playlist/catalogue", {})
    if not isinstance(data, dict) or data.get("code") != 200:
        raise RuntimeError(f"netease catalogue failed: code={data.get('code') if isinstance(data, dict) else 'invalid'}")
    names = data.get("categories") or {}
    grouped: dict[str, list[dict]] = {}
    for raw in data.get("sub") or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        group = str(names.get(str(raw.get("category")), "其他"))
        grouped.setdefault(group, []).append({"id": str(raw["name"]), "name": str(raw["name"])})
    categories = [{"id": "", "name": "全部"}]
    categories.extend({"id": name, "name": name} for name in grouped)
    try:
        high_rows = public_playlist_categories("highquality").get("tags") or []
        highquality_tags.extend(high_rows)
    except Exception:
        pass
    return {"ok": True, "categories": categories, "tags": grouped, "highquality_tags": highquality_tags}


def _public_summary(item: dict[str, Any], kind: str = "playlist") -> dict[str, Any]:
    creator = item.get("creator") if isinstance(item.get("creator"), dict) else {}
    cover = str(
        item.get("coverImgUrl") or item.get("cover_url")
        or item.get("picUrl") or item.get("cover") or ""
    )
    try:
        updated = int(item.get("updateTime") or item.get("trackNumberUpdateTime") or 0) // 1000
    except (TypeError, ValueError):
        updated = 0
    return {
        "id": str(item.get("id") or ""),
        "kind": kind,
        "source": "netease",
        "name": str(item.get("name") or ""),
        "cover_url": cover,
        "track_count": _int_or_zero(item.get("trackCount") or item.get("trackcount")),
        "author": str(item.get("nickname") or creator.get("nickname") or ""),
        "description": str(item.get("description") or item.get("copywriter") or ""),
        "updated_at": updated,
    }


def public_playlist_search(keyword: str, page: int = 1, limit: int = 30) -> dict[str, Any]:
    """网易公共歌单关键词搜索（/weapi/search/get type=1000，原生 offset 分页）。

    与 search_web_fallback 的歌曲降级不同：这里读取 result.playlists 并复用
    _public_summary 的歌单摘要映射，offset 推进基于上游原始窗口。
    """
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or 30), 50))
    text = str(keyword or "").strip()
    if not text:
        return {"ok": True, "items": [], "total": 0, "has_more": False}
    data = _public_post("/weapi/search/get", {
        "s": text, "type": 1000,
        "offset": (page - 1) * limit, "total": "true", "limit": limit,
    })
    result = data.get("result") if isinstance(data, dict) else None
    rows = result.get("playlists") if isinstance(result, dict) else None
    rows = rows if isinstance(rows, list) else []
    items = [_public_summary(row) for row in rows if isinstance(row, dict) and row.get("id")]
    total = _int_or_zero(result.get("playlistCount")) if isinstance(result, dict) else 0
    # 总数可能过期；真空页必须停，过滤条目则不改变上游窗口的推进。
    has_more = bool(rows) and (page * limit < total if total else True)
    return {"ok": True, "items": items, "raw_count": len(rows), "total": total, "has_more": has_more}


def public_playlists(
    channel: str,
    category: str = "",
    sort: str = "hot",
    page: int = 1,
    limit: int = 30,
    cursor: str = "",
    keyword: str = "",
) -> dict[str, Any]:
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or 30), 50))
    if channel == "search":
        return public_playlist_search(keyword, page, limit)
    if channel == "charts":
        data = _public_get("/api/toplist")
        rows = data.get("list") if isinstance(data, dict) else []
        items = [_public_summary(row, "chart") for row in rows or [] if isinstance(row, dict) and row.get("id")]
        return {"ok": True, "items": items, "total": len(items), "has_more": False}

    if channel == "recommend":
        data = _public_get("/api/personalized/playlist", {"limit": min(limit, 30)})
        rows = data.get("result") if isinstance(data, dict) else []
        return {"ok": True, "items": [_public_summary(row) for row in rows or [] if isinstance(row, dict) and row.get("id")], "has_more": False}

    if channel == "account_recommend":
        with _api_lock:
            api = _get_api_locked()
            raw = api.request("POST", "/weapi/v1/discovery/recommend/resource", {})
            if isinstance(raw, dict) and raw.get("code") == 301:
                return {"ok": False, "error": "not_logged_in", "logged_in": False}
            _validate_public_response(raw)
            rows = raw.get("recommend") or []
        if not isinstance(rows, list):
            raise RuntimeError("netease account recommend returned invalid payload")
        return {"ok": True, "items": [_public_summary(row) for row in rows if isinstance(row, dict) and row.get("id")], "has_more": False}

    if channel == "highquality":
        params: dict[str, Any] = {"limit": limit}
        if category:
            params["cat"] = category
        if cursor:
            params["before"] = cursor
        data = _public_get("/api/playlist/highquality/list", params)
        rows = data.get("playlists") if isinstance(data, dict) else []
        next_cursor = ""
        if rows:
            last = rows[-1] if isinstance(rows[-1], dict) else {}
            next_cursor = str(last.get("updateTime") or "")
        return {
            "ok": True,
            "items": [_public_summary(row) for row in rows or [] if isinstance(row, dict) and row.get("id")],
            "total": _int_or_zero(data.get("total")),
            # 空页时没有可推进的游标（next_cursor 取自最后一个原始条目），必须停：
            # 否则前端带空 before 会从头重复第一页
            "has_more": bool(rows) and (bool(data.get("more")) if isinstance(data, dict) else True),
            "next_cursor": next_cursor,
        }

    data = _public_post("/weapi/playlist/list", {
        "cat": category or "全部",
        "order": sort if sort in ("hot", "new") else "hot",
        "offset": (page - 1) * limit,
        "total": "true",
        "limit": limit,
    })
    if not isinstance(data, dict) or data.get("code") != 200:
        raise RuntimeError(f"netease playlists failed: code={data.get('code') if isinstance(data, dict) else 'invalid'}")
    rows = data.get("playlists") or []
    total = _int_or_zero(data.get("total"))
    return {
        "ok": True,
        "items": [_public_summary(row) for row in rows if isinstance(row, dict) and row.get("id")],
        "total": total,
        "has_more": bool(data.get("more")) or (page * limit < total if total else bool(rows)),
    }


def public_playlist(playlist_id: int, kind: str = "playlist") -> dict[str, Any]:
    if kind == "chart":
        try:
            rows = public_playlists("charts").get("items") or []
            for row in rows:
                if row.get("id") == str(playlist_id):
                    return {"ok": True, "data": row}
        except Exception as exc:
            logger.warning("public_playlist charts lookup failed for %s, falling back to detail: %s", playlist_id, exc)
    with _api_lock:
        api = _get_api_locked()
        raw = api.eapi_request("/api/v6/playlist/detail", {
            "id": int(playlist_id), "n": 0, "s": 0,
        })
    playlist = raw.get("playlist") if isinstance(raw, dict) else None
    if not isinstance(playlist, dict) or not playlist.get("id"):
        raise RuntimeError(f"playlist {playlist_id} not found")
    return {"ok": True, "data": _public_summary(playlist, "chart" if kind == "chart" else "playlist")}


def public_playlist_tracks(playlist_id: int, kind: str = "playlist") -> dict[str, Any]:
    with _api_lock:
        api = _get_api_locked()
        raw = api.eapi_request("/api/v6/playlist/detail", {
            "id": int(playlist_id), "n": 1000, "s": 0,
        })
    playlist = raw.get("playlist") if isinstance(raw, dict) else None
    if not isinstance(playlist, dict) or not isinstance(playlist.get("trackIds"), list):
        raise RuntimeError(f"playlist {playlist_id} tracks unavailable")
    track_ids_raw = playlist["trackIds"]
    ids: list[int] = []
    seen: set[int] = set()
    truncated = False
    for item in track_ids_raw or []:
        sid = item if isinstance(item, int) else (item.get("id") if isinstance(item, dict) else 0)
        sid = _int_or_zero(sid)
        if sid <= 0 or sid in seen:
            continue
        if len(ids) >= 1000:
            truncated = True
            break
        seen.add(sid)
        ids.append(sid)
    rows: list[dict[str, Any]] = []
    for start in range(0, len(ids), 100):
        rows.extend(batch_song_details(ids[start:start + 100]))
    by_id = {int(row["song_id"]): row for row in rows}
    if any(sid not in by_id for sid in ids):
        raise RuntimeError("netease playlist song details incomplete")
    total = max(len(track_ids_raw), _int_or_zero(playlist.get("trackCount")))
    return {"ok": True, "data": [by_id[sid] for sid in ids], "total": total, "truncated": truncated or total > 1000}
