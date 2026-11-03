"""网易云 HTTP 后端客户端（NeteaseCloudMusicApi 兼容协议）。

原网易音源包装 NetEase-MusicBox CLI；现直连兼容 NeteaseCloudMusicApi 的 HTTP
后端（默认 https://zm.wwoyun.cn），由 app.py 继续对外暴露同一套 /api/v1/*
契约，代理侧无需改动。

后端地址经 FNMUSIC_NETEASE_API_BASE 配置（.env）；未配置时用内置默认值。
后端自带服务端登录态（免扫码即可解析 VIP 直链），因此不再有用户级登录。
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover - 容器镜像已统一安装 httpx
    httpx = None

logger = logging.getLogger("neteasefree_service.netease_api")

DEFAULT_BASE = "https://zm.wwoyun.cn"
_BASE_ENV_KEYS = ("FNMUSIC_NETEASE_API_BASE", "NETEASE_API_BASE")

# 与 /song/url/v1 一致的七档音质（从低到高，含 sky 沉浸声）。
# 也用作质量探测：最低档能出直链即认为该曲可播。
QUALITY_LEVELS = ("standard", "higher", "exhigh", "lossless", "hires", "jymaster", "sky")
PROBE_LEVEL = "standard"

_TIMEOUT_S = 12.0
_MAX_CONNECTIONS = 16


class NeteaseApiError(Exception):
    """后端不可达 / 非 200 / 非 JSON。"""


def api_base() -> str:
    for key in _BASE_ENV_KEYS:
        val = (os.environ.get(key) or "").strip()
        if val:
            return val.rstrip("/")
    return DEFAULT_BASE


_client_lock = threading.Lock()
_client: "httpx.Client | None" = None


def _get_client() -> "httpx.Client":
    global _client
    if httpx is None:
        raise NeteaseApiError("httpx 未安装，无法访问网易后端")
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                timeout=_TIMEOUT_S,
                follow_redirects=True,
                limits=httpx.Limits(max_connections=_MAX_CONNECTIONS),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Referer": "https://music.163.com",
                },
            )
        return _client


def reset_client(reason: str = "") -> None:
    """丢弃连接池（测试隔离 / 后端地址变更后调用）。"""
    global _client
    with _client_lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
        _client = None
    if reason:
        logger.info("netease api client reset: %s", reason)


def request_json(path: str, params: "dict[str, Any] | None" = None, timeout: float | None = None) -> dict:
    """GET {base}{path} -> JSON dict；任何异常统一抛 NeteaseApiError。"""
    url = f"{api_base()}{path}"
    try:
        resp = _get_client().get(url, params=params or {}, timeout=timeout or _TIMEOUT_S)
    except Exception as exc:  # 网络/超时/连接池异常
        raise NeteaseApiError(f"request failed: {exc}") from exc
    if resp.status_code != 200:
        raise NeteaseApiError(f"http {resp.status_code} for {path}")
    try:
        data = resp.json()
    except Exception as exc:
        raise NeteaseApiError(f"invalid json for {path}") from exc
    if not isinstance(data, dict):
        raise NeteaseApiError(f"unexpected payload type for {path}")
    return data


def available() -> bool:
    """轻量探活：后端可达且返回网易成功码。"""
    try:
        data = request_json("/cloudsearch", {"keywords": "test", "limit": 1}, timeout=6.0)
    except NeteaseApiError:
        return False
    return data.get("code") == 200


# ------------------------------------------------------------------ 形状转换 --

def secure_url(url: Any) -> str:
    """网易 CDN 直链常为 http://；容器内与外网均可用 https，统一升级避免混合内容。"""
    raw = str(url or "").strip()
    if raw.startswith("http://"):
        return "https://" + raw[len("http://"):]
    return raw


def artists_joined(ar: Any) -> str:
    if not isinstance(ar, list):
        return ""
    return " / ".join(str(a.get("name")) for a in ar if isinstance(a, dict) and a.get("name"))


def _album_of(song: dict) -> dict:
    al = song.get("al") or song.get("album") or {}
    return al if isinstance(al, dict) else {}


def _quality_flag(song: dict, privilege: "dict | None") -> str:
    """给代理搜索用的 quality 字段：含 "SQ"/"HR" 时代理判定为 flac 档。"""
    if song.get("hr") or song.get("sq"):
        return "SQ"
    if isinstance(privilege, dict):
        level = str(privilege.get("maxBrLevel") or privilege.get("plLevel") or "").lower()
        if level in ("lossless", "hires", "jymaster", "sky"):
            return "SQ"
    return ""


def song_detail_row(song: dict, privilege: "dict | None" = None) -> dict:
    """网易原生歌曲对象 -> batch_song_details 行（代理内部统一详情形状）。"""
    al = _album_of(song)
    sid = song.get("id") or song.get("song_id") or 0
    try:
        song_id = int(sid)
    except (TypeError, ValueError):
        song_id = 0
    return {
        "song_id": song_id,
        "name": str(song.get("name") or ""),
        "artist": artists_joined(song.get("ar")),
        "album_name": str(al.get("name") or ""),
        "album_pic_url": secure_url(al.get("picUrl")),
        "duration_ms": _int(song.get("dt") or song.get("duration")),
        "has_sq": bool(song.get("sq")),
        "has_hr": bool(song.get("hr")),
    }


def song_search_row(song: dict, privilege: "dict | None" = None) -> dict:
    """网易原生歌曲对象 -> musicbox 搜索行（duration 为秒，对齐代理解析）。"""
    al = _album_of(song)
    sid = song.get("id") or 0
    return {
        "song_id": _int(sid),
        "id": _int(sid),
        "song_name": str(song.get("name") or ""),
        "name": str(song.get("name") or ""),
        "artist": artists_joined(song.get("ar")),
        "album_name": str(al.get("name") or ""),
        "album": str(al.get("name") or ""),
        "duration": _int(song.get("dt")) / 1000.0,
        "quality": _quality_flag(song, privilege),
        "version": "",
    }


def _int(val: Any) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------ 端点封装 --

def search_songs(keyword: str, limit: int = 20, offset: int = 0) -> list[dict]:
    data = request_json(
        "/cloudsearch",
        {"keywords": keyword, "type": 1, "limit": max(1, int(limit)), "offset": max(0, int(offset))},
    )
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    songs = result.get("songs") if isinstance(result.get("songs"), list) else []
    return [s for s in songs if isinstance(s, dict)]


def search_albums(keyword: str, limit: int = 10, offset: int = 0) -> list[dict]:
    data = request_json(
        "/cloudsearch",
        {"keywords": keyword, "type": 10, "limit": max(1, int(limit)), "offset": max(0, int(offset))},
    )
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    albums = result.get("albums") if isinstance(result.get("albums"), list) else []
    return [a for a in albums if isinstance(a, dict)]


def search_playlists(keyword: str, limit: int = 10, offset: int = 0) -> list[dict]:
    data = request_json(
        "/cloudsearch",
        {"keywords": keyword, "type": 1000, "limit": max(1, int(limit)), "offset": max(0, int(offset))},
    )
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    rows = result.get("playlists") if isinstance(result.get("playlists"), list) else []
    return [p for p in rows if isinstance(p, dict)]


def search_artists(keyword: str, limit: int = 10, offset: int = 0) -> list[dict]:
    data = request_json(
        "/cloudsearch",
        {"keywords": keyword, "type": 100, "limit": max(1, int(limit)), "offset": max(0, int(offset))},
    )
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    rows = result.get("artists") if isinstance(result.get("artists"), list) else []
    return [a for a in rows if isinstance(a, dict)]


def songs_detail(ids: "list[int]") -> "tuple[list[dict], list[dict]]":
    """批量详情 -> (song 原生对象列表, privilege 列表)。"""
    if not ids:
        return [], []
    data = request_json("/song/detail", {"ids": ",".join(str(int(i)) for i in ids)})
    songs = data.get("songs") if isinstance(data.get("songs"), list) else []
    privileges = data.get("privileges") if isinstance(data.get("privileges"), list) else []
    return [s for s in songs if isinstance(s, dict)], [p for p in privileges if isinstance(p, dict)]


def song_object(song_id: int) -> dict | None:
    """单曲原生对象（含 ar/al/dt/sq/hr），供 /api/v1/song/{id}/info 使用。"""
    songs, _ = songs_detail([int(song_id)])
    return songs[0] if songs else None


def song_urls(ids: "list[int]", level: str = PROBE_LEVEL) -> "list[dict]":
    if not ids:
        return []
    lvl = level if level in QUALITY_LEVELS else PROBE_LEVEL
    data = request_json(
        "/song/url/v1", {"id": ",".join(str(int(i)) for i in ids), "level": lvl}, timeout=20.0
    )
    rows = data.get("data") if isinstance(data.get("data"), list) else []
    out: list[dict] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        item = dict(item)
        if item.get("url"):
            item["url"] = secure_url(item["url"])
        out.append(item)
    return out


def song_url(song_id: int, level: str) -> dict | None:
    rows = song_urls([int(song_id)], level)
    return rows[0] if rows else None


def lyric(song_id: int) -> dict:
    data = request_json("/lyric", {"id": int(song_id)}, timeout=15.0)
    lrc = data.get("lrc") if isinstance(data.get("lrc"), dict) else {}
    tlyric = data.get("tlyric") if isinstance(data.get("tlyric"), dict) else {}
    return {
        "lyric": str(lrc.get("lyric") or ""),
        "tlyric": str(tlyric.get("lyric") or ""),
    }


def album_songs(album_id: int) -> "tuple[dict, list[dict]]":
    data = request_json("/album", {"id": int(album_id)}, timeout=20.0)
    album = data.get("album") if isinstance(data.get("album"), dict) else {}
    songs = data.get("songs") if isinstance(data.get("songs"), list) else []
    return album, [s for s in songs if isinstance(s, dict)]


def artist_songs(artist_id: int, limit: int = 50) -> "tuple[dict, list[dict]]":
    """歌手热门曲目 -> (artist 对象, hotSongs 原生列表)。"""
    data = request_json("/artists", {"id": int(artist_id), "limit": max(1, int(limit))}, timeout=20.0)
    artist = data.get("artist") if isinstance(data.get("artist"), dict) else {}
    songs = data.get("hotSongs") if isinstance(data.get("hotSongs"), list) else []
    return artist, [s for s in songs if isinstance(s, dict)]


def playlist_detail(playlist_id: int) -> dict:
    data = request_json("/playlist/detail", {"id": int(playlist_id)}, timeout=20.0)
    playlist = data.get("playlist") if isinstance(data.get("playlist"), dict) else {}
    return playlist


def playlist_track_all(playlist_id: int, limit: int = 1000) -> list[dict]:
    data = request_json(
        "/playlist/track/all", {"id": int(playlist_id), "limit": max(1, int(limit))}, timeout=25.0
    )
    songs = data.get("songs") if isinstance(data.get("songs"), list) else []
    return [s for s in songs if isinstance(s, dict)]


def top_playlists(cat: str = "", limit: int = 20) -> list[dict]:
    """分类歌单：网易官方分类（华语/流行/摇滚…）下的热门歌单列表。

    cat 为网易分类名（如「华语」）；limit 取该分类下前 N 个歌单（按热度）。
    """
    params: dict[str, Any] = {"limit": max(1, int(limit)), "order": "hot"}
    if cat and cat.strip():
        params["cat"] = cat.strip()
    data = request_json("/top/playlist", params, timeout=20.0)
    rows = data.get("playlists") if isinstance(data.get("playlists"), list) else []
    return [r for r in rows if isinstance(r, dict)]


def toplists() -> list[dict]:
    data = request_json("/toplist", timeout=20.0)
    rows = data.get("list") if isinstance(data.get("list"), list) else []
    return [r for r in rows if isinstance(r, dict)]


def recommend_daily() -> list[dict]:
    """网易每日推荐（后端登录态；失败返回 []）。"""
    data = request_json("/recommend/songs", timeout=20.0)
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    rows = inner.get("dailySongs") if isinstance(inner.get("dailySongs"), list) else []
    return [r for r in rows if isinstance(r, dict)]


def personalized_newsong(limit: int = 40) -> list[dict]:
    """平台通用推荐（匿名可用），返回原生 song 对象（在 result[].song 内）。"""
    data = request_json("/personalized/newsong", {"limit": max(1, int(limit))}, timeout=15.0)
    rows = data.get("result") if isinstance(data.get("result"), list) else []
    out: list[dict] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        song = item.get("song")
        if isinstance(song, dict):
            out.append(song)
    return out