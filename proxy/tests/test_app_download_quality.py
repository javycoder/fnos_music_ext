"""测试 App 下载音质贯穿、旧协议 320k 与新标准 128k 分离、转码及缓存身份隔离、非伪造无损。"""
import os
import pytest
from starlette.testclient import TestClient
from starlette.requests import Request
import httpx

from proxy.app import (
    CONF, app, _DL_TASKS, _dl_effective_quality,
    _dl_transcode_path, _dl_produce,
)
from proxy import quality_policy as qp
from proxy import transcode as tc


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


def test_dl_prepare_records_target_quality_and_path_isolation(monkeypatch, tmp_path):
    """验证 prepare 端点贯穿 target_quality 并对转码产物路径按音质隔离。"""
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path))
    monkeypatch.setitem(CONF, "download_quality", "app")
    monkeypatch.setitem(CONF, "play_quality", "lossless")
    monkeypatch.setattr("proxy.app._source_enabled", lambda guid: True)

    path_320 = _dl_transcode_path("online:wy:1001", "exhigh")
    path_128 = _dl_transcode_path("online:wy:1001", "standard")
    assert path_320 != path_128, "不同音质的下载产物路径必须隔离，防止互相串缓存"
    assert "exhigh" in path_320 or "320" in path_320
    assert "standard" in path_128 or "128" in path_128

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


@pytest.mark.anyio
async def test_dl_produce_never_fakes_lossless_from_mp3(tmp_path, monkeypatch):
    """不要把低码率转 FLAC 伪称无损：当拉到的源是 mp3 时，即使目标为 lossless，也不转成 flac。"""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setitem(CONF, "cache_dir", str(cache_dir))

    # 创建一个真实的假 mp3 文件
    mp3_file = cache_dir / "test_track.mp3"
    mp3_file.write_bytes(b"ID3" + b"\x00" * 2048)

    monkeypatch.setattr("proxy.app.find_cache_file", lambda guid, **kwargs: str(mp3_file))
    monkeypatch.setattr("proxy.app._source_enabled", lambda guid: True)

    task = {
        "guid": "online:wy:FAKE_MP3",
        "quality": "lossless",
        "state": "waiting",
        "percent": 0,
        "downloadId": "task_lossless_from_mp3",
    }
    await _dl_produce(task, {})

    assert task["state"] == "completed"
    assert task["path"] == str(mp3_file), "源为 MP3 时交付 MP3，严禁转码成伪 FLAC"
    assert not task["path"].endswith(".flac")


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
