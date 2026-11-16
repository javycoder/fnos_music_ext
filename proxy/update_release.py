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
_VERSION_PATTERN = rf"{_BASE_PATTERN}(?:-{_REVISION_PATTERN})?"
_GENERIC_FPK_RE = re.compile(rf"^fnmusic-ext-({_VERSION_PATTERN})\.fpk$")
_LEGACY_FPK_RE = re.compile(rf"^fnmusic-ext-({_VERSION_PATTERN})-({_ARCH_PATTERN})\.fpk$")
_PREBUILT_FPK_RE = re.compile(
    rf"^fnmusic-ext-({_VERSION_PATTERN})-prebuilt-({_ARCH_PATTERN})\.fpk$"
)


def formal_version(tag: str) -> str | None:
    """Extract 3-part base from formal tag (e.g. 'v2.12.4' -> '2.12.4').

    Returns None for prerelease tags with letters, drafts, or malformed tags.
    """
    match = _FORMAL_TAG_RE.fullmatch(str(tag or "").strip())
    return match.group(1) if match else None


def parse_install_package(name: str) -> tuple[str, str | None, str] | None:
    """Parse an FPK name into (install_version, arch, kind).

    ``kind`` is ``generic``, ``legacy`` (``-amd64``/``-arm64``), or
    ``prebuilt``. Malformed names return None. Current updaters recognize
    prebuilt names but still prefer a generic package when one is present.
    """
    text = str(name or "").strip()
    prebuilt = _PREBUILT_FPK_RE.fullmatch(text)
    if prebuilt:
        return prebuilt.group(1), prebuilt.group(2), "prebuilt"
    legacy = _LEGACY_FPK_RE.fullmatch(text)
    if legacy:
        return legacy.group(1), legacy.group(2), "legacy"
    generic = _GENERIC_FPK_RE.fullmatch(text)
    if generic:
        return generic.group(1), None, "generic"
    return None


def parse_install_asset(name: str) -> tuple[str, str | None] | None:
    """Parse an FPK asset name into (install_version, arch).

    Returns None if the asset name is not a valid fnmusic-ext FPK.
    Prebuilt packages report their CPU arch here; callers that need to
    distinguish them use ``parse_install_package``.
    """
    parsed = parse_install_package(name)
    if parsed is None:
        return None
    version, arch, _kind = parsed
    return version, arch


def package_kind(asset_name: str) -> str | None:
    parsed = parse_install_package(asset_name)
    return parsed[2] if parsed else None


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


def classify_fpk_assets(assets: list[Any]) -> dict[str, Any] | None:
    """Classify one release's FPK set, or return None when it is unsafe.

    Accepted sets:
    - one generic package (historical universal installer);
    - legacy ``-amd64`` and ``-arm64`` together, with no generic package;
    - generic plus both ``-prebuilt-amd64`` and ``-prebuilt-arm64``.

    Mixing generic with legacy names, a partial prebuilt pair, duplicate
    identities, or more than one install version all fail closed.
    """
    grouped: dict[str, dict[str, Any]] = {}
    saw_fpk = False
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "").strip()
        if not (name.startswith("fnmusic-ext-") and name.endswith(".fpk")):
            continue
        saw_fpk = True
        parsed = parse_install_package(name)
        if parsed is None:
            return None
        version, arch, kind = parsed
        bucket = grouped.setdefault(version, {"generic": None, "legacy": {}, "prebuilt": {}})
        if kind == "generic":
            if bucket["generic"] is not None:
                return None
            bucket["generic"] = asset
            continue
        slot = bucket[kind]
        if arch in slot:
            return None
        slot[arch] = asset
    if not saw_fpk or len(grouped) != 1:
        return None
    version, bucket = next(iter(grouped.items()))
    generic = bucket["generic"]
    legacy = bucket["legacy"]
    prebuilt = bucket["prebuilt"]
    if legacy and (generic is not None or prebuilt):
        return None
    if prebuilt:
        if generic is None or set(prebuilt) != {"amd64", "arm64"}:
            return None
    elif legacy:
        if set(legacy) != {"amd64", "arm64"}:
            return None
    elif generic is None:
        return None
    return {"version": version, "generic": generic, "legacy": legacy, "prebuilt": prebuilt}


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

    A generic package wins whenever it is present, including the three-package
    release that also carries prebuilt archives. Legacy dual-arch releases keep
    their architecture mapping. Incomplete or mixed sets return None.
    """
    target_arch = normalize_arch(arch)
    base = release_dict.get("formal_base") or formal_version(str(release_dict.get("tag_name") or ""))
    if not base:
        return None

    assets = release_dict.get("assets")
    if not isinstance(assets, list):
        return None

    sha256_files: dict[str, str] = {}  # asset_name -> url
    fpk_assets: list[dict[str, Any]] = []

    for a in assets:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "").strip()
        if name.endswith(".sha256"):
            base_name = name[:-7]
            sha256_files[base_name] = str(a.get("browser_download_url") or "")
            continue
        if name.startswith("fnmusic-ext-") and name.endswith(".fpk"):
            fpk_assets.append(a)
    classified = classify_fpk_assets(fpk_assets)
    if classified is None:
        return None
    version = str(classified["version"])
    if package_base(version) != base:
        return None
    if classified["generic"] is not None and not classified["legacy"]:
        chosen_asset = classified["generic"]
    else:
        chosen_asset = classified["legacy"].get(target_arch)
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
