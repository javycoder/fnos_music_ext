import pytest

from proxy import music_compat


@pytest.mark.parametrize("text, expected", [
    ("appname = trim.music\nversion = 1.0.10\n", (1, 0, 10)),
    ("version = 1.1.9\n", (1, 1, 9)),
    ("version = 1.1.19\n", (1, 1, 19)),
    ("version = 1.2.0\n", (1, 2, 0)),
    ("version = 1.1.19-beta\n", None),
    ("version = 1.1.19\nversion = 1.2.0\n", None),
])
def test_installed_version(tmp_path, text, expected):
    path = tmp_path / "manifest"
    path.write_text(text, encoding="utf-8")
    assert music_compat.installed_version(str(path)) == expected


def test_cache_expires_and_missing_version_is_safe(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(music_compat.time, "monotonic", lambda: now[0])
    path = tmp_path / "manifest"
    assert music_compat.installed_version(str(path)) is None
    path.write_text("version = 1.1.19\n", encoding="utf-8")
    assert music_compat.installed_version(str(path)) is None
    now[0] += music_compat.CACHE_SECONDS + 1
    assert music_compat.installed_version(str(path)) == (1, 1, 19)


def test_numeric_comparison(monkeypatch):
    monkeypatch.setattr(music_compat, "installed_version", lambda: (1, 1, 9))
    assert not music_compat.supports_search_default_100()
    monkeypatch.setattr(music_compat, "installed_version", lambda: (1, 1, 19))
    assert music_compat.supports_search_default_100()
    monkeypatch.setattr(music_compat, "installed_version", lambda: None)
    assert not music_compat.supports_search_default_100()
