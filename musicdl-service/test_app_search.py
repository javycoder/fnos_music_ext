"""musicdl-service /search 端点的超时与容错行为测试。

宿主环境未安装 musicdl 包（服务跑在 Docker 里），此处用 stub 顶替，
并通过 monkeypatch 替换单源搜索函数来模拟快源/慢源/熔断源。
"""
import sys
import time
import types
import importlib.util
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent

# 在导入 app 之前 stub 掉 musicdl 包（宿主环境未安装，服务跑在 Docker 里）
_musicdl_stub = types.ModuleType("musicdl")
_musicdl_stub.MusicClient = object
_musicdl_stub.musicdl = _musicdl_stub  # app.py: from musicdl import musicdl
sys.modules.setdefault("musicdl", _musicdl_stub)

# 以独立模块名加载 app，避免与 proxy/app.py 在同一 pytest 会话中的 `import app` 冲突
sys.path.insert(0, str(_HERE))
from hardening import AdaptiveTimeout, SourceBreaker  # noqa: E402
sys.path.pop(0)

_spec = importlib.util.spec_from_file_location("musicdl_service_app", _HERE / "app.py")
app_module = importlib.util.module_from_spec(_spec)
sys.modules["musicdl_service_app"] = app_module
_spec.loader.exec_module(app_module)

from fastapi.testclient import TestClient  # noqa: E402


class _FakeSong:
    def __init__(self, sid: int, source: str):
        self.identifier = str(sid)
        self.source = source
        self.song_name = f"Song {sid}"
        self.singers = "Artist"
        self.album = "Album"
        self.duration_s = 200
        self.ext = "mp3"
        self.file_size_bytes = 4096
        self.cover_url = ""
        self.download_url = f"http://example.com/{sid}.mp3"
        self.default_download_headers = {}
        self.lyric = ""


@pytest.fixture()
def clean_state(monkeypatch):
    """每个测试重置全局状态，并使用较短的测试配置。"""
    app_module._SONG_CACHE.clear()
    monkeypatch.setitem(app_module.CONF, "search_timeout", 4.0)
    monkeypatch.setitem(app_module.CONF, "slow_grace_s", 1.0)
    monkeypatch.setitem(app_module.CONF, "fast_return_items", 3)
    monkeypatch.setitem(app_module.CONF, "slow_degrade_s", 2.0)
    app_module.SOURCE_BREAKER = SourceBreaker(
        failure_threshold=app_module.CONF["breaker_threshold"],
        cooldown=app_module.CONF["breaker_cooldown"],
        slow_threshold_s=app_module.CONF["slow_degrade_s"],
    )
    app_module.ADAPTIVE = AdaptiveTimeout(
        base_timeout=app_module.CONF["search_timeout"],
        min_timeout=app_module.CONF["adaptive_min_timeout"],
        slow_latency=app_module.CONF["slow_degrade_s"],
    )
    yield


def _fake_search_factory(calls: dict, plan: dict):
    def _fake(source: str, keyword: str, limit: int) -> list:
        calls.setdefault(source, 0)
        calls[source] += 1
        cfg = plan.get(source)
        if cfg is None:
            return []
        if cfg.get("sleep"):
            time.sleep(cfg["sleep"])
        short = app_module._source_short(source)
        return [_FakeSong(cfg["start"] + i, source) for i in range(cfg.get("count", 0))]

    return _fake


def test_fast_return_when_enough_results(clean_state, monkeypatch):
    """快源返回足够结果后立即返回，不被慢源拖到全局超时。"""
    calls: dict = {}
    monkeypatch.setattr(
        app_module,
        "_search_one_source",
        _fake_search_factory(
            calls,
            {
                "KuwoMusicClient": {"count": 5, "start": 1},
                "MiguMusicClient": {"count": 5, "start": 100, "sleep": 30},
            },
        ),
    )

    with TestClient(app_module.app) as client:
        t0 = time.monotonic()
        resp = client.get("/search", params={"keyword": "hello", "limit": 5})
        elapsed = time.monotonic() - t0

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    # kuwo 的 5 条结果都在
    assert len(data["items"]) == 5
    assert all(item["id"].startswith("kuwo:") for item in data["items"])
    # 慢源被跳过并标记错误
    assert "MiguMusicClient" in data["errors"]
    assert "fast return" in data["errors"]["MiguMusicClient"]
    # 远小于慢源 sleep / 全局超时
    assert elapsed < 10
    assert calls["KuwoMusicClient"] == 1


def test_slow_grace_returns_partial_results(clean_state, monkeypatch):
    """快源有结果但未达阈值时，慢源最多再等 slow_grace_s 秒。"""
    monkeypatch.setattr(
        app_module,
        "_search_one_source",
        _fake_search_factory(
            {},
            {
                "KuwoMusicClient": {"count": 1, "start": 1},
                "MiguMusicClient": {"count": 5, "start": 100, "sleep": 30},
            },
        ),
    )

    with TestClient(app_module.app) as client:
        t0 = time.monotonic()
        resp = client.get("/search", params={"keyword": "grace", "limit": 5})
        elapsed = time.monotonic() - t0

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) >= 1
    # 1 秒宽限期 + 少量开销，远小于全局 4 秒超时或慢源的 30 秒
    assert elapsed < 3.5


def test_open_breaker_source_is_skipped(clean_state, monkeypatch):
    """熔断中的源不再发起搜索，直接以 breaker open 错误返回。"""
    for _ in range(app_module.CONF["breaker_threshold"]):
        app_module.SOURCE_BREAKER.record_failure("KuwoMusicClient")
    assert app_module.SOURCE_BREAKER.is_open("KuwoMusicClient")

    calls: dict = {}
    monkeypatch.setattr(
        app_module,
        "_search_one_source",
        _fake_search_factory(calls, {"MiguMusicClient": {"count": 2, "start": 1}}),
    )

    with TestClient(app_module.app) as client:
        resp = client.get("/search", params={"keyword": "breaker", "limit": 5})

    assert resp.status_code == 200
    data = resp.json()
    assert "KuwoMusicClient" not in calls
    assert data["errors"].get("KuwoMusicClient") == "circuit breaker open"
    assert len(data["items"]) == 2
    assert all(item["id"].startswith("migu:") for item in data["items"])


def test_timeout_shrinks_adaptively_after_failures(clean_state, monkeypatch):
    """源超时后自适应收紧下一次超时。"""
    calls: dict = {}

    def _always_slow(source: str, keyword: str, limit: int) -> list:
        calls.setdefault(source, 0)
        calls[source] += 1
        time.sleep(30)
        return []

    monkeypatch.setattr(app_module, "_search_one_source", _always_slow)

    base = app_module.ADAPTIVE.timeout_for("MiguMusicClient")
    with TestClient(app_module.app) as client:
        resp = client.get("/search", params={"keyword": "slow", "limit": 5})
    assert resp.status_code == 200
    after = app_module.ADAPTIVE.timeout_for("MiguMusicClient")
    assert after < base
    assert after >= app_module.CONF["adaptive_min_timeout"]
    assert calls["MiguMusicClient"] == 1


def test_healthz_reports_breaker_and_adaptive_state(clean_state):
    with TestClient(app_module.app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "breaker_open" in data
    assert "adaptive_timeouts" in data
