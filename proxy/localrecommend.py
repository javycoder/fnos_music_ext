"""Account-local recommendations from read-only library data."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import sqlite3
import threading
import time
from collections import Counter
from uuid import uuid4

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


def select_tracks(candidates: list[dict], profile: dict, user_guid: str, generation: str, now: float) -> list[dict]:
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

    for index in range(min(PLAYLIST_SIZE, len(remaining))):
        if index < 16:
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
            tracks = select_tracks(candidates, profile, user_guid, uuid4().hex, now)
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


async def get_bundle(user_guid: str, db_path: str) -> dict:
    return await asyncio.to_thread(_load_or_build, user_guid, db_path)
