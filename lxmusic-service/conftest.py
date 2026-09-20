"""lxmusic-service 测试公共设施：单实例加载 app + 用户源替身。

app.py 以 "lxmusic_service_app" 名字加载一次，各测试文件共享同一实例，
与线上进程内模块状态（缓存/熔断器/SOURCE_MANAGER）完全对应。
"""
from __future__ import annotations

import asyncio  # noqa: F401  (供替身解析器使用协程)
import importlib.util
import sys
import tempfile
from pathlib import Path

import httpx
import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from source_runtime import SourceError  # noqa: E402


def _load_app():
    spec = importlib.util.spec_from_file_location("lxmusic_service_app", HERE / "app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lxmusic_service_app"] = mod
    spec.loader.exec_module(mod)
    return mod


lxapp = _load_app()

# 通过 parse_script_meta 校验的桩脚本（SourceManager/UserSource 替身测试用）
STUB_SCRIPT = (
    "/*\n * @name test-src\n * @version 1.0.0\n * @author tester\n"
    " * @description stub source\n */\nconsole.log('boot')\n"
)


class FakeRuntime:
    """替身 UserSource：按声明暴露平台/档位，解析行为可注入。

    resolver 约定：None → 返回默认 FLAC 直链；Exception 实例 → 每次抛出；
    可调用对象 → (music_info, quality, platform) → 直链字符串。
    """

    def __init__(self, platforms=None, resolver=None):
        platforms = platforms if platforms is not None else {"kw": ["128k", "320k", "flac"]}
        self.platforms = {
            code: {"name": code, "actions": ["musicUrl"], "qualitys": list(qualitys)}
            for code, qualitys in platforms.items()
        }
        self._resolver = resolver
        self.calls: list[dict] = []
        self.running = True

    def qualitys(self, platform: str) -> list:
        return list(self.platforms.get(platform, {}).get("qualitys") or [])

    def music_platforms(self) -> list:
        return [p for p in ("kw", "kg", "tx", "wy", "mg") if p in self.platforms]

    async def music_url(self, music_info, quality, *, platform, timeout=10.0):
        self.calls.append({"platform": platform, "quality": quality, "info": music_info})
        resolver = self._resolver
        if resolver is None:
            return "https://media.test/a.flac"
        if isinstance(resolver, Exception):
            raise resolver
        if asyncio.iscoroutinefunction(resolver):
            return await resolver(music_info, quality, platform)
        return resolver(music_info, quality, platform)

    def describe(self) -> dict:
        return {
            "name": "fake-src",
            "platforms": {
                code: {"name": code, "qualitys": info["qualitys"]}
                for code, info in sorted(self.platforms.items())
            },
            "running": self.running,
        }


class FakeManager:
    """替身 SourceManager：满足 app.py 的读取面 + 生命周期端点。"""

    def __init__(self, runtime=None, url="", seed="", last_error=""):
        self._runtime = runtime
        self.active_url = url
        self.seed_url = seed
        self.last_error = last_error
        tmp = Path(tempfile.gettempdir())
        self.state_path = tmp / "lx-state-test.json"
        self.script_cache = tmp / "lx-source-test.js"
        self.activated: list[str] = []
        self.shut_down = 0

    def get(self):
        runtime = self._runtime
        if runtime is not None and getattr(runtime, "running", True):
            return runtime
        return None

    def describe(self) -> dict:
        runtime = self.get()
        return {
            "configured": bool(self.active_url or self.seed_url),
            "url": self.active_url or self.seed_url,
            "initialized": runtime is not None,
            "last_error": self.last_error,
            "source": runtime.describe() if runtime is not None else None,
        }

    async def load(self) -> None:
        return None

    async def shutdown(self) -> None:
        self.shut_down += 1
        self._runtime = None

    async def activate(self, url: str):
        if "bad-source" in url:
            raise SourceError("download", "下载失败: boom")
        self.activated.append(url)
        self.active_url = url


@pytest.fixture(autouse=True)
def isolated(monkeypatch) -> FakeManager:
    """每个测试拿到干净的缓存/熔断器/替身管理器与关闭的 HTTP 客户端。"""
    lxapp._SONG_CACHE.clear()
    lxapp._CHAIN_HEALTH.clear()
    lxapp._STATS.update(searches=0, url_resolutions=0, errors=0)
    lxapp.app.state.http = None
    manager = FakeManager()
    monkeypatch.setattr(lxapp, "SOURCE_MANAGER", manager)
    return manager


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
