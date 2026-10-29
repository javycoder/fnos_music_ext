"""moo 商店协议源索引测试（issue #36）。

不访问网络：用内存构造的 release 数据驱动 tools/update_moo_index.py 的
纯函数（formal_version / package_version / select_formal_release /
build_release_entry / update_index / validate_index），锁定——

- 正式身份：无字母 tag + draft/prerelease 显式布尔 false；缺失/字符串标志、
  预发布、草稿、畸形包都不入索引
- 安装版本必须取自实际 FPK 文件名并保留数字修订号（v2.11.0 +
  fnmusic-ext-2.11.0-4.fpk → 2.11.0-4，绝不降成 2.11.0）
- 历史 x.y.z 附件继续兼容；错 base / 畸形包直接拒绝
- 索引结构（schema_version/apps 字段/releases 数值降序/顶层最新一致）
- 版本比较按 base 三段 + 数字修订号（2.11.0-10 > 2.11.0-4 > 2.11.0-0）
- 幂等刷新与新修订置顶
- 包 download_url/sha256/size 与安装版本、x86/arm 一致
- packaging/fpk 的 FPK 命名与 manifest 版本口径与索引一致（仅静态核对）
- 真实 moo.json（若存在）通过结构自校验

真实的 FPK 构建、manifest 解包与 GitHub Release 资产均不在离线测试范围内。
"""
import importlib.util
import json
import os
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[2] / "tools"
_spec = importlib.util.spec_from_file_location("update_moo_index", _TOOLS / "update_moo_index.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _asset(version: str, sha: str, tag: "str | None" = None) -> dict:
    base = version.partition("-")[0]
    tag = tag or f"v{base}"
    name = f"fnmusic-ext-{version}.fpk"
    return {
        "name": name,
        "browser_download_url": f"https://github.com/x/y/releases/download/{tag}/{name}",
        "size": 12345,
        "digest": f"sha256:{sha}",
    }


def _rel(version: str, sha: str, published: str, tag: "str | None" = None) -> dict:
    """update_index 的规范化输入（安装版本已从 FPK 文件名解析，沿用既有约定）。"""
    return {
        "version": version,
        "published_at": published,
        "body": f"{version} 更新说明",
        "fpk": _asset(version, sha, tag),
    }


def _github_release(tag: str, version: str, sha: str = "", published: str = "",
                    prerelease: bool = False, asset_name: "str | None" = None) -> dict:
    """GitHub Releases API 形状的原始条目（tag 与安装版本分开）。"""
    sha = sha or _SHA_A
    published = published or "2026-10-29T12:00:00Z"
    fpk = _asset(version, sha, tag)
    if asset_name:
        fpk["name"] = asset_name
    checksum = {
        "name": f"{fpk['name']}.sha256",
        "browser_download_url": f"{fpk['browser_download_url']}.sha256",
        "size": 64,
        "digest": f"sha256:{'0' * 64}",
    }
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": prerelease,
        "body": f"{tag} 更新说明",
        "published_at": published,
        "assets": [fpk, checksum],
    }


_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_SHA_D = "d" * 64


def _valid_index() -> dict:
    index: dict = {}
    mod.update_index(index, [_rel("2.11.0-4", _SHA_A, "2026-10-29T12:00:00Z")], "t")
    return index


def test_formal_tag_and_fpk_version_rules():
    # 正式 tag 只有三段 base，不带数字修订号
    assert mod.formal_version("v2.11.0") == "2.11.0"
    assert mod.formal_version("2.8.3") == "2.8.3"
    assert mod.formal_version("v2.11.0-4") is None
    assert mod.formal_version("v2.11.0e") is None
    assert mod.formal_version("v2.11.0-rc1") is None
    assert mod.formal_version("v2.11") is None
    assert mod.formal_version("v2.11.00") is None
    assert mod.formal_version("") is None

    # 安装版本只认实际 FPK 文件名，数字修订号必须保留
    assert mod.package_version("fnmusic-ext-2.11.0-4.fpk") == "2.11.0-4"
    assert mod.package_version("fnmusic-ext-2.11.0.fpk") == "2.11.0"
    assert mod.package_version("fnmusic-ext-2.10.2.fpk") == "2.10.2"
    assert mod.package_version("fnmusic-ext-2.11.0-04.fpk") is None
    assert mod.package_version("fnmusic-ext-2.11.fpk") is None
    assert mod.package_version("fnmusic-ext-2.11.0-4.fpk.sha256") is None
    assert mod.package_version("fnmusic-ext-2.11.0-4.zip") is None
    assert mod.package_version("other-2.11.0-4.fpk") is None


def test_select_formal_release_keeps_install_revision():
    entry = mod.select_formal_release(_github_release("v2.11.0", "2.11.0-4"))
    assert entry is not None
    assert entry["version"] == "2.11.0-4"
    assert entry["tag"] == "v2.11.0"
    assert entry["tag_base"] == "2.11.0"
    assert entry["fpk"]["name"] == "fnmusic-ext-2.11.0-4.fpk"


def test_select_formal_release_filters_prerelease_and_draft():
    # prerelease=true 的字母 tag
    assert mod.select_formal_release(
        _github_release("v2.11.0e", "2.11.0-4", prerelease=True)) is None
    # 即便 prerelease=false，带字母的 tag 也不是正式版
    assert mod.select_formal_release(
        _github_release("v2.11.0e", "2.11.0-4", prerelease=False)) is None
    # GitHub 草稿发行不收
    assert mod.select_formal_release(
        dict(_github_release("v2.11.0", "2.11.0-4"), draft=True)) is None
    # 正式条目仍然保留
    assert mod.select_formal_release(
        _github_release("v2.11.0", "2.11.0-4"))["version"] == "2.11.0-4"


def test_select_formal_release_requires_boolean_release_flags():
    base = _github_release("v2.11.0", "2.11.0-4")
    # 缺失标志：失败关闭，不猜默认值
    missing_draft = dict(base)
    missing_draft.pop("draft")
    assert mod.select_formal_release(missing_draft) is None
    missing_prerelease = dict(base)
    missing_prerelease.pop("prerelease")
    assert mod.select_formal_release(missing_prerelease) is None
    # 非布尔标志（字符串/数字/None）一律不是正式版
    for value in ("false", "true", 0, 1, None):
        assert mod.select_formal_release(dict(base, draft=value)) is None
        assert mod.select_formal_release(dict(base, prerelease=value)) is None
    # 显式 False/False 才收录
    entry = mod.select_formal_release(dict(base, draft=False, prerelease=False))
    assert entry is not None and entry["version"] == "2.11.0-4"


@pytest.mark.parametrize("tag, version, asset_name", [
    ("v2.11.0", "2.10.2", None),                          # 错 base
    ("v2.11.0", "2.11.0-4", "fnmusic-ext-2.11.0-04.fpk"),  # 前导零
    ("v2.11.0", "2.11.0-4", "fnmusic-ext-2.11.fpk"),      # 少一段
    ("v2.11.0", "2.11.0-4", "fnmusic-ext-2.11.0-4.fpk.bak"),  # 畸形尾缀
    ("v2.11.0", "2.11.0-4", "fnmusic-ext-2.11.0-4.zip"),  # 非 FPK
])
def test_select_formal_release_rejects_wrong_base_or_malformed_packages(tag, version, asset_name):
    rel = _github_release(tag, version, asset_name=asset_name)
    assert mod.select_formal_release(rel) is None


def test_select_formal_release_rejects_missing_or_ambiguous_fpk():
    rel = _github_release("v2.11.0", "2.11.0-4")
    rel["assets"] = [{"name": "notes.txt"}]
    assert mod.select_formal_release(rel) is None

    rel = _github_release("v2.11.0", "2.11.0-4")
    rel["assets"].append(_asset("2.11.0-5", _SHA_B))
    assert mod.select_formal_release(rel) is None

    rel = _github_release("v2.11.0", "2.11.0-4")
    rel.pop("assets")
    assert mod.select_formal_release(rel) is None


def test_build_release_entry_rejects_version_downgrade():
    # 本次修复的核心事故：v2.11.0 tag 不能把安装版本降成 2.11.0
    rel = _rel("2.11.0-4", _SHA_A, "2026-10-29T12:00:00Z")
    rel["version"] = "2.11.0"
    with pytest.raises(ValueError):
        mod.build_release_entry(rel, "t")


def test_build_release_entry_rejects_tag_base_mismatch():
    rel = _rel("2.10.2", _SHA_A, "2026-10-25T12:00:00Z")
    rel["tag"] = "v2.11.0"
    rel["tag_base"] = "2.11.0"
    with pytest.raises(ValueError):
        mod.build_release_entry(rel, "t")


def test_build_release_entry_rejects_url_without_install_revision():
    rel = _rel("2.11.0-4", _SHA_A, "2026-10-29T12:00:00Z")
    rel["fpk"]["browser_download_url"] = (
        "https://github.com/x/y/releases/download/v2.11.0/fnmusic-ext-2.11.0.fpk")
    with pytest.raises(ValueError):
        mod.build_release_entry(rel, "t")


def test_build_release_entry_requires_sha_and_positive_size():
    rel = _rel("2.11.0-4", _SHA_A, "2026-10-29T12:00:00Z")
    rel["fpk"].pop("digest")
    with pytest.raises(RuntimeError):
        mod.build_release_entry(rel, "t")

    rel = _rel("2.11.0-4", _SHA_A, "2026-10-29T12:00:00Z")
    rel["fpk"]["size"] = 0
    with pytest.raises(RuntimeError):
        mod.build_release_entry(rel, "t")


def test_update_index_formal_release_keeps_install_revision():
    index: dict = {}
    mod.update_index(index, [_rel("2.11.0-4", _SHA_A, "2026-10-29T12:00:00Z")], "t")
    mod.validate_index(index)
    app = index["apps"]["fnmusic-ext"]
    assert list(app["releases"]) == ["2.11.0-4"]
    assert "2.11.0" not in app["releases"], "安装版本不得被降成 tag 的 2.11.0"
    assert app["version"] == "2.11.0-4"
    assert app["download_url"] == (
        "https://github.com/x/y/releases/download/v2.11.0/fnmusic-ext-2.11.0-4.fpk")
    assert app["sha256"] == _SHA_A
    assert app["size"] == 12345
    entry = app["releases"]["2.11.0-4"]
    assert entry["changelog"] == "2.11.0-4 更新说明"
    assert entry["packages"]["x86"] == entry["packages"]["arm"]
    assert entry["packages"]["x86"]["sha256"] == _SHA_A
    assert entry["packages"]["x86"]["size"] == 12345
    assert entry["packages"]["x86"]["download_url"] == app["download_url"]


def test_update_index_keeps_historical_xy_z_assets():
    index: dict = {}
    # 乱序喂入：历史 2.10.2（无修订）与新正式版 2.11.0-4（有修订）
    mod.update_index(index, [
        _rel("2.10.2", _SHA_B, "2026-10-25T12:00:00Z"),
        _rel("2.11.0-4", _SHA_A, "2026-10-29T12:00:00Z"),
    ], "t")
    mod.validate_index(index)
    app = index["apps"]["fnmusic-ext"]
    assert list(app["releases"]) == ["2.11.0-4", "2.10.2"]
    assert app["version"] == "2.11.0-4"
    assert app["sha256"] == _SHA_A
    legacy = app["releases"]["2.10.2"]["packages"]["x86"]
    assert legacy["download_url"].endswith("/v2.10.2/fnmusic-ext-2.10.2.fpk")
    assert legacy["sha256"] == _SHA_B


def test_update_index_sorts_same_base_revisions_numerically():
    index: dict = {}
    mod.update_index(index, [
        _rel("2.11.0-4", _SHA_A, "2026-10-29T12:00:00Z"),
        _rel("2.11.0-10", _SHA_B, "2026-10-29T13:00:00Z"),
        _rel("2.10.2", _SHA_C, "2026-10-25T12:00:00Z"),
        _rel("2.11.0-0", _SHA_D, "2026-10-29T10:00:00Z"),
    ], "t")
    mod.validate_index(index)
    app = index["apps"]["fnmusic-ext"]
    assert list(app["releases"]) == ["2.11.0-10", "2.11.0-4", "2.11.0-0", "2.10.2"]
    assert app["version"] == "2.11.0-10"
    assert app["sha256"] == _SHA_B
    assert app["first_release_at"] == "2026-10-25T12:00:00Z"


def test_version_key_orders_base_and_revision_numerically():
    assert mod.version_key("2.11.0-10") > mod.version_key("2.11.0-4")
    assert mod.version_key("2.11.0-4") > mod.version_key("2.11.0-0")
    # 历史无修订号 x.y.z 排在同样 base 的 -0 之前
    assert mod.version_key("2.11.0-0") > mod.version_key("2.11.0")
    assert mod.version_key("2.11.0") > mod.version_key("2.10.9")
    assert mod.version_key("2.10.2") > mod.version_key("2.9.99")
    assert mod.version_key("2.11.0-4") == (2, 11, 0, 4)
    assert mod.version_key("2.10.2") == (2, 10, 2, -1)
    with pytest.raises(ValueError):
        mod.version_key("2.11.0e")


def test_update_index_idempotent_for_revision():
    index: dict = {}
    rel = _rel("2.11.0-4", _SHA_A, "2026-10-29T12:00:00Z")
    mod.update_index(index, [rel], "t1")
    first_pass = json.dumps(
        index["apps"]["fnmusic-ext"]["releases"]["2.11.0-4"], ensure_ascii=False, sort_keys=True)
    # 同版本重跑：内容等价（仅 updated_at 刷新）、键与顺序不变
    mod.update_index(index, [rel], "t2")
    app = index["apps"]["fnmusic-ext"]
    assert list(app["releases"]) == ["2.11.0-4"]
    assert app["version"] == "2.11.0-4"
    refreshed = dict(app["releases"]["2.11.0-4"])
    refreshed.pop("updated_at")
    baseline = json.loads(first_pass)
    baseline.pop("updated_at")
    assert refreshed == baseline
    assert app["releases"]["2.11.0-4"]["updated_at"] == "t2"
    # 同 base 的新修订号置顶
    mod.update_index(index, [_rel("2.11.0-10", _SHA_B, "2026-10-30T12:00:00Z")], "t3")
    mod.validate_index(index)
    assert list(app["releases"]) == ["2.11.0-10", "2.11.0-4"]
    assert app["version"] == "2.11.0-10"


@pytest.mark.parametrize("version", [
    "2.11.0e",       # 字母
    "v2.11.0-4",     # v 前缀
    "2.11",          # 少一段
    "2.11.0-04",     # 前导零
    "2.11.0-4.1",    # 多段修订
])
def test_update_index_rejects_malformed_install_version(version):
    with pytest.raises(ValueError):
        mod.update_index({}, [_rel(version, _SHA_A, "2026-10-29T12:00:00Z")], "t")


def test_validate_index_rejects_drift():
    # 顶层 version 与 releases 首键脱钩
    index = _valid_index()
    index["apps"]["fnmusic-ext"]["version"] = "9.9.9"
    with pytest.raises(AssertionError):
        mod.validate_index(index)

    # 把安装版本降成 tag 的 x.y.z（URL 仍指 -4）
    index = _valid_index()
    app = index["apps"]["fnmusic-ext"]
    app["version"] = "2.11.0"
    app["releases"] = {"2.11.0": app["releases"]["2.11.0-4"]}
    with pytest.raises(AssertionError):
        mod.validate_index(index)

    # 字母版本键（预发布）不得进索引
    index = _valid_index()
    app = index["apps"]["fnmusic-ext"]
    app["version"] = "2.11.0e"
    app["releases"] = {"2.11.0e": app["releases"]["2.11.0-4"]}
    with pytest.raises(AssertionError):
        mod.validate_index(index)

    # 包 URL 指向的文件名与版本键不一致
    index = _valid_index()
    entry = index["apps"]["fnmusic-ext"]["releases"]["2.11.0-4"]
    entry["packages"]["x86"]["download_url"] = (
        "https://github.com/x/y/releases/download/v2.11.0/fnmusic-ext-2.11.0.fpk")
    with pytest.raises(AssertionError):
        mod.validate_index(index)

    # x86/arm 必须指向同一 FPK
    index = _valid_index()
    index["apps"]["fnmusic-ext"]["releases"]["2.11.0-4"]["packages"]["arm"]["sha256"] = _SHA_B
    with pytest.raises(AssertionError):
        mod.validate_index(index)

    # 同 base 数字修订顺序漂移
    index: dict = {}
    mod.update_index(index, [
        _rel("2.11.0-4", _SHA_A, "2026-10-29T12:00:00Z"),
        _rel("2.10.2", _SHA_B, "2026-10-25T12:00:00Z"),
        _rel("2.9.0", _SHA_C, "2026-10-12T12:00:00Z"),
    ], "t")
    rels = index["apps"]["fnmusic-ext"]["releases"]
    index["apps"]["fnmusic-ext"]["releases"] = {
        key: rels[key] for key in ("2.11.0-4", "2.9.0", "2.10.2")}
    with pytest.raises(AssertionError):
        mod.validate_index(index)


def test_fetch_formal_releases_normalizes_tag_and_package_version(monkeypatch):
    payload = [
        _github_release("v2.11.0", "2.11.0-4", _SHA_A, "2026-10-29T12:00:00Z"),
        _github_release("v2.11.0e", "2.11.0-4", _SHA_B, "2026-10-29T18:00:00Z",
                        prerelease=True),
        _github_release("v2.10.2", "2.10.2", _SHA_C, "2026-10-25T12:00:00Z"),
        {
            "tag_name": "v2.10.1", "draft": False, "prerelease": False,
            "assets": [{"name": "notes.txt"}], "published_at": "2026-10-20T12:00:00Z",
        },
    ]
    monkeypatch.setattr(mod, "_http_json", lambda url, token="": payload)
    found = mod.fetch_formal_releases("x/y", "", 10)
    assert [rel["version"] for rel in found] == ["2.11.0-4", "2.10.2"]
    assert found[0]["tag"] == "v2.11.0"
    assert found[0]["tag_base"] == "2.11.0"

    index: dict = {}
    mod.update_index(index, found, "t")
    mod.validate_index(index)
    app = index["apps"]["fnmusic-ext"]
    assert list(app["releases"]) == ["2.11.0-4", "2.10.2"]
    assert app["version"] == "2.11.0-4"
    assert app["download_url"].endswith("/v2.11.0/fnmusic-ext-2.11.0-4.fpk")


def test_packaging_uses_install_version_in_fpk_name_and_manifest():
    """静态核对打包口径：文件名/manifest 都用带 -N 的安装版本，不是 tag 显示版本。"""
    root = _TOOLS.parent
    build = (root / "packaging/fpk/build.sh").read_text(encoding="utf-8")
    manifest = (root / "packaging/fpk/manifest.in").read_text(encoding="utf-8")
    assert 'VERSION="$(python3 "${REPO_ROOT}/proxy/version.py" --format version)"' in build
    assert 'FINAL="${OUT_DIR}/fnmusic-ext-${VERSION}.fpk"' in build
    assert 'sed "s/@VERSION@/${VERSION}/"' in build
    assert "version               = @VERSION@" in manifest

    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    assert mod.package_version(f"fnmusic-ext-{version}.fpk") == version


def test_repo_moo_json_if_present():
    """仓库根的 moo.json（发布工作流生成）必须始终通过结构自校验。"""
    path = _TOOLS.parent / "moo.json"
    if not os.path.exists(path):
        pytest.skip("moo.json 尚未生成")
    with open(path, encoding="utf-8") as f:
        mod.validate_index(json.load(f))
