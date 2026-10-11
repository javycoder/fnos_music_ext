"""测试全局守卫：默认禁用真 ffmpeg，防止用例意外转码真实在线源。

需要验证转码路径的用例自行 monkeypatch transcode.FFMPEG_BIN 指向假实现
（tests 内置 fake_ffmpeg 脚本），其余用例一律走无 ffmpeg 的回落分支。
"""
import pytest

from proxy import transcode as tc


@pytest.fixture(autouse=True)
def _no_real_ffmpeg(monkeypatch):
    monkeypatch.setattr(tc, "FFMPEG_BIN", None)
    monkeypatch.setattr(tc, "FFPROBE_BIN", None)
    # 隔离会话注册表与并发槽，避免跨用例串扰
    monkeypatch.setattr(tc, "_SESSIONS", {})
    monkeypatch.setattr(tc, "_ACTIVE", 0)
    monkeypatch.setattr(tc, "_SLOT_COND", None)


@pytest.fixture(autouse=True)
def _isolate_cover_state(monkeypatch):
    """隔离封面 guid→直链 映射与 32hex 反解取证去重表（模块级内存状态，
    不隔离会让先跑用例的映射让后续用例跳过 mock 音源、甚至真实出网）。"""
    from proxy import app as pa

    monkeypatch.setattr(pa, "_COVER_GUID_URL_MEM", {})
    monkeypatch.setattr(pa, "_COVER_UNRESOLVED_SEEN", set())
