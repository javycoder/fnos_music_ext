"""Offline application-image bundle: verify, reuse, load (3.0.0+).

The FPK ships a per-architecture ``docker save`` archive plus a manifest that
records the exact image identity built from the shipped sources by CI. The
installer prefers this bundle so a fresh NAS never runs the in-container
apt/pip BuildKit layers (measured 219.9s of a 261s install on 2026-11-03),
while plain git checkouts without ``assets/image`` keep the legacy
build-on-NAS path (``ensure_image_current``).

Exit codes: 0 = bundle ready/loaded; 2 = bundle unusable (missing, corrupt,
architecture mismatch) so the shell caller may fall back to a local build
unless the operator forced bundle mode.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

SCHEMA = 1
DEFAULT_TAG = "fnmusic-sources:latest"
ARCH_MAP = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
# Fields every manifest must carry; extra fields are allowed for provenance.
REQUIRED_FIELDS = (
    "schema", "app", "arch", "image_tag", "image_id",
    "rootfs_diff_ids", "archive", "archive_sha256", "archive_bytes",
)


class BundleError(Exception):
    """Bundle is unusable; the caller decides whether to fall back."""


def info(msg: str) -> None:
    print(f"[image-bundle] {msg}")


def host_arch() -> str:
    machine = os.uname().machine if hasattr(os, "uname") else "amd64"
    try:
        return ARCH_MAP[machine]
    except KeyError as exc:
        raise BundleError(f"无法识别本机架构 {machine!r}（支持: amd64/arm64）") from exc


def load_manifest(bundle_dir: Path) -> dict:
    manifest_path = bundle_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BundleError(f"缺少镜像清单 {manifest_path}") from exc
    except (OSError, ValueError) as exc:
        raise BundleError(f"镜像清单不可读: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise BundleError(f"镜像清单 schema 不受支持（期望 {SCHEMA}）")
    missing = [key for key in REQUIRED_FIELDS if not manifest.get(key)]
    if missing:
        raise BundleError(f"镜像清单缺少字段: {', '.join(missing)}")
    if manifest.get("app") != "fnmusic-ext":
        raise BundleError(f"清单应用标识不符: {manifest.get('app')!r}")
    if not isinstance(manifest["rootfs_diff_ids"], list) or not manifest["rootfs_diff_ids"]:
        raise BundleError("rootfs_diff_ids 必须为非空列表")
    return manifest


def verify_archive(bundle_dir: Path, manifest: dict) -> Path:
    archive = bundle_dir / str(manifest["archive"])
    if not archive.is_file():
        raise BundleError(f"缺少镜像归档 {archive}")
    actual_bytes = archive.stat().st_size
    if actual_bytes != int(manifest["archive_bytes"]):
        raise BundleError(
            f"镜像归档大小不符（{actual_bytes} != {manifest['archive_bytes']}）")
    digest = hashlib.sha256()
    with archive.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != manifest["archive_sha256"]:
        raise BundleError(f"镜像归档 sha256 不符（{actual} != {manifest['archive_sha256']}）")
    return archive


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    if check and result.returncode != 0:
        detail = (result.stdout or "").strip().splitlines()
        raise BundleError(
            f"命令失败 ({shlex.join(cmd)}): {detail[-1] if detail else result.returncode}")
    return result


def inspect_image(docker: list[str], ref: str) -> dict | None:
    """Return {id, diff_ids, os, arch} for an image ref, or None if absent."""
    result = run(docker + ["image", "inspect", ref, "--format", "{{json .}}"], check=False)
    if result.returncode != 0:
        return None
    try:
        raw = json.loads(result.stdout)
        return {
            "id": raw.get("Id"),
            "diff_ids": (raw.get("RootFS") or {}).get("DiffIds") or [],
            "os": raw.get("Os"),
            "arch": raw.get("Architecture"),
        }
    except ValueError:
        return None


def image_matches(local: dict | None, manifest: dict) -> bool:
    if not local or not local.get("id"):
        return False
    if local["id"] != manifest["image_id"]:
        return False
    # image_id 是 config 摘要，相等即蕴含 rootfs 描述一致；diff_ids 仅在
    # inspect 提供（经典 docker 存储）时作加强校验——containerd image store
    # 的 inspect 不暴露 RootFS.DiffIds，不能据此判不符。
    local_diffs = list(local.get("diff_ids") or [])
    if local_diffs and local_diffs != list(manifest["rootfs_diff_ids"]):
        return False
    return local.get("os") == "linux" and local.get("arch") == manifest["arch"]


def load_archive(docker: list[str], archive: Path) -> None:
    """gzip -dc archive | docker load (explicit two-process pipe, no shell)."""
    if not archive.name.endswith(".tar.gz"):
        raise BundleError(f"仅支持 .tar.gz 归档，当前: {archive.name}")
    decompressor = subprocess.Popen(["gzip", "-dc", str(archive)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert decompressor.stdout is not None
    loader = subprocess.run(docker + ["load"], stdin=decompressor.stdout,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    decompressor.stdout.close()
    decompressor.wait()
    if decompressor.returncode != 0 or loader.returncode != 0:
        detail = (loader.stdout or "") + (decompressor.stderr.read() if decompressor.stderr else "")
        raise BundleError(f"docker load 失败: {detail.strip().splitlines()[-1:] or '未知错误'}")


def check(bundle_dir: Path, *, expect_arch: str | None = None) -> tuple[dict, Path]:
    """Validate manifest + host architecture + archive hash (no docker needed)."""
    manifest = load_manifest(bundle_dir)
    target_arch = expect_arch or manifest["arch"]
    if manifest["arch"] not in ARCH_MAP.values():
        raise BundleError(f"清单架构不受支持: {manifest['arch']!r}")
    if expect_arch and manifest["arch"] != expect_arch:
        raise BundleError(f"镜像架构 {manifest['arch']} 与目标 {expect_arch} 不符")
    archive = verify_archive(bundle_dir, manifest)
    return manifest, archive


def ensure(bundle_dir: Path, *, docker_cmd: str, tag: str | None = None,
           allow_mismatch: bool = False) -> dict:
    """Make the bundled image available locally under ``tag``; idempotent."""
    manifest, archive = check(bundle_dir)
    want_tag = tag or manifest.get("image_tag") or DEFAULT_TAG
    this_arch = host_arch()
    if manifest["arch"] != this_arch and not allow_mismatch:
        raise BundleError(
            f"镜像架构 {manifest['arch']} 与本机 {this_arch} 不符"
            f"（强制继续请设 FNMUSIC_IMAGE_BUNDLE_ALLOW_MISMATCH=1）")
    docker = shlex.split(docker_cmd)

    local = inspect_image(docker, want_tag)
    if image_matches(local, manifest):
        info(f"应用镜像已就绪：{want_tag} ({manifest['image_id'][:19]})，跳过导入。")
        return manifest

    # Same image under another reference (e.g. a pruned/renamed tag): repair
    # the reference instead of re-importing hundreds of megabytes.
    by_id = inspect_image(docker, manifest["image_id"])
    if image_matches(by_id, manifest):
        info(f"镜像 {manifest['image_id'][:19]} 已在本地，补齐引用 {want_tag}。")
        run(docker + ["tag", manifest["image_id"], want_tag])
        return manifest

    info(f"导入随包应用镜像（{archive.name}，{int(manifest['archive_bytes']) // (1024 * 1024)}MB）...")
    started = time.monotonic()
    load_archive(docker, archive)
    loaded = inspect_image(docker, want_tag)
    if not image_matches(loaded, manifest):
        raise BundleError(
            "导入后的镜像身份与清单不符"
            f"（got {loaded.get('id') if loaded else None}, want {manifest['image_id']}）")
    info(f"镜像导入并校验完成，耗时 {time.monotonic() - started:.1f}s。")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fnmusic-ext 离线应用镜像资产工具")
    parser.add_argument("command", choices=("check", "ensure"))
    parser.add_argument("--bundle-dir", type=Path, required=True,
                        help="资产目录（含 manifest.json 与镜像归档）")
    parser.add_argument("--docker-cmd", default=os.environ.get("FNMUSIC_DOCKER_CMD", "docker"),
                        help="docker 调用前缀（默认 'docker'，可为 'sudo docker'）")
    parser.add_argument("--tag", help="目标镜像引用（默认取清单 image_tag）")
    parser.add_argument("--arch", help="check 时校验清单面向该架构")
    parser.add_argument("--allow-mismatch", action="store_true",
                        help="允许清单架构与本机不同（跨架构调试用）")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            manifest, archive = check(args.bundle_dir, expect_arch=args.arch)
            info(f"清单与归档校验通过: {manifest['arch']} {archive.name}"
                 f" ({manifest['image_id'][:19]})")
        else:
            ensure(args.bundle_dir, docker_cmd=args.docker_cmd, tag=args.tag,
                   allow_mismatch=args.allow_mismatch)
    except BundleError as exc:
        print(f"[image-bundle] 资产不可用: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
