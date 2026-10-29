"""Download directory organization behavior."""
import asyncio
import threading
from types import SimpleNamespace

import proxy.organize as organize


class Runtime:
    def __init__(self, root, cache):
        self.CONF = {"cache_dir": str(cache), "music_db": str(cache / "missing.db")}
        self.dailyrec = SimpleNamespace(music_db_path=lambda: str(cache / "missing.db"))
        self.tc = SimpleNamespace(_SESSIONS={}, playback_in_use=lambda: False)
        self.localmatch = SimpleNamespace(reset_caches=lambda: None)
        self._LIBRARY_FILE_LOCK = threading.RLock()


def job(root, layout):
    return {"id": "1" * 32, "status": "running", "phase": "", "root": str(root),
            "layout": layout, "scanned": 0, "moved": 0, "unchanged": 0,
            "skipped": 0, "failed": 0, "errors": [], "details": [], "started_at": 0}


def test_organize_moves_group_updates_ref_and_preserves_conflict(tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    source_dir = root / "source"
    source_dir.mkdir(parents=True)
    cache = tmp_path / "cache"
    cache.mkdir()
    audio = source_dir / "song.mp3"
    lyric = source_dir / "song.lrc"
    cover = source_dir / "cover.jpg"
    audio.write_bytes(b"audio")
    lyric.write_text("lrc", encoding="utf-8")
    cover.write_bytes(b"cover")
    unknown = source_dir / "unknown.mp3"
    unknown.write_bytes(b"unknown")
    (root / "周杰伦" / "叶惠美").mkdir(parents=True)
    conflict = root / "周杰伦" / "叶惠美" / "song.mp3"
    conflict.write_bytes(b"existing")
    ref = cache / "online_test.ref"
    ref.write_text(str(audio.with_suffix("")), encoding="utf-8")

    monkeypatch.setattr(organize, "_tag_metadata", lambda path: (
        ("周杰伦", "叶惠美") if path == str(audio) else ("", "")))
    current = job(root, "artist_album")
    asyncio.run(organize.run_organize(Runtime(root, cache), current))

    target_audio = root / "周杰伦" / "叶惠美" / "song (2).mp3"
    target_lyric = root / "周杰伦" / "叶惠美" / "song (2).lrc"
    assert not audio.exists() and not lyric.exists()
    assert target_audio.read_bytes() == b"audio"
    assert target_lyric.read_text() == "lrc"
    assert conflict.read_bytes() == b"existing"
    assert unknown.exists()
    assert cover.exists()
    assert ref.read_text(encoding="utf-8") == str(target_audio.with_suffix(""))
    assert current["moved"] == 1 and current["skipped"] == 3 and current["failed"] == 0


def test_organize_flat_moves_nested_audio_and_ignores_symlink(tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    nested = root / "old"
    nested.mkdir(parents=True)
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")
    (nested / "inside.mp3").write_bytes(b"inside")
    (root / "link.mp3").symlink_to(outside)
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(organize, "_tag_metadata", lambda path: ("", ""))

    current = job(root, "flat")
    asyncio.run(organize.run_organize(Runtime(root, cache), current))
    assert (root / "inside.mp3").read_bytes() == b"inside"
    assert not (root / "old").exists()
    assert (root / "link.mp3").is_symlink()
    assert outside.read_bytes() == b"outside"


def test_organize_refuses_target_through_symlink(tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    audio = root / "song.mp3"
    audio.write_bytes(b"audio")
    (root / "歌手").symlink_to(outside)
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(organize, "_tag_metadata", lambda path: ("歌手", "专辑"))
    current = job(root, "artist")
    asyncio.run(organize.run_organize(Runtime(root, cache), current))
    assert audio.read_bytes() == b"audio"
    assert not (outside / "专辑" / "song.mp3").exists()
    assert current["failed"] == 1
