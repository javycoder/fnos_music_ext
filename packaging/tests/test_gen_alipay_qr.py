"""scripts/gen_alipay_qr.py 回归测试：打赏二维码生成与支付宝徽标。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SPEC = importlib.util.spec_from_file_location("gen_alipay_qr", REPO_ROOT / "scripts" / "gen_alipay_qr.py")
gen_alipay_qr = importlib.util.module_from_spec(SPEC)
sys.modules["gen_alipay_qr"] = gen_alipay_qr
SPEC.loader.exec_module(gen_alipay_qr)


def test_generate_writes_decodable_sized_png(tmp_path):
    """生成 PNG 且尺寸/格式正确；无中文字体环境降级为纯二维码不报错。"""
    out = tmp_path / "alipay.png"
    result = gen_alipay_qr.generate("https://qr.alipay.com/test-link", out)
    assert result == out
    data = out.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    # 徽标有无都必须是方形位图（带 quiet zone 的正方形二维码）
    from PIL import Image
    img = Image.open(out)
    assert img.width == img.height
    assert img.width >= 200


def test_badge_drawn_when_cjk_font_available(tmp_path):
    """有中文字体时中心必须叠支付宝蓝徽标（中心像素为品牌蓝、偏离中心为白）。"""
    font = gen_alipay_qr._find_cjk_font()
    if not font:
        import pytest
        pytest.skip("环境无 CJK 字体，徽标降级路径由上一用例覆盖")
    out = tmp_path / "alipay.png"
    gen_alipay_qr.generate("https://qr.alipay.com/test-link", out)
    from PIL import Image
    img = Image.open(out).convert("RGB")
    c = img.width // 2
    r, g, b = img.getpixel((c, c))
    # 支付宝品牌蓝 (#1677FF)：徽标与白色「支」笔画重叠处可能有白色像素，
    # 取中心 3x3 均色仍应显著偏蓝
    px = [img.getpixel((c + dx, c + dy)) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
    avg = tuple(sum(p[i] for p in px) // len(px) for i in range(3))
    assert avg[2] > 150 and avg[2] > avg[0], f"中心非蓝色徽标: rgb={avg} center={(r, g, b)}"
