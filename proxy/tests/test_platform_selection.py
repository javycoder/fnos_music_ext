"""「源+平台」粒度选择：lx 平台白名单过滤 / fetch_lx_search sources 透传 /
lx 榜单平台交集 / CONF 读取 LX_SOURCES。musicdl 平台白名单机制此前已存在，
此处仅回归其对新短名格式白名单的兼容。"""
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from proxy.app import CONF, _normalize_lx_sources, _source_enabled, _source_config, fetch_lx_search
from proxy import recommend as dailyrec

BASE = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- 归一化 --
def test_normalize_lx_sources():
    assert _normalize_lx_sources("") == []
    assert _normalize_lx_sources("kugou, kuwo,kugou") == ["kg", "kw"]
    assert _normalize_lx_sources("163") == ["wy"]
    assert _normalize_lx_sources("qq,tencent,tx") == ["tx"]
    assert _normalize_lx_sources("bogus,,") == []


def test_source_config_includes_platform_keys():
    cfg = _source_config()
    assert "online_sources" in cfg and "lx_sources" in cfg


# ---------------------------------------------------------------- 过滤 --
def test_source_enabled_lx_platform_filter(monkeypatch):
    monkeypatch.setitem(CONF, "lx_enabled", True)
    monkeypatch.setitem(CONF, "lx_sources", ["kw"])
    assert _source_enabled("online:lx:kw:ABC") is True
    assert _source_enabled("online:lx:kg:ABC") is False
    # 白名单为空 = 不限制（跟随 lx 服务配置）
    monkeypatch.setitem(CONF, "lx_sources", [])
    assert _source_enabled("online:lx:kg:ABC") is True
    # 源级开关仍然优先
    monkeypatch.setitem(CONF, "lx_enabled", False)
    assert _source_enabled("online:lx:kw:ABC") is False


def test_source_enabled_musicdl_short_name_whitelist(monkeypatch):
    """install.sh 新写入的短名白名单与旧全名格式行为一致。"""
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "online_sources", "kuwo,gequhai")
    assert _source_enabled("online:kuwo:1") is True
    assert _source_enabled("online:gequhai:1") is True
    assert _source_enabled("online:qq:1") is False
    monkeypatch.setitem(CONF, "online_sources", "KuwoMusicClient")
    assert _source_enabled("online:kuwo:1") is True
    assert _source_enabled("online:migu:1") is False


# ---------------------------------------------------------------- 透传 --
@pytest.mark.anyio
async def test_fetch_lx_search_sources_passthrough(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True, "items": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8772")
    try:
        monkeypatch.setitem(CONF, "lx_sources", ["kg", "wy"])
        await fetch_lx_search(client, "晴天", 5)
        assert seen["params"].get("sources") == "kg,wy"  # 默认回退 CONF 白名单
        await fetch_lx_search(client, "晴天", 5, sources=["kw"])
        assert seen["params"].get("sources") == "kw"  # 显式列表
        await fetch_lx_search(client, "晴天", 5, sources="kw")
        assert seen["params"].get("sources") == "kw"  # 显式逗号串
        await fetch_lx_search(client, "晴天", 5, sources=[])
        assert "sources" not in seen["params"]  # 空列表 = 不限制
        monkeypatch.setitem(CONF, "lx_sources", [])
        await fetch_lx_search(client, "晴天", 5)
        assert "sources" not in seen["params"]  # 未配置白名单 = 跟随 lx 服务
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_fetch_lx_charts_platform_intersection():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True, "items": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8772")
    try:
        await dailyrec.fetch_lx_charts(client, 10, sources=["kg", "mg", "kw"])
        assert seen["params"].get("sources") == "kg,kw"  # mg 无榜单被剔除
        seen.clear()
        assert await dailyrec.fetch_lx_charts(client, 10, sources=["mg", "tx"]) == []
        assert seen == {}  # 交集为空直接跳过，不发请求
        await dailyrec.fetch_lx_charts(client, 10)
        assert "sources" not in seen["params"]  # None = 不限制
    finally:
        await client.aclose()


# ---------------------------------------------------------------- CONF 读取 --
def _conf_probe(tmp_path, extra_env):
    env = os.environ.copy()
    env.pop("LX_SOURCES", None)  # 隔离宿主环境，未设置场景可测
    env.update(FNMUSIC_HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    for key in ("FNMUSIC_CACHE_DIR", "FNMUSIC_FAV_DIR", "FNMUSIC_RECOMMEND_DIR",
                "FNMUSIC_PLAY_HISTORY_DIR", "FNMUSIC_LIBRARY_DIR"):
        env[key] = str(tmp_path / key)
    env["FNMUSIC_MUSIC_DB"] = str(tmp_path / "unused.db")
    env.update(extra_env)
    code = 'import app;print(",".join(app.CONF["lx_sources"]))'
    return subprocess.check_output(
        [sys.executable, "-B", "-c", code], env=env, cwd=str(BASE / "proxy"), text=True
    ).strip()


def test_conf_reads_lx_sources_env(tmp_path):
    assert _conf_probe(tmp_path, {"LX_SOURCES": "kugou, kuwo"}) == "kg,kw"
    assert _conf_probe(tmp_path, {}) == ""  # 未设置 = 不限制
