"""保存与完整测试分离：执行真实配置入口，使用离线 HTTP 模拟慢脚本。"""
import asyncio
import json
import time
import threading
import shlex
import sys
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from test_webui import (
    authed_client, env_file, lx_ready_mock, svctl, webui, _mock_http,
)


def configure(env_file, active=("a",)):
    items = [{"url": f"https://s/{sid}.js", "id": sid, "name": sid,
              "active": sid in active} for sid in ("a", "b", "c")]
    env_file.write_text(
        "FNMUSIC_LX_ENABLED=true\nFNMUSIC_NETEASE_ENABLED=false\n"
        f"LX_SOURCE_LIST='{json.dumps(items)}'\n"
        f"LX_SOURCE_URL='https://s/{active[0]}.js'\n"
        "FNMUSIC_LX_SOURCE_ORDER='[\"a\"]'\n"
    )
    return items


def runtime(sid, *, status="success"):
    return {"id": sid, "url": f"https://s/{sid}.js", "name": sid,
            "enabled": True, "status": status, "platforms": ["kw"]}


def test_save_skips_full_verify_and_unchanged_ready_sources(env_file, svctl):
    items = configure(env_file)
    items[1]["active"] = True
    calls = []
    sources = [runtime("a")]

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json={"ok": True, "data": {"sources": sources}})
        body = json.loads(req.content)
        calls.append((req.url.path, body))
        assert req.url.path == "/api/v1/source"
        assert body["url"] == "https://s/b.js"
        sources.append(runtime("b"))
        return httpx.Response(200, json={
            "ok": True, "source_id": "b", "data": {"sources": sources}})

    _mock_http(handler)
    with authed_client() as client:
        response = client.put("/api/config", json={"values": {"LX_SOURCE_LIST": json.dumps(items)}})
    assert response.status_code == 200
    assert response.json()["complete"] is True
    assert len(calls) == 1
    assert json.loads(webui.read_env()["LX_SOURCE_LIST"])[1]["id"] == "b"


def test_cancel_is_committed_even_when_new_source_fails(env_file, svctl):
    items = configure(env_file, ("a", "b"))
    items[1]["active"], items[2]["active"] = False, True
    calls = []
    sources = [runtime("a"), runtime("b")]

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json={"ok": True, "data": {"sources": sources}})
        body = json.loads(req.content)
        calls.append(body)
        if body.get("enabled") is False:
            sources[:] = [s for s in sources if s["id"] != "b"]
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(400, json={"ok": False, "category": "runtime", "error": "初始化失败"})

    _mock_http(handler)
    with authed_client() as client:
        response = client.put("/api/config", json={"values": {"LX_SOURCE_LIST": json.dumps(items)}})
    assert response.status_code == 200
    assert calls[0]["enabled"] is False
    assert [i["active"] for i in json.loads(webui.read_env()["LX_SOURCE_LIST"])] == [True, False, False]
    assert response.json()["complete"] is False


def test_slow_sources_share_one_budget_and_leave_unstarted_items_inactive(env_file, svctl, monkeypatch):
    items = configure(env_file)
    items[1]["active"] = items[2]["active"] = True
    monkeypatch.setattr(webui, "CONFIG_SAVE_TIMEOUT", 0.3, raising=False)
    calls = []
    cancelled = []

    async def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json={"ok": True, "data": {"sources": [runtime("a")]}})
        calls.append(json.loads(req.content))
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        raise AssertionError("慢请求不能完成")

    _mock_http(handler)
    started = time.monotonic()
    with authed_client() as client:
        response = client.put("/api/config", json={"values": {"LX_SOURCE_LIST": json.dumps(items)}})
    assert time.monotonic() - started < 1
    assert response.status_code == 200
    assert response.json()["complete"] is False
    assert len(calls) == 1 and cancelled
    assert [i["active"] for i in json.loads(webui.read_env()["LX_SOURCE_LIST"])] == [True, False, False]
    assert {a["category"] for a in response.json()["actions"] if not a["ok"]} >= {
        "result_unknown", "not_executed"}


def test_unknown_write_is_read_back_without_replaying_mutation(env_file, svctl):
    items = configure(env_file)
    items[1]["active"] = True
    sources = [runtime("a")]
    calls = []

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json={"ok": True, "data": {"sources": sources}})
        calls.append(json.loads(req.content))
        sources.append(runtime("b"))
        raise httpx.ReadTimeout("response lost")

    _mock_http(handler)
    with authed_client() as client:
        response = client.put("/api/config", json={"values": {"LX_SOURCE_LIST": json.dumps(items)}})
    assert response.status_code == 200
    assert len(calls) == 1
    assert response.json()["complete"] is True
    assert response.json()["actions"][0]["confirmed_by_readback"] is True
    assert json.loads(webui.read_env()["LX_SOURCE_LIST"])[1]["active"] is True


def test_save_applies_order_in_same_budget_and_preserves_order_on_failure(env_file, svctl):
    configure(env_file, ("a", "b"))
    calls = []

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json={"ok": True, "data": {"sources": [runtime("a"), runtime("b")]}})
        calls.append((req.url.path, json.loads(req.content)))
        return httpx.Response(409, json={"ok": False, "category": "source_operation_busy", "error": "忙"})

    _mock_http(handler)
    with authed_client() as client:
        response = client.put("/api/config", json={"values": {
            "FNMUSIC_LX_SOURCE_ORDER": '["b","a"]',
            "FNMUSIC_RECOMMEND_DAILY": False,
        }})
    assert calls[0][0] == "/api/v1/source/reorder"
    assert calls[0][1]["source_ids"] == ["b", "a"]
    assert calls[0][1]["persist"] is False
    assert response.status_code == 200 and response.json()["complete"] is False
    assert webui.read_env()["FNMUSIC_LX_SOURCE_ORDER"] == '["a"]'
    assert webui.read_env()["FNMUSIC_RECOMMEND_DAILY"] == "false"


def test_env_lock_obeys_save_budget_without_late_write(env_file):
    from proxy import env_merge
    entered, release = threading.Event(), threading.Event()

    def holder():
        with env_merge.env_file_lock(env_file):
            entered.set()
            release.wait(2)

    worker = threading.Thread(target=holder)
    worker.start()
    assert entered.wait(1)
    started = time.monotonic()
    try:
        try:
            with env_merge.env_file_lock(env_file, timeout=0.03):
                raise AssertionError("不能越过占用中的配置锁")
        except TimeoutError:
            pass
        assert time.monotonic() - started < 0.3
    finally:
        release.set()
        worker.join(1)


def test_cross_process_env_lock_obeys_same_timeout(env_file):
    if sys.platform == "win32":
        return  # 实际发布目标为 Linux；Windows 锁分支不在此处使用 fcntl。
    worker = subprocess.Popen([
        sys.executable, "-c",
        "import fcntl,sys; f=open(sys.argv[1],'a+b'); "
        "fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.read()",
        str(env_file) + ".lock",
    ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert worker.stdout.readline().strip() == "locked"
        started = time.monotonic()
        try:
            with webui.env_file_lock(env_file, timeout=0.03):
                raise AssertionError("不能越过另一进程的配置锁")
        except TimeoutError:
            pass
        assert time.monotonic() - started < 0.3
    finally:
        worker.communicate(timeout=1)
        assert worker.returncode == 0


def test_concurrent_save_and_reorder_fail_fast(env_file, svctl):
    items = configure(env_file)
    items[1]["active"] = True

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(req):
            if req.method == "GET":
                return httpx.Response(200, json={"ok": True, "data": {"sources": [runtime("a")]}})
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"ok": True, "source_id": "b",
                                           "data": {"sources": [runtime("a"), runtime("b")]}})

        request = SimpleNamespace(app=webui.app)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
            webui.app.state.http = upstream
            first = asyncio.create_task(webui.api_config_put(
                webui.ConfigBody(values={"LX_SOURCE_LIST": json.dumps(items)}), request))
            await entered.wait()
            second = await webui.api_config_put(webui.ConfigBody(values={}), request)
            reorder = await webui.api_lx_source_reorder(webui.SourceReorderBody(source_ids=["a"]), request)
            assert second.status_code == reorder.status_code == 409
            release.set()
            assert (await first)["complete"] is True
        webui.app.state.http = None

    asyncio.run(run())


def test_slow_real_supervisor_is_bounded_without_blocking_event_loop(env_file, monkeypatch):
    monkeypatch.setattr(webui, "CONFIG_SAVE_TIMEOUT", 0.15)
    monkeypatch.setitem(webui.CONF, "supervisorctl", shlex.join([
        sys.executable, "-c", "import time; time.sleep(5)",
    ]))

    async def run():
        progressed = []

        async def tick():
            await asyncio.sleep(0.03)
            progressed.append(True)

        ticker = asyncio.create_task(tick())
        started = time.monotonic()
        response = await webui.api_config_put(webui.ConfigBody(values={
            "FNMUSIC_NETEASE_ENABLED": False, "FNMUSIC_MUSICDL_ENABLED": True,
        }), SimpleNamespace(app=webui.app))
        assert time.monotonic() - started < 0.5
        assert progressed
        assert response.status_code == 504
        assert json.loads(response.body)["category"] == "result_unknown"
        await ticker

    asyncio.run(run())


@pytest.mark.parametrize("snapshot", [
    {"ok": True}, {"ok": True, "data": {}},
    {"ok": True, "data": {"sources": [None]}},
])
def test_malformed_snapshot_cannot_be_used_as_empty_runtime(env_file, svctl, snapshot):
    items = configure(env_file)
    before = env_file.read_bytes()
    _mock_http(lambda req: httpx.Response(200, json=snapshot))
    with authed_client() as client:
        response = client.put("/api/config", json={"values": {"LX_SOURCE_LIST": items}})
    assert response.status_code == 503
    assert env_file.read_bytes() == before


def test_status_poll_uses_readonly_source_queries(env_file, svctl):
    configure(env_file)
    calls = []

    def handler(req):
        if req.url.path == "/api/v1/source":
            calls.append(req.url.params.get("read_only"))
            return httpx.Response(200, json={"ok": True, "data": {"sources": [runtime("a")]}})
        return httpx.Response(200, json={"ok": True})

    _mock_http(handler)
    with authed_client() as client:
        assert client.get("/api/status").status_code == 200
    assert calls and all(value == "true" for value in calls)
