#!/usr/bin/env python3
"""Host proxy wheelhouse manifest writer and fail-closed verifier.

Prebuilt FPK packages ship wheels for CPython 3.11, 3.12 and 3.13 on one
architecture. Installation may use them only when the manifest, hashes and
declared files are intact and the running interpreter matches. A missing
manifest is the caller's signal to use the network; this module never treats
corruption as a network fallback.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

SCHEMA = 1
PYTHON_VERSIONS = ("3.11", "3.12", "3.13")


class WheelhouseError(Exception):
    """Manifest, hash, or declared wheel is unusable."""


def normalize_arch(arch: str) -> str:
    raw = str(arch or "").strip().lower()
    if raw in {"x86_64", "amd64", "x64"}:
        return "amd64"
    if raw in {"aarch64", "arm64", "armv8", "armv8l"}:
        return "arm64"
    return raw


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wheel_python(filename: str) -> str:
    parts = filename[:-4].split("-") if filename.endswith(".whl") else filename.split("-")
    if len(parts) < 3:
        return "unknown"
    tag = parts[-3]
    if tag.startswith("cp") and len(tag) >= 5 and tag[2:].isdigit():
        return f"{int(tag[2])}.{int(tag[3:])}"
    if tag.startswith("py"):
        return "any"
    return tag


def _wheel_arch(filename: str) -> str:
    parts = filename[:-4].split("-") if filename.endswith(".whl") else filename.split("-")
    if len(parts) < 1:
        return "unknown"
    platform = parts[-1]
    if platform == "any":
        return "any"
    if "x86_64" in platform or platform.endswith("amd64"):
        return "amd64"
    if "aarch64" in platform or "arm64" in platform:
        return "arm64"
    return platform


def write_manifest(dest: Path, arch: str, requirements: Path) -> dict:
    """Record every wheel currently in ``dest`` and the requirements hash."""
    dest = dest.resolve()
    requirements = requirements.resolve()
    normalized = normalize_arch(arch)
    if normalized not in {"amd64", "arm64"}:
        raise WheelhouseError(f"wheelhouse 架构必须是 amd64 或 arm64，当前为 {arch!r}")
    if not requirements.is_file():
        raise WheelhouseError(f"缺少 requirements: {requirements}")
    wheels = []
    for path in sorted(dest.glob("*.whl")):
        if not path.is_file():
            continue
        wheels.append({
            "filename": path.name,
            "sha256": _sha256(path),
            "python": _wheel_python(path.name),
            "arch": _wheel_arch(path.name),
        })
    if not wheels:
        raise WheelhouseError(f"未下载到任何 wheel: {dest}")
    manifest = {
        "schema": SCHEMA,
        "arch": normalized,
        "python_versions": list(PYTHON_VERSIONS),
        "requirements_sha256": _sha256(requirements),
        "wheels": wheels,
    }
    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def _load_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WheelhouseError(f"wheel 清单不可读或不是 JSON: {path}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise WheelhouseError("wheel 清单 schema 无效")
    arch = data.get("arch")
    versions = data.get("python_versions")
    wheels = data.get("wheels")
    requirements_sha = str(data.get("requirements_sha256") or "")
    if normalize_arch(str(arch or "")) != arch or arch not in {"amd64", "arm64"}:
        raise WheelhouseError("wheel 清单架构无效")
    if (not isinstance(versions, list) or not versions
            or any(not isinstance(item, str) or not item for item in versions)):
        raise WheelhouseError("wheel 清单缺少 Python 版本")
    if not isinstance(wheels, list) or not wheels:
        raise WheelhouseError("wheel 清单没有声明任何 wheel")
    if len(requirements_sha) != 64 or any(ch not in "0123456789abcdef" for ch in requirements_sha):
        raise WheelhouseError("wheel 清单缺少 requirements 哈希")
    seen: set[str] = set()
    for item in wheels:
        if not isinstance(item, dict):
            raise WheelhouseError("wheel 条目不是对象")
        name = str(item.get("filename") or "")
        digest = str(item.get("sha256") or "")
        if not name.endswith(".whl") or "/" in name or "\\" in name or name in {".", ".."}:
            raise WheelhouseError(f"wheel 文件名非法: {name!r}")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise WheelhouseError(f"wheel 哈希非法: {name}")
        if name in seen:
            raise WheelhouseError(f"wheel 重复声明: {name}")
        seen.add(name)
        wheel_path = path.parent / name
        if not wheel_path.is_file():
            raise WheelhouseError(f"声明的 wheel 缺失: {name}")
        actual = _sha256(wheel_path)
        if actual != digest:
            raise WheelhouseError(f"wheel 哈希不符: {name}")
    extras = sorted(item.name for item in path.parent.glob("*.whl") if item.name not in seen)
    if extras:
        raise WheelhouseError(f"存在未列入清单的 wheel: {', '.join(extras)}")
    return data


def check_wheelhouse(manifest_path: Path, *, requirements: Path | None = None,
                     python_version: str = "", arch: str = "") -> int:
    """Return 0 when usable, 2 when intact but not for this target, 1 when corrupt."""
    try:
        manifest = _load_manifest(manifest_path)
        if requirements is not None:
            if not requirements.is_file():
                raise WheelhouseError(f"缺少 requirements: {requirements}")
            if _sha256(requirements) != manifest["requirements_sha256"]:
                raise WheelhouseError("requirements 与 wheel 清单不一致")
    except WheelhouseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if not python_version and not arch:
        return 0
    target_arch = normalize_arch(arch)
    version = str(python_version or "").strip()
    versions = manifest["python_versions"]
    if target_arch != manifest["arch"] or version not in versions:
        print(
            f"wheelhouse 不匹配: 清单 {manifest['arch']} {','.join(versions)}，"
            f"当前 {target_arch or 'unknown'} {version or 'unknown'}",
            file=sys.stderr,
        )
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验或生成宿主 proxy wheelhouse")
    sub = parser.add_subparsers(dest="command", required=True)
    write = sub.add_parser("write")
    write.add_argument("--dest", type=Path, required=True)
    write.add_argument("--arch", required=True)
    write.add_argument("--requirements", type=Path, required=True)
    check = sub.add_parser("check")
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--requirements", type=Path)
    check.add_argument("--python-version", default="")
    check.add_argument("--arch", default="")
    args = parser.parse_args(argv)
    try:
        if args.command == "write":
            write_manifest(args.dest, args.arch, args.requirements)
            return 0
        return check_wheelhouse(
            args.manifest,
            requirements=args.requirements,
            python_version=args.python_version,
            arch=args.arch,
        )
    except WheelhouseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
