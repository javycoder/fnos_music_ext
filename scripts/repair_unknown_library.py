#!/usr/bin/env python3
"""修复曲库里以 unknown 命名落盘的边听边存文件（issue #28 存量数据修复）。

背景：2.5.0 之前，客户端在音频下载结束瞬间快速断开等场景会让流式上下文的
元数据解析失败，文件以 "unknown (n).<ext>" 命名进曲库，飞牛扫描后音乐库显示
Unknown 且无元数据。2.5.0 已从根上修复（断流护航 + 元数据兜底回查），本脚本
用于修复修复点上线之前已经落盘的存量文件。

原理：边听边存落盘时会写 <cache_dir>/<cache_safe_guid>.ref 记录文件的词干；
播放历史/在线收藏里保存了 guid → 标题/歌手快照。三者一拼即可把 unknown 文件
重新定名并写回 ID3/Vorbis 标签。

用法（默认只预览，不改动任何文件）：
    python3 scripts/repair_unknown_library.py            # 预览可修复项
    python3 scripts/repair_unknown_library.py --apply    # 实际重命名 + 写标签
    python3 scripts/repair_unknown_library.py --base /vol1/@appcenter/fnmusic-ext/repo

只处理文件名恰为 unknown / unknown (n) 的词干；改名永不覆盖已有文件（自动编号）；
.ref 与同名 .lrc 一并随迁。写标签失败只告警，不影响重命名。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

UNKNOWN_RE = re.compile(r"unknown( \(\d+\))?$", re.IGNORECASE)


def _load_env_file(base: str) -> None:
    """把部署目录 .env 里的 FNMUSIC_* 目录配置并入进程环境（不覆盖已有变量）。

    代理服务的环境由 takeover/installer 注入；独立运行本脚本时需自行读取 .env，
    否则 cache/library/history 目录会回落到仓库默认值而找不到真实数据。
    """
    try:
        from proxy.env_merge import parse_env_file

        pairs, _ = parse_env_file(os.path.join(base, ".env"))
    except Exception:
        return
    for key, value in pairs:
        if key.startswith(("FNMUSIC_", "LX_")):
            os.environ.setdefault(key, value)


def load_guid_snapshots(app) -> dict[str, dict]:
    """guid → {title, artist, album}（播放历史 + 在线收藏，形状同 _lookup_online_snapshot）。"""
    out: dict[str, dict] = {}
    import json

    dirs = []
    try:
        dirs.append(app.dailyrec.play_history_dir())
    except Exception:
        pass
    fav_dir = app.CONF.get("fav_dir") or os.path.join(app._HOME, "online_favorites")
    dirs.append(fav_dir)
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            items = data.get("items") if isinstance(data, dict) else data
            if not isinstance(items, list):
                continue
            for it in reversed(items):
                if not isinstance(it, dict):
                    continue
                guid = str(it.get("guid") or "")
                snap = it.get("track") if isinstance(it.get("track"), dict) else None
                if not guid or not isinstance(snap, dict):
                    continue
                title = str(snap.get("title") or "").strip()
                artist = str(snap.get("artist") or "").strip()
                if not (title and artist):
                    continue
                album = str(snap.get("album") or "")
                if isinstance(snap.get("album"), dict):
                    album = str((snap.get("album") or {}).get("name") or "")
                prev = out.get(guid)
                if prev is None or (album and not prev.get("album")):
                    out[guid] = {"title": title, "artist": artist, "album": album.strip()}
    return out


def retag(path: str, title: str, artist: str, album: str) -> str | None:
    """写回标签；返回错误信息或 None。tag 缺失时不写空值。"""
    try:
        import mutagen

        audio = mutagen.File(path, easy=True)
        if audio is None:
            return "unsupported-format"
        if title:
            audio["title"] = title
        if artist:
            audio["artist"] = artist
        if album:
            audio["album"] = album
        audio.save()
        return None
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="实际执行重命名与标签写入（默认仅预览）")
    parser.add_argument("--base", default=str(REPO), help="fnmusic-ext 部署目录（默认脚本所在仓库）")
    args = parser.parse_args()

    os.environ.setdefault("FNMUSIC_HOME", args.base)
    _load_env_file(args.base)
    import proxy.app as app

    cache_dir = app.CONF["cache_dir"]
    if not os.path.isdir(cache_dir):
        print(f"[SKIP] 缓存目录不存在：{cache_dir}")
        return 0
    snapshots = load_guid_snapshots(app)
    if not snapshots:
        print("[SKIP] 播放历史/在线收藏里没有可用的元数据快照")
        return 0

    plan: list[tuple[str, str, str, str, dict]] = []  # (audio, lrc, new_stem, ref, meta)
    seen_unknown = 0
    for guid, meta in snapshots.items():
        ref = app.media_ref_path(guid)
        if not os.path.exists(ref):
            continue
        try:
            with open(ref, encoding="utf-8") as f:
                stem = app._path_stem(f.read().strip())
        except Exception:
            continue
        if not stem or not UNKNOWN_RE.match(os.path.basename(stem)):
            continue
        seen_unknown += 1
        audio = next(
            (f"{stem}.{ext}" for ext in app.CACHE_EXTS
             if os.path.exists(f"{stem}.{ext}") and os.path.getsize(f"{stem}.{ext}") > 0),
            None,
        )
        if not audio:
            continue
        lrc = f"{stem}.lrc" if os.path.exists(f"{stem}.lrc") else ""
        directory = os.path.dirname(stem)
        new_stem = app.unique_library_path(directory, app.library_basename(meta["title"], meta["artist"]),
                                           "x")[:-2]  # 去掉占位 ".x"，保留 " (n)" 编号逻辑
        plan.append((audio, lrc, new_stem, ref, meta))

    if not plan:
        print(f"[OK] 扫描 {len(snapshots)} 个 guid / {seen_unknown} 个 unknown 词干：没有可修复项"
              "（文件可能已被删除或已修复）")
        return 0

    print(f"{'[DRY-RUN] ' if not args.apply else ''}待修复 {len(plan)} 个文件：\n")
    errors = 0
    for audio, lrc, new_stem, ref, meta in plan:
        old_audio = audio
        new_audio = f"{new_stem}{os.path.splitext(audio)[1]}"
        new_lrc = f"{new_stem}.lrc" if lrc else ""
        print(f"  {os.path.basename(old_audio)}")
        print(f"    → {os.path.basename(new_audio)}   ({meta['artist']} - {meta['title']}"
              + (f" / 专辑 {meta['album']}" if meta["album"] else "") + ")")
        if not args.apply:
            continue
        try:
            if os.path.exists(new_audio):
                print(f"    [SKIP] 目标已存在：{new_audio}")
                errors += 1
                continue
            os.rename(old_audio, new_audio)
            if lrc and new_lrc:
                os.rename(lrc, new_lrc)
            with open(ref, "w", encoding="utf-8") as f:
                f.write(new_stem)
            err = retag(new_audio, meta["title"], meta["artist"], meta["album"])
            if err:
                print(f"    [WARN] 标签写入失败（文件已改名）：{err}")
                errors += 1
        except OSError as e:
            print(f"    [ERROR] {e}")
            errors += 1

    if args.apply:
        print(f"\n[DONE] 已修复 {len(plan) - errors} 个文件，失败 {errors} 个。"
              "请在飞牛音乐里触发一次媒体库扫描（或等自动重扫）后查看。")
    else:
        print("\n[预览结束] 确认无误后加 --apply 执行。")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
