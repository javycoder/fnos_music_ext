"""Issue #96 管理互斥与校验恢复回归；仅模拟 HTTP，不执行源脚本。"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from types import SimpleNamespace

import httpx
import pytest

from lxserver_client import (
    LxServerClient,
    SourceOperationBusy,
    management_operation,
    serialize_source_operation,
)


def _client(handler):
    client = LxServerClient(base_url="http://same-server", admin_password="pw")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=client.base_url,
    )
    return client


def test_decorator_preserves_metadata_and_resolves_signature():
    @serialize_source_operation
    async def operation(url: str, *, enabled: bool = True) -> bool:
        """original doc"""
        return enabled

    assert operation.__name__ == "operation"
    assert operation.__doc__ == "original doc"
    assert operation.__wrapped__.__name__ == "operation"
    assert inspect.signature(operation).parameters["url"].annotation is str
    assert inspect.signature(operation).return_annotation is bool
    assert asyncio.run(operation("x", enabled=False)) is False


def test_management_nested_task_reentry_and_client_calls():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/list"):
            return httpx.Response(200, json=[
                {"id": "x", "name": "X", "enabled": False, "status": "success"},
            ])
        return httpx.Response(200, json={"success": True})

    @serialize_source_operation
    async def nested(client):
        async with management_operation():
            async with client.mgmt_lock:
                return await client.set_source_enabled("x", True)

    async def run():
        client = _client(handler)
        try:
            async with management_operation():
                assert await nested(client) is True
            assert not client.mgmt_lock.locked()
        finally:
            await client.close()

    asyncio.run(run())
    assert calls == ["/api/custom-source/list", "/api/custom-source/toggle"]


def test_child_task_cannot_inherit_reentry_and_gets_bounded_busy():
    async def contender():
        started = asyncio.get_running_loop().time()
        with pytest.raises(SourceOperationBusy):
            async with management_operation():
                pytest.fail("child task inherited parent lock ownership")
        return asyncio.get_running_loop().time() - started

    async def run():
        async with management_operation():
            elapsed = await asyncio.create_task(contender())
            assert 0.08 <= elapsed <= 0.3
            # 冲突不应释放父任务的锁。
            async with management_operation():
                pass
        async with management_operation():
            pass

    asyncio.run(run())


def test_short_contention_waits_and_succeeds():
    async def run():
        release = asyncio.Event()
        entered = asyncio.Event()

        async def holder():
            async with management_operation():
                entered.set()
                await release.wait()

        task = asyncio.create_task(holder())
        await entered.wait()
        asyncio.get_running_loop().call_later(0.01, release.set)
        async with management_operation():
            pass
        await task

    asyncio.run(run())


def test_cancelled_waiter_does_not_leak_lock():
    async def run():
        entered = asyncio.Event()

        async def contender():
            entered.set()
            async with management_operation():
                pytest.fail("cancelled waiter acquired lock")

        async with management_operation():
            task = asyncio.create_task(contender())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        async with management_operation():
            pass

    asyncio.run(run())


def test_same_client_has_fresh_lock_across_event_loops():
    client = LxServerClient(base_url="http://same-server")
    locks = []

    async def run():
        lock = client.mgmt_lock
        locks.append(lock)  # 保留旧锁，确保不是 GC 后偶然重建。
        assert client.mgmt_lock is lock
        async with lock:
            async def contend():
                with pytest.raises(SourceOperationBusy):
                    async with client.mgmt_lock:
                        pass
            await asyncio.create_task(contend())
        async with client.mgmt_lock:
            pass

    asyncio.run(run())
    asyncio.run(run())
    assert locks[0] is not locks[1]


@pytest.mark.parametrize("operation", ["upload", "import", "toggle", "delete", "set"])
def test_clients_same_base_url_share_all_management_mutations(operation):
    requests = []

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"success": True})

    async def run():
        first, second = _client(handler), _client(handler)
        try:
            assert first.mgmt_lock is second.mgmt_lock
            async with first.mgmt_lock:
                async def mutation():
                    if operation == "upload":
                        await second.upload_custom_source("x.js", "/* inert */")
                    elif operation == "import":
                        await second.import_custom_source("https://example.test/inert.js")
                    elif operation == "toggle":
                        await second.toggle_custom_source("x", True)
                    elif operation == "delete":
                        await second.delete_custom_source("x")
                    else:
                        await second.set_source_enabled("x", True)
                with pytest.raises(SourceOperationBusy):
                    await asyncio.create_task(mutation())
            assert requests == []
        finally:
            await first.close()
            await second.close()

    asyncio.run(run())


def test_reads_and_playback_do_not_take_management_lock():
    def handler(request):
        if request.url.path == "/api/music/url":
            return httpx.Response(200, json={"url": "https://media.test/inert.mp3"})
        return httpx.Response(200, json=[])

    async def run():
        client = _client(handler)
        try:
            async with management_operation():
                async def read():
                    assert await client.list_custom_sources() == []
                    assert await client.search("test") == []
                    assert (await client.get_music_url({"songmid": "1"}))["url"]
                await asyncio.create_task(read())
        finally:
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize("enabled,status,expected_toggles", [
    (True, "success", 0),
    (True, "failed", 1),
    (True, "error", 1),
    (True, None, 1),
    (False, "success", 1),
])
def test_healthy_enabled_is_idempotent_but_failed_source_can_retry(enabled, status, expected_toggles):
    toggles = []

    def handler(request):
        if request.url.path.endswith("/list"):
            return httpx.Response(200, json=[
                {"id": "x", "name": "X", "enabled": enabled, "status": status},
                {"id": "other", "enabled": True, "status": "success"},
            ])
        toggles.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True})

    async def run():
        client = _client(handler)
        try:
            assert await client.set_source_enabled("X", True) is True
        finally:
            await client.close()

    asyncio.run(run())
    assert toggles == ([{"id": "x", "enabled": True}] if expected_toggles else [])


@pytest.mark.parametrize("payload", [None, {}, {"sources": None}, {"sources": {}}, "bad"])
def test_source_list_invalid_success_payload_is_not_empty_config(payload):
    async def run():
        client = _client(lambda request: httpx.Response(200, content=json.dumps(payload)))
        try:
            with pytest.raises(ValueError, match="invalid payload"):
                await client.list_custom_sources()
        finally:
            await client.close()

    asyncio.run(run())


@pytest.fixture
def verification(monkeypatch):
    """全链路使用真实客户端 + 内存 HTTP transport，脚本内容绝不执行。"""
    import verify_source

    state = SimpleNamespace(
        reused=False, enabled=False, toggle_failure=False, cleanup_failure=False,
        cleanup_hangs=False, close_hangs=False, calls=[], sources=[],
        search_entered=None, search_release=None, cleanup_entered=None,
        cleanup_release=None, client=None, closed=False,
    )

    async def handler(request):
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        state.calls.append((path, body))
        if path.endswith("/upload"):
            if state.reused:
                return httpx.Response(500, json={"error": '源 "X" 已存在'})
            return httpx.Response(200, json={"success": True, "id": "created"})
        if path.endswith("/list"):
            target_id = "existing" if state.reused else "created"
            # 故意把同名其他源放在前面，上传成功只能按创建 ID 选中。
            state.sources = [
                {"id": "other", "name": "X", "enabled": True, "supportedSources": ["kg"]},
                {"id": target_id, "name": "X", "enabled": state.enabled,
                 "supportedSources": ["kw"]},
            ] if not state.reused else [
                {"id": target_id, "name": "X", "enabled": state.enabled,
                 "supportedSources": ["kw"]},
                {"id": "other", "name": "Other", "enabled": True},
            ]
            return httpx.Response(200, json=state.sources)
        if path.endswith("/toggle"):
            if body["enabled"] and state.toggle_failure:
                return httpx.Response(500, json={"error": "enable failed"})
            if not body["enabled"] and state.cleanup_failure:
                raise httpx.ConnectError("restore offline", request=request)
            if not body["enabled"] and state.cleanup_hangs:
                state.cleanup_entered.set()
                await state.cleanup_release.wait()
            return httpx.Response(200, json={"success": True})
        if path.endswith("/delete"):
            if state.cleanup_failure:
                raise httpx.ConnectError("delete offline", request=request)
            if state.cleanup_hangs:
                state.cleanup_entered.set()
                await state.cleanup_release.wait()
            return httpx.Response(200, json={"success": True})
        if path == "/api/music/search":
            if state.search_entered is not None:
                state.search_entered.set()
                await state.search_release.wait()
            return httpx.Response(200, json=[])
        raise AssertionError(path)

    def factory(**kwargs):
        client = LxServerClient(**kwargs)
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=client.base_url,
        )
        original_close = client.close

        async def close():
            state.closed = True
            if state.close_hangs:
                await asyncio.Event().wait()
            await original_close()

        client.close = close
        state.client = client
        return client

    async def download(url):
        return "/*! @name X */ /* inert fixture: never executed */"

    async def no_wait(*args):
        pass

    monkeypatch.setattr(verify_source, "_new_temp_client", factory)
    monkeypatch.setattr(verify_source, "download_script", download)
    monkeypatch.setattr(verify_source.asyncio, "sleep", no_wait)
    # verify 只会在解析成功后使用探活客户端；当前 fixture 在搜索即返回空列表。
    monkeypatch.setitem(sys.modules, "app", SimpleNamespace(app=object(), get_http=lambda app: None))
    return verify_source, state


def test_verify_created_id_only_is_deleted_and_same_name_is_not_reused(verification):
    module, state = verification
    report = asyncio.run(module.verify_url("https://example.test/inert.js", keywords=["test"]))
    assert report["declared_platforms"] == ["kw"]
    assert state.closed
    assert [(path, body) for path, body in state.calls if path.endswith("/delete")] == [
        ("/api/custom-source/delete", {"id": "created"}),
    ]
    assert [body for path, body in state.calls if path.endswith("/toggle")] == [
        {"id": "created", "enabled": True},
    ]


@pytest.mark.parametrize("enabled", [False, True])
def test_verify_reused_source_only_restores_its_own_state(verification, enabled):
    module, state = verification
    state.reused, state.enabled = True, enabled
    asyncio.run(module.verify_url("https://example.test/inert.js", keywords=["test"]))
    assert not [path for path, body in state.calls if path.endswith("/delete")]
    assert [body for path, body in state.calls if path.endswith("/toggle")] == ([] if enabled else [
        {"id": "existing", "enabled": True},
        {"id": "existing", "enabled": False},
    ])
    assert state.closed


def test_verify_reused_failed_enable_is_not_reverted_as_if_changed(verification):
    module, state = verification
    state.reused, state.toggle_failure = True, True
    with pytest.raises(RuntimeError, match="enable failed"):
        asyncio.run(module.verify_url("https://example.test/inert.js", keywords=["test"]))
    assert [body for path, body in state.calls if path.endswith("/toggle")] == [
        {"id": "existing", "enabled": True},
    ]
    assert not [path for path, body in state.calls if path.endswith("/delete")]
    assert state.closed


@pytest.mark.parametrize("reused", [False, True])
@pytest.mark.parametrize("timeout", [False, True])
def test_verify_cancel_and_timeout_restore_before_releasing_lock(verification, reused, timeout):
    module, state = verification
    state.reused = reused

    async def run():
        state.search_entered, state.search_release = asyncio.Event(), asyncio.Event()

        async def verify():
            if timeout:
                async with asyncio.timeout(0.02):
                    return await module.verify_url("https://example.test/inert.js", keywords=["test"])
            return await module.verify_url("https://example.test/inert.js", keywords=["test"])

        task = asyncio.create_task(verify())
        await state.search_entered.wait()
        # 同一 base URL 的其他临时客户端也必须看到占锁，不能中途修改。
        async def mutate():
            async with management_operation():
                pass
        if not timeout:
            with pytest.raises(SourceOperationBusy):
                await mutate()
            task.cancel()
        with pytest.raises(TimeoutError if timeout else asyncio.CancelledError):
            await task
        assert state.closed
        async with management_operation():
            pass

    asyncio.run(run())
    cleanup = [(path, body) for path, body in state.calls
               if path.endswith("/delete") or (path.endswith("/toggle") and not body["enabled"])]
    assert cleanup == ([("/api/custom-source/toggle", {"id": "existing", "enabled": False})]
                       if reused else [("/api/custom-source/delete", {"id": "created"})])


@pytest.mark.parametrize("reused", [False, True])
def test_verify_cleanup_failure_is_logged_and_client_closed(verification, caplog, reused):
    module, state = verification
    state.reused, state.cleanup_failure = reused, True
    asyncio.run(module.verify_url("https://example.test/inert.js", keywords=["test"]))
    assert state.closed
    assert "verify_source cleanup failed" in caplog.text
    assert ("existing" if reused else "created") in caplog.text


@pytest.mark.parametrize("reused", [False, True])
def test_verify_cancel_cleanup_timeout_is_bounded_and_logged(verification, monkeypatch, caplog, reused):
    module, state = verification
    state.reused, state.cleanup_hangs = reused, True
    monkeypatch.setattr(module, "_CLEANUP_TIMEOUT_S", 0.02)

    async def run():
        state.search_entered, state.search_release = asyncio.Event(), asyncio.Event()
        state.cleanup_entered, state.cleanup_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(module.verify_url("https://example.test/inert.js", keywords=["test"]))
        await state.search_entered.wait()
        started = asyncio.get_running_loop().time()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert asyncio.get_running_loop().time() - started < 0.3
        assert state.cleanup_entered.is_set()
        assert state.closed
        async with management_operation():
            pass

    asyncio.run(run())
    assert "verify_source cleanup failed" in caplog.text
    assert "TimeoutError" in caplog.text


def test_verify_lock_covers_download_too(verification, monkeypatch):
    module, state = verification

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def download(url):
            entered.set()
            await release.wait()
            return "/*! @name X */"

        monkeypatch.setattr(module, "download_script", download)
        task = asyncio.create_task(module.verify_url("https://example.test/inert.js", keywords=["test"]))
        await entered.wait()
        with pytest.raises(SourceOperationBusy):
            async with management_operation():
                pass
        release.set()
        await task
        assert state.closed

    asyncio.run(run())


def test_verify_cleanup_lock_held_until_restore_finishes(verification, monkeypatch):
    module, state = verification
    state.cleanup_hangs = True
    monkeypatch.setattr(module, "_CLEANUP_TIMEOUT_S", 1.0)

    async def run():
        state.cleanup_entered, state.cleanup_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(module.verify_url("https://example.test/inert.js", keywords=["test"]))
        await state.cleanup_entered.wait()
        with pytest.raises(SourceOperationBusy):
            async with management_operation():
                pass
        state.cleanup_release.set()
        await task
        async with management_operation():
            pass

    asyncio.run(run())


def test_verify_client_close_timeout_is_bounded_and_logged(verification, monkeypatch, caplog):
    module, state = verification
    state.close_hangs = True
    monkeypatch.setattr(module, "_CLOSE_TIMEOUT_S", 0.02)

    async def run():
        started = asyncio.get_running_loop().time()
        await module.verify_url("https://example.test/inert.js", keywords=["test"])
        assert asyncio.get_running_loop().time() - started < 0.3
        assert state.closed
        # fixture 故意阻塞 close，补关 mock transport，避免测试本身遗留资源。
        await LxServerClient.close(state.client)
        async with management_operation():
            pass

    asyncio.run(run())
    assert "verify_source client close failed: TimeoutError" in caplog.text


def test_describe_source_snapshot_derives_file_url_and_ignores_non_dict_entries():
    import app as lx_app

    snap = lx_app._describe_source_snapshot([
        None,
        "bad-entry",
        {
            "id": "local_uploaded.js",
            "name": "本地上传源",
            "version": "1.2.0",
            "enabled": True,
            "status": "success",
            "supportedSources": ["kw", "tx"],
        },
        {
            "id": "remote_src.js",
            "name": "远程导入源",
            "sourceUrl": "https://example.test/remote.js",
            "enabled": True,
            "status": "failed",
            "error": "init err",
        },
    ])
    assert snap["state"] == "degraded"
    assert snap["ready_count"] == 1
    assert snap["failed_count"] == 1
    assert snap["pending_count"] == 0
    assert len(snap["sources"]) == 2
    assert snap["sources"][0]["url"] == f"file://{lx_app._lx_source_fs_path('local_uploaded.js')}"
    assert snap["sources"][0]["initialized"] is True
    assert snap["sources"][1]["url"] == "https://example.test/remote.js"
    assert snap["sources"][1]["initialized"] is False
    caps = lx_app._source_capabilities_snapshot([None, "bad", {"id": "a.js", "enabled": True, "status": "success"}])
    assert caps["kw"]["playback_available"] is True

