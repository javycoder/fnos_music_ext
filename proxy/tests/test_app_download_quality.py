"""测试 App 下载音质贯穿、旧协议 320k 与新标准 128k 分离、转码及缓存身份隔离、非伪造无损。"""
import os
import pytest
from starlette.testclient import TestClient
from starlette.requests import Request
import httpx

import proxy.app as appmod
from proxy.app import (
    CONF, app, _DL_TASKS, _dl_effective_quality,
    _dl_transcode_path, _dl_produce,
)
from proxy import quality_policy as qp
from proxy import transcode as tc


@pytest.mark.parametrize('actual', ['', 'unknown'])
def test_lx_unknown_actual_quality_is_not_replaced_by_request_target(actual):
    # LX响应quality是请求档；actual_quality为空时不能把sky当成真实品质。
    payload = {'quality': 'sky', 'actual_quality': actual, 'actual_tier': '', 'br': 0}
    assert appmod._netease_quality_evidence(payload) == 'unknown'


@pytest.mark.anyio
async def test_lx_download_ignores_playback_lossless_blacklist(monkeypatch):
    captured = {}
    class Response:
        status_code = 200
        headers = {'content-type': 'audio/mpeg'}
        async def aiter_bytes(self):
            yield b'fLaC' + b'\x00' * 2044
        async def aclose(self):
            pass
    class Client:
        def build_request(self, method, url, **kwargs):
            return httpx.Request(method, url)
        async def send(self, request, **kwargs):
            return Response()
        async def aclose(self):
            pass
    async def resolve(*args, **kwargs):
        captured.update(kwargs)
        return {'url': 'https://media.test/dl.flac', 'ext': 'flac', 'actual_quality': 'lossless'}
    monkeypatch.setattr(appmod, '_retained_track', lambda *args: ({'ext': 'flac'}, None))
    monkeypatch.setattr(appmod, '_lossless_is_blacklisted', lambda *args: True)
    monkeypatch.setattr(appmod, 'get_lx_client', lambda *args: object())
    monkeypatch.setattr(appmod, 'resolve_lx_url_shared', resolve)
    monkeypatch.setattr(appmod.httpx, 'AsyncClient', lambda **kwargs: Client())
    opened = await appmod._open_online_stream(
        appmod._synth_request({}), 'online:lx:kw:123', None,
        quality_override='lossless', purpose='download', generation=7, include_quality=True)
    try:
        assert captured['max_quality'] is None
        assert captured['quality_override'] == 'lossless'
        assert opened[2] == 'flac'
        assert opened[-1] == 'lossless'
    finally:
        await opened[0].aclose()
        await opened[1].aclose()


@pytest.mark.anyio
async def test_download_cancellation_reaps_encoder(tmp_path, monkeypatch):
    import asyncio
    started, exited = asyncio.Event(), asyncio.Event()
    killed, closed = [], []
    class Response:
        headers = {'content-length': '2048'}
        async def aclose(self):
            closed.append(True)
    async def chunks():
        yield b'b' * 1024
    async def stream(*args, **kwargs):
        return Response(), None, 'mp3', {}, chunks(), b'a' * 1024, 'exhigh'
    class Process:
        async def wait(self):
            started.set()
            await exited.wait()
            return -9
        def kill(self):
            killed.append(True)
            exited.set()
    async def spawn(*args, **kwargs):
        return Process()
    monkeypatch.setitem(CONF, 'cache_dir', str(tmp_path))
    monkeypatch.setattr(appmod, 'resolve_local_play', _async_none)
    monkeypatch.setattr(appmod, '_source_enabled', lambda *args: True)
    monkeypatch.setattr(appmod, '_open_online_stream', stream)
    monkeypatch.setattr(appmod.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(tc, 'FFMPEG_BIN', 'fixture-ffmpeg')
    task = {'guid': 'online:netease:CANCEL', 'quality': 'standard', 'downloadId': 'cancel'}
    producer = asyncio.create_task(_dl_produce(task, {}))
    await asyncio.wait_for(started.wait(), timeout=1)
    producer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await producer
    assert killed, '取消下载必须终止独立编码进程'
    assert closed
    assert not list(tmp_path.rglob('*.part'))


@pytest.mark.anyio
async def test_delete_cancels_active_download_and_preserves_original_file(tmp_path, monkeypatch):
    import asyncio
    original = tmp_path / 'original.flac'
    original.write_bytes(b'original-library-data')
    producer = asyncio.create_task(asyncio.Event().wait())
    task = {'guid': 'online:netease:DELETE', 'path': str(original), 'state': 'waiting',
            '_producer': producer, 'touched': 0}
    monkeypatch.setitem(_DL_TASKS, 'fixture-delete', task)
    request = appmod._synth_request({})
    request.scope['query_string'] = b'downloadId=fixture-delete'
    try:
        response = await appmod.dl_transcode_delete(request)
        assert response.status_code == 200
        assert producer.cancelling(), '删除活动下载必须取消生产任务'
        assert original.read_bytes() == b'original-library-data'
    finally:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)


def test_expired_download_cleans_only_recorded_owned_artifacts(tmp_path, monkeypatch):
    original, owned = tmp_path / 'original.flac', tmp_path / 'owned.mp3'
    original.write_bytes(b'original')
    owned.write_bytes(b'owned')
    expired_at = appmod.time.monotonic() - appmod._DL_TASK_TTL_S - 1
    monkeypatch.setitem(_DL_TASKS, 'expired-owned', {
        'state': 'completed', 'path': str(owned), '_owned_paths': {str(owned)}, 'touched': expired_at})
    monkeypatch.setitem(_DL_TASKS, 'expired-original', {
        'state': 'completed', 'path': str(original), 'touched': expired_at})
    appmod._dl_sweep()
    assert not owned.exists()
    assert original.read_bytes() == b'original'


@pytest.mark.anyio
@pytest.mark.parametrize('content_range, decode_ok, completed', [
    ('bytes 1024-3071/8192', True, False),
    ('', False, False),
    ('bytes 0-2047/2048', True, True),
])
async def test_download_requires_complete_representation_and_valid_lossless(
    tmp_path, monkeypatch, content_range, decode_ok, completed,
):
    closed = []
    class Response:
        status_code = 206 if content_range else 200
        headers = {'content-length': '2048', 'content-range': content_range}
        async def aclose(self):
            closed.append(True)
    async def chunks():
        yield b'\x00' * 1024
    async def stream(*args, **kwargs):
        return Response(), None, 'flac', {}, chunks(), b'fLaC' + b'\x00' * 1020, 'lossless'
    monkeypatch.setitem(CONF, 'cache_dir', str(tmp_path))
    monkeypatch.setattr(appmod, 'resolve_local_play', _async_none)
    monkeypatch.setattr(appmod, '_source_enabled', lambda *args: True)
    monkeypatch.setattr(appmod, '_open_online_stream', stream)
    monkeypatch.setattr(appmod, '_check_audio_file', lambda path: (decode_ok, 'ok' if decode_ok else 'decode_error'))
    task = {'guid': 'online:lx:kw:COMPLETE', 'quality': 'lossless', 'downloadId': 'complete'}
    await _dl_produce(task, {})
    assert (task['state'] == 'completed') is completed
    assert closed
    if not completed:
        assert not list(tmp_path.rglob('*.part'))
        assert not task.get('path')


def test_dl_effective_quality_semantics(monkeypatch):
    """测试 App 下载档位解释：
    - 旧 App standard 解释为 exhigh (320k)
    - 显式 original / is_original 解释为 sky (最高可用)
    - 未知 / 空值跟随当前生效播放档
    - 新显式七档支持原样规范化
    """
    monkeypatch.setitem(CONF, "play_quality", "hires")
    monkeypatch.setitem(CONF, "download_quality", "app")

    # 1. 官方旧 App 协议 standard -> 320k (exhigh)
    assert _dl_effective_quality("standard", False) == "exhigh"
    assert _dl_effective_quality("std", False) == "exhigh"
    assert _dl_effective_quality("mp3", False) == "exhigh"

    # 2. 显式 original -> 最高可用 (sky)
    assert _dl_effective_quality("original", False) == "sky"
    assert _dl_effective_quality("", True) == "sky"

    # 3. 未知或缺省 -> 跟随播放档 (hires)
    assert _dl_effective_quality("", False) == "hires"
    assert _dl_effective_quality("unknown_foo", False) == "hires"

    # 4. 新显式档位
    assert _dl_effective_quality("higher", False) == "higher"
    assert _dl_effective_quality("lossless", False) == "lossless"


def test_explicit_app_download_policy_ignores_legacy_override(monkeypatch):
    """新键 app 必须优先旧档位；旧 original 只迁移到 lossless。"""
    monkeypatch.setitem(CONF, "download_quality", "app")
    monkeypatch.setitem(CONF, "dl_quality", "standard")
    monkeypatch.setitem(CONF, "play_quality", "hires")

    assert _dl_effective_quality("higher", False) == "higher"
    assert _dl_effective_quality("original", False) == "sky"

    monkeypatch.setitem(CONF, "download_quality", None)
    monkeypatch.setitem(CONF, "dl_quality", "original")
    assert _dl_effective_quality("", False) == "lossless"


def test_dl_prepare_records_target_quality_and_path_isolation(monkeypatch, tmp_path):
    """验证 prepare 端点贯穿 target_quality 并对转码产物路径按音质隔离。"""
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path))
    monkeypatch.setitem(CONF, "download_quality", "app")
    monkeypatch.setitem(CONF, "play_quality", "lossless")
    monkeypatch.setattr("proxy.app._source_enabled", lambda guid: True)
    monkeypatch.setattr(appmod, "_dl_produce", _async_none)
    generation = appmod._DL_CONFIG_GENERATION

    path_320 = _dl_transcode_path("online:wy:1001", "exhigh")
    path_128 = _dl_transcode_path("online:wy:1001", "standard")
    assert path_320 != path_128, "不同音质的下载产物路径必须隔离，防止互相串缓存"
    assert "exhigh" in path_320 or "320" in path_320
    assert "standard" in path_128 or "128" in path_128
    path_old_generation = _dl_transcode_path(
        "online:wy:1001", "standard", purpose="download", generation=7, identity="task-a")
    path_new_generation = _dl_transcode_path(
        "online:wy:1001", "standard", purpose="download", generation=8, identity="task-b")
    assert path_old_generation != path_new_generation

    with TestClient(app) as client:
        # App 请求 standard
        resp = client.post("/music/api/v1/download/track/transcode/prepare", json={
            "guid": "online:wy:1001", "quality": "standard", "isOriginal": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        dl_id = data["data"]["downloadId"]
        task = _DL_TASKS[dl_id]
        assert task["quality"] == "exhigh", "下载任务必须记录目标音质 exhigh"
        assert task["target_quality"] == "exhigh"
        assert task["provider"] == "musicdl"
        assert task["purpose"] == "download"
        assert task["generation"] == generation


@pytest.mark.anyio
async def test_dl_produce_keeps_existing_local_mp3_without_reencoding(tmp_path, monkeypatch):
    """已有完整本地曲库文件保持原样，不因 App 目标档触发重编码。"""
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path / "cache"))

    mp3_file = library / "test_track.mp3"
    mp3_file.write_bytes(b"ID3" + b"\x00" * 2048)

    async def local_track(request, guid, probe=True):
        return {"path": str(mp3_file), "size": mp3_file.stat().st_size, "ext": "mp3"}

    async def unexpected_open(*args, **kwargs):
        raise AssertionError("本地完整曲库命中后不应再请求在线源")

    monkeypatch.setattr(appmod, "resolve_local_play", local_track)
    monkeypatch.setattr(appmod, "_open_online_stream", unexpected_open)

    task = {
        "guid": "online:wy:FAKE_MP3", "quality": "lossless",
        "state": "waiting", "percent": 0, "downloadId": "task_lossless_from_mp3",
    }
    await _dl_produce(task, {})

    assert task["state"] == "completed"
    assert task["path"] == str(mp3_file)
    assert not task["path"].endswith(".flac")
    assert task["actual_quality"] == "unknown"


@pytest.mark.anyio
async def test_transcode_session_bitrate_isolation(tmp_path, monkeypatch):
    """转码会话缓存必须按比特率隔离：128k 缓存绝不能被 320k 会话冒领复用。"""
    root = str(tmp_path)
    sess_dir = tc.session_dir(root, "GUID_BITRATE_TEST")
    os.makedirs(sess_dir, exist_ok=True)

    # 磁盘上已存在一个 128k 完成状态的转码会话
    (tmp_path / "hls" / "GUID_BITRATE_TEST" / tc.INIT_NAME).write_bytes(b"init_data")
    (tmp_path / "hls" / "GUID_BITRATE_TEST" / "00000.m4s").write_bytes(b"seg_0")
    dummy_sess = tc.Session(
        guid="GUID_BITRATE_TEST", directory=sess_dir, duration_s=10.0,
        hls_time=10.0, declared_count=1, bitrate="128k", status="done",
    )
    tc._write_state(dummy_sess)

    # 尝试以 320k 请求该 session
    monkeypatch.setattr(tc, "FFMPEG_BIN", None)  # 阻止启动新进程
    sess = await tc.ensure_session(
        "GUID_BITRATE_TEST", "http://audio/test.mp3", 10.0,
        root=root, bitrate="320k",
    )
    # 320k 请求不能复用 128k 的旧缓存；因为 FFMPEG_BIN 为 None，应返回 None 而不是复用 128k
    assert sess is None, "不同比特率的转码缓存不得串用"


@pytest.mark.anyio
async def test_dl_produce_uses_task_target_and_never_reuses_playback_cache(tmp_path, monkeypatch):
    """App 下载必须独立解析目标档，不能把同 GUID 的播放缓存当下载结果。"""
    guid = "online:netease:DL_TARGET_TEST"
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setitem(CONF, "cache_dir", str(cache_dir))
    monkeypatch.setattr(appmod, "resolve_local_play", _async_none)
    monkeypatch.setattr(appmod, "_source_enabled", lambda requested_guid: True)

    playback_cache = cache_dir / f"{appmod.cache_safe_guid(guid)}.mp3"
    playback_cache.write_bytes(b"playback-128k")
    monkeypatch.setattr(appmod, "find_cache_file", lambda *args, **kwargs: str(playback_cache))

    calls = []

    class Response:
        headers = {"content-length": "2048"}

        async def aclose(self):
            return None

    async def chunks():
        yield b"b" * 1024

    async def open_stream(request, requested_guid, range_header, **kwargs):
        calls.append((requested_guid, range_header, kwargs))
        return Response(), None, "mp3", {}, chunks(), b"a" * 1024, "exhigh"

    monkeypatch.setattr(appmod, "_open_online_stream", open_stream)

    ffmpeg_calls = []

    class Process:
        async def wait(self):
            with open(ffmpeg_calls[-1][-1], "wb") as output:
                output.write(b"encoded-standard")
            return 0

    async def create_process(*args, **kwargs):
        ffmpeg_calls.append(args)
        return Process()

    monkeypatch.setattr(tc, "FFMPEG_BIN", "ffmpeg-test")
    monkeypatch.setattr(appmod.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(appmod, "_full_fetch_download", _unexpected_full_fetch)

    task = {
        "guid": guid, "quality": "standard", "provider": "netease",
        "generation": 9, "downloadId": "task-9",
        "state": "waiting", "percent": 0,
    }
    await _dl_produce(task, {})

    assert task["state"] == "completed"
    assert task["path"] != str(playback_cache)
    assert calls[0][2] == {
        "quality_override": "standard", "purpose": "download", "generation": 9,
        "include_quality": True,
    }
    assert "-b:a" in ffmpeg_calls[0]
    assert ffmpeg_calls[0][ffmpeg_calls[0].index("-b:a") + 1] == "128k"
    assert task["actual_quality"] == "standard"


async def _async_none(*args, **kwargs):
    return None


async def _unexpected_full_fetch(*args, **kwargs):
    raise AssertionError("App 下载不得复用 playback 的整轨下载与 GUID 缓存")


@pytest.mark.anyio
async def test_netease_download_resolution_isolated_by_quality_and_purpose(monkeypatch):
    """网易下载 URL 按目标档与用途缓存，不能复用不同请求的播放 URL。"""
    requests = []

    def handler(request):
        quality = request.url.params["quality"]
        requests.append((quality, request.url.params.get("purpose")))
        return httpx.Response(200, json={
            "ok": True,
            "data": {"code": 200, "url": f"https://cdn.test/{quality}.mp3", "level": quality},
        })

    async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://musicbox.test") as client:
        lossless = await appmod._resolve_netease_download_media(
            client, "7788", "lossless", purpose="download", generation=4)
        standard = await appmod._resolve_netease_download_media(
            client, "7788", "standard", purpose="download", generation=4)
        cached_standard = await appmod._resolve_netease_download_media(
            client, "7788", "standard", purpose="download", generation=4)
        playback_standard = await appmod._resolve_netease_download_media(
            client, "7788", "standard", purpose="playback", generation=4)
        new_generation = await appmod._resolve_netease_download_media(
            client, "7788", "standard", purpose="download", generation=5)

    assert lossless["url"].endswith("lossless.mp3")
    assert standard["url"].endswith("standard.mp3")
    assert cached_standard == standard
    assert playback_standard == standard
    assert new_generation == standard
    assert requests == [
        ("lossless", "download"), ("standard", "download"),
        ("standard", "playback"), ("standard", "download"),
    ]


@pytest.mark.anyio
async def test_netease_missing_quality_evidence_stays_unknown(monkeypatch):
    """网易结果没有真实档位字段时不得把请求档或扩展名伪作实际音质。"""
    def handler(request):
        return httpx.Response(200, json={
            "ok": True, "data": {"code": 200, "url": "https://cdn.test/unknown.flac"},
        })

    async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://musicbox.test") as client:
        result = await appmod._resolve_netease_download_media(
            client, "7789", "sky", purpose="download", generation=4)

    assert result["actual_quality"] == "unknown"
    assert result["ext"] == ""


@pytest.mark.anyio
async def test_lx_legacy_config_sends_only_shared_first_quality(monkeypatch):
    """旧配置也只向 LX 服务发送共享策略算出的首档，由服务侧负责降档。"""
    seen = []

    def handler(request):
        seen.append(request.url.params.get("quality"))
        return httpx.Response(404, json={"ok": False, "data": None})

    monkeypatch.setitem(CONF, "play_quality", None)
    monkeypatch.setitem(CONF, "lx_enabled", True)
    monkeypatch.setitem(CONF, "quality_mode", "balanced")
    monkeypatch.setitem(CONF, "lx_quality", "lossless")
    async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://lx.test") as client:
        result = await appmod.resolve_lx_url(
            client, "lx:kg:LEGACY_SINGLE", quality_override="higher",
            purpose="download", generation=3)

    assert result is None
    assert seen == ["192k"]


@pytest.mark.anyio
@pytest.mark.parametrize("target,expect_mp3", [("standard", True), ("sky", False)])
async def test_musicdl_download_requests_only_supported_lossy_hint(monkeypatch, target, expect_mp3):
    """musicdl 只接收已支持的 mp3 提示，缺少证据时实际质量仍为 unknown。"""
    captured = {}

    class Response:
        status_code = 200
        headers = {"content-type": "audio/mpeg", "content-length": "2048"}

        async def aiter_bytes(self):
            yield b"a" * 1024
            yield b"b" * 1024

        async def aclose(self):
            return None

    class Client:
        def build_request(self, method, url, params=None, headers=None):
            captured.update(params or {})
            return httpx.Request(method, f"http://musicdl.test{url}")

        async def send(self, request, stream=False):
            return Response()

    monkeypatch.setattr(appmod, "get_musicdl_client", lambda application: Client())
    monkeypatch.setattr(appmod, "_lossless_is_blacklisted", lambda *args: True)
    monkeypatch.setattr(appmod, "_retained_track", lambda *args: ({"ext": "mp3"}, None))
    opened = await appmod._open_online_stream(
        appmod._synth_request({}), "online:musicdl:123", None,
        quality_override=target, purpose="download", generation=2, include_quality=True,
    )

    assert (captured.get("quality") == "mp3") is expect_mp3
    assert opened[2] == "mp3"
    assert opened[-1] == "unknown"
    await opened[0].aclose()
