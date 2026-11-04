"""纯离线七档协议契约；不创建 NEMbox 实例、不读取登录态。"""
import importlib.util
from pathlib import Path
import sys
import types

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "musicbox_quality_playback", Path(__file__).resolve().parents[2] / "musicbox-service" / "playback.py")
playback = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(playback)


@pytest.mark.parametrize("quality", [
    "standard", "higher", "exhigh", "lossless", "hires", "jymaster", "sky",
])
def test_seven_tiers_do_not_inherit_legacy_helper_fallback(monkeypatch, quality):
    api = types.ModuleType("NEMbox.api")
    # 旧 helper 可能返回非空默认值，不能把这种默认当成已支持高档。
    api.music_quality_to_level = lambda value: value if value in {"standard", "higher", "exhigh"} else "exhigh"
    api.level_to_encode_type = lambda value: "mp3"
    package = types.ModuleType("NEMbox")
    package.api = api
    monkeypatch.setitem(sys.modules, "NEMbox", package)
    monkeypatch.setitem(sys.modules, "NEMbox.api", api)
    expected_encode = "mp3" if quality in {"standard", "higher", "exhigh"} else "flac"
    assert playback._safe_level_and_encode_type(quality) == (quality, expected_encode)
