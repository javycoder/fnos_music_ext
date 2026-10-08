"""lxmusic-service 测试公共设施。
单实例加载 app + lxserver 桩客户端。
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import re
import sys
from typing import Any

import httpx
import pytest

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
    重复上传报"已存在"、toggle 以 enabled 键生效、activate 单源互斥。
    """

    def __init__(self):
        self.alive = True
        # 模拟 lxserver users/source/_open/sources.json 的列表项字段
        self.sources = [
            {"id": "source1", "name": "test-src", "enabled": True, "version": "1.0.0",
             "supportedSources": ["kw", "kg", "wy"]}
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
        self.url_result = {"url": "https://media.test/song.flac", "type": "flac", "sourceName": "test"}
        self.lyric_result = {"lyric": "[00:00.00] 歌词内容\n[00:05.00] 第二句"}
        self._song_info_cache = {}
        self.toggle_calls: list[tuple[str, bool]] = []
        self.upload_calls: list[tuple[str, str]] = []
        self.deleted_ids: list[str] = []

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

        res = []
        for raw in self.search_results:
            item = map_lxserver_song(raw, fallback_source=source)
            self._song_info_cache[item["id"]] = raw
            res.append(item)
        return res

    async def get_music_url(self, song_info: dict, quality: str = "128k") -> dict | None:
        if self.url_result:
            return dict(self.url_result)
        return None

    async def get_lyric(self, song_info: dict) -> dict | None:
        return self.lyric_result

    async def get_leaderboard_list(self, source: str, board_id: str, page: int = 1) -> list[dict]:
        return await self.search("榜单", source=source)

    def get_cached_song_info(self, track_id: str) -> dict | None:
        return self._song_info_cache.get(track_id)

    def synthesize_song_info(self, track_id: str, fallback_meta: dict | None = None) -> dict:
        return {
            "name": "测试曲目",
            "singer": "测试歌手",
            "source": "kw",
            "songmid": "5886682",
            "albumName": "测试专辑",
            "interval": 200,
            "img": "",
        }

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

    async def activate_single_source(self, target_id_or_name: str) -> bool:
        target = self._find(target_id_or_name)
        if not target:
            return False
        for s in self.sources:
            enable = s is target
            s["enabled"] = enable
            self.toggle_calls.append((s["id"], enable))
        return True

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

    return TestClient(lxapp.app)
