"""musicdl 在线流冷路径：首次取流超时允许重试一次。

回归背景：musicdl 首次解析直链 + 无损档解码探针（3MB 下载 + ffmpeg 校验）可能
超过代理单次尝试预算（`_STREAM_ATTEMPT_S=6s`），而重试分支此前只对 netease 生效，
musicdl 直接 break → 404「搜得到却播不了」，用户要点两次才响。musicdl 侧会缓存
URL 与探针结果，第二次尝试即可出流。
"""
import asyncio

import pytest
from starlette.requests import Request

from proxy import app as proxy


def _request(guid: str) -> Request:
    return Request({"type": "http", "app": proxy.app, "method": "GET",
                    "headers": [], "path": "/music/api/v1/track/stream",
                    "query_string": f"guid={guid}".encode()})


def _patch_common(monkeypatch, opener):
    monkeypatch.setattr(proxy, "_open_online_stream", opener)
    monkeypatch.setattr(proxy, "_source_enabled", lambda *args: True)
    monkeypatch.setattr(proxy, "_retained_track", lambda *args: (None, None))
    monkeypatch.setattr(proxy, "_register_full_fetch", lambda *args: None)
    monkeypatch.setattr(proxy, "stream_tee_response",
                        lambda *args, **kwargs: proxy.Response(b"audio-bytes"))


@pytest.mark.anyio
async def test_musicdl_timeout_retries_once_then_streams(monkeypatch):
    attempts = []

    async def opener(request, guid, range_header, **kwargs):
        attempts.append(guid)
        if len(attempts) == 1:
            raise asyncio.TimeoutError
        async def chunks():
            yield b"audio-bytes"
        return (None, None, "mp3", None, chunks(), b"audio-bytes")

    _patch_common(monkeypatch, opener)
    response = await proxy.stream_track(_request("online:kuwo:123"))
    assert response.status_code == 200
    assert attempts == ["online:kuwo:123", "online:kuwo:123"]


@pytest.mark.anyio
async def test_musicdl_retry_is_bounded_to_one_extra_attempt(monkeypatch):
    attempts = []

    async def opener(request, guid, range_header, **kwargs):
        attempts.append(guid)
        raise asyncio.TimeoutError

    _patch_common(monkeypatch, opener)
    response = await proxy.stream_track(_request("online:kuwo:123"))
    assert response.status_code == 404
    assert attempts == ["online:kuwo:123", "online:kuwo:123"]


@pytest.mark.anyio
async def test_musicdl_non_timeout_error_is_not_retried(monkeypatch):
    attempts = []

    async def opener(request, guid, range_header, **kwargs):
        attempts.append(guid)
        raise RuntimeError("bad gateway")

    _patch_common(monkeypatch, opener)
    response = await proxy.stream_track(_request("online:kuwo:123"))
    assert response.status_code == 404
    assert attempts == ["online:kuwo:123"]
