"""搜索首屏空结果有界重试测试（issue #18 后续加固）。"""
import asyncio
import logging
import pytest
from fastapi.testclient import TestClient

from proxy.app import (
    app,
    CONF,
    _SEARCH_CACHE,
    _aggregate_search,
)


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    import proxy.app as p
    p.reset_source_search_gates()
    _SEARCH_CACHE.clear()
    monkeypatch.setitem(CONF, "search_debounce_s", 0.0)
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "lx_enabled", True)
    monkeypatch.setitem(CONF, "search_timeout", 5.0)
    monkeypatch.setitem(CONF, "search_empty_retry", 1)
    monkeypatch.setitem(CONF, "search_empty_retry_delay", 0.01)


class _DummyReq:
    app = app
    headers = {}
    query_params = {}
    url = type("URL", (), {"path": "/music/api/v1/search/track", "query": ""})()


@pytest.mark.anyio
async def test_all_sources_fail_then_retry_succeeds(monkeypatch, caplog):
    """全部源首轮 None、重试轮成功 → items 聚合成功、fetch 被调用两轮。"""
    call_counts = {"netease": 0, "musicdl": 0, "lx": 0}

    async def fake_mb(client, keyword, limit):
        call_counts["netease"] += 1
        if call_counts["netease"] == 1:
            return None
        return [{"id": "netease:100", "title": "晴天", "artist": "周杰伦"}]

    async def fake_mdl(client, keyword, limit, sources=None):
        call_counts["musicdl"] += 1
        if call_counts["musicdl"] == 1:
            return None
        return {"items": [{"id": "kuwo:200", "title": "晴天", "artist": "周杰伦"}]}

    async def fake_lx(client, keyword, limit, sources=None):
        call_counts["lx"] += 1
        if call_counts["lx"] == 1:
            return None
        return [{"id": "lx:kg:300", "title": "晴天", "artist": "周杰伦"}]

    monkeypatch.setattr("proxy.app.fetch_musicbox_search", fake_mb)
    monkeypatch.setattr("proxy.app.fetch_musicdl_search", fake_mdl)
    monkeypatch.setattr("proxy.app.fetch_lx_search", fake_lx)

    entry = {"items": [], "pages": {}, "cursor": 0, "ts": 0}
    with caplog.at_level(logging.INFO, logger="fnmusic_proxy"):
        await _aggregate_search(_DummyReq(), "晴天", entry)

    assert len(entry["items"]) > 0
    assert call_counts["netease"] == 2
    assert call_counts["musicdl"] == 2
    assert call_counts["lx"] == 2
    assert any("搜索首屏空结果触发重试" in record.message for record in caplog.records)
    assert any("搜索首屏重试结束" in record.message for record in caplog.records)


@pytest.mark.anyio
async def test_first_round_succeeds_no_retry(monkeypatch, caplog):
    """首轮成功 → 不发生重试（fetch 只调一轮）。"""
    call_counts = {"netease": 0, "musicdl": 0, "lx": 0}

    async def fake_mb(client, keyword, limit):
        call_counts["netease"] += 1
        return [{"id": "netease:100", "title": "晴天", "artist": "周杰伦"}]

    async def fake_mdl(client, keyword, limit, sources=None):
        call_counts["musicdl"] += 1
        return {"items": [{"id": "kuwo:200", "title": "晴天", "artist": "周杰伦"}]}

    async def fake_lx(client, keyword, limit, sources=None):
        call_counts["lx"] += 1
        return [{"id": "lx:kg:300", "title": "晴天", "artist": "周杰伦"}]

    monkeypatch.setattr("proxy.app.fetch_musicbox_search", fake_mb)
    monkeypatch.setattr("proxy.app.fetch_musicdl_search", fake_mdl)
    monkeypatch.setattr("proxy.app.fetch_lx_search", fake_lx)

    entry = {"items": [], "pages": {}, "cursor": 0, "ts": 0}
    with caplog.at_level(logging.INFO, logger="fnmusic_proxy"):
        await _aggregate_search(_DummyReq(), "晴天", entry)

    assert len(entry["items"]) > 0
    assert call_counts["netease"] == 1
    assert call_counts["musicdl"] == 1
    assert call_counts["lx"] == 1
    assert not any("搜索首屏空结果触发重试" in record.message for record in caplog.records)


@pytest.mark.anyio
async def test_retry_disabled_when_config_zero(monkeypatch, caplog):
    """FNMUSIC_SEARCH_EMPTY_RETRY=0 → 不重试。"""
    monkeypatch.setitem(CONF, "search_empty_retry", 0)
    call_counts = {"netease": 0, "musicdl": 0, "lx": 0}

    async def fake_mb(client, keyword, limit):
        call_counts["netease"] += 1
        return None

    async def fake_mdl(client, keyword, limit, sources=None):
        call_counts["musicdl"] += 1
        return None

    async def fake_lx(client, keyword, limit, sources=None):
        call_counts["lx"] += 1
        return None

    monkeypatch.setattr("proxy.app.fetch_musicbox_search", fake_mb)
    monkeypatch.setattr("proxy.app.fetch_musicdl_search", fake_mdl)
    monkeypatch.setattr("proxy.app.fetch_lx_search", fake_lx)

    entry = {"items": [], "pages": {}, "cursor": 0, "ts": 0}
    with caplog.at_level(logging.INFO, logger="fnmusic_proxy"):
        await _aggregate_search(_DummyReq(), "晴天", entry)

    assert len(entry["items"]) == 0
    assert entry["partial"] is True
    assert call_counts["netease"] == 1
    assert call_counts["musicdl"] == 1
    assert call_counts["lx"] == 1
    assert not any("搜索首屏空结果触发重试" in record.message for record in caplog.records)


@pytest.mark.anyio
async def test_partial_sources_succeed_only_failed_rebuilt(monkeypatch, caplog):
    """部分源成功部分失败 → 只重建失败源（成功源只调用一次，失败源重试）。"""
    call_counts = {"netease": 0, "musicdl": 0, "lx": 0}

    # netease 首轮成功
    async def fake_mb(client, keyword, limit):
        call_counts["netease"] += 1
        return [{"id": "netease:100", "title": "晴天", "artist": "周杰伦"}]

    # musicdl 首轮失败 None
    async def fake_mdl(client, keyword, limit, sources=None):
        call_counts["musicdl"] += 1
        return None

    # lx 首轮失败 None
    async def fake_lx(client, keyword, limit, sources=None):
        call_counts["lx"] += 1
        return None

    monkeypatch.setattr("proxy.app.fetch_musicbox_search", fake_mb)
    monkeypatch.setattr("proxy.app.fetch_musicdl_search", fake_mdl)
    monkeypatch.setattr("proxy.app.fetch_lx_search", fake_lx)

    entry = {"items": [], "pages": {}, "cursor": 0, "ts": 0}
    with caplog.at_level(logging.INFO, logger="fnmusic_proxy"):
        await _aggregate_search(_DummyReq(), "晴天", entry)

    # 因为 netease 首轮成功，entry["items"] 非空，不触发空结果重试
    assert len(entry["items"]) > 0
    assert call_counts["netease"] == 1
    assert call_counts["musicdl"] == 1
    assert call_counts["lx"] == 1
    assert not any("搜索首屏空结果触发重试" in record.message for record in caplog.records)


@pytest.mark.anyio
async def test_rebuild_only_hard_failed_sources_when_items_empty(monkeypatch, caplog):
    """部分源硬失败（None），部分源正常返回空列表（[]）导致整体 items 为空 → 只重建硬失败源。"""
    call_counts = {"netease": 0, "musicdl": 0, "lx": 0}

    # netease 正常返回空列表 []（非 None）
    async def fake_mb(client, keyword, limit):
        call_counts["netease"] += 1
        return []

    # musicdl 首轮硬失败 None，重试轮成功返回数据
    async def fake_mdl(client, keyword, limit, sources=None):
        call_counts["musicdl"] += 1
        if call_counts["musicdl"] == 1:
            return None
        return {"items": [{"id": "kuwo:200", "title": "晴天", "artist": "周杰伦"}]}

    # lx 首轮硬失败 None，重试轮依然 None
    async def fake_lx(client, keyword, limit, sources=None):
        call_counts["lx"] += 1
        return None

    monkeypatch.setattr("proxy.app.fetch_musicbox_search", fake_mb)
    monkeypatch.setattr("proxy.app.fetch_musicdl_search", fake_mdl)
    monkeypatch.setattr("proxy.app.fetch_lx_search", fake_lx)

    entry = {"items": [], "pages": {}, "cursor": 0, "ts": 0}
    with caplog.at_level(logging.INFO, logger="fnmusic_proxy"):
        await _aggregate_search(_DummyReq(), "晴天", entry)

    # 第一轮后 items 为空，netease 结果是 [] 不是 None，所以重试时只重建 musicdl 和 lx
    assert len(entry["items"]) > 0
    assert call_counts["netease"] == 1  # 成功返回（虽无数据）的不重试
    assert call_counts["musicdl"] == 2  # 硬失败重试并成功
    assert call_counts["lx"] == 2       # 硬失败重试
    assert any("搜索首屏空结果触发重试" in record.message for record in caplog.records)


@pytest.mark.anyio
async def test_deadline_respected(monkeypatch, caplog):
    """超时 deadline 约束生效，不突破总 deadline。"""
    monkeypatch.setitem(CONF, "search_timeout", 0.1)
    monkeypatch.setitem(CONF, "search_empty_retry", 2)
    monkeypatch.setitem(CONF, "search_empty_retry_delay", 0.5)

    async def fake_mb(client, keyword, limit):
        return None

    async def fake_mdl(client, keyword, limit, sources=None):
        return None

    async def fake_lx(client, keyword, limit, sources=None):
        return None

    monkeypatch.setattr("proxy.app.fetch_musicbox_search", fake_mb)
    monkeypatch.setattr("proxy.app.fetch_musicdl_search", fake_mdl)
    monkeypatch.setattr("proxy.app.fetch_lx_search", fake_lx)

    entry = {"items": [], "pages": {}, "cursor": 0, "ts": 0}
    t0 = asyncio.get_running_loop().time()
    await _aggregate_search(_DummyReq(), "晴天", entry)
    elapsed = asyncio.get_running_loop().time() - t0

    assert entry["partial"] is True
    assert len(entry["items"]) == 0
    assert elapsed < .3


@pytest.mark.anyio
async def test_retry_once_max_and_failure_not_cached(monkeypatch):
    monkeypatch.setitem(CONF, "search_empty_retry", 99)
    calls = []
    async def fail(*args):
        calls.append(args[1])
        return None
    for name in ("fetch_musicbox_search", "fetch_musicdl_search", "fetch_lx_search"):
        monkeypatch.setattr("proxy.app." + name, fail)
    entry = {"items": [], "ts": 0}
    await _aggregate_search(_DummyReq(), "same", entry)
    assert calls == ["same"] * 6
    assert entry["ts"] == 0 and entry["partial"] and entry["items"] == []


@pytest.mark.anyio
@pytest.mark.parametrize("change_keyword", [False, True])
async def test_cancel_or_keyword_change_during_retry_delay_does_not_retry(monkeypatch, change_keyword):
    import proxy.app as p
    monkeypatch.setitem(CONF, "search_empty_retry_delay", .1)
    called = asyncio.Event()
    calls = []
    async def fail(*args):
        calls.append(args[1])
        called.set()
        return None
    for name in ("fetch_musicbox_search", "fetch_musicdl_search", "fetch_lx_search"):
        monkeypatch.setattr("proxy.app." + name, fail)
    scope = p._credential_scope(_DummyReq())
    gen = p._note_user_keyword(scope, "old")
    entry = {"items": [], "ts": 0, "gen": gen, "credentials": scope}
    task = asyncio.create_task(_aggregate_search(_DummyReq(), "old", entry))
    await called.wait()
    await asyncio.sleep(.02)
    if change_keyword:
        p._note_user_keyword(scope, "new")
    else:
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert calls == ["old"] * 3
    assert entry["items"] == [] and entry["ts"] == 0


@pytest.mark.anyio
async def test_retry_uses_original_deadline(monkeypatch):
    monkeypatch.setitem(CONF, "search_timeout", .12)
    monkeypatch.setitem(CONF, "search_empty_retry_delay", .02)
    calls = []
    async def fail_then_stall(*args):
        calls.append(args[1])
        if len(calls) <= 3:
            await asyncio.sleep(.04)
            return None
        await asyncio.sleep(10)
    for name in ("fetch_musicbox_search", "fetch_musicdl_search", "fetch_lx_search"):
        monkeypatch.setattr("proxy.app." + name, fail_then_stall)
    entry = {"items": [], "ts": 0}
    started = asyncio.get_running_loop().time()
    await _aggregate_search(_DummyReq(), "same", entry)
    assert asyncio.get_running_loop().time() - started < .18
    assert len(calls) == 6 and entry["ts"] == 0


@pytest.mark.anyio
async def test_musicdl_fetch_does_not_retry_inside_gate():
    import httpx
    import proxy.app as p
    calls = []
    def fail(req):
        calls.append(req)
        return httpx.Response(503)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail), base_url="http://test") as client:
        assert await p.fetch_musicdl_search(client, "same", 20) is None
    assert len(calls) == 1
