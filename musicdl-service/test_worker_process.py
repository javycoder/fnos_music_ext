"""Real exec regressions: these must never use a thread/local worker substitute."""
import asyncio
import os
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hardening import (SourceBulkhead, SourceBusy, WorkerDead, WorkerTaskError,
                       SingleFlight, spawn_subprocess_worker)


# Cold interpreter/import time is not part of the protocol-failure assertions.
# Production still charges startup to each request; test that independently below.
STARTUP_TIMEOUT = 10.0


PROTOCOL_WORKER = '''
import json, os, signal, sys, time
if os.environ.get("TEST_START_BLOCK"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True: time.sleep(1)
print(json.dumps({"type":"ready"}), flush=True)
for line in sys.stdin:
    task = json.loads(line)
    mode = task.get("mode", "ok")
    if mode == "crash": os._exit(17)
    if mode == "corrupt":
        print("SECRET https://credential.invalid/lyrics", flush=True)
        continue
    if mode == "unknown":
        print(json.dumps({"type":"unexpected"}), flush=True)
        continue
    if mode == "bad_entry":
        print(json.dumps({"type": "entry", "entry": []}), flush=True)
        continue
    if mode == "error":
        print(json.dumps({"type": "error", "error": "task failed"}), flush=True)
        continue
    if mode in ("partial_block", "partial", "hold", "ignore"):
        if mode == "ignore": signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print(json.dumps({"type":"entry", "entry":[{"id":"source:1"}, {}, "lyric"]}), flush=True)
    if mode == "hold":
        while not os.path.exists(task["release_path"]): time.sleep(.001)
    if mode in ("block", "partial_block", "ignore"):
        if mode == "ignore": signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True: time.sleep(1)
    if mode == "logs":
        os.write(2, b"SECRET https://credential.invalid/lyrics " * 100000)
    if mode == "slow": time.sleep(.3)
    print(json.dumps({"type":"result", "count": 1 if mode in ("partial", "hold") else 0,
                      "partial": mode == "partial"}), flush=True)
'''


@pytest.fixture
def process_factory(tmp_path):
    script = tmp_path / "protocol_worker.py"
    script.write_text(PROTOCOL_WORKER)
    processes = []

    async def spawn(source, **kwargs):
        worker = await spawn_subprocess_worker(source, script=script, **kwargs)
        processes.append(worker.proc)
        return worker

    yield spawn, processes
    assert all(proc.returncode is not None for proc in processes), "child leaked"
    for proc in processes:
        with pytest.raises(ProcessLookupError):
            os.kill(proc.pid, 0)


@pytest.mark.parametrize("mode,error", [("block", asyncio.TimeoutError),
    ("ignore", asyncio.TimeoutError), ("partial_block", asyncio.TimeoutError),
    ("crash", WorkerDead), ("corrupt", WorkerDead), ("unknown", WorkerDead),
    ("bad_entry", WorkerDead)])
def test_bad_worker_reaped_then_next_request_recovers(process_factory, mode, error, caplog):
    spawn, procs = process_factory
    async def main():
        pool = SourceBulkhead(["source", "fast"], spawn=spawn, term_grace_s=.05)
        entries = []
        try:
            # Complete the real ready handshake before measuring task failure.
            await pool.run("source", {}, timeout=STARTUP_TIMEOUT)
            started = time.monotonic()
            with pytest.raises(error):
                await pool.run("source", {"mode": mode},
                               timeout=.5 if error is asyncio.TimeoutError else STARTUP_TIMEOUT,
                               on_entry=entries.append)
            assert time.monotonic() - started < STARTUP_TIMEOUT + 1
            assert procs[0].returncode is not None
            assert entries == ([[{"id": "source:1"}, {}, "lyric"]]
                               if mode in ("partial_block", "ignore") else [])
            assert (await pool.run("source", {}, timeout=STARTUP_TIMEOUT))["count"] == 0
            assert procs[1].pid != procs[0].pid
            assert (await pool.run("fast", {}, timeout=STARTUP_TIMEOUT))["count"] == 0
        finally:
            await pool.aclose()
    with caplog.at_level("INFO", logger="musicdl_service"):
        asyncio.run(main())
    expected_count = 1 if mode in ("partial_block", "ignore") else 0
    task_logs = [record.getMessage() for record in caplog.records
                 if "source=source" in record.getMessage() and "recycle=complete" not in record.getMessage()]
    assert any(f"count={expected_count} " in message for message in task_logs)
    assert "SECRET" not in caplog.text
    assert "credential.invalid" not in caplog.text


def test_task_level_error_keeps_worker_reusable(process_factory):
    """任务级失败只上报不断进程：坑位保留复用，不回收不计崩溃。"""
    spawn, procs = process_factory
    async def main():
        pool = SourceBulkhead(["source"], spawn=spawn)
        try:
            await pool.run("source", {}, timeout=STARTUP_TIMEOUT)
            with pytest.raises(WorkerTaskError):
                await pool.run("source", {"mode": "error"}, timeout=STARTUP_TIMEOUT)
            assert (await pool.run("source", {}, timeout=STARTUP_TIMEOUT))["count"] == 0
            assert len(procs) == 1
            stats = pool.stats()["sources"]["source"]
            assert stats["task_failed"] == 1
            assert stats["crashed"] == 0
            assert stats["recycled"] == 0
        finally:
            await pool.aclose()
    asyncio.run(main())


def test_clean_reuse_partial_recycles_and_stderr_cannot_block(process_factory):
    spawn, procs = process_factory
    async def main():
        pool = SourceBulkhead(["source"], spawn=spawn)
        try:
            await pool.run("source", {}, timeout=STARTUP_TIMEOUT)
            await pool.run("source", {"mode": "logs"}, timeout=STARTUP_TIMEOUT)
            assert len(procs) == 1
            result = await pool.run("source", {"mode": "partial"}, timeout=STARTUP_TIMEOUT)
            assert result["partial"] and len(result["entries"]) == 1
            assert procs[0].returncode is not None
            await pool.run("source", {}, timeout=STARTUP_TIMEOUT)
            assert len(procs) == 2
        finally:
            await pool.aclose()
    asyncio.run(main())


@pytest.mark.parametrize("starting", [False, True])
def test_cancel_reaps_including_startup(process_factory, monkeypatch, starting):
    spawn, procs = process_factory
    if starting:
        monkeypatch.setenv("TEST_START_BLOCK", "1")
    async def main():
        entered = asyncio.Event()
        reaping = asyncio.Event()

        async def observed_spawn(source, **kwargs):
            worker = await spawn(source, **kwargs)
            wait_ready = worker.wait_ready
            terminate = worker.terminate

            async def observed_ready(timeout):
                if starting:
                    entered.set()
                await wait_ready(timeout)

            async def observed_terminate(grace_s):
                reaping.set()
                await terminate(grace_s)

            worker.wait_ready = observed_ready
            worker.terminate = observed_terminate
            return worker

        pool = SourceBulkhead(["source"], spawn=observed_spawn, term_grace_s=.05)
        task = asyncio.create_task(pool.run("source", {"mode": "ignore"},
                                           timeout=STARTUP_TIMEOUT,
                                           on_entry=lambda entry: entered.set()))
        try:
            # Either blocked in startup, or the child has installed SIG_IGN and
            # acknowledged execution. Do not guess that .05s was enough to exec.
            await asyncio.wait_for(entered.wait(), STARTUP_TIMEOUT)
            task.cancel()
            await asyncio.wait_for(reaping.wait(), STARTUP_TIMEOUT)
            task.cancel()  # cleanup must survive repeated cancellation
            with pytest.raises(asyncio.CancelledError):
                await task
            assert procs[0].returncode is not None
            assert not pool._slots["source"].busy
        finally:
            await pool.aclose()
    asyncio.run(main())


def test_queue_and_start_share_one_budget(process_factory, tmp_path, monkeypatch):
    spawn, procs = process_factory

    async def main():
        import hardening
        pool = SourceBulkhead(["source"], spawn=spawn, term_grace_s=.05)
        entered = asyncio.Event()
        queued = asyncio.Event()
        release = tmp_path / "release-first"
        original_sleep = asyncio.sleep
        second = None

        async def observe_queue(delay):
            if asyncio.current_task() is second:
                queued.set()
            await original_sleep(delay)

        monkeypatch.setattr(hardening.asyncio, "sleep", observe_queue)
        try:
            await pool.run("source", {}, timeout=STARTUP_TIMEOUT)
            worker = pool._slots["source"].worker
            execute = worker.execute
            deadlines = []

            async def observe_deadline(payload, on_entry, deadline):
                if payload.get("mode") == "block":
                    deadlines.append(deadline)
                return await execute(payload, on_entry, deadline)

            worker.execute = observe_deadline
            first = asyncio.create_task(pool.run(
                "source", {"mode": "hold", "release_path": str(release)},
                timeout=STARTUP_TIMEOUT, on_entry=lambda entry: entered.set()))
            await asyncio.wait_for(entered.wait(), STARTUP_TIMEOUT)
            with pytest.raises(SourceBusy):
                await pool.run("source", {}, timeout=.03)
            started = time.monotonic()
            second = asyncio.create_task(pool.run("source", {"mode": "block"}, timeout=.3))
            await asyncio.wait_for(queued.wait(), STARTUP_TIMEOUT)
            latest_deadline = time.monotonic() + .3
            release.touch()  # Release only after the second request is queued.
            await first
            with pytest.raises(asyncio.TimeoutError):
                await second
            assert time.monotonic() - started < 1
            assert len(deadlines) == 1
            assert started + .3 <= deadlines[0] <= latest_deadline
            assert all(p.returncode is not None for p in procs)
        finally:
            await pool.aclose()
    asyncio.run(main())


def test_startup_consumes_task_budget_and_reaps(process_factory):
    spawn, procs = process_factory

    async def main():
        startup_entered = asyncio.Event()
        never_ready = asyncio.Event()
        observed = []

        async def paused_spawn(source, **kwargs):
            worker = await spawn(source, **kwargs)
            real_ready = worker.wait_ready

            async def paused_ready(timeout):
                observed.append(timeout)
                startup_entered.set()
                await never_ready.wait()
                await real_ready(timeout)

            worker.wait_ready = paused_ready
            return worker

        pool = SourceBulkhead(["source"], spawn=paused_spawn, term_grace_s=.05)
        # Unlike protocol tests, this specifically times out the ready handshake.
        # Pause at a boundary, not with a guessed child interpreter startup delay.
        task = asyncio.create_task(pool.run("source", {}, timeout=2))
        try:
            await asyncio.wait_for(startup_entered.wait(), STARTUP_TIMEOUT)
            with pytest.raises(asyncio.TimeoutError):
                await task
            assert 0 < observed[0] <= 2
            assert procs[0].returncode is not None
            assert not pool._slots["source"].busy
            assert pool.stats()["sources"]["source"]["last_recycle_reason"] == "timeout"
        finally:
            await pool.aclose()
    asyncio.run(main())


def test_first_waiter_cancel_does_not_kill_shared_operation():
    async def main():
        flight = SingleFlight()
        started = asyncio.Event()
        release = asyncio.Event()
        stopped = asyncio.Event()
        calls = 0
        async def operation():
            nonlocal calls
            calls += 1
            started.set()
            try:
                await release.wait()
                return "ok"
            finally:
                stopped.set()
        first = asyncio.create_task(flight.run("key", operation))
        await started.wait()
        second = asyncio.create_task(flight.run("key", operation))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not stopped.is_set()
        release.set()
        assert await second == "ok"
        assert calls == 1 and not flight._inflight
        release.clear()
        stopped.clear()
        last = asyncio.create_task(flight.run("key", operation))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        last.cancel()
        with pytest.raises(asyncio.CancelledError):
            await last
        assert stopped.is_set() and not flight._inflight
    asyncio.run(main())


def test_actual_worker_redirects_library_output_and_blocked_probe_returns(tmp_path, monkeypatch):
    # Run the real worker + real search_core with an importable fake library and probe.
    package = tmp_path / "musicdl"
    package.mkdir()
    (package / "__init__.py").write_text("from . import musicdl\n")
    (package / "musicdl.py").write_text('''
import os, time
from types import SimpleNamespace
print("library stdout secret", flush=True)
class MusicClient:
    def __init__(self, **kwargs): pass
    def search(self, keyword):
        if keyword == "warm": return {}
        if keyword == "block":
            while True: time.sleep(1)
        return {"source": [SimpleNamespace(identifier=str(i), source="SourceMusicClient",
            song_name="test", download_url="http://offline/"+str(i)) for i in range(2)]}
''')
    (tmp_path / "sitecustomize.py").write_text('''
import httpx, time
class Client:
    def __init__(self, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def head(self, url, **kwargs):
        if url.endswith("1"):
            while True: time.sleep(1)
        time.sleep(.03)
        return type("R", (), {"status_code":200,"headers":{}})()
    def stream(self, *args, **kwargs):
        class PrefixResponse:
            status_code = 200
            headers = {"content-type": "audio/mpeg"}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def iter_bytes(self, size): yield b"ID3" + bytes([3]) + bytes(64)
        return PrefixResponse()
httpx.Client = Client
# Force standard httpx probe even on machines with curl_cffi installed.
import sys
sys.modules["curl_cffi"] = None
''')
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    procs = []
    async def spawn(source, **kwargs):
        worker = await spawn_subprocess_worker(source, **kwargs)
        procs.append(worker.proc)
        return worker
    async def main():
        pool = SourceBulkhead(["SourceMusicClient"], spawn=spawn, term_grace_s=.05)
        try:
            entries = []
            payload = dict(source="SourceMusicClient", keyword="partial", fetch_size=2, limit=2)

            async def warm():
                # Exercise real worker/search_core imports with a startup budget,
                # then measure blocked probes independently on the ready process.
                result = await pool.run("SourceMusicClient", dict(payload, keyword="warm"),
                                        timeout=STARTUP_TIMEOUT)
                assert not result["partial"] and result["count"] == 0
                assert pool._slots["SourceMusicClient"].worker.ready

            await warm()
            result = await pool.run("SourceMusicClient", payload, timeout=1, on_entry=entries.append)
            assert result["partial"] and len(entries) == 1
            assert procs[0].returncode is not None
            # limit hit while another probe is pending must also recycle.
            await warm()
            result = await pool.run("SourceMusicClient", dict(payload, limit=1), timeout=1)
            assert result["partial"]
            assert procs[1].returncode is not None
            await warm()
            with pytest.raises(asyncio.TimeoutError):
                await pool.run("SourceMusicClient", dict(payload, keyword="block"), timeout=1)
        finally:
            await pool.aclose()
        assert all(proc.returncode is not None for proc in procs)
    asyncio.run(main())
