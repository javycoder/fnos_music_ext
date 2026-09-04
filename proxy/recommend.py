"""每日推荐：基于最近收听记录生成可播放在线歌单。

已登录用户始终注入「每日推荐」歌单。配置了 FNMUSIC_LLM_BASE_URL + FNMUSIC_LLM_API_KEY
时走大模型；否则按最近播放歌手降级检索。密钥只从环境变量读取，绝不写入 CONF / 日志 / 缓存。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from typing import Any
from uuid import uuid4

import httpx

logger = logging.getLogger("fnmusic_proxy.recommend")

DAILY_GUID_PREFIX = "online:playlist:daily:"
DEFAULT_MODEL = "gpt-4o-mini"
SEED_LIMIT = 20
LLM_CANDIDATE_COUNT = 30
PLAYLIST_SIZE = 20
RECOMMEND_COUNT = PLAYLIST_SIZE  # 兼容旧引用
LLM_TIMEOUT_S = 20.0
BUILD_BUDGET_S = 25.0

_CJK = re.compile(r"[\u4e00-\u9fff]")
_HIRA_KATA = re.compile(r"[\u3040-\u30ff]")
_HANGUL = re.compile(r"[\uac00-\ud7af]")
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")
_LATIN = re.compile(r"[A-Za-z]")
_JSON_BLOCK = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_JSON_ARRAY = re.compile(r"\[[\s\S]*\]")


def home_dir() -> str:
    env = (os.environ.get("FNMUSIC_HOME") or "").strip()
    if env:
        return env
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def llm_api_key() -> str:
    return (os.environ.get("FNMUSIC_LLM_API_KEY") or "").strip()


def llm_base_url() -> str:
    return (os.environ.get("FNMUSIC_LLM_BASE_URL") or "").strip().rstrip("/")


def llm_model() -> str:
    return (os.environ.get("FNMUSIC_LLM_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL


def llm_enabled() -> bool:
    return bool(llm_base_url() and llm_api_key())


def recommend_cache_dir() -> str:
    return os.environ.get("FNMUSIC_RECOMMEND_DIR") or os.path.join(home_dir(), "recommend_cache")


def play_history_dir() -> str:
    return os.environ.get("FNMUSIC_PLAY_HISTORY_DIR") or os.path.join(home_dir(), "play_history")


def music_db_path() -> str:
    return os.environ.get(
        "FNMUSIC_MUSIC_DB", "/usr/local/apps/@appdata/trim.music/db/music.db"
    )


def today_key(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d")


def daily_playlist_guid(day: str | None = None, user_guid: str = "") -> str:
    day = day or today_key()
    suffix = re.sub(r"[^A-Za-z0-9]", "", user_guid)[:12]
    if suffix:
        return f"{DAILY_GUID_PREFIX}{day}:{suffix}"
    return f"{DAILY_GUID_PREFIX}{day}"


def is_daily_playlist_guid(guid: str | None) -> bool:
    return str(guid or "").startswith(DAILY_GUID_PREFIX)


def infer_language(title: str = "", artist: str = "", album: str = "") -> str:
    text = f"{title} {artist} {album}"
    if _HIRA_KATA.search(text):
        return "日语"
    if _HANGUL.search(text):
        return "韩语"
    if _CYRILLIC.search(text):
        return "俄语"
    if _CJK.search(text):
        return "中文"
    if _LATIN.search(text):
        return "英语"
    return "未知"


def _safe_user_name(user_guid: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9\-_]", "_", str(user_guid or "").strip())
    return safe or "shared"


def _atomic_write_json(path: str, payload: Any) -> bool:
    parent = os.path.dirname(path) or "."
    part = f"{path}.{uuid4().hex[:8]}.part"
    try:
        os.makedirs(parent, exist_ok=True)
        with open(part, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(part, path)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return True
    except Exception as e:
        logger.warning("failed to write %s: %s", path, e)
        if os.path.exists(part):
            try:
                os.remove(part)
            except Exception:
                pass
        return False


def load_online_play_history(user_guid: str) -> list[dict]:
    path = os.path.join(play_history_dir(), f"{_safe_user_name(user_guid)}.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return [x for x in data["items"] if isinstance(x, dict)]
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except Exception as e:
        logger.warning("failed to load play history for %s: %s", user_guid, e)
    return []


def save_online_play_history(user_guid: str, items: list[dict]) -> bool:
    path = os.path.join(play_history_dir(), f"{_safe_user_name(user_guid)}.json")
    return _atomic_write_json(path, {"items": items[-500:]})


def record_online_play(user_guid: str, guid: str, track: dict | None = None) -> None:
    if not guid:
        return
    now = int(time.time())
    items = load_online_play_history(user_guid)
    items = [it for it in items if it.get("guid") != guid]
    snapshot = dict(track or {})
    snapshot.setdefault("guid", guid)
    items.append({"guid": guid, "playedAt": now, "track": snapshot})
    save_online_play_history(user_guid, items)


def user_id_from_guid(db_path: str, user_guid: str) -> int | None:
    if not db_path or not os.path.exists(db_path) or not user_guid:
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT id FROM user WHERE guid = ? LIMIT 1", (user_guid,)).fetchone()
            return int(row[0]) if row else None
        finally:
            con.close()
    except Exception as e:
        logger.warning("failed to map user guid: %s", e)
        return None


def read_local_recent_tracks(db_path: str, user_guid: str, limit: int = SEED_LIMIT) -> list[dict]:
    """只读飞牛 play_history，不写官方库。"""
    uid = user_id_from_guid(db_path, user_guid)
    if uid is None:
        return []
    sql = """
        SELECT t.guid, t.title, t.year,
               ph.play_count, ph.updated_at,
               (SELECT GROUP_CONCAT(a.name, '/')
                  FROM track_artist ta JOIN artist a ON a.id = ta.artist_id
                 WHERE ta.track_id = t.id) AS artists,
               (SELECT GROUP_CONCAT(g.name, '/')
                  FROM track_genre tg JOIN genre g ON g.id = tg.genre_id
                 WHERE tg.track_id = t.id) AS genres,
               al.name AS album
          FROM play_history ph
          JOIN track t ON t.id = ph.track_id
          LEFT JOIN album al ON al.id = t.album_id
         WHERE ph.user_id = ?
         ORDER BY ph.updated_at DESC
         LIMIT ?
    """
    out: list[dict] = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(sql, (uid, limit)).fetchall()
        finally:
            con.close()
    except Exception as e:
        logger.warning("failed to read local play_history: %s", e)
        return []
    for guid, title, year, play_count, updated_at, artists, genres, album in rows:
        artist = str(artists or "")
        title_s = str(title or "")
        album_s = str(album or "")
        genre_s = str(genres or "")
        out.append({
            "guid": str(guid or ""),
            "title": title_s,
            "artist": artist,
            "album": album_s,
            "genre": genre_s,
            "year": year,
            "language": infer_language(title_s, artist, album_s),
            "play_count": play_count or 1,
            "playedAt": _coerce_ts(updated_at),
            "source": "local",
        })
    return out


def read_local_favorite_tracks(db_path: str, user_guid: str, limit: int = 200) -> list[dict]:
    """只读飞牛 favorite_track，不写官方库。"""
    uid = user_id_from_guid(db_path, user_guid)
    if uid is None:
        return []
    sql = """
        SELECT t.guid, t.title,
               (SELECT GROUP_CONCAT(a.name, '/')
                  FROM track_artist ta JOIN artist a ON a.id = ta.artist_id
                 WHERE ta.track_id = t.id) AS artists,
               al.name AS album
          FROM favorite_track ft
          JOIN track t ON t.id = ft.track_id
          LEFT JOIN album al ON al.id = t.album_id
         WHERE ft.user_id = ?
         ORDER BY ft.updated_at DESC
         LIMIT ?
    """
    out: list[dict] = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(sql, (uid, limit)).fetchall()
        finally:
            con.close()
    except Exception as e:
        logger.warning("failed to read local favorite_track: %s", e)
        return []
    for guid, title, artists, album in rows:
        title_s = str(title or "")
        artist = str(artists or "")
        album_s = str(album or "")
        out.append({
            "guid": str(guid or ""),
            "title": title_s,
            "artist": artist,
            "album": album_s,
            "source": "favorite-local",
        })
    return out


def _item_title_artist(item: dict) -> tuple[str, str]:
    track = item.get("track") if isinstance(item.get("track"), dict) else item
    title = str(track.get("title") or track.get("name") or "").strip()
    artist = str(track.get("artist") or "").strip()
    if not artist:
        artists = track.get("artists")
        if isinstance(artists, list) and artists:
            first = artists[0]
            artist = str(first.get("name") if isinstance(first, dict) else first).strip()
    return title, artist


def identity_key(title: str, artist: str) -> tuple[str, str]:
    return (title.strip().lower(), artist.strip().lower())


def collect_exclude_sets(*groups: list[dict] | None) -> tuple[set[str], set[tuple[str, str]]]:
    guids: set[str] = set()
    tas: set[tuple[str, str]] = set()
    for group in groups:
        for it in group or []:
            if not isinstance(it, dict):
                continue
            guid = str(it.get("guid") or "")
            if guid:
                guids.add(guid)
            nested = it.get("track") if isinstance(it.get("track"), dict) else None
            if nested:
                ng = str(nested.get("guid") or "")
                if ng:
                    guids.add(ng)
            title, artist = _item_title_artist(it)
            key = identity_key(title, artist)
            if key != ("", ""):
                tas.add(key)
    return guids, tas


def _coerce_ts(value: Any) -> int:
    if isinstance(value, (int, float)):
        n = int(value)
        return n // 1000 if n > 10_000_000_000 else n
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except (TypeError, ValueError):
        pass
    try:
        normalized = text.replace("Z", "+00:00")
        if re.search(r"[+-]\d{2}:\d{2}$", normalized):
            pass
        elif re.search(r"[+-]\d{4}$", normalized):
            normalized = normalized[:-2] + ":" + normalized[-2:]
        return int(datetime.fromisoformat(normalized).timestamp())
    except Exception:
        return 0


def seeds_from_online_history(user_guid: str) -> list[dict]:
    items = load_online_play_history(user_guid)
    out: list[dict] = []
    for it in reversed(items):
        track = it.get("track") if isinstance(it.get("track"), dict) else {}
        title = str(track.get("title") or "")
        artist = str(track.get("artist") or "")
        if not artist:
            artists = track.get("artists")
            if isinstance(artists, list) and artists:
                first = artists[0]
                artist = str(first.get("name") if isinstance(first, dict) else first)
        album = str(track.get("albumName") or "")
        if not album and isinstance(track.get("album"), dict):
            album = str(track["album"].get("name") or "")
        genres = track.get("genres") if isinstance(track.get("genres"), list) else []
        genre = "/".join(str(g) for g in genres if g)
        out.append({
            "guid": str(it.get("guid") or track.get("guid") or ""),
            "title": title,
            "artist": artist,
            "album": album,
            "genre": genre,
            "language": infer_language(title, artist, album),
            "playedAt": int(it.get("playedAt") or 0),
            "source": "online",
        })
    return out


def merge_recent_seeds(
    local_seeds: list[dict],
    online_seeds: list[dict],
    extra: list[dict] | None = None,
    limit: int = SEED_LIMIT,
) -> list[dict]:
    merged = list(online_seeds) + list(local_seeds) + list(extra or [])
    merged.sort(key=lambda x: int(x.get("playedAt") or 0), reverse=True)
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for it in merged:
        key = (
            str(it.get("title") or "").strip().lower(),
            str(it.get("artist") or "").strip().lower(),
        )
        if key == ("", ""):
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
        if len(out) >= limit:
            break
    return out


def build_llm_prompt(
    play_seeds: list[dict],
    favorite_seeds: list[dict],
    count: int = LLM_CANDIDATE_COUNT,
) -> str:
    def _block(items: list[dict], empty: str) -> str:
        lines = []
        for i, s in enumerate(items, 1):
            lines.append(
                f"{i}. 《{s.get('title') or ''}》 / {s.get('artist') or '未知'} / "
                f"专辑:{s.get('album') or '-'} / "
                f"语言:{s.get('language') or infer_language(str(s.get('title') or ''), str(s.get('artist') or ''))} / "
                f"曲风:{s.get('genre') or '-'} / 来源:{s.get('source') or '-'}"
            )
        return "\n".join(lines) if lines else empty

    history_block = _block(play_seeds, "（暂无最近播放）")
    fav_block = _block(favorite_seeds, "（暂无收藏）")
    return f"""你是音乐推荐引擎。根据用户最近收听和收藏口味，生成 {count} 首可在线检索的「每日推荐」候选。

必须同时覆盖这些维度（每首标注 dimension）：
- artist：同歌手或高度相关合作歌手的其他作品
- language：同一语种的其他歌手
- genre：相同或相邻曲风（流行/摇滚/民谣/电子/古风/R&B 等）
- type：歌曲类型气质（抒情、热血、怀旧、舞曲、ACG 等）

约束：
1. 不要推荐用户已经收藏或最近听过的「歌名+歌手」组合。
2. 不要推荐仅存在于用户本地曲库、网上很难搜到的冷门私货。
3. 结果要多样：四个 dimension 都要出现，避免全是同一歌手。
4. 必须正好输出 {count} 首。
5. 只输出 JSON 数组，不要 markdown，不要解释。
6. 每项字段：title, artist, album, language, genre, type, dimension, reason。

最近收听：
{history_block}

收藏：
{fav_block}
"""


def parse_llm_recommendations(text: str) -> list[dict]:
    raw = (text or "").strip()
    if not raw:
        return []
    candidates: list[str] = []
    fenced = _JSON_BLOCK.search(raw)
    if fenced:
        candidates.append(fenced.group(1).strip())
    arr = _JSON_ARRAY.search(raw)
    if arr:
        candidates.append(arr.group(0))
    candidates.append(raw)
    data = None
    for c in candidates:
        try:
            data = json.loads(c)
            break
        except Exception:
            continue
    if isinstance(data, dict):
        for key in ("recommendations", "songs", "tracks", "items", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for it in data:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or it.get("name") or it.get("song") or "").strip()
        artist = str(it.get("artist") or it.get("singer") or it.get("singers") or "").strip()
        if not title:
            continue
        key = (title.lower(), artist.lower())
        if key in seen:
            continue
        seen.add(key)
        dim = str(it.get("dimension") or "").strip().lower()
        if dim not in ("artist", "language", "genre", "type"):
            dim = "genre"
        out.append({
            "title": title,
            "artist": artist,
            "album": str(it.get("album") or "").strip(),
            "language": str(it.get("language") or infer_language(title, artist)).strip(),
            "genre": str(it.get("genre") or "").strip(),
            "type": str(it.get("type") or "").strip(),
            "dimension": dim,
            "reason": str(it.get("reason") or "").strip(),
        })
    return out


async def call_llm(http_client: httpx.AsyncClient, prompt: str) -> list[dict]:
    base = llm_base_url()
    key = llm_api_key()
    if not base or not key:
        return []
    url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
    payload = {
        "model": llm_model(),
        "temperature": 0.8,
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": "你只输出合法 JSON 数组，不要 markdown。"},
            {"role": "user", "content": prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    try:
        resp = await http_client.post(url, json=payload, headers=headers, timeout=LLM_TIMEOUT_S)
        if resp.status_code >= 400:
            logger.warning("llm http %s", resp.status_code)
            return []
        body = resp.json()
        content = ""
        if isinstance(body, dict):
            choices = body.get("choices") or []
            if choices and isinstance(choices[0], dict):
                msg = choices[0].get("message") or {}
                content = str(msg.get("content") or "")
        return parse_llm_recommendations(content)
    except Exception as e:
        logger.warning("llm call failed: %s", e)
        return []


def _match_score(item: dict, title: str, artist: str) -> int:
    it = str(item.get("title") or item.get("name") or "").strip().lower()
    ia = str(item.get("artist") or "").strip().lower()
    t = title.strip().lower()
    a = artist.strip().lower()
    score = 0
    if t and it == t:
        score += 12
    elif t and (t in it or it in t):
        score += 6
    if a and ia == a:
        score += 10
    elif a and (a in ia or ia in a):
        score += 5
    return score


async def _search_keyword(
    keyword: str,
    musicdl_client: httpx.AsyncClient | None,
    musicbox_client: httpx.AsyncClient | None,
    netease_enabled: bool,
) -> list[dict]:
    async def _mb() -> list[dict]:
        if not (netease_enabled and musicbox_client):
            return []
        try:
            r = await musicbox_client.get(
                "/api/v1/search",
                params={"keyword": keyword, "limit": 5, "type": "song"},
                timeout=8.0,
            )
            if r.status_code != 200:
                return []
            data = r.json()
            rows = data.get("data") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                return []
            out: list[dict] = []
            for it in rows:
                if not isinstance(it, dict):
                    continue
                sid = str(it.get("song_id") or it.get("id") or "")
                if not sid:
                    continue
                quality = str(it.get("quality") or "").upper()
                dur = float(it.get("duration") or 0) or 0
                if dur > 10_000:
                    dur = dur / 1000.0
                out.append({
                    "id": f"netease:{sid}",
                    "source": "netease",
                    "title": str(it.get("song_name") or it.get("title") or ""),
                    "artist": str(it.get("artist") or ""),
                    "album": str(it.get("album_name") or it.get("album") or ""),
                    "duration_s": dur,
                    "ext": "flac" if any(q in quality for q in ("SQ", "HR", "无损")) else "mp3",
                })
            return out
        except Exception as e:
            logger.debug("musicbox recommend search failed: %s", e)
            return []

    async def _mdl() -> list[dict]:
        if not musicdl_client:
            return []
        try:
            r = await musicdl_client.get(
                "/search",
                params={"keyword": keyword, "limit": 5},
                timeout=8.0,
            )
            if r.status_code != 200:
                return []
            data = r.json()
            rows = data.get("items") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                return []
            return [x for x in rows if isinstance(x, dict)]
        except Exception as e:
            logger.debug("musicdl recommend search failed: %s", e)
            return []

    jobs = [asyncio.create_task(coro) for coro in (_mb(), _mdl())]
    try:
        for fut in asyncio.as_completed(jobs):
            try:
                items = await fut
            except Exception:
                continue
            if items:
                return items
        return []
    finally:
        for t in jobs:
            if not t.done():
                t.cancel()


async def resolve_recommendations(
    recs: list[dict],
    musicdl_client: httpx.AsyncClient | None,
    musicbox_client: httpx.AsyncClient | None,
    netease_enabled: bool,
    build_track,
    limit: int = PLAYLIST_SIZE,
    exclude_guids: set[str] | None = None,
    exclude_ta: set[tuple[str, str]] | None = None,
) -> list[dict]:
    """把候选歌名检索成可播放的在线 Track，跳过已收藏，凑满 limit 首。"""
    skip_ids = set(exclude_guids or ())
    skip_ta = set(exclude_ta or ())
    out: list[dict] = []
    seen_ids: set[str] = set()
    seen_ta: set[tuple[str, str]] = set()
    sem = asyncio.Semaphore(6)

    def _excluded(guid: str, title: str, artist: str) -> bool:
        if guid and guid in skip_ids:
            return True
        key = identity_key(title, artist)
        return key != ("", "") and key in skip_ta

    async def one(rec: dict) -> list[dict]:
        title = rec.get("title") or ""
        artist = rec.get("artist") or ""
        keyword = " ".join(x for x in (artist, title) if x).strip() or title
        async with sem:
            items = await _search_keyword(keyword, musicdl_client, musicbox_client, netease_enabled)
        if not items and artist:
            async with sem:
                items = await _search_keyword(artist, musicdl_client, musicbox_client, netease_enabled)
        if not items:
            return []
        ranked = sorted(items, key=lambda it: _match_score(it, title, artist), reverse=True)
        want = 1 if title else 3
        found: list[dict] = []
        for pick in ranked:
            if not isinstance(pick, dict):
                continue
            pid = str(pick.get("id") or "")
            guid = f"online:{pid}" if pid and not str(pid).startswith("online:") else pid
            ptitle = str(pick.get("title") or pick.get("name") or "")
            partist = str(pick.get("artist") or "")
            if _excluded(guid, ptitle, partist) or _excluded(pid, ptitle, partist):
                continue
            track = build_track(pick)
            tg = str(track.get("guid") or guid)
            tt, ta = str(track.get("title") or ptitle), str(track.get("artist") or partist)
            if _excluded(tg, tt, ta):
                continue
            if rec.get("genre") and isinstance(track.get("genres"), list) and not track["genres"]:
                track["genres"] = [rec["genre"]]
            track["recommendDimension"] = rec.get("dimension") or ""
            track["recommendReason"] = rec.get("reason") or ""
            found.append(track)
            if len(found) >= want:
                break
        return found

    tasks = [asyncio.create_task(one(rec)) for rec in recs]
    try:
        for fut in asyncio.as_completed(tasks):
            try:
                tracks = await fut
            except Exception as e:
                logger.debug("resolve rec failed: %s", e)
                continue
            for track in tracks or []:
                guid = str(track.get("guid") or "")
                ta = identity_key(str(track.get("title") or ""), str(track.get("artist") or ""))
                if guid in seen_ids or guid in skip_ids:
                    continue
                if ta != ("", "") and (ta in seen_ta or ta in skip_ta):
                    continue
                seen_ids.add(guid)
                if ta != ("", ""):
                    seen_ta.add(ta)
                out.append(track)
                if len(out) >= limit:
                    break
            if len(out) >= limit:
                break
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    return out


_FALLBACK_POOL = [
    ("七里香", "周杰伦"), ("海阔天空", "Beyond"), ("Shape of You", "Ed Sheeran"),
    ("夜に駆ける", "YOASOBI"), ("Dynamite", "BTS"), ("告白气球", "周杰伦"),
    ("后来", "刘若英"), ("平凡之路", "朴树"), ("起风了", "买辣椒也用券"),
    ("光年之外", "邓紫棋"), ("演员", "薛之谦"), ("消愁", "毛不易"),
    ("成都", "赵雷"), ("南山南", "马頔"), ("岁月神偷", "金玟岐"),
    ("Lemon", "米津玄師"), ("Pretender", "Official髭男dism"),
    ("Blinding Lights", "The Weeknd"), ("Someone Like You", "Adele"),
    ("Bohemian Rhapsody", "Queen"), ("Numb", "Linkin Park"),
    ("Rolling in the Deep", "Adele"), ("Bad Guy", "Billie Eilish"),
    ("Stay", "Justin Bieber"), ("Peaches", "Justin Bieber"),
    ("花海", "周杰伦"), ("稻香", "周杰伦"), ("江南", "林俊杰"),
    ("曹操", "林俊杰"), ("红豆", "王菲"), ("传奇", "王菲"),
    ("小幸运", "田馥甄"), ("修炼爱情", "林俊杰"), ("突然好想你", "五月天"),
    ("倔强", "五月天"), ("温柔", "五月天"), ("春风十里", "鹿先森乐队"),
    ("理想三旬", "陈鸿宇"), ("董小姐", "宋冬野"),
]


def fallback_queries_from_seeds(seeds: list[dict], count: int = LLM_CANDIDATE_COUNT) -> list[dict]:
    """LLM 失败或不足 30 首时：按种子歌手 + 常见在线曲目补候选。"""
    recs: list[dict] = []
    seen_artist: set[str] = set()
    seen_ta: set[tuple[str, str]] = set()
    for s in seeds:
        artist = str(s.get("artist") or "").split("/")[0].strip()
        if not artist or artist.lower() in seen_artist:
            continue
        seen_artist.add(artist.lower())
        recs.append({
            "title": "",
            "artist": artist,
            "album": "",
            "language": s.get("language") or "",
            "genre": s.get("genre") or "",
            "type": "",
            "dimension": "artist",
            "reason": "fallback: same artist online",
        })
        if len(recs) >= count:
            return recs[:count]
    for title, artist in _FALLBACK_POOL:
        key = identity_key(title, artist)
        if key in seen_ta:
            continue
        seen_ta.add(key)
        recs.append({
            "title": title,
            "artist": artist,
            "dimension": "language",
            "reason": "fallback",
        })
        if len(recs) >= count:
            break
    return recs[:count]


def stamp_playlist_tracks(tracks: list[dict], now: int | None = None) -> list[dict]:
    ts = int(now or time.time())
    out: list[dict] = []
    for t in tracks:
        item = dict(t)
        item.setdefault("createdAt", ts)
        item.setdefault("updatedAt", ts)
        item.setdefault("isFavorite", False)
        item.setdefault("isCue", False)
        item.setdefault("accessStatus", 0)
        item.setdefault("year", None)
        item.setdefault("discNo", None)
        item.setdefault("trackNo", None)
        item.setdefault("isrc", "")
        artists = item.get("artists")
        if isinstance(artists, list):
            shaped = []
            for a in artists:
                if not isinstance(a, dict):
                    continue
                aa = dict(a)
                aa.setdefault("guid", aa.get("guid") or f"{item.get('guid')}:artist")
                aa.setdefault("coverId", aa.get("guid"))
                aa.setdefault("createdAt", ts)
                aa.setdefault("updatedAt", ts)
                shaped.append(aa)
            item["artists"] = shaped
        album = item.get("album")
        if isinstance(album, dict):
            alb = dict(album)
            alb.setdefault("createdAt", ts)
            alb.setdefault("updatedAt", ts)
            alb.setdefault("releaseDate", "")
            alb.setdefault("barcode", "")
            item["album"] = alb
        out.append(item)
    return out


def _dedupe_extend(base: list[dict], extra: list[dict], limit: int) -> list[dict]:
    seen_ids = {str(t.get("guid") or "") for t in base}
    seen_ta = {identity_key(str(t.get("title") or ""), str(t.get("artist") or "")) for t in base}
    out = list(base)
    for track in extra:
        guid = str(track.get("guid") or "")
        ta = identity_key(str(track.get("title") or ""), str(track.get("artist") or ""))
        if guid and guid in seen_ids:
            continue
        if ta != ("", "") and ta in seen_ta:
            continue
        seen_ids.add(guid)
        if ta != ("", ""):
            seen_ta.add(ta)
        out.append(track)
        if len(out) >= limit:
            break
    return out[:limit]


def cache_path(user_guid: str, day: str) -> str:
    return os.path.join(recommend_cache_dir(), _safe_user_name(user_guid), f"{day}.json")


def load_daily_cache(user_guid: str, day: str) -> dict | None:
    path = cache_path(user_guid, day)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        if str(data.get("day") or "") != day:
            return None
        if data.get("status") not in ("ready", "partial"):
            return None
        tracks = data.get("tracks")
        if not isinstance(tracks, list) or not tracks:
            return None
        return data
    except Exception as e:
        logger.warning("failed to load recommend cache: %s", e)
    return None


def save_daily_cache(user_guid: str, day: str, payload: dict) -> None:
    _atomic_write_json(cache_path(user_guid, day), payload)


def purge_stale_daily_cache(user_guid: str, keep_day: str) -> None:
    folder = os.path.join(recommend_cache_dir(), _safe_user_name(user_guid))
    if not os.path.isdir(folder):
        return
    keep = f"{keep_day}.json"
    for name in os.listdir(folder):
        if not name.endswith(".json") or name == keep:
            continue
        path = os.path.join(folder, name)
        try:
            os.remove(path)
            logger.info("purged stale daily recommend %s", path)
        except Exception as e:
            logger.warning("failed to purge %s: %s", path, e)


def build_playlist_record(
    guid: str,
    name: str,
    cover_id: str | None,
    track_count: int,
    created_at: int | None = None,
) -> dict:
    ts = created_at or int(time.time())
    return {
        "guid": guid,
        "name": name,
        "coverId": cover_id or guid,
        "createdAt": ts,
        "updatedAt": ts,
        "trackCount": track_count,
        "isDaily": True,
    }


def empty_daily_bundle(user_guid: str) -> dict:
    day = today_key()
    guid = daily_playlist_guid(day, user_guid)
    playlist = build_playlist_record(
        guid=guid,
        name=f"每日推荐 {day[4:6]}-{day[6:8]}",
        cover_id=guid,
        track_count=0,
    )
    return {
        "day": day,
        "guid": guid,
        "playlist": playlist,
        "tracks": [],
        "seedCount": 0,
        "builtAt": int(time.time()),
    }


async def get_or_build_daily(
    user_guid: str,
    musicdl_client: httpx.AsyncClient | None,
    musicbox_client: httpx.AsyncClient | None,
    llm_http: httpx.AsyncClient | None,
    build_track,
    netease_enabled: bool,
    extra_seeds: list[dict] | None = None,
    favorite_items: list[dict] | None = None,
) -> dict:
    day = today_key()
    guid = daily_playlist_guid(day, user_guid)
    purge_stale_daily_cache(user_guid, day)
    cached = load_daily_cache(user_guid, day)
    existing = list(cached.get("tracks") or []) if cached else []
    if len(existing) >= PLAYLIST_SIZE:
        return cached

    local_seeds = read_local_recent_tracks(music_db_path(), user_guid, SEED_LIMIT)
    online_seeds = seeds_from_online_history(user_guid)
    local_favs = read_local_favorite_tracks(music_db_path(), user_guid)
    online_favs = [x for x in (favorite_items or []) if isinstance(x, dict)]
    fav_seeds: list[dict] = []
    for it in online_favs + local_favs:
        title, artist = _item_title_artist(it)
        album = ""
        track = it.get("track") if isinstance(it.get("track"), dict) else it
        if isinstance(track.get("album"), dict):
            album = str(track["album"].get("name") or "")
        elif track.get("albumName"):
            album = str(track.get("albumName") or "")
        elif track.get("album"):
            album = str(track.get("album") or "")
        fav_seeds.append({
            "guid": str(it.get("guid") or track.get("guid") or ""),
            "title": title,
            "artist": artist,
            "album": album,
            "genre": "",
            "language": infer_language(title, artist, album),
            "playedAt": int(it.get("createdAt") or it.get("playedAt") or 0),
            "source": "favorite",
        })
    play_seeds = merge_recent_seeds(local_seeds, online_seeds, extra_seeds, SEED_LIMIT)
    exclude_guids, exclude_ta = collect_exclude_sets(play_seeds, fav_seeds, extra_seeds, online_favs, local_favs)
    if existing:
        exclude_guids = exclude_guids | {str(t.get("guid") or "") for t in existing}
        exclude_ta = exclude_ta | {
            identity_key(str(t.get("title") or ""), str(t.get("artist") or "")) for t in existing
        }

    async def from_llm() -> list[dict]:
        if llm_http is None or not llm_enabled():
            return []
        recs = await call_llm(
            llm_http, build_llm_prompt(play_seeds, fav_seeds[:40], LLM_CANDIDATE_COUNT)
        )
        if not recs:
            return []
        return await resolve_recommendations(
            recs, musicdl_client, musicbox_client, netease_enabled, build_track,
            PLAYLIST_SIZE, exclude_guids, exclude_ta,
        )

    async def from_fallback() -> list[dict]:
        recs = fallback_queries_from_seeds(play_seeds + fav_seeds, LLM_CANDIDATE_COUNT)
        return await resolve_recommendations(
            recs, musicdl_client, musicbox_client, netease_enabled, build_track,
            PLAYLIST_SIZE, exclude_guids, exclude_ta,
        )

    tracks = list(existing)
    jobs = [asyncio.create_task(from_fallback())]
    if llm_http is not None and llm_enabled():
        jobs.append(asyncio.create_task(from_llm()))
    t0 = time.monotonic()
    try:
        for fut in asyncio.as_completed(jobs):
            remaining = BUILD_BUDGET_S - (time.monotonic() - t0)
            if remaining <= 0:
                break
            try:
                chunk = await asyncio.wait_for(asyncio.shield(fut), timeout=remaining)
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug("daily recommend branch failed: %s", e)
                continue
            tracks = _dedupe_extend(tracks, chunk, PLAYLIST_SIZE)
            if len(tracks) >= PLAYLIST_SIZE:
                break
    finally:
        for t in jobs:
            if not t.done():
                t.cancel()

    tracks = stamp_playlist_tracks(tracks[:PLAYLIST_SIZE])
    cover_id = tracks[0].get("coverId") or tracks[0].get("guid") if tracks else guid
    playlist = build_playlist_record(
        guid=guid,
        name=f"每日推荐 {day[4:6]}-{day[6:8]}",
        cover_id=str(cover_id or guid),
        track_count=len(tracks),
    )
    payload = {
        "day": day,
        "guid": guid,
        "status": "ready" if len(tracks) >= PLAYLIST_SIZE else "partial",
        "playlist": playlist,
        "tracks": tracks,
        "seedCount": len(play_seeds),
        "favoriteCount": len(fav_seeds),
        "builtAt": int(time.time()),
    }
    if tracks:
        save_daily_cache(user_guid, day, payload)
        logger.info("daily recommend %s tracks=%s status=%s", guid, len(tracks), payload["status"])
    return payload
