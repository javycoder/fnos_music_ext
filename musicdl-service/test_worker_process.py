"""Real exec regressions: these must never use a thread/local worker substitute."""
import asyncio
import os
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hardening import SourceBulkhead, SourceBusy, WorkerDead, SingleFlight, spawn_subprocess_worker


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
        print(json.dumps({"type":"entry", "entry": []}), flush=True)
        continue
    if mode in ("partial_block", "partial"):
        print(json.dumps({"type":"entry", "entry":[{"id":"source:1"}, {}, "lyric"]}), flush=True)
    if mode in ("block", "partial_block", "ignore"):
        if mode == "ignore": signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True: time.sleep(1)
    if mode == "logs":
        os.write(2, b"SECRET https://credential.invalid/lyrics " * 100000)
    if mode == "slow": time.sleep(.3)
    print(json.dumps({"type":"result", "count": 1 if mode == "partial" else 0,
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
            started = time.monotonic()
            with pytest.raises(error):
                await pool.run("source", {"mode": mode}, timeout=.2, on_entry=entries.append)
            assert time.monotonic() - started < 1
            assert procs[0].returncode is not None
            assert entries == ([[{"id": "source:1"}, {}, "lyric"]] if mode == "partial_block" else [])
            assert (await pool.run("source", {}, timeout=1))["count"] == 0
            assert procs[1].pid != procs[0].pid
            assert (await pool.run("fast", {}, timeout=1))["count"] == 0
        finally:
            await pool.aclose()
    asyncio.run(main())
    assert "SECRET" not in caplog.text
    assert "credential.invalid" not in caplog.text


def test_clean_reuse_partial_recycles_and_stderr_cannot_block(process_factory):
    spawn, procs = process_factory
    async def main():
        pool = SourceBulkhead(["source"], spawn=spawn)
        try:
            await pool.run("source", {}, timeout=1)
            await pool.run("source", {"mode": "logs"}, timeout=2)
            assert len(procs) == 1
            result = await pool.run("source", {"mode": "partial"}, timeout=1)
            assert result["partial"] and len(result["entries"]) == 1
            assert procs[0].returncode is not None
            await pool.run("source", {}, timeout=1)
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
        pool = SourceBulkhead(["source"], spawn=spawn, term_grace_s=.05)
        task = asyncio.create_task(pool.run("source", {"mode": "ignore"}, timeout=5))
        while not procs:
            await asyncio.sleep(.001)
        await asyncio.sleep(.05)
        task.cancel()
        await asyncio.sleep(.01)
        task.cancel()  # cleanup must survive repeated disconnect/shutdown cancellation
        with pytest.raises(asyncio.CancelledError):
            await task
        assert procs[0].returncode is not None
        assert not pool._slots["source"].busy
        await pool.aclose()
    asyncio.run(main())


def test_queue_and_start_share_one_budget(process_factory):
    spawn, procs = process_factory
    async def main():
        pool = SourceBulkhead(["source"], spawn=spawn)
        try:
            first = asyncio.create_task(pool.run("source", {"mode": "slow"}, timeout=1))
            while not procs:
                await asyncio.sleep(.001)
            with pytest.raises(SourceBusy):
                await pool.run("source", {}, timeout=.03)
            started = time.monotonic()
            with pytest.raises(asyncio.TimeoutError):
                await pool.run("source", {"mode": "slow"}, timeout=.4)
            assert time.monotonic() - started < .7
            await first
            assert all(p.returncode is not None for p in procs)
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
            result = await pool.run("SourceMusicClient", payload, timeout=1, on_entry=entries.append)
            assert result["partial"] and len(entries) == 1
            assert procs[0].returncode is not None
            # limit hit while another probe is pending must also recycle.
            result = await pool.run("SourceMusicClient", dict(payload, limit=1), timeout=1)
            assert result["partial"]
            assert procs[1].returncode is not None
            with pytest.raises(asyncio.TimeoutError):
                await pool.run("SourceMusicClient", dict(payload, keyword="block"), timeout=1)
        finally:
            await pool.aclose()
        assert all(proc.returncode is not None for proc in procs)
    asyncio.run(main())
