"""付款码 CI 接线与真实嵌套 FPK 资源校验（全部使用虚构数据）。"""
from __future__ import annotations

import importlib.util
import io
import tarfile
from pathlib import Path

from PIL import Image
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_donate_assets", ROOT / "scripts/check_donate_assets.py")
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)


def make_fpk(tmp_path, files):
    inner_bytes = io.BytesIO()
    with tarfile.open(fileobj=inner_bytes, mode="w:gz") as inner:
        for name, data in files.items():
            info = tarfile.TarInfo(check.STATIC_PREFIX + name)
            info.size = len(data)
            inner.addfile(info, io.BytesIO(data))
    archive = tmp_path / "test.fpk"
    data = inner_bytes.getvalue()
    with tarfile.open(archive, "w:gz") as outer:
        info = tarfile.TarInfo("app.tgz")
        info.size = len(data)
        outer.addfile(info, io.BytesIO(data))
    return archive


def png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.parametrize("names", [[], ["wechat.png"], ["alipay.png"], ["wechat.png", "alipay.png"]])
def test_configured_channels_must_be_in_inner_archive(tmp_path, names):
    archive = make_fpk(tmp_path, {name: png_bytes() for name in names})
    check.verify(archive, names)


def test_unconfigured_channels_do_not_require_an_archive(tmp_path):
    check.verify(tmp_path / "absent.fpk", [])


def test_missing_channel_fails(tmp_path):
    archive = make_fpk(tmp_path, {"alipay.png": png_bytes()})
    with pytest.raises(ValueError, match="wechat.png"):
        check.verify(archive, ["wechat.png", "alipay.png"])


@pytest.mark.parametrize("data", [b"", b"not png", b"\x89PNG\r\n\x1a\n", png_bytes()[:16] + b"\0" * 8])
def test_invalid_png_fails(tmp_path, data):
    with pytest.raises(ValueError, match="PNG"):
        check.verify(make_fpk(tmp_path, {"wechat.png": data}), ["wechat.png"])


def test_main_selects_channels_without_logging_values(tmp_path, monkeypatch, capsys):
    archive = make_fpk(tmp_path, {"wechat.png": png_bytes()})
    text = "wxp://synthetic-check-only"
    monkeypatch.setenv("WECHAT_QRCODE", text)
    monkeypatch.setenv("ALIPAY_QRCODE", " \n")
    monkeypatch.setattr("sys.argv", ["check_donate_assets.py", str(archive)])
    check.main()
    assert text not in capsys.readouterr().out


def test_main_fails_when_archive_missing_even_without_configured_channels(tmp_path, monkeypatch):
    monkeypatch.delenv("WECHAT_QRCODE", raising=False)
    monkeypatch.delenv("ALIPAY_QRCODE", raising=False)
    monkeypatch.setattr("sys.argv", ["check_donate_assets.py", str(tmp_path / "missing.fpk")])
    with pytest.raises(SystemExit) as exc:
        check.main()
    assert exc.value.code == 1


@pytest.mark.parametrize("filename,job_name", [("ci.yml", "fpk"), ("release.yml", "release")])
def test_workflows_generate_channels_independently_and_verify_payload(filename, job_name):
    workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text(encoding="utf-8"))
    job = workflow["jobs"][job_name]
    for key in ("WECHAT_QRCODE", "ALIPAY_QRCODE"):
        assert job["env"][key] == "${{ secrets." + key + " }}"
    steps = job["steps"]
    dependency_step = next(s for s in steps if s.get("name") == "Install donation QR dependencies (optional)")
    assert dependency_step["if"] == "env.ALIPAY_QRCODE != '' || env.WECHAT_QRCODE != ''"
    assert "qrcode pillow" in dependency_step["run"]
    for channel in ("wechat", "alipay"):
        generation = next(s for s in steps if f"scripts/gen_{channel}_qr.py" in s.get("run", ""))
        assert generation["if"] == f"env.{channel.upper()}_QRCODE != ''"
        assert f"test -s webui-service/static/{channel}.png" in generation["run"]
        assert "|| true" not in generation["run"]
        assert steps.index(dependency_step) < steps.index(generation)
    font = next(s for s in steps if "Install CJK font" in s.get("name", ""))
    assert font["if"] == "env.ALIPAY_QRCODE != ''"
    build = next(i for i, s in enumerate(steps) if s.get("name") == "Build fpk package")
    verification = next(i for i, s in enumerate(steps) if "scripts/check_donate_assets.py" in s.get("run", ""))
    assert verification > build
    assert "if" not in steps[verification]
    # Decoder is test-only and must actually be available in both CI suites.
    test_steps = steps if filename == "release.yml" else workflow["jobs"]["test"]["steps"]
    assert any("pip install" in s.get("run", "") and "zxing-cpp" in s["run"] for s in test_steps)


def test_generated_payment_images_are_ignored():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "webui-service/static/wechat.png" in ignored
    assert "webui-service/static/alipay.png" in ignored
