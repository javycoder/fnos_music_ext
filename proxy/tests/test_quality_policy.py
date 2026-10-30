"""共享音质契约：官方档位、严格降档与旧配置语义保持独立。"""
import importlib.util
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "quality_policy.py"


def policy():
    # 缺少实现时先给出可解释的失败，避免导入错误掩盖契约目的。
    assert MODULE.exists(), "缺少统一音质策略模块"
    spec = importlib.util.spec_from_file_location("quality_policy_under_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_official_levels_and_strict_downward_fallback():
    p = policy()
    levels = ("standard", "higher", "exhigh", "lossless", "hires", "jymaster", "sky")
    assert p.QUALITY_LEVELS == levels
    for index, level in enumerate(levels):
        assert p.quality_ladder(level) == list(reversed(levels[:index + 1]))
    assert p.QUALITY_LABELS["exhigh"] == "极高"
    assert p.QUALITY_LABELS["jymaster"] == "超清母带"
    assert p.QUALITY_LABELS["sky"] == "沉浸声"


@pytest.mark.parametrize("level,lx", [
    ("standard", "128k"), ("higher", "192k"), ("exhigh", "320k"),
    ("lossless", "flac"), ("hires", "flac24bit"),
    ("jymaster", "master"), ("sky", "atmos"),
])
def test_lx_mapping_keeps_all_seven_levels(level, lx):
    p = policy()
    assert p.to_lx_quality(level) == lx
    assert p.normalize_quality(lx) == level


def test_unknown_quality_does_not_invent_capabilities():
    p = policy()
    with pytest.raises(ValueError):
        p.normalize_quality("future-super", default=None)
    assert p.normalize_quality("future-super") == "lossless"
    assert p.normalize_quality("high") == "exhigh"


@pytest.mark.parametrize("env,expected", [
    ({}, "lossless"),
    ({"FNMUSIC_QUALITY_MODE": "smooth"}, "standard"),
    ({"FNMUSIC_QUALITY_MODE": "balanced", "FNMUSIC_LX_ENABLED": "true"}, "exhigh"),
    ({"FNMUSIC_QUALITY_MODE": "balanced", "FNMUSIC_NETEASE_ENABLED": "true"}, "lossless"),
    ({"FNMUSIC_QUALITY_MODE": "high", "FNMUSIC_LX_ENABLED": "true", "FNMUSIC_LX_QUALITY": "flac24bit"}, "hires"),
    ({"FNMUSIC_QUALITY_MODE": "high", "FNMUSIC_NETEASE_QUALITY": "jymaster"}, "jymaster"),
    ({"FNMUSIC_PLAY_QUALITY": "higher", "FNMUSIC_QUALITY_MODE": "smooth"}, "higher"),
])
def test_play_quality_migration_respects_original_provider(env, expected):
    assert policy().effective_play_quality(env) == expected


@pytest.mark.parametrize("old,expected", [("standard", "exhigh"), ("original", "lossless"), ("app", "app")])
def test_download_legacy_standard_was_320k(old, expected):
    p = policy()
    assert p.effective_download_quality({"FNMUSIC_DL_QUALITY": old}) == expected
    assert p.effective_download_quality({"FNMUSIC_DOWNLOAD_QUALITY": "standard", "FNMUSIC_DL_QUALITY": old}) == "standard"


def test_app_protocol_standard_is_not_new_standard():
    p = policy()
    assert p.app_download_quality("standard", False, "hires") == "exhigh"
    assert p.app_download_quality("higher", False, "hires") == "higher"
    assert p.app_download_quality("", True, "hires") == "sky"
    assert p.app_download_quality("unknown", False, "higher") == "higher"
    assert p.app_download_quality("", False, "lossless") == "lossless"


def test_migration_only_proposes_missing_new_keys():
    p = policy()
    values = {"FNMUSIC_DL_QUALITY": "standard", "FNMUSIC_QUALITY_MODE": "smooth"}
    updates = p.quality_migration(values)
    assert updates == {"FNMUSIC_PLAY_QUALITY": "standard", "FNMUSIC_DOWNLOAD_QUALITY": "exhigh"}
    values.update(updates)
    assert p.quality_migration(values) == {}
