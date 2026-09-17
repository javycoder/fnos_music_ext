#!/usr/bin/env python3
"""在线试听滚动缓存清理，运行时淘汰与 restore 还原共用。

滚动缓存产物 = cache 目录下 online_*.<音频扩展名>（cache_safe_guid 命名）及同名 .lrc。
.ref 只在目标文件（音频或 .lrc）已不存在时删除：指向飞牛曲库中仍存在文件的 .ref
必须保留，否则 restore 后重播同曲会重新出网，在曲库里产生重复文件。
"""
from __future__ import annotations

import argparse
import os
import re
import sys

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
    try:
        entries = list(os.scandir(cache_dir))
    except (FileNotFoundError, NotADirectoryError):
        return removed
    for e in entries:
        if not (e.is_file() and e.name.endswith(".ref")):
            continue
        stem = _ref_stem(e.path)
        if not stem or not _stem_has_file(stem):
            _safe_remove(e.path, removed)
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
