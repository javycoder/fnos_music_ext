#!/usr/bin/env python3
"""Generate the fnmusic-ext App Center and WebUI icons from master.png.

Outputs:
  packaging/fpk/ICON.PNG            64x64   package icon
  packaging/fpk/ICON_256.PNG        256x256 package icon
  packaging/fpk/app/ui/images/icon_64.png   desktop entry
  packaging/fpk/app/ui/images/icon_256.png  desktop entry
  webui-service/static/icon.png     256x256 favicon + sidebar mark

Master is a 1:1 PNG. White corner fill is flood-cleared to alpha, then a
rounded-rect silhouette is applied (18% radius, fnOS icon guidelines).
Drawn at master resolution and downscaled with Lanczos.

Run: python3 packaging/fpk/icons/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ICONS_DIR = Path(__file__).resolve().parent
FPK_DIR = ICONS_DIR.parent
REPO_ROOT = FPK_DIR.parent.parent
MASTER = ICONS_DIR / "master.png"

CORNER_RATIO = 18  # percent; matches existing fnOS silhouette


def _clear_white_corners(img: Image.Image, thresh: int = 40) -> Image.Image:
    """Flood near-white canvas corners to transparent; interior whites stay."""
    rgba = img.convert("RGBA")
    w, h = rgba.size
    for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        ImageDraw.floodfill(rgba, xy, (0, 0, 0, 0), thresh=thresh)
    return rgba


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=radius, fill=255
    )
    return mask


def build_master() -> Image.Image:
    if not MASTER.is_file():
        raise SystemExit(f"missing source icon: {MASTER}")
    src = _clear_white_corners(Image.open(MASTER))
    size = src.size[0]
    if src.size[0] != src.size[1]:
        raise SystemExit(f"master must be square, got {src.size}")
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(src, (0, 0))
    out.putalpha(_rounded_mask(size, size * CORNER_RATIO // 100))
    return out


def _save_png(img: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")
    print(f"wrote {path}")


def write(master: Image.Image) -> None:
    img256 = master.resize((256, 256), Image.LANCZOS)
    img64 = master.resize((64, 64), Image.LANCZOS)
    images = FPK_DIR / "app" / "ui" / "images"
    _save_png(img256, FPK_DIR / "ICON_256.PNG")
    _save_png(img64, FPK_DIR / "ICON.PNG")
    _save_png(img256, images / "icon_256.png")
    _save_png(img64, images / "icon_64.png")
    _save_png(img256, REPO_ROOT / "webui-service" / "static" / "icon.png")


if __name__ == "__main__":
    write(build_master())
