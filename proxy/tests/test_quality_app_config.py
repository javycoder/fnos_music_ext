"""测试 proxy 应用层的七档配置、旧配置兼容及热重载策略。"""
import pytest
from proxy.app import CONF, _env_watch_parse, _ENV_WATCH_KEYS
from proxy import quality_policy as qp


def test_play_and_download_quality_specs_registered():
    """验证 FNMUSIC_PLAY_QUALITY 与 FNMUSIC_DOWNLOAD_QUALITY 已注册到热重载白名单中。"""
    assert "FNMUSIC_PLAY_QUALITY" in _ENV_WATCH_KEYS
    assert "FNMUSIC_DOWNLOAD_QUALITY" in _ENV_WATCH_KEYS
    assert _ENV_WATCH_KEYS["FNMUSIC_PLAY_QUALITY"][0] == "play_quality"
    assert _ENV_WATCH_KEYS["FNMUSIC_DOWNLOAD_QUALITY"][0] == "download_quality"


def test_validate_quality_config_values():
    """验证配置项值的校验规则：仅允许合法七档，下载额外支持 app。"""
    # 播放档位必须是七档之一
    for level in ("standard", "higher", "exhigh", "lossless", "hires", "jymaster", "sky"):
        assert _env_watch_parse(level, "play_quality") == level
    assert _env_watch_parse("invalid_tier", "play_quality") is None

    # 下载档位支持 app 与七档
    assert _env_watch_parse("app", "download_quality") == "app"
    for level in ("standard", "higher", "exhigh", "lossless", "hires", "jymaster", "sky"):
        assert _env_watch_parse(level, "download_quality") == level
    assert _env_watch_parse("invalid_dl", "download_quality") is None


def test_effective_play_quality_prefers_new_and_falls_back_to_legacy(monkeypatch):
    """验证新配置优先；未显式配置时从旧 CONF 动态计算首档兼容。"""
    from proxy.app import effective_play_quality_conf

    # 1. 未配置新键，测试旧 quality_mode 兼容
    monkeypatch.setitem(CONF, "play_quality", None)
    monkeypatch.setitem(CONF, "lx_enabled", False)
    monkeypatch.setitem(CONF, "quality_mode", "smooth")
    assert effective_play_quality_conf() == "standard"

    monkeypatch.setitem(CONF, "quality_mode", "balanced")
    assert effective_play_quality_conf() == "lossless"

    monkeypatch.setitem(CONF, "lx_enabled", True)
    assert effective_play_quality_conf() == "exhigh"

    monkeypatch.setitem(CONF, "quality_mode", "high")
    monkeypatch.setitem(CONF, "lx_quality", "lossless")
    assert effective_play_quality_conf() == "lossless"

    # 2. 显式配置新键，必须优先于所有旧配置
    monkeypatch.setitem(CONF, "play_quality", "hires")
    monkeypatch.setitem(CONF, "quality_mode", "smooth")
    assert effective_play_quality_conf() == "hires"

    monkeypatch.setitem(CONF, "play_quality", "sky")
    assert effective_play_quality_conf() == "sky"


def test_effective_download_quality_prefers_new_and_falls_back_to_legacy(monkeypatch):
    """验证下载配置：新配置优先；未配置时旧 standard 迁移为 exhigh (320k)。"""
    from proxy.app import effective_download_quality_conf

    # 1. 未配置新键，旧 dl_quality 兼容
    monkeypatch.setitem(CONF, "download_quality", None)
    monkeypatch.setitem(CONF, "dl_quality", "standard")
    assert effective_download_quality_conf() == "exhigh"

    monkeypatch.setitem(CONF, "dl_quality", "original")
    assert effective_download_quality_conf() == "lossless"

    monkeypatch.setitem(CONF, "dl_quality", "app")
    assert effective_download_quality_conf() == "app"

    # 2. 显式配置新键
    monkeypatch.setitem(CONF, "download_quality", "lossless")
    assert effective_download_quality_conf() == "lossless"

    monkeypatch.setitem(CONF, "download_quality", "app")
    assert effective_download_quality_conf() == "app"
