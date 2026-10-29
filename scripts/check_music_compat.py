#!/usr/bin/env python3
"""Validate the release's recorded official music compatibility baseline."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re


def validate(root: Path) -> dict:
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    data = json.loads((root / "FNMUSIC_COMPAT.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("extension_version") != version:
        raise ValueError("FNMUSIC_COMPAT.json 的扩展版本必须与 VERSION 一致")
    versions = data.get("music_versions")
    if (not isinstance(versions, list) or not versions
            or any(not isinstance(item, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:\.\d+)?", item)
                   for item in versions)
            or len(versions) != len(set(versions))):
        raise ValueError("music_versions 必须记录非空且不重复的飞牛音乐版本")
    checked_at = data.get("checked_at")
    if (not isinstance(checked_at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", checked_at)
            or date.fromisoformat(checked_at) > date.today()):
        raise ValueError("checked_at 必须是实际核对日期，不能填写未来日期")
    if not isinstance(data.get("notes"), str) or not data["notes"].strip():
        raise ValueError("notes 必须说明适配核对依据")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        data = validate(args.root)
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(1, f"飞牛音乐适配记录校验失败: {exc}\n")
    print(f"飞牛音乐适配记录通过: {data['extension_version']} -> {', '.join(data['music_versions'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
