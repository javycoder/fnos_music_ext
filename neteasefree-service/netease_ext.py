"""网易音源内部实现：批量详情 / 可播性 / 取链 / 歌词 / 账号歌单。

数据来自 netease_api（NeteaseCloudMusicApi 兼容 HTTP 后端），不再依赖
NetEase-MusicBox CLI。对外函数签名与返回形状沿用旧实现，app.py 契约不变。

关于「登录态」：后端自带服务端登录态，免扫码即可解析 VIP 直链，因此
check_is_logged_in() 表示「后端 VIP 解析可用」；而网易「账号歌单」是用户级
数据，后端无用户账号，user_playlists/playlist_track_ids 恒返回 None（未登录），
代理据此静默跳过「音乐页显示网易账号歌单」功能。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import netease_api as api
from netease_api import NeteaseApiError

logger = logging.getLogger("neteasefree_service.netease_ext")

# 后端可用性探测缓存：成功长缓存、失败短缓存，避免每次搜索都多打一次探活。
_state_lock = threading.Lock()
_state: "tuple[bool, float] | None" = None
_STATE_TTL_OK_S = 300.0
_STATE_TTL_FAIL_S = 30.0

# 取链结果短缓存：网易直链自带过期时间，只做短缓存。
_url_cache_lock = threading.Lock()
_url_cache: "dict[tuple[int, str], tuple[dict, float]]" = {}
_URL_CACHE_TTL_OK_S = 600.0
_URL_CACHE_TTL_FAIL_S = 60.0
_URL_CACHE_MAX = 4096


def reset_api(reason: str = "") -> None:
    """清空探活/取链缓存并重建连接池（切后端地址或异常自愈时调用）。"""
    global _state
    with _state_lock:
        _state = None
    with _url_cache_lock:
        _url_cache.clear()
    api.reset_client(reason)
    if reason:
        logger.info("netease api reset: %s", reason)


def check_is_logged_in() -> bool:
    """后端 VIP 解析是否可用（服务端登录态，非用户扫码）。"""
    global _state
    with _state_lock:
        state = _state
    if state is not None:
        ok, at = state
        if time.monotonic() - at < (_STATE_TTL_OK_S if ok else _STATE_TTL_FAIL_S):
            return ok
    ok = api.available()
    with _state_lock:
        _state = (ok, time.monotonic())
    return ok


def filter_playable_song_ids(ids: "list[int]") -> set[int]:
    """批量取链判定可播：最低档能拿到非空直链即视为可播。

    与旧实现一致，区分「带 freeTrialInfo 的试听片段」并剔除；不同之处在于
    后端自带 VIP，收费曲目同样能出直链，因此不再按 fee 过滤。
    传输异常时放行全部（fail-open）：真正的可播性在播放时仍会再解析一次，
    宁可多展示一条也不要让搜索因一次网络抖动而空掉。
    """
    wanted = [int(i) for i in ids if i]
    if not wanted:
        return set()
    try:
        rows = api.song_urls(wanted, api.PROBE_LEVEL)
    except NeteaseApiError as exc:
        logger.warning("playable probe failed, pass through %d ids: %s", len(wanted), exc)
        return set(wanted)

    playable: set[int] = set()
    for item in rows:
        url = str(item.get("url") or "").strip()
        if not url or item.get("code") == 404:
            continue
        if item.get("freeTrialInfo"):
            continue
        sid = api._int(item.get("id"))
        if sid > 0:
            playable.add(sid)
    return playable


def batch_song_details(ids: "list[int]") -> list[dict[str, Any]]:
    """批量详情（按入参顺序，已剔除不可播）——对齐旧实现返回形状。"""
    wanted = [int(i) for i in ids if i]
    if not wanted:
        return []
    try:
        songs, privileges = api.songs_detail(wanted)
    except NeteaseApiError as exc:
        logger.warning("batch_song_details failed: %s", exc)
        return []
    priv_map = {api._int(p.get("id")): p for p in privileges}
    playable = filter_playable_song_ids(wanted)
    detail_map: dict[int, dict] = {}
    for song in songs:
        row = api.song_detail_row(song, priv_map.get(api._int(song.get("id"))))
        sid = row["song_id"]
        if sid in playable:
            detail_map[sid] = row
    return [detail_map[sid] for sid in wanted if sid in detail_map]


def get_song_url(song_id: int, quality: str) -> "dict[str, Any] | None":
    """单曲取链（进程内短缓存）；后端异常返回 None 交由调用方降级。"""
    key = (int(song_id), str(quality))
    now = time.monotonic()
    with _url_cache_lock:
        hit = _url_cache.get(key)
        if hit and now < hit[1]:
            return hit[0]
    try:
        item = api.song_url(int(song_id), str(quality))
    except NeteaseApiError as exc:
        logger.warning("get_song_url failed for %s(%s): %s", song_id, quality, exc)
        return None
    if item is None:
        return None
    ok = item.get("code") == 200 and bool(item.get("url"))
    with _url_cache_lock:
        if len(_url_cache) >= _URL_CACHE_MAX:
            expire = time.monotonic()
            for k in [k for k, v in _url_cache.items() if v[1] <= expire]:
                del _url_cache[k]
        _url_cache[key] = (item, time.monotonic() + (_URL_CACHE_TTL_OK_S if ok else _URL_CACHE_TTL_FAIL_S))
    return item


def song_lyric_pair(song_id: int) -> dict[str, str]:
    """歌词 + 翻译（缺失返回空串，不抛异常）。"""
    try:
        data = api.lyric(int(song_id))
    except NeteaseApiError as exc:
        logger.warning("song_lyric_pair failed for %s: %s", song_id, exc)
        return {"lyric": "", "tlyric": ""}
    return {"lyric": str(data.get("lyric") or ""), "tlyric": str(data.get("tlyric") or "")}


def song_info(song_id: int) -> "dict[str, Any] | None":
    """单曲原生对象（ar/al/dt/sq/hr），供 /api/v1/song/{id}/info 与专辑解析。"""
    try:
        return api.song_object(int(song_id))
    except NeteaseApiError as exc:
        logger.warning("song_info failed for %s: %s", song_id, exc)
        return None


def album_songs(album_id: int) -> list[dict]:
    """专辑原生曲目列表（含 ar/al/dt/sq/hr，由代理自行归一化）。"""
    try:
        _album, songs = api.album_songs(int(album_id))
        return songs
    except NeteaseApiError as exc:
        logger.warning("album_songs failed for %s: %s", album_id, exc)
        return []


def search_rows(keyword: str, stype: str = "song", limit: int = 20) -> list[dict]:
    """搜索归一化行：song -> 搜索行（duration 秒）；album -> 原生专辑对象。"""
    if not keyword.strip():
        return []
    try:
        if stype == "album":
            return api.search_albums(keyword, limit)
        if stype == "playlist":
            return api.search_playlists(keyword, limit)
        if stype == "artist":
            return api.search_artists(keyword, limit)
        return [api.song_search_row(s, s.get("privilege")) for s in api.search_songs(keyword, limit)]
    except NeteaseApiError as exc:
        logger.warning("search_rows failed (%s/%s): %s", stype, keyword, exc)
        return []


def recommend_rows(limit: int) -> list[dict]:
    """每日推荐 -> 详情行（已过滤不可播）；后端未提供时回落平台通用推荐。"""
    songs: list[dict] = []
    try:
        songs = api.recommend_daily()
    except NeteaseApiError as exc:
        logger.debug("recommend_daily failed: %s", exc)
    if not songs:
        try:
            songs = api.personalized_newsong(limit)
        except NeteaseApiError as exc:
            logger.debug("personalized_newsong failed: %s", exc)
    ids = [api._int(s.get("id")) for s in songs if api._int(s.get("id")) > 0]
    return batch_song_details(ids[: max(1, int(limit))])


def toplist_list() -> list[dict]:
    """网易榜单列表（index 基准与 musicbox toplist 一致）。"""
    try:
        return api.toplists()
    except NeteaseApiError as exc:
        logger.warning("toplist_list failed: %s", exc)
        return []


def toplist_rows(index: int, limit: int) -> list[dict]:
    """按榜单下标取可播曲目（详情行）。下标越界回落热歌榜。"""
    charts = toplist_list()
    if not charts:
        return []
    idx = int(index)
    if idx < 0 or idx >= len(charts):
        logger.warning("toplist index %s out of range (0..%d), fallback to hot chart", idx, len(charts) - 1)
        idx = 0
    chart_id = api._int(charts[idx].get("id"))
    if chart_id <= 0:
        return []
    try:
        songs = api.playlist_track_all(chart_id, max(1, int(limit)))
    except NeteaseApiError as exc:
        logger.warning("toplist_rows failed (%s): %s", chart_id, exc)
        return []
    ids = [api._int(s.get("id")) for s in songs if api._int(s.get("id")) > 0]
    return batch_song_details(ids[: max(1, int(limit))])


# 分类歌单聚合：每个分类取前 N 个热门歌单，聚合其曲目后再统一做可播过滤。
# N 越大覆盖面越广（不同歌单去重后曲量更大），但后端请求数也越多。
CATEGORY_PLAYLIST_COUNT = 10


def category_rows(cat: str, limit: int) -> list[dict]:
    """歌单聚合曲目 -> 详情行（已过滤不可播），供代理构建「华语/流行…」及每日推荐扩容。

    cat 为空表示不限分类（/top/playlist 不带 cat=全部热门歌单），用于把每日推荐
    从原生 ~40 首补到 200+。
    流程：/top/playlist?cat=<分类> 取热门歌单 -> 逐个取全部曲目 -> 按 ID 去重 ->
    批量详情（内部含可播过滤）。任一歌单失败只跳过它，不影响整体。
    """
    name = str(cat or "").strip()
    want = max(1, int(limit))
    try:
        playlists = api.top_playlists(name, CATEGORY_PLAYLIST_COUNT)
    except NeteaseApiError as exc:
        logger.warning("top_playlists failed (%s): %s", name or "全部", exc)
        return []
    if not playlists:
        return []
    ids: list[int] = []
    seen: set[int] = set()
    for pl in playlists:
        pid = api._int(pl.get("id"))
        if pid <= 0:
            continue
        try:
            songs = api.playlist_track_all(pid, want)
        except NeteaseApiError as exc:
            logger.debug("playlist_track_all failed (%s): %s", pid, exc)
            continue
        for s in songs:
            sid = api._int(s.get("id"))
            if sid > 0 and sid not in seen:
                seen.add(sid)
                ids.append(sid)
        if len(ids) >= want * 2:
            break
    if not ids:
        return []
    # 多取一些候选以抵消可播过滤带来的损耗，最终仍截到 want 首
    rows = batch_song_details(ids[: max(want, int(want * 1.5))])
    return rows[:want]


def search_web_fallback(keyword: str, stype: str = "song", limit: int = 20) -> list[dict]:
    """旧接口名保留：现直接走同一后端搜索（不再访问 music.163.com web 接口）。"""
    rows = search_rows(keyword, stype, limit)
    if stype == "song":
        return [
            {
                "song_id": r["song_id"],
                "id": r["song_id"],
                "song_name": r["song_name"],
                "title": r["song_name"],
                "name": r["song_name"],
                "artist": r["artist"],
                "album_name": r["album_name"],
                "album": r["album_name"],
                "duration": r["duration"],
                "quality": r.get("quality") or "",
            }
            for r in rows
        ]
    return rows


# --------------------------------------------------------------- 用户账号歌单 --

def user_playlists(limit: int = 100) -> "dict[str, Any] | None":
    """后端无用户账号，恒返回 None -> 代理判为「未登录」并跳过该功能。"""
    return None


def playlist_track_ids(playlist_id: int) -> "list[int] | None":
    """用户账号歌单曲目不可用（无用户账号），返回 None。"""
    return None