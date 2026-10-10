"""Hourly retention is independent of environment changes and owns its lifecycle."""
import asyncio
import os
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

import proxy.app as pa
from proxy import fnlog


@pytest.fixture(autouse=True)
def isolated_lifespan(monkeypatch, tmp_path):
    monkeypatch.setenv("FNMUSIC_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(pa, "_SEARCH_CACHE", {})
    monkeypatch.setattr(pa, "_LX_SCRIPT_MAP_TASK", None)
    monkeypatch.setattr(pa, "env_snapshot_lines", lambda scope: [])
    monkeypatch.setattr(pa, "write_env_snapshot", lambda scope: None)
    monkeypatch.setitem(pa.CONF, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(pa.CONF, "library_dir", str(tmp_path / "library"))


def _age_log(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("old log", encoding="utf-8")
    old = time.time() - (fnlog.RETENTION_DAYS + 1) * 86400
    os.utime(path, (old, old))


@pytest.mark.parametrize("env_watch", [False, True])
def test_lifespan_retention_runs_hourly_without_env_changes(monkeypatch, tmp_path, env_watch):
    monkeypatch.setenv("FNMUSIC_BACKGROUND_JOBS", "1")
    monkeypatch.setitem(pa.CONF, "env_watch", env_watch)
    stable_env = tmp_path / ".env"
    stable_env.write_text("LX_ENABLED=true\n", encoding="utf-8")
    monkeypatch.setattr(pa, "_env_watch_path", lambda: str(stable_env))
    reloads = []
    monkeypatch.setattr(pa, "apply_env_hot_reload", lambda: reloads.append(True))
    worker_threads = []
    main_thread = threading.get_ident()
    stale = tmp_path / "logs" / "install.log"
    fresh = tmp_path / "logs" / "recent.log"
    _age_log(stale)
    fresh.write_text("recent", encoding="utf-8")

    def purge():
        worker_threads.append(threading.get_ident())
        fnlog.purge_stale()

    monkeypatch.setattr(pa, "purge_stale", purge)

    async def run():
        log_sleeps = asyncio.Queue()
        env_sleeps = asyncio.Queue()
        log_ticks = asyncio.Queue()
        env_ticks = asyncio.Queue()
        sweeper_started = asyncio.Event()
        sweeper_cancelled = asyncio.Event()

        async def sleep(seconds):
            task = asyncio.current_task()
            if task.get_coro().__name__ == "_log_retention_loop":
                await log_sleeps.put((seconds, task))
                await log_ticks.get()
            else:
                assert task.get_coro().__name__ == "_env_watch_loop"
                await env_sleeps.put((seconds, task))
                await env_ticks.get()

        # Override this module's clock waits, not asyncio.sleep globally.
        monkeypatch.setattr(pa, "asyncio", SimpleNamespace(**{
            name: sleep if name == "sleep" else getattr(asyncio, name)
            for name in dir(asyncio) if not name.startswith("__")
        }))

        async def sweeper():
            sweeper_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                sweeper_cancelled.set()

        monkeypatch.setattr(pa, "_lyric_orphan_sweeper", sweeper)
        test_app = FastAPI()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as client:
            # No sockets/network or unrelated real directory sweeps in this test.
            for name in ("upstream", "musicdl", "musicbox", "lx", "llm"):
                setattr(test_app.state, name + "_client", client)
            async with pa.lifespan(test_app):
                seconds, log_task = await asyncio.wait_for(log_sleeps.get(), 2)
                await asyncio.wait_for(sweeper_started.wait(), 1)
                assert seconds == 3600
                assert not stale.exists()
                assert fresh.exists()
                assert len(worker_threads) == 1
                assert worker_threads[0] != main_thread
                env_task = None
                if env_watch:
                    _, env_task = await asyncio.wait_for(env_sleeps.get(), 1)
                    original_stat = stable_env.stat()
                    # A full unchanged-env polling iteration still performs no reload.
                    env_ticks.put_nowait(None)
                    await asyncio.wait_for(env_sleeps.get(), 1)
                    assert stable_env.stat() == original_stat
                    assert reloads == []
                else:
                    assert env_sleeps.empty()
                # New stale exports are removed on the next hour, even with no env reload.
                export = tmp_path / "logs" / "export.logzip"
                _age_log(export)
                log_ticks.put_nowait(None)
                next_seconds, next_task = await asyncio.wait_for(log_sleeps.get(), 2)
                assert next_seconds == 3600 and next_task is log_task
                assert not export.exists()
                assert fresh.exists()
                assert len(worker_threads) == 2
                assert all(ident != main_thread for ident in worker_threads)
                assert reloads == []
            assert log_task.cancelled()
            assert sweeper_cancelled.is_set()
            if env_task is not None:
                assert env_task.cancelled()
            # Shutdown really joined cancellation; no further purge is scheduled.
            await asyncio.sleep(0)
            assert len(worker_threads) == 2
    asyncio.run(run())


def test_lifespan_background_opt_out_disables_retention(monkeypatch):
    monkeypatch.setenv("FNMUSIC_BACKGROUND_JOBS", "0")
    monkeypatch.setitem(pa.CONF, "env_watch", True)
    calls = []

    async def unexpected():
        calls.append(True)

    monkeypatch.setattr(pa, "_log_retention_loop", unexpected)
    monkeypatch.setattr(pa, "_env_watch_loop", unexpected)
    monkeypatch.setattr(pa, "_lyric_orphan_sweeper", unexpected)

    async def run():
        async with pa.lifespan(FastAPI()):
            await asyncio.sleep(0)
        assert not calls
    asyncio.run(run())


def test_retention_can_be_cancelled_while_purge_thread_is_blocked(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def purge():
        entered.set()
        try:
            assert release.wait(5), "test did not release purge thread"
        finally:
            finished.set()

    monkeypatch.setattr(pa, "purge_stale", purge)

    async def run():
        task = asyncio.create_task(pa._log_retention_loop())
        try:
            # Waiting from another executor worker proves the event loop remains available.
            assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert not finished.is_set()  # cancellation need not wait for synchronous IO
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            assert await asyncio.wait_for(asyncio.to_thread(finished.wait, 2), 3)
    asyncio.run(run())


def test_retention_retries_after_purge_error_and_propagates_cancellation(monkeypatch):
    calls = []

    def purge():
        calls.append(True)
        if len(calls) == 1:
            raise OSError("temporary IO failure")

    monkeypatch.setattr(pa, "purge_stale", purge)

    async def run():
        sleeps = asyncio.Queue()
        ticks = asyncio.Queue()

        async def sleep(seconds):
            await sleeps.put(seconds)
            await ticks.get()

        monkeypatch.setattr(pa, "asyncio", SimpleNamespace(**{
            name: sleep if name == "sleep" else getattr(asyncio, name)
            for name in dir(asyncio) if not name.startswith("__")
        }))
        task = asyncio.create_task(pa._log_retention_loop())
        try:
            assert await asyncio.wait_for(sleeps.get(), 2) == 3600
            assert len(calls) == 1
            ticks.put_nowait(None)
            assert await asyncio.wait_for(sleeps.get(), 2) == 3600
            assert len(calls) == 2
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(calls) == 2
    asyncio.run(run())
