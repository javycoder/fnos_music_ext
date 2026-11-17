"""源验证矩阵：平台、候选、音质和阶段归因必须分别留证。"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

import app as lx_app
import verify_source as verify
from source_runtime import SourceUnsafeVmRequired


@pytest.fixture
def verify_env(fake_lx, source_runtime_stub, monkeypatch):
    async def script(_url):
        return "/**\n * @name matrix-source\n * @version 1\n */"

    async def no_wait(_delay):
        return None

    async def probe(_client, url, headers=None, **_kwargs):
        return True, url, "audio/mpeg", 1024

    monkeypatch.setattr(verify, "download_script", script)
    monkeypatch.setattr(verify, "_new_temp_client", lambda **_kwargs: fake_lx)
    monkeypatch.setattr(verify, "_new_temp_manager", lambda *_args, **_kwargs: source_runtime_stub)
    monkeypatch.setattr(verify.asyncio, "sleep", no_wait)
    monkeypatch.setattr(lx_app, "probe_url", probe)
    return fake_lx, source_runtime_stub


@pytest.mark.anyio
async def test_verification_visits_all_platforms_candidates_and_declared_qualities(verify_env, monkeypatch):
    fake_lx, manager = verify_env
    manager.record.update({
        "supportedSources": ["kg", "tx"],
        "qualitys": {"kg": ["320k", "flac"], "tx": ["192k"]},
    })

    async def search(keyword, source="kg", **_kwargs):
        items = []
        for index in range(2):
            track_id = f"lx:{source}:{index}"
            raw = {
                "source": source,
                "songmid": str(index),
                "name": keyword,
                "singer": "测试",
            }
            fake_lx.cache_song_info(track_id, raw)
            items.append({"id": track_id, "title": keyword})
        return items

    calls, probes = [], []

    def resolve(source_id, song_info, quality, _deadline):
        calls.append((source_id, song_info["source"], song_info["songmid"], quality))
        if song_info["songmid"] == "0":
            return None
        return {
            "url": "https://media.invalid/audio",
            "sourceId": source_id,
            "sourceName": "matrix-source",
        }

    async def probe(_client, url, headers=None, **_kwargs):
        probes.append(dict(headers or {}))
        return True, url, "audio/flac", 1000

    manager.resolve_fn = resolve
    monkeypatch.setattr(fake_lx, "search", search)
    monkeypatch.setattr(lx_app, "probe_url", probe)

    report = await verify.verify_url(
        "https://scripts.invalid/matrix.js", keywords=["关键词一", "关键词二"]
    )
    assert report["ok"]
    assert report["platform_results"] == {"kg": "ok", "tx": "ok"}
    assert {"flac", "320k", "192k"} <= {quality for _, _, _, quality in calls}
    assert any(candidate == "1" for _, _, candidate, _ in calls)
    assert all(source_id == "temp.js" for source_id, _, _, _ in calls)
    assert all(item[0] == "temp.js" for item in manager.resolve_calls)
    assert all(headers.get("User-Agent") for headers in probes)
    assert any(headers.get("Referer") == "https://www.kugou.com/" for headers in probes)
    assert any(headers.get("Referer") == "https://y.qq.com/" for headers in probes)
    assert manager.toggle_calls == [("temp.js", True)]
    assert manager.close_calls == 1
    assert all("https://" not in str(attempt.get("error", "")) for attempt in report["attempts"])


@pytest.mark.anyio
@pytest.mark.parametrize("include_source_id", [True, False])
async def test_unknown_runtime_quality_metadata_explores_more_than_128k(
    verify_env, monkeypatch, include_source_id
):
    fake_lx, manager = verify_env
    manager.record.update({"id": "unknown.js", "name": "unknown", "supportedSources": ["kg"], "qualitys": {}})
    report = {
        "platforms": ["kg"],
        "qualitys": {"kg": []},
        "attempts": [],
        "platform_results": {"kg": "untested"},
        "ok": False,
        "meta": {"name": "unknown"},
    }

    async def search(*_args, **_kwargs):
        return [{"id": "lx:kg:one", "title": "test"}]

    calls, probes = [], []

    def resolve(source_id, _song, quality, _deadline):
        calls.append((quality, source_id))
        result = {"sourceName": "unknown", "url": "https://media.invalid/test"}
        if include_source_id:
            result["sourceId"] = source_id
        return result

    async def probe(*_args, **_kwargs):
        probes.append(True)
        return True, "https://media.invalid/test", "audio/mpeg", 1024

    manager.resolve_fn = resolve
    monkeypatch.setattr(fake_lx, "search", search)
    monkeypatch.setattr(lx_app, "probe_url", probe)
    await verify._verification_samples(
        lx_app, fake_lx, manager, "unknown.js", report, ["test"], time.monotonic() + 5
    )
    assert calls == [("flac", "unknown.js"), ("128k", "unknown.js")]
    assert report["ok"] is include_source_id
    assert bool(probes) is include_source_id
    assert all(not attempt["capability_known"] for attempt in report["attempts"])
    assert report["qualitys"]["kg"] == []


@pytest.mark.anyio
async def test_verifier_marks_unsafe_vm_as_untested_not_broken_script(verify_env):
    _, manager = verify_env
    manager.failures["upload"] = SourceUnsafeVmRequired()
    report = await verify.verify_url("https://scripts.invalid/fixture.js")
    assert not report["ok"]
    assert report["category"] == "unsafe_vm_required"
    assert report["requires_unsafe_vm"] is True
    assert report["complete"] is False
    assert manager.toggle_calls == []
    assert manager.close_calls == 1


def test_malformed_headers_do_not_crash_verification():
    headers = verify._verification_headers(
        SimpleNamespace(UA_PC="test-agent"), "kg", {"headers": "invalid"}
    )
    assert headers == {"User-Agent": "test-agent", "Referer": "https://www.kugou.com/"}
    headers = verify._verification_headers(
        SimpleNamespace(UA_PC="test-agent"),
        "tx",
        {"headers": {"Cookie": "private", "Host": "internal", "Referer": "https://source.invalid/"}},
    )
    assert "Cookie" not in headers and "Host" not in headers
    assert headers["Referer"] == "https://source.invalid/"


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["search", "resolve"])
@pytest.mark.parametrize("success_first", [False, True])
async def test_sample_read_timeout_preserves_evidence(
    verify_env, monkeypatch, stage, success_first
):
    fake_lx, manager = verify_env
    calls = []

    async def search(*args, **kwargs):
        calls.append(True)
        if success_first and len(calls) == 1:
            item = {
                "name": "Song",
                "singer": "Artist",
                "source": "kw",
                "songmid": "1",
            }
            fake_lx.cache_song_info("lx:kw:1", item)
            return [{"id": "lx:kw:1", "title": "Song"}]
        raise httpx.ReadTimeout("read stalled")

    def resolve(source_id, _song, _quality, _deadline):
        calls.append(True)
        if success_first and len(calls) == 1:
            return {"url": "https://media.invalid/audio", "sourceId": source_id}
        raise httpx.ReadTimeout("read stalled")

    if stage == "search":
        monkeypatch.setattr(fake_lx, "search", search)
    else:
        manager.resolve_fn = resolve

    report = await verify.verify_url(
        "https://scripts.invalid/bounded.js", keywords=["one", "two"]
    )
    assert report["ok"] is success_first
    assert report["category"] == ("partial" if success_first else "timeout")
    assert report["complete"] is False
    assert any(
        attempt["result"] == "timeout"
        and attempt["stage"] == stage
        and attempt["error"] == "ReadTimeout"
        for attempt in report["attempts"]
    )
    assert len(calls) > 1
    assert manager.close_calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("success_first", [False, True])
async def test_sample_budget_remains_hard_bound(verify_env, monkeypatch, success_first):
    fake_lx, manager = verify_env
    calls = []

    async def stalled(*_args, **_kwargs):
        calls.append(True)
        if success_first and len(calls) == 1:
            fake_lx.cache_song_info(
                "lx:kw:1",
                {"name": "Song", "singer": "Artist", "source": "kw", "songmid": "1"},
            )
            return [{"id": "lx:kw:1", "title": "Song"}]
        await asyncio.Event().wait()

    monkeypatch.setattr(fake_lx, "search", stalled)
    monkeypatch.setattr(verify, "_SAMPLE_BUDGET_S", 0.02)
    started = asyncio.get_running_loop().time()
    report = await verify.verify_url(
        "https://scripts.invalid/bounded.js", keywords=["one", "two"]
    )
    assert asyncio.get_running_loop().time() - started < 0.5
    assert report["ok"] is success_first
    assert report["category"] == ("partial" if success_first else "timeout")
    assert report["complete"] is False
    assert manager.close_calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("prestage", ["download", "upload", "enable"])
@pytest.mark.parametrize("success", [False, True])
async def test_total_deadline_counts_all_pre_sample_time(
    verify_env, monkeypatch, prestage, success
):
    _, manager = verify_env
    clock = [100.0]
    monkeypatch.setattr(verify, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 100.0))
    monkeypatch.setattr(verify, "_VERIFY_BUDGET_S", 0.1)
    monkeypatch.setattr(verify, "_SAMPLE_BUDGET_S", 0.2)

    if prestage == "download":
        original = verify.download_script
    else:
        original = getattr(manager, "upload_source" if prestage == "upload" else "set_enabled")

    async def costly(*args, **kwargs):
        clock[0] += 0.08
        return await original(*args, **kwargs)

    monkeypatch.setattr(verify, "download_script", costly) if prestage == "download" else monkeypatch.setattr(
        manager, "upload_source" if prestage == "upload" else "set_enabled", costly
    )

    original_timeout = asyncio.timeout
    budgets = []

    def timeout(delay):
        budgets.append(delay)
        return original_timeout(delay)

    async def samples(_app, _client, _manager, _target, report, _keywords, _deadline):
        assert budgets[-1] == pytest.approx(0.02)
        if success:
            report["ok"] = True
            report["attempts"].append({"result": "ok"})
        await asyncio.Event().wait()

    monkeypatch.setattr(verify.asyncio, "timeout", timeout)
    monkeypatch.setattr(verify, "_verification_samples", samples)
    report = await verify.verify_url("https://scripts.invalid/bounded.js")
    assert any(budget == pytest.approx(0.02) for budget in budgets)
    assert report["ok"] is success
    assert report["category"] == ("partial" if success else "timeout")
    assert manager.close_calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("elapsed", [0.125, 0.25])
async def test_exhausted_total_deadline_skips_samples(verify_env, monkeypatch, elapsed):
    _, manager = verify_env
    clock = [100.0]
    monkeypatch.setattr(verify, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 100.0))
    monkeypatch.setattr(verify, "_VERIFY_BUDGET_S", 0.125)
    original = manager.set_enabled

    async def toggle(*args, **kwargs):
        clock[0] += elapsed
        return await original(*args, **kwargs)

    async def unexpected(*_args, **_kwargs):
        pytest.fail("总预算已耗尽，不得进入 samples")

    monkeypatch.setattr(manager, "set_enabled", toggle)
    monkeypatch.setattr(verify, "_verification_samples", unexpected)
    report = await verify.verify_url("https://scripts.invalid/bounded.js")
    assert report["category"] == "timeout"
    assert report["ok"] is False
    assert report["complete"] is False
    assert manager.close_calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["search", "resolve"])
@pytest.mark.parametrize("kind", ["connect", "protocol", "502"])
async def test_real_service_failure_still_wins(verify_env, monkeypatch, stage, kind):
    fake_lx, manager = verify_env
    calls = []

    def error():
        request = httpx.Request("GET", "http://lx.test/internal")
        if kind == "502":
            httpx.Response(502, request=request).raise_for_status()
        error_type = httpx.ConnectError if kind == "connect" else httpx.RemoteProtocolError
        raise error_type("offline", request=request)

    async def search(*_args, **_kwargs):
        calls.append(True)
        if len(calls) == 1:
            fake_lx.cache_song_info(
                "lx:kw:1",
                {"name": "Song", "singer": "Artist", "source": "kw", "songmid": "1"},
            )
            return [{"id": "lx:kw:1", "title": "Song"}]
        error()

    def resolve(source_id, _song, _quality, _deadline):
        calls.append(True)
        if len(calls) == 1:
            return {"url": "https://media.invalid/audio", "sourceId": source_id}
        error()

    if stage == "search":
        monkeypatch.setattr(fake_lx, "search", search)
    else:
        manager.resolve_fn = resolve

    report = await verify.verify_url(
        "https://scripts.invalid/bounded.js", keywords=["one", "two"]
    )
    assert report["category"] == "service_unavailable"
    assert report["ok"] is False
    assert any(attempt["result"] == "ok" for attempt in report["attempts"])
    assert manager.close_calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["upload", "enable"])
async def test_management_read_timeout_not_replayed(verify_env, monkeypatch, stage):
    _, manager = verify_env
    calls = []

    async def unknown(*args, **kwargs):
        calls.append(args)
        error = httpx.ReadTimeout("write outcome unknown; 已存在 is not a safe response")
        error.result_unknown = True
        raise error

    monkeypatch.setattr(manager, stage + "_source" if stage == "upload" else "set_enabled", unknown)
    report = await verify.verify_url("https://scripts.invalid/bounded.js")
    assert report["category"] == "timeout"
    assert report["complete"] is False
    assert report["result_unknown"] is True
    assert report["stage"] == stage
    assert len(calls) == 1
    assert manager.resolve_calls == []
    assert manager.close_calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["connect", "protocol", "502"])
async def test_management_service_failure_not_replayed(verify_env, monkeypatch, kind):
    _, manager = verify_env
    calls = []

    async def unavailable(*args, **kwargs):
        calls.append(args)
        request = httpx.Request("POST", "http://lx.test/api/custom-source/upload")
        if kind == "502":
            error = httpx.HTTPStatusError(
                "upstream failed", request=request, response=httpx.Response(502, request=request)
            )
        else:
            error_type = httpx.ConnectError if kind == "connect" else httpx.RemoteProtocolError
            error = error_type("write outcome unknown", request=request)
        error.result_unknown = True
        raise error

    monkeypatch.setattr(manager, "upload_source", unavailable)
    report = await verify.verify_url("https://scripts.invalid/bounded.js")
    assert report["category"] == "service_unavailable"
    assert report["complete"] is False
    assert report["result_unknown"] is True
    assert len(calls) == 1
    assert manager.toggle_calls == []
    assert manager.resolve_calls == []
    assert manager.close_calls == 1


@pytest.mark.anyio
async def test_verify_uses_dedicated_manager_without_touching_same_name_source(
    verify_env, monkeypatch
):
    fake_lx, manager = verify_env
    manager.record.update({"name": "共享同名源"})
    fake_lx.sources = [
        {"id": "shared.js", "name": "共享同名源", "enabled": True, "status": "success",
         "supportedSources": ["kw"]},
    ]
    before = [dict(item) for item in fake_lx.sources]
    manager_paths = []
    original_factory = verify._new_temp_manager

    def factory(path, **kwargs):
        manager_paths.append(str(path))
        return original_factory(path, **kwargs)

    monkeypatch.setattr(verify, "_new_temp_manager", factory)
    report = await verify.verify_url("https://scripts.invalid/shared.js", keywords=["one"])
    assert report["ok"] is True
    assert len(manager_paths) == 1
    assert manager_paths[0].endswith("_open")
    assert fake_lx.sources == before
    assert fake_lx.upload_calls == []
    assert fake_lx.toggle_calls == []
    assert fake_lx.deleted_ids == []
    assert len(manager.upload_calls) == 1
    assert manager.toggle_calls == [("temp.js", True)]


def test_unknown_source_capabilities_are_not_fabricated():
    assert hasattr(verify, "_declared_qualities"), "需要独立解析运行时声明档位"
    assert verify._declared_qualities({"supportedSources": ["kg"]}, "kg") == []
    assert verify._declared_qualities(
        {"sources": {"kg": {"qualitys": ["master", "flac"]}}}, "kg"
    ) == ["master", "flac"]
