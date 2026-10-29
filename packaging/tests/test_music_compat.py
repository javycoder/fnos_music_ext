import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_music_compat.py"
spec = importlib.util.spec_from_file_location("check_music_compat", SCRIPT)
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)


@pytest.mark.parametrize("change", [
    {"extension_version": "other"}, {"music_versions": []},
    {"music_versions": ["bad"]}, {"checked_at": "2099-01-01"},
    {"checked_at": "2026-99-01"}, {"notes": ""},
])
def test_invalid_compat_blocks_packaging(tmp_path, change):
    (tmp_path / "VERSION").write_text("2.11.0a")
    value = {"extension_version": "2.11.0a", "music_versions": ["1.0.10"],
             "checked_at": date.today().isoformat(), "notes": "verified"}
    value.update(change)
    (tmp_path / "FNMUSIC_COMPAT.json").write_text(json.dumps(value))
    with pytest.raises(ValueError):
        compat.validate(tmp_path)


def test_repository_record_valid():
    compat.validate(SCRIPT.parents[1])


def test_missing_record_blocks_packaging(tmp_path):
    (tmp_path / "VERSION").write_text("2.11.0a")
    with pytest.raises(OSError):
        compat.validate(tmp_path)
