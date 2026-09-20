"""source_runtime 单元测试：元数据解析、下载防护、stdio 协议（FakeProc）、SourceManager 状态机。

Node 沙箱端到端（bridge.js 真进程）仅在环境有 node 时执行。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import httpx
import pytest

import source_runtime as sr
from conftest import STUB_SCRIPT

NODE_BIN = shutil.which("node")

_VALID_HEADER = (
    "/*!\n"
    " * @name 测试源\n"
    " * @version 1.2.3\n"
    " * @author someone\n"
    " * @description a test source\n"
    " * @homepage https://example.com/src\n"
    " */\n"
    "console.log('boot')\n"
)


# ------------------------------------------------------------------ 元数据 ---

def test_parse_script_meta_fields():
    meta = sr.parse_script_meta(_VALID_HEADER)
    assert meta["name"] == "测试源"
    assert meta["version"] == "1.2.3"
    assert meta["author"] == "someone"
    assert meta["description"] == "a test source"
    assert meta["homepage"] == "https://example.com/src"


def test_parse_script_meta_requires_header_block():
    with pytest.raises(sr.SourceError) as ei:
        sr.parse_script_meta("// @name x\nvar a = 1")
    assert ei.value.category == "invalid"


def test_parse_script_meta_requires_name():
    with pytest.raises(sr.SourceError) as ei:
        sr.parse_script_meta("/*\n * @version 1.0.0\n */\nvar a = 1")
    assert ei.value.category == "invalid"


def test_parse_script_meta_truncates_long_fields():
    script = f"/*\n * @name {'x' * 40}\n * @author {'y' * 80}\n */\n"
    meta = sr.parse_script_meta(script)
    # 超长字段截断为 limit 长度 + "..."
    assert meta["name"] == "x" * 24 + "..."
    assert meta["author"] == "y" * 56 + "..."


def test_parse_script_meta_ignores_unknown_keys():
    script = "/*\n * @name ok\n * @unknown whatever\n */\n"
    meta = sr.parse_script_meta(script)
    assert meta == {"name": "ok"}


# ------------------------------------------------------------------ 档位映射 ---

def test_script_quality_for_tier_preferences():
    assert sr.script_quality_for_tier("lossless", ["128k", "320k", "flac", "flac24bit"]) == "flac"
    # flac24bit 在无 flac 时兜底
    assert sr.script_quality_for_tier("lossless", ["flac24bit", "hires"]) == "flac24bit"
    assert sr.script_quality_for_tier("high", ["128k", "320k"]) == "320k"
    assert sr.script_quality_for_tier("standard", ["flac", "128k"]) == "128k"
    assert sr.script_quality_for_tier("lossless", ["128k"]) is None
    assert sr.script_quality_for_tier("high", ["128k", "flac"]) is None


def test_build_music_info_platform_keys():
    item = {
        "id": "lx:kg:KGHASH", "_identifier": "KGHASH", "title": "晴天", "artist": "周杰伦",
        "album": "叶惠美", "duration_s": 269, "cover_url": "https://img/1.jpg", "hash": "KGHASH",
    }
    info = sr.build_music_info(item, "kg")
    assert info["hash"] == "KGHASH"
    assert info["songmid"] == "KGHASH"  # 部分脚本读 songmid
    assert info["interval"] == "04:29"
    assert info["meta"]["picUrl"] == "https://img/1.jpg"

    kw = sr.build_music_info({"_identifier": "228908", "rid": "228908", "title": "t"}, "kw")
    assert kw["rid"] == "228908"
    wy = sr.build_music_info({"_identifier": "186016", "song_id": "186016"}, "wy")
    assert wy["songId"] == "186016"
    tx = sr.build_music_info({"_identifier": "MID", "songmid": "MID"}, "tx")
    assert tx["songmid"] == "MID"
    assert sr.build_music_info({"_identifier": "1"}, "wy")["meta"]["picUrl"] is None
    assert sr.build_music_info({"_identifier": "1", "duration_s": 61.6}, "kg")["interval"] == "01:01"


# ------------------------------------------------------------------ 下载防护 ---

def _patch_http(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(sr.httpx, "AsyncClient", factory)


def test_download_script_ok(monkeypatch):
    def handler(request):
        return httpx.Response(200, text=_VALID_HEADER)

    _patch_http(monkeypatch, handler)
    script = asyncio.run(sr.download_script("https://src.test/1.js"))
    assert script == _VALID_HEADER


def test_download_script_rejects_non_http_scheme():
    with pytest.raises(sr.SourceError) as ei:
        asyncio.run(sr.download_script("ftp://src.test/1.js"))
    assert ei.value.category == "download"


def test_download_script_http_error(monkeypatch):
    _patch_http(monkeypatch, lambda r: httpx.Response(404))
    with pytest.raises(sr.SourceError) as ei:
        asyncio.run(sr.download_script("https://src.test/missing.js"))
    assert ei.value.category == "download"


def test_download_script_redirect_limit(monkeypatch):
    def handler(request):
        return httpx.Response(302, headers={"Location": str(request.url)})

    _patch_http(monkeypatch, handler)
    with pytest.raises(sr.SourceError) as ei:
        asyncio.run(sr.download_script("https://loop.test/1.js"))
    assert "重定向" in str(ei.value)


def test_download_script_declared_size_limit(monkeypatch):
    _patch_http(monkeypatch, lambda r: httpx.Response(200, headers={"Content-Length": "9000001"}))
    with pytest.raises(sr.SourceError) as ei:
        asyncio.run(sr.download_script("https://big.test/1.js"))
    assert ei.value.category == "download"


def test_download_script_body_size_limit(monkeypatch):
    _patch_http(monkeypatch, lambda r: httpx.Response(200, content=b"x" * (sr.SCRIPT_MAX_BYTES + 1)))
    with pytest.raises(sr.SourceError) as ei:
        asyncio.run(sr.download_script("https://big.test/1.js"))
    assert ei.value.category == "download"


def test_download_script_invalid_utf8(monkeypatch):
    _patch_http(monkeypatch, lambda r: httpx.Response(200, content=b"\xff\xfe\xfa"))
    with pytest.raises(sr.SourceError) as ei:
        asyncio.run(sr.download_script("https://bin.test/1.js"))
    assert ei.value.category == "invalid"


def test_download_script_missing_header(monkeypatch):
    _patch_http(monkeypatch, lambda r: httpx.Response(200, text="var noHeader = 1"))
    with pytest.raises(sr.SourceError) as ei:
        asyncio.run(sr.download_script("https://bad.test/1.js"))
    assert ei.value.category == "invalid"


# ------------------------------------------------------------ stdio 协议（FakeProc） ---

class FakeReader:
    def __init__(self):
        self._q: asyncio.Queue = asyncio.Queue()

    async def readline(self) -> bytes:
        item = await self._q.get()
        return b"" if item is None else item

    def feed(self, line: bytes):
        self._q.put_nowait(line)

    def eof(self):
        self._q.put_nowait(None)


class FakeStdin:
    def __init__(self, sink: list):
        self.sink = sink

    def write(self, data: bytes):
        self.sink.append(data)

    async def drain(self):
        return None


class FakeProc:
    def __init__(self):
        self.stdout = FakeReader()
        self.stderr = FakeReader()
        self.lines_in: list[bytes] = []
        self.stdin = FakeStdin(self.lines_in)
        self.returncode = None
        self.spawn_args = None

    def terminate(self):
        self.returncode = 0
        self.stdout.eof()
        self.stderr.eof()

    def kill(self):
        self.returncode = -9
        self.stdout.eof()
        self.stderr.eof()

    async def wait(self):
        return self.returncode


def _inited_line(sources=None, status=True) -> bytes:
    payload = {
        "status": status,
        "sources": sources if sources is not None else {
            "kw": {"name": "酷我", "actions": ["musicUrl"], "qualitys": ["128k", "320k", "flac"]},
            "xx": {"name": "未知", "actions": ["musicUrl"], "qualitys": ["128k"]},  # 非法平台被忽略
        },
    }
    return (json.dumps({"type": "event", "name": "inited", "payload": payload}) + "\n").encode()


@pytest.fixture
def fake_spawn(monkeypatch):
    procs: list[FakeProc] = []

    async def spawn(*args, **kwargs):
        proc = FakeProc()
        proc.spawn_args = args
        procs.append(proc)
        return proc

    monkeypatch.setattr(sr.asyncio, "create_subprocess_exec", spawn)
    return procs


def _new_runtime(tmp_path):
    meta = sr.parse_script_meta(STUB_SCRIPT)
    return sr.UserSource(STUB_SCRIPT, meta, script_dir=str(tmp_path))


async def _respond(proc, *, result=None, error=None):
    while not proc.lines_in:
        await asyncio.sleep(0.001)
    msg = json.loads(proc.lines_in.pop(0))
    resp = {"id": msg["id"]}
    if error is not None:
        resp.update(ok=False, error=error)
    else:
        resp.update(ok=True, result=result)
    proc.stdout.feed((json.dumps(resp) + "\n").encode())


async def _drain_one(proc) -> dict:
    while not proc.lines_in:
        await asyncio.sleep(0.001)
    return json.loads(proc.lines_in.pop(0))


def test_user_source_start_and_platforms(fake_spawn, tmp_path):
    async def run():
        rt = _new_runtime(tmp_path)

        async def feeder():
            while not fake_spawn:
                await asyncio.sleep(0.001)
            fake_spawn[0].stdout.feed(_inited_line())

        feed_task = asyncio.create_task(feeder())
        await rt.start()
        await feed_task
        proc = fake_spawn[0]
        assert set(rt.platforms) == {"kw"}  # xx 平台被 MUSIC_PLATFORMS 过滤
        assert rt.platforms["kw"]["qualitys"] == ["128k", "320k", "flac"]
        assert rt.running is True
        assert proc.spawn_args[0] == "node"
        assert proc.spawn_args[1].endswith("bridge.js")
        meta_json = json.loads(proc.spawn_args[3])
        assert meta_json.get("name") == "test-src"

        info = {"songmid": "42", "name": "晴天", "singer": "周杰伦", "interval": "04:29"}
        resp_task = asyncio.create_task(_respond(proc, result="https://media.test/a.flac"))
        url = await rt.music_url(info, "320k", platform="kw", timeout=2.0)
        await resp_task
        return url

    assert asyncio.run(run()) == "https://media.test/a.flac"


def test_user_source_request_payload_shape(fake_spawn, tmp_path):
    async def run():
        rt = _new_runtime(tmp_path)

        async def feeder():
            while not fake_spawn:
                await asyncio.sleep(0.001)
            fake_spawn[0].stdout.feed(_inited_line())

        feed_task = asyncio.create_task(feeder())
        await rt.start()
        await feed_task
        proc = fake_spawn[0]
        info = {"songmid": "7", "name": "n", "singer": "s", "interval": "03:00"}
        call = asyncio.create_task(rt.music_url(info, "128k", platform="kw", timeout=2.0))
        sent = await _drain_one(proc)  # 先取走请求行，再回写响应
        proc.stdout.feed((json.dumps({"id": sent["id"], "ok": True, "result": "https://m/x.flac"}) + "\n").encode())
        url = await call
        return url, sent

    url, sent = asyncio.run(run())
    assert url == "https://m/x.flac"
    assert sent["type"] == "request"
    assert sent["source"] == "kw"
    assert sent["action"] == "musicUrl"
    assert sent["info"]["type"] == "128k"
    assert sent["info"]["musicInfo"]["songmid"] == "7"


def test_user_source_script_error(fake_spawn, tmp_path):
    async def run():
        rt = _new_runtime(tmp_path)

        async def feeder():
            while not fake_spawn:
                await asyncio.sleep(0.001)
            fake_spawn[0].stdout.feed(_inited_line())

        feed_task = asyncio.create_task(feeder())
        await rt.start()
        await feed_task
        resp_task = asyncio.create_task(_respond(fake_spawn[0], error="cannot resolve"))
        try:
            await rt.music_url({"songmid": "1"}, "128k", platform="kw", timeout=2.0)
        except sr.SourceError as exc:
            await resp_task
            return exc
        await resp_task
        return None

    exc = asyncio.run(run())
    assert exc is not None and exc.category == "resolve"
    assert "cannot resolve" in str(exc)


def test_user_source_timeout(fake_spawn, tmp_path, monkeypatch):
    async def run():
        rt = _new_runtime(tmp_path)

        async def feeder():
            while not fake_spawn:
                await asyncio.sleep(0.001)
            fake_spawn[0].stdout.feed(_inited_line())

        feed_task = asyncio.create_task(feeder())
        await rt.start()
        await feed_task
        try:
            await rt.music_url({"songmid": "1"}, "128k", platform="kw", timeout=0.05)
        except sr.SourceError as exc:
            return exc
        return None

    exc = asyncio.run(run())
    assert exc is not None and exc.category == "resolve"
    assert "超时" in str(exc)


def test_user_source_rejects_non_http_result(fake_spawn, tmp_path):
    async def run():
        rt = _new_runtime(tmp_path)

        async def feeder():
            while not fake_spawn:
                await asyncio.sleep(0.001)
            fake_spawn[0].stdout.feed(_inited_line())

        feed_task = asyncio.create_task(feeder())
        await rt.start()
        await feed_task
        resp_task = asyncio.create_task(_respond(fake_spawn[0], result="javascript:alert(1)"))
        try:
            await rt.music_url({"songmid": "1"}, "128k", platform="kw", timeout=2.0)
        except sr.SourceError as exc:
            await resp_task
            return exc
        await resp_task
        return None

    exc = asyncio.run(run())
    assert exc is not None and exc.category == "resolve"


def test_user_source_init_status_false(fake_spawn, tmp_path):
    async def run():
        rt = _new_runtime(tmp_path)

        async def feeder():
            while not fake_spawn:
                await asyncio.sleep(0.001)
            fake_spawn[0].stdout.feed(_inited_line(status=False))

        feed_task = asyncio.create_task(feeder())
        try:
            await rt.start()
        except sr.SourceError as exc:
            return exc
        finally:
            await feed_task
        return None

    exc = asyncio.run(run())
    assert exc is not None and exc.category == "init"
    assert "status" in str(exc)


def test_user_source_init_timeout(fake_spawn, tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "INIT_TIMEOUT_S", 0.05)
    state = {}

    async def run():
        rt = _new_runtime(tmp_path)
        try:
            await rt.start()
        except sr.SourceError as exc:
            state["exc"] = exc
            state["proc"] = fake_spawn[0] if fake_spawn else None

    asyncio.run(run())
    assert state["exc"].category == "init"
    proc = state["proc"]
    assert proc is not None and proc.returncode is not None  # 已 terminate


def test_user_source_no_music_platform(fake_spawn, tmp_path):
    async def run():
        rt = _new_runtime(tmp_path)

        async def feeder():
            while not fake_spawn:
                await asyncio.sleep(0.001)
            # 只声明歌词 action：无 musicUrl → 不可用
            fake_spawn[0].stdout.feed(_inited_line(sources={
                "kw": {"name": "kw", "actions": ["lyric"], "qualitys": ["128k"]},
            }))

        feed_task = asyncio.create_task(feeder())
        try:
            await rt.start()
        except sr.SourceError as exc:
            await feed_task
            return exc
        await feed_task
        return None

    exc = asyncio.run(run())
    assert exc is not None and exc.category == "init"


def test_user_source_process_exit_fails_pending(fake_spawn, tmp_path):
    async def run():
        rt = _new_runtime(tmp_path)

        async def feeder():
            while not fake_spawn:
                await asyncio.sleep(0.001)
            fake_spawn[0].stdout.feed(_inited_line())

        feed_task = asyncio.create_task(feeder())
        await rt.start()
        await feed_task
        proc = fake_spawn[0]
        call = asyncio.create_task(rt.music_url({"songmid": "1"}, "128k", platform="kw", timeout=3.0))
        while not proc.lines_in:
            await asyncio.sleep(0.001)
        proc.stdout.eof()  # Node 进程退出
        try:
            await call
        except sr.SourceError as exc:
            return exc
        return None

    exc = asyncio.run(run())
    assert exc is not None and exc.category == "resolve"


def test_user_source_stop_cleans_script(fake_spawn, tmp_path):
    async def run():
        rt = _new_runtime(tmp_path)

        async def feeder():
            while not fake_spawn:
                await asyncio.sleep(0.001)
            fake_spawn[0].stdout.feed(_inited_line())

        feed_task = asyncio.create_task(feeder())
        await rt.start()
        await feed_task
        script_path = rt._script_path
        assert script_path is not None and script_path.exists()
        await rt.stop()
        return script_path

    script_path = asyncio.run(run())
    assert not script_path.exists()


# ------------------------------------------------------------------ SourceManager ---

class FakeUserSource:
    instances: list["FakeUserSource"] = []

    def __init__(self, script: str, meta: dict, *, script_dir=None):
        self.script = script
        self.meta = meta
        self.stopped = False
        self.running = True
        FakeUserSource.instances.append(self)

    async def start(self):
        return None

    async def stop(self):
        self.stopped = True
        self.running = False

    def qualitys(self, platform):
        return ["128k", "320k", "flac"]

    def music_platforms(self):
        return ["kw"]

    async def music_url(self, info, quality, *, platform, timeout=10.0):
        return "https://media.test/a.flac"

    def describe(self):
        return {"name": self.meta.get("name"), "running": self.running}


@pytest.fixture
def fake_user_source(monkeypatch):
    FakeUserSource.instances = []
    monkeypatch.setattr(sr, "UserSource", FakeUserSource)

    async def fake_download(url):
        return STUB_SCRIPT

    monkeypatch.setattr(sr, "download_script", fake_download)
    return FakeUserSource


def test_manager_activate_persists_state(fake_user_source, tmp_path):
    mgr = sr.SourceManager(state_dir=str(tmp_path))
    asyncio.run(mgr.activate("https://src.test/one.js"))
    state = mgr.read_state()
    assert state["url"] == "https://src.test/one.js"
    assert mgr.script_cache.read_text(encoding="utf-8") == STUB_SCRIPT
    assert mgr.active_url == "https://src.test/one.js"
    assert mgr.get() is FakeUserSource.instances[0]

    # 二次激活：旧运行时停用
    asyncio.run(mgr.activate("https://src.test/two.js"))
    assert FakeUserSource.instances[0].stopped is True
    assert mgr.get() is FakeUserSource.instances[1]
    assert mgr.read_state()["url"] == "https://src.test/two.js"


def test_manager_load_prefers_state_over_seed(fake_user_source, tmp_path, monkeypatch):
    mgr = sr.SourceManager(state_dir=str(tmp_path), seed_url="https://src.test/seed.js")
    mgr.state_path.write_text(json.dumps({"url": "https://src.test/state.js"}), encoding="utf-8")
    asyncio.run(mgr.load())
    assert mgr.active_url == "https://src.test/state.js"


def test_manager_load_falls_back_to_cached_script(fake_user_source, tmp_path, monkeypatch):
    async def failing_download(url):
        raise sr.SourceError("download", "下载失败: network down")

    monkeypatch.setattr(sr, "download_script", failing_download)
    mgr = sr.SourceManager(state_dir=str(tmp_path), seed_url="https://src.test/gone.js")
    # 上次激活留下的缓存脚本
    mgr.state_path.write_text(json.dumps({"url": "https://src.test/gone.js"}), encoding="utf-8")
    mgr.script_cache.write_text(STUB_SCRIPT, encoding="utf-8")
    asyncio.run(mgr.load())
    assert mgr.get() is not None
    assert mgr.last_error.startswith("download:")


def test_manager_load_without_anything_is_silent(fake_user_source, tmp_path):
    mgr = sr.SourceManager(state_dir=str(tmp_path))
    asyncio.run(mgr.load())
    assert mgr.get() is None
    assert mgr.active_url == ""
    assert mgr.last_error == ""


def test_manager_get_returns_none_after_stop(fake_user_source, tmp_path):
    mgr = sr.SourceManager(state_dir=str(tmp_path))
    asyncio.run(mgr.activate("https://src.test/one.js"))
    runtime = mgr.get()
    asyncio.run(mgr.shutdown())
    assert mgr.get() is None


def test_manager_state_dir_fallback_when_unwritable(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    mgr = sr.SourceManager(state_dir=str(blocker / "sub"))
    assert mgr.state_dir != blocker / "sub"
    assert mgr.state_dir.exists()


# ------------------------------------------------------------------ Node 集成 ---

@pytest.mark.skipif(NODE_BIN is None, reason="node runtime not available")
def test_bridge_end_to_end_with_stub_script(tmp_path):
    stub = (
        "/*!\n"
        " * @name test-source\n"
        " * @version 1.0.0\n"
        " * @author tester\n"
        " * @description node sandbox integration stub\n"
        " */\n"
        "lx.on(lx.EVENT_NAMES.request, ({ source, action, info }) => {\n"
        "  if (action !== 'musicUrl') return Promise.reject(new Error('unsupported action'))\n"
        "  return new Promise((resolve) => {\n"
        "    setTimeout(() => resolve('https://example.com/' + source + '/' + info.type + '.flac'), 5)\n"
        "  })\n"
        "})\n"
        "lx.send(lx.EVENT_NAMES.inited, {\n"
        "  status: true,\n"
        "  sources: { kw: { name: '酷我', actions: ['musicUrl'], qualitys: ['128k', '320k', 'flac'] } },\n"
        "})\n"
        "console.log('stub source booted')\n"
    )

    async def run():
        meta = sr.parse_script_meta(stub)
        rt = sr.UserSource(stub, meta, script_dir=str(tmp_path))
        try:
            await rt.start()
            assert rt.music_platforms() == ["kw"]
            assert rt.qualitys("kw") == ["128k", "320k", "flac"]
            url = await rt.music_url(
                {"songmid": "42", "name": "晴天", "singer": "周杰伦", "interval": "04:29"},
                "320k", platform="kw", timeout=10.0,
            )
            assert url == "https://example.com/kw/320k.flac"
        finally:
            await rt.stop()

    asyncio.run(run())
