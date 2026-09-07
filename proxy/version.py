"""fnmusic-ext 版本号管理.

版本号规则（语义化）：
- Bug 修复: +0.0.1
- 功能新增: +0.1.0
- 大版本重构: +1.0.0

版本号唯一来源为仓库根目录的 VERSION 文件（基准 1.0.0）。
运行期可通过环境变量 FNMUSIC_VERSION 覆盖（install.sh 会将该值写入 .env）。
"""
from __future__ import annotations

import os
from pathlib import Path

FALLBACK_VERSION = "0.0.0"


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
