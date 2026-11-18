"""FNMUSIC_MUSICDL_OFFICIAL_SEARCH 缺省值判定（老用户旧版 / 其余新版）。

resolve_musicdl_official_search 是安装脚本、musicdl 服务入口与 WebUI GET
/api/config 共用的唯一实现；这里的组合矩阵锁死三处不漂移。
"""
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from proxy.env_merge import merge_env, resolve_musicdl_official_search  # noqa: E402


def test_existing_key_always_wins():
    assert resolve_musicdl_official_search(
        {"FNMUSIC_MUSICDL_OFFICIAL_SEARCH": "true"}, env_file_exists=True) is True
    assert resolve_musicdl_official_search(
        {"FNMUSIC_MUSICDL_OFFICIAL_SEARCH": "true"}, env_file_exists=False) is True
    assert resolve_musicdl_official_search(
        {"FNMUSIC_MUSICDL_OFFICIAL_SEARCH": "false"}, env_file_exists=False) is False
    assert resolve_musicdl_official_search(
        {"FNMUSIC_MUSICDL_OFFICIAL_SEARCH": "false",
         "FNMUSIC_MUSICDL_ENABLED": "true"}, env_file_exists=True) is False


@pytest.mark.parametrize("raw", ["on", "1", "yes", "ON", " True "])
def test_truthy_spellings(raw):
    assert resolve_musicdl_official_search(
        {"FNMUSIC_MUSICDL_OFFICIAL_SEARCH": raw}, env_file_exists=False) is True


@pytest.mark.parametrize("raw", ["off", "0", "no", "OFF"])
def test_falsy_spellings(raw):
    assert resolve_musicdl_official_search(
        {"FNMUSIC_MUSICDL_OFFICIAL_SEARCH": raw}, env_file_exists=True) is False


def test_missing_key_installed_musicdl_user_defaults_legacy():
    """已安装（.env 存在）且当前启用 musicdl 的老用户：默认旧版自实现聚合。"""
    assert resolve_musicdl_official_search(
        {"FNMUSIC_MUSICDL_ENABLED": "true"}, env_file_exists=True) is False
    assert resolve_musicdl_official_search(
        {"FNMUSIC_MUSICDL_ENABLED": "true", "FNMUSIC_MUSICDL_OFFICIAL_SEARCH": ""},
        env_file_exists=True) is False


def test_missing_key_fresh_install_or_musicdl_disabled_defaults_official():
    """全新安装（无 .env）或存量安装但未启用 musicdl：默认新版官方聚合。"""
    assert resolve_musicdl_official_search({}, env_file_exists=False) is True
    assert resolve_musicdl_official_search(
        {"FNMUSIC_MUSICDL_ENABLED": "true"}, env_file_exists=False) is True
    assert resolve_musicdl_official_search(
        {"FNMUSIC_MUSICDL_ENABLED": "false"}, env_file_exists=True) is True
    assert resolve_musicdl_official_search({}, env_file_exists=True) is True


def test_unrecognized_value_falls_back_to_missing_key_rules():
    """非空但无法识别的值按缺键处理，不当成 true。"""
    assert resolve_musicdl_official_search(
        {"FNMUSIC_MUSICDL_OFFICIAL_SEARCH": "banana",
         "FNMUSIC_MUSICDL_ENABLED": "true"}, env_file_exists=True) is False
    assert resolve_musicdl_official_search(
        {"FNMUSIC_MUSICDL_OFFICIAL_SEARCH": "banana"}, env_file_exists=False) is True


def test_merge_env_never_overwrites_saved_choice_on_upgrade():
    """键不在 explicit 时：env_merge 保留用户已有值，安装升级不覆盖选择。"""
    existing = [
        ("FNMUSIC_MUSICDL_ENABLED", "true"),
        ("FNMUSIC_MUSICDL_OFFICIAL_SEARCH", "false"),
    ]
    desired = [
        ("FNMUSIC_MUSICDL_ENABLED", "true"),
        ("FNMUSIC_MUSICDL_OFFICIAL_SEARCH", "true"),  # 全新安装默认
    ]
    merged, summary = merge_env(existing, desired)
    assert dict(merged)["FNMUSIC_MUSICDL_OFFICIAL_SEARCH"] == "false"
    assert "FNMUSIC_MUSICDL_OFFICIAL_SEARCH" in summary["preserved"]


def test_merge_env_adds_missing_key_for_old_install():
    """老安装缺键：安装脚本按判定写入的 desired 值被安全补齐。"""
    existing = [("FNMUSIC_MUSICDL_ENABLED", "true")]
    desired = [("FNMUSIC_MUSICDL_OFFICIAL_SEARCH", "false")]
    merged, summary = merge_env(existing, desired)
    assert dict(merged)["FNMUSIC_MUSICDL_OFFICIAL_SEARCH"] == "false"
    assert "FNMUSIC_MUSICDL_OFFICIAL_SEARCH" in summary["added"]
