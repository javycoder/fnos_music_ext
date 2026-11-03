"""多排行榜歌单：酷狗 + 网易云热门榜单生成飞牛在线歌单。

提供 20 个酷狗榜单（TOP500、国潮音乐榜、网络热歌榜等）与 18 个网易云官方榜单
（飙升榜、新歌榜、古典榜、电音榜等）：自动拉取、缓存并对齐飞牛前端数据格式。

参考二开分支（zouclang/fnos_music_ext）的榜单实现移植而来，保留其稳定做法：
  - 内存 + 磁盘双层缓存，磁盘按天落盘、无当天文件时回落最近历史缓存（绝不丢数据）；
  - 启动即把磁盘缓存读回内存并建索引（preload_local_caches），0 毫秒极速恢复；
  - 后台平滑预热（warmup_charts_background），冷启动进列表不再空等。
曲目 source 对齐本仓库音源命名：酷狗榜走 lx（online:lx:kg:<hash>），网易榜在
启用网易系音源（musicbox 或 neteasefree）时走网易系（online:netease:<sid>），
否则回落 lx。七档音质路由：网易系走七档白名单，lx 走阶梯，musicdl 保持探针降级。
校验统一用 standard 最低成本档，与目标档解耦。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime
from typing import Any

import httpx

logger = logging.getLogger("fnmusic_proxy.charts")

CHART_GUID_PREFIX = "online:playlist:chart:"
DEFAULT_UA_MOBILE = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
DEFAULT_UA_PC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# 酷狗音乐榜单列表（严格对齐主流热门排行榜）
KG_CHARTS: list[dict[str, Any]] = [
    {"id": "kg_8888", "name": "TOP500", "source": "kg", "rankid": 8888, "cover": "http://imge.kugou.com/mcommon/400/20241219/20241219164209670219.png"},
    {"id": "kg_52144", "name": "国潮音乐榜", "source": "kg", "rankid": 85897, "cover": "http://imge.kugou.com/mcommon/400/20241120/20241120202644296668.jpg"},
    {"id": "kg_52767", "name": "视频号热歌酷狗榜", "source": "kg", "rankid": 100530, "cover": "http://imge.kugou.com/mcommon/400/20260701/20260701213145443732.jpg"},
    {"id": "kg_31313", "name": "民谣榜", "source": "kg", "rankid": 51341, "cover": "http://imge.kugou.com/mcommon/400/20241211/20241211184719475668.jpg"},
    {"id": "kg_33161", "name": "纯音乐榜", "source": "kg", "rankid": 59900, "cover": "http://imge.kugou.com/mcommon/400/20241211/20241211185324343605.jpg"},
    {"id": "kg_33162", "name": "电音榜", "source": "kg", "rankid": 33160, "cover": "http://imge.kugou.com/mcommon/400/20241211/20241211184628349205.jpg"},
    {"id": "kg_23784", "name": "网络热歌榜", "source": "kg", "rankid": 82831, "cover": "http://imge.kugou.com/mcommon/400/20241219/20241219195626644029.png"},
    {"id": "kg_6666", "name": "飙升榜", "source": "kg", "rankid": 6666, "cover": "http://imge.kugou.com/mcommon/400/20241219/20241219193628550054.png"},
    {"id": "kg_52055", "name": "短视频热歌榜", "source": "kg", "rankid": 52144, "cover": "http://imge.kugou.com/mcommon/400/20260604/20260604150723458910.png"},
    {"id": "kg_46908", "name": "摇滚榜", "source": "kg", "rankid": 59896, "cover": "http://imge.kugou.com/mcommon/400/20241211/20241211184944114279.jpg"},
    {"id": "kg_24971", "name": "DJ热歌榜", "source": "kg", "rankid": 24971, "cover": "http://imge.kugou.com/mcommon/400/20241219/20241219194419154116.png"},
    {"id": "kg_54884", "name": "国乐榜", "source": "kg", "rankid": 80025, "cover": "http://imge.kugou.com/mcommon/400/20241211/20241211184535924138.png"},
    {"id": "kg_52054", "name": "百万收藏榜", "source": "kg", "rankid": 85432, "cover": "http://imge.kugou.com/mcommon/400/20241219/20241219171042182683.png"},
    {"id": "kg_59717", "name": "短视频收藏人气榜", "source": "kg", "rankid": 52767, "cover": "http://imge.kugou.com/mcommon/400/20260702/20260702101133223934.png"},
    {"id": "kg_24306", "name": "新歌榜", "source": "kg", "rankid": 74534, "cover": "http://imge.kugou.com/mcommon/400/20241219/20241219200509300768.png"},
    {"id": "kg_52895", "name": "名品堂", "source": "kg", "rankid": 84235, "cover": "http://imge.kugou.com/mcommon/400/20241219/20241219195054422404.png"},
    {"id": "kg_31308", "name": "内地榜", "source": "kg", "rankid": 31308, "cover": "http://imge.kugou.com/mcommon/400/20241211/20241211192146592398.png"},
    {"id": "kg_33163", "name": "粤语金曲榜", "source": "kg", "rankid": 33165, "cover": "http://imge.kugou.com/mcommon/400/20241219/20241219200119140556.png"},
    {"id": "kg_31310", "name": "欧美榜", "source": "kg", "rankid": 31310, "cover": "http://imge.kugou.com/mcommon/400/20241211/20241211192454699571.jpg"},
    {"id": "kg_30972", "name": "伤感榜", "source": "kg", "rankid": 51340, "cover": "http://imge.kugou.com/mcommon/400/20241219/20241219195213930623.png"},
]

# 网易云音乐榜单列表（严格对齐网易云官方 Toplist，实时 CDN 直链）
WY_CHARTS: list[dict[str, Any]] = [
    {"id": "wy_19723756", "name": "飙升榜", "source": "wy", "toplist_id": 19723756, "cover": "https://p2.music.126.net/rIi7Qzy2i2Y_1QD7cd0MYA==/109951170048506929.jpg"},
    {"id": "wy_3779629", "name": "新歌榜", "source": "wy", "toplist_id": 3779629, "cover": "https://p1.music.126.net/5guhqPBTcIrrhLBotgaT6w==/109951170048511751.jpg"},
    {"id": "wy_2884035", "name": "原创榜", "source": "wy", "toplist_id": 2884035, "cover": "https://p1.music.126.net/BaP9nrocNTL3gGThysv4eQ==/109951170091896587.jpg"},
    {"id": "wy_3778678", "name": "热歌榜", "source": "wy", "toplist_id": 3778678, "cover": "https://p1.music.126.net/0SUEG8yDACfx0Bw2MYFv4Q==/109951170048519512.jpg"},
    {"id": "wy_71385702", "name": "网易云古典榜", "source": "wy", "toplist_id": 71385702, "cover": "https://p1.music.126.net/na1kEeCS1iZEkzOrs9r_9g==/109951167976973667.jpg"},
    {"id": "wy_1978921795", "name": "网易云电音榜", "source": "wy", "toplist_id": 1978921795, "cover": "https://p1.music.126.net/hXGObvXfsGtFjFvRhOYAkA==/109951170091888741.jpg"},
    {"id": "wy_991319590", "name": "网易云中文说唱榜", "source": "wy", "toplist_id": 991319590, "cover": "https://p1.music.126.net/GgHbgDfGXHpE2YTchU7IvA==/109951171510498108.jpg"},
    {"id": "wy_5338990334", "name": "实时分享榜", "source": "wy", "toplist_id": 5338990334, "cover": "https://p1.music.126.net/Mi4QPklg1mtbWAfq74tEqQ==/109951165498334721.jpg"},
    {"id": "wy_21845217", "name": "网易云全球说唱榜", "source": "wy", "toplist_id": 21845217, "cover": "https://p1.music.126.net/5wDP78s43ydVTKt62C8OjQ==/109951165613100063.jpg"},
    {"id": "wy_60198", "name": "潮流风向榜", "source": "wy", "toplist_id": 60198, "cover": "https://p1.music.126.net/rwRsVIJHQ68gglhA6TNEYA==/109951165611413732.jpg"},
    {"id": "wy_5059632704", "name": "音乐合伙人推荐榜", "source": "wy", "toplist_id": 5059632704, "cover": "https://p2.music.126.net/fFQA72TD6CR-xuaINAGzEw==/109951164631802043.jpg"},
    {"id": "wy_5059642708", "name": "音乐合伙人热歌榜", "source": "wy", "toplist_id": 5059642708, "cover": "https://p2.music.126.net/kTJC5OBhg8I477X_ZmXyDQ==/109951168539740982.jpg"},
    {"id": "wy_5059644681", "name": "音乐合伙人留名榜", "source": "wy", "toplist_id": 5059644681, "cover": "https://p1.music.126.net/YFBFNI2F-4BveUpv6FKFuw==/109951167430864069.jpg"},
    {"id": "wy_5312894314", "name": "音乐合伙人高分新歌榜", "source": "wy", "toplist_id": 5312894314, "cover": "https://p2.music.126.net/QmKJczFCnfzVguxnFvxP2g==/109951166294017311.jpg"},
    {"id": "wy_5312895267", "name": "音乐合伙人高分榜", "source": "wy", "toplist_id": 5312895267, "cover": "https://p2.music.126.net/sfcWaSciUmnfxLYoI19uZQ==/4453022092493067.jpg"},
    {"id": "wy_5453912201", "name": "黑胶VIP爱听榜", "source": "wy", "toplist_id": 5453912201, "cover": "https://p2.music.126.net/qo6-o9n5AhMjNyejev38-A==/109951169743111905.jpg"},
    {"id": "wy_71384707", "name": "网易云ACG榜", "source": "wy", "toplist_id": 71384707, "cover": "https://p1.music.126.net/urByD_AmfBDBrs7fA9-O8A==/109951167976973225.jpg"},
    {"id": "wy_745956260", "name": "网易云韩语榜", "source": "wy", "toplist_id": 745956260, "cover": "https://p2.music.126.net/5oN9YaFznwNGXkmi8i2Ytw==/109951167430864741.jpg"},
]

ALL_CHARTS: list[dict[str, Any]] = KG_CHARTS + WY_CHARTS
_CHART_MAP: dict[str, dict[str, Any]] = {c["id"]: c for c in ALL_CHARTS}

# 内存快速缓存，避免高频并发重复拉取
_MEM_CACHE: dict[str, tuple[float, list[dict]]] = {}
_FETCH_LOCKS: dict[str, asyncio.Lock] = {}
_CHART_COVERS: dict[str, str] = {}
_CHART_TRACKS_BY_GUID: dict[str, dict] = {}


def _index_tracks(tracks: list[dict]) -> None:
    """把榜单曲目建立全索引（id / online:id / online:src:id），供封面和播放极速命中"""
    for t in tracks or []:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "")
        src = str(t.get("source") or "lx")
        if tid:
            _CHART_TRACKS_BY_GUID[tid] = t
            if not tid.startswith("online:"):
                _CHART_TRACKS_BY_GUID[f"online:{tid}"] = t
                if ":" not in tid:
                    _CHART_TRACKS_BY_GUID[f"online:{src}:{tid}"] = t


def find_track(guid: str) -> dict | None:
    """根据 online guid 查找榜单曲目元数据（含封面 cover_url 等）"""
    if not guid:
        return None
    if guid in _CHART_TRACKS_BY_GUID:
        return _CHART_TRACKS_BY_GUID[guid]
    if guid.startswith("online:"):
        stripped = guid[len("online:"):]
        if stripped in _CHART_TRACKS_BY_GUID:
            return _CHART_TRACKS_BY_GUID[stripped]
    day = _today()
    for cid in list(_CHART_MAP.keys()):
        if cid not in _MEM_CACHE:
            load_chart_cache(cid, day)
        if guid in _CHART_TRACKS_BY_GUID:
            return _CHART_TRACKS_BY_GUID[guid]
    return None


def chart_guid(chart_id: str) -> str:
    return f"{CHART_GUID_PREFIX}{chart_id}"


def is_chart_guid(guid: str | None) -> bool:
    return bool(guid and str(guid).startswith(CHART_GUID_PREFIX))


def chart_id_from_guid(guid: str | None) -> str:
    s = str(guid or "")
    if s.startswith(CHART_GUID_PREFIX):
        return s[len(CHART_GUID_PREFIX):]
    return ""


def chart_meta(chart_id: str) -> dict[str, Any] | None:
    return _CHART_MAP.get(chart_id)


def _cache_dir() -> str:
    d = os.environ.get("FNMUSIC_RECOMMEND_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "recommend_cache"
    )
    chart_dir = os.path.join(d, "charts")
    os.makedirs(chart_dir, exist_ok=True)
    return chart_dir


def _cache_file(chart_id: str, day: str) -> str:
    return os.path.join(_cache_dir(), f"{chart_id}_{day}.json")


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def load_chart_cache(chart_id: str, day: str) -> list[dict] | None:
    # 1. 查内存
    mem = _MEM_CACHE.get(chart_id)
    if mem:
        ts, data = mem
        # 缓存有效 4 小时
        if time.time() - ts < 14400 and data:
            _index_tracks(data)
            return data

    # 2. 查本地文件（优先查当天文件，无当天文件时查最新历史缓存兜底，绝不丢数据）
    target_path = _cache_file(chart_id, day)
    paths_to_try = [target_path]
    if not os.path.exists(target_path):
        try:
            cdir = _cache_dir()
            prefix = f"{chart_id}_"
            candidates = sorted(
                [os.path.join(cdir, fn) for fn in os.listdir(cdir) if fn.startswith(prefix) and fn.endswith(".json")],
                reverse=True
            )
            paths_to_try.extend(candidates)
        except Exception:
            pass

    for path in paths_to_try:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    tracks = data.get("tracks") or []
                    cov = data.get("cover")
                    if cov:
                        _CHART_COVERS[chart_id] = str(cov)
                    if tracks:
                        _MEM_CACHE[chart_id] = (time.time(), tracks)
                        _index_tracks(tracks)
                        return tracks
                elif isinstance(data, list) and data:
                    _MEM_CACHE[chart_id] = (time.time(), data)
                    _index_tracks(data)
                    return data
            except Exception as e:
                logger.warning("Failed to load chart cache from %s: %s", path, e)
    return None


def save_chart_cache(chart_id: str, day: str, tracks: list[dict], cover_url: str = "") -> None:
    _MEM_CACHE[chart_id] = (time.time(), tracks)
    if cover_url:
        _CHART_COVERS[chart_id] = cover_url
    _index_tracks(tracks)
    path = _cache_file(chart_id, day)
    tmp_path = path + f".tmp.{os.getpid()}"
    try:
        payload = {"cover": cover_url, "tracks": tracks}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp_path, path)
        _prune_chart_history(chart_id, keep=3)
    except Exception as e:
        logger.warning("Failed to save chart cache to %s: %s", path, e)
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _prune_chart_history(chart_id: str, keep: int = 3) -> None:
    """历史回退保留最近三份，超出删除最旧。"""
    try:
        cdir = _cache_dir()
        prefix = f"{chart_id}_"
        files = sorted(
            [os.path.join(cdir, fn) for fn in os.listdir(cdir)
             if fn.startswith(prefix) and fn.endswith(".json")],
            reverse=True,
        )
        for stale in files[max(keep, 0):]:
            try:
                os.remove(stale)
            except OSError:
                pass
    except Exception:
        pass


def purge_chart_caches() -> int:
    """切源与版本变化时统一失效：清内存并删除全部榜单磁盘缓存。"""
    removed = 0
    _MEM_CACHE.clear()
    _CHART_TRACKS_BY_GUID.clear()
    try:
        cdir = _cache_dir()
        for fn in os.listdir(cdir):
            if fn.endswith(".json"):
                try:
                    os.remove(os.path.join(cdir, fn))
                    removed += 1
                except OSError:
                    pass
    except Exception:
        pass
    return removed


def _is_dummy_cover(url: str) -> bool:
    if not url:
        return True
    return "123456" in url or "109951168172" in url


def get_chart_cover(chart_id: str) -> str:
    cov = _CHART_COVERS.get(chart_id)
    if cov and not _is_dummy_cover(cov):
        return cov
    meta = chart_meta(chart_id) or {}
    static_cov = str(meta.get("cover") or "")
    if static_cov and not _is_dummy_cover(static_cov):
        return static_cov
    day = _today()
    tracks = load_chart_cache(chart_id, day)
    cov = _CHART_COVERS.get(chart_id)
    if cov and not _is_dummy_cover(cov):
        return cov
    # 取第一首带有效封面的歌曲
    if tracks:
        for t in tracks:
            url = str(t.get("cover_url") or "")
            if url and "artistpicserver.kuwo.cn" not in url and not _is_dummy_cover(url):
                _CHART_COVERS[chart_id] = url
                return url
    return ""


async def fetch_kg_chart(client: httpx.AsyncClient, rankid: int, limit: int = 100) -> tuple[list[dict], str]:
    """拉取酷狗音乐排行榜单歌曲与封面（自动分页拼接，最多 limit 首，默认 100 首）"""
    url = "http://m.kugou.com/rank/info/"
    headers = {"User-Agent": DEFAULT_UA_MOBILE}
    tracks: list[dict] = []
    cover_url = ""
    seen_hashes: set[str] = set()
    page = 1
    max_pages = 5  # 酷狗每页 30 首，4 页可达 120 首，足以满足 100 首需求，5 页作为安全上限

    while len(tracks) < limit and page <= max_pages:
        params = {"rankid": rankid, "page": page, "json": "true"}
        try:
            r = await client.get(url, params=params, headers=headers, timeout=10.0)
            r.raise_for_status()
            res = r.json() or {}
            rows = (res.get("songs") or {}).get("list") or []
            if not rows:
                break
            if not cover_url:
                info = res.get("info") or {}
                cover_url = str(
                    info.get("banner_9") or info.get("banner_7") or info.get("imgurl") or ""
                ).replace("{size}", "400")
        except Exception as e:
            logger.warning("kg rank %s page %d fetch failed: %s", rankid, page, e)
            break

        added_in_page = 0
        for it in rows:
            if not isinstance(it, dict):
                continue
            fhash = str(it.get("hash") or "")
            if not fhash or fhash in seen_hashes:
                continue
            seen_hashes.add(fhash)
            authors = it.get("authors") or []
            singer = " / ".join(
                str(a.get("author_name") or "")
                for a in authors
                if isinstance(a, dict) and a.get("author_name")
            )
            title = str(it.get("songname") or it.get("filename") or "").replace(f"{singer} - ", "").strip()
            if not title:
                continue
            cover = str(it.get("album_sizable_cover") or "").replace("{size}", "480")
            tracks.append({
                "id": f"lx:kg:{fhash}",
                "source": "lx",
                "title": title,
                "artist": singer,
                "album": "",
                "duration_s": int(it.get("duration") or 0),
                "cover_url": cover,
                "ext": "mp3",
                "hash": fhash,
                "mixsongid": str(it.get("album_audio_id") or ""),
            })
            added_in_page += 1
            if len(tracks) >= limit:
                break

        # 当前页无新增有效歌曲或返回条目不足一页（30首），说明已到底
        if added_in_page == 0 or len(rows) < 30:
            break
        page += 1

    if not cover_url and tracks:
        cover_url = tracks[0].get("cover_url") or ""
    return tracks[:limit], cover_url


async def fetch_wy_chart(
    client: httpx.AsyncClient,
    toplist_id: int,
    netease_enabled: bool = False,
    neteasefree_enabled: bool = False,
    limit: int = 100
) -> tuple[list[dict], str]:
    """拉取网易云音乐排行榜单歌曲与封面"""
    url = "https://music.163.com/api/playlist/detail"
    params = {"id": toplist_id}
    headers = {
        "User-Agent": DEFAULT_UA_PC,
        "Referer": "https://music.163.com/",
        "Cookie": "os=pc; appver=9.1.15",
    }
    try:
        r = await client.get(url, params=params, headers=headers, timeout=12.0)
        r.raise_for_status()
        res = r.json() or {}
        result = res.get("result") or res.get("playlist") or {}
        tracks_data = result.get("tracks") or []
        cover_url = str(result.get("coverImgUrl") or result.get("picUrl") or "")
    except Exception as e:
        logger.warning("wy toplist %s fetch failed: %s", toplist_id, e)
        return [], ""

    tracks: list[dict] = []
    use_netease = bool(netease_enabled or neteasefree_enabled)
    source = "netease" if use_netease else "lx"
    for it in tracks_data:
        if not isinstance(it, dict):
            continue
        sid = str(it.get("id") or "")
        if not sid:
            continue
        title = str(it.get("name") or "").strip()
        artists = it.get("artists") or it.get("ar") or []
        artist = " / ".join(str(a.get("name") or "") for a in artists if isinstance(a, dict))
        album_data = it.get("album") or it.get("al") or {}
        album = str(album_data.get("name") or "") if isinstance(album_data, dict) else ""
        cover = str(album_data.get("picUrl") or "") if isinstance(album_data, dict) else ""
        duration_ms = it.get("duration") or it.get("dt") or 0

        item_id = sid if use_netease else f"lx:wy:{sid}"
        tracks.append({
            "id": item_id,
            "source": source,
            "title": title,
            "artist": artist,
            "album": album,
            "duration_s": float(duration_ms) / 1000.0,
            "cover_url": cover,
            "ext": "mp3",
            "song_id": sid,
        })
        if len(tracks) >= limit:
            break
    if not cover_url and tracks:
        cover_url = tracks[0].get("cover_url") or ""
    return tracks, cover_url


async def get_or_load_chart_tracks(
    chart_id: str,
    client: httpx.AsyncClient | None = None,
    netease_enabled: bool = False,
    neteasefree_enabled: bool = False,
    limit: int = 100
) -> list[dict]:
    """获取指定榜单的歌曲列表（优先缓存，无则抓取并落盘）"""
    meta = chart_meta(chart_id)
    if not meta:
        return []

    day = _today()
    cached = load_chart_cache(chart_id, day)
    if cached:
        return cached

    lock = _FETCH_LOCKS.setdefault(chart_id, asyncio.Lock())
    async with lock:
        cached = load_chart_cache(chart_id, day)
        if cached:
            return cached

        owned_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
            owned_client = True
        try:
            tracks: list[dict] = []
            cover_url = ""
            if meta["source"] == "kg":
                tracks, cover_url = await fetch_kg_chart(client, meta["rankid"], limit=limit)
            elif meta["source"] == "wy":
                tracks, cover_url = await fetch_wy_chart(
                    client, meta["toplist_id"],
                    netease_enabled=netease_enabled,
                    neteasefree_enabled=neteasefree_enabled,
                    limit=limit,
                )

            if tracks:
                save_chart_cache(chart_id, day, tracks, cover_url=cover_url)
            return tracks
        finally:
            if owned_client:
                await client.aclose()


def build_chart_playlist_record(chart_id: str, track_count: int = 0) -> dict[str, Any]:
    """构造飞牛前端所需的歌单元数据"""
    meta = chart_meta(chart_id) or {"name": "排行榜", "cover": ""}
    guid = chart_guid(chart_id)
    now = int(time.time())
    name = meta.get("name") or "排行榜"
    cover_url = get_chart_cover(chart_id)
    fake_id = hashlib.md5(f"fnmusic-ext::{guid}".encode()).hexdigest()
    return {
        "guid": guid,
        "name": name,
        "title": name,
        "coverUrl": cover_url,
        "coverId": f"track_{fake_id}",
        "createdAt": now,
        "updatedAt": now,
        "trackCount": track_count or 100,
        "isDaily": True,
    }


def list_enabled_charts(
    enable_charts: bool = True,
    enable_kg: bool = True,
    enable_wy: bool = True,
    custom_whitelist: "str | list[str] | set[str] | None" = None,
) -> list[dict[str, Any]]:
    """根据开关配置和自定义白名单返回要注入的榜单列表"""
    if not enable_charts:
        return []
    res: list[dict[str, Any]] = []
    if enable_kg:
        res.extend(KG_CHARTS)
    if enable_wy:
        res.extend(WY_CHARTS)

    wl_raw = custom_whitelist if custom_whitelist is not None else os.environ.get("FNMUSIC_ENABLED_CHARTS", "")
    if isinstance(wl_raw, str):
        wl = {x.strip() for x in wl_raw.split(",") if x.strip()}
    elif isinstance(wl_raw, (list, set)):
        wl = {str(x).strip() for x in wl_raw if str(x).strip()}
    else:
        wl = set()

    if wl:
        res = [c for c in res if c["id"] in wl]
    return res


def preload_local_caches() -> int:
    """服务启动时同步把磁盘上已有的所有榜单缓存读入内存并建立索引，0 毫秒极速恢复。"""
    loaded = 0
    day = _today()
    for cid in list(_CHART_MAP.keys()):
        try:
            tracks = load_chart_cache(cid, day)
            if tracks:
                loaded += 1
        except Exception:
            pass
    return loaded


async def warmup_charts_background(
    enabled_charts: list[dict[str, Any]],
    netease_enabled: bool = False,
    neteasefree_enabled: bool = False,
) -> None:
    """后台平滑预热所有榜单数据并落盘，防止冷启动时缓存未命中。"""
    if not enabled_charts:
        return
    day = _today()
    logger.info("Starting background chart warmup for %d charts", len(enabled_charts))
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            for c in enabled_charts:
                cid = c.get("id")
                if not cid:
                    continue
                # 若今日缓存已存在且在内存，跳过
                mem = _MEM_CACHE.get(cid)
                target_path = _cache_file(cid, day)
                if mem and os.path.exists(target_path):
                    continue
                try:
                    await get_or_load_chart_tracks(
                        cid, client=client,
                        netease_enabled=netease_enabled,
                        neteasefree_enabled=neteasefree_enabled,
                    )
                    await asyncio.sleep(0.5)  # 礼貌间隔，防突发网络流量
                except Exception as e:
                    logger.debug("Warmup chart %s failed: %s", cid, e)
        logger.info("Background chart warmup completed")
    except Exception as e:
        logger.warning("Background chart warmup failed: %s", e)