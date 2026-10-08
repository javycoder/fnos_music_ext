"""lxmusic-service/lxserver_client.py
lxserver (XCQ0607/lxserver) HTTP API 客户端与数据映射层。

职责：
1. 与容器内本地运行的 lxserver (默认 http://127.0.0.1:8005) 通信；
2. 带 x-frontend-auth 头部鉴权访问管理端点；
3. 将 lxserver 的音乐对象映射为 fnmusic-ext 契约格式 (lx:<src>:<id>)；
4. 维护 songInfo 双向映射缓存，确保 track/url 与 track/info 解析高命中率；
5. 封装自定义音源管理接口 (import/upload/delete/toggle/list/validate)。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

import httpx

logger = logging.getLogger("fnmusic.lxserver")

DEFAULT_LXSERVER_URL = os.environ.get("LXSERVER_URL", "http://127.0.0.1:8005").rstrip("/")
DEFAULT_ADMIN_PASSWORD = os.environ.get("LXSERVER_ADMIN_PASSWORD", "123456")

# 平台别名归一
_SOURCE_ALIASES = {
    "kugou": "kg",
    "kg": "kg",
    "netease": "wy",
    "163": "wy",
    "wy": "wy",
    "migu": "mg",
    "mg": "mg",
    "qq": "tx",
    "tencent": "tx",
    "tx": "tx",
    "kuwo": "kw",
    "kw": "kw",
}

# 支持的平台集合
SUPPORTED_PLATFORMS = ("kw", "kg", "tx", "wy", "mg")


def normalize_source(src: str | None) -> str:
    if not src:
        return ""
    return _SOURCE_ALIASES.get(src.strip().lower(), "")


def parse_interval_to_seconds(interval: str | int | float | None) -> int:
    """解析 "05:24" 或 324 -> 324 秒。"""
    if interval is None:
        return 0
    if isinstance(interval, (int, float)):
        return max(0, int(interval))
    text = str(interval).strip()
    if not text:
        return 0
    if ":" in text:
        parts = text.split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except ValueError:
            return 0
    try:
        return max(0, int(float(text)))
    except ValueError:
        return 0


def extract_track_identifier(song: dict, source: str) -> str:
    """提取平台主键 identifier：保持与现有契约一致。
    kg: hash (或 songmid)
    kw: songmid (即 rid)
    wy: songmid (即 id)
    tx: songmid
    mg: copyrightId (或 songmid)
    """
    source = normalize_source(source or song.get("source"))
    # 酷狗优先用 hash
    if source == "kg":
        # 寻找最高音质的 hash 或顶层 hash
        for key in ("hash", "hash_flac", "hash_320k", "hash_128k"):
            val = song.get(key)
            if val:
                return str(val).lower()
        types = song.get("types") or []
        for t in types:
            if isinstance(t, dict) and t.get("hash"):
                return str(t["hash"]).lower()
    # 咪咕优先用 copyrightId
    if source == "mg":
        cid = song.get("copyrightId")
        if cid:
            return str(cid)
    # 默认用 songmid 或 id
    mid = song.get("songmid") or song.get("id") or song.get("rid")
    if mid:
        return str(mid)
    return ""


def map_lxserver_song(song: dict, fallback_source: str = "") -> dict:
    """将 lxserver song 对象映射为 fnmusic-ext 标准条目。"""
    source = normalize_source(song.get("source") or fallback_source)
    identifier = extract_track_identifier(song, source)
    if not identifier:
        identifier = str(song.get("songmid") or song.get("id") or int(time.time() * 1000))

    track_id = f"lx:{source}:{identifier}"
    duration_s = parse_interval_to_seconds(song.get("interval"))
    title = str(song.get("name") or song.get("title") or "未知曲目").strip()
    artist = str(song.get("singer") or song.get("artist") or "未知歌手").strip()
    album = str(song.get("albumName") or song.get("album") or "").strip()
    cover_url = str(song.get("img") or song.get("pic") or song.get("picUrl") or "").strip()

    # 提取音质与文件扩展名
    types_list = []
    types_raw = song.get("types")
    if isinstance(types_raw, list):
        for t in types_raw:
            if isinstance(t, dict) and t.get("type"):
                types_list.append(str(t["type"]))
            elif isinstance(t, str):
                types_list.append(t)
    elif isinstance(types_raw, dict):
        types_list = list(types_raw.keys())

    ext = "mp3"
    if "flac" in types_list or "flac24bit" in types_list or "hires" in types_list:
        ext = "flac"

    item: dict[str, Any] = {
        "id": track_id,
        "lx_source": source,
        "title": title,
        "artist": artist,
        "album": album,
        "duration_s": duration_s,
        "ext": ext,
        "cover_url": cover_url,
        "types": types_list,
        # 兼容旧搜索器字段
        "songmid": str(song.get("songmid") or identifier),
        "album_id": str(song.get("albumId") or ""),
    }

    # 针对酷狗保留 hash 映射
    if source == "kg":
        item["hash"] = identifier
        if isinstance(song.get("types"), list):
            for t in song["types"]:
                if isinstance(t, dict) and t.get("hash"):
                    q = t.get("type", "")
                    if q == "128k":
                        item["hash_128k"] = t["hash"]
                    elif q == "320k":
                        item["hash_320k"] = t["hash"]
                    elif "flac" in q:
                        item["hash_flac"] = t["hash"]

    # 针对咪咕保留 copyrightId
    if source == "mg":
        item["copyrightId"] = str(song.get("copyrightId") or identifier)

    return item


class LxServerClient:
    """与本地 lxserver 通信的 HTTP 客户端。"""

    def __init__(
        self,
        base_url: str = DEFAULT_LXSERVER_URL,
        admin_password: str = DEFAULT_ADMIN_PASSWORD,
        timeout: float = 20.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.admin_password = admin_password
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        # songInfo 反查缓存: track_id -> songInfo dict
        self._song_info_cache: dict[str, dict] = {}
        self._cache_max = 2000

    async def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=5.0),
                headers={
                    "User-Agent": "fnmusic-lxserver-client/1.0",
                    "x-frontend-auth": self.admin_password,
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def cache_song_info(self, track_id: str, song_info: dict) -> None:
        if not track_id or not song_info:
            return
        if len(self._song_info_cache) >= self._cache_max:
            # 清理前 25% 的旧条目
            keys = list(self._song_info_cache.keys())[: self._cache_max // 4]
            for k in keys:
                self._song_info_cache.pop(k, None)
        self._song_info_cache[track_id] = song_info

    def get_cached_song_info(self, track_id: str) -> dict | None:
        return self._song_info_cache.get(track_id)

    def synthesize_song_info(self, track_id: str, fallback_meta: dict | None = None) -> dict:
        """从 track_id 及元数据合成一个合法的洛雪 songInfo 对象。"""
        # 优先读取缓存
        cached = self.get_cached_song_info(track_id)
        if cached:
            return cached

        parts = track_id.split(":", 2)
        if len(parts) == 3 and parts[0] == "lx":
            source = normalize_source(parts[1])
            identifier = parts[2]
        elif len(parts) == 2:
            source = normalize_source(parts[0])
            identifier = parts[1]
        else:
            source = "kw"
            identifier = track_id

        meta = fallback_meta or {}
        name = meta.get("title") or meta.get("name") or "未知曲目"
        singer = meta.get("artist") or meta.get("singer") or "未知歌手"
        album_name = meta.get("album") or meta.get("albumName") or ""

        info: dict[str, Any] = {
            "name": name,
            "singer": singer,
            "source": source,
            "songmid": identifier,
            "albumName": album_name,
            "interval": meta.get("duration_s") or 0,
            "img": meta.get("cover_url") or "",
        }

        if source == "kg":
            info["hash"] = identifier
            info["albumId"] = meta.get("album_id") or ""
        elif source == "mg":
            info["copyrightId"] = identifier
        elif source == "kw":
            info["rid"] = identifier
            info["albumId"] = meta.get("album_id") or ""

        return info

    # ------------------------------------------------------------- 状态端点 --
    async def get_status(self) -> dict | None:
        """获取 lxserver 状态信息。"""
        try:
            client = await self.get_client()
            resp = await client.get("/api/status", timeout=3.0)
            if resp.status_code == 200:
                return resp.json()
        except Exception as exc:
            logger.debug("lxserver /api/status failed: %s", exc)
        return None

    async def is_alive(self) -> bool:
        status = await self.get_status()
        return status is not None

    # ------------------------------------------------------------- 搜索端点 --
    async def search(
        self,
        keyword: str,
        source: str = "kw",
        page: int = 1,
        pages: int = 1,
        limit: int = 20,
    ) -> list[dict]:
        """单平台搜索。返回已映射的标准 item 列表。"""
        src = normalize_source(source)
        if not src:
            return []
        client = await self.get_client()
        params = {
            "name": keyword,
            "source": src,
            "page": page,
            "pages": pages,
            "limit": limit,
        }
        resp = await client.get("/api/music/search", params=params, timeout=self.timeout)
        if resp.status_code != 200:
            logger.warning("lxserver search error %s for %s (%s)", resp.status_code, keyword, src)
            return []
        data = resp.json()
        if not isinstance(data, list):
            return []

        results = []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            item = map_lxserver_song(raw, fallback_source=src)
            track_id = item["id"]
            # 记录完整原始 songInfo 供后续反查解析
            self.cache_song_info(track_id, raw)
            results.append(item)
        return results

    # ------------------------------------------------------------- 播放链接解析 --
    async def get_music_url(self, song_info: dict, quality: str = "128k") -> dict | None:
        """调用 lxserver /api/music/url 解析直链。

        返回格式:
        {"url": "...", "type": "128k", "sourceName": "...", "sourceId": "...", ...}
        若解析失败返回 None。
        """
        client = await self.get_client()
        body = {
            "songInfo": song_info,
            "quality": quality,
            "enableAutoSwitchApiSource": True,
        }
        resp = await client.post("/api/music/url", json=body, timeout=self.timeout)
        if resp.status_code != 200:
            logger.warning("lxserver get_music_url error %s: %.150s", resp.status_code, resp.text)
            return None
        data = resp.json()
        if isinstance(data, dict) and data.get("url"):
            return data
        return None

    # ------------------------------------------------------------- 歌词端点 --
    async def get_lyric(self, song_info: dict) -> dict | None:
        """调用 lxserver /api/music/lyric 获取歌词。"""
        client = await self.get_client()
        body = {"songInfo": song_info}
        resp = await client.post("/api/music/lyric", json=body, timeout=self.timeout)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if isinstance(data, dict):
            return data
        return None

    # ------------------------------------------------------------- 榜单端点 --
    async def get_leaderboard_boards(self, source: str = "kg") -> list[dict]:
        """获取平台的可用榜单列表。"""
        src = normalize_source(source) or "kg"
        client = await self.get_client()
        resp = await client.get("/api/music/leaderboard/boards", params={"source": src}, timeout=self.timeout)
        if resp.status_code != 200:
            return []
        data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            return data["list"]
        if isinstance(data, list):
            return data
        return []

    async def get_leaderboard_list(self, source: str, board_id: str, page: int = 1) -> list[dict]:
        """获取指定榜单内的歌曲列表并做数据映射。"""
        src = normalize_source(source)
        client = await self.get_client()
        params = {"source": src, "id": board_id, "page": page}
        resp = await client.get("/api/music/leaderboard/list", params=params, timeout=self.timeout)
        if resp.status_code != 200:
            return []
        data = resp.json()
        raw_list = []
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            raw_list = data["list"]
        elif isinstance(data, list):
            raw_list = data

        items = []
        for raw in raw_list:
            if not isinstance(raw, dict):
                continue
            item = map_lxserver_song(raw, fallback_source=src)
            self.cache_song_info(item["id"], raw)
            items.append(item)
        return items

    # ------------------------------------------------------------- 自定义音源管理 --
    async def list_custom_sources(self) -> list[dict]:
        """获取所有已导入的自定义音源列表。"""
        client = await self.get_client()
        resp = await client.get("/api/custom-source/list", timeout=self.timeout)
        if resp.status_code != 200:
            return []
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("sources"), list):
            return data["sources"]
        return []

    async def import_custom_source(self, url: str) -> dict:
        """从 URL 导入自定义源。lxserver 端点期望 JSON {url}，成功返回 {success,id,metadata,...}。"""
        client = await self.get_client()
        resp = await client.post("/api/custom-source/import", json={"url": url}, timeout=self.timeout)
        data = self._parse_admin_resp(resp, "导入自定义源")
        return data

    async def upload_custom_source(self, filename: str, script_content: str) -> dict:
        """上传自定义源脚本文本。lxserver 端点期望 JSON {filename, content}，成功返回 {success,id,metadata,...}。"""
        client = await self.get_client()
        resp = await client.post(
            "/api/custom-source/upload",
            json={"filename": filename, "content": script_content},
            timeout=self.timeout,
        )
        return self._parse_admin_resp(resp, "上传自定义源")

    async def toggle_custom_source(self, source_id: str, enable: bool) -> bool:
        """启用或禁用某音源（lxserver 端点键名为 enabled）。"""
        client = await self.get_client()
        resp = await client.post(
            "/api/custom-source/toggle",
            json={"id": source_id, "enabled": bool(enable)},
            timeout=self.timeout,
        )
        data = self._parse_admin_resp(resp, f"切换自定义源 {source_id}")
        return bool(data.get("success", True))

    async def delete_custom_source(self, source_id: str) -> bool:
        """删除某音源。"""
        client = await self.get_client()
        resp = await client.post("/api/custom-source/delete", json={"id": source_id}, timeout=self.timeout)
        data = self._parse_admin_resp(resp, f"删除自定义源 {source_id}")
        return bool(data.get("success", True))

    @staticmethod
    def _parse_admin_resp(resp: httpx.Response, action: str) -> dict:
        """lxserver 管理端点约定：HTTP 200 + {success: true} 才算成功，
        失败时（HTTP 500/403 或 success=false）抛 RuntimeError 带服务端错误文案。"""
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code == 200 and data.get("success") is not False:
            return data
        message = data.get("error") or data.get("message") or f"{action}失败 (HTTP {resp.status_code})"
        raise RuntimeError(message)

    async def activate_single_source(self, target_id_or_name: str) -> bool:
        """单源激活语义：只启用 target，禁用其余所有源。"""
        sources = await self.list_custom_sources()
        found = False
        for s in sources:
            sid = str(s.get("id") or "")
            sname = str(s.get("name") or "")
            is_target = (sid == target_id_or_name) or (sname == target_id_or_name)
            if is_target:
                found = True
                await self.toggle_custom_source(sid, True)
            else:
                await self.toggle_custom_source(sid, False)
        return found
