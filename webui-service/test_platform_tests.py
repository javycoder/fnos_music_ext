"""平台批量测试的取消、限流及生命周期回归。"""
import asyncio
import json

import httpx
import pytest
from starlette.requests import Request

from test_webui import webui


@pytest.fixture(autouse=True)
def isolated_jobs(monkeypatch):
    monkeypatch.setattr(webui, "_platform_test_jobs", {})
    monkeypatch.setattr(webui, "_platform_test_tasks", {})


def request(body=None):
    async def receive():
        return {"type": "http.request", "body": json.dumps(body or {}).encode()}
    return Request({"type": "http", "app": webui.app, "headers": []}, receive)


def job():
    return {"id": "test", "status": "running", "results": [], "done": 0,
            "total": 3, "cancelled": False, "created_at": 0, "updated_at": 0}


def test_stop_cancels_active_request_and_queued_platforms(monkeypatch):
    async def scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()
        calls = []

        async def blocked(client, platform, keyword):
            calls.append(platform)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(webui, "_test_one_platform", blocked)
        webui._platform_test_jobs["test"] = j = job()
        task = asyncio.create_task(webui._run_platform_test_job("test", ["a", "b", "c"], "song", 1))
        webui._platform_test_tasks["test"] = task
        await asyncio.wait_for(started.wait(), 2)
        assert await webui.api_platform_test_stop(request({"job_id": "test"})) == {"ok": True}
        assert task.done() and cancelled.is_set()
        assert calls == ["a"]
        assert j["status"] == "cancelled"
        assert j["done"] == 0
    asyncio.run(scenario())


def test_stop_before_task_starts_still_reports_cancelled():
    async def scenario():
        webui._platform_test_jobs["test"] = j = job()
        task = asyncio.create_task(webui._run_platform_test_job("test", ["a"], "song", 1))
        webui._platform_test_tasks["test"] = task
        await webui.api_platform_test_stop(request({"job_id": "test"}))
        assert task.done()
        assert j["status"] == "cancelled"
    asyncio.run(scenario())


def test_start_rejects_overlapping_batches(monkeypatch):
    async def sources(*args):
        return True, {"registered": ["a", "b"]}
    monkeypatch.setattr(webui, "_fetch_json", sources)
    webui._platform_test_jobs["test"] = job()
    response = asyncio.run(webui.api_platform_test_start(request()))
    assert response.status_code == 409
    assert webui._platform_test_tasks == {}


def test_cleanup_preserves_running_jobs_and_bounds_finished_history():
    webui._platform_test_jobs["test"] = job()
    webui._cleanup_platform_test_jobs()
    assert "test" in webui._platform_test_jobs
    for i in range(40):
        webui._platform_test_jobs[str(i)] = {
            "status": "done", "updated_at": webui.time.monotonic()}
    webui._cleanup_platform_test_jobs()
    assert "test" in webui._platform_test_jobs
    assert len(webui._platform_test_jobs) < webui._PLATFORM_TEST_MAX_JOBS


def test_httpx_timeout_with_empty_message_is_reported():
    async def scenario():
        def timeout(req):
            raise httpx.ReadTimeout("", request=req)
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            result = await webui._test_one_platform(client, "a", "song")
        assert result["status"] == "error"
        assert result["error"] == "timeout"
    asyncio.run(scenario())


def test_completed_job_counts_and_concurrency(monkeypatch):
    async def scenario():
        active = peak = 0

        async def search(client, platform, keyword):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {"platform": platform, "status": "ok" if platform != "c" else "error"}

        monkeypatch.setattr(webui, "_test_one_platform", search)
        webui._platform_test_jobs["test"] = j = job()
        await webui._run_platform_test_job("test", ["a", "b", "c"], "song", 2)
        assert peak == 2
        assert (j["status"], j["done"], j["ok_count"], j["fail_count"]) == ("done", 3, 2, 1)
    asyncio.run(scenario())


def test_running_status_renews_preview(monkeypatch):
    webui._platform_test_jobs["test"] = {**job(), "keyword": "song", "concurrency": 1}
    monkeypatch.setattr(webui, "_preview_until", {"musicdl": 1.0})
    result = asyncio.run(webui.api_platform_test_status(request(), job="test"))
    assert result["status"] == "running"
    assert webui._preview_until["musicdl"] > webui.time.monotonic()
