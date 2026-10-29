"""Numeric installation versions and explicit release channels.

VERSION is always the installation/package version (new releases: x.y.z-N).
RELEASE_CHANNEL, not the numeric revision, selects stable versus prerelease.
Stable UI/tag: vx.y.z; prerelease UI/tag: vx.y.z + a..z (N=0..25).
Legacy base/letter versions remain readable for existing installations only.
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
RELEASE_CHANNELS = ("stable", "prerelease")


def release_info(version: str, *, channel: str | None = None,
                 allow_legacy: bool = False) -> dict[str, str]:
    """Keep the package revision while deriving identity from an explicit channel.

    Missing channel is accepted only for explicitly requested legacy reads; new
    publication must never infer its release type from the presence of ``-N``.
    """
    if channel is not None and channel not in RELEASE_CHANNELS:
        raise ValueError(f"发布通道必须为 stable 或 prerelease，当前为 {channel!r}")
    if channel is None and not allow_legacy:
        raise ValueError("必须通过 RELEASE_CHANNEL 显式指定发布通道 stable 或 prerelease")
    version = version.strip()
    match = _PACKAGE_VERSION.fullmatch(version)
    if match:
        revision = match["revision"]
        if channel is None:  # Read-only compatibility with previously shipped packages.
            channel = "stable" if revision is None else "prerelease"
        if not allow_legacy and revision is None:
            raise ValueError("新发行安装版本必须保留数字修订号 x.y.z-N")
        if channel == "prerelease":
            if revision is None or int(revision) > 25:
                raise ValueError("预发布修订号必须为 0–25（对应 a–z）")
            display = match["base"] + chr(ord("a") + int(revision))
        else:
            display = match["base"]
    elif allow_legacy and (match := _LEGACY_VERSION.fullmatch(version)):
        channel = "prerelease"
        display = version
    else:
        raise ValueError(f"安装版本必须为数字 x.y.z-N，当前为 {version!r}")
    return {
        "version": version,
        "display_version": display,  # CHANGELOG heading has no v prefix.
        "ui_version": f"v{display}",
        "tag": f"v{display}",
        "channel": channel,
        "formal": "true" if channel == "stable" else "false",
    }


def display_version(version: str, *, channel: str | None = None) -> str:
    """Unprefixed release identity; legacy installs remain readable."""
    return release_info(version, channel=channel, allow_legacy=True)["display_version"]


def ui_version(version: str, *, channel: str | None = None) -> str:
    """UI and tag share the v-prefixed identity, never the package revision."""
    return release_info(version, channel=channel, allow_legacy=True)["ui_version"]


def read_release_channel(path: Path, *, allow_legacy: bool = False) -> str | None:
    try:
        channel = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        if allow_legacy:
            return None
        raise ValueError(f"缺少发布通道文件: {path}") from None
    if channel not in RELEASE_CHANNELS:
        raise ValueError(f"{path} 必须仅包含 stable 或 prerelease")
    return channel


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
    """Installation version: FNMUSIC_VERSION > repository VERSION > fallback."""
    env_ver = (os.environ.get("FNMUSIC_VERSION") or "").strip()
    if env_ver:
        return env_ver.splitlines()[0].strip()
    candidates = (Path(__file__).resolve().parent.parent / "VERSION", Path.cwd() / "VERSION")
    for cand in candidates:
        ver = _read_version_file(cand)
        if ver:
            return ver
    return FALLBACK_VERSION


__version__ = get_version()


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate numeric package version and explicit release channel")
    parser.add_argument("--version-file", type=Path,
                        default=Path(__file__).resolve().parent.parent / "VERSION")
    parser.add_argument("--channel-file", type=Path,
                        help="Defaults to RELEASE_CHANNEL next to --version-file")
    parser.add_argument("--tag", help="Require this tag to match VERSION and RELEASE_CHANNEL")
    parser.add_argument("--format", choices=("version", "display_version", "ui_version", "tag", "channel", "formal"),
                        default="display_version")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--allow-legacy", action="store_true",
                        help="Read historical releases only; do not use for new publication")
    args = parser.parse_args()
    try:
        channel_file = args.channel_file or args.version_file.with_name("RELEASE_CHANNEL")
        channel = read_release_channel(channel_file, allow_legacy=args.allow_legacy)
        info = release_info(_read_version_file(args.version_file), channel=channel,
                            allow_legacy=args.allow_legacy)
        if args.tag is not None and args.tag != info["tag"]:
            raise ValueError(f"tag {args.tag} 与 VERSION {info['version']} / 通道 {info['channel']} 对应的 {info['tag']} 不一致")
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as output:
                output.writelines(f"{key}={value}\n" for key, value in info.items())
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(info[args.format])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
