from __future__ import annotations

import pytest

from proxy.update_release import (
    formal_version,
    normalize_arch,
    normalize_release,
    package_arch,
    package_base,
    package_version,
    parse_install_asset,
    select_release_assets,
)


def test_formal_version():
    assert formal_version("v2.12.4") == "2.12.4"
    assert formal_version("2.12.4") == "2.12.4"
    assert formal_version("v2.12.4a") is None
    assert formal_version("2.12.4-0") is None
    assert formal_version("v2.12.4-rc1") is None
    assert formal_version("") is None
    assert formal_version("invalid") is None


def test_parse_install_asset():
    assert parse_install_asset("fnmusic-ext-2.12.4-0.fpk") == ("2.12.4-0", None)
    assert parse_install_asset("fnmusic-ext-2.12.4-0-amd64.fpk") == ("2.12.4-0", "amd64")
    assert parse_install_asset("fnmusic-ext-2.12.4-0-arm64.fpk") == ("2.12.4-0", "arm64")
    assert parse_install_asset("fnmusic-ext-2.12.4.fpk") == ("2.12.4", None)

    assert parse_install_asset("fnmusic-ext-2.12.4-0a.fpk") is None
    assert parse_install_asset("fnmusic-ext-2.12.4-0.zip") is None
    assert parse_install_asset("otherapp-2.12.4-0.fpk") is None
    assert parse_install_asset("fnmusic-ext-2.12.4-0-mips.fpk") is None

    assert package_version("fnmusic-ext-2.12.4-0-amd64.fpk") == "2.12.4-0"
    assert package_arch("fnmusic-ext-2.12.4-0-amd64.fpk") == "amd64"
    assert package_arch("fnmusic-ext-2.12.4-0.fpk") is None


def test_package_base():
    assert package_base("2.12.4-0") == "2.12.4"
    assert package_base("2.12.4") == "2.12.4"
    with pytest.raises(ValueError):
        package_base("2.12.4a")


def test_normalize_arch():
    assert normalize_arch("x86_64") == "amd64"
    assert normalize_arch("AMD64") == "amd64"
    assert normalize_arch("aarch64") == "arm64"
    assert normalize_arch("arm64") == "arm64"


def test_normalize_release_rejects_prerelease_and_draft():
    raw_prerelease = {"tag_name": "v2.12.4", "prerelease": True, "assets": []}
    assert normalize_release(raw_prerelease, "github") is None

    raw_draft = {"tag_name": "v2.12.4", "draft": True, "assets": []}
    assert normalize_release(raw_draft, "github") is None

    raw_bad_tag = {"tag_name": "v2.12.4b", "prerelease": False, "assets": []}
    assert normalize_release(raw_bad_tag, "github") is None


def test_select_release_assets_universal_and_arch_specific():
    universal_release = {
        "tag_name": "v2.12.4",
        "formal_base": "2.12.4",
        "source": "github",
        "assets": [
            {
                "name": "fnmusic-ext-2.12.4-0.fpk",
                "browser_download_url": "https://example.com/fnmusic-ext-2.12.4-0.fpk",
                "size": 12345,
                "digest": "sha256:87ae7e5bce671217ca2c1f038f1904a629b0c826a4e4836a0af7a00bc1166d48",
            },
            {
                "name": "fnmusic-ext-2.12.4-0.fpk.sha256",
                "browser_download_url": "https://example.com/fnmusic-ext-2.12.4-0.fpk.sha256",
            },
        ],
    }
    res = select_release_assets(universal_release, "amd64")
    assert res is not None
    assert res["install_version"] == "2.12.4-0"
    assert res["asset_name"] == "fnmusic-ext-2.12.4-0.fpk"
    assert res["sha256"] == "87ae7e5bce671217ca2c1f038f1904a629b0c826a4e4836a0af7a00bc1166d48"
    assert res["sha256_url"] == "https://example.com/fnmusic-ext-2.12.4-0.fpk.sha256"

    # Dual-arch release
    dual_release = {
        "tag_name": "v2.12.4",
        "formal_base": "2.12.4",
        "source": "github",
        "assets": [
            {
                "name": "fnmusic-ext-2.12.4-0-amd64.fpk",
                "browser_download_url": "https://example.com/amd64.fpk",
                "size": 100,
            },
            {
                "name": "fnmusic-ext-2.12.4-0-arm64.fpk",
                "browser_download_url": "https://example.com/arm64.fpk",
                "size": 100,
            },
        ],
    }
    res_amd64 = select_release_assets(dual_release, "x86_64")
    assert res_amd64 is not None
    assert res_amd64["asset_name"] == "fnmusic-ext-2.12.4-0-amd64.fpk"

    res_arm64 = select_release_assets(dual_release, "aarch64")
    assert res_arm64 is not None
    assert res_arm64["asset_name"] == "fnmusic-ext-2.12.4-0-arm64.fpk"


def test_select_release_assets_rejects_conflicts():
    # Two different versions in same release
    competing = {
        "tag_name": "v2.12.4",
        "formal_base": "2.12.4",
        "assets": [
            {"name": "fnmusic-ext-2.12.4-0.fpk", "browser_download_url": "https://a.fpk"},
            {"name": "fnmusic-ext-2.12.4-1.fpk", "browser_download_url": "https://b.fpk"},
        ],
    }
    assert select_release_assets(competing, "amd64") is None

    # Mismatched base
    mismatched = {
        "tag_name": "v2.12.4",
        "formal_base": "2.12.4",
        "assets": [
            {"name": "fnmusic-ext-2.12.5-0.fpk", "browser_download_url": "https://a.fpk"},
        ],
    }
    assert select_release_assets(mismatched, "amd64") is None
