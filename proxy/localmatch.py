"""Read-only exact library matching, file fingerprints and bounded probe/fault caches."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata

logger = logging.getLogger("fnmusic_proxy.localmatch")

MISS_TTL_S = 30.0        # 未命中阴性缓存上限：过期即重查，新入库歌曲最迟 30s 可见
HIT_TTL_S = 600.0        # 上次命中指纹；实际候选每次重查以排除软删除
FAULT_TTL_S = 300.0      # 故障指纹：5 分钟内跳过
SERVED_TTL_S = 900.0     # “已向客户端供过本地字节”标记的时效
PROBE_TIMEOUT_S = 2.0    # 音轨探测上限（秒）
_ENTRY_CAP = 2048        # 各注册表容量硬顶（防进程生命周期内无限增长）

_LOCK = threading.Lock()
_HITS: dict[str, tuple[dict, float]] = {}
_MISSES: dict[str, float] = {}
_FAULTS: dict[str, tuple[str, int, float, float]] = {}   # guid -> (path, size, mtime, until)
_SERVED: dict[str, tuple[str, int, float, float]] = {}   # guid -> (path, size, mtime, until)
_PROBE_OK: dict[tuple[str, int, float], dict] = {}

# 歌手分隔符：中西文常见并列写法；& 不拆（乐队名常含 &，误拆会造成错误命中）
_ARTIST_SPLIT_RE = re.compile(
    r"[/、，,;；|]| feat\. | ft\. | featuring | with ", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_DB_ARTIST_SEP = "\x1f"   # GROUP_CONCAT 分隔符（与官方库读取口径一致）


def normalize_text(value: str) -> str:
    """匹配用规范化：NFKC + casefold + 首尾去空白 + 连续空白折叠。"""
    text = unicodedata.normalize("NFKC", str(value or ""))
    return _WS_RE.sub(" ", text).strip().casefold()


def title_key(value: str) -> str:
    return normalize_text(value)


def split_artist_set(value: str) -> frozenset[str]:
    """拆出规范化后的完整歌手集合；空串返回空集合。"""
    raw = unicodedata.normalize("NFKC", str(value or ""))
    if _DB_ARTIST_SEP in raw:
        parts = raw.split(_DB_ARTIST_SEP)
    else:
        parts = _ARTIST_SPLIT_RE.split(raw)
    return frozenset(name for name in (title_key(p) for p in parts) if name)


_CANDIDATE_SQL = """
    SELECT t.id, t.guid, t.title, t.duration_ms,
           af.path, af.size, af.suffix,
           al.name AS album,
           (SELECT GROUP_CONCAT(a.name, char(31))
              FROM track_artist ta JOIN artist a ON a.id = ta.artist_id
             WHERE ta.track_id = t.id) AS artist_values
      FROM track t
      JOIN audio_file af ON af.id = t.audio_file_id
      LEFT JOIN album al ON al.id = t.album_id
     WHERE local_title(t.title) = ?
       AND t.is_audio_file_deleted = 0
       AND t.is_admin_deleted = 0
       AND t.is_cue = 0
       AND af.is_physical_file_deleted = 0
"""


def _readable_audio_file(path: str) -> tuple[int, float] | None:
    """文件在盘、非空且可读才可用；返回 (size, mtime) 指纹。"""
    try:
        if not os.path.isfile(path) or not os.access(path, os.R_OK):
            return None
        stat = os.stat(path)
        if stat.st_size <= 0:
            return None
        return int(stat.st_size), float(stat.st_mtime)
    except OSError:
        return None


def _select_candidate(rows: list[dict], needle_title: str, needle_artists: frozenset[str],
                      needle_album: str) -> dict | None:
    """Python 侧精确复核（规范化相等 + 完整歌手集合），再按专辑优先/ID 降序选择。"""
    verified: list[dict] = []
    for row in rows:
        if title_key(row.get("title") or "") != needle_title:
            continue
        if split_artist_set(row.get("artist_values") or "") != needle_artists:
            continue
        row = dict(row)
        row["fingerprint"] = _readable_audio_file(str(row.get("path") or ""))
        if row["fingerprint"] is None:
            continue
        verified.append(row)
    if not verified:
        return None
    album_norm = normalize_text(needle_album)
    same_album = [row for row in verified
                  if album_norm and normalize_text(row.get("album") or "") == album_norm]
    pool = same_album or verified
    chosen = max(pool, key=lambda row: int(row.get("id") or 0))
    size, mtime = chosen["fingerprint"]
    try:
        duration_s = float(chosen.get("duration_ms") or 0) / 1000.0
    except (TypeError, ValueError):
        duration_s = 0.0
    return {
        "path": str(chosen.get("path") or ""),
        "size": size,
        "mtime": mtime,
        "ext": str(chosen.get("suffix") or "").lstrip(".").lower() or "mp3",
        "duration_s": duration_s,
        "album": str(chosen.get("album") or ""),
        "track_guid": str(chosen.get("guid") or ""),
        "select_reason": "album" if same_album else "latest",
    }


def resolve_in_library(db_path: str, title: str, artist: str, album: str = "") -> dict | None:
    """按规范化歌名+完整歌手集合在官方曲库匹配可播本地文件；无命中返回 None。

    SQLite 使用同一 Python Unicode 归一化函数筛选全部候选，无数量截断。
    """
    title_s = str(title or "").strip()
    if not title_s or not str(artist or "").strip():
        return None
    if not db_path or not os.path.exists(db_path):
        raise sqlite3.OperationalError("library unavailable")
    needle_title = title_key(title_s)
    needle_artists = split_artist_set(artist)
    if not needle_artists:
        return None
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    con.create_function("local_title", 1, title_key, deterministic=True)
    try:
        rows = [dict(row) for row in con.execute(_CANDIDATE_SQL, (needle_title,))]
        return _select_candidate(rows, needle_title, needle_artists, album)
    finally:
        con.close()


def resolve_guid(guid: str, needle_fn, db_path: str) -> "dict | None":
    """按 guid 解析本地播放匹配（含正/阴性缓存）；needle_fn 返回 (title, artist, album)。

    命中结果附带触发匹配的在线元数据 meta，供元数据端点零网络重建展示信息。
    """
    now = time.monotonic()
    with _LOCK:
        hit = _HITS.get(guid)
        if hit and now < hit[1]:
            current = _readable_audio_file(hit[0]["path"])
            # Re-query flags and candidate ordering as the read-only DB may change.
            if current is None:
                _FAULTS[guid] = (hit[0]["path"], hit[0]["size"], hit[0]["mtime"],
                                 now + FAULT_TTL_S)
            _HITS.pop(guid, None)
            _MISSES.pop(guid, None)
        if guid not in _FAULTS and guid in _MISSES and now < _MISSES[guid]:
            return None
    meta = {}
    try:
        values = tuple(needle_fn() or ())
        if len(values) >= 3:
            meta = {
                "title": str(values[0] or ""),
                "artist": str(values[1] or ""),
                "album": str(values[2] or ""),
                "cover_url": str(values[3]) if len(values) > 3 and values[3] else "",
            }
    except Exception as e:  # noqa: BLE001 — 元数据提取失败按未命中处理，不阻断播放
        logger.debug("local match needle failed for %s: %s", guid, type(e).__name__)
    title = meta.get("title") or ""
    artist = meta.get("artist") or ""
    album = meta.get("album") or ""
    match = None
    if title and artist:
        try:
            match = resolve_in_library(db_path, title, artist, album)
        except sqlite3.Error as e:
            logger.debug("local match db error for %s: %s", guid, type(e).__name__)
            return None   # 数据库异常不写阴性缓存
    now = time.monotonic()
    with _LOCK:
        if match is not None:
            match = {**match, "meta": dict(meta)}
            _HITS[guid] = (dict(match), now + HIT_TTL_S)
            _MISSES.pop(guid, None)
            if len(_HITS) > _ENTRY_CAP:
                _HITS.clear()
        else:
            _MISSES[guid] = now + MISS_TTL_S
            _HITS.pop(guid, None)
            if len(_MISSES) > _ENTRY_CAP:
                _MISSES.clear()
    return dict(match) if match else None


def invalidate(guid: str) -> None:
    """登记新元数据时失效该 guid 的匹配缓存（新入库歌曲立即可见）。"""
    with _LOCK:
        _HITS.pop(guid, None)
        _MISSES.pop(guid, None)


def fault_active(guid: str, match: dict) -> bool:
    """该 guid 当前匹配文件是否处于故障跳过窗口（指纹变化即放行重试）。"""
    entry = _FAULTS.get(guid)
    if entry is None:
        return False
    path, size, mtime, until = entry
    now = time.monotonic()
    if now >= until:
        _FAULTS.pop(guid, None)
        return False
    if (path, size, mtime) != (match.get("path"), match.get("size"), match.get("mtime")):
        return False
    return True


def record_fault(guid: str, match: dict, reason: str = "") -> None:
    """记录故障文件指纹（路径+大小+mtime），5 分钟内跳过本地直连。"""
    with _LOCK:
        if len(_FAULTS) > _ENTRY_CAP:
            _FAULTS.clear()
        _FAULTS[guid] = (str(match.get("path") or ""), int(match.get("size") or 0),
                         float(match.get("mtime") or 0.0), time.monotonic() + FAULT_TTL_S)
    if reason:
        logger.info("local-first fault recorded for %s: %s", guid, reason)


def mark_served(guid: str, match: dict) -> None:
    """记录已向客户端供出本地字节（判定续拉 Range 是否来自本地布局）。"""
    with _LOCK:
        if len(_SERVED) > _ENTRY_CAP:
            _SERVED.clear()
        _SERVED[guid] = (str(match.get("path") or ""), int(match.get("size") or 0),
                         float(match.get("mtime") or 0.0),
                         time.monotonic() + SERVED_TTL_S)


def range_from_local_layout(guid: str) -> bool:
    """客户端当前 Range 偏移是否可能来自本地文件字节布局。

    仅在近期实际供出过本地字节、且同一文件随后被记故障时为真：此时本地中
    途失败，续拉偏移若套到在线文件会错位断流，应整段重起播。
    """
    with _LOCK:
        mark = _SERVED.get(guid)
        fault = _FAULTS.get(guid)
    if not mark or not fault:
        return False
    now = time.monotonic()
    if now >= mark[3] or now >= fault[3]:
        return False
    return mark[:3] == fault[:3]


def probe_audio(match: dict) -> "dict | None":
    """起播前校验：文件可开、可读首块且含可解析音轨；成功结果按指纹缓存。

    返回实测时长及 audio_spec；失败不缓存，由调用方记录故障指纹。
    """
    path = str(match.get("path") or "")
    size = int(match.get("size") or 0)
    mtime = float(match.get("mtime") or 0.0)
    key = (path, size, mtime)
    cached = _PROBE_OK.get(key)
    if cached is not None:
        return dict(cached)
    try:
        with open(path, "rb") as fp:
            if not fp.read(64 * 1024):
                return None
        from mutagen import File as MutagenFile

        media = MutagenFile(path)
        if media is None or media.info is None:
            return None
        length = getattr(media.info, "length", 0) or 0
        spec = {}
        for field, attr in (("channel", "channels"), ("sampleRate", "sample_rate"),
                            ("bitDepth", "bits_per_sample"), ("bitrate", "bitrate")):
            value = getattr(media.info, attr, None)
            if value:
                spec[field] = int(value)
    except Exception as e:  # noqa: BLE001 — 探测失败视同文件不可播
        logger.debug("local probe failed: %s", type(e).__name__)
        return None
    result = {"duration_s": float(length) if length > 0 else float(match.get("duration_s") or 0),
              "audio_spec": spec}
    if len(_PROBE_OK) > _ENTRY_CAP:
        _PROBE_OK.clear()
    _PROBE_OK[key] = dict(result)
    return result


async def probe_audio_async(match: dict) -> "dict | None":
    """带 2 秒上限的异步探测（超时按失败处理，线程自然结束不强杀）。"""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(probe_audio, match), timeout=PROBE_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — 超时/线程异常均按探测失败处理
        return None


def reset_caches() -> None:
    """测试辅助：清空全部进程内缓存/注册表。"""
    with _LOCK:
        _HITS.clear()
        _MISSES.clear()
        _FAULTS.clear()
        _SERVED.clear()
    _PROBE_OK.clear()
