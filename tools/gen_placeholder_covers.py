#!/usr/bin/env python3
"""生成封面兜底占位图池 proxy/static/covers/placeholder-N.png。

纯标准库实现（zlib + struct 手写 PNG 编码，不依赖 Pillow），生成确定性
几何图案：不同色相的对角渐变 + 同心圆韵律。仅在新增/调整占位图时运行：

    python3 tools/gen_placeholder_covers.py
"""
from __future__ import annotations

import os
import struct
import sys
import zlib

SIZE = 300
COUNT = 6
# 柔和的深色系色相（避免纯黑/纯白刺眼，暗色调在深浅两种 UI 下都不突兀）
PALETTES = [
    ((64, 81, 181), (32, 38, 106)),    # 靛蓝
    ((0, 121, 107), (0, 64, 58)),      # 青绿
    ((102, 63, 132), (55, 32, 74)),    # 紫罗兰
    ((158, 100, 53), (92, 55, 25)),    # 琥珀
    ((139, 68, 78), (82, 38, 46)),     # 绛红
    ((63, 93, 113), (33, 51, 64)),     # 石板蓝
]


def _clamp(v: float) -> int:
    return max(0, min(255, int(round(v))))


def _lerp(a: int, b: int, t: float) -> float:
    return a + (b - a) * t


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def render(index: int) -> bytes:
    top, bottom = PALETTES[index % len(PALETTES)]
    cx, cy = SIZE / 2.0, SIZE / 2.0
    rows = []
    for y in range(SIZE):
        row = bytearray([0])  # PNG filter type 0
        for x in range(SIZE):
            # 对角渐变
            t = (x + y) / (2.0 * (SIZE - 1))
            r = _lerp(top[0], bottom[0], t)
            g = _lerp(top[1], bottom[1], t)
            b = _lerp(top[2], bottom[2], t)
            # 同心圆韵律：每 40px 一圈亮度波动（锯齿三角波）
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            phase = (d % 40.0) / 40.0
            lift = 18.0 * (phase if phase < 0.5 else 1.0 - phase) * 2.0
            row += bytes((_clamp(r + lift), _clamp(g + lift), _clamp(b + lift)))
        rows.append(bytes(row))
    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + _chunk(b"IEND", b"")
    )


def main() -> int:
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy", "static", "covers")
    os.makedirs(out_dir, exist_ok=True)
    for i in range(COUNT):
        path = os.path.join(out_dir, f"placeholder-{i}.png")
        with open(path, "wb") as f:
            f.write(render(i))
        print(f"wrote {path} ({os.path.getsize(path)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
