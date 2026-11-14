"""Issue #96 管理互斥与校验恢复回归；仅模拟源进程，不执行源脚本。"""

from __future__ import annotations

import asyncio
import inspect
import json
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
def verification(monkeypatch, fake_lx, source_runtime_stub):
    """真实 verify_url + 替身元数据客户端、替身独立运行时。"""
    import app as lx_app
    import verify_source

    async def download(_url):
        return "/*! @name X */ /* inert fixture: never executed */"

    async def probe(_client, url, *_args, **_kwargs):
        return True, url, "audio/mpeg", 1024

    monkeypatch.setattr(verify_source, "_new_temp_client", lambda **_kwargs: fake_lx)
    monkeypatch.setattr(
        verify_source, "_new_temp_manager", lambda *_args, **_kwargs: source_runtime_stub
    )
    monkeypatch.setattr(verify_source, "download_script", download)
    monkeypatch.setattr(lx_app, "probe_url", probe)
    return verify_source, fake_lx, source_runtime_stub


def test_verify_uses_dedicated_manager_and_closes_it(verification):
    module, metadata, manager = verification
    report = asyncio.run(
        module.verify_url("https://example.test/inert.js", keywords=["test"])
    )
    assert report["ok"] is True
    assert report["declared_platforms"] == ["kw"]
    assert len(manager.upload_calls) == 1
    assert manager.toggle_calls == [("temp.js", True)]
    assert manager.close_calls == 1
    assert metadata.upload_calls == []
    assert metadata.toggle_calls == []
    assert metadata.deleted_ids == []


def test_verify_url_quick_gate_skips_sampling(verification, monkeypatch):
    module, _metadata, manager = verification

    async def fail_sampling(*_args, **_kwargs):
        raise AssertionError("quick=True should not run sampling")

    monkeypatch.setattr(module, "_verification_samples", fail_sampling)
    result = asyncio.run(
        module.verify_url("https://example.test/inert.js", quick=True)
    )
    assert result["ok"] is True
    assert result["category"] == "quick_gate"
    assert result["complete"] is True
    assert "kw" in result["platforms"]
    assert manager.toggle_calls == []
    assert manager.close_calls == 1


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
def test_verify_cancel_or_timeout_still_closes_temp_runtime(verification, monkeypatch, mode):
    module, metadata, manager = verification
    entered, release = asyncio.Event(), asyncio.Event()

    async def stalled(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return []

    monkeypatch.setattr(metadata, "search", stalled)

    async def run():
        async def verify():
            if mode == "timeout":
                async with asyncio.timeout(0.02):
                    return await module.verify_url(
                        "https://example.test/inert.js", keywords=["test"]
                    )
            return await module.verify_url(
                "https://example.test/inert.js", keywords=["test"]
            )

        task = asyncio.create_task(verify())
        await entered.wait()
        if mode == "cancel":
            with pytest.raises(SourceOperationBusy):
                async with management_operation():
                    pass
            task.cancel()
        with pytest.raises(TimeoutError if mode == "timeout" else asyncio.CancelledError):
            await task
        release.set()
        assert manager.close_calls == 1
        async with management_operation():
            pass

    asyncio.run(run())


def test_verify_cleanup_failure_is_logged_and_manager_closed(verification, caplog):
    module, _metadata, manager = verification
    manager.failures["close"] = RuntimeError("cleanup broken")
    asyncio.run(module.verify_url("https://example.test/inert.js", keywords=["test"]))
    assert manager.close_calls == 1
    assert "verify_source runtime cleanup failed: RuntimeError" in caplog.text


def test_verify_cancel_cleanup_timeout_is_bounded_and_logged(
    verification, monkeypatch, caplog
):
    module, metadata, manager = verification
    entered, release = asyncio.Event(), asyncio.Event()
    manager.close_entered, manager.close_gate = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(module, "_CLEANUP_TIMEOUT_S", 0.02)

    async def stalled(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return []

    monkeypatch.setattr(metadata, "search", stalled)

    async def run():
        task = asyncio.create_task(
            module.verify_url("https://example.test/inert.js", keywords=["test"])
        )
        await entered.wait()
        started = asyncio.get_running_loop().time()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert asyncio.get_running_loop().time() - started < 0.3
        assert manager.close_entered.is_set()
        assert manager.closed is True
        async with management_operation():
            pass

    asyncio.run(run())
    assert "verify_source runtime cleanup failed" in caplog.text
    assert "TimeoutError" in caplog.text


def test_verify_lock_covers_download_too(verification, monkeypatch):
    module, _metadata, manager = verification

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def download(_url):
            entered.set()
            await release.wait()
            return "/*! @name X */"

        monkeypatch.setattr(module, "download_script", download)
        task = asyncio.create_task(
            module.verify_url("https://example.test/inert.js", keywords=["test"])
        )
        await entered.wait()
        with pytest.raises(SourceOperationBusy):
            async with management_operation():
                pass
        release.set()
        await task
        assert manager.close_calls == 1

    asyncio.run(run())


def test_verify_cleanup_lock_held_until_close_finishes(verification):
    module, _metadata, manager = verification
    manager.close_entered, manager.close_gate = asyncio.Event(), asyncio.Event()

    async def run():
        task = asyncio.create_task(
            module.verify_url("https://example.test/inert.js", keywords=["test"])
        )
        await manager.close_entered.wait()
        with pytest.raises(SourceOperationBusy):
            async with management_operation():
                pass
        manager.close_gate.set()
        await task
        async with management_operation():
            pass

    asyncio.run(run())


def test_verify_metadata_close_timeout_is_bounded_and_logged(
    verification, monkeypatch, caplog
):
    module, metadata, manager = verification
    closed = []
    monkeypatch.setattr(module, "_CLOSE_TIMEOUT_S", 0.02)

    async def hanging_close():
        closed.append(True)
        await asyncio.Event().wait()

    monkeypatch.setattr(metadata, "close", hanging_close)

    async def run():
        started = asyncio.get_running_loop().time()
        await module.verify_url("https://example.test/inert.js", keywords=["test"])
        assert asyncio.get_running_loop().time() - started < 0.3
        assert closed == [True]
        assert manager.close_calls == 1
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
    caps = lx_app._source_capabilities_snapshot(
        [None, "bad", {"id": "a.js", "enabled": True, "status": "success"}]
    )
    assert caps["kw"]["playback_available"] is True
