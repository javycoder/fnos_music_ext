#!/usr/bin/env python3
"""在线试听滚动缓存清理，运行时淘汰与 restore 还原共用；孤儿歌词清扫。

滚动缓存产物 = cache 目录下 online_*.<音频扩展名>（cache_safe_guid 命名）及同名 .lrc。
.ref 只在目标文件（音频或 .lrc）已不存在时删除：指向飞牛曲库中仍存在文件的 .ref
必须保留，否则 restore 后重播同曲会重新出网，在曲库里产生重复文件。

孤儿歌词 = 写进曲库目录、但同名词干下已无音频的 .lrc（旧版无音频时把曲库当
歌词兜底目录所致）。sweep_orphan_lyrics 只清"词干能对上 cache/*.ref 记录"的
那部分——没有记录的一律视为用户自有歌词，绝不触碰。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

AUDIO_EXTS = ("mp3", "flac", "wav", "ogg", "opus", "m4a", "aac", "ape", "wv", "dsf", "dff", "tta")

_ROLLING_AUDIO_RE = re.compile(r"^online_.+\.(" + "|".join(AUDIO_EXTS) + r")$", re.IGNORECASE)
_KNOWN_EXTS = set(AUDIO_EXTS) | {"lrc", "ref", "part"}


def _rolling_audio_files(cache_dir: str) -> list[str]:
    """cache 目录下的滚动音频，按 mtime 新→旧排序。"""
    try:
        entries = list(os.scandir(cache_dir))
    except (FileNotFoundError, NotADirectoryError):
        return []
    files = [e.path for e in entries if e.is_file() and _ROLLING_AUDIO_RE.match(e.name)]
    files.sort(key=lambda path: os.stat(path).st_mtime_ns, reverse=True)
    return files


def _stem_has_file(stem: str) -> bool:
    return any(os.path.exists(f"{stem}.{ext}") for ext in (*AUDIO_EXTS, "lrc"))


def _safe_remove(path: str, removed: list[str]) -> None:
    try:
        os.remove(path)
        removed.append(path)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _ref_stem(ref_path: str) -> str:
    try:
        with open(ref_path, encoding="utf-8") as f:
            stem = f.read().strip()
    except Exception:
        return ""
    ext = os.path.splitext(stem)[1].lstrip(".").lower()
    if ext in _KNOWN_EXTS:
        stem = stem[: -len(ext) - 1] if ext else stem
    return stem


def purge_rolling(cache_dir: str, keep: int = 0) -> list[str]:
    """保留最新 keep 条滚动音频，删除其余音频与同名 .lrc，并清理目标已消失的 .ref。

    返回被删除的文件路径列表（用于日志）。
    """
    removed: list[str] = []
    audio = _rolling_audio_files(cache_dir)
    for path in audio[max(0, int(keep)):]:
        _safe_remove(path, removed)
        _safe_remove(os.path.splitext(path)[0] + ".lrc", removed)
    _purge_dead_refs(cache_dir, removed)
    return removed


def _purge_dead_refs(cache_dir: str, removed: list[str]) -> None:
    """删除目标文件（音频或 .lrc）已全部消失的 .ref。"""
    try:
        entries = list(os.scandir(cache_dir))
    except (FileNotFoundError, NotADirectoryError):
        return
    for e in entries:
        if not (e.is_file() and e.name.endswith(".ref")):
            continue
        stem = _ref_stem(e.path)
        if not stem or not _stem_has_file(stem):
            _safe_remove(e.path, removed)


def _within(path: str, directory: str) -> bool:
    try:
        rp, rd = os.path.realpath(path), os.path.realpath(directory)
        return os.path.commonpath([rp, rd]) == rd
    except ValueError:
        return False


def sweep_orphan_lyrics(cache_dir: str, media_dirs: list[str], min_age_s: float = 120.0) -> list[str]:
    """清理本插件写进曲库、音频已不存在的孤儿 .lrc。

    只删除同时满足以下条件的 .lrc（缺一不可）：
    1. 词干来自 cache/*.ref 记录（证明是本插件写的，用户自有歌词无记录永不命中）；
    2. 位于 media_dirs 之一（曲库/落盘目录）之内，cache 内部归 purge_rolling 管；
    3. 同名词干下不存在任何音频文件（词曲成对的一律保留）；
    4. mtime 距今超过 min_age_s（防误删正在配对中的新文件）。

    返回被删除的文件路径列表（用于日志）。
    """
    removed: list[str] = []
    try:
        entries = list(os.scandir(cache_dir))
    except (FileNotFoundError, NotADirectoryError):
        return removed
    stems: list[str] = []
    for e in entries:
        if not (e.is_file() and e.name.endswith(".ref")):
            continue
        stem = _ref_stem(e.path)
        if stem:
            stems.append(stem)
    now = time.time()
    seen: set[str] = set()
    for stem in stems:
        lrc = f"{stem}.lrc"
        if not os.path.isfile(lrc):
            continue
        key = os.path.realpath(lrc)
        if key in seen:
            continue
        seen.add(key)
        if not any(_within(lrc, d) for d in media_dirs):
            continue
        if any(os.path.exists(f"{stem}.{ext}") for ext in AUDIO_EXTS):
            continue
        try:
            if now - os.path.getmtime(lrc) < min_age_s:
                continue
        except OSError:
            continue
        _safe_remove(lrc, removed)
    if removed:
        _purge_dead_refs(cache_dir, removed)
    return removed


def _cache_dir_from_env(base: str) -> str:
    env_path = os.path.join(base, ".env")
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"(?:export\s+)?FNMUSIC_CACHE_DIR\s*=\s*(.+)$", line)
                if m:
                    value = m.group(1).strip().strip("'\"")
                    if value:
                        return value
    except OSError:
        pass
    return os.path.join(base, "cache")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理在线试听滚动缓存（曲库引用与其余数据不受影响）")
    parser.add_argument("--base", default=os.getcwd(), help="项目根目录（从 .env 读取 FNMUSIC_CACHE_DIR，缺省 <base>/cache）")
    parser.add_argument("--keep", type=int, default=0, help="保留最新 N 条滚动音频（默认 0 即全清）")
    args = parser.parse_args(argv)
    cache_dir = _cache_dir_from_env(args.base)
    removed = purge_rolling(cache_dir, keep=args.keep)
    print(f"rolling-cache purge: dir={cache_dir} removed={len(removed)}")
    for path in removed:
        print(f"  removed: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
