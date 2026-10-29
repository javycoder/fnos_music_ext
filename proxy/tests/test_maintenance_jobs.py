import asyncio
import sys
import types

import pytest

import proxy.app as runtime


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_jobs_confirmation_dedup_status_and_failure(monkeypatch):
    gate = asyncio.Event()

    async def cleanup(rt, job):
        assert rt is runtime
        await gate.wait()
        job["removed"] = 2

    monkeypatch.setitem(sys.modules, "proxy.maintenance", types.SimpleNamespace(run_cleanup=cleanup))
    monkeypatch.setattr(runtime, "_maintenance_jobs", {})
    monkeypatch.setattr(runtime, "_maintenance_task", None)
    status, _ = await runtime._maintenance_request("POST", "/api/maintenance/cleanup", {})
    assert status == 400
    status, first = await runtime._maintenance_request("POST", "/api/maintenance/cleanup", {"confirm_beta": True})
    assert status == 200
    _, second = await runtime._maintenance_request("POST", "/api/maintenance/cleanup", {"confirm_beta": True})
    assert first["job"]["id"] == second["job"]["id"]
    gate.set()
    await runtime._maintenance_task
    _, result = await runtime._maintenance_request("GET", "/api/maintenance/cleanup/" + first["job"]["id"], {})
    assert result["job"]["status"] == "completed"
    assert result["job"]["removed"] == 2
    assert (await runtime._maintenance_request("GET", "/api/maintenance/cleanup/" + "0" * 32, {}))[0] == 404

    async def broken(rt, job):
        raise OSError("isolated test failure")

    monkeypatch.setitem(sys.modules, "proxy.maintenance", types.SimpleNamespace(run_cleanup=broken))
    _, started = await runtime._maintenance_request("POST", "/api/maintenance/cleanup", {"confirm_beta": True})
    await runtime._maintenance_task
    job = runtime._maintenance_jobs[started["job"]["id"]]
    assert job["status"] == "failed"
    assert "isolated test failure" in job["errors"]


def test_cover_gc_respects_separate_ttls(tmp_path, monkeypatch):
    import os
    import time

    covers = tmp_path / "covers"
    covers.mkdir()
    monkeypatch.setitem(runtime.CONF, "cache_dir", str(tmp_path))
    monkeypatch.setattr(runtime, "_cover_disk_last_clean", 0)
    image = covers / ("a" * 40 + ".jpg")
    mapping = covers / ("g_" + "b" * 40 + ".url")
    old_mapping = covers / ("g_" + "c" * 40 + ".url")
    for file, days in ((image, 8), (mapping, 8), (old_mapping, 15)):
        file.write_text("test")
        t = time.time() - days * 86400
        os.utime(file, (t, t))
    runtime._cover_disk_gc()
    assert not image.exists()
    assert mapping.exists()
    assert not old_mapping.exists()


@pytest.mark.parametrize("kind", ["image", "mapping"])
def test_failed_cover_write_cleans_actual_temp(tmp_path, monkeypatch, kind):
    monkeypatch.setitem(runtime.CONF, "cache_dir", str(tmp_path))

    def fail(*args):
        raise OSError("test replace failure")

    monkeypatch.setattr(runtime.os, "replace", fail)
    if kind == "image":
        runtime._cover_disk_write("https://example.invalid/cover", b"image", "image/jpeg")
    else:
        runtime._cover_guid_url_persist("online:kuwo:test", "https://example.invalid/cover")
    assert list((tmp_path / "covers").iterdir()) == []


@pytest.mark.anyio
async def test_organize_confirmation_conflict_dedup_and_status(tmp_path, monkeypatch):
    gate = asyncio.Event()

    async def organize_run(rt, job):
        assert rt is runtime
        await gate.wait()
        job["moved"] = 1

    monkeypatch.setitem(sys.modules, "proxy.organize",
                        types.SimpleNamespace(run_organize=organize_run))
    monkeypatch.setattr(runtime, "_organize_jobs", {})
    monkeypatch.setattr(runtime, "_organize_task", None)
    monkeypatch.setattr(runtime, "_last_organize_job", None)
    monkeypatch.setitem(runtime.CONF, "tee_save_dir", str(tmp_path))
    monkeypatch.setitem(runtime.CONF, "cache_dir", str(tmp_path.parent / "cache"))
    monkeypatch.setitem(runtime.CONF, "download_layout", "artist_album")
    body = {"confirm_beta": True, "root": str(tmp_path), "layout": "artist_album"}
    assert (await runtime._maintenance_request(
        "POST", "/api/maintenance/organize", {**body, "layout": "flat"}))[0] == 409
    _, first = await runtime._maintenance_request("POST", "/api/maintenance/organize", body)
    _, second = await runtime._maintenance_request("POST", "/api/maintenance/organize", body)
    assert first["job"]["id"] == second["job"]["id"]
    _, current = await runtime._maintenance_request("GET", "/api/maintenance/organize", {})
    assert current["root"] == str(tmp_path)
    assert current["layout"] == "artist_album"
    gate.set()
    await runtime._organize_task
    _, result = await runtime._maintenance_request(
        "GET", "/api/maintenance/organize/" + first["job"]["id"], {})
    assert result["job"]["status"] == "completed"


def test_organize_refuses_unavailable_explicit_directory_and_cache_fallback(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setitem(runtime.CONF, "cache_dir", str(cache))
    monkeypatch.setitem(runtime.CONF, "tee_save_dir", str(tmp_path / "missing"))
    monkeypatch.setattr(runtime, "detect_library_dir", lambda: str(cache))
    with pytest.raises(ValueError, match="不可用"):
        runtime._organize_target()
    assert not (tmp_path / "missing").exists()
    monkeypatch.setitem(runtime.CONF, "tee_save_dir", "")
    with pytest.raises(ValueError, match="缓存目录"):
        runtime._organize_target()


@pytest.mark.anyio
async def test_organize_and_cleanup_exclude_each_other_and_job_keeps_snapshot(tmp_path, monkeypatch):
    gate = asyncio.Event()

    async def run(rt, job):
        await gate.wait()

    monkeypatch.setitem(sys.modules, "proxy.organize", types.SimpleNamespace(run_organize=run))
    monkeypatch.setattr(runtime, "_organize_jobs", {})
    monkeypatch.setattr(runtime, "_organize_task", None)
    monkeypatch.setattr(runtime, "_last_organize_job", None)
    monkeypatch.setattr(runtime, "_maintenance_task", None)
    monkeypatch.setitem(runtime.CONF, "tee_save_dir", str(tmp_path))
    monkeypatch.setitem(runtime.CONF, "cache_dir", str(tmp_path.parent / "cache"))
    monkeypatch.setitem(runtime.CONF, "download_layout", "artist")
    body = {"confirm_beta": True, "root": str(tmp_path), "layout": "artist"}
    cleanup = asyncio.create_task(gate.wait())
    monkeypatch.setattr(runtime, "_maintenance_task", cleanup)
    try:
        assert (await runtime._maintenance_request(
            "POST", "/api/maintenance/organize", body))[0] == 409
    finally:
        cleanup.cancel()
        await asyncio.gather(cleanup, return_exceptions=True)
    _, response = await runtime._maintenance_request("POST", "/api/maintenance/organize", body)
    task = runtime._organize_task
    try:
        assert (await runtime._maintenance_request(
            "POST", "/api/maintenance/cleanup", {"confirm_beta": True}))[0] == 409
        monkeypatch.setitem(runtime.CONF, "download_layout", "album")
        _, current = await runtime._maintenance_request(
            "GET", "/api/maintenance/organize/" + response["job"]["id"], {})
        assert current["job"]["root"] == str(tmp_path)
        assert current["job"]["layout"] == "artist"
    finally:
        gate.set()
        await task
