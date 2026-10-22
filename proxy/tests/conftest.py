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
    monkeypatch.setattr(tc, "_PLAYBACK_LEASES", {})
    monkeypatch.setattr(tc, "_SESSION_LOCKS", {})
    monkeypatch.setattr(tc, "_ACTIVE", 0)
    monkeypatch.setattr(tc, "_SLOT_COND", None)


@pytest.fixture(autouse=True)
def _isolate_cover_state(monkeypatch):
    """隔离封面 guid→直链 映射与 32hex 反解取证去重表（模块级内存状态，
    不隔离会让先跑用例的映射让后续用例跳过 mock 音源、甚至真实出网）。"""
    from proxy import app as pa

    monkeypatch.setattr(pa, "_COVER_GUID_URL_MEM", {})
    monkeypatch.setattr(pa, "_COVER_UNRESOLVED_SEEN", set())
    monkeypatch.setattr(pa, "_LX_URL_INFLIGHT", {})
    monkeypatch.setattr(pa, "_LX_URL_PREFETCH", set())
    monkeypatch.setattr(pa, "_LX_CONFIG_SIG", None)
    monkeypatch.setattr(pa, "_LX_RESOLVE_EPOCH", 0)


@pytest.fixture(autouse=True)
def _isolate_llm_usage_state(tmp_path, monkeypatch):
    """LLM token 账本指到临时文件并清空进程内当日缓存，
    防止用例把 llm_usage.json 写进真实仓库、或跨用例串账。"""
    from proxy import llm_usage

    monkeypatch.setenv("FNMUSIC_LLM_USAGE_FILE", str(tmp_path / "llm_usage.json"))
    monkeypatch.setattr(llm_usage, "_STATE", None)
    monkeypatch.setattr(llm_usage, "_STATE_DAY", "")
