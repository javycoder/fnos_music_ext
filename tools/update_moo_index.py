#!/usr/bin/env python3
"""moo 商店协议源索引生成/更新（issue #36）。

从 GitHub Releases 读取全部正式版本（跳过 prerelease 与带字母尾巴的迭代号
如 2.9.0a），以 Release 资产的真实 download_url/size/sha256 与 Release 说明
（changelog）生成/更新仓库根目录 moo.json——moo 商店协议源要求的溯源格式。

发布工作流在 Release 上传后自动调用（.github/workflows/release.yml 的
update-moo-index job），也可本地手动执行：

    python3 tools/update_moo_index.py [--repo javycoder/fnos_music_ext] \
        [--out moo.json] [--token "$GITHUB_TOKEN"] [--max-releases 10]

幂等：同一版本重跑只刷新该版本条目与 updated_at；迭代预发布（2.9.0a 等）
不改写索引（索引始终指向最新正式版，update-check 同口径）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

DEFAULT_REPO = "javycoder/fnos_music_ext"
APP_KEY = "fnmusic-ext"
# 沿用仓库文档的时区口径（README/CHANGELOG 均为东八区）
_TZ = timezone(timedelta(hours=8))

# moo 协议源参考样例（Blue-Mink/moo main 分支 moo.json）中的条目静态字段；
# 图标/说明直接指向本仓库 main 分支的 raw 地址，随仓库内容自然更新
_ICON_URL = f"https://raw.githubusercontent.com/{DEFAULT_REPO}/main/packaging/fpk/ICON_256.PNG"
_README_URL = f"https://raw.githubusercontent.com/{DEFAULT_REPO}/main/README.md"
_DESC = ("飞牛音乐第三方音源扩展：零侵入接管官方音乐入口，提供 musicdl 聚合、网易云、"
         "洛雪自定义源在线搜索播放、热门/每日推荐、边听边存与收藏自动下载，内置管理页。")
_LABELS = "影音娱乐"
_LICENSE = "MIT"
_SERVICE_PORT = 8774  # WebUI（经飞牛网关同源暴露；仅供商店展示口径）

_FORMAL_TAG_RE = re.compile(r"^v?(\d+(?:\.\d+)*)$")


def formal_version(tag: str) -> "str | None":
    """正式版本号（纯数字点分）；v 前缀剥离，字母尾巴（2.9.0a）与杂形返回 None。"""
    m = _FORMAL_TAG_RE.match(str(tag or "").strip())
    return m.group(1) if m else None


def _http_json(url: str, token: str = "") -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_formal_releases(repo: str, token: str = "", limit: int = 30) -> list[dict]:
    """按发布时间倒序返回正式版本 release（含资产与说明正文）。"""
    releases = _http_json(f"https://api.github.com/repos/{repo}/releases?per_page={limit}", token)
    if not isinstance(releases, list):
        raise RuntimeError(f"GitHub Releases 接口返回异常: {type(releases).__name__}")
    out = []
    for rel in releases:
        if not isinstance(rel, dict) or rel.get("prerelease"):
            continue
        ver = formal_version(str(rel.get("tag_name") or ""))
        if not ver:
            continue
        fpk = None
        for asset in rel.get("assets") or []:
            name = str(asset.get("name") or "")
            if name.startswith("fnmusic-ext-") and name.endswith(".fpk"):
                fpk = asset
                break
        if not fpk:
            continue
        out.append({"version": ver, "fpk": fpk, "body": str(rel.get("body") or "").strip(),
                    "published_at": str(rel.get("published_at") or "")})
    out.sort(key=lambda r: r["published_at"], reverse=True)
    return out


def _asset_sha256(asset: dict) -> str:
    digest = str(asset.get("digest") or "")
    if digest.startswith("sha256:"):
        return digest[len("sha256:"):]
    return ""


def _clean_changelog(body: str) -> str:
    """Release 说明压成 moo changelog 用的纯文本（保段落，限长防索引膨胀）。"""
    text = body.strip()
    return text[:4000]


def _pkg(asset: dict) -> dict:
    url = str(asset.get("browser_download_url") or "")
    size = int(asset.get("size") or 0)
    sha = _asset_sha256(asset)
    if not sha:
        raise RuntimeError(f"资产缺少 sha256 digest（GitHub API 未返回且无本地兜底）: {url}")
    return {
        "download_url": url,
        "size": size,
        "size_mb": round(size / 1024 / 1024, 1),
        "sha256": sha,
    }


def build_release_entry(rel: dict, updated_at: str) -> dict:
    pkg = _pkg(rel["fpk"])
    # manifest platform=all：同一 FPK 同时适配 x86/arm，两架构指向同一资产
    return {
        "changelog": _clean_changelog(rel["body"]),
        "packages": {"x86": pkg, "arm": dict(pkg)},
        "updated_at": updated_at,
    }


def update_index(index: dict, releases: list[dict], updated_at: str) -> dict:
    """把正式版本合入索引：新版本在前，已有条目保持原位（幂等刷新）。"""
    index.setdefault("schema_version", "moo")
    source_info = index.setdefault("source_info", {})
    source_info.update({
        "name": "fnmusic-ext 官方源",
        "homepage": f"https://github.com/{DEFAULT_REPO}",
        "distributor": "javycoder",
        "generated_by": "tools/update_moo_index.py",
        "description": "fnMusic 音源扩展（fnos_music_ext）官方协议源索引：发布产物与版本信息由 release 工作流自动回填。",
        "updated_at": updated_at,
    })
    apps = index.setdefault("apps", {})
    app = apps.setdefault(APP_KEY, {})
    app.update({
        "display_name": "fnMusic 音源扩展",
        "app_type": "docker",
        "platform": ["x86", "arm"],
        "desc": _DESC,
        "author": "javycoder",
        "distributor": "javycoder",
        "maintainer": "javycoder",
        "maintainer_url": f"https://github.com/{DEFAULT_REPO}",
        "homepage": f"https://github.com/{DEFAULT_REPO}",
        "license": _LICENSE,
        "labels": _LABELS,
        "icon_url": _ICON_URL,
        "readme_url": _README_URL,
        "service_port": _SERVICE_PORT,
        "install_dep_apps": "trim.music",
    })
    rel_dict = app.setdefault("releases", {})
    for rel in releases:
        entry = build_release_entry(rel, updated_at)
        existing = rel_dict.get(rel["version"])
        if existing:
            # 幂等刷新：只更新内容，条目位置由下方统一重排
            existing.update(entry)
        else:
            rel_dict[rel["version"]] = entry
        first_release_at = app.get("first_release_at")
        if not first_release_at or rel["published_at"] < first_release_at:
            app["first_release_at"] = rel["published_at"] or updated_at
    if rel_dict:
        # 版本号降序重建（最新在前），不受插入顺序影响
        def _ver_key(v: str) -> tuple:
            return tuple(int(p) for p in str(v).split("."))
        app["releases"] = dict(sorted(rel_dict.items(), key=lambda kv: _ver_key(kv[0]), reverse=True))
        rel_dict = app["releases"]
        latest = next(iter(rel_dict.items()))
        latest_pkg = latest[1]["packages"]["x86"]
        app["version"] = latest[0]
        app["download_url"] = latest_pkg["download_url"]
        app["sha256"] = latest_pkg["sha256"]
        app["size"] = latest_pkg["size"]
    app["updated_at"] = updated_at
    return index


def validate_index(index: dict) -> None:
    """结构自校验：测试与 CI 共用，格式漂移直接失败。"""
    assert index.get("schema_version") == "moo", "schema_version 必须是 moo"
    app = (index.get("apps") or {}).get(APP_KEY)
    assert isinstance(app, dict), f"apps.{APP_KEY} 条目缺失"
    for field in ("display_name", "desc", "icon_url", "readme_url", "version",
                  "download_url", "sha256", "size", "platform"):
        assert app.get(field), f"缺少必填字段 {field}"
    assert re.fullmatch(r"[0-9a-f]{64}", str(app["sha256"])), "sha256 必须是 64 位十六进制"
    assert str(app["download_url"]).startswith("https://github.com/"), "download_url 必须指向 GitHub Release 资产"
    assert not re.search(r"[a-z]", str(app["version"])), "索引只收录正式版本号"
    releases = app.get("releases") or {}
    assert releases, "releases 不能为空"
    assert next(iter(releases)) == app["version"], "releases 首键必须与顶层 version 一致（最新在前）"
    for ver, entry in releases.items():
        pkgs = entry.get("packages") or {}
        assert set(pkgs) >= {"x86"}, f"{ver} 缺少 x86 包"
        for pkg in pkgs.values():
            assert re.fullmatch(r"[0-9a-f]{64}", str(pkg.get("sha256") or "")), f"{ver} 包 sha256 非法"
            assert str(pkg.get("download_url") or "").startswith("https://"), f"{ver} 包 download_url 非法"


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="生成/更新 moo 商店协议源索引")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "moo.json"))
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    parser.add_argument("--max-releases", type=int, default=10, help="索引保留的正式版本数（默认 10）")
    args = parser.parse_args(argv)

    releases = fetch_formal_releases(args.repo, args.token)[: max(1, args.max_releases)]
    if not releases:
        print("未找到任何带 FPK 资产的正式版本，索引不改动", file=sys.stderr)
        return 1

    index: dict = {}
    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                index = loaded
        except (OSError, json.JSONDecodeError) as e:
            print(f"现有索引不可读，将重建: {e}", file=sys.stderr)

    updated_at = datetime.now(_TZ).isoformat(timespec="seconds")
    update_index(index, releases, updated_at)
    validate_index(index)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
        f.write("\n")
    latest = releases[0]
    print(f"moo.json 已更新：最新正式版 {latest['version']}（共 {len(releases)} 个版本条目）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
