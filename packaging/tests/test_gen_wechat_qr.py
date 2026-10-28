"""微信二维码：原始文本、中心 FPK 图标、可解码性与不泄漏输入。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageOps
import qrcode

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("gen_wechat_qr", ROOT / "scripts/gen_wechat_qr.py")
gen = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gen)


@pytest.mark.parametrize("text", ["wxp://example-payment-for-tests-only", "二维码测试文本", "x" * 400])
def test_png_with_fpk_badge_is_decodable_and_has_quiet_zone(tmp_path, monkeypatch, text):
    decoder = pytest.importorskip("zxingcpp")
    created = []
    original = qrcode.QRCode

    def capture(*args, **kwargs):
        qr = original(*args, **kwargs)
        created.append(qr)
        return qr

    monkeypatch.setattr(qrcode, "QRCode", capture)
    out = tmp_path / "nested/wechat.png"
    assert gen.generate(text, out) == out
    with Image.open(out) as image:
        image.load()
        assert image.format == "PNG" and image.width == image.height >= 200
        qr = created[0]
        assert qr.error_correction == qrcode.constants.ERROR_CORRECT_H
        assert qr.border == 4
        assert b"".join(part.data for part in qr.data_list) == text.encode("utf-8")
        quiet = qr.border * qr.box_size
        for box in ((0, 0, image.width, quiet), (0, 0, quiet, image.height),
                    (0, image.height - quiet, image.width, image.height),
                    (image.width - quiet, 0, image.width, image.height)):
            assert image.crop(box).getextrema() == ((255, 255),) * 3
        result = decoder.read_barcode(image)
        assert result is not None and result.text == text
        code_width = qr.modules_count * qr.box_size
        side = int(code_width * gen.BADGE_RATIO)
        outer = int(code_width * gen.BADGE_OUTER_RATIO)
        assert outer <= code_width * 0.22
        with Image.open(gen.DEFAULT_ICON) as source:
            icon = ImageOps.contain(source.convert("RGBA"), (side, side), Image.Resampling.LANCZOS)
        expected = Image.new("RGB", icon.size, "white")
        expected.paste(icon, (0, 0), icon)
        left = (image.width - icon.width) // 2
        top = (image.height - icon.height) // 2
        actual = image.crop((left, top, left + icon.width, top + icon.height))
        assert ImageChops.difference(actual, expected).getbbox() is None


def test_transparent_non_square_icon_uses_white_background(tmp_path):
    icon = tmp_path / "icon.png"
    Image.new("RGBA", (80, 40), (255, 0, 0, 128)).save(icon)
    out = gen.generate("wxp://synthetic-test-only", tmp_path / "qr.png", icon)
    with Image.open(out) as image:
        center = image.getpixel((image.width // 2, image.height // 2))
        assert center == (255, 127, 127)


@pytest.mark.parametrize("text", ["", "   \n"])
def test_empty_text_rejected_before_writing(tmp_path, text):
    out = tmp_path / "qr.png"
    with pytest.raises(ValueError):
        gen.generate(text, out)
    assert not out.exists()


@pytest.mark.parametrize("corrupt", [False, True])
def test_missing_or_corrupt_icon_fails_without_output(tmp_path, corrupt):
    icon = tmp_path / "icon.png"
    if corrupt:
        icon.write_bytes(b"not an image")
    out = tmp_path / "qr.png"
    with pytest.raises(OSError):
        gen.generate("wxp://synthetic-test-only", out, icon)
    assert not out.exists()


def test_main_uses_env_without_logging_text(tmp_path, monkeypatch, capsys):
    text = "wxp://synthetic-env-test-only"
    out = tmp_path / "qr.png"
    monkeypatch.setenv("WECHAT_QRCODE", " \n" + text + "\n ")
    monkeypatch.setattr(gen, "DEFAULT_OUT", out)
    assert gen.main(["gen_wechat_qr.py"]) == 0
    assert out.is_file()
    captured = capsys.readouterr()
    assert text not in captured.out + captured.err


def test_main_explicit_arguments_override_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WECHAT_QRCODE", "ignored-env")
    out = tmp_path / "qr.png"
    calls = []

    def generate(text, path):
        calls.append((text, path))
        Path(path).write_bytes(b"png")
        return Path(path)

    monkeypatch.setattr(gen, "generate", generate)
    assert gen.main(["gen_wechat_qr.py", " wxp://explicit-test-only ", str(out)]) == 0
    assert calls == [("wxp://explicit-test-only", str(out))]
    assert "wxp://" not in capsys.readouterr().out


def test_main_empty_env_and_failure_do_not_leak(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WECHAT_QRCODE", "  ")
    assert gen.main(["gen_wechat_qr.py"]) == 2
    capsys.readouterr()
    text = "wxp://synthetic-sensitive-test-only"

    def fail(*args):
        raise ValueError(text)

    monkeypatch.setattr(gen, "generate", fail)
    assert gen.main(["gen_wechat_qr.py", text, str(tmp_path / "qr.png")]) == 1
    captured = capsys.readouterr()
    assert text not in captured.out + captured.err
    assert "ERROR" in captured.err
