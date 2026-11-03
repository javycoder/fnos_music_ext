"""自定义歌单：歌单文件导入解析、定义存储与去重合并（纯标准库，无网络）。

职责：
1. 解析 m3u/m3u8/pls/cue 文本为歌名歌手列表，异常行跳过并计数；
2. 定义文件落数据卷自定义歌单目录（JSON），支持独立启用停用删除；
3. 手动列表与生成列表去重合并；
4. 自定义缓存文件清理（proxy 与 webui 共用）。

歌单定义文件：<custom_dir>/<id>.json
  {"id","name","enabled","source","tracks":[{"title","artist"}],"createdAt","updatedAt"}
定义变更即时失效缓存：proxy 下发时比对定义 updatedAt 与缓存 payload 的
definitionUpdatedAt，不一致即重建；切源时由统一失效规则清 custom-* 缓存。
"""
from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import re
import time
from uuid import uuid4

logger = logging.getLogger("fnmusic_proxy.customplaylists")

CUSTOM_GUID_PREFIX = "online:playlist:custom:"
SUPPORTED_EXTS = (".m3u", ".m3u8", ".pls", ".cue")
_MAX_TRACKS = 1000
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def home_dir() -> str:
    """FNMUSIC_HOME > WEBUI_REPO_DIR > 仓库根（容器 /srv 兜底不做默认数据目录）。"""
    for key in ("FNMUSIC_HOME", "WEBUI_REPO_DIR"):
        env = (os.environ.get(key) or "").strip()
        if env:
            return env
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def custom_dir() -> str:
    """自定义歌单定义目录：显式配置优先，否则落数据卷（/data 存在时），最后回落仓库。"""
    env = (os.environ.get("FNMUSIC_CUSTOM_PLAYLISTS_DIR") or "").strip()
    if env:
        return env
    if os.path.isdir("/data"):
        return os.path.join("/data", "custom_playlists")
    return os.path.join(home_dir(), "custom_playlists")


def recommend_dir() -> str:
    """推荐缓存根（与 recommend.recommend_cache_dir 同口径的轻量实现，避免循环导入）。"""
    env = (os.environ.get("FNMUSIC_RECOMMEND_DIR") or "").strip()
    if env:
        return env
    return os.path.join(home_dir(), "recommend_cache")


def ensure_custom_dir(path: str | None = None) -> str:
    target = path or custom_dir()
    os.makedirs(target, exist_ok=True)
    return target


def _safe_id(pid: str) -> str:
    text = str(pid or "").strip()
    if not _ID_RE.fullmatch(text):
        raise ValueError(f"自定义歌单 id 非法: {pid!r}")
    return text


def _definition_path(pid: str, directory: str | None = None) -> str:
    return os.path.join(directory or custom_dir(), f"{_safe_id(pid)}.json")


def _atomic_write_json(path: str, payload: dict) -> bool:
    part = f"{path}.{uuid4().hex[:8]}.part"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(part, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(part, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return True
    except Exception as e:
        logger.warning("custom playlist write %s failed: %s", path, e)
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass
        return False


def normalize_track(title: str = "", artist: str = "") -> dict:
    return {"title": str(title or "").strip(), "artist": str(artist or "").strip()}


def _split_artist_title(text: str) -> tuple[str, str]:
    """“歌手 - 歌名”切分：兼容常见连接符，切不出则全归歌名，歌手留空。"""
    raw = str(text or "").strip()
    if not raw:
        return "", ""
    for sep in (" - ", " – ", " — ", "–", "—"):
        if sep in raw:
            left, right = raw.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right:
                return right, left
            # “ - ”仅作分隔符时无有效歌名，交由调用方计为跳过
            if sep.strip() == "" or not right:
                return "", ""
    if " - " in raw:
        left, right = raw.split(" - ", 1)
        return right.strip(), left.strip()
    return raw, ""


def _is_valid_title(title: str) -> bool:
    text = str(title or "").strip()
    if not text:
        return False
    # 仅剩分隔符的“歌名”（如 EXTINF 里的 “ - ”）视为异常行
    if text in ("-", "–", "—") or not text.strip("-–— "):
        return False
    return True


def _basename_title(path: str) -> str:
    base = str(path or "").strip().split("?")[0].split("#")[0]
    base = base.replace("\\", "/").rstrip("/").split("/")[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return base.replace("_", " ").strip()


def _parse_m3u(content: str) -> tuple[list[dict], int]:
    tracks: list[dict] = []
    skipped = 0
    pending = ""
    for raw in str(content or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if line.upper().startswith("#EXTINF"):
                pending = line.split(",", 1)[1].strip() if "," in line else ""
            continue
        title, artist = "", ""
        if pending:
            title, artist = _split_artist_title(pending)
            if not _is_valid_title(title):
                # EXTINF 自身异常：计一次跳过，仍按文件名单兜底解析本行
                skipped += 1
                pending = ""
                title, artist = _split_artist_title(_basename_title(line) or line)
            else:
                pending = ""
        else:
            title, artist = _split_artist_title(_basename_title(line) or line)
        if not _is_valid_title(title):
            skipped += 1
            continue
        tracks.append(normalize_track(title, artist))
        if len(tracks) >= _MAX_TRACKS:
            break
    if pending and not _is_valid_title(_split_artist_title(pending)[0]):
        # 尾部悬空的异常 EXTINF 计一次跳过
        skipped += 1
    return tracks, skipped


def _parse_pls(content: str) -> tuple[list[dict], int]:
    titles: dict[str, str] = {}
    files: dict[str, str] = {}
    for raw in str(content or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().lower(), value.strip()
        m = re.fullmatch(r"(title|file)(\d+)", key)
        if not m or not value:
            continue
        (titles if m.group(1) == "title" else files)[m.group(2)] = value
    tracks: list[dict] = []
    skipped = 0
    for num in sorted(set(titles) | set(files), key=lambda x: int(x)):
        raw_title = titles.get(num, "")
        if raw_title:
            title, artist = _split_artist_title(raw_title)
        else:
            title, artist = _split_artist_title(_basename_title(files.get(num, "")))
        if not _is_valid_title(title):
            skipped += 1
            continue
        tracks.append(normalize_track(title, artist))
        if len(tracks) >= _MAX_TRACKS:
            break
    return tracks, skipped


def _parse_cue(content: str) -> tuple[list[dict], int]:
    tracks: list[dict] = []
    skipped = 0
    global_artist = ""
    cur_title, cur_artist, in_track = "", "", False
    quoted = re.compile(r'"([^"]*)"')

    def _flush() -> None:
        nonlocal cur_title, cur_artist, in_track, skipped
        if not in_track:
            return
        title = cur_title.strip()
        artist = (cur_artist or global_artist).strip()
        if title:
            tracks.append(normalize_track(title, artist))
        else:
            skipped += 1
        cur_title, cur_artist, in_track = "", "", False

    for raw in str(content or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("TRACK "):
            _flush()
            in_track = True
            cur_title, cur_artist = "", ""
        elif upper.startswith("TITLE "):
            m = quoted.search(line)
            value = m.group(1).strip() if m else line[5:].strip().strip('"')
            if in_track:
                cur_title = value
        elif upper.startswith("PERFORMER "):
            m = quoted.search(line)
            value = m.group(1).strip() if m else line[9:].strip().strip('"')
            if in_track:
                cur_artist = value
            elif not global_artist:
                global_artist = value
        if len(tracks) >= _MAX_TRACKS:
            break
    _flush()
    return tracks, skipped


def parse_playlist_file(content: str, filename: str = "") -> tuple[list[dict], int]:
    """按扩展名解析歌单文件，返回 (曲目列表, 跳过行数)。未知扩展名按 m3u 兜底。"""
    ext = os.path.splitext(str(filename or "").lower())[1]
    text = str(content or "").lstrip("﻿")
    # 空文件视为 0 首 0 跳过，不报错，由调用方提示空歌单。
    if not text.strip():
        return [], 0
    if ext == ".pls":
        return _parse_pls(text)
    if ext == ".cue":
        return _parse_cue(text)
    return _parse_m3u(text)


def dedupe_merge(base: list[dict] | None, extra: list[dict] | None, limit: int = _MAX_TRACKS) -> list[dict]:
    """手动列表与生成列表去重合并：保序，歌名歌手大小写不敏感去重，空标题跳过。"""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []

    def _key(title: str, artist: str) -> tuple[str, str]:
        return (str(title or "").strip().lower(), str(artist or "").strip().lower())

    for item in list(base or []) + list(extra or []):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or "").strip()
        artist = str(item.get("artist") or "").strip()
        if not title:
            continue
        key = _key(title, artist)
        if key in seen:
            continue
        seen.add(key)
        out.append(normalize_track(title, artist))
        if len(out) >= max(1, limit):
            break
    return out


def create_definition(name: str, tracks: list[dict] | None = None, source: str = "manual",
                      directory: str | None = None) -> dict:
    now = int(time.time())
    data = {
        "id": uuid4().hex[:12],
        "name": str(name or "").strip() or "未命名歌单",
        "enabled": True,
        "source": source if source in ("manual", "import", "prompt") else "manual",
        "tracks": dedupe_merge(None, tracks),
        "createdAt": now,
        "updatedAt": now,
    }
    ensure_custom_dir(directory or custom_dir())
    _atomic_write_json(_definition_path(data["id"], directory), data)
    return data


def list_definitions(directory: str | None = None) -> list[dict]:
    root = directory or custom_dir()
    out: list[dict] = []
    try:
        names = os.listdir(root)
    except OSError:
        return []
    for name in sorted(names):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(root, name), "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict) or not data.get("id"):
            continue
        tracks = data.get("tracks")
        out.append({
            "id": str(data.get("id")),
            "name": str(data.get("name") or "未命名歌单"),
            "enabled": bool(data.get("enabled", True)),
            "source": str(data.get("source") or "manual"),
            "trackCount": len(tracks) if isinstance(tracks, list) else 0,
            "createdAt": data.get("createdAt") or 0,
            "updatedAt": data.get("updatedAt") or 0,
        })
    return out


def load_definition(pid: str, directory: str | None = None) -> dict | None:
    try:
        with open(_definition_path(pid, directory), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict) or str(data.get("id") or "") != _safe_id(pid):
        return None
    tracks = [normalize_track(t.get("title"), t.get("artist"))
              for t in (data.get("tracks") or []) if isinstance(t, dict)]
    tracks = [t for t in tracks if t["title"]]
    data["tracks"] = tracks[:_MAX_TRACKS]
    return data


def save_definition(data: dict, directory: str | None = None) -> bool:
    if not isinstance(data, dict) or not data.get("id"):
        return False
    pid = _safe_id(str(data["id"]))
    payload = {
        "id": pid,
        "name": str(data.get("name") or "").strip() or "未命名歌单",
        "enabled": bool(data.get("enabled", True)),
        "source": str(data.get("source") or "manual"),
        "tracks": dedupe_merge(None, data.get("tracks")),
        "createdAt": int(data.get("createdAt") or time.time()),
        "updatedAt": int(time.time()),
    }
    return _atomic_write_json(_definition_path(pid, directory), payload)


def set_enabled(pid: str, enabled: bool, directory: str | None = None) -> dict | None:
    data = load_definition(pid, directory)
    if data is None:
        return None
    data["enabled"] = bool(enabled)
    if not save_definition(data, directory):
        return None
    return load_definition(pid, directory)


def delete_definition(pid: str, directory: str | None = None) -> bool:
    """硬删除定义文件，调用方需先经用户确认；磁盘缓存由 purge_custom_cache_files 同步清理。"""
    try:
        os.remove(_definition_path(pid, directory))
        return True
    except FileNotFoundError:
        return True
    except OSError as e:
        logger.warning("custom playlist delete %s failed: %s", pid, e)
        return False


def playlist_guid(pid: str) -> str:
    return f"{CUSTOM_GUID_PREFIX}{_safe_id(pid)}"


def id_from_guid(guid: str | None) -> str:
    s = str(guid or "")
    if not s.startswith(CUSTOM_GUID_PREFIX):
        return ""
    tail = s[len(CUSTOM_GUID_PREFIX):].split(":")[0]
    return tail if _ID_RE.fullmatch(tail or "") else ""


def is_custom_playlist_guid(guid: str | None) -> bool:
    return bool(id_from_guid(guid))


def purge_custom_cache_files(pid: str | None = None, root: str | None = None) -> int:
    """删除自定义歌单磁盘缓存（含 fallback）：pid 为空则清全部 custom-*。返回删除数。"""
    base = root or recommend_dir()
    removed = 0
    try:
        users = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
    except OSError:
        return 0
    infix = f"custom-{pid}-" if pid else "custom-"
    for user in users:
        folder = os.path.join(base, user)
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".json") or not name.startswith(infix):
                continue
            try:
                os.remove(os.path.join(folder, name))
                removed += 1
            except OSError:
                continue
    return removed


def definition_fingerprint(data: dict) -> str:
    tracks = data.get("tracks") if isinstance(data, dict) else None
    count = len(tracks) if isinstance(tracks, list) else 0
    updated = str((data or {}).get("updatedAt") or "")
    raw = f"{updated}:{count}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
