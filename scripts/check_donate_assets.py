#!/usr/bin/env python3
"""验证配置了收款码的渠道确实随 FPK 打包；不读取或输出二维码文本。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import struct
import tarfile

CHANNELS = {"WECHAT_QRCODE": "wechat.png", "ALIPAY_QRCODE": "alipay.png"}
STATIC_PREFIX = "repo/webui-service/static/"


def verify(archive: str | Path, expected: list[str]) -> None:
    if not expected:
        return
    missing = {STATIC_PREFIX + name for name in expected}
    with tarfile.open(archive, "r:gz") as outer:
        stream = outer.extractfile("app.tgz")
        if stream is None:
            raise ValueError("FPK 缺少 app.tgz")
        with stream, tarfile.open(fileobj=stream, mode="r|gz") as inner:
            for member in inner:
                if member.name not in missing:
                    continue
                if not member.isfile():
                    raise ValueError(f"二维码不是普通文件：{member.name}")
                png = inner.extractfile(member)
                if png is None:
                    raise ValueError(f"二维码不可读：{member.name}")
                with png:
                    header = png.read(24)
                if (len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n"
                        or header[12:16] != b"IHDR"
                        or min(struct.unpack(">II", header[16:24])) <= 0):
                    raise ValueError(f"二维码不是有效 PNG：{member.name}")
                missing.remove(member.name)
    if missing:
        raise ValueError("FPK 缺少二维码：" + ", ".join(sorted(missing)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    expected = [name for key, name in CHANNELS.items() if os.environ.get(key, "").strip()]
    try:
        if not args.archive.is_file():
            raise FileNotFoundError(f"FPK 归档不存在：{args.archive}")
        verify(args.archive, expected)
    except (OSError, ValueError, tarfile.TarError, KeyError):
        parser.exit(1, "ERROR: FPK 收款码资源校验失败\n")
    print(f"FPK 收款码资源校验通过（{len(expected)} 个已配置渠道）")


if __name__ == "__main__":
    main()
