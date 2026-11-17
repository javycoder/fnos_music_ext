"""Source runtime state, migration and rollback tests that need no live source."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import source_runtime as sr


class _FakeRuntime:
    def __init__(self, runtime_id: str = "run-1"):
        self.runtime_id = runtime_id
        self.script_hash = "hash"
        self.metadata = {"name": "Runtime Name", "version": "2.0.0"}
        self.platforms = ["kw"]
        self.qualities = {"kw": ["128k"]}
        self.pid = 123
        self.start_time_ticks = 1
        self.process = SimpleNamespace(returncode=None)
        self.exited = False
        self.stopping = False
        self.blocked = False
        self.cancel_unconfirmed = False
        self.stopped = False

    async def stop(self, budget_s=5.0):
        self.stopped = True
        self.exited = True
        return True


def test_derive_source_id_matches_upstream_generation_rules():
    assert sr.derive_source_id("源 名", "fallback.js") == "源 名.js"
    assert sr.derive_source_id("a/b:c*.js", "fallback.js") == "b_c_.js"
    assert sr.derive_source_id("", "folder/f.js") == "f.js"
    assert sr.derive_source_id("", "fallback.js") == "fallback.js"


def test_parse_script_metadata_keeps_existing_contract():
    script = """/*!
 * @name 测试源
 * @version 1.2.3
 * @author someone
 * @description a test source
 * @repository https://example.test/source
 */"""
    meta = sr.parse_script_metadata(script, "fallback")
    assert meta.name == "测试源"
    assert meta.version == "1.2.3"
    assert meta.author == "someone"
    assert meta.description == "a test source"
    assert meta.homepage == "https://example.test/source"


def test_enable_save_failure_rolls_back_record_and_stops_new_runtime(tmp_path, monkeypatch):
    manager = sr.SourceRuntimeManager(source_root=tmp_path / "sources")
    manager.source_dir.mkdir(parents=True)
    script_path = manager.source_dir / "one.js"
    script_path.write_text("/*!\n * @name One\n */\n", encoding="utf-8")
    record = {
        "id": "one.js",
        "name": "One",
        "enabled": False,
        "supportedSources": [],
        "version": "1.0.0",
    }
    runtime = _FakeRuntime()

    async def fake_start(record, *, deadline, script=None):
        return runtime

    monkeypatch.setattr(manager, "_start_record", fake_start)
    monkeypatch.setattr(manager, "_save_records", lambda records: (_ for _ in ()).throw(OSError("disk full")))

    async def run():
        with pytest.raises(OSError):
            await manager._enable_record([record], record, time.monotonic() + 5)

    asyncio.run(run())
    assert record["enabled"] is False
    assert runtime.stopped is True
    assert "one.js" not in manager._runtimes


def test_disable_save_failure_keeps_previous_enabled_state(tmp_path, monkeypatch):
    manager = sr.SourceRuntimeManager(source_root=tmp_path / "sources")
    manager.source_dir.mkdir(parents=True)
    (manager.source_dir / "one.js").write_text("/*!\n * @name One\n */\n", encoding="utf-8")
    record = {"id": "one.js", "name": "One", "enabled": True}
    runtime = _FakeRuntime()
    manager._runtimes["one.js"] = runtime
    monkeypatch.setattr(manager, "_save_records", lambda records: (_ for _ in ()).throw(OSError("disk full")))

    async def run():
        with pytest.raises(OSError):
            await manager._disable_record([record], record, time.monotonic() + 5)

    asyncio.run(run())
    assert record["enabled"] is True
    assert runtime.stopped is True
    assert "one.js" not in manager._runtimes


def test_replacement_does_not_spawn_when_old_worker_cannot_stop(tmp_path, monkeypatch):
    manager = sr.SourceRuntimeManager(source_root=tmp_path / "sources")
    runtime = _FakeRuntime()
    runtime.blocked = True

    async def fail_stop(budget_s=5.0):
        return False

    async def unexpected_spawn(**kwargs):
        pytest.fail("a second worker must not be spawned")

    runtime.stop = fail_stop
    manager._runtimes["one.js"] = runtime
    monkeypatch.setattr(manager, "_spawn_worker", unexpected_spawn)

    async def run():
        with pytest.raises(sr.SourceProcessUnavailable, match="source_process_stop_failed"):
            await manager._start_record(
                {"id": "one.js"}, deadline=time.monotonic() + 5, script="source",
            )

    asyncio.run(run())
    assert manager._runtimes["one.js"] is runtime


@pytest.mark.parametrize("failure_mode", ["false", "exception"])
def test_missing_record_retains_worker_that_cannot_stop(tmp_path, monkeypatch, failure_mode):
    manager = sr.SourceRuntimeManager(source_root=tmp_path / "sources")
    runtime = _FakeRuntime()

    async def fail_stop(budget_s=5.0):
        if failure_mode == "exception":
            raise OSError("stop failed")
        return False

    runtime.stop = fail_stop
    manager._runtimes["one.js"] = runtime
    monkeypatch.setattr(manager, "_load_records", lambda: [])

    async def run():
        assert await manager.list_sources() == []
        assert manager._runtimes["one.js"] is runtime
        assert runtime.blocked

    asyncio.run(run())


def test_failed_validation_is_tracked_and_blocks_second_worker(tmp_path, monkeypatch):
    manager = sr.SourceRuntimeManager(source_root=tmp_path / "sources")
    runtime = _FakeRuntime()
    spawns = []

    async def fail_stop(budget_s=5.0):
        return False

    async def spawn(**kwargs):
        spawns.append(kwargs)
        return runtime

    runtime.stop = fail_stop
    monkeypatch.setattr(manager, "_spawn_worker", spawn)

    async def run():
        for _ in range(2):
            with pytest.raises(sr.SourceProcessUnavailable, match="source_process_stop_failed"):
                await manager._validate_script("one.js", "source", time.monotonic() + 5)
        assert manager._runtimes["one.js"] is runtime
        assert runtime.blocked
        assert len(spawns) == 1

    asyncio.run(run())


def test_failed_worker_initialization_keeps_unstopped_process(tmp_path, monkeypatch):
    manager = sr.SourceRuntimeManager(source_root=tmp_path / "sources", lxserver_dir=tmp_path)
    monkeypatch.setattr(manager, "_check_node", lambda: None)
    monkeypatch.setattr(sr, "linux_monitoring_available", lambda: False)

    class FailedRuntime(_FakeRuntime):
        def __init__(self, **kwargs):
            super().__init__(kwargs["runtime_id"])
            self.writer = kwargs["writer"]

        def start_reader(self):
            pass

        def prepare_request(self, request_id):
            raise sr.SourceInitializationFailed("bad_init")

        async def stop(self, budget_s=5.0):
            return False

    async def spawn(*args, **kwargs):
        return SimpleNamespace(pid=123, returncode=None)

    async def connect(*, sock, limit):
        return None, SimpleNamespace(close=sock.close)

    monkeypatch.setattr(sr, "SourceProcess", FailedRuntime)
    monkeypatch.setattr(sr.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(sr.asyncio, "open_connection", connect)

    async def run():
        with pytest.raises(sr.SourceInitializationFailed, match="bad_init"):
            await manager._spawn_worker(
                source_id="one.js", runtime_id="run-1", filename="one.js",
                script="source", script_hash="hash", deadline=time.monotonic() + 5,
            )
        runtime = manager._runtimes["one.js"]
        assert runtime.blocked
        runtime.writer.close()

    asyncio.run(run())


def test_failed_enable_rollback_keeps_unstopped_worker(tmp_path, monkeypatch):
    manager = sr.SourceRuntimeManager(source_root=tmp_path / "sources")
    runtime = _FakeRuntime()
    record = {"id": "one.js", "name": "One", "enabled": False}
    monkeypatch.setattr(manager, "_read_script", lambda source_id: "source")
    monkeypatch.setattr(manager, "_save_records", lambda records: (_ for _ in ()).throw(OSError("disk full")))

    async def fail_stop(budget_s=5.0):
        return False

    async def start(record, *, deadline, script=None):
        manager._runtimes["one.js"] = runtime
        return runtime

    runtime.stop = fail_stop
    monkeypatch.setattr(manager, "_start_record", start)

    async def run():
        with pytest.raises(OSError, match="disk full"):
            await manager._enable_record([record], record, time.monotonic() + 5)
        assert manager._runtimes["one.js"] is runtime
        assert runtime.blocked
        assert record["enabled"] is False

    asyncio.run(run())


def test_close_keeps_unstopped_worker_tracked(tmp_path):
    manager = sr.SourceRuntimeManager(source_root=tmp_path / "sources")
    runtime = _FakeRuntime()
    attempts = 0

    async def fail_stop(budget_s=5.0):
        nonlocal attempts
        attempts += 1
        return attempts > 1

    runtime.stop = fail_stop
    manager._runtimes["one.js"] = runtime

    async def run():
        await manager.close()
        assert manager._runtimes["one.js"] is runtime
        assert runtime.blocked
        await manager.close()
        assert "one.js" not in manager._runtimes

    asyncio.run(run())


def test_startup_restore_preserves_files_and_uses_enabled_records(tmp_path, monkeypatch):
    manager = sr.SourceRuntimeManager(source_root=tmp_path / "sources", lxserver_dir=tmp_path / "missing")
    root = manager.source_dir
    root.mkdir(parents=True)
    (root / "one.js").write_text("/*!\n * @name One\n */\n", encoding="utf-8")
    (root / "two.js").write_text("/*!\n * @name Two\n */\n", encoding="utf-8")
    records = [
        {"id": "one.js", "name": "One", "enabled": True, "supportedSources": ["kw"]},
        {"id": "two.js", "name": "Two", "enabled": False, "supportedSources": ["kw"]},
    ]
    (root / "sources.json").write_text(json.dumps(records), encoding="utf-8")
    before = (root / "sources.json").read_bytes()
    async def fake_start(record, *, deadline, script=None):
        runtime = _FakeRuntime(runtime_id=f"run-{record['id']}")
        manager._runtimes[record["id"]] = runtime
        return runtime

    monkeypatch.setattr(manager, "_start_record", fake_start)
    monkeypatch.setattr(manager, "_check_node", lambda: None)

    async def run():
        await manager.start()
        rows = await manager.list_sources()
        assert {row["id"] for row in rows if row["process_state"] == "running"} == {"one.js"}
        await manager.close()

    asyncio.run(run())
    assert (root / "sources.json").read_bytes() == before
    assert (root / "one.js").exists()
    assert (root / "two.js").exists()


def test_disabled_source_reports_stopped_without_fake_zero():
    manager = sr.SourceRuntimeManager(source_root="/tmp/does-not-matter")
    usage = manager._public_record({"id": "one.js", "enabled": False})["resource_usage"]
    assert usage["monitoring_status"] == "stopped"
    assert usage["cpu_percent"] is None
    assert usage["abnormal"] is False
