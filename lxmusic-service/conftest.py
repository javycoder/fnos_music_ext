"""lxmusic-service 测试公共设施。
单实例加载 app + lxserver 桩客户端。
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
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
    """替身 LxServerClient，供离线单元测试使用。"""

    def __init__(self):
        self.alive = True
        self.sources = [
            {"id": "source1", "name": "test-src", "enable": True, "sources": {"kw": {}, "kg": {}, "wy": {}}}
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
        return {"ok": True, "id": filename, "name": filename}

    async def activate_single_source(self, target_id_or_name: str) -> bool:
        return True

    async def toggle_custom_source(self, source_id: str, enable: bool) -> bool:
        return True

    async def delete_custom_source(self, source_id: str) -> bool:
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
