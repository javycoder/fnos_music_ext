"""Download layout configuration and path selection."""
import os

import proxy.app as app


def test_layout_sanitization_and_escape_guard(tmp_path, monkeypatch):
    monkeypatch.setitem(app.CONF, "download_layout", "artist_album")
    target = app.download_subdir(str(tmp_path), "../Artist\nX", "/Album")
    assert target == os.path.join(str(tmp_path), "_Artist_X", "_Album")


def test_library_media_path_supports_all_layouts(tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    root.mkdir()
    monkeypatch.setitem(app.CONF, "tee_save_dir", str(root))
    monkeypatch.setitem(app.CONF, "library_dir", str(tmp_path / "auto"))
    monkeypatch.setitem(app.CONF, "cache_dir", str(tmp_path / "cache"))
    expected = {
        "flat": root,
        "artist": root / "周杰伦",
        "album": root / "叶惠美",
        "artist_album": root / "周杰伦" / "叶惠美",
    }
    for layout, directory in expected.items():
        monkeypatch.setitem(app.CONF, "download_layout", layout)
        path = app.library_media_path(
            f"online:test:{layout}", "晴天", "flac",
            artist="周杰伦", album="叶惠美", directory=str(root))
        assert path == str(directory / "周杰伦 - 晴天.flac")


def test_layout_change_reuses_existing_flat_file(tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    root.mkdir()
    existing = root / "周杰伦 - 晴天.flac"
    existing.write_bytes(b"audio")
    monkeypatch.setitem(app.CONF, "tee_save_dir", str(root))
    monkeypatch.setitem(app.CONF, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(app.CONF, "download_layout", "artist_album")
    assert app.library_media_path(
        "online:test:new-guid", "晴天", "flac",
        artist="周杰伦", album="叶惠美", directory=str(root)) == str(existing)


def test_layout_hot_reload_accepts_only_allowed_values(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_DOWNLOAD_LAYOUT", "flat")
    monkeypatch.setitem(app.CONF, "download_layout", "flat")
    env = tmp_path / ".env"
    env.write_text("FNMUSIC_DOWNLOAD_LAYOUT=flat\n", encoding="utf-8")
    monkeypatch.setattr(app, "_env_watch_path", lambda: str(env))
    env.write_text("FNMUSIC_DOWNLOAD_LAYOUT=artist_album\n", encoding="utf-8")
    assert "download_layout" in app.apply_env_hot_reload()
    assert app.download_layout() == "artist_album"
    env.write_text("FNMUSIC_DOWNLOAD_LAYOUT=bad\n", encoding="utf-8")
    assert "download_layout" not in app.apply_env_hot_reload()
    assert app.download_layout() == "artist_album"
