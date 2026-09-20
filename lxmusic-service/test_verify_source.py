"""verify_source 校验链路测试：下载→init→平台交集→搜索→musicUrl→探活→报告。

verify_source 内部 `import app`：每个用例把已加载的 lxmusic_service_app 实例
注册为 sys.modules["app"]，保证操作的是同一份模块状态。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

from conftest import FakeRuntime, STUB_SCRIPT, lxapp, mock_client
import source_runtime as sr
from source_runtime import SourceError

_KW_RS_BODY = (
    "{'abslist':["
    "{'MUSICRID':'MUSIC_228908','SONGNAME':'晴天','ARTIST':'周杰伦','ALBUM':'叶惠美',"
    "'DURATION':269,'PAY':0,'payInfo':{'cannotOnlinePlay':'0'}}"
    "]}"
)


def _load_verify():
    path = Path(sr.__file__).with_name("verify_source.py")
    spec = importlib.util.spec_from_file_location("verify_source_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vsr = _load_verify()


class UserSourceShim(FakeRuntime):
    """按 verify_source 的 UserSource(script, meta, script_dir=...) 构造签名适配替身。"""

    def __init__(self, script, meta, *, script_dir=None, platforms=None):
        super().__init__(platforms=platforms)
        self.script = script
        self.meta = meta
        self.stopped = False

    async def start(self):
        return None

    async def stop(self):
        self.stopped = True
        self.running = False


class BrokenSource(UserSourceShim):
    async def start(self):
        raise SourceError("init", "脚本初始化失败")


@pytest.fixture
def app_alias(monkeypatch):
    monkeypatch.setitem(sys.modules, "app", lxapp)


def _kw_handler(media_ok=True):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "search.kuwo.cn" in url:
            return httpx.Response(200, text=_KW_RS_BODY)
        if "media.test" in url:
            if media_ok:
                return httpx.Response(
                    206,
                    headers={"Content-Type": "audio/x-flac", "Content-Range": "bytes 0-1/38210000"},
                    content=b"fLaC",
                )
            return httpx.Response(404)
        return httpx.Response(404)

    return handler


def test_verify_url_ok_full_chain(app_alias, isolated, monkeypatch):
    async def fake_download(url):
        return STUB_SCRIPT

    monkeypatch.setattr(vsr, "download_script", fake_download)
    monkeypatch.setattr(vsr, "UserSource", UserSourceShim)
    lxapp.app.state.http = mock_client(_kw_handler())

    report = asyncio.run(vsr.verify_url("https://src.test/1.js"))
    assert report["ok"] is True
    assert report["category"] == ""
    assert report["meta"]["name"] == "test-src"
    assert report["platforms"] == ["kw"]  # 源声明 ∩ 内置可搜索
    assert report["probe"]["platform"] == "kw"
    assert report["probe"]["quality"] == "128k"
    assert report["probe"]["title"] == "晴天"
    assert report["probe"]["file_size"] == 38210000
    # 试运行不得替换进程级 SOURCE_MANAGER（并发播放仍走当前激活源）
    assert lxapp.SOURCE_MANAGER is isolated
    assert lxapp._RUNTIME_OVERRIDE.get() is None


def test_verify_url_download_failure(app_alias, monkeypatch):
    async def failing(url):
        raise SourceError("download", "下载失败: boom")

    monkeypatch.setattr(vsr, "download_script", failing)
    report = asyncio.run(vsr.verify_url("https://gone.test/1.js"))
    assert report["ok"] is False
    assert report["category"] == "download"
    assert "boom" in report["message"]


def test_verify_url_init_failure(app_alias, monkeypatch):
    async def fake_download(url):
        return STUB_SCRIPT

    monkeypatch.setattr(vsr, "download_script", fake_download)
    monkeypatch.setattr(vsr, "UserSource", BrokenSource)
    report = asyncio.run(vsr.verify_url("https://src.test/1.js"))
    assert report["ok"] is False
    assert report["category"] == "init"


def test_verify_url_no_usable_platform(app_alias, monkeypatch):
    async def fake_download(url):
        return STUB_SCRIPT

    def empty_source(script, meta, *, script_dir=None):
        return UserSourceShim(script, meta, script_dir=script_dir, platforms={})

    monkeypatch.setattr(vsr, "download_script", fake_download)
    monkeypatch.setattr(vsr, "UserSource", empty_source)
    report = asyncio.run(vsr.verify_url("https://src.test/1.js"))
    assert report["ok"] is False
    assert report["category"] == "no_platform"


def test_verify_url_probe_failure(app_alias, isolated, monkeypatch):
    async def fake_download(url):
        return STUB_SCRIPT

    monkeypatch.setattr(vsr, "download_script", fake_download)
    monkeypatch.setattr(vsr, "UserSource", UserSourceShim)

    def handler(request: httpx.Request) -> httpx.Response:
        # kg 搜索免费曲直接收录（不经探活），把失败留到 musicUrl 之后的媒体探活
        if "mobilecdn.kugou.com" in str(request.url):
            return httpx.Response(
                200,
                json={"data": {"info": [{"hash": "H1", "songname": "晴天",
                                         "singername": "周杰伦", "duration": 269000,
                                         "pay_type": 0}]}},
            )
        return httpx.Response(404)

    lxapp.app.state.http = mock_client(handler)
    shim = UserSourceShim(STUB_SCRIPT, {"name": "t"}, platforms={"kg": ["128k", "320k"]})
    monkeypatch.setattr(vsr, "UserSource", lambda *a, **kw: shim)

    report = asyncio.run(vsr.verify_url("https://src.test/1.js"))
    assert report["ok"] is False
    assert report["category"] == "resolve"
    assert "探活" in report["message"]


def test_verify_url_search_empty(app_alias, isolated, monkeypatch):
    async def fake_download(url):
        return STUB_SCRIPT

    monkeypatch.setattr(vsr, "download_script", fake_download)
    monkeypatch.setattr(vsr, "UserSource", UserSourceShim)
    lxapp.app.state.http = mock_client(lambda r: httpx.Response(200, text="{'abslist':[]}"))

    report = asyncio.run(vsr.verify_url("https://src.test/1.js"))
    assert report["ok"] is False
    assert report["category"] == "resolve"


def test_format_report_renders():
    report = {
        "ok": True, "meta": {"name": "测试源", "version": "1.0"},
        "platforms": ["kw", "kg"],
        "probe": {"title": "晴天", "artist": "周杰伦", "platform": "kw",
                  "quality": "128k", "content_type": "audio/flac", "file_size": 38210000},
    }
    text = vsr.format_report(report)
    assert "测试源" in text and "v1.0" in text
    assert "kw,kg" in text
    assert "可用 ✓" in text

    fail = {"ok": False, "meta": None, "platforms": [], "category": "download", "message": "boom"}
    fail_text = vsr.format_report(fail)
    assert "不可用 ✗" in fail_text and "download" in fail_text
