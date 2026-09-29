"""musicdl-service /search 端点的超时与容错行为测试。

宿主环境未安装 musicdl 包（服务跑在 Docker 里），此处用 stub 顶替，
并通过 monkeypatch 替换单源搜索函数来模拟快源/慢源/熔断源。
"""
import asyncio
import shutil
import subprocess
import sys
import time
import types
import threading
import importlib.util
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent

# 会话收尾信号：让假慢源线程立即结束，避免 30 秒睡眠拖住解释器退出
_RELEASE_SLOW_SOURCES = threading.Event()


def _interruptible_sleep(seconds: float) -> None:
    _RELEASE_SLOW_SOURCES.wait(seconds)


@pytest.fixture(scope="module", autouse=True)
def _release_slow_source_threads():
    yield
    _RELEASE_SLOW_SOURCES.set()

# 在导入 app 之前 stub 掉 musicdl 包（宿主环境未安装，服务跑在 Docker 里）
_musicdl_stub = types.ModuleType("musicdl")
_musicdl_stub.MusicClient = object
_musicdl_stub.musicdl = _musicdl_stub  # app.py: from musicdl import musicdl
sys.modules.setdefault("musicdl", _musicdl_stub)

# 以独立模块名加载 app，避免与 proxy/app.py 在同一 pytest 会话中的 `import app` 冲突
sys.path.insert(0, str(_HERE))
from hardening import AdaptiveTimeout, SourceBreaker, SourceBulkhead, SingleFlight  # noqa: E402
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
    app_module.SEARCH_CACHE.clear()
    _RELEASE_SLOW_SOURCES.clear()
    workers = SourceBulkhead(app_module.SOURCE_NAMES.values())
    monkeypatch.setattr(app_module, "SOURCE_WORKERS", workers)
    monkeypatch.setattr(app_module, "SINGLE_FLIGHT", SingleFlight())
    monkeypatch.setattr(app_module, "_probe_playable_sync", lambda url, headers: True)
    monkeypatch.setitem(app_module.CONF, "search_timeout", 4.0)
    monkeypatch.setitem(app_module.CONF, "adaptive_min_timeout", 1.0)
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
    _RELEASE_SLOW_SOURCES.set()
    workers.shutdown(wait=True)


def _fake_search_factory(calls: dict, plan: dict):
    def _fake(source: str, keyword: str, limit: int) -> list:
        calls.setdefault(source, 0)
        calls[source] += 1
        cfg = plan.get(source)
        if cfg is None:
            return []
        if cfg.get("sleep"):
            _interruptible_sleep(cfg["sleep"])
        short = app_module._source_short(source)
        return [_FakeSong(cfg["start"] + i, source) for i in range(cfg.get("count", 0))]

    return _fake


def test_normalize_startup_sources():
    """MUSICDL_SOURCES 白名单启动归一：短名→注册全名、未知丢弃（回退默认）。"""
    registered = {"KuwoMusicClient": 1, "BilibiliMusicClient": 2, "MiguMusicClient": 3}
    norm = app_module._normalize_startup_sources
    assert norm(["kuwo", "bilibili", "kuwo"], registered) == ["KuwoMusicClient", "BilibiliMusicClient"]
    assert norm(["KuwoMusicClient", "migu"], registered) == ["KuwoMusicClient", "MiguMusicClient"]
    assert norm(["nope"], registered) == ["KuwoMusicClient", "MiguMusicClient"]
    assert norm([], registered) == ["KuwoMusicClient", "MiguMusicClient"]


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
    assert len(app_module.SEARCH_CACHE) == 0


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
        _interruptible_sleep(30)
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


def test_search_filters_unplayable_and_trial_and_404(clean_state, monkeypatch):
    """过滤无效 download_url、试听标题标记以及探活 404 直链。"""
    s1 = _FakeSong(1, "KuwoMusicClient")
    s1.download_url = ""  # 无直链，应过滤

    s2 = _FakeSong(2, "KuwoMusicClient")
    s2.song_name = "晴天(试听版)"  # 试听标题，应过滤

    s3 = _FakeSong(3, "KuwoMusicClient")
    s3.download_url = "http://example.com/404/error.html"  # 错误页，应过滤

    s4 = _FakeSong(4, "KuwoMusicClient")
    s4.download_url = "http://example.com/dead.mp3"  # 探活返回 False，应过滤

    s5 = _FakeSong(5, "KuwoMusicClient")
    s5.download_url = "http://example.com/valid.mp3"  # 正常有效直链

    def _fake_search(source: str, keyword: str, limit: int) -> list:
        if source == "KuwoMusicClient":
            return [s1, s2, s3, s4, s5]
        return []

    def _fake_probe(url: str, headers: dict) -> bool:
        if "dead.mp3" in url:
            return False
        return True

    monkeypatch.setattr(app_module, "_search_one_source", _fake_search)
    monkeypatch.setattr(app_module, "_probe_playable_sync", _fake_probe)

    with TestClient(app_module.app) as client:
        resp = client.get("/search", params={"keyword": "晴天", "sources": "kuwo"})
    assert resp.status_code == 200
    rj = resp.json()
    assert rj["ok"] is True
    items = rj["items"]
    assert len(items) == 1
    assert items[0]["id"] == "kuwo:5"
    assert items[0]["download_url"] == "http://example.com/valid.mp3"


def test_repeated_slow_searches_do_not_starve_fast_source(clean_state, monkeypatch):
    started = threading.Event()
    calls = {}
    def fake(source, keyword, limit):
        calls[source] = calls.get(source, 0) + 1
        if source == "MiguMusicClient":
            started.set()
            _interruptible_sleep(30)
        return [_FakeSong(1, source)]
    monkeypatch.setattr(app_module, "_search_one_source", fake)
    monkeypatch.setattr(app_module, "ADAPTIVE", AdaptiveTimeout(0.05, 0.01))
    with TestClient(app_module.app) as client:
        first = client.get("/search", params={"keyword": "slow", "sources": "migu"}).json()
        assert started.is_set()
        assert "timeout" in first["errors"]["MiguMusicClient"]
        for index in range(10):
            data = client.get("/search", params={"keyword": f"next{index}"}).json()
            assert [item["id"] for item in data["items"]] == ["kuwo:1"]
            assert "busy" in data["errors"]["MiguMusicClient"]
        assert calls["MiguMusicClient"] == 1
        assert calls["KuwoMusicClient"] == 10


def test_search_playable_probes_parallel_and_keeps_library_order(clean_state, monkeypatch):
    """并行探活：凑够 limit 即停、结果保持库序，慢探活不再串行拖住 worker。"""
    songs = [_FakeSong(i, "KuwoMusicClient") for i in range(1, 9)]
    monkeypatch.setattr(app_module, "_search_one_source", lambda source, keyword, limit: songs)

    def _fake_probe(url: str, headers: dict) -> bool:
        time.sleep(0.1)
        return int(url.rsplit("/", 1)[-1].split(".")[0]) % 2 == 0  # 偶数 id 有效

    monkeypatch.setattr(app_module, "_probe_playable_sync", _fake_probe)

    started = time.monotonic()
    entries = app_module._search_playable(
        "KuwoMusicClient", "k", 8, 3, None, time.monotonic() + 5, None)
    elapsed = time.monotonic() - started
    # 库序前 3 条有效直链：2、4、6；凑够后 7、8 不再影响输出
    assert [e[0]["id"] for e in entries] == ["kuwo:2", "kuwo:4", "kuwo:6"]
    # 串行逐首探测至少 6 次 × 0.1s = 0.6s；并行一轮 + 凑够即停应明显更快
    assert elapsed < 0.5


def test_search_playable_marks_partial_on_probe_deadline(clean_state, monkeypatch):
    """探活超出 deadline：标记 partial，已确认的结果照常交出。"""
    songs = [_FakeSong(i, "KuwoMusicClient") for i in range(1, 5)]
    monkeypatch.setattr(app_module, "_search_one_source", lambda source, keyword, limit: songs)

    def _fake_probe(url: str, headers: dict) -> bool:
        if url.endswith("/1.mp3"):
            time.sleep(0.05)
            return True  # 第一首快速通过
        time.sleep(0.5)  # 其余都超过 deadline
        return True

    monkeypatch.setattr(app_module, "_probe_playable_sync", _fake_probe)

    progress = app_module.SearchProgress(10, time.monotonic() + 1.0)
    entries = app_module._search_playable(
        "KuwoMusicClient", "k", 4, 4, None, time.monotonic() + 0.25, progress)
    assert progress.partial is True
    assert [e[0]["id"] for e in entries] == ["kuwo:1"]
    assert [it["id"] for it, _, _ in progress.finish()] == ["kuwo:1"]


def test_search_playable_song_id_mode_filters_to_target(clean_state, monkeypatch):
    """song_id 模式（URL 过期重搜）只探目标曲目。"""
    songs = [_FakeSong(i, "KuwoMusicClient") for i in range(1, 5)]
    monkeypatch.setattr(app_module, "_search_one_source", lambda source, keyword, limit: songs)
    monkeypatch.setattr(app_module, "_probe_playable_sync", lambda url, headers: True)
    entries = app_module._search_playable(
        "KuwoMusicClient", "k", 4, 1, "kuwo:3", None, None)
    assert [e[0]["id"] for e in entries] == ["kuwo:3"]


def test_consecutive_keyword_searches_wait_for_busy_source(clean_state, monkeypatch):
    """上一个关键词的慢任务占坑时，下一个关键词排队等坑而不是立即交回 0 条。"""
    first_started = threading.Event()
    release = threading.Event()
    calls = {}

    def fake(source, keyword, limit):
        calls[(source, keyword)] = calls.get((source, keyword), 0) + 1
        if keyword == "slowkw":
            first_started.set()
            release.wait(5)
        return [_FakeSong(7, source)]

    monkeypatch.setattr(app_module, "_search_one_source", fake)
    monkeypatch.setattr(app_module, "ADAPTIVE", AdaptiveTimeout(base_timeout=1.0, min_timeout=0.5))

    with TestClient(app_module.app) as client:
        first = client.get("/search", params={"keyword": "slowkw", "sources": "migu"}).json()
        assert first_started.is_set()
        assert "timeout" in first["errors"]["MiguMusicClient"]
        # 第一个 worker 仍占着 migu 的坑；开一个线程稍后放行
        def _release_later():
            time.sleep(0.2)
            release.set()
        threading.Thread(target=_release_later, daemon=True).start()
        second = client.get("/search", params={"keyword": "fastkw", "sources": "migu"}).json()
        assert [item["id"] for item in second["items"]] == ["migu:7"]
        assert calls[("MiguMusicClient", "fastkw")] == 1


@pytest.mark.parametrize("canonical,alias", [
    ("HTQYYMusicClient", "htqyy"),
    ("FiveSingMusicClient", "FiVeSiNg"),
    ("MyFreeMP3MusicClient", "myfreemp3"),
    ("QQMusicClient", "qqmusicclient"),
])
def test_canonical_search_and_refresh_mapping(clean_state, monkeypatch, canonical, alias):
    monkeypatch.setitem(app_module.CONF, "sources", [canonical])
    mapping = app_module._source_mapping()
    monkeypatch.setattr(app_module, "SOURCE_NAMES", mapping)
    workers = SourceBulkhead(mapping.values())
    monkeypatch.setattr(app_module, "SOURCE_WORKERS", workers)
    calls = []
    def fake(source, keyword, limit):
        calls.append(source)
        return [_FakeSong(7, source)]
    monkeypatch.setattr(app_module, "_search_one_source", fake)
    try:
        with TestClient(app_module.app) as client:
            data = client.get("/search", params={"keyword": "test", "sources": f" {alias}, {canonical} "}).json()
            song_id = data["items"][0]["id"]
            assert song_id == f"{app_module._source_short(canonical)}:7"
            refreshed = asyncio.run(app_module._refresh_by_keyword(song_id))
            assert refreshed["item"]["id"] == song_id
            assert calls == [canonical, canonical]
            bad = client.get("/search", params={"keyword": "test", "sources": "invented"})
            assert bad.status_code == 400
    finally:
        workers.shutdown()


def test_registry_mapping_preserves_unconfigured_names(clean_state, monkeypatch):
    registry = types.ModuleType("musicdl.modules.sources")
    registry.MusicClientBuilder = types.SimpleNamespace(REGISTERED_MODULES={
        "HTQYYMusicClient": object, "FiveSingMusicClient": object,
        "MyFreeMP3MusicClient": object,
    })
    monkeypatch.setitem(sys.modules, "musicdl.modules.sources", registry)
    mapping = app_module._source_mapping()
    assert mapping["htqyy"] == "HTQYYMusicClient"
    assert mapping["fivesingmusicclient"] == "FiveSingMusicClient"
    assert mapping["myfreemp3"] == "MyFreeMP3MusicClient"


def test_refresh_shares_search_bulkhead(clean_state, monkeypatch):
    song = _FakeSong(3, "KuwoMusicClient")
    app_module._cache_put(app_module._normalize(song, "same"), "same", {}, "")
    release = threading.Event()
    started = threading.Event()
    def slow(*args):
        started.set()
        assert release.wait(5)
        return [song]
    monkeypatch.setattr(app_module, "_search_one_source", slow)
    async def main():
        task = asyncio.create_task(app_module.SOURCE_WORKERS.run(
            "KuwoMusicClient", slow, timeout=0.05))
        with pytest.raises(asyncio.TimeoutError):
            await task
        assert started.is_set()
        assert await app_module._refresh_by_keyword("kuwo:3") is None
    try:
        asyncio.run(main())
    finally:
        release.set()


def test_worker_skips_probes_after_deadline(clean_state, monkeypatch):
    monkeypatch.setattr(app_module, "_search_one_source", lambda *args: [_FakeSong(1, "KuwoMusicClient")])
    def unexpected(*args):
        pytest.fail("expired search should not start a probe")
    monkeypatch.setattr(app_module, "_probe_playable_sync", unexpected)
    assert app_module._search_playable("KuwoMusicClient", "expired", 10, 1,
                                      deadline=time.monotonic() - 1) == []


def test_musicdl_receives_supported_network_limits(clean_state, monkeypatch):
    captured = {}
    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)
        def search(self, keyword):
            return {"KuwoMusicClient": []}
    monkeypatch.setattr(app_module.musicdl, "MusicClient", FakeClient)
    app_module._search_one_source("KuwoMusicClient", "offline", 10)
    assert captured["requests_overrides"] == {
        "KuwoMusicClient": {"timeout": app_module.CONF["request_timeout"]}}
    assert captured["clients_threadings"] == {"KuwoMusicClient": 1}
    assert captured["init_music_clients_cfg"]["KuwoMusicClient"]["max_retries"] == 1
    assert captured["init_music_clients_cfg"]["KuwoMusicClient"]["search_size_per_source"] == 10
    captured.clear()
    app_module._search_one_source("KuwoMusicClient", "offline", 60)
    assert captured["init_music_clients_cfg"]["KuwoMusicClient"]["search_size_per_source"] == app_module.CONF["search_page_cap"]


def test_restart_errors_and_one_research_restore_same_id(clean_state, monkeypatch):
    calls = {}
    monkeypatch.setattr(app_module, "_search_one_source", _fake_search_factory(
        calls, {"KuwoMusicClient": {"count": 1, "start": 8}}))
    with TestClient(app_module.app) as client:
        params = {"keyword": "recover", "sources": "kuwo"}
        song_id = client.get("/search", params=params).json()["items"][0]["id"]
        app_module._SONG_CACHE.clear()
        # Also exercises search-result cache surviving song-cache eviction.
        for endpoint in ("/info", "/stream"):
            response = client.get(endpoint, params={"id": song_id})
            assert response.status_code == 404
            assert "re-search keyword and source once" in response.json()["detail"]
        recovered = client.get("/search", params=params).json()
        assert recovered["cached"] is False
        assert recovered["items"][0]["id"] == song_id
        assert calls == {"KuwoMusicClient": 2}
        assert client.get("/info", params={"id": song_id}).status_code == 200
        assert client.get("/stream", params={"id": song_id}, follow_redirects=False).status_code == 302


def test_failed_refresh_does_not_redirect_to_expired_url(clean_state, monkeypatch):
    song = _FakeSong(1, "KuwoMusicClient")
    app_module._cache_put(app_module._normalize(song, "old"), "old", {}, "")
    app_module._SONG_CACHE["kuwo:1"]["ts"] = 0
    monkeypatch.setattr(app_module, "_head_probe_sync", lambda *args: False)
    calls = []
    def fake(*args):
        calls.append(args)
        return []
    monkeypatch.setattr(app_module, "_search_one_source", fake)
    with TestClient(app_module.app) as client:
        response = client.get("/stream", params={"id": "kuwo:1"}, follow_redirects=False)
        assert response.status_code == 502
        assert "bounded refresh failed" in response.json()["detail"]
        assert len(calls) == 1


def test_default_budget_limit30_keeps_healthy_results(clean_state, monkeypatch):
    """60 首候选、直链全部有效：并行探活凑满 limit=30 即停，不落共享缓存。"""
    monkeypatch.setattr(app_module, "_search_one_source", lambda *args: [
        _FakeSong(i, "KuwoMusicClient") for i in range(60)])
    monkeypatch.setattr(app_module, "_probe_playable_sync", lambda *args: True)
    progress = app_module.SearchProgress(30, time.monotonic() + 12.0)
    entries = app_module._search_playable("KuwoMusicClient", "budget", 60, 30,
                                        deadline=time.monotonic() + 11.9, progress=progress)
    assert len(entries) == 30
    # 交付顺序允许乱序（按探活完成顺序），但集合与返回值一致
    handed = progress.finish()
    assert len(handed) == 30
    assert {item["id"] for item, _, _ in handed} == {entry[0]["id"] for entry in entries}
    assert not progress.partial
    assert not app_module._SONG_CACHE


def test_parallel_probes_return_partial_before_budget(clean_state, monkeypatch):
    """探活预算内探不完：已确认条目照常交付并标记 partial，不落缓存。"""
    monkeypatch.setattr(app_module, "ADAPTIVE", AdaptiveTimeout(0.3, 0.2))
    songs = [_FakeSong(i, "KuwoMusicClient") for i in range(5)]
    monkeypatch.setattr(app_module, "_search_one_source", lambda *args: songs)

    def probe(url, headers):
        # 前两首立即确认，其余慢过探活预算
        if url.endswith(("/0.mp3", "/1.mp3")):
            time.sleep(0.01)
            return True
        time.sleep(0.5)
        return True

    monkeypatch.setattr(app_module, "_probe_playable_sync", probe)
    with TestClient(app_module.app) as client:
        for _ in range(2):
            data = client.get("/search", params={"keyword": "partial", "sources": "kuwo", "limit": 5}).json()
            assert len(data["items"]) == 2
            assert "partial" in data["errors"]["KuwoMusicClient"]
            assert data["cached"] is False
        assert len(app_module.SEARCH_CACHE) == 0


@pytest.mark.parametrize("grace", [False, True])
def test_stalled_probe_hands_off_completed_results_only(clean_state, monkeypatch, grace):
    release = threading.Event()
    stalled = threading.Event()
    monkeypatch.setattr(app_module, "ADAPTIVE", AdaptiveTimeout(1 if grace else 0.08, 0.08))
    monkeypatch.setitem(app_module.CONF, "slow_grace_s", 0.04)
    monkeypatch.setitem(app_module.CONF, "fast_return_items", 99)
    def search(source, *args):
        if source == "MiguMusicClient":
            assert stalled.wait(1)
            return [_FakeSong(9, source)]
        return [_FakeSong(i, source) for i in range(3)]
    def probe(url, headers):
        if url.endswith("/1.mp3"):
            stalled.set()
            assert release.wait(3)
        return True
    monkeypatch.setattr(app_module, "_search_one_source", search)
    monkeypatch.setattr(app_module, "_probe_playable_sync", probe)
    try:
        with TestClient(app_module.app) as client:
            data = client.get("/search", params={"keyword": "stalled", "sources": "kuwo,migu" if grace else "kuwo"}).json()
            ids = {item["id"] for item in data["items"]}
            assert "kuwo:0" in ids
            assert "kuwo:1" not in ids
            assert "partial" in data["errors"]["KuwoMusicClient"]
            assert len(app_module.SEARCH_CACHE) == 0
            cached_ids = set(app_module._SONG_CACHE)
            release.set()
            app_module.SOURCE_WORKERS.shutdown(wait=True)
            assert set(app_module._SONG_CACHE) == cached_ids
            assert {item["id"] for item in data["items"]} == ids
    finally:
        release.set()


@pytest.mark.parametrize("use_curl", [False, True])
def test_stream_rejects_encoded_origin_before_reading(monkeypatch, use_curl):
    import gzip
    import httpx
    payload = gzip.compress(b"audio" * 100)
    read = []
    closed = []
    requests = []
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            read.append(True)
            yield payload
        def close(self):
            closed.append(True)
    def handler(request):
        requests.append(dict(request.headers))
        return httpx.Response(200, headers={"Content-Encoding": "gzip", "Content-Length": str(len(payload))}, stream=Body())
    monkeypatch.setattr(app_module, "HAS_CURL_CFFI", use_curl)
    if use_curl:
        def get(url, **kwargs):
            requests.append({k.lower(): v for k, v in kwargs["headers"].items()})
            return types.SimpleNamespace(headers={"Content-Encoding": "gzip", "Content-Length": str(len(payload))}, close=lambda: closed.append(True))
        monkeypatch.setattr(app_module, "curl_requests", types.SimpleNamespace(get=get))
    else:
        orig_client = httpx.Client
        monkeypatch.setattr(httpx, "Client", lambda **kwargs: orig_client(transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(app_module.HTTPException) as exc:
        asyncio.run(app_module._fetch_upstream_stream("http://offline/audio", {"accept-encoding": "gzip", "Range": "bytes=0-9"}))
    assert exc.value.status_code == 502
    assert requests[0]["accept-encoding"] == "identity"
    assert requests[0]["range"] == "bytes=0-9"
    assert not read
    assert closed


def test_probe_playable_sync_unit(monkeypatch):
    """单元测试 _probe_playable_sync 校验各类 URL 与 HTTP 响应。"""
    assert app_module._probe_playable_sync("", {}) is False
    assert app_module._probe_playable_sync("ftp://test.com", {}) is False
    assert app_module._probe_playable_sync("http://test.com/404/error.html", {}) is False

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "audio.mp3" in url_str:
            return httpx.Response(200, headers={"Content-Type": "audio/mpeg", "Content-Length": "1000000"})
        if "html_error" in url_str:
            return httpx.Response(200, headers={"Content-Type": "text/html; charset=utf-8"}, text="<html>404 Not Found</html>")
        if "notfound" in url_str:
            return httpx.Response(404)
        return httpx.Response(500)

    monkeypatch.setattr(app_module, "HAS_CURL_CFFI", False)
    orig_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: orig_client(transport=httpx.MockTransport(handler)))

    assert app_module._probe_playable_sync("http://test.com/audio.mp3", {}) is True
    assert app_module._probe_playable_sync("http://test.com/html_error", {}) is False
    assert app_module._probe_playable_sync("http://test.com/notfound", {}) is False



# ------------------------------------------------------------------ 启动白名单来源 --

def test_startup_sources_env_file_overrides_stale_process_env(monkeypatch, tmp_path):
    """.env 有白名单时优先于进程环境变量（supervisord restart 继承的是容器启动时刻的陈旧值）。"""
    envf = tmp_path / ".env"
    envf.write_text("MUSICDL_SOURCES='kugou,netease'\n", encoding="utf-8")
    monkeypatch.setenv("FNMUSIC_ENV_FILE", str(envf))
    monkeypatch.setenv("MUSICDL_SOURCES", "KuwoMusicClient,MiguMusicClient")
    sources, origin = app_module._startup_sources()
    assert sources == ["kugou", "netease"]
    assert origin.startswith("env file")


def test_startup_sources_falls_back_to_env_var(monkeypatch, tmp_path):
    """.env 缺键、值留空或文件不存在时回退进程环境变量。"""
    envf = tmp_path / ".env"
    envf.write_text("MUSICDL_SOURCES=''\nOTHER=1\n", encoding="utf-8")
    monkeypatch.setenv("FNMUSIC_ENV_FILE", str(envf))
    monkeypatch.setenv("MUSICDL_SOURCES", "KuwoMusicClient")
    sources, origin = app_module._startup_sources()
    assert sources == ["KuwoMusicClient"]
    assert origin == "process env"

    monkeypatch.setenv("FNMUSIC_ENV_FILE", str(tmp_path / "absent.env"))
    sources, origin = app_module._startup_sources()
    assert sources == ["KuwoMusicClient"]
    assert origin == "process env"


def test_startup_sources_builtin_default(monkeypatch, tmp_path):
    """.env 不存在且环境变量未设时用内置默认（酷我 + 咪咕）。"""
    monkeypatch.setenv("FNMUSIC_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.delenv("MUSICDL_SOURCES", raising=False)
    sources, origin = app_module._startup_sources()
    assert sources == ["KuwoMusicClient", "MiguMusicClient"]
    assert origin == "builtin default"


def test_module_conf_sources_from_env_file_normalized(monkeypatch, tmp_path):
    """导入期端到端：CONF['sources'] 取 .env 白名单并完成短名归一（issue #31 回归）。"""
    builder = types.SimpleNamespace(REGISTERED_MODULES={
        "KuwoMusicClient": object, "MiguMusicClient": object,
        "KugouMusicClient": object, "NeteaseMusicClient": object,
    })
    src_mod = types.ModuleType("musicdl.modules.sources")
    src_mod.MusicClientBuilder = builder
    pkg_mod = types.ModuleType("musicdl.modules")
    pkg_mod.sources = src_mod
    _musicdl_stub.modules = pkg_mod
    sys.modules.setdefault("musicdl.modules", pkg_mod)
    sys.modules.setdefault("musicdl.modules.sources", src_mod)

    envf = tmp_path / ".env"
    envf.write_text("MUSICDL_SOURCES='kugou,netease'\n", encoding="utf-8")
    monkeypatch.setenv("FNMUSIC_ENV_FILE", str(envf))
    monkeypatch.setenv("MUSICDL_SOURCES", "KuwoMusicClient,MiguMusicClient")

    spec = importlib.util.spec_from_file_location("musicdl_service_app_envfile", _HERE / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.CONF["sources"] == ["KugouMusicClient", "NeteaseMusicClient"]
    assert mod._SOURCES_ORIGIN.startswith("env file")


# === 损坏流防护：无损头探针 + kuwo mp3 降级（2026-09-29 kuwo CDN 实测） ===

ffmpeg_path = shutil.which("ffmpeg")


@pytest.mark.skipif(ffmpeg_path is None, reason="宿主机无 ffmpeg")
def test_decode_probe_ok_judges_by_decoded_duration(tmp_path):
    """截断的完好样本解出数秒=好；开头就坏的样本 time≈0=坏。"""
    good = tmp_path / "good.flac"
    subprocess.run(
        [ffmpeg_path, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
         "-c:a", "flac", str(good)], check=True, timeout=60)
    # 截断到头部若干字节：仍是可解出前几秒的完好流
    data = good.read_bytes()
    trunc = tmp_path / "trunc.flac"
    trunc.write_bytes(data[: min(len(data) - 100, 160 * 1024)])
    assert app_module._decode_probe_ok(str(trunc)) is True
    # 头部完好、正文损坏（对齐 kuwo 坏流形态：合法 fLaC 头 + 垃圾正文）
    corrupt = tmp_path / "corrupt.flac"
    corrupt.write_bytes(data[:200] + b"\x00" * 100_000)
    assert app_module._decode_probe_ok(str(corrupt)) is False


@pytest.mark.anyio
async def test_maybe_downgrade_swaps_corrupt_lossless_to_mp3(clean_state, monkeypatch):
    entry = {"item": {"ext": "flac", "download_url": "http://src/flac"},
             "download_headers": {"User-Agent": "x"}}
    async def _probe_bad(url, headers, song_id):
        return False
    monkeypatch.setattr(app_module, "_head_probe", _probe_bad)
    monkeypatch.setattr(app_module, "_kuwo_force_mp3_url_sync",
                        lambda sid: ("http://src/mp3", {"User-Agent": "okhttp/3.10.0"}))
    url, headers = await app_module._maybe_downgrade(
        "kuwo:123", entry, "http://src/flac", "", None)
    assert url == "http://src/mp3"
    assert headers["User-Agent"] == "okhttp/3.10.0"


@pytest.mark.anyio
async def test_maybe_downgrade_keeps_clean_lossless(clean_state, monkeypatch):
    entry = {"item": {"ext": "flac"}, "download_headers": {"Referer": "r"}}
    async def _probe_ok(url, headers, song_id):
        return True
    monkeypatch.setattr(app_module, "_head_probe", _probe_ok)
    url, headers = await app_module._maybe_downgrade(
        "kuwo:123", entry, "http://src/flac", "", "bytes=0-99")
    assert url == "http://src/flac" and headers["Range"] == "bytes=0-99"


@pytest.mark.anyio
async def test_maybe_downgrade_quality_param_forces_mp3_without_probe(clean_state, monkeypatch):
    entry = {"item": {"ext": "flac"}, "download_headers": {}}
    async def _fail_probe(url, headers, song_id):
        raise AssertionError("quality=mp3 不应再探测无损档")
    monkeypatch.setattr(app_module, "_head_probe", _fail_probe)
    monkeypatch.setattr(app_module, "_kuwo_force_mp3_url_sync",
                        lambda sid: ("http://src/mp3", {"User-Agent": "okhttp/3.10.0"}))
    url, _ = await app_module._maybe_downgrade(
        "kuwo:123", entry, "http://src/flac", "mp3", None)
    assert url == "http://src/mp3"


def test_kuwo_force_mp3_url_extracts_from_official_api(monkeypatch):
    """convert_url2 应答文本中提取直链（stub 掉库的加密工具与 HTTP）。"""
    kuwo_stub = types.ModuleType("musicdl.modules.sources.kuwo")
    class _U:
        @staticmethod
        def encryptquery(q):
            return "ENCRYPTED"
    kuwo_stub.KuwoMusicClientUtils = _U
    monkeypatch.setitem(sys.modules, "musicdl.modules.sources.kuwo", kuwo_stub)
    monkeypatch.setattr(app_module, "HAS_CURL_CFFI", False)

    class _Resp:
        text = 'xxx\r\nhttp://kw-er.kuwo.cn/abc/mp3_320.mp3?sign=1\r\n000'
    class _Client:
        def __init__(self, timeout=None):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get(self, url, headers=None):
            assert "q=ENCRYPTED" in url
            return _Resp()
    monkeypatch.setattr(app_module.httpx, "Client", _Client)
    result = app_module._kuwo_force_mp3_url_sync("kuwo:456")
    assert result is not None
    assert result[0].startswith("http://kw-er.kuwo.cn/") and "mp3" in result[0]
    assert result[1]["User-Agent"] == "okhttp/3.10.0"
