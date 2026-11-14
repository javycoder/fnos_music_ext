"""Worker protocol and real v2.2.0 source execution tests.

Real upstream tests are opt-in through FNMUSIC_TEST_LXSERVER_DIR so ordinary
offline pytest runs do not depend on a downloaded release artifact.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import time

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import source_runtime as sr


class _DummyWriter:
    def __init__(self):
        self.payloads: list[bytes] = []
        self.closed = False

    def write(self, payload: bytes) -> None:
        self.payloads.append(bytes(payload))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _DummyProcess:
    def __init__(self, pid: int = 4321):
        self.pid = pid
        self.returncode = None

    async def wait(self) -> int:
        await asyncio.sleep(3600)
        return 0

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _source_process(reader: asyncio.StreamReader, writer: _DummyWriter) -> sr.SourceProcess:
    return sr.SourceProcess(
        source_id="one.js",
        runtime_id="runtime-1",
        script_hash="hash",
        process=_DummyProcess(),
        reader=reader,
        writer=writer,
        start_time_ticks=1,
        metadata={},
        platforms=[],
        qualities={},
    )


async def _stop_reader(process: sr.SourceProcess) -> None:
    if process._reader_task is not None:
        process._reader_task.cancel()
        try:
            await process._reader_task
        except asyncio.CancelledError:
            pass


def _real_lxserver_dir() -> Path | None:
    candidates = [
        os.environ.get("FNMUSIC_TEST_LXSERVER_DIR", ""),
        os.environ.get("LXSERVER_DIR", ""),
        "/srv/lxserver",
    ]
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw)
        if (path / "server" / "server" / "sources" / "userApi.js").is_file():
            return path
    return None


def test_response_is_not_lost_when_it_arrives_before_waiter_starts():
    async def run():
        reader = asyncio.StreamReader(limit=sr.MAX_INIT_FRAME_BYTES)
        writer = _DummyWriter()
        process = _source_process(reader, writer)
        future = process.prepare_request("req-1")
        reader.feed_data((json.dumps({
            "type": "result",
            "request_id": "req-1",
            "runtime_id": "runtime-1",
            "source_id": "one.js",
            "ok": True,
            "result": {"url": "https://media.test/a.flac"},
        }) + "\n").encode())
        process.start_reader()
        try:
            message = await process.wait_request(
                "req-1", time.monotonic() + 1, future=future
            )
            assert message["result"]["url"] == "https://media.test/a.flac"
        finally:
            await _stop_reader(process)

    asyncio.run(run())


def test_cancel_ack_with_wrong_runtime_identity_is_rejected():
    async def run():
        reader = asyncio.StreamReader(limit=sr.MAX_INIT_FRAME_BYTES)
        writer = _DummyWriter()
        process = _source_process(reader, writer)
        waiter = asyncio.get_running_loop().create_future()
        process._close_waiters["req-2"] = waiter
        reader.feed_data((json.dumps({
            "type": "cancel_ack",
            "request_id": "req-2",
            "runtime_id": "stale-runtime",
            "source_id": "one.js",
        }) + "\n").encode())
        process.start_reader()
        try:
            with pytest.raises(sr.SourceProcessUnavailable):
                await asyncio.wait_for(waiter, 1)
        finally:
            await _stop_reader(process)

    asyncio.run(run())


def test_shutdown_can_be_sent_after_stopping_flag_is_set():
    async def run():
        reader = asyncio.StreamReader(limit=sr.MAX_INIT_FRAME_BYTES)
        writer = _DummyWriter()
        process = _source_process(reader, writer)
        process.stopping = True
        with pytest.raises(sr.SourceProcessUnavailable):
            await process.send({"type": "resolve"})
        await process.send({
            "type": "shutdown",
            "request_id": "stop-1",
            "runtime_id": "runtime-1",
            "source_id": "one.js",
        }, allow_stopping=True)
        assert json.loads(writer.payloads[-1].decode())["type"] == "shutdown"
        await _stop_reader(process)

    asyncio.run(run())


def test_stop_reaps_live_child_after_ipc_disconnect():
    node = shutil.which("node")
    if not node or not sr.linux_monitoring_available():
        pytest.skip("requires Node and Linux procfs")

    async def run():
        child = await asyncio.create_subprocess_exec(node, "-e", "setInterval(() => {}, 1000)")
        try:
            sample = sr.read_proc_stat(child.pid)
            assert sample is not None
            reader = asyncio.StreamReader()
            reader.feed_eof()
            runtime = sr.SourceProcess(
                source_id="one.js", runtime_id="runtime-1", script_hash="hash",
                process=child, reader=reader, writer=_DummyWriter(),
                start_time_ticks=sample.start_time_ticks, metadata={}, platforms=[], qualities={},
            )
            runtime.start_reader()
            await runtime._reader_task
            assert runtime.exited is True
            assert child.returncode is None
            assert await runtime.stop(0.2) is True
            assert child.returncode is not None
        finally:
            if child.returncode is None:
                child.kill()
            await child.wait()

    asyncio.run(run())


def test_fake_unsafe_vm_module_is_reported_as_unsupported(tmp_path):
    lxserver = tmp_path / "lxserver"
    user_api = lxserver / "server" / "server" / "sources" / "userApi.js"
    user_api.parent.mkdir(parents=True)
    user_api.write_text(
        "exports.extractMetadata = () => ({name: 'Unsafe'});\n"
        "exports.loadUserApi = async () => ({success: false, apiInstance: null, "
        "error: 'REQUIRE_UNSAFE_VM', requireUnsafe: true});\n",
        encoding="utf-8",
    )
    vm2 = lxserver / "node_modules" / "vm2" / "index.js"
    vm2.parent.mkdir(parents=True)
    vm2.write_text("class VM {}\nmodule.exports = {VM};\n", encoding="utf-8")

    manager = sr.SourceRuntimeManager(
        source_root=tmp_path / "sources",
        lxserver_dir=lxserver,
        node_bin=shutil.which("node") or "node",
        init_timeout_s=3,
        stop_budget_s=1,
    )

    async def run():
        await manager.start()
        try:
            with pytest.raises(sr.SourceUnsafeVmRequired):
                await manager.upload_source(
                    "unsafe.js",
                    "/*!\n * @name Unsafe\n */\n",
                    deadline=time.monotonic() + 3,
                )
        finally:
            await manager.close()

    asyncio.run(run())
    assert not (tmp_path / "sources" / "Unsafe.js").exists()


PROMISE_SOURCE = """/*!
 * @name Promise Source
 * @version 1.0.0
 */
globalThis.lx.on('request', async ({source, action, info}) => {
  if (action !== 'musicUrl' || source !== 'kw') return undefined;
  return `https://media.test/${info.quality}/${info.musicInfo.hash || 'none'}`
    + `?album=${encodeURIComponent(info.musicInfo.albumId || '')}`;
});
globalThis.lx.send('inited', {sources: {kw: {qualitys: ['128k', 'flac']}}});
"""

CALLBACK_SOURCE = """/*!
 * @name Callback Source
 * @version 1.0.0
 */
globalThis.lx.on('request', (_payload, callback) => {
  setTimeout(() => callback({status: true, data: 'https://media.test/callback.flac'}), 20);
});
globalThis.lx.send('inited', {sources: {kw: {qualitys: ['128k']}}});
"""

AES_SOURCE = """/*!
 * @name AES Source
 * @version 1.0.0
 */
const key = Buffer.from(Array.from({length: 32}, (_, i) => i));
const out = globalThis.lx.utils.crypto.aesEncrypt(
  Buffer.from('fnmusic-aes-test'), 'ecb', key, null
);
globalThis.lx.on('request', async () => `https://media.test/${out.toString('hex')}`);
globalThis.lx.send('inited', {sources: {kw: {qualitys: ['128k']}}});
"""

BLOCKED_SOURCE = """/*!
 * @name Blocked Source
 * @version 1.0.0
 */
globalThis.lx.on('request', () => { while (true) {} });
globalThis.lx.send('inited', {sources: {kw: {qualitys: ['128k']}}});
"""

NORMAL_SOURCE = """/*!
 * @name Normal Source
 * @version 1.0.0
 */
globalThis.lx.on('request', async () => 'https://media.test/normal.flac');
globalThis.lx.send('inited', {sources: {kw: {qualitys: ['128k']}}});
"""


@pytest.fixture
def real_lxserver_dir():
    path = _real_lxserver_dir()
    if path is None:
        pytest.skip("set FNMUSIC_TEST_LXSERVER_DIR to a real lxserver v2.2.0 tree")
    return path


def _new_manager(tmp_path: Path, lxserver: Path) -> sr.SourceRuntimeManager:
    return sr.SourceRuntimeManager(
        source_root=tmp_path / "sources",
        lxserver_dir=lxserver,
        node_bin=shutil.which("node") or "node",
        init_timeout_s=20,
        stop_budget_s=2,
    )


def test_real_worker_promise_callback_aes_and_declarations(tmp_path, real_lxserver_dir):
    async def run():
        manager = _new_manager(tmp_path, real_lxserver_dir)
        await manager.start()
        try:
            promise = await manager.upload_source(
                "promise.js", PROMISE_SOURCE, deadline=time.monotonic() + 20
            )
            callback = await manager.upload_source(
                "callback.js", CALLBACK_SOURCE, deadline=time.monotonic() + 20
            )
            aes = await manager.upload_source(
                "aes.js", AES_SOURCE, deadline=time.monotonic() + 20
            )
            assert promise["platforms"] == {"kw": {"qualitys": ["128k", "flac"]}}
            assert callback["platforms"] == {"kw": {"qualitys": ["128k"]}}
            assert aes["platforms"] == {"kw": {"qualitys": ["128k"]}}

            for record in (promise, callback, aes):
                await manager.set_enabled(
                    record["id"], True, deadline=time.monotonic() + 20
                )
            rows = await manager.list_sources()
            runtime_ids = {row["runtime_id"] for row in rows}
            assert len(rows) == 3
            assert len(runtime_ids) == 3
            assert all(row["process_state"] == "running" for row in rows)

            first = await manager.resolve(
                promise["id"],
                {"source": "kw", "meta": {"hash": "abc", "albumId": "42"}},
                "flac",
                deadline=time.monotonic() + 5,
            )
            second = await manager.resolve(
                callback["id"], {"source": "kw"}, "128k",
                deadline=time.monotonic() + 5,
            )
            third = await manager.resolve(
                aes["id"], {"source": "kw"}, "128k",
                deadline=time.monotonic() + 5,
            )
            assert first["url"] == "https://media.test/flac/abc?album=42"
            assert first["sourceId"] == promise["id"]
            assert second["url"] == "https://media.test/callback.flac"
            assert third["url"] == (
                "https://media.test/c507394af2d8c62e2b9bb4a24c8b782f"
                "9f3b7504926f8bd36e3118e903a4cd4a"
            )
        finally:
            await manager.close()

    asyncio.run(run())


def test_real_worker_processes_are_isolated_from_blocked_source(tmp_path, real_lxserver_dir):
    async def run():
        manager = _new_manager(tmp_path, real_lxserver_dir)
        await manager.start()
        pids = []
        try:
            normal = await manager.upload_source(
                "normal.js", NORMAL_SOURCE, deadline=time.monotonic() + 20
            )
            blocked = await manager.upload_source(
                "blocked.js", BLOCKED_SOURCE, deadline=time.monotonic() + 20
            )
            await manager.set_enabled(normal["id"], True, deadline=time.monotonic() + 20)
            await manager.set_enabled(blocked["id"], True, deadline=time.monotonic() + 20)
            pids = [runtime.pid for runtime in manager._runtimes.values()]
            assert len(set(pids)) == 2

            blocked_task = asyncio.create_task(manager.resolve(
                blocked["id"], {"source": "kw"}, "128k",
                deadline=time.monotonic() + 0.4,
            ))
            await asyncio.sleep(0.05)
            started = time.monotonic()
            result = await manager.resolve(
                normal["id"], {"source": "kw"}, "128k",
                deadline=time.monotonic() + 3,
            )
            assert result["url"] == "https://media.test/normal.flac"
            assert time.monotonic() - started < 1.5
            with pytest.raises(sr.SourceResolveTimeout):
                await blocked_task
            rows = await manager.list_sources()
            blocked_row = next(row for row in rows if row["id"] == blocked["id"])
            assert blocked_row["status"] == "failed"
            assert blocked_row["error"] == "source_process_blocked"
            assert blocked_row["process_state"] == "running"
        finally:
            await manager.close()
        await asyncio.sleep(0.2)
        assert all(not Path(f"/proc/{pid}").exists() for pid in pids)

    asyncio.run(run())


def test_reenable_reuses_runtime_and_disable_creates_fresh_runtime(
    tmp_path, real_lxserver_dir
):
    async def run():
        manager = _new_manager(tmp_path, real_lxserver_dir)
        await manager.start()
        try:
            record = await manager.upload_source(
                "normal.js", NORMAL_SOURCE, deadline=time.monotonic() + 20
            )
            await manager.set_enabled(record["id"], True, deadline=time.monotonic() + 20)
            first = next(row for row in await manager.list_sources()
                         if row["id"] == record["id"])["runtime_id"]
            await manager.set_enabled(record["id"], True, deadline=time.monotonic() + 20)
            same = next(row for row in await manager.list_sources()
                        if row["id"] == record["id"])["runtime_id"]
            assert same == first
            manager.cpu._states[first].abnormal = True
            assert await manager.set_enabled(
                record["id"], False, deadline=time.monotonic() + 20
            ) is True
            stopped = next(row for row in await manager.list_sources()
                           if row["id"] == record["id"])
            assert stopped["resource_usage"]["monitoring_status"] == "stopped"
            assert stopped["resource_usage"]["abnormal"] is False
            assert await manager.set_enabled(
                record["id"], True, deadline=time.monotonic() + 20
            ) is True
            fresh = next(row for row in await manager.list_sources()
                         if row["id"] == record["id"])["runtime_id"]
            assert fresh != first
        finally:
            await manager.close()

    asyncio.run(run())


def test_invalid_source_is_not_persisted(tmp_path, real_lxserver_dir):
    async def run():
        manager = _new_manager(tmp_path, real_lxserver_dir)
        await manager.start()
        try:
            with pytest.raises(sr.SourceInitializationFailed):
                await manager.upload_source(
                    "bad.js",
                    "/*!\n * @name Bad Source\n */\nthrow new Error('nope');\n",
                    deadline=time.monotonic() + 20,
                )
            assert not (manager.source_dir / "Bad Source.js").exists()
            assert await manager.list_sources() == []
        finally:
            await manager.close()

    asyncio.run(run())
