"""Organization failure recovery and sidecar behavior; temporary data only."""
import asyncio
import errno
import json
import os
import threading
import wave

import pytest

from proxy import organize
from proxy.tests.test_organize import Runtime, job


def setup_audio(tmp_path, name="song.live.mp3"):
    root = tmp_path / "downloads"
    nested = root / "old" / "nested"
    nested.mkdir(parents=True)
    cache = tmp_path / "cache"
    cache.mkdir()
    audio = nested / name
    audio.write_bytes(b"audio bytes")
    return root, cache, audio, Runtime(root, cache)


def test_all_reference_aliases_and_journal(tmp_path, monkeypatch):
    root, cache, audio, runtime = setup_audio(tmp_path)
    aliases = [cache / f"alias-{n}.ref" for n in range(3)]
    for ref in aliases:
        ref.write_text(str(audio.with_suffix("")))
    unrelated = cache / "unrelated.ref"
    unrelated.write_text(str(audio.with_name("song.other")))
    monkeypatch.setattr(organize, "_tag_metadata", lambda _: ("artist", "album"))
    current = job(root, "artist_album")
    asyncio.run(organize.run_organize(runtime, current))
    target = root / "artist" / "album" / audio.name
    assert target.read_bytes() == b"audio bytes"
    assert all(ref.read_text() == str(target.with_suffix("")) for ref in aliases)
    assert unrelated.read_text().endswith("song.other")
    assert not (root / "old").exists()
    with open(current["journal"]) as handle:
        records = [json.loads(line) for line in handle]
    assert [entry["state"] for entry in records] == ["prepared", "installed", "completed"]


def test_copy_failure_leaves_sources_and_refs_intact(tmp_path, monkeypatch):
    root, cache, audio, runtime = setup_audio(tmp_path)
    lyric = audio.with_suffix(".lrc")
    lyric.write_text("lyrics")
    ref = cache / "a.ref"
    ref.write_text(str(audio.with_suffix("")))
    copy = organize._install_copy

    def fail_lyric(source, *args):
        if source.endswith(".lrc"):
            raise OSError("injected disk failure")
        copy(source, *args)

    monkeypatch.setattr(organize, "_install_copy", fail_lyric)
    with pytest.raises(OSError, match="injected disk failure"):
        organize._move_group(str(audio), [str(lyric)], str(root), organize._refs(runtime), str(root))
    assert audio.read_bytes() == b"audio bytes"
    assert lyric.read_text() == "lyrics"
    assert ref.read_text() == str(audio.with_suffix(""))
    assert not (root / audio.name).exists()


def test_removal_failure_restores_group_on_filesystem_without_links(tmp_path, monkeypatch):
    root, cache, audio, runtime = setup_audio(tmp_path)
    lyric = audio.with_suffix(".lrc")
    lyric.write_text("lyrics")
    ref = cache / "a.ref"
    ref.write_text(str(audio.with_suffix("")))
    remove = organize._remove_owned

    def fail_lyric(path, snapshot):
        if path == str(lyric):
            raise PermissionError("injected permission failure")
        remove(path, snapshot)

    def no_links(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "no hardlinks")

    monkeypatch.setattr(organize.os, "link", no_links)
    monkeypatch.setattr(organize, "_remove_owned", fail_lyric)
    with pytest.raises(PermissionError):
        organize._move_group(str(audio), [str(lyric)], str(root), organize._refs(runtime), str(root))
    assert audio.read_bytes() == b"audio bytes"
    assert lyric.read_text() == "lyrics"
    assert ref.read_text() == str(audio.with_suffix(""))
    assert not (root / audio.name).exists()


def test_failed_recovery_keeps_complete_target_copy(tmp_path, monkeypatch):
    root, cache, audio, runtime = setup_audio(tmp_path)
    lyric = audio.with_suffix(".lrc")
    lyric.write_text("lyrics")
    copy, remove = organize._install_copy, organize._remove_owned

    def fail_restore(source, target, *args):
        if target == str(audio):
            raise PermissionError("cannot restore")
        return copy(source, target, *args)

    def fail_remove(path, snapshot):
        if path == str(lyric):
            raise PermissionError("cannot remove lyric")
        return remove(path, snapshot)

    monkeypatch.setattr(organize, "_install_copy", fail_restore)
    monkeypatch.setattr(organize, "_remove_owned", fail_remove)
    with pytest.raises(OSError, match="已保留可用副本"):
        organize._move_group(str(audio), [str(lyric)], str(root), [], str(root))
    assert (root / audio.name).read_bytes() == b"audio bytes"
    assert lyric.read_text() == "lyrics"


def test_shared_cover_splits_to_artists_and_repeat_is_unchanged(tmp_path, monkeypatch):
    root, cache, audio, runtime = setup_audio(tmp_path, "one.mp3")
    other = audio.with_name("two.mp3")
    other.write_bytes(b"two")
    cover = audio.with_name("folder.jpg")
    cover.write_bytes(b"shared")
    (root / "already-empty").mkdir()
    monkeypatch.setattr(organize, "_tag_metadata", lambda path: (
        "first" if os.path.basename(path) == "one.mp3" else "second", "same album"))
    first = job(root, "artist")
    asyncio.run(organize.run_organize(runtime, first))
    assert first["moved"] == 2 and first["failed"] == 0
    assert (root / "first" / "folder.jpg").read_bytes() == b"shared"
    assert (root / "second" / "folder.jpg").read_bytes() == b"shared"
    assert cover.exists()
    second = job(root, "artist")
    asyncio.run(organize.run_organize(runtime, second))
    assert second["moved"] == 0 and second["unchanged"] == 2
    assert (root / "already-empty").is_dir()


def test_mismatching_shared_cover_is_preserved(tmp_path, monkeypatch):
    root, cache, audio, runtime = setup_audio(tmp_path)
    cover = audio.with_name("cover.jpg")
    cover.write_bytes(b"original cover")
    destination = root / "artist"
    destination.mkdir()
    existing = destination / "cover.jpg"
    existing.write_bytes(b"existing cover")
    monkeypatch.setattr(organize, "_tag_metadata", lambda _: ("artist", "album"))
    current = job(root, "artist")
    asyncio.run(organize.run_organize(runtime, current))
    assert cover.read_bytes() == b"original cover"
    assert existing.read_bytes() == b"existing cover"
    assert any("不同内容" in row["reason"] for row in current["details"])


@pytest.mark.parametrize("layout", ["artist", "album", "artist_album", "flat"])
def test_required_metadata_depends_on_layout(tmp_path, monkeypatch, layout):
    root, cache, audio, runtime = setup_audio(tmp_path)
    monkeypatch.setattr(organize, "_tag_metadata", lambda _: ("artist", ""))
    current = job(root, layout)
    asyncio.run(organize.run_organize(runtime, current))
    assert current["moved"] == (1 if layout in ("flat", "artist") else 0)
    assert audio.exists() == (layout in ("album", "artist_album"))


def test_unrecognized_and_open_files_are_skipped(tmp_path):
    root, cache, audio, runtime = setup_audio(tmp_path)
    current = job(root, "flat")
    asyncio.run(organize.run_organize(runtime, current))
    assert current["skipped"] == 1 and audio.exists()
    with audio.open("rb"):
        assert organize._busy(runtime, str(audio))


def test_recognized_audio_without_tags_can_be_flattened(tmp_path):
    root, cache, audio, runtime = setup_audio(tmp_path, "untagged.wav")
    with wave.open(str(audio), "wb") as handle:
        handle.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        handle.writeframes(b"\x00\x00" * 80)
    original = audio.read_bytes()
    current = job(root, "flat")
    asyncio.run(organize.run_organize(runtime, current))
    assert current["moved"] == 1
    assert (root / audio.name).read_bytes() == original


def test_metadata_priority_tags_database_extension(tmp_path, monkeypatch):
    root, cache, audio, runtime = setup_audio(tmp_path)
    (cache / "known.ref").write_text(str(audio.with_suffix("")))
    runtime._ONLINE_META = {"known": {"artist": "extension", "album": "extension album"}}
    monkeypatch.setattr(organize, "_tag_metadata", lambda _: ("tags", ""))
    monkeypatch.setattr(organize, "_database_metadata",
                        lambda _: {str(audio): ("database", "database album")})
    current = job(root, "artist_album")
    asyncio.run(organize.run_organize(runtime, current))
    assert (root / "tags" / "database album" / audio.name).exists()


def test_move_waits_for_file_lock_without_blocking_event_loop(tmp_path, monkeypatch):
    root, cache, audio, runtime = setup_audio(tmp_path)
    monkeypatch.setattr(organize, "_tag_metadata", lambda _: ("artist", "album"))

    async def run():
        runtime._LIBRARY_FILE_LOCK.acquire()
        task = asyncio.create_task(organize.run_organize(runtime, job(root, "artist")))
        try:
            await asyncio.sleep(0.05)
            assert audio.exists() and not task.done()
        finally:
            runtime._LIBRARY_FILE_LOCK.release()
        await asyncio.wait_for(task, 5)

    asyncio.run(run())
    assert (root / "artist" / audio.name).exists()


def test_reference_replaced_before_fsync_failure_is_rolled_back(tmp_path, monkeypatch):
    root, cache, audio, runtime = setup_audio(tmp_path)
    reference = cache / "track.ref"
    original = str(audio.with_suffix(""))
    reference.write_text(original)
    write = organize._write_ref
    calls = []

    def fail_after_replace(path, value):
        write(path, value)
        calls.append(value)
        if len(calls) == 1:
            raise OSError("injected directory fsync failure after ref replacement")

    monkeypatch.setattr(organize, "_write_ref", fail_after_replace)
    with pytest.raises(OSError, match="fsync failure"):
        organize._move_group(str(audio), [], str(root), organize._refs(runtime), str(root))
    assert audio.read_bytes() == b"audio bytes"
    assert reference.read_text() == original
    assert not (root / audio.name).exists()
    assert calls == [str((root / audio.name).with_suffix("")), original]


def test_reference_scan_skips_fifo_and_symlink(tmp_path):
    root, cache, audio, runtime = setup_audio(tmp_path)
    os.mkfifo(cache / "fifo.ref")
    (cache / "symlink.ref").symlink_to(audio)
    assert organize._refs(runtime) == []


def test_special_files_never_enter_copy_or_cover_comparison(tmp_path):
    root, cache, audio, runtime = setup_audio(tmp_path)
    fifo = root / "cover.jpg"
    os.mkfifo(fifo)
    with pytest.raises(OSError, match="source changed"):
        organize._install_copy(str(fifo), str(root / "copied.jpg"), fifo.stat(), str(root))
    cover = audio.with_name("cover.jpg")
    cover.write_bytes(b"cover")
    current = job(root, "flat")
    organize._copy_cover(str(cover), str(fifo), current, str(root))
    assert current["skipped"] == 1
    assert cover.read_bytes() == b"cover"


def test_path_caches_are_invalidated_between_groups(tmp_path, monkeypatch):
    root, cache, audio, runtime = setup_audio(tmp_path, "one.mp3")
    audio.with_name("two.mp3").write_bytes(b"two")
    invalidations = []
    runtime.localmatch.reset_caches = lambda: invalidations.append(True)
    metadata_calls = []

    def metadata(path):
        if metadata_calls:
            assert invalidations, "first group must invalidate paths before second group"
        metadata_calls.append(path)
        return "artist", "album"

    monkeypatch.setattr(organize, "_tag_metadata", metadata)
    current = job(root, "artist")
    asyncio.run(organize.run_organize(runtime, current))
    assert current["moved"] == 2 and current["failed"] == 0


def test_activity_started_after_scan_prevents_move(tmp_path, monkeypatch):
    root, cache, audio, runtime = setup_audio(tmp_path)
    monkeypatch.setattr(organize, "_tag_metadata", lambda _: ("artist", "album"))
    checks = iter((False, True))
    monkeypatch.setattr(organize, "_busy", lambda *args: next(checks))
    current = job(root, "artist")
    asyncio.run(organize.run_organize(runtime, current))
    assert current["moved"] == 0 and current["skipped"] == 1
    assert audio.read_bytes() == b"audio bytes"


def test_cache_hot_reload_waits_for_group_before_copying_references(tmp_path, monkeypatch):
    from proxy import app

    root, cache, audio, runtime = setup_audio(tmp_path)
    new_cache = tmp_path / "new-cache"
    reference = cache / "known.ref"
    reference.write_text(str(audio.with_suffix("")))
    env = tmp_path / ".env"
    env.write_text(f"FNMUSIC_CACHE_DIR={new_cache}\n")
    monkeypatch.setitem(app.CONF, "cache_dir", str(cache))
    monkeypatch.setenv("FNMUSIC_CACHE_DIR", str(cache))
    entered, release = threading.Event(), threading.Event()
    new_stem = str(root / "new-song")

    def migration_group():
        with app._LIBRARY_FILE_LOCK:
            entered.set()
            assert release.wait(5)
            organize._write_ref(str(reference), new_stem)

    async def run():
        group = asyncio.create_task(asyncio.to_thread(migration_group))
        assert await asyncio.to_thread(entered.wait, 2)
        reload = asyncio.create_task(asyncio.to_thread(app.apply_env_hot_reload, str(env)))
        try:
            await asyncio.sleep(0.02)
            assert not reload.done()
            assert app.CONF["cache_dir"] == str(cache)
        finally:
            release.set()
        await asyncio.gather(group, reload)

    asyncio.run(run())
    assert (new_cache / "known.ref").read_text() == new_stem
