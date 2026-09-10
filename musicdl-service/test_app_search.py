"""musicdl-service /search 端点的超时与容错行为测试。

宿主环境未安装 musicdl 包（服务跑在 Docker 里），此处用 stub 顶替，
并通过 monkeypatch 替换单源搜索函数来模拟快源/慢源/熔断源。
"""
import asyncio
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


def test_default_budget_limit30_keeps_healthy_serial_results(clean_state, monkeypatch):
    # Virtual clock: 0.5-second probes in the default 12-second budget.
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(app_module, "_search_one_source", lambda *args: [
        _FakeSong(i, "KuwoMusicClient") for i in range(60)])
    def probe(*args):
        now[0] += 0.5
        return True
    monkeypatch.setattr(app_module, "_probe_playable_sync", probe)
    progress = app_module.SearchProgress(30, 12.0)
    entries = app_module._search_playable("KuwoMusicClient", "budget", 60, 30,
                                        deadline=11.9, progress=progress)
    assert len(entries) == 23
    assert progress.finish() == entries
    assert progress.partial
    assert now[0] < 12
    assert not app_module._SONG_CACHE


def test_serial_probes_return_partial_before_budget(clean_state, monkeypatch):
    monkeypatch.setattr(app_module, "ADAPTIVE", AdaptiveTimeout(0.08, 0.08))
    songs = [_FakeSong(i, "KuwoMusicClient") for i in range(5)]
    monkeypatch.setattr(app_module, "_search_one_source", lambda *args: songs)
    def probe(*args):
        time.sleep(0.03)
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

