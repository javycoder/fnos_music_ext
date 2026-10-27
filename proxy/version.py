"""fnmusic-ext 版本号管理.

版本号规则（语义化）：
- Bug 修复: +0.0.1
- 功能新增: +0.1.0
- 大版本重构: +1.0.0

版本号唯一来源为仓库根目录的 VERSION 文件（基准 1.0.0）。
正式版为 x.y.z；预发布为 x.y.z-N，N 从 0 开始，对应 tag/WebUI 的 a..z。
运行期可通过环境变量 FNMUSIC_VERSION 覆盖（install.sh 会将该值写入 .env）。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re

FALLBACK_VERSION = "0.0.0"
_BASE_PATTERN = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
_PACKAGE_VERSION = re.compile(rf"(?P<base>{_BASE_PATTERN})(?:-(?P<revision>0|[1-9][0-9]*))?")
_LEGACY_VERSION = re.compile(rf"(?P<base>{_BASE_PATTERN})(?P<letter>[a-z])")


def release_info(version: str, *, allow_legacy: bool = False) -> dict[str, str]:
    """Derive package version, display version, tag and release kind from VERSION.

    FPK uses x.y.z / x.y.z-N; N=0..25 maps to the a..z tag/UI suffix.
    Legacy letter versions are accepted only when reading older installations.
    """
    version = version.strip()
    match = _PACKAGE_VERSION.fullmatch(version)
    if match:
        revision = match["revision"]
        formal = revision is None
        if revision is not None and int(revision) > 25:
            raise ValueError("预发布修订号必须为 0–25（对应 a–z）")
        display = match["base"] if formal else match["base"] + chr(ord("a") + int(revision))
    elif allow_legacy and (match := _LEGACY_VERSION.fullmatch(version)):
        formal = False
        display = version
    else:
        raise ValueError(f"版本必须为 x.y.z 或 x.y.z-N，当前为 {version!r}")
    return {
        "version": version,
        "display_version": display,
        "tag": f"v{display}",
        "formal": "true" if formal else "false",
    }


def display_version(version: str) -> str:
    """WebUI version, including support for existing letter-suffixed installs."""
    return release_info(version, allow_legacy=True)["display_version"]


def _read_version_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def get_version() -> str:
    """返回当前 fnmusic-ext 版本号。

    优先级: FNMUSIC_VERSION 环境变量 > 仓库根 VERSION 文件 > FALLBACK_VERSION
    """
    env_ver = (os.environ.get("FNMUSIC_VERSION") or "").strip()
    if env_ver:
        return env_ver.splitlines()[0].strip()
    candidates = (
        Path(__file__).resolve().parent.parent / "VERSION",
        Path.cwd() / "VERSION",
    )
    for cand in candidates:
        ver = _read_version_file(cand)
        if ver:
            return ver
    return FALLBACK_VERSION


__version__ = get_version()


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and derive fnmusic-ext release versions")
    parser.add_argument("--version-file", type=Path,
                        default=Path(__file__).resolve().parent.parent / "VERSION")
    parser.add_argument("--tag", help="Require this Git tag to match VERSION")
    parser.add_argument("--format", choices=("version", "display_version", "tag", "formal"),
                        default="display_version")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--allow-legacy", action="store_true")
    args = parser.parse_args()
    try:
        info = release_info(_read_version_file(args.version_file), allow_legacy=args.allow_legacy)
        if args.tag is not None and args.tag != info["tag"]:
            raise ValueError(f"tag {args.tag} 与 VERSION {info['version']} 对应的 {info['tag']} 不一致")
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as output:
                output.writelines(f"{key}={value}\n" for key, value in info.items())
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(info[args.format])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
