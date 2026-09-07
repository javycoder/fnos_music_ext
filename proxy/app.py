"""fnmusic-ext 拦截代理 (FastAPI + httpx).

功能：
1. 通用透传：所有非拦截路径原样转发到 trim-music unix socket
2. 搜索合并：GET /music/api/v1/search/track* （兼容 q/keyword，并行 musicdl）
3. 在线播放：stream + HLS 兜底 + transcode 空操作 + tee 缓存回放（音频与歌词 sidecar）
4. 在线元数据/歌词/封面
5. GET /_ext/healthz
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Callable, Coroutine
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

try:
    from . import recommend as dailyrec
    from .version import get_version
except ImportError:  # uvicorn --app-dir proxy
    import recommend as dailyrec  # type: ignore
    from version import get_version  # type: ignore

logger = logging.getLogger("fnmusic_proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_HOME = dailyrec.home_dir()

CONF = {
    "musicdl_url": os.environ.get("FNMUSIC_MUSICDL_URL", "http://127.0.0.1:8768"),
    "musicbox_url": os.environ.get("FNMUSIC_MUSICBOX_URL", "http://127.0.0.1:8770"),
    "musicdl_enabled": os.environ.get("FNMUSIC_MUSICDL_ENABLED", "true").lower() in ("true", "1", "yes"),
    "netease_enabled": os.environ.get("FNMUSIC_NETEASE_ENABLED", "true").lower() in ("true", "1", "yes"),
    "netease_wait_s": float(os.environ.get("FNMUSIC_NETEASE_WAIT_S", "2.5")),
    "netease_quality": os.environ.get("FNMUSIC_NETEASE_QUALITY", "lossless"),
    "netease_search_limit": int(os.environ.get("FNMUSIC_NETEASE_SEARCH_LIMIT", "50")),
    "upstream_sock": os.environ.get("FNMUSIC_UPSTREAM_SOCK", "/var/run/trim_music_upstream.socket"),
    "online_limit": int(os.environ.get("FNMUSIC_ONLINE_LIMIT", "30")),
    "search_list_path": os.environ.get("FNMUSIC_SEARCH_LIST_PATH", "data.list"),
    "cache_dir": os.environ.get("FNMUSIC_CACHE_DIR", os.path.join(_HOME, "cache")),
    # 空=从飞牛 shared_library.path 自动探测；测试可覆盖到临时目录
    "library_dir": os.environ.get("FNMUSIC_LIBRARY_DIR", ""),
    "music_db": os.environ.get(
        "FNMUSIC_MUSIC_DB", "/usr/local/apps/@appdata/trim.music/db/music.db"
    ),
    "merge_suggest": os.environ.get("FNMUSIC_MERGE_SUGGEST", "false").lower() in ("true", "1", "yes"),
    "online_sources": os.environ.get("FNMUSIC_ONLINE_SOURCES", "KuwoMusicClient,MiguMusicClient"),
    "lyric_field": os.environ.get("FNMUSIC_LYRIC_FIELD", "data.lyric"),
    "search_timeout": float(os.environ.get("FNMUSIC_SEARCH_TIMEOUT", "15")),
    "search_cache_ttl": float(os.environ.get("FNMUSIC_SEARCH_CACHE_TTL", "300")),
    "late_page_wait_s": float(os.environ.get("FNMUSIC_LATE_PAGE_WAIT_S", "5")),
    "fav_dir": os.environ.get(
        "FNMUSIC_FAV_DIR", os.path.join(_HOME, "online_favorites")
    ),
    "llm_base_url": (os.environ.get("FNMUSIC_LLM_BASE_URL") or "").strip().rstrip("/"),
    "llm_model": (os.environ.get("FNMUSIC_LLM_MODEL") or "gpt-4o-mini").strip() or "gpt-4o-mini",
}

_REDACT_KEY_PARTS = ("api_key", "apikey", "token", "secret", "password")

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

CACHE_EXTS = ("mp3", "flac", "wav", "ogg", "opus", "m4a", "aac", "ape", "wv", "dsf", "dff", "tta")

# 飞牛 Kl() 归一化：mpeg/mp3→mp3，wav/pcm→wav，m4a/aac/mp4→m4a，其余小写原样（flac/ogg/ape/wv…）
_FORMAT_ALIASES = {
    "mp3": "mp3",
    "mpeg": "mp3",
    "mpga": "mp3",
    "flac": "flac",
    "wav": "wav",
    "wave": "wav",
    "pcm": "wav",
    "lpcm": "wav",
    "ogg": "ogg",
    "vorbis": "ogg",
    "opus": "opus",
    "m4a": "m4a",
    "mp4": "m4a",
    "mp4a": "m4a",
    "aac": "m4a",
    "alac": "m4a",
    "ape": "ape",
    "wv": "wv",
    "wavpack": "wv",
    "dsf": "dsf",
    "dff": "dff",
    "dsd": "dsd",
    "tta": "tta",
    "tak": "tak",
    "wma": "wma",
    "aiff": "aiff",
    "aif": "aiff",
}


# 模块级搜索缓存
_SEARCH_CACHE: dict[str, dict] = {}


def _clean_search_cache() -> None:
    """写入时若 len(_SEARCH_CACHE) > 200，按 ts 升序砍掉最旧一半。"""
    if len(_SEARCH_CACHE) > 200:
        sorted_keys = sorted(_SEARCH_CACHE.keys(), key=lambda k: _SEARCH_CACHE[k].get("ts", 0))
        to_remove = sorted_keys[: len(sorted_keys) // 2]
        for k in to_remove:
            _SEARCH_CACHE.pop(k, None)


def _set_search_cache(keyword: str, entry: dict) -> None:
    _clean_search_cache()
    _SEARCH_CACHE[keyword] = entry


def deduplicate_online_items(items: list[dict]) -> list[dict]:
    """在线条目合并去重：按 (title, artist) 小写，保留最先出现的（musicbox 优先）。"""
    seen = set()
    res = []
    for it in items:
        t = str(it.get("title") or it.get("name") or "").strip().lower()
        a = str(it.get("artist") or "").strip().lower()
        if t and a:
            key = (t, a)
            if key in seen:
                continue
            seen.add(key)
        res.append(it)
    return res


def play_format_from_ext(ext: str | None) -> str:
    raw = (ext or "mp3").strip().lower().lstrip(".")
    if raw.startswith("audio/"):
        raw = raw.split("/", 1)[-1]
    return _FORMAT_ALIASES.get(raw, raw or "mp3")


def filter_headers(headers: Any, exclude_keys: set | None = None) -> dict:
    exclude = HOP_BY_HOP | {k.lower() for k in (exclude_keys or set())}
    return {k: v for k, v in headers.items() if k.lower() not in exclude}


def copy_incoming_headers(request: Request) -> dict:
    """透传鉴权 Cookie / Token。Starlette 头名为小写，需显式回填以免丢失 music-token。"""
    headers = filter_headers(request.headers, exclude_keys={"host", "content-length"})
    headers["accept-encoding"] = "identity"
    for key in ("cookie", "authorization", "x-trim-music-temp-token"):
        val = request.headers.get(key)
        if val:
            headers[key] = val
    return headers


def get_by_path(d: Any, path: str) -> Any:
    curr = d
    for p in path.split("."):
        if isinstance(curr, dict) and p in curr:
            curr = curr[p]
        else:
            return None
    return curr


def set_by_path(d: dict, path: str, val: Any):
    parts = path.split(".")
    curr = d
    for p in parts[:-1]:
        if p not in curr or not isinstance(curr[p], dict):
            curr[p] = {}
        curr = curr[p]
    curr[parts[-1]] = val


def extract_keyword(request: Request) -> str:
    """前端打包用 q，部分调用/验收用 keyword。"""
    params = request.query_params
    return (params.get("keyword") or params.get("q") or params.get("query") or "").strip()


def online_guid_from_item(item: dict) -> str:
    raw_id = str(item.get("id") or "")
    src = str(item.get("source") or "")
    if raw_id.startswith("online:"):
        return raw_id
    if ":" in raw_id:
        return f"online:{raw_id}"
    return f"online:{src}:{raw_id}"


def song_id_from_online_guid(guid: str) -> str:
    if guid.startswith("online:"):
        return guid[len("online:") :]
    return guid


def is_online_guid(guid: str) -> bool:
    return bool(guid) and guid.startswith("online:")


def source_from_online_guid(guid: str) -> str:
    parts = (guid or "").split(":")
    return parts[1] if len(parts) >= 3 else ""


def build_online_track(item: dict) -> dict:
    """对齐飞牛前端 ZQ 解构 / _h() 期望：artists、album 对象、genres 数组、audioSpec、duration 毫秒。"""
    guid = online_guid_from_item(item)
    src = str(item.get("source") or source_from_online_guid(guid) or "")
    title = str(item.get("title") or item.get("name") or "")
    artist = str(item.get("artist") or "")
    album = str(item.get("album") or "")
    duration_s = item.get("duration_s") or 0
    try:
        duration_s = float(duration_s)
    except (TypeError, ValueError):
        duration_s = 0
    duration_ms = int(duration_s * 1000)
    ext = str(item.get("ext") or "mp3") or "mp3"
    play_format = play_format_from_ext(ext)
    file_size = item.get("file_size") or 0
    try:
        file_size = int(file_size or 0)
    except (TypeError, ValueError):
        file_size = 0
    cover = str(item.get("cover_url") or "")
    # 路径带真实后缀，飞牛 ll() 用 path 解析 extension；封面走 guid 以便 /static/cover 拦截
    spec_path = f"online/{src}/{guid}.{play_format}"

    artists_list = [{"name": artist, "guid": f"{guid}:artist"}] if artist else []
    album_obj = {
        "name": album,
        "guid": f"{guid}:album",
        "artists": artists_list,
        "coverId": guid,
    }
    audio_spec = {
        "path": spec_path,
        "format": play_format,
        "codec": play_format,
        "container": play_format,
        "duration": duration_ms,
        "size": file_size,
        "channel": 2,
        "sampleRate": 44100,
        "bitDepth": 16 if play_format in ("wav", "flac", "aiff") else None,
        "bitrate": 1411000 if play_format in ("flac", "wav", "ape", "wv") else 320000,
    }
    audio_spec = {k: v for k, v in audio_spec.items() if v is not None}

    return {
        "guid": guid,
        "id": guid,
        "title": title,
        "name": title,
        "artist": artist,
        "artists": artists_list,
        "album": album_obj,
        "albumName": album,
        "audioSpec": audio_spec,
        "duration": duration_ms,
        "duration_ms": duration_ms,
        "durationMs": duration_ms,
        "duration_s": duration_s,
        "codec": play_format,
        "codecName": play_format,
        "format": play_format,
        "ext": ext,
        "size": file_size,
        "file_size": file_size,
        "coverId": guid,
        "cover_url": cover,
        "coverUrl": cover,
        "coverURL": cover,
        "source": src,
        "is_online": True,
        "isFavorite": False,
        "isCue": False,
        "hasLyric": bool(item.get("lyric")),
        "genres": [],
        "accessStatus": 0,
    }


def artist_from_track(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    a = item.get("artist") or item.get("singer") or item.get("singers") or ""
    if isinstance(a, list):
        names = []
        for x in a:
            if isinstance(x, dict):
                names.append(str(x.get("name") or ""))
            else:
                names.append(str(x))
        return " ".join(n for n in names if n).strip().lower()
    if isinstance(a, dict):
        return str(a.get("name") or "").strip().lower()
    return str(a).strip().lower()


def title_from_track(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("title") or item.get("name") or "").strip().lower()


def should_cache(range_header: str | None) -> bool:
    """完整拉取才落盘：无 Range，或 bytes=0-（开区间）。Safari bytes=0-1 探测不落盘。"""
    if not range_header:
        return True
    r = range_header.strip().lower()
    return bool(re.match(r"^bytes=0-$", r))


def is_range_from_zero_or_none(range_header: str | None) -> bool:
    return should_cache(range_header)


def cache_safe_guid(guid: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", guid)


def online_file_id(guid: str) -> str:
    """online:migu:600929… → 600929…，仅用于查找旧文件，不再写进文件名。"""
    return song_id_from_online_guid(guid).rsplit(":", 1)[-1]


def safe_basename_title(title: str) -> str:
    t = re.sub(r'[/\\:\0]', "_", (title or "").strip()) or "unknown"
    t = re.sub(r"\s+", " ", t).strip(" .")
    return t[:120]


def library_basename(title: str, artist: str = "") -> str:
    """曲库文件名：歌手 - 歌名（无源站 id）。飞牛无标签时会用文件名当标题。"""
    title_s = safe_basename_title(title)
    artist_s = safe_basename_title(artist) if (artist or "").strip() else ""
    if artist_s and artist_s.lower() != title_s.lower() and artist_s != "unknown":
        return f"{artist_s} - {title_s}"
    return title_s


def media_ref_path(guid: str) -> str:
    return os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.ref")


def _path_stem(path: str) -> str:
    root, ext = os.path.splitext(path)
    known = set(CACHE_EXTS) | {"lrc", "part"}
    if ext.lstrip(".").lower() in known:
        return root
    return path


def remember_media_path(guid: str, media_path: str) -> None:
    """记住曲库里的文件词干（不含扩展名），音频和 .lrc 共用。"""
    try:
        os.makedirs(CONF["cache_dir"], exist_ok=True)
        with open(media_ref_path(guid), "w", encoding="utf-8") as f:
            f.write(_path_stem(media_path))
    except Exception as e:
        logger.warning("Failed to remember media path for %s: %s", guid, e)


def recalled_media_stem(guid: str) -> str | None:
    ref = media_ref_path(guid)
    if not os.path.exists(ref):
        return None
    try:
        with open(ref, encoding="utf-8") as f:
            stem = _path_stem(f.read().strip())
        if stem:
            return stem
    except Exception:
        return None
    return None


def recalled_media_path(guid: str) -> str | None:
    stem = recalled_media_stem(guid)
    if not stem:
        return None
    for ext in CACHE_EXTS:
        path = f"{stem}.{ext}"
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    return None


def unique_library_path(directory: str, basename: str, ext: str) -> str:
    dest = os.path.join(directory, f"{basename}.{ext}")
    if not os.path.exists(dest):
        return dest
    n = 2
    while os.path.exists(os.path.join(directory, f"{basename} ({n}).{ext}")):
        n += 1
    return os.path.join(directory, f"{basename} ({n}).{ext}")


def write_audio_tags(path: str, title: str, artist: str = "", album: str = "") -> None:
    """写入 title/artist/album，飞牛扫描后用标签而不是文件名显示。"""
    title, artist, album = (title or "").strip(), (artist or "").strip(), (album or "").strip()
    if not title and not artist:
        return
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
        if audio is None:
            return
        if getattr(audio, "tags", None) is None:
            try:
                audio.add_tags()
            except Exception:
                pass
        if title:
            audio["title"] = title
        if artist:
            audio["artist"] = artist
        if album:
            audio["album"] = album
        audio.save()
    except Exception as e:
        logger.warning("Failed to write audio tags for %s: %s", path, e)


def detect_library_dir() -> str:
    """优先环境变量，否则读飞牛 music.db 的共享库路径，最后回退到仓库 cache/。"""
    explicit = str(CONF.get("library_dir") or "").strip()
    if explicit:
        return explicit
    db = str(CONF.get("music_db") or "")
    if db and os.path.exists(db):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rows = con.execute("SELECT path FROM shared_library ORDER BY id").fetchall()
            finally:
                con.close()
            for (path,) in rows:
                if path and os.path.isdir(path):
                    return path
        except Exception as e:
            logger.warning("Failed to read shared_library path: %s", e)
    return CONF["cache_dir"]


def iter_media_dirs() -> list[str]:
    dirs: list[str] = []
    lib = detect_library_dir()
    for d in (lib, CONF["cache_dir"]):
        if d and d not in dirs:
            dirs.append(d)
    return dirs


def adopt_library_perms(path: str) -> None:
    try:
        parent = os.path.dirname(path) or "."
        st = os.stat(parent)
        os.chown(path, st.st_uid, st.st_gid)
        os.chmod(path, 0o644)
    except Exception:
        pass


def find_cache_file(guid: str) -> str | None:
    recalled = recalled_media_path(guid)
    if recalled:
        return recalled
    file_id = online_file_id(guid)
    safe = cache_safe_guid(guid)
    for d in iter_media_dirs():
        if not os.path.isdir(d):
            continue
        for ext in CACHE_EXTS:
            exact = os.path.join(d, f"{safe}.{ext}")
            if os.path.exists(exact) and os.path.getsize(exact) > 0:
                return exact
            pattern = os.path.join(d, f"* - {glob.escape(file_id)}.{ext}")
            for path in glob.glob(pattern):
                if os.path.getsize(path) > 0:
                    return path
    return None


def promote_cache_hit(guid: str, audio_path: str) -> str:
    """旧 cache/ 音频：若曲库已有对应文件或歌词，则对齐过去。"""
    recalled = recalled_media_path(guid)
    if recalled:
        return recalled
    lib = detect_library_dir()
    try:
        if os.path.abspath(os.path.dirname(audio_path)) == os.path.abspath(lib):
            remember_media_path(guid, audio_path)
            return audio_path
    except Exception:
        return audio_path
    file_id = online_file_id(guid)
    ext = os.path.splitext(audio_path)[1] or ".mp3"
    dest = None
    if os.path.isdir(lib):
        for lrc in glob.glob(os.path.join(lib, f"* - {glob.escape(file_id)}.lrc")):
            dest = os.path.splitext(lrc)[0] + ext
            break
    if not dest:
        return audio_path
    if not os.path.exists(dest):
        try:
            os.makedirs(lib, exist_ok=True)
            shutil.copy2(audio_path, dest)
            adopt_library_perms(dest)
        except Exception as e:
            logger.warning("Failed to promote cache audio into library: %s", e)
            return audio_path
    remember_media_path(guid, dest)
    return dest


def library_media_path(guid: str, title: str, ext: str, artist: str = "") -> str:
    recalled = recalled_media_path(guid)
    if recalled:
        return recalled
    stem = recalled_media_stem(guid)
    if stem:
        return f"{stem}.{ext}"
    lib = detect_library_dir()
    file_id = online_file_id(guid)
    if os.path.isdir(lib):
        for path in glob.glob(os.path.join(lib, f"* - {glob.escape(file_id)}.{ext}")):
            if os.path.getsize(path) > 0:
                return path
    os.makedirs(lib, exist_ok=True)
    return unique_library_path(lib, library_basename(title, artist), ext)


def find_lyric_file(guid: str) -> str | None:
    stem = recalled_media_stem(guid)
    if stem:
        sibling = f"{stem}.lrc"
        if os.path.exists(sibling) and os.path.getsize(sibling) > 0:
            return sibling
    audio = find_cache_file(guid)
    if audio:
        sibling = os.path.splitext(audio)[0] + ".lrc"
        if os.path.exists(sibling) and os.path.getsize(sibling) > 0:
            return sibling
    file_id = online_file_id(guid)
    safe = cache_safe_guid(guid)
    for d in iter_media_dirs():
        if not os.path.isdir(d):
            continue
        exact = os.path.join(d, f"{safe}.lrc")
        if os.path.exists(exact) and os.path.getsize(exact) > 0:
            return exact
        pattern = os.path.join(d, f"* - {glob.escape(file_id)}.lrc")
        for path in glob.glob(pattern):
            if os.path.getsize(path) > 0:
                return path
    return None


def lyric_cache_path(guid: str, title: str = "", artist: str = "") -> str:
    found = find_lyric_file(guid)
    if found:
        return found
    audio = find_cache_file(guid)
    if audio:
        return os.path.splitext(audio)[0] + ".lrc"
    d = detect_library_dir()
    os.makedirs(d, exist_ok=True)
    if (title or "").strip() or (artist or "").strip():
        return os.path.join(d, f"{library_basename(title, artist)}.lrc")
    return os.path.join(d, f"{cache_safe_guid(guid)}.lrc")


def read_lyric_cache(guid: str) -> str:
    path = find_lyric_file(guid)
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        logger.warning("Failed to read lyric cache %s: %s", path, e)
        return ""


def write_lyric_cache(guid: str, text: str, title: str = "", artist: str = "") -> None:
    text = (text or "").strip()
    if not text:
        return
    if text == read_lyric_cache(guid):
        return
    path = lyric_cache_path(guid, title=title, artist=artist)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    part_path = f"{path}.{uuid4().hex[:8]}.part"
    try:
        with open(part_path, "w", encoding="utf-8") as f:
            f.write(text)
            f.write("\n")
        os.replace(part_path, path)
        adopt_library_perms(path)
        remember_media_path(guid, path)
    except Exception as e:
        logger.warning("Failed to write lyric cache %s: %s", path, e)
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except Exception:
                pass


async def cache_lyrics_from_musicdl(musicdl_client: httpx.AsyncClient, guid: str) -> dict | None:
    """与音频 tee 并行：把 musicdl /info 里的 LRC 落到曲库同目录 sidecar。"""
    song_id = song_id_from_online_guid(guid)
    try:
        r = await musicdl_client.get("/info", params={"id": song_id}, timeout=10.0)
        if r.status_code != 200:
            return None
        data = r.json()
        if not (isinstance(data, dict) and data.get("ok") is not False):
            return None
        write_lyric_cache(
            guid,
            str(data.get("lyric") or ""),
            title=str(data.get("title") or ""),
            artist=str(data.get("artist") or ""),
        )
        return data
    except Exception as e:
        logger.warning("lyric sidecar fetch failed for %s: %s", guid, e)
        return None


async def resolve_online_lyric(request: Request, guid: str) -> str:
    """本地 .lrc 优先；没有再向源站要，拿到就落盘。"""
    cached = read_lyric_cache(guid)
    if cached:
        return cached

    src = source_from_online_guid(guid)
    if src == "netease":
        musicbox_client = get_musicbox_client(request.app)
        raw_song_id = song_id_from_online_guid(guid)
        song_id = raw_song_id.split(":")[-1]
        try:
            r = await musicbox_client.get(f"/api/v1/song/{song_id}/lyric", timeout=10.0)
            if r.status_code == 200:
                res_data = r.json()
                if isinstance(res_data, dict) and res_data.get("ok") is not False:
                    l_data = res_data.get("data")
                    if isinstance(l_data, dict):
                        lyric_text = str(l_data.get("lyric") or "").strip()
                        if lyric_text:
                            write_lyric_cache(guid, lyric_text)
                            return lyric_text
        except Exception as e:
            logger.warning("musicbox lyric fetch failed for %s: %s", guid, e)
        return ""

    data = await _online_info(request, guid)
    text = str((data or {}).get("lyric") or "").strip()
    if text:
        write_lyric_cache(
            guid,
            text,
            title=str((data or {}).get("title") or ""),
            artist=str((data or {}).get("artist") or ""),
        )
    return text


def media_type_for_ext(ext: str) -> str:
    return {
        "mp3": "audio/mpeg",
        "flac": "audio/flac",
        "wav": "audio/wav",
        "ogg": "audio/ogg",
        "opus": "audio/ogg",
        "m4a": "audio/mp4",
        "aac": "audio/aac",
        "ape": "audio/x-ape",
        "wv": "audio/x-wavpack",
        "dsf": "audio/x-dsd",
        "dff": "audio/x-dff",
        "tta": "audio/x-tta",
        "wma": "audio/x-ms-wma",
        "aiff": "audio/aiff",
    }.get(ext.lower(), "application/octet-stream")


def ext_from_content_type(content_type: str) -> str:
    ct = (content_type or "").lower()
    if "flac" in ct:
        return "flac"
    if "wavpack" in ct or "x-wv" in ct:
        return "wv"
    if "wav" in ct or "wave" in ct:
        return "wav"
    if "opus" in ct:
        return "opus"
    if "ogg" in ct:
        return "ogg"
    if "ape" in ct:
        return "ape"
    if "aiff" in ct:
        return "aiff"
    if "mp4" in ct or "m4a" in ct:
        return "m4a"
    if "aac" in ct:
        return "aac"
    if "mpeg" in ct or "mp3" in ct:
        return "mp3"
    return play_format_from_ext(ct.split("/")[-1] if "/" in ct else "mp3")


def parse_http_range(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    if not range_header:
        return None
    m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip(), re.I)
    if not m:
        return None
    start_s, end_s = m.group(1), m.group(2)
    if start_s == "" and end_s == "":
        return None
    if start_s == "":
        suffix = int(end_s)
        start = max(file_size - suffix, 0)
        end = file_size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else file_size - 1
    end = min(end, file_size - 1)
    if start < 0 or start >= file_size or start > end:
        return None
    return start, end


def serve_file_with_range(path: str, range_header: str | None, media_type: str) -> Response:
    file_size = os.path.getsize(path)
    rng = parse_http_range(range_header, file_size)

    def iter_file(offset: int, length: int) -> AsyncGenerator[bytes, None]:
        async def gen() -> AsyncGenerator[bytes, None]:
            remaining = length
            with open(path, "rb") as fp:
                fp.seek(offset)
                while remaining > 0:
                    chunk = fp.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return gen()

    if rng is None:
        return StreamingResponse(
            iter_file(0, file_size),
            status_code=200,
            headers={
                "Content-Type": media_type,
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
            },
        )

    start, end = rng
    length = end - start + 1
    return StreamingResponse(
        iter_file(start, length),
        status_code=206,
        headers={
            "Content-Type": media_type,
            "Content-Length": str(length),
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
        },
    )


def get_upstream_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "upstream_client", None)
    if client is None:
        transport = httpx.AsyncHTTPTransport(uds=CONF["upstream_sock"])
        client = httpx.AsyncClient(transport=transport, base_url="http://unix", timeout=30.0)
        fastapi_app.state.upstream_client = client
    return client


def get_musicdl_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "musicdl_client", None)
    if client is None:
        client = httpx.AsyncClient(base_url=CONF["musicdl_url"], timeout=45.0)
        fastapi_app.state.musicdl_client = client
    return client


def get_musicbox_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "musicbox_client", None)
    if client is None:
        client = httpx.AsyncClient(base_url=CONF["musicbox_url"], timeout=20.0)
        fastapi_app.state.musicbox_client = client
    return client


def get_llm_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "llm_client", None)
    if client is None:
        client = httpx.AsyncClient(timeout=dailyrec.LLM_TIMEOUT_S)
        fastapi_app.state.llm_client = client
    return client


async def forward_to_upstream(request: Request, client: httpx.AsyncClient) -> Response:
    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"

    headers = copy_incoming_headers(request)
    body = await request.body()

    req = client.build_request(
        method=request.method,
        url=url_path,
        headers=headers,
        content=body if body else None,
    )
    resp = await client.send(req, stream=True)
    resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})

    async def body_stream() -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        body_stream(),
        status_code=resp.status_code,
        headers=resp_headers,
    )


async def fetch_upstream_envelope(request: Request, client: httpx.AsyncClient) -> Response | dict:
    """透传上游并解析 JSON 信封。失败时返回 Response，成功返回 dict。"""
    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)
    body = await request.body()
    req = client.build_request(
        method=request.method,
        url=url_path,
        headers=headers,
        content=body if body else None,
    )
    resp = await client.send(req)
    resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})
    if resp.status_code != 200:
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    try:
        payload = resp.json()
    except Exception:
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    if not isinstance(payload, dict):
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    payload["_ext_headers"] = resp_headers
    return payload


async def fetch_musicdl_search(client: httpx.AsyncClient, keyword: str, limit: int) -> dict | None:
    if not keyword:
        return None
    params: dict[str, Any] = {"keyword": keyword, "limit": limit}
    if CONF["online_sources"]:
        params["sources"] = CONF["online_sources"]
    timeout = max(float(CONF.get("search_timeout") or 25), 8.0)
    try:
        r = await client.get("/search", params=params, timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                if data.get("errors"):
                    logger.warning("musicdl search partial errors: %s", data.get("errors"))
                return data
    except Exception as e:
        logger.warning("Failed to fetch online search from musicdl: %s", e)
    return None


async def fetch_musicbox_search(client: httpx.AsyncClient, keyword: str, limit: int) -> list[dict] | None:
    if not keyword:
        return None
    try:
        r = await client.get(
            "/api/v1/search",
            params={"keyword": keyword, "limit": limit, "type": "song"},
            timeout=20.0,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict) or data.get("ok") is False:
            return None
        raw_list = data.get("data")
        if not isinstance(raw_list, list):
            return None
        items = []
        song_ids = []
        for it in raw_list:
            if not isinstance(it, dict):
                continue
            sid = str(it.get("song_id") or it.get("id") or "")
            if not sid:
                continue
            title = str(it.get("song_name") or it.get("title") or it.get("name") or "")
            artist = str(it.get("artist") or "")
            album = str(it.get("album_name") or it.get("album") or "")
            duration = it.get("duration") or 0
            try:
                duration_s = float(duration)
            except (TypeError, ValueError):
                duration_s = 0.0
            quality = str(it.get("quality") or "").upper()
            ext = "flac" if any(q in quality for q in ("SQ", "HR", "无损")) else "mp3"
            items.append({
                "id": f"netease:{sid}",
                "source": "netease",
                "title": title,
                "artist": artist,
                "album": album,
                "duration_s": duration_s,
                "ext": ext,
                "cover_url": "",
                "lyric": "",
            })
            song_ids.append(sid)

        if song_ids:
            try:
                detail_resp = await client.get(
                    "/api/v1/songs/detail",
                    params={"ids": ",".join(song_ids)},
                    timeout=15.0,
                )
                if detail_resp.status_code == 200:
                    detail_json = detail_resp.json()
                    if isinstance(detail_json, dict) and detail_json.get("ok") is not False:
                        detail_list = detail_json.get("data")
                        if isinstance(detail_list, list):
                            detail_map = {}
                            for d_item in detail_list:
                                if isinstance(d_item, dict):
                                    d_sid = str(d_item.get("song_id") or d_item.get("id") or "")
                                    if d_sid:
                                        detail_map[d_sid] = d_item
                            for item in items:
                                raw_sid = item["id"].split(":", 1)[-1]
                                d_info = detail_map.get(raw_sid)
                                if d_info:
                                    pic_url = str(d_info.get("album_pic_url") or "")
                                    if pic_url:
                                        item["cover_url"] = pic_url
                                    if d_info.get("has_sq") or d_info.get("has_hr"):
                                        item["ext"] = "flac"
            except Exception as detail_err:
                logger.warning("Failed to fetch songs detail for %s: %s", keyword, detail_err)

        return items
    except Exception as e:
        logger.warning("Failed to fetch musicbox search: %s", e)
        return None


async def resolve_netease_url(client: httpx.AsyncClient, song_id: str) -> str | None:
    qualities = []
    primary = str(CONF.get("netease_quality") or "lossless").strip()
    if primary:
        qualities.append(primary)
    if "exhigh" not in qualities:
        qualities.append("exhigh")

    for q in qualities:
        try:
            r = await client.get(f"/api/v1/song/{song_id}/url", params={"quality": q}, timeout=10.0)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("ok") is not False:
                    inner = data.get("data")
                    if isinstance(inner, dict):
                        code = inner.get("code")
                        url = inner.get("url")
                        if code == 200 and url:
                            return str(url)
        except Exception as e:
            logger.warning("resolve_netease_url error for %s (quality=%s): %s", song_id, q, e)
    return None


def ensure_search_list(upstream_json: dict) -> list:
    """保证 data.list 存在，本地 0 条时仍能追加在线条目。"""
    data = upstream_json.get("data")
    if not isinstance(data, dict):
        data = {}
        upstream_json["data"] = data
    target = get_by_path(upstream_json, CONF["search_list_path"])
    if isinstance(target, list):
        return target
    for key in ("list", "items", "tracks", "records"):
        if isinstance(data.get(key), list):
            if key != "list":
                data["list"] = data[key]
            return data["list"]
    data["list"] = []
    if "total" not in data:
        data["total"] = 0
    return data["list"]


def merge_online_tracks(
    upstream_json: dict,
    online_data: list[dict] | dict | None,
    page: int = 1,
    size: int = 50,
) -> dict:
    target_list = ensure_search_list(upstream_json)
    if not online_data:
        return upstream_json

    if isinstance(online_data, dict):
        raw_items = online_data.get("items", [])
    elif isinstance(online_data, list):
        raw_items = online_data
    else:
        raw_items = []

    if not raw_items:
        return upstream_json

    existing_keys = set()
    for item in target_list:
        t = title_from_track(item)
        a = artist_from_track(item)
        if t and a:
            existing_keys.add((t, a))

    filtered_online = []
    for online_item in raw_items:
        ot = str(online_item.get("title") or online_item.get("name") or "").strip().lower()
        oa = str(online_item.get("artist") or "").strip().lower()
        if ot and oa and (ot, oa) in existing_keys:
            continue
        filtered_online.append(online_item)

    online_limit = CONF["online_limit"]
    if page == 1:
        page_online = filtered_online[:online_limit]
    else:
        page_online = filtered_online[online_limit + (page - 2) * size : online_limit + (page - 1) * size]

    for it in page_online:
        target_list.append(build_online_track(it))

    parts = CONF["search_list_path"].split(".")
    parent = upstream_json
    for p in parts[:-1]:
        if isinstance(parent, dict) and p in parent:
            parent = parent[p]
    if isinstance(parent, dict):
        orig_total = parent.get("total")
        if not isinstance(orig_total, int):
            orig_total = len(target_list) - len(page_online)
        parent["total"] = orig_total + len(filtered_online)

    return upstream_json


def extract_guid(request: Request, path_guid: str | None = None) -> str:
    if path_guid:
        return path_guid
    return (
        request.query_params.get("guid")
        or request.query_params.get("trackGUID")
        or request.query_params.get("trackGuid")
        or request.query_params.get("coverId")
        or request.query_params.get("id")
        or request.query_params.get("trackId")
        or ""
    )


async def extract_guid_from_body(request: Request) -> str:
    guid = extract_guid(request)
    if guid:
        return guid
    try:
        body = await request.json()
    except Exception:
        return ""
    if isinstance(body, dict):
        return str(
            body.get("guid")
            or body.get("trackGUID")
            or body.get("trackGuid")
            or body.get("id")
            or body.get("trackId")
            or ""
        )
    return ""


def empty_ok() -> JSONResponse:
    return JSONResponse(content={"code": 0, "msg": "ok", "data": {}})


def build_lyric_list_payload(guid: str, lyric_text: str) -> dict:
    """对齐飞牛 $n.lyric.list → xr(list, preferred)。

    每条需有非空 content；source=2 表示 EXTERNAL_LRC（非内嵌，不强制 offset）。
    """
    text = (lyric_text or "").strip()
    if not text:
        return {"code": 0, "msg": "ok", "data": {"list": [], "preferred": ""}}
    lyric_guid = f"{guid}:lyric"
    now = int(time.time())
    item = {
        "guid": lyric_guid,
        "content": text,
        "source": 2,
        "isLRC": True,
        "offset": 0,
        "createdAt": now,
        "updatedAt": now,
    }
    return {
        "code": 0,
        "msg": "ok",
        "data": {"list": [item], "preferred": lyric_guid},
    }


def stub_online_info(guid: str) -> dict:
    song_id = song_id_from_online_guid(guid)
    return {
        "id": song_id,
        "source": source_from_online_guid(guid),
        "title": "",
        "artist": "",
        "album": "",
        "duration_s": 0,
        "ext": "mp3",
        "file_size": 0,
        "cover_url": "",
        "lyric": "",
    }


def build_metadata_payload(guid: str, data: dict | None) -> dict:
    """飞牛 resolveTrackPlayback._h() 会无防护读取 data.track.genres.join / album / artists。

    缺 genres 或 album 不是对象时直接抛错，播放器跳过且不会请求 stream。
    """
    info = dict(data or {})
    info.setdefault("id", song_id_from_online_guid(guid))
    info.setdefault("source", source_from_online_guid(guid))
    vo = build_online_track(info)
    album_obj = vo["album"] if isinstance(vo.get("album"), dict) else {
        "name": str(vo.get("album") or ""),
        "guid": f"{guid}:album",
        "artists": vo.get("artists") or [],
        "coverId": guid,
    }
    track = {
        "guid": guid,
        "id": guid,
        "title": vo.get("title") or "",
        "artists": vo.get("artists") or [],
        "album": album_obj,
        "genres": list(vo.get("genres") or []),
        "duration": vo.get("duration") or 0,
        "coverId": guid,
        "coverUrl": vo.get("coverUrl") or "",
        "format": vo.get("format") or "mp3",
        "hasLyric": bool(vo.get("hasLyric") or info.get("lyric")),
        "isFavorite": False,
        "isCue": False,
        "accessStatus": 0,
        "audioSpec": vo["audioSpec"],
    }
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            **vo,
            "guid": guid,
            "id": guid,
            "album": album_obj,
            "audioSpec": vo["audioSpec"],
            "track": track,
        },
    }


def _conf_log_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(part in lowered for part in _REDACT_KEY_PARTS):
        return "***" if value else ""
    return value


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    logger.info("=== fnmusic-ext v%s configuration ===", get_version())
    for k, v in CONF.items():
        logger.info("  %s = %s", k, _conf_log_value(k, v))
    logger.info("  llm_enabled = %s", dailyrec.llm_enabled())
    logger.info("==================================")

    created_upstream = False
    created_musicdl = False
    created_musicbox = False
    created_llm = False

    if getattr(fastapi_app.state, "upstream_client", None) is None:
        fastapi_app.state.upstream_client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=CONF["upstream_sock"]),
            base_url="http://unix",
            timeout=30.0,
        )
        created_upstream = True

    if getattr(fastapi_app.state, "musicdl_client", None) is None:
        fastapi_app.state.musicdl_client = httpx.AsyncClient(
            base_url=CONF["musicdl_url"],
            timeout=45.0,
        )
        created_musicdl = True

    if getattr(fastapi_app.state, "musicbox_client", None) is None:
        fastapi_app.state.musicbox_client = httpx.AsyncClient(
            base_url=CONF["musicbox_url"],
            timeout=20.0,
        )
        created_musicbox = True

    if getattr(fastapi_app.state, "llm_client", None) is None:
        fastapi_app.state.llm_client = httpx.AsyncClient(timeout=dailyrec.LLM_TIMEOUT_S)
        created_llm = True

    try:
        yield
    finally:
        if created_upstream and getattr(fastapi_app.state, "upstream_client", None):
            await fastapi_app.state.upstream_client.aclose()
            fastapi_app.state.upstream_client = None
        if created_musicdl and getattr(fastapi_app.state, "musicdl_client", None):
            await fastapi_app.state.musicdl_client.aclose()
            fastapi_app.state.musicdl_client = None
        if created_musicbox and getattr(fastapi_app.state, "musicbox_client", None):
            await fastapi_app.state.musicbox_client.aclose()
            fastapi_app.state.musicbox_client = None
        if created_llm and getattr(fastapi_app.state, "llm_client", None):
            await fastapi_app.state.llm_client.aclose()
            fastapi_app.state.llm_client = None


app = FastAPI(title="fnmusic-ext", lifespan=lifespan)


@app.get("/_ext/healthz")
async def ext_healthz(request: Request):
    upstream_client = get_upstream_client(request.app)
    musicdl_client = get_musicdl_client(request.app)
    musicbox_client = get_musicbox_client(request.app)

    upstream_status = "fail"
    musicdl_status = "fail"
    musicbox_status = "fail"

    try:
        r = await upstream_client.get("/music/api/v1/search/track?keyword=healthz_probe", timeout=2.0)
        if r.status_code < 500:
            upstream_status = "ok"
    except Exception as e:
        logger.debug("Upstream health check failed: %s", e)

    if not CONF.get("musicdl_enabled", True):
        musicdl_status = "disabled"
    else:
        try:
            r = await musicdl_client.get("/healthz", timeout=2.0)
            if r.status_code == 200:
                musicdl_status = "ok"
        except Exception as e:
            logger.debug("Musicdl health check failed: %s", e)

    if not CONF["netease_enabled"]:
        musicbox_status = "disabled"
    else:
        try:
            r = await musicbox_client.get("/healthz", timeout=2.0)
            if r.status_code == 200:
                musicbox_status = "ok"
        except Exception as e:
            logger.debug("Musicbox health check failed: %s", e)

    llm_status = "enabled" if dailyrec.llm_enabled() else "disabled"
    source_ok = musicdl_status == "ok" or musicbox_status == "ok"

    return {
        "ok": upstream_status == "ok" and source_ok,
        "version": get_version(),
        "upstream": upstream_status,
        "musicdl": musicdl_status,
        "musicbox": musicbox_status,
        "llm": llm_status,
    }


@app.get("/music/api/v1/search/track")
@app.get("/music/api/v1/search/track/{subpath:path}")
async def search_track(request: Request):
    upstream_client = get_upstream_client(request.app)
    musicdl_client = get_musicdl_client(request.app)
    musicbox_client = get_musicbox_client(request.app)
    keyword = extract_keyword(request)

    page_str = request.query_params.get("page")
    try:
        page = int(page_str) if page_str else 1
    except (TypeError, ValueError):
        page = 1
    if page < 1:
        page = 1

    size_str = request.query_params.get("size")
    try:
        size = int(size_str) if size_str else 50
    except (TypeError, ValueError):
        size = 50
    if size < 1:
        size = 50

    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)

    req = upstream_client.build_request("GET", url_path, headers=headers)
    upstream_resp = await upstream_client.send(req)

    resp_headers = filter_headers(upstream_resp.headers, exclude_keys={"content-length", "content-encoding"})

    if upstream_resp.status_code != 200:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    try:
        upstream_json = upstream_resp.json()
    except Exception:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    if not isinstance(upstream_json, dict) or upstream_json.get("code") != 0:
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    if not keyword:
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    now = time.time()
    cached_entry = _SEARCH_CACHE.get(keyword)
    is_valid_cache = cached_entry is not None and (now - cached_entry.get("ts", 0) < CONF["search_cache_ttl"])

    if is_valid_cache and cached_entry is not None:
        task = cached_entry.get("task")
        if task and not task.done() and page >= 2:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=CONF["late_page_wait_s"])
            except Exception:
                pass
        online_all = cached_entry.get("items", [])
    else:
        entry: dict[str, Any] = {"items": [], "ts": time.time(), "task": None}
        mb_task: asyncio.Task | None = None
        mdl_task: asyncio.Task | None = None
        if CONF["netease_enabled"]:
            mb_task = asyncio.create_task(
                fetch_musicbox_search(musicbox_client, keyword, CONF["netease_search_limit"])
            )
        if CONF.get("musicdl_enabled", True):
            mdl_task = asyncio.create_task(
                fetch_musicdl_search(musicdl_client, keyword, CONF["online_limit"])
            )

        async def _bg_aggregator(e: dict, t_mb: asyncio.Task | None, t_mdl: asyncio.Task | None):
            mb_res = None
            mdl_res = None
            if t_mb:
                try:
                    mb_res = await t_mb
                except Exception as exc:
                    logger.warning("musicbox search bg failed: %s", exc)
            if t_mdl:
                try:
                    mdl_res = await t_mdl
                except Exception as exc:
                    logger.warning("musicdl search bg failed: %s", exc)
            mb_list = mb_res if isinstance(mb_res, list) else []
            mdl_list = (
                mdl_res.get("items", [])
                if isinstance(mdl_res, dict) and isinstance(mdl_res.get("items"), list)
                else []
            )
            e["items"] = deduplicate_online_items(mb_list + mdl_list)

        agg_task = asyncio.create_task(_bg_aggregator(entry, mb_task, mdl_task))
        entry["task"] = agg_task
        _set_search_cache(keyword, entry)

        if page == 1:
            if mb_task:
                try:
                    mb_res = await asyncio.wait_for(asyncio.shield(mb_task), timeout=CONF["netease_wait_s"])
                    if isinstance(mb_res, list) and not agg_task.done():
                        entry["items"] = deduplicate_online_items(mb_res)
                except Exception:
                    pass
            elif mdl_task:
                try:
                    mdl_res = await asyncio.wait_for(
                        asyncio.shield(mdl_task), timeout=min(float(CONF["search_timeout"]), 4.0)
                    )
                    if isinstance(mdl_res, dict) and not agg_task.done():
                        entry["items"] = deduplicate_online_items(mdl_res.get("items") or [])
                except Exception:
                    pass
        else:
            try:
                await asyncio.wait_for(asyncio.shield(agg_task), timeout=CONF["late_page_wait_s"])
            except Exception:
                pass

        online_all = entry.get("items", [])

    merged = merge_online_tracks(upstream_json, online_all, page=page, size=size)
    return JSONResponse(content=merged, status_code=upstream_resp.status_code, headers=resp_headers)


@app.get("/music/api/v1/search/suggest")
@app.get("/music/api/v1/search/suggest/{subpath:path}")
async def search_suggest(request: Request):
    if not CONF["merge_suggest"]:
        return await forward_to_upstream(request, get_upstream_client(request.app))

    upstream_client = get_upstream_client(request.app)
    musicdl_client = get_musicdl_client(request.app)
    keyword = extract_keyword(request)

    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)

    musicdl_task: asyncio.Task | None = None
    if keyword:
        musicdl_task = asyncio.create_task(fetch_musicdl_search(musicdl_client, keyword, 5))

    req = upstream_client.build_request("GET", url_path, headers=headers)
    upstream_resp = await upstream_client.send(req)
    resp_headers = filter_headers(upstream_resp.headers, exclude_keys={"content-length", "content-encoding"})

    if upstream_resp.status_code != 200:
        if musicdl_task:
            musicdl_task.cancel()
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    try:
        upstream_json = upstream_resp.json()
    except Exception:
        if musicdl_task:
            musicdl_task.cancel()
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    if not isinstance(upstream_json, dict) or upstream_json.get("code") != 0:
        if musicdl_task:
            musicdl_task.cancel()
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    musicdl_data = None
    if musicdl_task:
        try:
            musicdl_data = await asyncio.wait_for(asyncio.shield(musicdl_task), timeout=10.0)
        except Exception as e:
            logger.warning("Suggest musicdl error: %s", e)
            musicdl_task.cancel()

    data_field = upstream_json.get("data")
    if isinstance(data_field, list) and musicdl_data and "items" in musicdl_data:
        for item in musicdl_data.get("items", [])[:5]:
            title = item.get("title")
            if title and title not in data_field:
                data_field.append(title)

    return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)


def stream_tee_response(
    resp: httpx.Response,
    guid: str,
    range_header: str | None,
    coro_factory: Callable[[], Coroutine[Any, Any, Any]] | None = None,
    client_to_close: httpx.AsyncClient | None = None,
    resolved_ext: str | None = None,
    pre_info: dict | None = None,
) -> Response:
    out_headers = {"Accept-Ranges": "bytes"}
    for k in ("content-type", "content-length", "content-range"):
        v = resp.headers.get(k)
        if v:
            out_headers[k] = v

    if resolved_ext:
        out_headers["content-type"] = media_type_for_ext(resolved_ext)

    status_code = resp.status_code
    content_length_str = resp.headers.get("content-length")
    content_length = (
        int(content_length_str) if content_length_str and content_length_str.isdigit() else None
    )

    ext = (resolved_ext or "").strip().lower() or ext_from_content_type(resp.headers.get("content-type") or "")
    store_dir = detect_library_dir()

    if should_cache(range_header):
        os.makedirs(store_dir, exist_ok=True)
        part_path = os.path.join(store_dir, f"{cache_safe_guid(guid)}.{uuid4().hex[:8]}.part")
        info_task: asyncio.Task | None = None
        if pre_info is None and coro_factory is not None:
            info_task = asyncio.create_task(coro_factory())

        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def _downloader():
            written = 0
            part_file = None
            try:
                part_file = open(part_path, "wb")
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        part_file.write(chunk)
                        written += len(chunk)
                        await queue.put(chunk)
            except Exception as e:
                logger.warning("tee download failed for %s: %s", guid, e)
            finally:
                if part_file:
                    try:
                        part_file.close()
                    except Exception:
                        pass
                await resp.aclose()
                if client_to_close:
                    await client_to_close.aclose()
                info: dict | None = pre_info
                if info is None and info_task:
                    try:
                        info = await asyncio.wait_for(asyncio.shield(info_task), timeout=8.0)
                    except Exception as e:
                        logger.warning("info/lyric wait failed for %s: %s", guid, e)
                title = str((info or {}).get("title") or "")
                artist = str((info or {}).get("artist") or "")
                album = str((info or {}).get("album") or "")
                complete = written >= 1024 and (content_length is None or written == content_length)
                if complete:
                    dest = library_media_path(guid, title, ext, artist=artist)
                    try:
                        os.replace(part_path, dest)
                        adopt_library_perms(dest)
                        remember_media_path(guid, dest)
                        write_audio_tags(dest, title=title, artist=artist, album=album)
                    except Exception as e:
                        logger.warning("Failed to rename cache file: %s", e)
                        if os.path.exists(part_path):
                            try:
                                os.remove(part_path)
                            except Exception:
                                pass
                elif os.path.exists(part_path):
                    try:
                        os.remove(part_path)
                    except Exception:
                        pass
                await queue.put(None)

        dl_task = asyncio.create_task(_downloader())

        async def stream_tee() -> AsyncGenerator[bytes, None]:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk

        return StreamingResponse(stream_tee(), status_code=status_code, headers=out_headers)

    async def stream_no_cache() -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in resp.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            await resp.aclose()
            if client_to_close:
                await client_to_close.aclose()

    return StreamingResponse(stream_no_cache(), status_code=status_code, headers=out_headers)


@app.get("/music/api/v1/track/stream")
@app.get("/music/api/v1/track/stream/{subpath:path}")
async def stream_track(request: Request):
    guid = extract_guid(request)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    range_header = request.headers.get("range")
    cached = find_cache_file(guid)
    if cached:
        cached = promote_cache_hit(guid, cached)
        ext = os.path.splitext(cached)[1].lstrip(".") or "mp3"
        return serve_file_with_range(cached, range_header, media_type_for_ext(ext))

    src = source_from_online_guid(guid)
    if src == "netease":
        musicbox_client = get_musicbox_client(request.app)
        raw_song_id = song_id_from_online_guid(guid)
        song_id = raw_song_id.split(":")[-1]

        play_url_res, info_res = await asyncio.gather(
            resolve_netease_url(musicbox_client, song_id),
            _online_info(request, guid),
            return_exceptions=True,
        )
        play_url = None if isinstance(play_url_res, Exception) else play_url_res
        info = None if isinstance(info_res, Exception) else info_res

        if not play_url:
            return JSONResponse(
                content={"code": 404, "msg": "online source unavailable", "data": None},
                status_code=404,
            )

        resolved_ext = str(info.get("ext")) if (isinstance(info, dict) and info.get("ext")) else None

        req_headers = {}
        if range_header:
            req_headers["Range"] = range_header

        stream_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        try:
            stream_req = stream_client.build_request("GET", play_url, headers=req_headers)
            resp = await stream_client.send(stream_req, stream=True)
            if resp.status_code >= 400:
                await resp.aclose()
                await stream_client.aclose()
                return JSONResponse(
                    content={"code": 404, "msg": "online source unavailable", "data": None},
                    status_code=404,
                )
        except Exception as e:
            logger.warning("Failed to stream direct url %s for %s: %s", play_url, guid, e)
            await stream_client.aclose()
            return JSONResponse(
                content={"code": 404, "msg": "online source unavailable", "data": None},
                status_code=404,
            )

        return stream_tee_response(
            resp,
            guid=guid,
            range_header=range_header,
            coro_factory=None if info is not None else (lambda: _online_info(request, guid)),
            client_to_close=stream_client,
            resolved_ext=resolved_ext,
            pre_info=info if isinstance(info, dict) else None,
        )

    musicdl_client = get_musicdl_client(request.app)
    song_id = song_id_from_online_guid(guid)

    req_headers = {}
    if range_header:
        req_headers["Range"] = range_header

    req = musicdl_client.build_request(
        "GET",
        "/stream",
        params={"id": song_id, "proxy": "true"},
        headers=req_headers,
    )
    resp = await musicdl_client.send(req, stream=True)

    if resp.status_code in (404, 502) or resp.status_code >= 400:
        await resp.aclose()
        return JSONResponse(
            content={"code": 404, "msg": "online source unavailable", "data": None},
            status_code=404,
        )

    return stream_tee_response(
        resp,
        guid=guid,
        range_header=range_header,
        coro_factory=lambda: cache_lyrics_from_musicdl(musicdl_client, guid),
        client_to_close=None,
    )


@app.get("/music/api/v1/track/hls/{guid}/preset.m3u8")
@app.get("/music/api/v1/track/hls/{guid}/{filename}")
async def track_hls(request: Request, guid: str, filename: str = "preset.m3u8"):
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    info = await _online_info(request, guid)
    duration_s = 0
    if info:
        try:
            duration_s = int(float(info.get("duration_s") or 0))
        except (TypeError, ValueError):
            duration_s = 0
    if duration_s <= 0:
        duration_s = 240

    stream_url = f"/music/api/v1/track/stream?guid={quote(guid, safe='')}"
    playlist = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        f"#EXT-X-TARGETDURATION:{max(duration_s, 1)}\n"
        "#EXT-X-PLAYLIST-TYPE:VOD\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n"
        f"#EXTINF:{duration_s:.3f},\n"
        f"{stream_url}\n"
        "#EXT-X-ENDLIST\n"
    )
    return Response(content=playlist, media_type="application/vnd.apple.mpegurl")


@app.api_route("/music/api/v1/track/transcode/heartbeat", methods=["GET", "POST"])
@app.api_route("/music/api/v1/track/transcode/quit", methods=["GET", "POST"])
async def track_transcode_session(request: Request):
    guid = await extract_guid_from_body(request)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))
    return JSONResponse(content={"code": 0, "msg": "ok", "data": {"guid": guid}})


@app.api_route("/music/api/v1/track/transcode", methods=["GET", "POST"])
async def track_transcode(request: Request):
    guid = await extract_guid_from_body(request)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))
    return JSONResponse(
        content={
            "code": 0,
            "msg": "ok",
            "status": "success",
            "data": {"guid": guid, "status": "ready"},
        }
    )


async def _online_info(request: Request, guid: str) -> dict | None:
    src = source_from_online_guid(guid)
    if src == "netease":
        musicbox_client = get_musicbox_client(request.app)
        raw_song_id = song_id_from_online_guid(guid)
        song_id = raw_song_id.split(":")[-1]
        try:
            r = await musicbox_client.get(f"/api/v1/song/{song_id}/info", timeout=10.0)
            if r.status_code == 200:
                res_data = r.json()
                if isinstance(res_data, dict) and res_data.get("ok") is not False:
                    data = res_data.get("data")
                    if isinstance(data, dict):
                        name = str(data.get("name") or "")
                        ar = data.get("ar") or []
                        ar_names = []
                        if isinstance(ar, list):
                            for x in ar:
                                if isinstance(x, dict) and x.get("name"):
                                    ar_names.append(str(x["name"]))
                                elif isinstance(x, str):
                                    ar_names.append(x)
                        artist = " / ".join(ar_names)
                        al = data.get("al") or {}
                        album_name = str(al.get("name") or "") if isinstance(al, dict) else ""
                        cover_url = str(al.get("picUrl") or "") if isinstance(al, dict) else ""
                        dt = data.get("dt") or 0
                        duration_s = float(dt) / 1000.0 if dt else 0.0
                        sq = data.get("sq")
                        hr = data.get("hr")
                        h = data.get("h") or {}
                        ext = "flac" if (sq or hr) else "mp3"
                        size_obj = sq or h or {}
                        file_size = int(size_obj.get("size", 0) or 0) if isinstance(size_obj, dict) else 0

                        lyric_text = ""
                        try:
                            lr = await musicbox_client.get(f"/api/v1/song/{song_id}/lyric", timeout=10.0)
                            if lr.status_code == 200:
                                l_res = lr.json()
                                if isinstance(l_res, dict) and l_res.get("ok") is not False:
                                    l_data = l_res.get("data")
                                    if isinstance(l_data, dict):
                                        lyric_text = str(l_data.get("lyric") or "").strip()
                        except Exception as l_err:
                            logger.warning("musicbox lyric fetch in _online_info failed for %s: %s", guid, l_err)

                        return {
                            "id": f"netease:{song_id}",
                            "source": "netease",
                            "title": name,
                            "artist": artist,
                            "album": album_name,
                            "cover_url": cover_url,
                            "duration_s": duration_s,
                            "ext": ext,
                            "file_size": file_size,
                            "lyric": lyric_text,
                        }
        except Exception as e:
            logger.warning("musicbox /info failed for %s: %s", guid, e)
        return None

    musicdl_client = get_musicdl_client(request.app)
    song_id = song_id_from_online_guid(guid)
    try:
        r = await musicdl_client.get("/info", params={"id": song_id}, timeout=10.0)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and data.get("ok") is not False:
                return data
    except Exception as e:
        logger.warning("musicdl /info failed for %s: %s", guid, e)
    return None


@app.get("/music/api/v1/lyric/list")
@app.get("/music/api/v1/lyric/list/{subpath:path}")
async def lyric_list(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    lyric_text = await resolve_online_lyric(request, guid)
    return JSONResponse(content=build_lyric_list_payload(guid, lyric_text))


@app.get("/music/api/v1/track/lyrics")
@app.get("/music/api/v1/track/lyrics/{subpath:path}")
@app.get("/music/api/v1/detail/lyrics/{subpath:path}")
async def track_lyrics(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    lyric_text = await resolve_online_lyric(request, guid)
    if lyric_text:
        res = {"code": 0, "msg": "ok", "data": {"guid": guid, "lyric": lyric_text}}
        set_by_path(res, CONF["lyric_field"], lyric_text)
        return JSONResponse(content=res)
    return empty_ok()


@app.get("/music/api/v1/track/metadata")
@app.get("/music/api/v1/track/metadata/{subpath:path}")
@app.get("/music/api/v1/track/audio-info")
async def track_metadata(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    data = await _online_info(request, guid) or stub_online_info(guid)
    cached_lyric = read_lyric_cache(guid)
    if cached_lyric:
        data = {**data, "lyric": cached_lyric}
    elif data.get("lyric"):
        write_lyric_cache(
            guid,
            str(data.get("lyric") or ""),
            title=str(data.get("title") or ""),
            artist=str(data.get("artist") or ""),
        )
    return JSONResponse(content=build_metadata_payload(guid, data))


@app.api_route("/music/api/v1/static/cover", methods=["GET", "HEAD"])
@app.api_route("/music/api/v1/static/cover/{subpath:path}", methods=["GET", "HEAD"])
async def static_cover(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not guid and subpath.startswith("online:"):
        guid = subpath
    if dailyrec.is_daily_playlist_guid(guid):
        upstream_client = get_upstream_client(request.app)
        is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
        if not is_authed and auth_resp is not None:
            return auth_resp
        cached = dailyrec.load_daily_cache(user_guid, dailyrec.today_key())
        tracks = (cached or {}).get("tracks") or []
        if tracks:
            first_guid = str(tracks[0].get("guid") or "")
            if is_online_guid(first_guid):
                guid = first_guid
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    data = await _online_info(request, guid)
    cover = (data or {}).get("cover_url") or ""
    if cover:
        return RedirectResponse(cover, status_code=302)
    return empty_ok()


# === online favorites ===

_FAV_LOCK = asyncio.Lock()


def sanitize_user_guid(guid: str | None) -> str:
    """过滤文件名合法字符 [A-Za-z0-9-_]，非法字符替换为 _；为空则返回 'shared'。"""
    raw = str(guid or "").strip()
    safe = re.sub(r"[^A-Za-z0-9\-_]", "_", raw)
    return safe or "shared"


def user_fav_path(user_guid: str) -> str:
    fav_dir = CONF.get("fav_dir") or os.path.join(_HOME, "online_favorites")
    safe_name = sanitize_user_guid(user_guid)
    return os.path.join(fav_dir, f"{safe_name}.json")


def load_online_favorites(user_guid: str) -> list[dict]:
    path = user_fav_path(user_guid)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data["items"]
            if isinstance(data, list):
                return data
    except Exception as e:
        logger.warning("Failed to load online favorites for %s from %s: %s", user_guid, path, e)
    return []


def save_online_favorites(user_guid: str, items: list[dict]) -> bool:
    path = user_fav_path(user_guid)
    parent = os.path.dirname(path) or "."
    part_path = f"{path}.{uuid4().hex[:8]}.part"
    try:
        os.makedirs(parent, exist_ok=True)
        with open(part_path, "w", encoding="utf-8") as f:
            json.dump({"items": items}, f, ensure_ascii=False, indent=2)
        os.replace(part_path, path)
        return True
    except Exception as e:
        logger.warning("Failed to save online favorites for %s to %s: %s", user_guid, path, e)
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except Exception:
                pass
        return False


def build_favorite_track_obj(guid: str, info: dict | None = None, created_at: int | None = None) -> dict:
    raw_info = dict(info or {})
    raw_info.setdefault("id", song_id_from_online_guid(guid))
    raw_info.setdefault("source", source_from_online_guid(guid))
    vo = build_online_track(raw_info)

    now = int(time.time())
    ts = created_at or now

    artist_name = vo.get("artist") or ""
    artists_list = [
        {
            "guid": f"{guid}:artist",
            "name": artist_name,
            "coverId": guid,
            "createdAt": ts,
            "updatedAt": ts,
        }
    ] if artist_name else []

    album_name = vo.get("albumName") or (vo.get("album", {}).get("name") if isinstance(vo.get("album"), dict) else "") or ""
    album_obj = {
        "guid": f"{guid}:album",
        "name": album_name,
        "artists": artists_list,
        "coverId": guid,
        "releaseDate": 0,
        "barcode": "",
        "createdAt": ts,
        "updatedAt": ts,
    }

    audio_spec = vo.get("audioSpec") or {}

    return {
        "guid": guid,
        "title": vo.get("title") or "",
        "duration": vo.get("duration") or 0,
        "isFavorite": True,
        "isCue": False,
        "genres": [],
        "artists": artists_list,
        "album": album_obj,
        "audioSpec": audio_spec,
        "accessStatus": 0,
        "coverId": guid,
        "year": 0,
        "discNo": 1,
        "trackNo": 1,
        "isrc": "",
        "createdAt": ts,
        "updatedAt": ts,
    }


async def _probe_upstream_auth(request: Request, client: httpx.AsyncClient) -> tuple[bool, str, Response | None]:
    """向上游探测用户是否已登录。复用当前请求 headers。
    返回 (is_authed, user_guid, error_response)。
    """
    headers = copy_incoming_headers(request)
    try:
        probe_req = client.build_request("GET", "/music/api/v1/user/me", headers=headers)
        probe_resp = await client.send(probe_req)
        resp_headers = filter_headers(probe_resp.headers, exclude_keys={"content-length", "content-encoding"})

        if probe_resp.status_code == 401:
            return False, "", Response(
                content=probe_resp.content,
                status_code=401,
                headers=resp_headers,
                media_type=probe_resp.headers.get("content-type"),
            )

        if probe_resp.status_code == 200:
            try:
                probe_json = probe_resp.json()
                if isinstance(probe_json, dict) and probe_json.get("code") == 99999:
                    return False, "", JSONResponse(
                        content=probe_json,
                        status_code=200,
                        headers=resp_headers,
                    )
                if isinstance(probe_json, dict) and probe_json.get("code") == 0:
                    data = probe_json.get("data")
                    if isinstance(data, dict) and data.get("guid"):
                        return True, str(data["guid"]), None
                    logger.warning("user/me response missing data.guid, falling back to 'shared': %s", probe_json)
                    return True, "shared", None
            except Exception as e:
                logger.warning("Failed to parse user/me json response: %s", e)
                return True, "shared", None
            return True, "shared", None

        # 其他非 200/401 状态码，上游异常
        return True, "shared", None
    except Exception as e:
        logger.warning("Upstream auth probe failed: %s", e)
        # 探测异常时保守放行
        return True, "shared", None


@app.post("/music/api/v1/favorite-track/create")
async def favorite_track_create(request: Request):
    upstream_client = get_upstream_client(request.app)
    try:
        body = await request.json()
    except Exception:
        body = {}

    guid = ""
    if isinstance(body, dict):
        guid = str(body.get("trackGUID") or body.get("guid") or "").strip()

    if not is_online_guid(guid):
        return await forward_to_upstream(request, upstream_client)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    now = int(time.time())
    info = await _online_info(request, guid)
    if not info:
        cached_lyric = read_lyric_cache(guid)
        title = ""
        artist = ""
        cached_media = find_cache_file(guid)
        if cached_media:
            base = os.path.splitext(os.path.basename(cached_media))[0]
            if " - " in base:
                artist, title = base.split(" - ", 1)
            else:
                title = base
        info = {
            "id": song_id_from_online_guid(guid),
            "source": source_from_online_guid(guid),
            "title": title,
            "artist": artist,
            "lyric": cached_lyric,
        }

    track_obj = build_favorite_track_obj(guid, info, created_at=now)

    async with _FAV_LOCK:
        try:
            items = load_online_favorites(user_guid)
            # 查重
            idx = next((i for i, it in enumerate(items) if it.get("guid") == guid), None)
            if idx is not None:
                # 幂等更新
                items[idx]["track"] = track_obj
            else:
                items.append({
                    "guid": guid,
                    "createdAt": now,
                    "track": track_obj,
                })
            save_online_favorites(user_guid, items)
        except Exception as e:
            logger.warning("Error updating online favorites for user %s: %s", user_guid, e)

    return JSONResponse(content={"code": 0, "msg": "", "data": None})


@app.post("/music/api/v1/favorite-track/delete")
async def favorite_track_delete(request: Request):
    upstream_client = get_upstream_client(request.app)
    try:
        body = await request.json()
    except Exception:
        body = {}

    guid = ""
    if isinstance(body, dict):
        guid = str(body.get("trackGUID") or body.get("guid") or "").strip()

    if not is_online_guid(guid):
        return await forward_to_upstream(request, upstream_client)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    async with _FAV_LOCK:
        try:
            items = load_online_favorites(user_guid)
            items = [it for it in items if it.get("guid") != guid]
            save_online_favorites(user_guid, items)
        except Exception as e:
            logger.warning("Error deleting from online favorites for user %s: %s", user_guid, e)

    return JSONResponse(content={"code": 0, "msg": "", "data": None})


@app.get("/music/api/v1/favorite-track/list")
async def favorite_track_list(request: Request):
    upstream_client = get_upstream_client(request.app)
    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)

    req = upstream_client.build_request("GET", url_path, headers=headers)
    upstream_resp = await upstream_client.send(req)
    resp_headers = filter_headers(upstream_resp.headers, exclude_keys={"content-length", "content-encoding"})

    if upstream_resp.status_code != 200:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    try:
        upstream_json = upstream_resp.json()
    except Exception:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    if not isinstance(upstream_json, dict) or upstream_json.get("code") != 0:
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    # 探测当前用户身份
    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    # 成功获取官方列表，合并本地在线收藏
    data = upstream_json.get("data")
    if not isinstance(data, dict):
        data = {"list": [], "total": 0}
        upstream_json["data"] = data

    official_list = data.get("list")
    if not isinstance(official_list, list):
        official_list = []
        data["list"] = official_list

    # 飞牛音乐前端收藏列表依赖 isFavorite=True 状态判断，遍历补齐官方列表中可能缺失的字段
    for item in official_list:
        if isinstance(item, dict):
            item["isFavorite"] = True

    official_total = data.get("total")
    if not isinstance(official_total, int):
        official_total = len(official_list)

    async with _FAV_LOCK:
        try:
            fav_items = load_online_favorites(user_guid)
        except Exception as e:
            logger.warning("Error reading online favorites for list for user %s: %s", user_guid, e)
            fav_items = []

    # 按 createdAt 倒序
    fav_items_sorted = sorted(fav_items, key=lambda x: x.get("createdAt", 0), reverse=True)
    online_tracks = []
    for it in fav_items_sorted:
        t = it.get("track")
        if isinstance(t, dict):
            # 确保关键属性为最新或格式完整
            t["isFavorite"] = True
            online_tracks.append(t)
        else:
            g = it.get("guid") or ""
            if g:
                online_tracks.append(build_favorite_track_obj(g, created_at=it.get("createdAt")))

    data["list"] = official_list + online_tracks
    data["total"] = official_total + len(online_tracks)

    return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)


# === daily recommend + play history ===

_HISTORY_LOCK = asyncio.Lock()
_DAILY_TASKS: dict[str, asyncio.Task] = {}


def _prune_stale_daily_tasks(day: str) -> None:
    suffix = f":{day}"
    stale = [k for k in list(_DAILY_TASKS) if not str(k).endswith(suffix)]
    for k in stale:
        old = _DAILY_TASKS.pop(k, None)
        if old is not None and not old.done():
            old.cancel()


async def _ensure_daily_task(request: Request, user_guid: str) -> asyncio.Task:
    day = dailyrec.today_key()
    _prune_stale_daily_tasks(day)
    key = f"{user_guid}:{day}"
    task = _DAILY_TASKS.get(key)
    if task is not None and not task.done():
        return task
    if task is not None and task.done():
        try:
            if task.exception() is None:
                result = task.result()
                if isinstance(result, dict) and len(result.get("tracks") or []) >= dailyrec.PLAYLIST_SIZE:
                    return task
        except (asyncio.CancelledError, Exception):
            pass
    async with _FAV_LOCK:
        favs = load_online_favorites(user_guid)
    task = asyncio.create_task(
        dailyrec.get_or_build_daily(
            user_guid=user_guid,
            musicdl_client=get_musicdl_client(request.app) if CONF.get("musicdl_enabled", True) else None,
            musicbox_client=get_musicbox_client(request.app) if CONF["netease_enabled"] else None,
            llm_http=get_llm_client(request.app) if dailyrec.llm_enabled() else None,
            build_track=build_online_track,
            netease_enabled=CONF["netease_enabled"],
            favorite_items=favs,
        )
    )
    _DAILY_TASKS[key] = task
    return task


async def _peek_daily_bundle(request: Request, user_guid: str) -> dict:
    """歌单列表用：有缓存立刻返回；否则后台生成，最多等 2s，超时仍返回占位歌单。"""
    day = dailyrec.today_key()
    dailyrec.purge_stale_daily_cache(user_guid, day)
    cached = dailyrec.load_daily_cache(user_guid, day)
    if cached and cached.get("tracks"):
        return cached
    task = await _ensure_daily_task(request, user_guid)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
    except asyncio.TimeoutError:
        cached = dailyrec.load_daily_cache(user_guid, day)
        if cached and cached.get("tracks"):
            return cached
        return dailyrec.empty_daily_bundle(user_guid)
    except Exception as e:
        logger.warning("daily recommend peek failed: %s", e)
        return dailyrec.empty_daily_bundle(user_guid)


async def _load_daily_bundle(request: Request, user_guid: str) -> dict:
    day = dailyrec.today_key()
    dailyrec.purge_stale_daily_cache(user_guid, day)
    cached = dailyrec.load_daily_cache(user_guid, day)
    if cached and cached.get("tracks"):
        return cached

    task = await _ensure_daily_task(request, user_guid)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=20.0)
    except asyncio.TimeoutError:
        cached = dailyrec.load_daily_cache(user_guid, day)
        if cached and cached.get("tracks"):
            return cached
        return dailyrec.empty_daily_bundle(user_guid)


def _playlist_public_fields(record: dict) -> dict:
    return {
        "guid": record.get("guid"),
        "name": record.get("name") or "每日推荐",
        "coverId": record.get("coverId") or record.get("guid"),
        "createdAt": int(record.get("createdAt") or time.time()),
        "updatedAt": int(record.get("updatedAt") or time.time()),
        "trackCount": int(record.get("trackCount") or 0),
        "isDaily": True,
    }


@app.get("/music/api/v1/playlist/list")
@app.get("/music/api/v1/playlist/list/{subpath:path}")
async def playlist_list(request: Request):
    upstream_client = get_upstream_client(request.app)
    envelope = await fetch_upstream_envelope(request, upstream_client)
    if isinstance(envelope, Response):
        return envelope
    headers = envelope.pop("_ext_headers", {})
    if envelope.get("code") != 0:
        return JSONResponse(content=envelope, headers=headers)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed:
        return auth_resp or JSONResponse(content=envelope, headers=headers)

    try:
        bundle = await _peek_daily_bundle(request, user_guid)
    except Exception as e:
        logger.warning("daily recommend list inject failed: %s", e)
        return JSONResponse(content=envelope, headers=headers)

    data = envelope.get("data")
    if not isinstance(data, dict):
        data = {"list": [], "total": 0}
        envelope["data"] = data
    official = data.get("list")
    if not isinstance(official, list):
        official = []
        data["list"] = official
    rec = _playlist_public_fields(bundle.get("playlist") or {})
    rec["trackCount"] = len(bundle.get("tracks") or [])
    official = [
        it for it in official
        if not (isinstance(it, dict) and dailyrec.is_daily_playlist_guid(str(it.get("guid") or "")))
    ]
    data["list"] = [rec] + official
    total = data.get("total")
    data["total"] = (total if isinstance(total, int) else len(official)) + 1
    return JSONResponse(content=envelope, headers=headers)


@app.get("/music/api/v1/playlist/detail")
async def playlist_detail(request: Request):
    guid = str(request.query_params.get("guid") or "").strip()
    if not dailyrec.is_daily_playlist_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    upstream_client = get_upstream_client(request.app)
    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp
    bundle = await _load_daily_bundle(request, user_guid)
    rec = _playlist_public_fields(bundle.get("playlist") or {})
    rec["trackCount"] = len(bundle.get("tracks") or [])
    return JSONResponse(content={"code": 0, "msg": "ok", "data": rec})


@app.get("/music/api/v1/playlist/batch-detail")
async def playlist_batch_detail(request: Request):
    raw = request.query_params.get("guids") or request.query_params.get("guid") or ""
    guids = [g.strip() for g in raw.split(",") if g.strip()]
    daily_ids = [g for g in guids if dailyrec.is_daily_playlist_guid(g)]
    if not daily_ids:
        return await forward_to_upstream(request, get_upstream_client(request.app))

    upstream_client = get_upstream_client(request.app)
    rest = [g for g in guids if not dailyrec.is_daily_playlist_guid(g)]
    official_list: list = []
    if rest:
        headers = copy_incoming_headers(request)
        req = upstream_client.build_request(
            "GET",
            f"/music/api/v1/playlist/batch-detail?guids={quote(','.join(rest), safe=',')}",
            headers=headers,
        )
        resp = await upstream_client.send(req)
        if resp.status_code == 200:
            try:
                payload = resp.json()
                if isinstance(payload, dict) and payload.get("code") == 0:
                    data = payload.get("data") or {}
                    if isinstance(data, dict) and isinstance(data.get("list"), list):
                        official_list = data["list"]
                    elif isinstance(data, list):
                        official_list = data
            except Exception:
                official_list = []

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp
    bundle = await _load_daily_bundle(request, user_guid)
    rec = _playlist_public_fields(bundle.get("playlist") or {})
    rec["trackCount"] = len(bundle.get("tracks") or [])
    return JSONResponse(content={"code": 0, "msg": "ok", "data": {"list": [rec] + official_list}})


@app.get("/music/api/v1/track/playlist-detail/list")
async def playlist_track_list(request: Request):
    guid = str(
        request.query_params.get("playlistGUID")
        or request.query_params.get("playlistGuid")
        or request.query_params.get("guid")
        or ""
    ).strip()
    if not dailyrec.is_daily_playlist_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    upstream_client = get_upstream_client(request.app)
    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp
    bundle = await _load_daily_bundle(request, user_guid)
    tracks = dailyrec.stamp_playlist_tracks(list(bundle.get("tracks") or []))
    try:
        page = max(int(request.query_params.get("page") or 1), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        size = int(request.query_params.get("size") or 50)
    except (TypeError, ValueError):
        size = 50
    if size < 1:
        size = 50
    start = (page - 1) * size
    page_tracks = tracks[start:start + size] if size != -1 else tracks
    return JSONResponse(
        content={
            "code": 0,
            "msg": "ok",
            "data": {"list": page_tracks, "total": len(tracks), "sort": request.query_params.get("sort") or ""},
        }
    )


@app.post("/music/api/v1/event/report")
async def event_report(request: Request):
    upstream_client = get_upstream_client(request.app)
    raw = await request.body()
    try:
        body = json.loads(raw.decode("utf-8") or "{}") if raw else {}
    except Exception:
        body = {}
    events = body.get("events") if isinstance(body, dict) else None
    online_plays: list[str] = []
    other_events: list = []
    if isinstance(events, list):
        for ev in events:
            if not isinstance(ev, dict):
                continue
            et = str(ev.get("eventType") or ev.get("type") or "")
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            guid = str(payload.get("trackGUID") or payload.get("guid") or "")
            if et in ("track_play", "TrackPlay") and is_online_guid(guid):
                online_plays.append(guid)
            else:
                other_events.append(ev)
    else:
        return await forward_to_upstream(request, upstream_client)

    if online_plays:
        is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
        if is_authed:
            async with _HISTORY_LOCK:
                for guid in online_plays:
                    info = stub_online_info(guid)
                    cached = find_cache_file(guid)
                    title = str(info.get("title") or "")
                    artist = str(info.get("artist") or "")
                    if cached:
                        base = os.path.splitext(os.path.basename(cached))[0]
                        if " - " in base:
                            artist, title = base.split(" - ", 1)
                        elif not title:
                            title = base
                    dailyrec.record_online_play(
                        user_guid,
                        guid,
                        {"guid": guid, "title": title, "artist": artist, "source": source_from_online_guid(guid)},
                    )
        elif auth_resp is not None and not other_events:
            return auth_resp

    if other_events:
        headers = copy_incoming_headers(request)
        fwd = dict(body)
        fwd["events"] = other_events
        req = upstream_client.build_request(
            "POST",
            "/music/api/v1/event/report",
            headers=headers,
            content=json.dumps(fwd).encode("utf-8"),
        )
        resp = await upstream_client.send(req)
        resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    return JSONResponse(content={"code": 0, "msg": "ok", "data": None})


@app.get("/music/api/v1/play-history/list")
async def play_history_list(request: Request):
    upstream_client = get_upstream_client(request.app)
    envelope = await fetch_upstream_envelope(request, upstream_client)
    if isinstance(envelope, Response):
        return envelope
    headers = envelope.pop("_ext_headers", {})
    if envelope.get("code") != 0:
        return JSONResponse(content=envelope, headers=headers)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed:
        return auth_resp or JSONResponse(content=envelope, headers=headers)

    data = envelope.get("data")
    if not isinstance(data, dict):
        data = {"list": [], "total": 0}
        envelope["data"] = data
    official = data.get("list")
    if not isinstance(official, list):
        official = []
        data["list"] = official

    async with _HISTORY_LOCK:
        online_items = dailyrec.load_online_play_history(user_guid)
    online_tracks = []
    for it in reversed(online_items):
        guid = str(it.get("guid") or "")
        if not guid:
            continue
        track = it.get("track") if isinstance(it.get("track"), dict) else {}
        obj = build_favorite_track_obj(guid, track, created_at=int(it.get("playedAt") or time.time()))
        obj["isFavorite"] = False
        online_tracks.append(obj)

    seen = {str(x.get("guid")) for x in official if isinstance(x, dict)}
    merged_online = [t for t in online_tracks if t.get("guid") not in seen]
    data["list"] = merged_online + official
    official_total = data.get("total")
    if not isinstance(official_total, int):
        official_total = len(official)
    data["total"] = official_total + len(merged_online)
    return JSONResponse(content=envelope, headers=headers)


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def catch_all(request: Request, full_path: str):
    return await forward_to_upstream(request, get_upstream_client(request.app))
