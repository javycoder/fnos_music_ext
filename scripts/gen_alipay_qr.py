#!/usr/bin/env python3
"""生成打赏支付宝二维码（webui-service/static/alipay.png）。

CI（release.yml / ci.yml）在配置了 ALIPAY_QRCODE secret（支付宝收款链接）时调用；
本地可显式传参：python3 scripts/gen_alipay_qr.py <收款链接> [输出路径]。

用户反馈裸二维码无法辨认收款渠道，故：
1. 二维码中心叠加支付宝徽标（蓝底白「支」圆角方块）。徽标占用中心模块，
   必须用 ERROR_CORRECT_H（30% 冗余）保证可扫；徽标宽高取二维码的 ~22%，
   低于容错上限。
2. WebUI「关于」页在二维码下方另附「支付宝」文字徽标（不依赖本图，见
   webui-service/static/index.html 打赏卡片）。

沙箱/CI 环境可能没有中文字体：候选路径找不到、fc-list 也无 zh 字体时降级为
不带徽标的纯二维码（仍 ERROR_CORRECT_H），文字说明由 WebUI 承担，不视为失败。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "webui-service" / "static" / "alipay.png"

# 支付宝品牌蓝（App 图标底色）
ALIPAY_BLUE = (22, 119, 255, 255)
BADGE_RATIO = 0.22  # 徽标边长 / 二维码总宽（含 quiet zone），ECC H 安全线内

# 常见 CJK 字体候选（Debian/Ubuntu：fonts-noto-cjk / fonts-wqy-*）
FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
)


def _find_cjk_font() -> str:
    for path in FONT_CANDIDATES:
        if os.path.isfile(path):
            return path
    try:
        out = subprocess.run(
            ["fc-list", ":lang=zh", "file"], capture_output=True, text=True, timeout=10
        ).stdout
        for line in out.splitlines():
            path = line.strip().rstrip(":")
            if path and os.path.isfile(path) and path.lower().endswith((".ttc", ".ttf", ".otf")):
                return path
    except Exception:
        pass
    return ""


def generate(link: str, out_path: str | Path = DEFAULT_OUT) -> Path:
    import qrcode
    from PIL import Image, ImageDraw, ImageFont

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, border=4)
    qr.add_data(link)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    font_path = _find_cjk_font()
    if font_path:
        total = img.width
        outer = max(24, int(total * BADGE_RATIO))
        center = (total - outer) // 2
        # 白色衬底隔开二维码模块，再画蓝色圆角方块，视觉上等价于桌面端 App 图标
        pad = max(4, outer // 12)
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            (center - pad, center - pad, center + outer + pad, center + outer + pad),
            radius=pad, fill="white",
        )
        draw.rounded_rectangle(
            (center, center, center + outer, center + outer),
            radius=outer // 5, fill=ALIPAY_BLUE,
        )
        # 「支」字号随徽标缩放；wqy 像素字体按方块 0.72 倍取值居中
        font = ImageFont.truetype(font_path, int(outer * 0.72))
        text = "支"
        box = draw.textbbox((0, 0), text, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
        draw.text(
            (center + (outer - tw) / 2 - box[0], center + (outer - th) / 2 - box[1]),
            text, font=font, fill="white",
        )

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def main(argv: list[str]) -> int:
    link = argv[1] if len(argv) > 1 else os.environ.get("ALIPAY_QRCODE", "").strip()
    if not link:
        print("ERROR: 缺少收款链接（argv[1] 或环境变量 ALIPAY_QRCODE）", file=sys.stderr)
        return 2
    out = generate(link, argv[2] if len(argv) > 2 else DEFAULT_OUT)
    print(f"alipay.png generated: {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
