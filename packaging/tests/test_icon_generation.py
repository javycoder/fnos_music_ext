"""Check the generated fnOS icons and their transparency."""

import importlib.util
from pathlib import Path

import pytest

Image = pytest.importorskip("PIL.Image")
ImageChops = pytest.importorskip("PIL.ImageChops")
ImageDraw = pytest.importorskip("PIL.ImageDraw")

REPO_ROOT = Path(__file__).resolve().parents[2]
FPK_DIR = REPO_ROOT / "packaging" / "fpk"


@pytest.fixture(scope="module")
def icon_generator():
    script = FPK_DIR / "icons" / "make_icons.py"
    spec = importlib.util.spec_from_file_location("fpk_make_icons", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generator_preserves_cleared_corner_alpha(icon_generator):
    with Image.open(icon_generator.MASTER) as source:
        source_alpha = icon_generator._clear_white_corners(source).getchannel("A")
    generated_alpha = icon_generator.build_master().getchannel("A")

    assert ImageChops.subtract(generated_alpha, source_alpha).getbbox() is None


def test_generator_preserves_existing_transparency_and_interior_white(
    icon_generator, tmp_path, monkeypatch
):
    source = Image.new("RGBA", (100, 100), (255, 255, 255, 255))
    drawing = ImageDraw.Draw(source)
    drawing.rounded_rectangle((0, 0, 99, 99), radius=30, fill=(20, 150, 220, 255))
    drawing.rectangle((40, 40, 60, 60), fill=(255, 255, 255, 255))
    source.putpixel((50, 50), (0, 0, 0, 0))
    master = tmp_path / "master.png"
    source.save(master)
    monkeypatch.setattr(icon_generator, "MASTER", master)

    generated = icon_generator.build_master()

    assert generated.getpixel((50, 50))[3] == 0
    assert generated.getpixel((45, 45)) == (255, 255, 255, 255)


@pytest.mark.parametrize(
    ("relative_path", "size"),
    [
        ("packaging/fpk/ICON.PNG", 64),
        ("packaging/fpk/ICON_256.PNG", 256),
        ("packaging/fpk/app/ui/images/icon_64.png", 64),
        ("packaging/fpk/app/ui/images/icon_256.png", 256),
        ("webui-service/static/icon.png", 256),
    ],
)
def test_generated_icon_has_transparent_corners_without_black_border(relative_path, size):
    with Image.open(REPO_ROOT / relative_path) as image:
        assert image.format == "PNG"
        assert image.mode == "RGBA"
        assert image.size == (size, size)
        assert image.info.get("srgb") == 0
        assert image.getpixel((0, 0))[3] == 0
        assert image.getpixel((size - 1, 0))[3] == 0
        assert image.getpixel((0, size - 1))[3] == 0
        assert image.getpixel((size - 1, size - 1))[3] == 0

        border = size // 8
        for vertical in range(size):
            for horizontal in range(size):
                if (
                    border <= horizontal < size - border
                    and border <= vertical < size - border
                ):
                    continue
                red, green, blue, alpha = image.getpixel((horizontal, vertical))
                assert alpha < 128 or max(red, green, blue) >= 20, (
                    f"{relative_path}: black border at {(horizontal, vertical)}"
                )


def test_matching_sizes_use_identical_assets():
    groups = [
        [
            FPK_DIR / "ICON.PNG",
            FPK_DIR / "app" / "ui" / "images" / "icon_64.png",
        ],
        [
            FPK_DIR / "ICON_256.PNG",
            FPK_DIR / "app" / "ui" / "images" / "icon_256.png",
            REPO_ROOT / "webui-service" / "static" / "icon.png",
        ],
    ]
    for group in groups:
        assert all(path.read_bytes() == group[0].read_bytes() for path in group[1:])
