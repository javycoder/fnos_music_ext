"""Download layouts and cooperative, journaled directory organization.

Only extension-owned references are updated. The official music database is
opened read-only; users must rescan it after moving songs.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext
import errno
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import time
import uuid

logger = logging.getLogger("fnmusic_proxy")
LAYOUTS = ("flat", "artist", "album", "artist_album")
LAYOUT_LABELS = {
    "flat": "歌曲", "artist": "歌手/歌曲",
    "album": "专辑/歌曲", "artist_album": "歌手/专辑/歌曲",
}
AUDIO_EXTS = frozenset("mp3 flac wav ogg opus m4a aac ape wv dsf dff tta aiff aif alac".split())
IMAGE_EXTS = frozenset("jpg jpeg png webp".split())
LYRIC_EXTS = frozenset(("lrc", "srt"))
DIR_COVER_RE = re.compile(r"(?:cover|folder)\.(?:jpe?g|png|webp)\Z", re.IGNORECASE)
DETAIL_LIMIT = 200


def _absolute(path: str) -> str:
    if not path or not os.path.isabs(path):
        raise ValueError(f"需要绝对路径: {path}")
    return os.path.abspath(path)


def _within(path: str, root: str) -> bool:
    return os.path.commonpath((path, root)) == root


def _identity(value: os.stat_result) -> tuple:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns)


def safe_part(value: str, fallback: str) -> str:
    part = re.sub(r"[/\\\x00-\x1f\x7f]", "_", str(value or "").strip())
    part = re.sub(r"\s+", " ", part).strip(" .")
    # NAME_MAX is measured in bytes, including on Chinese music libraries.
    part = part.encode("utf-8")[:120].decode("utf-8", errors="ignore").rstrip(" .")
    return part or fallback


def target_dir(root: str, layout: str, artist: str = "", album: str = "") -> str:
    root = _absolute(root)
    if layout not in LAYOUTS:
        raise ValueError(f"unsupported layout: {layout}")
    parts = []
    if layout in ("artist", "artist_album"):
        parts.append(safe_part(artist, "未知歌手"))
    if layout in ("album", "artist_album"):
        parts.append(safe_part(album, "未知专辑"))
    target = os.path.abspath(os.path.join(root, *parts))
    if not _within(target, root):
        raise ValueError("target escaped the download root")
    return target


@contextmanager
def _directory(path: str, *, create: bool = False):
    """Walk every ancestor with O_NOFOLLOW and keep the parent fd pinned."""
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in _absolute(path).split("/"):
            if not component:
                continue
            try:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o755, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=current)
            os.close(current)
            current = child
        yield current
    finally:
        os.close(current)


def _ensure_target_dir(root: str, target: str, *, create_root: bool = False) -> None:
    if not _within(_absolute(target), _absolute(root)):
        raise ValueError("target escaped the download root")
    with _directory(root, create=create_root), _directory(target, create=True):
        pass


def _stat(path: str) -> os.stat_result:
    with _directory(os.path.dirname(path)) as parent:
        return os.stat(os.path.basename(path), dir_fd=parent, follow_symlinks=False)


def _check(path: str, snapshot: os.stat_result) -> None:
    current = _stat(path)
    if not stat.S_ISREG(current.st_mode) or _identity(current) != _identity(snapshot):
        raise OSError(f"文件发生变化，保留以待核对: {path}")


def _remove_owned(path: str, snapshot: os.stat_result) -> None:
    with _directory(os.path.dirname(path)) as parent:
        current = os.stat(os.path.basename(path), dir_fd=parent, follow_symlinks=False)
        if _identity(current) != _identity(snapshot):
            raise OSError(f"文件发生变化，保留以待核对: {path}")
        os.unlink(os.path.basename(path), dir_fd=parent)
        os.fsync(parent)


def _digest(handle) -> bytes:
    digest = hashlib.sha256()
    while block := handle.read(1024 * 1024):
        digest.update(block)
    return digest.digest()


def _install_copy(source: str, target: str, snapshot: os.stat_result, root: str) -> None:
    """Copy, verify and install without replacing any destination entry."""
    _ensure_target_dir(root, os.path.dirname(target))
    with _directory(os.path.dirname(source)) as srcdir, \
            _directory(os.path.dirname(target)) as dstdir:
        temporary = f".organize-{uuid.uuid4().hex}.part"
        target_name = os.path.basename(target)
        installed = None
        try:
            source_fd = os.open(os.path.basename(source), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                dir_fd=srcdir)
            with os.fdopen(source_fd, "rb") as src:
                current = os.fstat(src.fileno())
                if not stat.S_ISREG(current.st_mode) or _identity(current) != _identity(snapshot):
                    raise OSError("source changed before copy")
                out_fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600,
                                 dir_fd=dstdir)
                with os.fdopen(out_fd, "w+b") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                    dst.flush()
                    src.seek(0)
                    dst.seek(0)
                    if _digest(src) != _digest(dst):
                        raise OSError("copy checksum mismatch")
                    if _identity(os.fstat(src.fileno())) != _identity(snapshot):
                        raise OSError("source changed during copy")
                    os.fchmod(dst.fileno(), stat.S_IMODE(snapshot.st_mode))
                    os.utime(dst.fileno(), ns=(snapshot.st_atime_ns, snapshot.st_mtime_ns))
                    os.fsync(dst.fileno())
            _check(source, snapshot)
            try:
                os.link(temporary, target_name, src_dir_fd=dstdir, dst_dir_fd=dstdir,
                        follow_symlinks=False)
            except OSError as exc:
                if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV):
                    raise
                # Filesystems without hard links still get O_EXCL protection.
                output = os.open(target_name, os.O_RDWR | os.O_CREAT | os.O_EXCL |
                                 os.O_NOFOLLOW, 0o600, dir_fd=dstdir)
                installed = os.fstat(output)
                with os.fdopen(output, "w+b") as dst:
                    inp = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dstdir)
                    with os.fdopen(inp, "rb") as src:
                        shutil.copyfileobj(src, dst, length=1024 * 1024)
                        dst.flush()
                        src.seek(0)
                        dst.seek(0)
                        if _digest(src) != _digest(dst):
                            raise OSError("install checksum mismatch")
                    os.fchmod(dst.fileno(), stat.S_IMODE(snapshot.st_mode))
                    os.utime(dst.fileno(), ns=(snapshot.st_atime_ns, snapshot.st_mtime_ns))
                    os.fsync(dst.fileno())
                installed = os.stat(target_name, dir_fd=dstdir, follow_symlinks=False)
            else:
                installed = os.stat(target_name, dir_fd=dstdir, follow_symlinks=False)
            os.unlink(temporary, dir_fd=dstdir)
            os.fsync(dstdir)
        except BaseException:
            if installed is not None:
                current = os.stat(target_name, dir_fd=dstdir, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (installed.st_dev, installed.st_ino):
                    os.unlink(target_name, dir_fd=dstdir)
            raise
        finally:
            try:
                os.unlink(temporary, dir_fd=dstdir)
            except FileNotFoundError:
                pass


def _first_tag(value) -> str:
    value = getattr(value, "text", value)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip()


def _tag_metadata(path: str) -> tuple[str, str]:
    from mutagen import File as MutagenFile
    media = MutagenFile(path, easy=True)
    if media is None or getattr(media, "info", None) is None:
        raise ValueError("无法识别音频文件")
    return (_first_tag(media.get("artist") or media.get("TPE1")),
            _first_tag(media.get("album") or media.get("TALB")))


def _database_metadata(runtime) -> dict[str, tuple[str, str]]:
    db = str(runtime.CONF.get("music_db") or runtime.dailyrec.music_db_path())
    if not os.path.isfile(db):
        return {}
    sql = """
        SELECT af.path, ar.name, al.name
          FROM track t JOIN audio_file af ON af.id=t.audio_file_id
          LEFT JOIN album al ON al.id=t.album_id
          LEFT JOIN track_artist ta ON ta.track_id=t.id
          LEFT JOIN artist ar ON ar.id=ta.artist_id
         WHERE t.is_audio_file_deleted=0 AND t.is_admin_deleted=0
           AND t.is_cue=0 AND af.is_physical_file_deleted=0
    """
    try:
        with sqlite3.connect(Path(db).absolute().as_uri() + "?mode=ro",
                             uri=True, timeout=2) as con:
            rows = con.execute(sql).fetchall()
    except (sqlite3.Error, OSError) as exc:
        logger.warning("organize metadata database read failed: %s", exc)
        return {}
    result = {}
    for raw_path, artist, album in rows:
        if isinstance(raw_path, str) and os.path.isabs(raw_path):
            key = os.path.realpath(raw_path)
            old = result.get(key, ("", ""))
            result[key] = (old[0] or str(artist or "").strip(),
                           old[1] or str(album or "").strip())
    return result


def _detail(job: dict, category: str, action: str, path: str, reason: str) -> None:
    if len(job["details"]) < DETAIL_LIMIT:
        job["details"].append({"category": category, "action": action,
                               "path": path, "reason": reason})


def _error(job: dict, category: str, path: str, exc: Exception) -> None:
    job["failed"] += 1
    if len(job["errors"]) < DETAIL_LIMIT:
        job["errors"].append({"category": category, "path": path, "error": str(exc)[:500]})


def _scan(root: str, job: dict) -> tuple[list[dict], dict[str, list[str]]]:
    audios, covers = [], {}

    def scan_error(exc):
        _error(job, "scan", str(exc.filename), exc)

    for base, dirs, files in os.walk(root, followlinks=False, onerror=scan_error):
        for name in list(dirs):
            if os.path.islink(os.path.join(base, name)):
                dirs.remove(name)
                job["skipped"] += 1
                _detail(job, "scan", "skipped", os.path.join(base, name), "符号链接目录")
        for name in sorted(files):
            path = os.path.join(base, name)
            try:
                current = _stat(path)
                ext = os.path.splitext(name)[1].lstrip(".").lower()
                if not stat.S_ISREG(current.st_mode):
                    job["skipped"] += 1
                    _detail(job, "scan", "skipped", path, "符号链接或特殊文件")
                elif ext in AUDIO_EXTS:
                    job["scanned"] += 1
                    audios.append({"path": path, "stat": current})
                elif DIR_COVER_RE.fullmatch(name):
                    covers.setdefault(base, []).append(path)
            except OSError as exc:
                _error(job, "scan", path, exc)
    return audios, covers


def _companions(audio: str) -> list[str]:
    stem = os.path.splitext(audio)[0]
    result = []
    for name in os.listdir(os.path.dirname(audio)):
        path = os.path.join(os.path.dirname(audio), name)
        base, ext = os.path.splitext(path)
        if base == stem and ext.lstrip(".").lower() in IMAGE_EXTS | LYRIC_EXTS:
            current = _stat(path)
            if stat.S_ISREG(current.st_mode):
                result.append(path)
    return sorted(result)


def _refs(runtime) -> list[tuple[str, str]]:
    result = []
    cache = os.path.realpath(_absolute(str(runtime.CONF["cache_dir"])))
    try:
        with _directory(cache) as directory:
            for name in os.listdir(directory):
                if not name.endswith(".ref"):
                    continue
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                    with os.fdopen(fd, "r", encoding="utf-8") as handle:
                        stem = handle.read(8192).strip()
                    if os.path.isabs(stem):
                        result.append((os.path.join(cache, name), os.path.realpath(stem)))
                except (OSError, UnicodeError):
                    continue
    except OSError:
        pass
    return result


def _write_ref(path: str, stem: str) -> None:
    with _directory(os.path.dirname(path)) as directory:
        temporary = f".organize-ref-{uuid.uuid4().hex}.part"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                         dir_fd=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(stem)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, os.path.basename(path), src_dir_fd=directory,
                       dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def _journal(path: str | None, event: dict) -> None:
    if not path:
        return
    # Durable intent precedes source deletion; completion/rollback records allow
    # interrupted groups and their .ref entries to be checked after a restart.
    with _directory(os.path.dirname(path), create=True) as directory:
        fd = os.open(os.path.basename(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND |
                     os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": time.time(), **event}, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.fsync(directory)


def _move_group(source_audio: str, companions: list[str], target_directory: str,
                refs: list[tuple[str, str]], root: str, *, snapshot=None,
                journal: str | None = None) -> list[tuple[str, str]]:
    sources = [source_audio, *companions]
    snapshots = {source: _stat(source) for source in sources}
    if snapshot is not None:
        _check(source_audio, snapshot)
    stem = os.path.splitext(os.path.basename(source_audio))[0]
    number = 1
    while True:
        suffix = "" if number == 1 else f" ({number})"
        targets = [(source, os.path.join(target_directory, stem + suffix +
                    os.path.splitext(source)[1])) for source in sources]
        if not any(os.path.lexists(target) for _, target in targets):
            break
        number += 1
    # References contain stems; do not strip a second extension from song.live.
    source_stem = os.path.splitext(source_audio)[0]
    target_stem = os.path.splitext(targets[0][1])[0]
    updates = [(path, value, target_stem) for path, value in refs
               if value == source_stem or value == source_audio]
    record = {"group": uuid.uuid4().hex, "files": targets, "refs": updates}
    _journal(journal, {**record, "state": "prepared"})
    created, removed, rewritten = {}, [], []
    try:
        for source, target in targets:
            _install_copy(source, target, snapshots[source], root)
            created[target] = _stat(target)
        _journal(journal, {**record, "state": "installed"})
        for ref_path, old, new in updates:
            _write_ref(ref_path, new)
            rewritten.append((ref_path, old, new))
        for source, _ in targets:
            _remove_owned(source, snapshots[source])
            removed.append(source)
        _journal(journal, {**record, "state": "completed"})
    except BaseException as failure:
        recovery_errors = []
        # unlink may have succeeded even if the following directory fsync
        # failed; inspect every installed pair before cleaning up any copy.
        removed = [source for source, target in targets
                   if target in created and not os.path.lexists(source)]
        # Recovery also supports source/target directories on different mounts.
        for source, target in targets:
            if source in removed:
                try:
                    _install_copy(target, source, created[target], root)
                except OSError as exc:
                    recovery_errors.append(str(exc))
        for ref_path, old, new in rewritten:
            try:
                if all(os.path.exists(source) for source in removed):
                    _write_ref(ref_path, old)
            except OSError as exc:
                recovery_errors.append(str(exc))
        for source, target in targets:
            if target in created and not recovery_errors:
                try:
                    _remove_owned(target, created[target])
                except OSError as exc:
                    recovery_errors.append(str(exc))
        _journal(journal, {**record, "state": "recovery_required" if recovery_errors else
                          "rolled_back", "error": str(failure), "recovery_errors": recovery_errors})
        if recovery_errors:
            raise OSError(f"{failure}; 已保留可用副本，请核对迁移日志: {recovery_errors}") from failure
        raise
    return targets


def _same_bytes(left: str, right: str) -> bool:
    handles = []
    try:
        for path in (left, right):
            with _directory(os.path.dirname(path)) as directory:
                fd = os.open(os.path.basename(path), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            handles.append(os.fdopen(fd, "rb"))
        return _digest(handles[0]) == _digest(handles[1])
    finally:
        for handle in handles:
            handle.close()


def _copy_cover(source: str, target: str, job: dict, root: str) -> None:
    if source == target:
        return
    if os.path.lexists(target):
        if os.path.islink(target) or not _same_bytes(source, target):
            job["skipped"] += 1
            _detail(job, "cover", "skipped", source, f"目标已有不同内容，保留原图片: {target}")
        return
    _install_copy(source, target, _stat(source), root)


def _busy(runtime, path: str) -> bool:
    if any(bool(getattr(runtime, name, None)) for name in
           ("_tee_active", "_full_fetch_tasks", "_tee_handoff_active")):
        return True
    transcode = getattr(runtime, "tc", None)
    leased = getattr(transcode, "playback_in_use", None)
    if getattr(transcode, "_SESSIONS", {}) or (leased and leased()):
        return True
    # Also catch files being copied/played through another local process.
    identity = _stat(path)
    for process in Path("/proc").glob("[0-9]*/fd"):
        try:
            for fd in process.iterdir():
                try:
                    current = fd.stat()
                    if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                        return True
                except OSError:
                    pass
        except OSError:
            pass
    return False


async def _worker(function, *args):
    """Wait for a group to finish/recover on cancellation before unlocking."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def _extension_metadata(runtime, path: str, refs) -> tuple[str, str]:
    metadata = getattr(runtime, "_ONLINE_META", {})
    stem = os.path.splitext(path)[0]
    for ref_path, ref_stem in refs:
        if ref_stem not in (stem, path):
            continue
        for guid, entry in list(metadata.items()):
            safe_guid = getattr(runtime, "cache_safe_guid", lambda value: value)(guid)
            if os.path.basename(ref_path) == safe_guid + ".ref":
                return str(entry.get("artist") or ""), str(entry.get("album") or "")
    return "", ""


async def run_organize(runtime, job: dict) -> None:
    root, layout = _absolute(job["root"]), job["layout"]
    if layout not in LAYOUTS:
        raise ValueError(f"unsupported layout: {layout}")
    _ensure_target_dir(root, root)
    job.update(status="running", phase="扫描下载目录")
    audios, covers = await _worker(_scan, root, job)
    database = await _worker(_database_metadata, runtime)
    references = await _worker(_refs, runtime)
    journal = os.path.join(os.path.realpath(_absolute(str(runtime.CONF["cache_dir"]))), "organize-journal",
                           job["id"] + ".jsonl")
    job["journal"] = journal
    by_directory, by_stem = {}, {}
    for item in audios:
        by_directory.setdefault(os.path.dirname(item["path"]), []).append(item)
        by_stem.setdefault(os.path.splitext(item["path"])[0], []).append(item)
    touched = set()

    def move(item):
        with getattr(runtime, "_LIBRARY_FILE_LOCK", nullcontext()):
            # Recheck in the SAME worker-thread lock used by downloads/lyrics.
            if any(_busy(runtime, path) for path in [item["path"], *item["companions"]]):
                raise BlockingIOError("文件正被下载、播放或转码占用")
            return _move_group(item["path"], item["companions"], item["target"],
                               _refs(runtime), root, snapshot=item["stat"], journal=journal)

    try:
        for index, item in enumerate(audios):
            path = item["path"]
            job["phase"] = f"整理歌曲 {index + 1}/{len(audios)}"
            try:
                if await _worker(_busy, runtime, path):
                    raise BlockingIOError("文件正被下载、播放或转码占用")
                artist, album = await _worker(_tag_metadata, path)
                db_artist, db_album = database.get(path, ("", ""))
                ext_artist, ext_album = _extension_metadata(runtime, path, references)
                item["artist"], item["album"] = artist or db_artist or ext_artist, album or db_album or ext_album
                if layout in ("artist", "artist_album") and not item["artist"]:
                    raise ValueError("缺少歌手信息")
                if layout in ("album", "artist_album") and not item["album"]:
                    raise ValueError("缺少专辑信息")
                item["target"] = target_dir(root, layout, item["artist"], item["album"])
                if os.path.dirname(path) == item["target"]:
                    item["organized"] = True
                    job["unchanged"] += 1
                    continue
                item["companions"] = await _worker(_companions, path)
                if len(by_directory[os.path.dirname(path)]) > 1:
                    item["companions"] = [companion for companion in item["companions"]
                                          if not DIR_COVER_RE.fullmatch(os.path.basename(companion))]
                if len(by_stem[os.path.splitext(path)[0]]) > 1 and item["companions"]:
                    raise ValueError("多个音频共享同词干附属文件，无法确定归属")
            except Exception as exc:
                job["skipped"] += 1
                _detail(job, "metadata", "skipped", path, str(exc)[:300])
                continue
            try:
                await _worker(move, item)
                job["moved"] += 1
                item["organized"] = True
                _detail(job, "move", "moved", path, item["target"])
                directory = os.path.dirname(path)
                while directory != root:
                    touched.add(directory)
                    directory = os.path.dirname(directory)
            except BlockingIOError as exc:
                job["skipped"] += 1
                _detail(job, "move", "skipped", path, str(exc))
            except Exception as exc:
                _error(job, "move", path, exc)

        job["phase"] = "处理封面"
        for directory, paths in covers.items():
            records = by_directory.get(directory, [])
            organized = [item for item in records if item.get("organized")]
            albums = {item.get("album", "") for item in records}
            associated = len(records) == 1 or (len(albums) == 1 and "" not in albums)
            if not records or len(organized) != len(records) or not associated:
                for source in paths:
                    job["skipped"] += 1
                    _detail(job, "cover", "skipped", source, "存在跳过歌曲或图片归属不明，保留原图片")
                continue
            for source in paths:
                for destination in sorted({item["target"] for item in organized}):
                    try:
                        def copy_cover():
                            with getattr(runtime, "_LIBRARY_FILE_LOCK", nullcontext()):
                                if os.path.exists(source):
                                    _copy_cover(source, os.path.join(destination, os.path.basename(source)),
                                                job, root)
                        await _worker(copy_cover)
                    except Exception as exc:
                        _error(job, "cover", source, exc)
        job["phase"] = "清理空目录"
        for directory in sorted(touched, key=len, reverse=True):
            try:
                with _directory(os.path.dirname(directory)) as parent:
                    os.rmdir(os.path.basename(directory), dir_fd=parent)
            except OSError:
                pass
        job.update(status="completed", phase="整理完成，请在飞牛音乐中重新扫描曲库")
    finally:
        try:
            runtime.localmatch.reset_caches()
        except AttributeError:
            pass
        job["finished_at"] = time.time()
