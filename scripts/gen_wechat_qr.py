#!/usr/bin/env python3
"""生成带 FPK 图标的微信打赏二维码（静态图片不入库）。

CI 从 WECHAT_QRCODE secret 读取二维码原始文本（包括 wxp:// 等协议），
本地也可使用：python scripts/gen_wechat_qr.py <二维码文本> [输出路径]。
只记录输出文件信息，不打印收款文本；无需中文字体。
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "webui-service" / "static" / "wechat.png"
DEFAULT_ICON = REPO_ROOT / "packaging" / "fpk" / "ICON_256.PNG"
# 按不含静区的码区计算；图标及白色衬底合计不超过码区边长的 22%。
BADGE_RATIO = 0.18
BADGE_OUTER_RATIO = 0.22


def generate(text: str, out_path: str | Path = DEFAULT_OUT,
             icon_path: str | Path = DEFAULT_ICON) -> Path:
    import qrcode
    from PIL import Image, ImageDraw, ImageOps

    text = text.strip()
    if not text:
        raise ValueError("二维码文本不能为空")
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, border=4)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    code_width = qr.modules_count * qr.box_size
    outer = max(1, int(code_width * BADGE_OUTER_RATIO))
    side = max(1, int(code_width * BADGE_RATIO))
    with Image.open(icon_path) as source:
        icon = ImageOps.contain(source.convert("RGBA"), (side, side), Image.Resampling.LANCZOS)
    left = (img.width - outer) // 2
    top = (img.height - outer) // 2
    ImageDraw.Draw(img).rectangle((left, top, left + outer - 1, top + outer - 1), fill="white")
    pos = ((img.width - icon.width) // 2, (img.height - icon.height) // 2)
    img.paste(icon, pos, icon)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, format="PNG")
    return out


def main(argv: list[str]) -> int:
    text = (argv[1] if len(argv) > 1 else os.environ.get("WECHAT_QRCODE", "")).strip()
    if not text:
        print("ERROR: 缺少二维码文本（argv[1] 或环境变量 WECHAT_QRCODE）", file=sys.stderr)
        return 2
    try:
        out = generate(text, argv[2] if len(argv) > 2 else DEFAULT_OUT)
    except Exception:
        # 不回显异常详情，避免依赖库的错误消息泄漏收款文本。
        print("ERROR: 微信二维码生成失败，请检查依赖、图标和输入长度", file=sys.stderr)
        return 1
    print(f"wechat.png generated: {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
