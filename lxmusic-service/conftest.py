"""lxmusic-service 测试公共设施。
单实例加载 app + lxserver 桩客户端。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import tempfile
from pathlib import Path
import re
import sys
from typing import Any

import httpx
import pytest

# songInfo 磁盘存储（issue #49）路径在 app 导入时定格：先指向临时目录，
# 测试既不读写真实数据目录，也能覆盖加载/落盘行为
os.environ.setdefault("LX_DATA_DIR", tempfile.mkdtemp(prefix="lxmusic-test-data-"))

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def _load_app():
    spec = importlib.util.spec_from_file_location("lxmusic_service_app", HERE / "app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lxmusic_service_app"] = mod
    spec.loader.exec_module(mod)
    return mod


lxapp = _load_app()


class FakeLxServerClient:
    """替身 LxServerClient，供离线单元测试使用。

    自定义源管理方法按 lxserver 真实契约模拟：上传以脚本 @name 派生唯一 id、
    重复上传报"已存在"、toggle 以 enabled 键生效、启用态可多源并存（set_source_enabled 只动目标）。
    """

    def __init__(self):
        self.alive = True
        # 模拟 lxserver users/source/_open/sources.json 的列表项字段（默认两个源、其一启用）
        # status 语义与上游 v2.1.2 对齐：初始化完成后为 success/failed，重载窗口期缺失。
        self.sources = [
            {"id": "source1", "name": "test-src", "enabled": True, "version": "1.0.0",
             "status": "success", "supportedSources": ["kw", "kg", "wy"]},
            {"id": "source2", "name": "test-src-2", "enabled": False, "version": "1.0.0",
             "supportedSources": ["kw", "wy"]},
        ]
        self.search_results = [
            {
                "name": "海阔天空",
                "singer": "Beyond",
                "source": "kw",
                "songmid": "5886682",
                "albumName": "乐与怒",
                "interval": "05:24",
                "img": "https://img.test/cover.jpg",
                "types": [{"type": "128k"}, {"type": "320k"}, {"type": "flac"}],
            }
        ]
        # 跨平台候选搜索替身：平台 -> 原始 song 列表；为空时回退 search_results。
        self.search_results_by_source: dict[str, list[dict]] = {}
        # 搜索调用记录 (source, keyword, page, pages, limit)：候选搜索/缓存断言用。
        self.search_calls: list[tuple[str, str, int, int, int]] = []
        self.url_result = {"url": "https://media.test/song.flac", "type": "flac", "sourceName": "test"}
        self.lyric_result = {"lyric": "[00:00.00] 歌词内容\n[00:05.00] 第二句"}
        self._song_info_cache = {}
        self.toggle_calls: list[tuple[str, bool]] = []
        self.upload_calls: list[tuple[str, str]] = []
        self.import_calls: list[str] = []
        self.deleted_ids: list[str] = []
        # get_music_url 调用记录 (songmid, quality)：缓存/在途合并测试统计实际解析次数用
        self.url_calls: list[tuple[str, str]] = []
        self.url_timeouts: list[float | None] = []
        # 同步记录每次调用的 song_info.source（跨平台候选解析断言用）
        self.url_song_sources: list[str] = []
        # 同步记录每次调用的 excludeApiSources（换源轮换测试断言用）
        self.url_excludes: list[list[str] | None] = []
        # 同步记录每次调用传入的 enableAutoSwitchApiSource（固定脚本必须 false）
        self.url_auto_switch: list[bool] = []
        # 非空时按序弹出作为 get_music_url 返回值（先于 url_result 判定），耗尽后返回 None
        self.url_script: list[dict | None] = []
        # 非空时 get_music_url 等待该事件（同测试循环内使用）：并发时序控制
        self.url_gate: asyncio.Event | None = None
        # 上游排序替身：记录 sourceIds；reorder_mutate=False 模拟上游未生效（校验失败回滚）
        self.reorder_calls: list[list[str]] = []
        self.reorder_fail: Exception | None = None
        self.reorder_mutate = True

    @staticmethod
    def _derive_source_id(filename: str, script: str) -> str:
        # 与 lxserver extractMetadata/generateId 同契约：仅解析 /*! 或 /** 块注释
        block = re.search(r"/\*[*!]([\s\S]*?)\*/", script)
        name = ""
        if block:
            m = re.search(r"@name\s+(.+)", block.group(1))
            if m:
                name = m.group(1).strip()
        if not name:
            name = filename or "source"
        if name.lower().endswith(".js"):
            name = name[:-3]
        return re.sub(r'[\\/:*?"<>|]', "_", name) + ".js"

    def _find(self, target: str):
        for s in self.sources:
            if s["id"] == target or s["name"] == target:
                return s
        return None

    async def is_alive(self) -> bool:
        return self.alive

    async def list_custom_sources(self) -> list[dict]:
        return self.sources

    async def search(self, keyword: str, source: str = "kw", page: int = 1, pages: int = 1, limit: int = 20) -> list[dict]:
        from lxserver_client import map_lxserver_song

        self.search_calls.append((source, keyword, page, pages, limit))
        rows = self.search_results_by_source.get(source, self.search_results)
        res = []
        for raw in rows:
            item = map_lxserver_song(raw, fallback_source=source)
            self._song_info_cache[item["id"]] = raw
            res.append(item)
            if len(res) >= limit:
                break
        return res

    async def get_music_url(self, song_info: dict, quality: str = "128k",
                            exclude_api_sources: list[str] | None = None,
                            request_timeout: float | None = None,
                            auto_switch_api_source: bool = True) -> dict | None:
        """模拟上游 /api/music/url 的真实返回契约。

        真实 lxserver 固定脚本时会在响应里带 ``sourceId``（被执行的脚本 ID）。
        替身按 ``excludeApiSources`` 推断本次被固定的脚本并补上 ``sourceId``；
        ``url_script`` 里的条目是有意构造的上游应答，按原样返回（可模拟
        缺 ID、别的脚本或内置平台应答）。``url_auto_switch`` 记录调用方传入的
        自动切源开关，供“固定脚本必须显式 false”的断言使用。
        """
        self.url_calls.append((str(song_info.get("songmid") or ""), quality))
        self.url_song_sources.append(str(song_info.get("source") or ""))
        self.url_excludes.append(list(exclude_api_sources) if exclude_api_sources else None)
        self.url_timeouts.append(request_timeout)
        self.url_auto_switch.append(bool(auto_switch_api_source))
        if self.url_gate is not None:
            await self.url_gate.wait()
        if self.url_script:
            return self.url_script.pop(0)
        if self.url_result:
            result = dict(self.url_result)
            if not result.get("sourceId"):
                result["sourceId"] = self._pinned_script_id(exclude_api_sources)
            return result
        return None

    def _pinned_script_id(self, exclude_api_sources: list[str] | None) -> str:
        """被固定的脚本 = 启用且就绪、又不在排除列表里的第一个脚本。"""
        excluded = set(exclude_api_sources or ())
        for source in self.sources:
            if not (source.get("enabled") or source.get("enable")):
                continue
            if str(source.get("status") or "") != "success":
                continue
            sid = str(source.get("id") or "")
            if sid and sid not in excluded:
                return sid
        return ""

    async def get_lyric(self, song_info: dict) -> dict | None:
        return self.lyric_result

    async def get_leaderboard_list(self, source: str, board_id: str, page: int = 1, *, strict: bool = False) -> list[dict]:
        return await self.search("榜单", source=source)

    async def get_leaderboard_boards(self, source: str = "kg") -> list[dict]:
        # 与 app._CHART_TARGETS 同名对齐，让动态 bangid 解析在测试里走通 boards 路径
        target, bid = lxapp._CHART_TARGETS.get(source, ("榜单", "1"))
        return [{"id": f"{source}__{bid}", "name": target, "bangid": bid}]

    def get_cached_song_info(self, track_id: str) -> dict | None:
        return self._song_info_cache.get(track_id)

    def cache_song_info(self, track_id: str, song_info: dict) -> None:
        if track_id and song_info:
            self._song_info_cache[track_id] = song_info

    def flush_song_info_store(self) -> None:
        pass

    def synthesize_song_info(self, track_id: str, fallback_meta: dict | None = None) -> dict:
        # 与真实契约对齐：优先消费 fallback_meta 的真实标签，songmid 从 track_id 反解
        meta = fallback_meta or {}
        identifier = track_id.split(":")[-1] if ":" in track_id else track_id
        info = {
            "name": meta.get("title") or meta.get("name") or "测试曲目",
            "singer": meta.get("artist") or meta.get("singer") or "测试歌手",
            "source": "kw",
            "songmid": identifier or "5886682",
            "albumName": meta.get("album") or meta.get("albumName") or "测试专辑",
            "interval": meta.get("duration_s") or 200,
            "img": meta.get("cover_url") or "",
        }
        if info["source"] == "kw":
            info["rid"] = info["songmid"]
        return info

    async def upload_custom_source(self, filename: str, script_content: str) -> dict:
        self.upload_calls.append((filename, script_content))
        source_id = self._derive_source_id(filename, script_content)
        existing = next((s for s in self.sources if s["id"] == source_id), None)
        if existing:
            raise RuntimeError(f'源 "{existing["name"]}" 已存在于 [open]')
        block = re.search(r"/\*[*!]([\s\S]*?)\*/", script_content)
        m = re.search(r"@name\s+(.+)", block.group(1)) if block else None
        name = (m.group(1).strip() if m else filename) or filename
        entry = {"id": source_id, "name": name, "enabled": False, "version": "1.0.0",
                 "supportedSources": ["kw", "wy"]}
        self.sources.append(entry)
        return {"success": True, "id": source_id,
                "metadata": {"name": name, "version": "1.0.0"}, "supportedSources": ["kw", "wy"]}

    async def import_custom_source(self, url: str) -> dict:
        self.import_calls.append(url)
        source_id = url.split("?")[0].rstrip("/").split("/")[-1] or "imported_source.js"
        if not source_id.endswith(".js"):
            source_id += ".js"
        existing = next((s for s in self.sources if s.get("sourceUrl") == url or s["id"] == source_id), None)
        if existing:
            raise RuntimeError(f'源 "{existing["name"]}" 已存在于 [open]')
        name = source_id.removesuffix(".js")
        entry = {
            "id": source_id,
            "name": name,
            "enabled": False,
            "version": "1.0.0",
            "sourceUrl": url,
            "supportedSources": ["kw", "wy"],
        }
        self.sources.append(entry)
        return {
            "success": True,
            "id": source_id,
            "metadata": {"name": name, "version": "1.0.0"},
            "supportedSources": ["kw", "wy"],
        }

    async def set_source_enabled(self, target_id_or_name: str, enabled: bool) -> bool:
        """多源叠加语义：只改目标源启用态，不动其他源。目标不存在返回 False。

        与真实客户端契约对齐：已启用且初始化成功的源重复启用是幂等操作，
        不重复下发 toggle（避免触发上游全组重载）。"""
        target = self._find(target_id_or_name)
        if not target:
            return False
        if enabled and target.get("enabled") and target.get("status") == "success":
            return True
        target["enabled"] = bool(enabled)
        if enabled:
            # 模拟上游初始化完成（真实实现需要轮询等待，这里立即置终态）
            target["status"] = "success"
        self.toggle_calls.append((target["id"], bool(enabled)))
        return True

    async def reorder_custom_sources(self, source_ids: list[str]) -> dict:
        """模拟上游 POST /api/custom-source/reorder {sourceIds: []}。

        reorder_mutate=False 时只记调用、不改列表，用于验证"上游未生效→回滚"。
        """
        self.reorder_calls.append([str(i) for i in source_ids])
        if self.reorder_fail is not None:
            raise self.reorder_fail
        if self.reorder_mutate:
            by_id = {str(s.get("id") or ""): s for s in self.sources}
            ordered = [by_id[i] for i in source_ids if i in by_id]
            ordered += [s for s in self.sources if str(s.get("id") or "") not in source_ids]
            self.sources = ordered
        return {"success": True}

    async def toggle_custom_source(self, source_id: str, enable: bool) -> bool:
        target = self._find(source_id)
        if not target:
            raise RuntimeError("源不存在")
        target["enabled"] = bool(enable)
        self.toggle_calls.append((source_id, bool(enable)))
        return True

    async def delete_custom_source(self, source_id: str) -> bool:
        target = self._find(source_id)
        if target:
            self.sources.remove(target)
            self.deleted_ids.append(source_id)
        return True

    async def close(self) -> None:
        pass


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


@pytest.fixture
def fake_lx():
    fake = FakeLxServerClient()
    orig = lxapp.LXSERVER
    lxapp.LXSERVER = fake
    yield fake
    lxapp.LXSERVER = orig


@pytest.fixture
def test_app_client(fake_lx):
    # 模拟外部调用 app 的客户端
    from starlette.testclient import TestClient

    lxapp.get_http(lxapp.app)
    return TestClient(lxapp.app)
