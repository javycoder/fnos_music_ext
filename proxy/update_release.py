"""Release asset parsing and verification helpers for fnmusic-ext updates.

Strictly handles formal releases, package versions, and architecture matching.
Pure stdlib implementation with no runtime dependency on developer tools.
"""
from __future__ import annotations

import platform
import re
from typing import Any

_BASE_PATTERN = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
_REVISION_PATTERN = r"(?:0|[1-9][0-9]*)"
_INSTALL_VERSION_RE = re.compile(rf"^{_BASE_PATTERN}(?:-{_REVISION_PATTERN})?$")
_FORMAL_TAG_RE = re.compile(rf"^v?({_BASE_PATTERN})$")
_ARCH_PATTERN = r"(?:amd64|arm64)"
_FPK_NAME_RE = re.compile(
    rf"^fnmusic-ext-({_BASE_PATTERN}(?:-{_REVISION_PATTERN})?)(?:-({_ARCH_PATTERN}))?\.fpk$"
)


def formal_version(tag: str) -> str | None:
    """Extract 3-part base from formal tag (e.g. 'v2.12.4' -> '2.12.4').

    Returns None for prerelease tags with letters, drafts, or malformed tags.
    """
    match = _FORMAL_TAG_RE.fullmatch(str(tag or "").strip())
    return match.group(1) if match else None


def parse_install_asset(name: str) -> tuple[str, str | None] | None:
    """Parse an FPK asset name into (install_version, arch).

    Returns None if the asset name is not a valid fnmusic-ext FPK.
    """
    match = _FPK_NAME_RE.fullmatch(str(name or "").strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def package_version(asset_name: str) -> str | None:
    parsed = parse_install_asset(asset_name)
    return parsed[0] if parsed else None


def package_arch(asset_name: str) -> str | None:
    parsed = parse_install_asset(asset_name)
    return parsed[1] if parsed else None


def package_base(version: str) -> str:
    v = str(version or "").strip()
    if not _INSTALL_VERSION_RE.fullmatch(v):
        raise ValueError(f"安装版本必须为 x.y.z 或 x.y.z-N，当前为 {version!r}")
    return v.partition("-")[0]


def normalize_arch(arch: str | None = None) -> str:
    """Normalize system machine arch to 'amd64' or 'arm64'."""
    raw = (arch or platform.machine() or "").strip().lower()
    if raw in ("x86_64", "amd64", "x64"):
        return "amd64"
    if raw in ("aarch64", "arm64", "armv8", "armv8l"):
        return "arm64"
    return raw


def normalize_release(data: dict[str, Any], source: str) -> dict[str, Any] | None:
    """Validate that release data represents a formal release and normalize assets.

    Returns None if draft, prerelease, missing tag, or non-formal tag.
    """
    if not isinstance(data, dict):
        return None
    if data.get("draft") is True or data.get("prerelease") is True:
        return None

    tag_raw = str(data.get("tag_name") or "").strip()
    base = formal_version(tag_raw)
    if not base:
        return None

    raw_assets = data.get("assets")
    assets: list[dict[str, Any]] = []
    if isinstance(raw_assets, list):
        for a in raw_assets:
            if not isinstance(a, dict):
                continue
            name = str(a.get("name") or "").strip()
            url = str(a.get("browser_download_url") or "").strip()
            if not name or not url:
                continue
            size = a.get("size")
            digest = str(a.get("digest") or "").strip()
            sha256 = ""
            if digest.lower().startswith("sha256:"):
                sha256 = digest.split(":", 1)[1].strip().lower()
            assets.append({
                "name": name,
                "browser_download_url": url,
                "size": int(size) if isinstance(size, (int, float)) else None,
                "sha256": sha256,
            })

    return {
        "source": source,
        "tag_name": f"v{base}",
        "formal_base": base,
        "name": str(data.get("name") or "").strip(),
        "published_at": str(data.get("published_at") or data.get("created_at") or "").strip(),
        "body": str(data.get("body") or ""),
        "html_url": str(data.get("html_url") or ""),
        "assets": assets,
    }


def select_release_assets(release_dict: dict[str, Any], arch: str | None = None) -> dict[str, Any] | None:
    """Select the appropriate FPK and sha256 asset for target architecture.

    Refuses ambiguous releases (conflicting versions, mismatched bases).
    """
    target_arch = normalize_arch(arch)
    base = release_dict.get("formal_base") or formal_version(str(release_dict.get("tag_name") or ""))
    if not base:
        return None

    assets = release_dict.get("assets")
    if not isinstance(assets, list):
        return None

    fpk_candidates: dict[str, dict[str, dict[str, Any]]] = {}
    sha256_files: dict[str, str] = {}  # asset_name -> url

    for a in assets:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "").strip()
        if name.endswith(".sha256"):
            base_name = name[:-7]
            sha256_files[base_name] = str(a.get("browser_download_url") or "")
            continue
        parsed = parse_install_asset(name)
        if not parsed:
            continue
        version, pkg_arch = parsed
        if package_base(version) != base:
            return None  # Mismatched base, reject release
        arch_key = pkg_arch or "all"
        fpk_candidates.setdefault(version, {})[arch_key] = a

    if len(fpk_candidates) != 1:
        return None  # Missing FPK or multiple competing versions in same release

    version, arch_map = next(iter(fpk_candidates.items()))
    chosen_asset: dict[str, Any] | None = None
    if target_arch in arch_map:
        chosen_asset = arch_map[target_arch]
    elif "all" in arch_map:
        chosen_asset = arch_map["all"]

    if not chosen_asset:
        return None

    asset_name = str(chosen_asset["name"])
    sha256 = str(chosen_asset.get("sha256") or "").strip().lower()
    if not sha256 and chosen_asset.get("digest"):
        d = str(chosen_asset["digest"]).strip()
        if d.lower().startswith("sha256:"):
            sha256 = d.split(":", 1)[1].strip().lower()

    return {
        "tag": f"v{base}",
        "formal_base": base,
        "install_version": version,
        "target_arch": target_arch,
        "asset_name": asset_name,
        "download_url": str(chosen_asset["browser_download_url"]),
        "size": chosen_asset.get("size"),
        "sha256": sha256,
        "sha256_url": sha256_files.get(asset_name, ""),
        "source": release_dict.get("source", ""),
    }
