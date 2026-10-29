#!/usr/bin/env python3
"""moo 商店协议源索引生成/更新（issue #36）。

从 GitHub Releases 读取全部正式版本（跳过 prerelease 与带字母尾巴的迭代号
如 2.11.0e），以 Release 资产的真实 download_url/size/sha256 与 Release 说明
（changelog）生成/更新仓库根目录 moo.json——moo 商店协议源要求的溯源格式。

正式版身份只看「无字母 tag + draft/prerelease 显式布尔 false」；索引里的安装
版本必须取自实际 FPK 文件名并保留数字修订号（fnmusic-ext-2.11.0-4.fpk →
2.11.0-4），绝不降成 tag 的 2.11.0——否则应用中心会认为版本没变而跳过升级。
历史发行仍可能使用 fnmusic-ext-2.10.2.fpk 这类无修订附件，继续兼容。

发布工作流在 Release 上传后自动调用（.github/workflows/release.yml 的
update-moo-index job），也可本地手动执行：

    python3 tools/update_moo_index.py [--repo javycoder/fnos_music_ext] \
        [--out moo.json] [--token "$GITHUB_TOKEN"] [--max-releases 10]

幂等：同一版本重跑只刷新该版本条目与 updated_at；预发布不改写索引（索引始终
指向最新正式版，update-check 同口径）。
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

# 安装版本口径与 proxy/version.py / packaging/fpk/build.sh 一致：
# 三段式 base + 可选 -N 数字修订号，均不允许前导零；FPK 文件名就是该版本。
_BASE_PATTERN = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
_REVISION_PATTERN = r"(?:0|[1-9][0-9]*)"
_INSTALL_VERSION_RE = re.compile(rf"^{_BASE_PATTERN}(?:-{_REVISION_PATTERN})?$")
# 正式 tag 只有 vx.y.z：数字修订号属于安装包，字母尾巴属于预发布迭代号。
_FORMAL_TAG_RE = re.compile(rf"^v?({_BASE_PATTERN})$")
# 实际 FPK 文件名：fnmusic-ext-<安装版本>.fpk（版本含 -N，历史发行可无 -N）
_FPK_NAME_RE = re.compile(rf"^fnmusic-ext-({_BASE_PATTERN}(?:-{_REVISION_PATTERN})?)\.fpk$")


def formal_version(tag: str) -> "str | None":
    """正式 tag 的三段 base（v2.11.0 → 2.11.0）；字母尾巴与杂形返回 None。"""
    match = _FORMAL_TAG_RE.fullmatch(str(tag or "").strip())
    return match.group(1) if match else None


def package_version(asset_name: str) -> "str | None":
    """从 FPK 文件名提取实际安装版本（保留数字修订号）；畸形返回 None。"""
    match = _FPK_NAME_RE.fullmatch(str(asset_name or "").strip())
    return match.group(1) if match else None


def _require_install_version(version: str) -> str:
    """严格校验安装版本，避免字母/前导零/少段版本混进索引。"""
    version = str(version or "").strip()
    if not _INSTALL_VERSION_RE.fullmatch(version):
        raise ValueError(
            f"安装版本必须为 x.y.z 或 x.y.z-N（不允许字母或前导零），当前为 {version!r}")
    return version


def package_base(version: str) -> str:
    """安装版本的三段 base；格式不严格直接报错（fail-closed）。"""
    return _require_install_version(version).partition("-")[0]


def version_key(version: str) -> tuple[int, int, int, int]:
    """索引排序键：三段 base + 数字修订号。

    历史无修订号 x.y.z 视为 -1，排在同样 base 的 -0 之前；数字修订号按数值
    比较（2.11.0-10 > 2.11.0-4），绝不因为带 -N 就把正式版当预发布排除。
    """
    version = _require_install_version(version)
    base, _, revision = version.partition("-")
    major, minor, patch = (int(part) for part in base.split("."))
    return (major, minor, patch, int(revision) if revision else -1)


def _http_json(url: str, token: str = "") -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _release_fpk(rel: dict) -> "tuple[str, str, dict] | None":
    """定位 Release 里唯一合法的 FPK：tag base 与包 base 必须一致。

    返回 (tag_base, 安装版本, 资产)；非正式 tag、畸形包名、错 base、同发行
    多个安装版本等一律返回 None（宁可漏收也不把错误版本写进索引）。
    """
    tag_base = formal_version(str(rel.get("tag_name") or ""))
    if tag_base is None:
        return None
    assets = rel.get("assets")
    if not isinstance(assets, list):
        return None
    candidates: dict[str, dict] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        if not (name.startswith("fnmusic-ext-") and name.endswith(".fpk")):
            continue
        version = package_version(name)
        if version is None:
            return None  # 形似 FPK 但版本畸形：整条发行拒绝
        candidates.setdefault(version, asset)
    if len(candidates) != 1:
        return None
    version, fpk = next(iter(candidates.items()))
    if package_base(version) != tag_base:
        return None
    return tag_base, version, fpk


def select_formal_release(rel: dict) -> "dict | None":
    """筛选并规范化单个 GitHub Release；非正式/畸形直接跳过（返回 None）。

    正式身份：无字母 tag + draft/prerelease 显式为布尔 false（缺失、字符串、
    0/1 等一律失败关闭）；安装版本只从实际 FPK 文件名取，因此 v2.11.0 +
    fnmusic-ext-2.11.0-4.fpk 得到 2.11.0-4。
    """
    if not isinstance(rel, dict):
        return None
    # GitHub 明确返回 draft/prerelease 布尔值；缺失或非 bool 不接受，避免把
    # 预发布/草稿误当正式版收进稳定索引。
    if rel.get("draft") is not False or rel.get("prerelease") is not False:
        return None
    found = _release_fpk(rel)
    if found is None:
        return None
    tag_base, version, fpk = found
    return {
        "version": version,
        "tag": str(rel.get("tag_name") or "").strip(),
        "tag_base": tag_base,
        "fpk": fpk,
        "body": str(rel.get("body") or "").strip(),
        "published_at": str(rel.get("published_at") or ""),
    }


def fetch_formal_releases(repo: str, token: str = "", limit: int = 30) -> list[dict]:
    """按发布时间倒序返回正式版本 release（含资产与说明正文）。"""
    releases = _http_json(f"https://api.github.com/repos/{repo}/releases?per_page={limit}", token)
    if not isinstance(releases, list):
        raise RuntimeError(f"GitHub Releases 接口返回异常: {type(releases).__name__}")
    out = []
    for rel in releases:
        entry = select_formal_release(rel)
        if entry:
            out.append(entry)
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
    if size <= 0:
        raise RuntimeError(f"资产 size 异常（应为正整数）: {url}")
    return {
        "download_url": url,
        "size": size,
        "size_mb": round(size / 1024 / 1024, 1),
        "sha256": sha,
    }


def build_release_entry(rel: dict, updated_at: str) -> dict:
    """构造单个版本条目；索引版本与 FPK 文件名/正式 tag base 必须一致。"""
    version = _require_install_version(rel.get("version"))
    fpk = rel.get("fpk") or {}
    source_name = (str(fpk.get("name") or "")
                   or str(fpk.get("browser_download_url") or "").rsplit("/", 1)[-1])
    if source_name:
        asset_version = package_version(source_name)
        if asset_version != version:
            raise ValueError(
                f"索引版本 {version!r} 与 FPK 文件名 {source_name!r} 不一致："
                "安装版本必须取自实际 FPK 并保留数字修订号")
    tag_base = rel.get("tag_base")
    if not tag_base and rel.get("tag"):
        tag_base = formal_version(str(rel.get("tag")))
    if tag_base and package_base(version) != tag_base:
        raise ValueError(
            f"正式 tag base {tag_base!r} 与安装版本 {version!r} 的 base 不一致")
    pkg = _pkg(fpk)
    expected = f"fnmusic-ext-{version}.fpk"
    if pkg["download_url"] and pkg["download_url"].rsplit("/", 1)[-1] != expected:
        raise ValueError(f"download_url 必须指向实际 FPK 文件名 {expected}: {pkg['download_url']}")
    # manifest platform=all：同一 FPK 同时适配 x86/arm，两架构指向同一资产
    return {
        "changelog": _clean_changelog(str(rel.get("body") or "")),
        "packages": {"x86": pkg, "arm": dict(pkg)},
        "updated_at": updated_at,
    }


def update_index(index: dict, releases: list[dict], updated_at: str) -> dict:
    """把正式版本合入索引：安装版本数值降序，已有条目保持内容幂等刷新。"""
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
        version = _require_install_version(rel.get("version"))
        entry = build_release_entry(rel, updated_at)
        existing = rel_dict.get(version)
        if existing:
            # 幂等刷新：只更新内容，条目位置由下方统一重排
            existing.update(entry)
        else:
            rel_dict[version] = entry
        first_release_at = app.get("first_release_at")
        published_at = str(rel.get("published_at") or "")
        if not first_release_at or published_at < first_release_at:
            app["first_release_at"] = published_at or updated_at
    if rel_dict:
        # 安装版本数值降序重建（base 三段 + 数字修订号），不受插入顺序影响
        app["releases"] = dict(sorted(
            rel_dict.items(), key=lambda kv: version_key(kv[0]), reverse=True))
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
    version = str(app["version"])
    try:
        _require_install_version(version)
    except ValueError as exc:
        raise AssertionError(str(exc)) from exc
    assert re.fullmatch(r"[0-9a-f]{64}", str(app["sha256"])), "sha256 必须是 64 位十六进制"
    assert str(app["download_url"]).startswith("https://github.com/"), "download_url 必须指向 GitHub Release 资产"
    assert str(app["download_url"]).rsplit("/", 1)[-1] == f"fnmusic-ext-{version}.fpk", \
        "顶层 download_url 必须指向顶层安装版本对应的 FPK"
    releases = app.get("releases") or {}
    assert releases, "releases 不能为空"
    keys = list(releases)
    assert keys[0] == version, "releases 首键必须与顶层 version 一致（最新在前）"
    try:
        ordered = sorted(keys, key=version_key, reverse=True)
    except ValueError as exc:
        raise AssertionError(str(exc)) from exc
    assert keys == ordered, "releases 必须按安装版本数值降序（同 base 按数字修订号）"
    for ver, entry in releases.items():
        try:
            _require_install_version(ver)
        except ValueError as exc:
            raise AssertionError(str(exc)) from exc
        expected = f"fnmusic-ext-{ver}.fpk"
        pkgs = entry.get("packages") or {}
        assert set(pkgs) >= {"x86"}, f"{ver} 缺少 x86 包"
        for arch, pkg in pkgs.items():
            assert re.fullmatch(r"[0-9a-f]{64}", str(pkg.get("sha256") or "")), \
                f"{ver}/{arch} 包 sha256 非法"
            url = str(pkg.get("download_url") or "")
            assert url.startswith("https://"), f"{ver}/{arch} 包 download_url 非法"
            assert url.rsplit("/", 1)[-1] == expected, \
                f"{ver}/{arch} 包 download_url 必须指向 {expected}"
            assert int(pkg.get("size") or 0) > 0, f"{ver}/{arch} 包 size 非法"
        if "arm" in pkgs:
            assert pkgs["arm"].get("sha256") == pkgs["x86"].get("sha256"), \
                f"{ver} 的 x86/arm 必须指向同一 FPK（sha256 一致）"
            assert int(pkgs["arm"].get("size") or 0) == int(pkgs["x86"].get("size") or 0), \
                f"{ver} 的 x86/arm size 必须一致"


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
    latest = index["apps"][APP_KEY]["version"]
    print(f"moo.json 已更新：最新正式版 {latest}（本次处理 {len(releases)} 个版本条目）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
