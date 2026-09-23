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
    global _api_instance, _login_state
    with _api_lock:
        if _api_instance is not None:
            try:
                _api_instance.session.close()
            except Exception:
                pass
        _api_instance = None
    with _login_state_lock:
        _login_state = None
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
    return {
        "song_id": song_id,
        "name": name,
        "artist": artist,
        "album_name": album_name,
        "album_pic_url": album_pic_url,
        "duration_ms": duration_ms,
        "has_sq": bool(item.get("sq")),
        "has_hr": bool(item.get("hr")),
    }


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


def get_song_url(song_id: int, quality: str) -> "dict[str, Any] | None":
    """进程内解析单曲播放链接（复用常驻实例，免起 CLI 子进程）。

    返回对齐 CLI `song url --json` 的 data 字段（含 url/code/fee 等）；接口
    异常或返回空时返回 None，由调用方降级 CLI（保留结构化错误语义）。
    结果短缓存（成功 600s / 失败 60s，键含音质），reset_api 时随实例一并清空。
    """
    key = (int(song_id), str(quality))
    now = time.monotonic()
    with _url_cache_lock:
        hit = _url_cache.get(key)
        if hit and now < hit[1]:
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
        _url_cache[key] = (item, time.monotonic() + (_URL_CACHE_TTL_OK_S if ok else _URL_CACHE_TTL_FAIL_S))
    return item


def batch_song_details(ids: list[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    with _api_lock:
        api = _get_api_locked()
        raw_items = api.songs_detail(ids)
    if not raw_items or not isinstance(raw_items, list):
        return []

    playable_ids = filter_playable_song_ids(ids)

    detail_map: dict[int, dict[str, Any]] = {}
    for item in raw_items:
        if isinstance(item, dict):
            mapped = _map_song_detail(item)
            if mapped["song_id"] in playable_ids:
                detail_map[mapped["song_id"]] = mapped
    return [detail_map[sid] for sid in ids if sid in detail_map]


def song_lyric_pair(song_id: int) -> dict[str, str]:
    with _api_lock:
        api = _get_api_locked()
        raw_lyric = api.song_lyric(song_id)
        raw_tlyric = api.song_tlyric(song_id)
    lyric_str = "\n".join(str(line) for line in raw_lyric) if isinstance(raw_lyric, list) else ""
    tlyric_str = "\n".join(str(line) for line in raw_tlyric) if isinstance(raw_tlyric, list) else ""
    return {"lyric": lyric_str, "tlyric": tlyric_str}


def search_web_fallback(keyword: str, stype: str = "song", limit: int = 20) -> list[dict[str, Any]]:
    """网易官方 Web 搜索接口降级容错（https://music.163.com/api/search/get/web）。

    当主接口被风控（如 405 操作频繁）或失败时，调用官方备用接口获取歌曲列表。
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
        "offset": "0",
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
            "duration": dur,
            "quality": "lossless",
        })
    return out
