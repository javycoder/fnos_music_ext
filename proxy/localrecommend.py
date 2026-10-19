"""Account-local recommendations from read-only library data."""
from __future__ import annotations

import asyncio
import hashlib
import json
import inspect
import logging
import math
import os
import sqlite3
import threading
import time
from pathlib import Path
from collections import Counter
from urllib.parse import quote
from uuid import uuid4

import httpx

try:
    from . import recommend as rec
except ImportError:
    import recommend as rec


logger = logging.getLogger("fnmusic_proxy.localrecommend")
PLAYLIST_SIZE = 20
CACHE_SECONDS = 1800
FEATURE_WEIGHTS = {"artist": .45, "genre": .20, "album": .15, "decade": .10, "language": .10}
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _features(row: dict) -> dict[str, tuple[str, ...]]:
    def split_values(value: str) -> tuple[str, ...]:
        return tuple(sorted({part.strip().casefold() for part in value.split("\x1f") if part.strip()}))

    language = rec.infer_language(row.get("title", ""), row.get("artist", ""), row.get("album", ""))
    year = row.get("year")
    try:
        decade = str(int(year) // 10 * 10) if int(year) > 0 else ""
    except (ValueError, TypeError):
        decade = ""
    album = str(row.get("album") or "").strip().casefold()
    return {
        "artist": split_values(row.get("artist_values", "")),
        "genre": split_values(row.get("genre_values", "")),
        "album": (album,) if album else (),
        "decade": (decade,) if decade else (),
        "language": (language,) if language != "未知" else (),
    }


def read_library(db_path: str, user_guid: str) -> list[dict]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN")
        user = con.execute("SELECT id FROM user WHERE guid = ?", (user_guid,)).fetchone()
        if user is None:
            raise ValueError("unknown library user")
        rows = con.execute("""
            SELECT t.guid, t.title, t.year, t.disc_no, t.track_no, t.isrc,
                   t.duration_ms, t.is_cue, t.cover_guid,
                   af.path, af.suffix, af.size, af.bitrate, af.sample_rate,
                   af.bit_depth, af.channel, af.container, af.codec,
                   al.guid AS album_guid, al.name AS album, al.release_date,
                   (SELECT GROUP_CONCAT(a.name, char(31))
                      FROM track_artist ta JOIN artist a ON a.id = ta.artist_id
                     WHERE ta.track_id = t.id) AS artist_values,
                   (SELECT GROUP_CONCAT(a.guid, '/')
                      FROM track_artist ta JOIN artist a ON a.id = ta.artist_id
                     WHERE ta.track_id = t.id) AS artist_guids,
                   (SELECT GROUP_CONCAT(g.name, char(31))
                      FROM track_genre tg JOIN genre g ON g.id = tg.genre_id
                     WHERE tg.track_id = t.id) AS genre_values,
                   ph.play_count, ph.last_played_at,
                   EXISTS(SELECT 1 FROM favorite_track ft
                           WHERE ft.track_id = t.id AND ft.user_id = ?) AS favorite,
                   (t.is_audio_file_deleted = 0 AND t.is_admin_deleted = 0
                    AND af.is_physical_file_deleted = 0) AS valid
              FROM track t
              LEFT JOIN audio_file af ON af.id = t.audio_file_id
              LEFT JOIN album al ON al.id = t.album_id
              LEFT JOIN (SELECT track_id, SUM(play_count) AS play_count,
                                MAX(updated_at) AS last_played_at
                           FROM play_history WHERE user_id = ? GROUP BY track_id) ph
                     ON ph.track_id = t.id
             ORDER BY t.guid
        """, (user["id"], user["id"])).fetchall()
        result = []
        for raw in rows:
            row = dict(raw)
            row["artist_values"] = row["artist_values"] or ""
            row["genre_values"] = row["genre_values"] or ""
            row["artist"] = row["artist_values"].replace("\x1f", "/")
            row["title"] = row["title"] or ""
            row["album"] = row["album"] or ""
            row["features"] = _features(row)
            result.append(row)
        return result
    finally:
        con.close()


def _age_days(timestamp, now: float) -> float:
    played_at = rec._coerce_ts(timestamp)
    return max(0, (now - played_at) / 86400) if played_at else 36500


def _history_weight(play_count: int, timestamp, now: float) -> float:
    return (1 + math.log1p(min(max(play_count, 0), 50))) * (
        .35 + .65 * 2 ** (-_age_days(timestamp, now) / 30)
    )


def build_profile(rows: list[dict], online: list[dict], now: float) -> dict[str, dict[str, float]]:
    profile = {feature: Counter() for feature in FEATURE_WEIGHTS}

    def add(features: dict, weight: float) -> None:
        for feature, values in features.items():
            for value in values:
                profile[feature][value] += weight / len(values)

    for row in rows:
        count = int(row.get("play_count") or 0)
        weight = _history_weight(count, row["last_played_at"], now) if count > 0 else 0
        weight += 3 if row["favorite"] else 0
        if weight:
            add(row["features"], weight)
    for seed in online:
        row = dict(seed)
        row["artist_values"] = row.get("artist", "").replace("/", "\x1f")
        row["genre_values"] = row.get("genre", "").replace("/", "\x1f")
        add(_features(row), _history_weight(1, row.get("playedAt"), now))
    result = {}
    for feature, values in profile.items():
        if not values:
            result[feature] = {}
            continue
        max_weight = max(values.values())
        result[feature] = {
            value: (weight / max_weight if max_weight > 0 else 0.0)
            for value, weight in values.items()
        }
    return result


def score_track(row: dict, profile: dict, user_guid: str, generation: str, now: float) -> float:
    numerator = 0.0
    denominator = 0.0
    for feature, values in row["features"].items():
        if not values or not profile[feature]:
            continue
        weight = FEATURE_WEIGHTS[feature]
        numerator += weight * sum(profile[feature].get(value, 0) for value in values) / len(values)
        denominator += weight
    affinity = numerator / denominator if denominator else 0
    age = _age_days(row["last_played_at"], now)
    freshness = min(1, age / 30)
    digest = hashlib.sha256(f"{user_guid}:{row['guid']}:{generation}".encode()).digest()
    variation = int.from_bytes(digest[:8], "big") / (2 ** 64 - 1)
    score = .7 * affinity + .2 * freshness + .1 * variation
    return score * (.2 if age < 1 else .6 if age < 7 else 1)


def select_tracks(
    candidates: list[dict], profile: dict, user_guid: str, generation: str, now: float,
    limit: int = PLAYLIST_SIZE,
) -> list[dict]:
    remaining = {row["guid"]: row for row in candidates}
    scores = {guid: score_track(row, profile, user_guid, generation, now) for guid, row in remaining.items()}
    artists = Counter()
    albums = Counter()
    selected = []

    def adjusted(row: dict) -> float:
        features = row["features"]
        return (scores[row["guid"]]
                - .12 * sum(artists[value] for value in features["artist"])
                - .08 * sum(albums[value] for value in features["album"]))

    wanted = max(0, min(limit, len(remaining)))
    explore_from = max(0, wanted - max(1, int(wanted * .2)))
    for index in range(wanted):
        if index < explore_from:
            chosen = max(remaining.values(), key=lambda row: (adjusted(row), row["guid"]))
        else:
            chosen = max(remaining.values(), key=lambda row: (
                not bool(row["last_played_at"]),
                _age_days(row["last_played_at"], now),
                adjusted(row),
                row["guid"],
            ))
        del remaining[chosen["guid"]]
        artists.update(chosen["features"]["artist"])
        albums.update(chosen["features"]["album"])
        track = rec.build_local_track(chosen)
        track["coverId"] = str(chosen.get("cover_guid") or "")
        track["genres"] = list(chosen["features"]["genre"])
        track["recommendDimension"] = "explore" if index >= 16 else "preference"
        track["recommendReason"] = "发现本地曲库中少听的歌曲" if index >= 16 else "根据你的收听与收藏偏好推荐"
        selected.append(track)
    return selected


def _validate_cached_tracks(db_path: str, user_guid: str, guids: list[str]) -> bool:
    if len(guids) != PLAYLIST_SIZE or len(set(guids)) != PLAYLIST_SIZE:
        return False
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    try:
        con.execute("BEGIN")
        user = con.execute("SELECT id FROM user WHERE guid = ?", (user_guid,)).fetchone()
        if user is None:
            return False
        user_id = user[0]
        placeholders = ",".join("?" for _ in guids)
        query = f"""
            SELECT t.guid
              FROM track t
              LEFT JOIN audio_file af ON af.id = t.audio_file_id
             WHERE t.guid IN ({placeholders})
               AND t.is_audio_file_deleted = 0
               AND t.is_admin_deleted = 0
               AND af.is_physical_file_deleted = 0
               AND NOT EXISTS (
                   SELECT 1 FROM favorite_track ft
                    WHERE ft.track_id = t.id AND ft.user_id = ?
               )
        """
        rows = con.execute(query, (*guids, user_id)).fetchall()
        valid_guids = {row[0] for row in rows}
        return len(valid_guids) == PLAYLIST_SIZE and valid_guids == set(guids)
    finally:
        con.close()


def _load_or_build(user_guid: str, db_path: str) -> dict:
    empty = rec.empty_daily_bundle(user_guid, "liked")
    if not user_guid.strip() or user_guid == "shared":
        return empty
    user_key = hashlib.sha256(user_guid.encode()).hexdigest()
    path = os.path.join(rec.recommend_cache_dir(), user_key, "liked.json")
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(path, threading.Lock())
    with lock:
        try:
            now = time.time()
            try:
                with open(path, encoding="utf-8") as cache_file:
                    cached = json.load(cache_file)
                tracks = cached.get("tracks") or []
                track_guids = [track.get("guid") for track in tracks if isinstance(track, dict) and track.get("guid")]
                if (cached.get("guid") == empty["guid"] and cached.get("user") == user_guid
                        and 0 <= now - cached.get("builtAt", 0) < CACHE_SECONDS
                        and cached.get("engine", "local") == "local"
                        and not cached.get("configFingerprint")
                        and len(tracks) == PLAYLIST_SIZE
                        and len(set(track_guids)) == PLAYLIST_SIZE):
                    if _validate_cached_tracks(db_path, user_guid, track_guids):
                        return cached
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                pass
            # 缓存缺失/过期/有曲目失效走全量重建路径
            # 已知可接受取舍：曲库总量骤降到 20 以下但缓存 20 首仍有效时继续供缓存
            rows = read_library(db_path, user_guid)
            candidates = [row for row in rows if row["valid"] and not row["favorite"] and row["guid"]]
            if len(candidates) < PLAYLIST_SIZE:
                return empty
            profile = build_profile(rows, rec.seeds_from_online_history(user_guid), now)
            tracks = select_tracks(candidates, profile, user_guid, uuid4().hex, now, PLAYLIST_SIZE)
            bundle = {
                **empty, "tracks": tracks, "builtAt": now, "user": user_guid,
                "playlist": rec.build_playlist_record(
                    empty["guid"], "猜你喜欢", empty["guid"], len(tracks), int(now),
                ),
            }
            rec._atomic_write_json(path, bundle)
            return bundle
        except Exception as error:
            logger.warning("local recommendations unavailable: %s", error)
            return empty


# Persisted LLM batches are explicitly distinct from the fixed-size local engine.
_LLM_ENGINE = "llm-liked-v1"
_GENERATION = 0
_BUILD_TOKENS: dict[str, str] = {}


async def get_bundle(user_guid: str, db_path: str) -> dict:
    if rec.llm_liked_enabled() and rec.llm_liked_size() == 0:
        return rec.empty_daily_bundle(user_guid, "liked")
    if not rec.llm_liked_enabled():
        return await asyncio.to_thread(_load_or_build, user_guid, db_path)
    return (await asyncio.to_thread(load_llm_bundle, user_guid, db_path=db_path)
            or rec.empty_daily_bundle(user_guid, "liked"))


def _liked_path(user_guid: str) -> str:
    return os.path.join(rec.recommend_cache_dir(), hashlib.sha256(user_guid.encode()).hexdigest(), "liked.json")


def _liked_state_path(user_guid: str) -> str:
    return _liked_path(user_guid) + ".state"


def _lock_for(path: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.Lock())


def _account(user_guid: str) -> bool:
    return bool(user_guid.strip() and user_guid != "shared")


def list_user_guids(db_path: str) -> list[str]:
    con = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        return [str(row[0]) for row in con.execute("SELECT guid FROM user ORDER BY id")
                if row[0] and _account(str(row[0]))]
    except sqlite3.Error as error:
        logger.warning("liked user discovery failed: %s", error)
        return []
    finally:
        if con is not None:
            con.close()


def current_source_fingerprint(conf: dict | None = None) -> str:
    """Same normalized material for app CONF and default environment-only readers."""
    fields = {
        "musicdl_enabled": ("FNMUSIC_MUSICDL_ENABLED", False),
        "netease_enabled": ("FNMUSIC_NETEASE_ENABLED", False),
        "lx_enabled": ("FNMUSIC_LX_ENABLED", False),
        "online_sources": ("FNMUSIC_ONLINE_SOURCES", "KuwoMusicClient,MiguMusicClient"),
        "lx_sources": ("LX_SOURCES", ""),
        "lx_source_url": ("LX_SOURCE_URL", ""),
        "lx_source_list": ("LX_SOURCE_LIST", "[]"),
    }
    aliases = {"kugou": "kg", "kuwo": "kw", "netease": "wy", "163": "wy",
               "migu": "mg", "qq": "tx", "tencent": "tx"}
    material = {}
    for key, (env, default) in fields.items():
        value = conf.get(key, default) if conf is not None else os.environ.get(env, default)
        if isinstance(default, bool):
            value = value is True or str(value).strip().lower() in ("true", "1", "yes")
        elif key in ("online_sources", "lx_sources"):
            parts = value if isinstance(value, list) else str(value).split(",")
            value = sorted({str(part).strip() for part in parts if str(part).strip()})
            if key == "lx_sources":
                value = sorted({aliases.get(part.lower(), part.lower()) for part in value}
                               & {"kg", "kw", "wy", "mg", "tx"})
        elif key == "lx_source_list":
            try:
                value = json.loads(value) if isinstance(value, str) else value
            except (ValueError, TypeError):
                value = str(value)
        else:
            value = str(value).strip()
        material[key] = value
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def liked_config_fingerprint(source_fingerprint: str | None = None) -> str:
    material = json.dumps({
        "engine": _LLM_ENGINE, "enabled": rec.llm_liked_enabled(), "base": rec.llm_base_url(),
        "key": hashlib.sha256(rec.llm_api_key().encode()).hexdigest(),
        "model": rec.llm_model(), "size": rec.llm_liked_size(),
        "source": current_source_fingerprint() if source_fingerprint is None else source_fingerprint,
    }, sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _unlink(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _eligible_guids(db_path: str, user_guid: str) -> set[str]:
    """Read-only snapshot: account existence and independent 20-song gate."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    try:
        con.execute("BEGIN")
        user = con.execute("SELECT id FROM user WHERE guid = ?", (user_guid,)).fetchone()
        if user is None or not _account(user_guid):
            return set()
        return {str(row[0]) for row in con.execute("""
            SELECT t.guid FROM track t JOIN audio_file af ON af.id = t.audio_file_id
             WHERE t.guid IS NOT NULL AND t.guid != ''
               AND t.is_audio_file_deleted = 0 AND t.is_admin_deleted = 0
               AND af.is_physical_file_deleted = 0
               AND NOT EXISTS (SELECT 1 FROM favorite_track ft
                                WHERE ft.track_id = t.id AND ft.user_id = ?)
        """, (user[0],))}
    finally:
        con.close()


def _valid_batch(cached: dict | None, user_guid: str, fingerprint: str, db_path: str) -> bool:
    if (not cached or cached.get("engine") != _LLM_ENGINE
            or cached.get("user") != user_guid or not cached.get("batchId")
            or cached.get("guid") != rec.empty_daily_bundle(user_guid, "liked")["guid"]
            or cached.get("configFingerprint") != fingerprint):
        return False
    tracks = cached.get("tracks")
    if not isinstance(tracks, list) or not tracks or len(tracks) > rec.llm_liked_size():
        return False
    eligible = _eligible_guids(db_path, user_guid)
    if len(eligible) < PLAYLIST_SIZE:
        return False
    ids, identities = set(), set()
    for track in tracks:
        if not isinstance(track, dict):
            return False
        guid = str(track.get("guid") or "")
        key = rec.identity_key(str(track.get("title") or ""), str(track.get("artist") or ""))
        if not guid or guid in ids or (key != ("", "") and key in identities):
            return False
        if guid.startswith("online:"):
            if not guid.startswith(("online:netease:", "online:lx:")):
                return False
        elif guid not in eligible:
            return False
        ids.add(guid)
        identities.add(key)
    return True


def load_llm_bundle(user_guid: str, source_fingerprint: str | None = None, *,
                    db_path: str | None = None) -> dict | None:
    """Validate arbitrary LLM batch lengths, never the local engine's fixed 20."""
    if not _account(user_guid) or not rec.llm_liked_enabled() or rec.llm_liked_size() == 0:
        return None
    path = _liked_path(user_guid)
    with _lock_for(path):
        cached = _read_json(path)
        fingerprint = liked_config_fingerprint(source_fingerprint)
        try:
            if len(_eligible_guids(db_path or rec.music_db_path(), user_guid)) < PLAYLIST_SIZE:
                if cached and cached.get("engine") == _LLM_ENGINE:
                    _unlink(path)
                _unlink(_liked_state_path(user_guid))
                return None
            if _valid_batch(cached, user_guid, fingerprint, db_path or rec.music_db_path()):
                return cached
        except (sqlite3.Error, ValueError, TypeError):
            if cached and cached.get("engine") == _LLM_ENGINE:
                _unlink(path)
            return None
        # Never erase a newer configuration's batch or the other engine's cache.
        if cached and cached.get("engine") == _LLM_ENGINE and cached.get("configFingerprint") == fingerprint:
            _unlink(path)
        return None


def mark_llm_bundle_fetched(user_guid: str, batch_id: str | None = None, *,
                            source_fingerprint: str | None = None, db_path: str | None = None,
                            now: float | None = None) -> bool:
    """Pass the response's batchId; absent IDs deliberately acknowledge nothing."""
    if not batch_id or not _account(user_guid) or not rec.llm_liked_enabled() or rec.llm_liked_size() == 0:
        return False
    path = _liked_path(user_guid)
    with _lock_for(path):
        cached = _read_json(path)
        try:
            if (not _valid_batch(cached, user_guid, liked_config_fingerprint(source_fingerprint),
                                 db_path or rec.music_db_path())
                    or cached.get("batchId") != batch_id or cached.get("firstFetchedAt") is not None):
                return False
        except (sqlite3.Error, ValueError, TypeError):
            return False
        cached["firstFetchedAt"] = time.time() if now is None else now
        return rec._atomic_write_json(path, cached)


def llm_build_due(user_guid: str, fingerprint: str, now: float, *, db_path: str | None = None) -> bool:
    if not _account(user_guid) or not rec.llm_liked_enabled() or rec.llm_liked_size() == 0:
        return False
    path = _liked_path(user_guid)
    with _lock_for(path):
        cached = _read_json(path)
        try:
            if len(_eligible_guids(db_path or rec.music_db_path(), user_guid)) < PLAYLIST_SIZE:
                if cached and cached.get("engine") == _LLM_ENGINE:
                    _unlink(path)
                _unlink(_liked_state_path(user_guid))
                return False
            valid = _valid_batch(cached, user_guid, fingerprint, db_path or rec.music_db_path())
        except (sqlite3.Error, ValueError, TypeError):
            if cached and cached.get("engine") == _LLM_ENGINE:
                _unlink(path)
            return False
        state = _read_json(_liked_state_path(user_guid)) or {}
        try:
            if state.get("configFingerprint") == fingerprint and float(state.get("nextRetryAt", 0)) > now:
                return False
            if not valid:
                return True
            first = cached.get("firstFetchedAt")
            return first is not None and now - float(first) >= rec.llm_liked_refresh_hours() * 3600
        except (ValueError, TypeError):
            return True


def record_llm_failure(user_guid: str, now: float, fingerprint: str | None = None, *,
                       batch_id: str | None = None) -> None:
    if not _account(user_guid) or not rec.llm_liked_enabled() or rec.llm_liked_size() == 0:
        return
    path = _liked_path(user_guid)
    with _lock_for(path):
        cached = _read_json(path) or {}
        if batch_id is not None and cached.get("batchId") != batch_id:
            return
        rec._atomic_write_json(_liked_state_path(user_guid), {
            "configFingerprint": fingerprint or liked_config_fingerprint(),
            "failedAt": now, "nextRetryAt": now + 300,
        })


def invalidate_llm_bundles() -> int:
    global _GENERATION
    with _LOCKS_GUARD:
        _GENERATION += 1
        _BUILD_TOKENS.clear()
    removed = 0
    root = Path(rec.recommend_cache_dir())
    try:
        for folder in root.iterdir() if root.exists() else []:
            if not folder.is_dir():
                continue
            path = str(folder / "liked.json")
            with _lock_for(path):
                cached = _read_json(path)
                if cached and (cached.get("engine") == _LLM_ENGINE or cached.get("configFingerprint")):
                    _unlink(path)
                    removed += 1
                _unlink(path + ".state")
    except OSError as error:
        logger.warning("liked cache invalidation failed: %s", error)
    return removed


def _candidate_line(candidate: dict) -> str:
    return json.dumps(candidate, ensure_ascii=False)


async def _live_lx_sources(client, permitted: list[str] | None) -> list[str]:
    response = await client.get("/healthz", timeout=10.0)
    payload = response.json() if response.status_code == 200 else {}
    if not isinstance(payload, dict) or payload.get("ok") is False:
        return []
    configured = payload.get("sources") or []
    caps = payload.get("capabilities") or {}
    charts = payload.get("charts") or list(caps)
    if not isinstance(caps, dict) or not isinstance(configured, list):
        return []
    return [source for source in configured if source in charts
            and (permitted is None or source in permitted)
            and isinstance(caps.get(source), dict) and caps[source].get("playback_available") is True]


async def _fetch_new_candidates(musicbox_client, netease_enabled: bool,
                                lx_client, lx_enabled: bool, lx_sources: list[str] | None) -> list[dict]:
    candidates = []
    if netease_enabled and musicbox_client is not None:
        try:
            candidates.extend(await rec.fetch_musicbox_recommend(
                musicbox_client, "/api/v1/toplist", {"index": 1, "limit": 100}))
        except Exception as error:
            logger.warning("liked netease new-song fetch failed: %s", error)
    if lx_enabled and lx_client is not None:
        try:
            sources = await _live_lx_sources(lx_client, lx_sources)
        except Exception as error:
            logger.warning("liked lx discovery failed: %s", error)
            sources = []
        for source in sources:
            try:
                response = await lx_client.get("/api/v1/public-playlists/categories",
                                               params={"source": source, "kind": "chart"}, timeout=10.0)
                payload = response.json() if response.status_code == 200 else {}
                for board in payload.get("tags") or []:
                    if not isinstance(board, dict) or "新歌" not in str(board.get("name") or ""):
                        continue
                    # Raw boards expose a display id (kg__8888) alongside the
                    # actual bangid (8888); the tracks endpoint expects bangid.
                    board_id = str(board.get("bangid") or board.get("id") or "").strip()
                    board_id = board_id.removeprefix(f"{source}__")
                    if not board_id:
                        continue
                    response = await lx_client.get(
                        f"/api/v1/public-playlists/{quote(board_id, safe='')}/tracks",
                        params={"source": source, "kind": "chart"}, timeout=30.0)
                    payload = response.json() if response.status_code == 200 else {}
                    for row in payload.get("data") or []:
                        if not isinstance(row, dict):
                            continue
                        item = rec._lx_recommend_item(row)
                        if item and item["id"].startswith(f"lx:{source}:"):
                            candidates.append({**row, **item})
            except Exception as error:
                logger.warning("liked lx new-song fetch failed: %s", error)
    out, ids, identities = [], set(), set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        identifier = str(item.get("id") or "")
        key = rec.identity_key(str(item.get("title") or ""), str(item.get("artist") or ""))
        if (not identifier or not str(item.get("title") or "").strip() or identifier in ids
                or (key != ("", "") and key in identities)):
            continue
        ids.add(identifier)
        identities.add(key)
        out.append(dict(item, id=identifier))
    return out[:300]


def _selected_ids(payload: object) -> dict[str, list[str]]:
    def values(items):
        return [item if isinstance(item, str) else str(item.get("id") or item.get("candidateId") or "")
                for item in items if isinstance(item, (str, dict))]
    if isinstance(payload, dict):
        return {segment: values(items) for segment, items in payload.items() if isinstance(items, list)}
    return {"all": values(payload)} if isinstance(payload, list) else {}


async def _await_value(value):
    return await value if inspect.isawaitable(value) else value


def _local_track(row: dict) -> dict:
    return {**rec.build_local_track(row), "coverId": str(row.get("cover_guid") or ""),
            "genres": list(row["features"]["genre"])}


async def build_llm_bundle(
    user_guid: str, db_path: str, llm_http: httpx.AsyncClient, build_track,
    musicbox_client=None, netease_enabled: bool = False,
    lx_client=None, lx_enabled: bool = False, lx_sources: list[str] | None = None,
    source_fingerprint: str | None = None, *, publication_guard=None,
    get_source_fingerprint=None, source_enabled=None, verify_playable=None,
    online_favorites=None, online_history: list[dict] | None = None,
) -> dict:
    """Build privately, then publish under the account lock without an intervening await.

    publication_guard() synchronously checks the app's captured generation epoch/task.
    get_source_fingerprint() returns the live source fingerprint synchronously.
    source_enabled(track) is synchronous; verify_playable(track) and
    online_favorites(user_guid) may be sync or async. No app globals are imported.
    Internal generation/tokens also reject invalidated and overlapping builders.
    """
    empty = rec.empty_daily_bundle(user_guid, "liked")
    target = rec.llm_liked_size()
    if not _account(user_guid) or not rec.llm_liked_enabled() or target == 0:
        return empty
    path = _liked_path(user_guid)
    token = uuid4().hex
    with _lock_for(path):
        epoch = _GENERATION
        _BUILD_TOKENS[path] = token
    source_reader = get_source_fingerprint or current_source_fingerprint
    source_at_start = source_reader() if source_fingerprint is None else source_fingerprint
    env_at_start = current_source_fingerprint()
    fingerprint = liked_config_fingerprint(source_at_start)

    def current() -> bool:
        return (rec.llm_liked_enabled() and rec.llm_liked_size() == target and target > 0
                and epoch == _GENERATION and _BUILD_TOKENS.get(path) == token
                and current_source_fingerprint() == env_at_start
                and (get_source_fingerprint is None or source_reader() == source_at_start)
                and liked_config_fingerprint(source_at_start) == fingerprint
                and (publication_guard is None or publication_guard() is True))

    try:
        rows = await asyncio.to_thread(read_library, db_path, user_guid)
    except (sqlite3.Error, ValueError) as error:
        logger.warning("liked library unavailable: %s", error)
        rows = []
    eligible = {row["guid"]: row for row in rows if row["valid"] and not row["favorite"] and row["guid"]}

    def discard_ineligible() -> None:
        with _lock_for(path):
            if current():
                cached = _read_json(path)
                if cached and cached.get("engine") == _LLM_ENGINE:
                    _unlink(path)
                _unlink(_liked_state_path(user_guid))

    if len(eligible) < PLAYLIST_SIZE:
        discard_ineligible()
        return empty
    history = rec.seeds_from_online_history(user_guid) if online_history is None else online_history
    favorites = []
    if online_favorites is not None:
        try:
            favorites = await _await_value(online_favorites(user_guid)) or []
        except Exception as error:
            logger.warning("liked online favorites unavailable: %s", error)
    seeds = list(history)
    for item in favorites:
        if isinstance(item, dict):
            track = item.get("track") if isinstance(item.get("track"), dict) else item
            seeds.extend([dict(track, playedAt=time.time())] * 3)
    profile = build_profile(rows, seeds, time.time())
    generation = uuid4().hex
    ranked = sorted(eligible.values(), key=lambda row: (
        score_track(row, profile, user_guid, generation, time.time()), row["guid"]), reverse=True)
    explore = sorted(eligible.values(), key=lambda row: (
        not bool(row["last_played_at"]), _age_days(row["last_played_at"], time.time()),
        -score_track(row, profile, user_guid, generation, time.time()), row["guid"]), reverse=True)
    pool_size = min(300, max(60, target * 3))
    groups = {"preference": [f"local:{row['guid']}" for row in ranked[:pool_size]],
              "explore": [f"local:{row['guid']}" for row in explore[:pool_size]], "new": []}
    pool_ids = set(groups["preference"]) | set(groups["explore"])
    candidate_map = {f"local:{row['guid']}": _local_track(row) for row in ranked
                     if f"local:{row['guid']}" in pool_ids}
    excluded_keys = {rec.identity_key(row["title"], row["artist"]) for row in rows}
    excluded_ids, favorite_keys = rec.collect_exclude_sets(favorites)
    excluded_keys.update(favorite_keys)
    try:
        items = await _fetch_new_candidates(musicbox_client, netease_enabled, lx_client, lx_enabled, lx_sources)
    except Exception as error:
        logger.warning("liked new candidates unavailable: %s", error)
        items = []

    def allowed(track: dict) -> bool:
        guid = str(track.get("guid") or "")
        if guid.startswith("online:netease:"):
            enabled = (bool(guid[len("online:netease:"):]) and netease_enabled
                       and track.get("source", "netease") == "netease"
                       and (musicbox_client is not None or verify_playable is not None))
        elif guid.startswith("online:lx:"):
            parts = guid.split(":")
            platform = parts[2] if len(parts) >= 4 and parts[3] else ""
            enabled = (lx_enabled and bool(platform) and track.get("source", "lx") == "lx"
                       and (lx_sources is None or platform in lx_sources)
                       and (lx_client is not None or verify_playable is not None))
        else:
            return False  # Never use musicdl or arbitrary external identifiers.
        return bool(enabled and (source_enabled is None or source_enabled(track) is True))

    online_ids, online_keys = set(), set()
    for item in items:
        if not current():
            return empty
        try:
            identifier = str(item.get("id") or "")
            if not identifier.startswith(("netease:", "lx:")):
                continue
            key = rec.identity_key(str(item.get("title") or ""), str(item.get("artist") or ""))
            if (not str(item.get("title") or "").strip() or f"online:{identifier}" in excluded_ids
                    or key in excluded_keys or identifier in online_ids or key in online_keys):
                continue
            built = await _await_value(build_track(dict(item)))
            if not isinstance(built, dict) or built.get("guid") != f"online:{identifier}":
                continue
            track = {**item, **built}
            built_key = rec.identity_key(str(track.get("title") or ""), str(track.get("artist") or ""))
            if (not str(track.get("title") or "").strip() or built_key in excluded_keys
                    or built_key in online_keys or not allowed(track)):
                continue
            playable = (await _await_value(verify_playable(track)) if verify_playable is not None
                        else await rec.verify_track_playable(track, None, musicbox_client, lx_client))
            if not playable:
                continue
            online_ids.add(identifier)
            online_keys.update((key, built_key))
            candidate_id = f"new:{identifier}"
            candidate_map[candidate_id] = track
            groups["new"].append(candidate_id)
        except Exception as error:
            logger.debug("liked candidate rejected: %s", error)
    if not current():
        return empty
    quotas = {"preference": target - 2 * (target // 5), "new": target // 5, "explore": target // 5}
    prompt = ("你是音乐推荐引擎。仅从真实候选中按用户偏好排序选歌，不得编造 ID。"
              f"目标 {target} 首；软配额 {json.dumps(quotas)}，缺额由其他分组补足。\n"
              f"用户偏好={json.dumps(profile, ensure_ascii=False)[:6000]}\n")
    for segment, identifiers in groups.items():
        prompt += f"{segment}候选（JSON行）：\n" + "\n".join(_candidate_line({
            "id": identifier, "title": candidate_map[identifier].get("title", ""),
            "artist": candidate_map[identifier].get("artist", ""), "segment": segment,
        }) for identifier in identifiers) + "\n"
    prompt += '返回 JSON {"preference":[候选id],"new":[候选id],"explore":[候选id]}。'
    try:
        selections = _selected_ids(await rec.call_llm_json(llm_http, prompt))
    except Exception as error:
        logger.warning("liked model failed; using local fallback: %s", error)
        selections = {}
    model_ok = any(identifier in groups[segment] for segment in groups
                   for identifier in selections.get(segment, selections.get("all", [])))
    if not model_ok:
        groups["new"] = []
    # All asynchronous work is over. Re-read current metadata, favorites and deletion flags.
    try:
        fresh = read_library(db_path, user_guid)
        fresh_rows = {row["guid"]: row for row in fresh if row["valid"] and not row["favorite"] and row["guid"]}
    except (sqlite3.Error, ValueError):
        fresh_rows = {}
    if len(fresh_rows) < PLAYLIST_SIZE:
        discard_ineligible()
        return empty
    fresh_keys = {rec.identity_key(row["title"], row["artist"]) for row in fresh}
    tracks, used, used_keys = [], set(), set()
    reasons = {"preference": "根据你的常听口味筛选", "new": "符合口味的新歌联动", "explore": "拓展你的听歌边界"}

    def take(segment: str, count: int) -> None:
        requested = selections.get(segment, selections.get("all", [])) if model_ok else []
        order = list(dict.fromkeys([identifier for identifier in requested if identifier in groups[segment]]
                                   + groups[segment]))
        added = 0
        for identifier in order:
            if added >= count or len(tracks) >= target:
                break
            track = candidate_map[identifier]
            if identifier.startswith("local:"):
                row = fresh_rows.get(identifier[len("local:"):])
                if row is None:
                    continue
                track = _local_track(row)
            elif not allowed(track):
                continue
            guid = str(track.get("guid") or "")
            key = rec.identity_key(str(track.get("title") or ""), str(track.get("artist") or ""))
            if (guid in used or (key != ("", "") and key in used_keys)
                    or (identifier.startswith("new:") and key in fresh_keys)):
                continue
            tracks.append({**track, "recommendDimension": segment, "recommendReason": reasons[segment]})
            used.add(guid)
            used_keys.add(key)
            added += 1

    for segment, quota in quotas.items():
        take(segment, quota)
    for segment in ("preference", "explore", "new"):
        take(segment, target - len(tracks))
    if not tracks:
        return empty
    published = time.time()
    bundle = {**empty, "engine": _LLM_ENGINE, "user": user_guid, "tracks": tracks,
              "builtAt": published, "publishedAt": published, "firstFetchedAt": None,
              "batchId": uuid4().hex, "configFingerprint": fingerprint, "sourceFingerprint": source_at_start,
              "playlist": rec.build_playlist_record(empty["guid"], "猜你喜欢", empty["guid"], len(tracks), int(published))}
    with _lock_for(path):
        if not current():
            return empty
        try:
            if not _valid_batch(bundle, user_guid, fingerprint, db_path) or not current():
                return empty
        except (sqlite3.Error, ValueError, TypeError):
            return empty
        if not rec._atomic_write_json(path, bundle):
            return empty
        _unlink(_liked_state_path(user_guid))
    return bundle
