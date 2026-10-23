"""Regressions for file identity, conflicts, references and worker shutdown."""
import asyncio
import errno
import os
import threading
from types import SimpleNamespace

import pytest

from proxy import app, organize


def test_chinese_layout_directory_names_fit_filesystem_limits(tmp_path, monkeypatch):
    monkeypatch.setitem(app.CONF, "download_layout", "artist_album")
    directory = app.download_subdir(str(tmp_path), "歌手" * 100, "专辑" * 100)
    assert os.path.isdir(directory)
    assert all(len(part.encode("utf-8")) <= 120
               for part in os.path.relpath(directory, tmp_path).split(os.sep))


def test_configured_download_and_cache_roots_can_use_symlinks(tmp_path, monkeypatch):
    real_root = tmp_path / "real"
    real_root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real_root, target_is_directory=True)
    cache = real_root / "cache"
    cache.mkdir()
    source = real_root / "song.mp3"
    source.write_bytes(b"audio")
    reference = cache / "song.ref"
    reference.write_text(str(alias / "song"), encoding="utf-8")
    runtime = SimpleNamespace(CONF={"cache_dir": str(alias / "cache")})
    references = organize._refs(runtime)
    assert references == [(str(reference), str(real_root / "song"))]
    monkeypatch.setitem(app.CONF, "download_layout", "artist")
    directory = app.download_subdir(str(alias), "歌手")
    assert directory == str(real_root / "歌手")
    organize._move_group(str(source), [], directory, references, str(real_root))
    assert reference.read_text() == str(real_root / "歌手" / "song")


def test_dotted_filename_preserves_reference(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()
    audio = root / "song.live.mp3"
    audio.write_bytes(b"audio")
    lyric = root / "song.live.lrc"
    lyric.write_bytes(b"lyric")
    reference = tmp_path / "song.ref"
    reference.write_text(str(audio.with_suffix("")), encoding="utf-8")
    targets = organize._move_group(str(audio), [str(lyric)], str(root / "artist"),
                                   [(str(reference), str(audio.with_suffix("")))], str(root))
    assert reference.read_text() == str(root / "artist" / "song.live")
    assert all(os.path.isfile(target) for _, target in targets)
    assert not audio.exists() and not lyric.exists()


def test_copy_never_overwrites_concurrent_destination(tmp_path, monkeypatch):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"source")
    target = tmp_path / "target.mp3"

    def unavailable_link(*_args, **_kwargs):
        target.write_bytes(b"another writer")
        raise OSError(errno.EOPNOTSUPP, "hard links unavailable")

    monkeypatch.setattr(organize.os, "link", unavailable_link)
    with pytest.raises(FileExistsError):
        organize._install_copy(str(source), str(target), source.stat(), str(tmp_path))
    assert source.read_bytes() == b"source"
    assert target.read_bytes() == b"another writer"
    assert list(tmp_path.glob("*.part")) == []


def test_ref_failure_keeps_original_group(tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    root.mkdir()
    audio = root / "song.mp3"
    audio.write_bytes(b"audio")
    reference = tmp_path / "song.ref"
    original_stem = str(audio.with_suffix(""))
    reference.write_text(original_stem)

    def fail_ref(*_args):
        raise OSError("reference unavailable")

    monkeypatch.setattr(organize, "_write_ref", fail_ref)
    with pytest.raises(OSError, match="reference unavailable"):
        organize._move_group(str(audio), [], str(root / "artist"),
                             [(str(reference), original_stem)], str(root))
    assert audio.read_bytes() == b"audio"
    assert reference.read_text() == original_stem
    assert not (root / "artist" / "song.mp3").exists()


def test_rejects_source_replaced_after_scan(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"original")
    snapshot = audio.stat()
    audio.unlink()
    audio.write_bytes(b"replacement")
    with pytest.raises(OSError):
        organize._move_group(str(audio), [], str(tmp_path / "artist"), [], str(tmp_path),
                             snapshot=snapshot)
    assert audio.read_bytes() == b"replacement"
    assert not (tmp_path / "artist" / "song.mp3").exists()


@pytest.mark.asyncio
async def test_cancellation_waits_for_worker():
    started, release = threading.Event(), threading.Event()
    completed = []

    def work():
        started.set()
        release.wait(5)
        completed.append(True)

    task = asyncio.create_task(organize._worker(work))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed == [True]
