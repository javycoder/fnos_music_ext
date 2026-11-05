"""测试全局守卫：默认禁用真 ffmpeg，防止用例意外转码真实在线源。

需要验证转码路径的用例自行 monkeypatch transcode.FFMPEG_BIN 指向假实现
（tests 内置 fake_ffmpeg 脚本），其余用例一律走无 ffmpeg 的回落分支。
"""
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from proxy import transcode as tc


@pytest.fixture(scope="session", autouse=True)
def _share_tls_context():
    """复用一份 CA 上下文，避免每次 httpx.AsyncClient 重新解析证书库。

    httpx 构造 Transport 时默认调用 create_ssl_context() 加载 certifi 的
    证书链；Windows 上单次约 1.5 秒。每个 TestClient 生命周期会新建 4~5 个
    客户端，数百个用例累计数十分钟。证书内容与校验语义不变，只是不再重复
    解析同一份 cacert.pem。仅在测试会话内生效。
    """
    import certifi
    from httpx import _transports

    cached = ssl.create_default_context(cafile=certifi.where())
    original = _transports.default.create_ssl_context

    def _create_ssl_context(verify=True, cert=None, trust_env=True):
        # 自定义 CA / verify=False 必须保持原语义，只复用默认 CA 那条路径。
        if verify is not True or cert is not None:
            return original(verify=verify, cert=cert, trust_env=trust_env)
        return cached

    _transports.default.create_ssl_context = _create_ssl_context
    try:
        yield
    finally:
        _transports.default.create_ssl_context = original


def _python3_works() -> bool:
    """Windows 的 `python3` 常命中应用执行别名，非交互调用会失败/挂起。"""
    exe = shutil.which("python3")
    if not exe:
        return False
    try:
        done = subprocess.run([exe, "-c", "print(1)"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and b"1" in done.stdout


@pytest.fixture(scope="session", autouse=True)
def _python3_shim():
    """给 bash 子进程提供可用的 python3，保持与 CI Linux 一致的脚本行为。

    安装脚本用 python3 解析 JSON；Windows 上该名字往往指向不可用的应用别名，
    会让脚本长期挂起。这里只在需要时把一个指向当前解释器的 shim 目录前置到
    PATH，不改动被测脚本本身。
    """
    if os.name != "nt" or _python3_works():
        yield
        return
    shim_dir = Path(tempfile.mkdtemp(prefix="fn-py3-"))
    shim = shim_dir / "python3"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    original = os.environ.get("PATH", "")
    os.environ["PATH"] = str(shim_dir) + os.pathsep + original
    try:
        yield
    finally:
        os.environ["PATH"] = original
        shutil.rmtree(shim_dir, ignore_errors=True)


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
    monkeypatch.setattr(llm_usage, "_MEM_LEDGER", None)
    monkeypatch.setattr(llm_usage, "_LAST_PATH", None)
    monkeypatch.setattr(llm_usage, "_STATUS", "ok")
    monkeypatch.setattr(llm_usage, "_STATUS_MSG", "正常")
