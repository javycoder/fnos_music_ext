"""moo 商店协议源索引测试（issue #36）。

不访问网络：用内存构造的 release 数据驱动 tools/update_moo_index.py 的
纯函数（formal_version / update_index / validate_index），锁定——
- 正式/迭代版本号口径（字母尾巴不入索引，与 update-check 同口径）
- 索引结构（schema_version/apps 字段/releases 版本降序/顶层最新一致）
- 幂等刷新与新版本置顶
- 真实 moo.json（若存在）通过结构自校验
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


def _rel(ver: str, sha: str, published: str, prerelease: bool = False) -> dict:
    return {
        "version": ver,
        "published_at": published,
        "body": f"{ver} 更新说明",
        "fpk": {
            "name": f"fnmusic-ext-{ver}.fpk",
            "browser_download_url": f"https://github.com/x/y/releases/download/v{ver}/fnmusic-ext-{ver}.fpk",
            "size": 12345,
            "digest": f"sha256:{sha}",
        },
        "prerelease": prerelease,
    }


_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64


def test_formal_version_rules():
    assert mod.formal_version("v2.9.0") == "2.9.0"
    assert mod.formal_version("2.8.3") == "2.8.3"
    # 迭代预发布与杂形不入索引
    assert mod.formal_version("v2.9.0a") is None
    assert mod.formal_version("v2.9.0-rc1") is None
    assert mod.formal_version("") is None


def test_update_index_orders_and_latest_fields():
    index = {}
    # 故意乱序喂入：最终必须版本降序、顶层指向最新
    mod.update_index(index, [
        _rel("2.8.3", _SHA_A, "2026-10-11T00:00:00Z"),
        _rel("2.9.0", _SHA_B, "2026-10-12T00:00:00Z"),
        _rel("2.8.2", _SHA_C, "2026-10-10T00:00:00Z"),
    ], "2026-10-12T00:00:00+08:00")
    mod.validate_index(index)
    app = index["apps"]["fnmusic-ext"]
    assert list(app["releases"]) == ["2.9.0", "2.8.3", "2.8.2"]
    assert app["version"] == "2.9.0"
    assert app["sha256"] == _SHA_B
    assert app["first_release_at"] == "2026-10-10T00:00:00Z"
    # x86/arm 双架构包指向同一 FPK（manifest platform=all）
    pkg = app["releases"]["2.8.3"]["packages"]
    assert pkg["x86"]["sha256"] == _SHA_A and pkg["arm"]["sha256"] == _SHA_A
    assert pkg["x86"]["download_url"].startswith("https://github.com/x/y/releases/download/")


def test_update_index_idempotent_and_prepends_newer():
    index = {}
    mod.update_index(index, [_rel("2.8.2", _SHA_C, "2026-10-10T00:00:00Z")], "t1")
    first_pass = json.dumps(index["apps"]["fnmusic-ext"]["releases"]["2.8.2"], ensure_ascii=False)
    # 同版本重跑：内容等价（仅 updated_at 时间戳刷新）、顺序不变
    mod.update_index(index, [_rel("2.8.2", _SHA_C, "2026-10-10T00:00:00Z")], "t2")
    assert list(index["apps"]["fnmusic-ext"]["releases"]) == ["2.8.2"]
    refreshed = dict(index["apps"]["fnmusic-ext"]["releases"]["2.8.2"])
    refreshed.pop("updated_at")
    baseline = json.loads(first_pass)
    baseline.pop("updated_at")
    assert refreshed == baseline
    # 新版本置顶
    mod.update_index(index, [_rel("2.8.3", _SHA_A, "2026-10-11T00:00:00Z")], "t3")
    mod.validate_index(index)
    assert list(index["apps"]["fnmusic-ext"]["releases"]) == ["2.8.3", "2.8.2"]
    assert index["apps"]["fnmusic-ext"]["version"] == "2.8.3"


def test_validate_index_rejects_drift():
    index = {}
    mod.update_index(index, [_rel("2.8.3", _SHA_A, "2026-10-11T00:00:00Z")], "t")
    # 顶层 version 与 releases 首键脱钩
    index["apps"]["fnmusic-ext"]["version"] = "9.9.9"
    with pytest.raises(AssertionError):
        mod.validate_index(index)


def test_repo_moo_json_if_present():
    """仓库根的 moo.json（发布工作流生成）必须始终通过结构自校验。"""
    path = _TOOLS.parent / "moo.json"
    if not os.path.exists(path):
        pytest.skip("moo.json 尚未生成")
    with open(path, encoding="utf-8") as f:
        mod.validate_index(json.load(f))
