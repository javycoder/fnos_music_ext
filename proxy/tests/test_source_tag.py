"""搜索结果在线来源标记：netease→[music box]、musicdl→[dl]、lx→逐曲 [脚本名-平台]
（如 [墨澜-kg]；脚本无法归属落 lx，平台未知回退激活源备注名或 [lx]）。

标记仅改下发显示的 title/name（搜索结果列表），本地条目与收藏/历史/元数据等
其余出口保持干净标题；播放事件上报入库前剥离标记，业务逻辑不受影响。
"""
import asyncio
import json
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import proxy.app as pa
from proxy.app import (
    CONF,
    _LX_SCRIPT_MAP_CACHE,
    _SEARCH_CACHE,
    app,
    apply_env_hot_reload,
    build_favorite_track_obj,
    build_metadata_payload,
    build_online_track,
    lx_platform_from_item,
    refresh_lx_script_map,
    source_display_prefix,
    strip_source_tag,
)


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path, monkeypatch):
    _SEARCH_CACHE.clear()
    monkeypatch.setenv("FNMUSIC_BACKGROUND_JOBS", "0")
    monkeypatch.setattr(pa, "_LX_SCRIPT_MAP_TASK", None)
    monkeypatch.setattr(pa, "_LX_DISPLAY_TAGS", {})
    _LX_SCRIPT_MAP_CACHE["ts"] = -1.0
    _LX_SCRIPT_MAP_CACHE["by_platform"] = {}
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(CONF, "library_dir", str(tmp_path / "library"))
    monkeypatch.setitem(CONF, "fav_dir", str(tmp_path / "online_favorites"))
    monkeypatch.setitem(CONF, "search_list_path", "data.list")
    monkeypatch.setitem(CONF, "online_limit", 30)
    monkeypatch.setitem(CONF, "netease_search_limit", 50)
    monkeypatch.setitem(CONF, "lx_search_limit", 50)
    monkeypatch.setitem(CONF, "lx_sources", [])
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "netease_enabled", False)
    monkeypatch.setitem(CONF, "lx_enabled", False)
    monkeypatch.setitem(CONF, "netease_wait_s", 3.0)
    monkeypatch.setitem(CONF, "merge_suggest", False)
    monkeypatch.setitem(CONF, "lyric_field", "data.lyric")
    monkeypatch.setitem(CONF, "search_cache_ttl", 604800.0)
    monkeypatch.setitem(CONF, "late_page_wait_s", 5.0)
    monkeypatch.setitem(CONF, "search_debounce_s", 0.0)
    monkeypatch.setitem(CONF, "search_deep_page", True)
    monkeypatch.setitem(CONF, "search_deep_max_pages", 10)
    monkeypatch.setitem(CONF, "lx_source_url", "")
    monkeypatch.setitem(CONF, "lx_source_list", "[]")

    def quiet_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"ok": False, "data": []})

    musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(quiet_handler), base_url="http://127.0.0.1:8770"
    )
    monkeypatch.setattr(app.state, "musicbox_client", musicbox_client, raising=False)
    yield
    asyncio.run(musicbox_client.aclose())
    _SEARCH_CACHE.clear()
    _LX_SCRIPT_MAP_CACHE.update(ts=-1.0, by_platform={})


def _set_lx_source(monkeypatch, url: str, entries: list[dict]):
    monkeypatch.setitem(CONF, "lx_source_url", url)
    monkeypatch.setitem(CONF, "lx_source_list", json.dumps(entries, ensure_ascii=False))


def test_source_display_prefix_mapping(monkeypatch):
    _set_lx_source(monkeypatch, "http://s1/one.js", [
        {"name": "星海源", "url": "http://s1/one.js"},
        {"name": "", "url": "http://s2/two.js"},
    ])
    assert source_display_prefix("netease") == "[music box] "
    assert source_display_prefix("lx") == "[星海源] "
    assert source_display_prefix("kuwo") == "[dl] "
    assert source_display_prefix("migu") == "[dl] "
    assert source_display_prefix("") == ""
    # 激活源在列表里但未备注 → [lx]
    monkeypatch.setitem(CONF, "lx_source_url", "http://s2/two.js")
    assert source_display_prefix("lx") == "[lx] "
    # 激活地址不在列表（.env 手工直配）→ [lx]
    monkeypatch.setitem(CONF, "lx_source_url", "http://elsewhere/x.js")
    assert source_display_prefix("lx") == "[lx] "


def test_source_display_prefix_multi_active(monkeypatch):
    """多源同时激活：列表 active 标记决定备注；≥2 个激活时无法归属单曲，回退 [lx]。"""
    # 恰好 1 个 active → 备注名
    _set_lx_source(monkeypatch, "", [
        {"name": "星海源", "url": "http://s1/one.js", "active": True},
        {"name": "云海源", "url": "http://s2/two.js", "active": False},
    ])
    assert source_display_prefix("lx") == "[星海源] "
    # 2 个 active → [lx]
    _set_lx_source(monkeypatch, "", [
        {"name": "星海源", "url": "http://s1/one.js", "active": True},
        {"name": "云海源", "url": "http://s2/two.js", "active": True},
    ])
    assert source_display_prefix("lx") == "[lx] "
    # active 项无备注 → [lx]
    _set_lx_source(monkeypatch, "", [
        {"name": "", "url": "http://s1/one.js", "active": True},
    ])
    assert source_display_prefix("lx") == "[lx] "


def test_source_display_prefix_lx_script_platform(monkeypatch):
    """lx 条目逐曲 [脚本名-平台]：条目 lx_script 优先 → 映射缓存 → 单激活备注 → lx。"""
    # 多源同时激活（旧逻辑回退 [lx] 的场景）
    _set_lx_source(monkeypatch, "", [
        {"name": "星海源", "url": "http://s1/one.js", "active": True},
        {"name": "云海源", "url": "http://s2/two.js", "active": True},
    ])
    # 条目注记了归属脚本 → [脚本名-平台]
    assert source_display_prefix("lx", {"id": "lx:kg:1", "lx_source": "kg", "lx_script": "星海源"}) == "[星海源-kg] "
    assert source_display_prefix("lx", {"id": "lx:kw:2", "lx_source": "kw", "lx_script": "云海源"}) == "[云海源-kw] "
    # 无注记 + 映射缓存新鲜 → 用缓存；缓存无该平台（多源无备注）→ 脚本名落 lx
    _LX_SCRIPT_MAP_CACHE["ts"] = time.monotonic()
    _LX_SCRIPT_MAP_CACHE["by_platform"] = {"kg": "星海源"}
    assert source_display_prefix("lx", {"id": "lx:kg:1", "lx_source": "kg"}) == "[星海源-kg] "
    assert source_display_prefix("lx", {"id": "lx:tx:3", "lx_source": "tx"}) == "[lx-tx] "
    # 平台未知 → 旧回退链（多源无备注 → [lx]）
    assert source_display_prefix("lx", {"id": "lx:xx:1", "lx_source": "xx"}) == "[lx] "
    # lx_source 缺失 → 回退解析 id 第 3 段（含 guid 形态）
    assert source_display_prefix("lx", {"id": "lx:wy:9", "lx_script": "星海源"}) == "[星海源-wy] "
    assert source_display_prefix("lx", {"id": "online:lx:mg:7", "lx_script": "星海源"}) == "[星海源-mg] "
    # 无缓存时单激活备注可作为脚本名兜底
    _LX_SCRIPT_MAP_CACHE["ts"] = -1.0
    _LX_SCRIPT_MAP_CACHE["by_platform"] = {}
    _set_lx_source(monkeypatch, "", [
        {"name": "星海源", "url": "http://s1/one.js", "active": True},
    ])
    assert source_display_prefix("lx", {"id": "lx:kg:1", "lx_source": "kg"}) == "[星海源-kg] "
    # 非 lx 条目不受 item 影响
    assert source_display_prefix("netease", {"id": "lx:kw:1"}) == "[music box] "
    assert lx_platform_from_item(None) == ""
    assert lx_platform_from_item({"id": "migu:1"}) == ""


def test_refresh_lx_script_map_attribution(monkeypatch):
    """映射归属规则：单源全归属；多源按声明平台唯一归属；接口不可用回退单激活备注。"""

    def _client(handler):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://lx")

    def _desc(sources):
        return httpx.Response(200, json={"ok": True, "data": {"sources": sources, "active_count": len(sources)}})

    _set_lx_source(monkeypatch, "", [
        {"name": "星海源", "url": "http://s1/one.js", "active": True},
        {"name": "云海源", "url": "http://s2/two.js", "active": True},
    ])

    # 单激活源 → 全平台归属，且备注名优先于脚本自带名
    _LX_SCRIPT_MAP_CACHE["ts"] = -1.0
    m = asyncio.run(refresh_lx_script_map(_client(lambda r: _desc([
        {"name": "六音", "url": "http://s1/one.js", "platforms": ["kw", "kg"]},
    ]))))
    assert m["kg"] == "星海源" and m["wy"] == "星海源"

    # 多源、声明平台不相交 → 各自归属；两家都未声明的平台不归属
    _LX_SCRIPT_MAP_CACHE["ts"] = -1.0
    m = asyncio.run(refresh_lx_script_map(_client(lambda r: _desc([
        {"name": "星海", "url": "http://s1/one.js", "platforms": ["kg", "kw"]},
        {"name": "云海", "url": "http://s2/two.js", "platforms": ["wy", "mg"]},
    ]))))
    assert m == {"kg": "星海源", "kw": "星海源", "wy": "云海源", "mg": "云海源"}

    # 多源、同一平台两家都声明 → 该平台无法归属
    _LX_SCRIPT_MAP_CACHE["ts"] = -1.0
    m = asyncio.run(refresh_lx_script_map(_client(lambda r: _desc([
        {"name": "星海", "url": "http://s1/one.js", "platforms": ["kg"]},
        {"name": "云海", "url": "http://s2/two.js", "platforms": ["kg", "wy"]},
    ]))))
    assert m == {"wy": "云海源"}

    # 描述接口不可用 → 回退 LX_SOURCE_LIST：多源无备注空映射；单激活备注全归属
    _LX_SCRIPT_MAP_CACHE["ts"] = -1.0
    m = asyncio.run(refresh_lx_script_map(_client(lambda r: httpx.Response(404))))
    assert m == {}
    _set_lx_source(monkeypatch, "", [{"name": "星海源", "url": "http://s1/one.js", "active": True}])
    _LX_SCRIPT_MAP_CACHE["ts"] = -1.0
    m = asyncio.run(refresh_lx_script_map(_client(lambda r: httpx.Response(404))))
    assert m["kg"] == "星海源"

    # TTL 内直接复用缓存，不再请求描述接口
    _LX_SCRIPT_MAP_CACHE["ts"] = -1.0
    called = {"n": 0}

    def counting_handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return _desc([])

    asyncio.run(refresh_lx_script_map(_client(counting_handler)))
    assert called["n"] == 1
    asyncio.run(refresh_lx_script_map(_client(counting_handler)))
    assert called["n"] == 1


@pytest.mark.parametrize("name_field", ["descriptor", "remark"])
def test_same_name_distinct_sources_are_ambiguous(monkeypatch, name_field):
    """Distinct owners must not collapse to one merely because labels match."""
    entries = [
        {"url": "http://s1/one.js", "name": "同名" if name_field == "remark" else "", "active": True},
        {"url": "http://s2/two.js", "name": "同名" if name_field == "remark" else "", "active": True},
    ]
    _set_lx_source(monkeypatch, "", entries)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
            "data": {"sources": [
                {"url": entries[0]["url"], "name": "同名", "platforms": ["kg", "kw"]},
                {"url": entries[1]["url"], "name": "同名", "platforms": ["kg", "wy"]},
            ]},
        })), base_url="http://lx") as client:
            assert await refresh_lx_script_map(client) == {"kw": "同名", "wy": "同名"}
            assert source_display_prefix("lx", {"id": "lx:kg:1"}) == "[lx-kg] "
    asyncio.run(run())


@pytest.mark.parametrize("platforms", [17, "kg", {"kg": True}, [[], {}, "kg", None]])
def test_malformed_descriptor_platforms_do_not_fail_background_refresh(monkeypatch, platforms):
    async def run():
        errors = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda loop, context: errors.append(context))
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
            "data": {"sources": [{"name": "实际脚本", "platforms": platforms}]},
        })), base_url="http://lx") as client:
            pa._schedule_lx_script_map_refresh(client)
            task = pa._LX_SCRIPT_MAP_TASK
            assert task is not None
            assert await task == dict.fromkeys(pa._LX_PLATFORMS, "实际脚本")
            await asyncio.sleep(0)
            assert pa._LX_SCRIPT_MAP_TASK is None
            assert not errors
    asyncio.run(run())


def test_unnamed_source_still_counts_as_platform_owner():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
            "data": {"sources": [
                {"name": "有名", "platforms": ["kg", "kw"]},
                {"name": "", "platforms": ["kg"]},
            ]},
        })), base_url="http://lx") as client:
            assert await refresh_lx_script_map(client) == {"kw": "有名"}
    asyncio.run(run())


def test_refresh_exception_is_consumed_and_retryable(monkeypatch, caplog):
    caplog.set_level("DEBUG", logger=pa.logger.name)

    async def run():
        errors = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda loop, context: errors.append(context))
        finished = asyncio.Event()
        calls = []

        async def broken(client):
            calls.append(client)
            raise RuntimeError("descriptor failure")

        monkeypatch.setattr(pa, "refresh_lx_script_map", broken)
        pa._schedule_lx_script_map_refresh(None)
        task = pa._LX_SCRIPT_MAP_TASK
        task.add_done_callback(lambda task: finished.set())
        await asyncio.wait_for(finished.wait(), 1)
        assert pa._LX_SCRIPT_MAP_TASK is None
        assert "lx script map refresh failed: RuntimeError" in caplog.text
        pa._schedule_lx_script_map_refresh(None)
        retry = pa._LX_SCRIPT_MAP_TASK
        assert retry is not task
        finished.clear()
        retry.add_done_callback(lambda task: finished.set())
        await asyncio.wait_for(finished.wait(), 1)
        assert len(calls) == 2
        # Drop references so an unconsumed failure would reach the loop error handler.
        del task, retry
        import gc
        gc.collect()
        await asyncio.sleep(0)
        assert not errors
    asyncio.run(run())


def test_refresh_task_from_foreign_loop_does_not_block_new_loop(monkeypatch):
    old_loop = asyncio.new_event_loop()
    old_task = old_loop.create_task(asyncio.sleep(3600))
    monkeypatch.setattr(pa, "_LX_SCRIPT_MAP_TASK", old_task)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)),
                                    base_url="http://lx") as client:
            pa._schedule_lx_script_map_refresh(client)
            assert pa._LX_SCRIPT_MAP_TASK is not old_task
            await pa._LX_SCRIPT_MAP_TASK
            await asyncio.sleep(0)
            assert pa._LX_SCRIPT_MAP_TASK is None

    try:
        # Even a dormant task belonging to another loop must not suppress this refresh.
        asyncio.run(run())
    finally:
        old_task.cancel()
        old_loop.run_until_complete(asyncio.gather(old_task, return_exceptions=True))
        old_loop.close()


@pytest.mark.parametrize("descriptor", [None, [], {"data": []}, {"data": {"sources": 1}},
                                       {"data": {"sources": [None, "not a source"]}}])
def test_invalid_descriptor_envelope_is_cached_without_unhandled_errors(monkeypatch, descriptor):
    async def run():
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=descriptor)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://lx") as client:
            pa._schedule_lx_script_map_refresh(client)
            await pa._LX_SCRIPT_MAP_TASK
            await asyncio.sleep(0)
            assert pa._LX_SCRIPT_MAP_TASK is None
            assert _LX_SCRIPT_MAP_CACHE["by_platform"] == {}
            assert _LX_SCRIPT_MAP_CACHE["ts"] >= 0
            pa._schedule_lx_script_map_refresh(client)
            assert pa._LX_SCRIPT_MAP_TASK is None
            assert len(calls) == 1
    asyncio.run(run())


def test_cold_cache_refreshes_at_low_monotonic_uptime(monkeypatch):
    # ts=-1 is invalid, not a freshly populated cache during the first minute of boot.
    monkeypatch.setattr(pa, "time", type("Clock", (), {"monotonic": staticmethod(lambda: 2.0)}))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
            "data": {"sources": [{"name": "开机脚本"}]},
        })), base_url="http://lx") as client:
            pa._schedule_lx_script_map_refresh(client)
            assert pa._LX_SCRIPT_MAP_TASK is not None
            assert await pa._LX_SCRIPT_MAP_TASK == dict.fromkeys(pa._LX_PLATFORMS, "开机脚本")
            await asyncio.sleep(0)
            assert pa._LX_SCRIPT_MAP_TASK is None
    asyncio.run(run())


def test_strip_source_tag_precise(monkeypatch):
    _set_lx_source(monkeypatch, "http://s1/one.js", [{"name": "星海源", "url": "http://s1/one.js"}])
    assert strip_source_tag("[music box] 晴天") == "晴天"
    assert strip_source_tag("[dl] 晴天") == "晴天"
    assert strip_source_tag("[lx] 晴天") == "晴天"
    # [脚本名-平台] 全部变体可剥离（列表备注名与兜底 lx × 全部平台码）
    assert strip_source_tag("[lx-kg] 晴天") == "晴天"
    assert strip_source_tag("[星海源-kw] 夜曲") == "夜曲"
    # 列表内全部备注名都可剥离（含未激活源，历史标记切换源后仍可还原）
    assert strip_source_tag("[星海源] 夜曲") == "夜曲"
    # 未知方括号开头的内容不是来源标记，不剥离
    assert strip_source_tag("[AI生成] 晴天") == "[AI生成] 晴天"
    assert strip_source_tag("晴天") == "晴天"
    assert strip_source_tag("") == ""


def test_build_online_track_mark_source_only_title():
    item = {
        "id": "netease:123",
        "source": "netease",
        "title": "晴天",
        "artist": "周杰伦",
        "album": "叶惠美",
        "duration_s": 269,
        "ext": "flac",
    }
    clean = build_online_track(item)
    assert clean["title"] == "晴天"
    assert clean["name"] == "晴天"

    marked = build_online_track(item, mark_source=True)
    assert marked["title"] == "[music box] 晴天"
    assert marked["name"] == "[music box] 晴天"
    # 标记只加在歌名前，歌手/专辑不动
    assert marked["artist"] == "周杰伦"
    assert marked["album"]["name"] == "叶惠美"

    # 空标题不加标记
    empty = build_online_track(
        {"id": "migu:1", "source": "migu", "title": "", "artist": "x"}, mark_source=True
    )
    assert empty["title"] == ""


def test_search_merge_marks_online_not_local():
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "code": 0,
            "msg": "OK",
            "data": {
                "list": [{
                    "guid": "local:101",
                    "title": "夜曲",
                    "artist": "周杰伦",
                    "album": "十一月的萧邦",
                    "duration": 226000,
                }],
                "total": 1,
            },
        })

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": [{
            "id": "migu:600902",
            "source": "migu",
            "title": "晴天",
            "artist": "周杰伦",
            "album": "叶惠美",
            "duration_s": 269,
            "ext": "mp3",
            "file_size": 4096000,
        }]})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?keyword=晴天")
        lst = resp.json()["data"]["list"]
    titles = [it["title"] for it in lst]
    assert titles[0] == "夜曲"
    assert "[dl] 晴天" in titles
    # 同名本地条目存在时在线去重（按干净标题），不会出现带标记的重复
    assert titles.count("晴天") == 0


def test_search_merge_lx_script_platform_and_fallback(monkeypatch):
    monkeypatch.setitem(CONF, "musicdl_enabled", False)
    monkeypatch.setitem(CONF, "lx_enabled", True)
    _set_lx_source(monkeypatch, "", [
        {"name": "星海源", "url": "http://s1/one.js", "active": True},
        {"name": "云海源", "url": "http://s2/two.js", "active": True},
    ])

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": "OK", "data": {"list": [], "total": 0}})

    def lx_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/source":
            # 多源激活：星海源只声明 kg，云海源只声明 wy → kg 条目可唯一归属星海源
            return httpx.Response(200, json={"ok": True, "data": {"active_count": 2, "sources": [
                {"name": "星海", "url": "http://s1/one.js", "platforms": ["kg"]},
                {"name": "云海", "url": "http://s2/two.js", "platforms": ["wy"]},
            ]}})
        return httpx.Response(200, json={"ok": True, "items": [{
            "id": "lx:kg:ABC123",
            "lx_source": "kg",
            "title": "晴天",
            "artist": "周杰伦",
            "album": "叶惠美",
            "duration_s": 269,
            "ext": "flac",
            "file_size": 28000000,
        }]})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lx_handler), base_url="http://127.0.0.1:8772"
    )
    # 标签刷新不阻塞搜索；精确标签用例预填已有描述映射，冷缓存另用事件控制测试。
    _LX_SCRIPT_MAP_CACHE.update(ts=time.monotonic(), by_platform={"kg": "星海源", "wy": "云海源"})
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?keyword=晴天")
        titles = [it["title"] for it in resp.json()["data"]["list"]]
        # 平台已知且唯一归属星海源 → [星海源-kg]
        assert titles == ["[星海源-kg] 晴天"]

        # 平台未知 + 无备注：清缓存重搜 → [lx] 兜底
        def lx_handler_unknown(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/source":
                return httpx.Response(404)
            return httpx.Response(200, json={"ok": True, "items": [{
                "id": "lx::ABC",
                "title": "夜曲",
                "artist": "周杰伦",
                "album": "十一月的萧邦",
                "duration_s": 226,
                "ext": "flac",
                "file_size": 26000000,
            }]})

        app.state.lx_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lx_handler_unknown), base_url="http://127.0.0.1:8772"
        )
        _SEARCH_CACHE.clear()
        _LX_SCRIPT_MAP_CACHE["ts"] = -1.0
        _LX_SCRIPT_MAP_CACHE["by_platform"] = {}
        monkeypatch.setitem(CONF, "lx_source_url", "")
        monkeypatch.setitem(CONF, "lx_source_list", "[]")
        resp = client.get("/music/api/v1/search/track?keyword=晴天")
        titles = [it["title"] for it in resp.json()["data"]["list"]]
        assert titles == ["[lx] 夜曲"]


@pytest.mark.parametrize("refresh_after_display", [False, True])
def test_event_report_strips_source_tag(tmp_path, monkeypatch, refresh_after_display):
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "ph"))
    _set_lx_source(monkeypatch, "http://s1/one.js", [{"name": "星海源", "url": "http://s1/one.js"}])

    def auth_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": "user-tag-1"}})
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": None})

    # Actual descriptor name is absent from LX_SOURCE_LIST (no user remark).
    async def load_descriptor(name):
        _LX_SCRIPT_MAP_CACHE["ts"] = -1.0
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
            "data": {"sources": [{"name": name, "url": "http://unnamed/source.js", "platforms": ["kg"]}]},
        })), base_url="http://lx") as lx:
            await refresh_lx_script_map(lx)

    asyncio.run(load_descriptor("脚本自带名"))
    actual_title = build_online_track({
        "id": "lx:kg:actual", "source": "lx", "title": "原名", "artist": "歌手",
    }, mark_source=True)["title"]
    assert actual_title == "[脚本自带名-kg] 原名"
    if refresh_after_display:
        asyncio.run(load_descriptor("新脚本名"))
        assert "脚本自带名" not in _LX_SCRIPT_MAP_CACHE["by_platform"].values()

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(auth_handler), base_url="http://unix"
    )
    with TestClient(app) as client:
        resp = client.post("/music/api/v1/event/report", json={"events": [
            {"eventType": "track_play", "occurredAt": 1,
             "payload": {"trackGUID": "online:migu:1", "title": "[dl] 晴天", "artist": "周杰伦"}},
            {"eventType": "track_play", "occurredAt": 2,
             "payload": {"trackGUID": "online:lx:kg:9", "title": "[星海源] 夜曲", "artist": "周杰伦"}},
            {"eventType": "track_play", "occurredAt": 3,
             "payload": {"trackGUID": "online:netease:5", "title": "[music box] 七里香", "artist": "周杰伦"}},
            {"eventType": "track_play", "occurredAt": 4,
             "payload": {"trackGUID": "online:lx:kw:3", "title": "[星海源-kw] 搁浅", "artist": "周杰伦"}},
            {"eventType": "track_play", "occurredAt": 5,
             "payload": {"trackGUID": "online:lx:mg:4", "title": "[lx-mg] 七里香（Live）", "artist": "周杰伦"}},
            {"eventType": "track_play", "occurredAt": 6,
             "payload": {"trackGUID": "online:lx:kg:actual", "title": actual_title, "artist": "歌手"}},
            {"eventType": "track_play", "occurredAt": 7,
             "payload": {"trackGUID": "online:lx:kg:unknown", "title": "[AI生成-kg] 原名", "artist": "歌手"}},
        ]})
        assert resp.json()["code"] == 0

    from proxy import recommend as dailyrec

    items = dailyrec.load_online_play_history("user-tag-1")
    by_guid = {it["guid"]: it for it in items}
    assert by_guid["online:migu:1"]["track"]["title"] == "晴天"
    assert by_guid["online:lx:kg:9"]["track"]["title"] == "夜曲"
    assert by_guid["online:netease:5"]["track"]["title"] == "七里香"
    assert by_guid["online:lx:kw:3"]["track"]["title"] == "搁浅"
    assert by_guid["online:lx:mg:4"]["track"]["title"] == "七里香（Live）"
    assert by_guid["online:lx:kg:actual"]["track"]["title"] == "原名"
    assert by_guid["online:lx:kg:unknown"]["track"]["title"] == "[AI生成-kg] 原名"


@pytest.mark.parametrize("warm_cache", [False, True])
def test_slow_descriptor_preserves_aggregate_and_refreshes_cached_labels(monkeypatch, warm_cache):
    monkeypatch.setitem(CONF, "lx_enabled", True)
    monkeypatch.setitem(CONF, "search_timeout", 0.05)
    monkeypatch.setitem(CONF, "search_deep_page", False)
    if warm_cache:
        _LX_SCRIPT_MAP_CACHE.update(ts=time.monotonic() - pa._LX_SCRIPT_MAP_TTL - 1,
                                    by_platform={"kg": "旧脚本"})

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        descriptor_calls = []
        search_calls = []

        async def lx_handler(request):
            if request.url.path == "/api/v1/source":
                descriptor_calls.append(request)
                entered.set()
                await release.wait()
                return httpx.Response(200, json={"data": {"sources": [
                    {"name": "实际脚本", "url": "http://s1/one.js", "platforms": ["kg"]},
                ]}})
            search_calls.append(request)
            return httpx.Response(200, json={"ok": True, "items": [{
                "id": "lx:kg:slow", "lx_source": "kg", "title": "晴天", "artist": "周杰伦",
                "duration_s": 269, "ext": "flac", "file_size": 28000000,
            }]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(lx_handler), base_url="http://lx") as lx, \
                httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
                    "code": 0, "data": {"list": [], "total": 0},
                })), base_url="http://unix") as upstream, \
                httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
                    "ok": True, "items": [{"id": "migu:1", "source": "migu", "title": "夜曲", "artist": "周杰伦"}],
                })), base_url="http://dl") as dl, \
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as api:
            monkeypatch.setattr(app.state, "lx_client", lx, raising=False)
            monkeypatch.setattr(app.state, "upstream_client", upstream, raising=False)
            monkeypatch.setattr(app.state, "musicdl_client", dl, raising=False)
            async with pa.lifespan(app):
                response = await asyncio.wait_for(api.get("/music/api/v1/search/track?keyword=晴天"), 1)
                await asyncio.wait_for(entered.wait(), 1)
                task = pa._LX_SCRIPT_MAP_TASK
                assert task is not None and not task.done()
                assert not release.is_set()
                entry = next(iter(_SEARCH_CACHE.values()))
                await asyncio.wait_for(entry["task"], 1)
                assert {item["source"] for item in entry["items"]} == {"lx", "migu"}
                assert entry["partial"] is False
                assert not entry.get("abandoned")
                assert all("lx_script" not in item for item in entry["items"])
                expected_lx = "[旧脚本-kg] 晴天" if warm_cache else "[lx-kg] 晴天"
                assert response.status_code == 200
                assert expected_lx in [item["title"] for item in response.json()["data"]["list"]]

                # Repeated requests/scheduling while the descriptor is blocked are single-flight.
                repeat_items = await pa._lx_search_request(lx, "晴天", 30, None, "")
                assert repeat_items and "lx_script" not in repeat_items[0]
                for _ in range(10):
                    pa._schedule_lx_script_map_refresh(lx)
                assert pa._LX_SCRIPT_MAP_TASK is task
                assert len(descriptor_calls) == 1
                release.set()
                await asyncio.wait_for(task, 1)
                await asyncio.sleep(0)
                assert pa._LX_SCRIPT_MAP_TASK is None
                assert _LX_SCRIPT_MAP_CACHE["by_platform"]["kg"] == "实际脚本"

                # Same cached search, no reaggregation: only its display label changes.
                count = len(search_calls)
                response = await api.get("/music/api/v1/search/track?keyword=晴天")
                titles = [item["title"] for item in response.json()["data"]["list"]]
                assert set(titles) == {"[实际脚本-kg] 晴天", "[dl] 夜曲"}
                assert len(search_calls) == count
                assert all("lx_script" not in item for item in entry["items"])
                pa._schedule_lx_script_map_refresh(lx)
                assert pa._LX_SCRIPT_MAP_TASK is None  # fresh TTL suppresses refresh
                assert len(descriptor_calls) == 1
    asyncio.run(run())


def test_lifespan_cancels_pending_descriptor(monkeypatch):
    async def run():
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def handler(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://lx") as lx:
            async with pa.lifespan(app):
                pa._schedule_lx_script_map_refresh(lx)
                task = pa._LX_SCRIPT_MAP_TASK
                await asyncio.wait_for(entered.wait(), 1)
            assert cancelled.is_set()
            assert task.cancelled()
            assert pa._LX_SCRIPT_MAP_TASK is None
    asyncio.run(run())


def test_lx_display_tags_are_bounded_and_only_track_displayed_labels():
    _LX_SCRIPT_MAP_CACHE.update(ts=time.monotonic(), by_platform={"kg": "当前映射"})
    assert strip_source_tag("[当前映射-kg] 歌名") == "歌名"
    build_online_track({"id": "lx:kg:clean", "source": "lx", "title": "未标记", "lx_script": "未显示"})
    assert "[未显示-kg] " not in pa._LX_DISPLAY_TAGS
    for i in range(4097):
        build_online_track({"id": f"lx:kg:{i}", "source": "lx", "title": "歌名", "lx_script": f"脚本{i}"},
                           mark_source=True)
    assert len(pa._LX_DISPLAY_TAGS) == 4096
    assert "[脚本0-kg] " not in pa._LX_DISPLAY_TAGS
    _LX_SCRIPT_MAP_CACHE["by_platform"] = {}
    assert strip_source_tag("[脚本4096-kg] 歌名") == "歌名"
    assert strip_source_tag("[未知-kg] 歌名") == "[未知-kg] 歌名"


def test_env_hot_reload_lx_source(tmp_path, monkeypatch):
    # 预登记环境变量，让 monkeypatch 在收尾时恢复原值（apply_env_hot_reload 会直写 os.environ）
    monkeypatch.setenv("LX_SOURCE_URL", "")
    monkeypatch.setenv("LX_SOURCE_LIST", "[]")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LX_SOURCE_URL=http://s1/one.js\n"
        "LX_SOURCE_LIST='[{\"name\": \"星海源\", \"url\": \"http://s1/one.js\"}]'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pa, "CONF", dict(CONF))
    apply_env_hot_reload(str(env_file))
    assert pa.CONF["lx_source_url"] == "http://s1/one.js"
    assert json.loads(pa.CONF["lx_source_list"])[0]["name"] == "星海源"
    assert source_display_prefix("lx") == "[星海源] "
    assert strip_source_tag("[星海源] 晴天") == "晴天"


def test_favorite_and_metadata_vo_not_marked():
    guid = "online:netease:123"
    info = {"title": "晴天", "artist": "周杰伦", "album": "叶惠美", "duration_s": 269}
    fav = build_favorite_track_obj(guid, info=info)
    assert fav["title"] == "晴天"

    meta = build_metadata_payload(guid, info)
    assert meta["data"]["track"]["title"] == "晴天"
