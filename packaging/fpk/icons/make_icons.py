#!/usr/bin/env python3
"""Generate the fnmusic-ext App Center icons.

Outputs (relative to this script's directory):
  ICON.PNG            64x64   package icon
  ICON_256.PNG        256x256 package icon
  app/ui/images/icon_64.png   desktop entry icon
  app/ui/images/icon_256.png  desktop entry icon

Design: rounded square with a diagonal teal->indigo gradient and a white
eighth note, drawn at 4x and downscaled with Lanczos (sRGB square canvas,
rounded-rect silhouette per the fnOS icon guidelines).

Run: python3 packaging/fpk/icons/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent.parent  # packaging/fpk/

SIZE = 1024          # master canvas (4x of the 256 output)
CORNER = SIZE * 18 // 100

C1 = (45, 212, 191)    # teal-400
C2 = (67, 56, 202)     # indigo-700
WHITE = (255, 255, 255, 255)


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)) + (255,)


def _gradient(size):
    img = Image.new("RGBA", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            px[x, y] = _lerp(C1, C2, (x + y) / (2 * size - 2))
    return img


def _rounded_mask(size, radius):
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=radius, fill=255
    )
    return mask


def _draw_note(draw, size):
    # Eighth note: oblique stem, filled head, small flag. Coordinates in
    # fractions of the canvas, tuned to stay legible at 64px.
    def pt(fx, fy):
        return (fx * size, fy * size)

    head_c = pt(0.36, 0.70)
    head_r = 0.135 * size
    stem_w = 0.052 * size
    stem_top = pt(0.36 + 0.135 - stem_w / size / 2.6, 0.24)

    # Stem (slightly slanted): polygon from head top-right up to the top.
    draw.polygon(
        [
            (head_c[0] + head_r * 0.82, head_c[1] - head_r * 0.30),
            (stem_top[0] + stem_w, stem_top[1]),
            (stem_top[0] + stem_w * 2.6, stem_top[1] + 0.012 * size),
            (head_c[0] + head_r * 0.82 + stem_w, head_c[1] - head_r * 0.30 + 0.02 * size),
        ],
        fill=WHITE,
    )
    # Head: ellipse rotated look via a slightly squashed ellipse.
    draw.ellipse(
        [head_c[0] - head_r, head_c[1] - head_r * 0.78,
         head_c[0] + head_r, head_c[1] + head_r * 0.78],
        fill=WHITE,
    )
    # Flag: from the stem top toward the right, a curved wedge.
    x0, y0 = stem_top[0] + stem_w * 1.1, stem_top[1] + 0.008 * size
    draw.polygon(
        [
            (x0, y0),
            (x0 + 0.20 * size, y0 + 0.070 * size),
            (x0 + 0.155 * size, y0 + 0.235 * size),
            (x0 + 0.115 * size, y0 + 0.215 * size),
            (x0 + 0.145 * size, y0 + 0.095 * size),
            (x0, y0 + 0.062 * size),
        ],
        fill=WHITE,
    )


def build_master() -> Image.Image:
    grad = _gradient(SIZE)
    note = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    _draw_note(ImageDraw.Draw(note), SIZE)
    master = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    master.paste(grad, (0, 0), _rounded_mask(SIZE, CORNER))
    master.alpha_composite(note)
    return master


def write(master: Image.Image) -> None:
    here = HERE
    img256 = master.resize((256, 256), Image.LANCZOS)
    img64 = master.resize((64, 64), Image.LANCZOS)
    img256.save(here / "ICON_256.PNG", format="PNG")
    img64.save(here / "ICON.PNG", format="PNG")
    images = here / "app" / "ui" / "images"
    images.mkdir(parents=True, exist_ok=True)
    img256.save(images / "icon_256.png", format="PNG")
    img64.save(images / "icon_64.png", format="PNG")
    for p in (here / "ICON_256.PNG", here / "ICON.PNG",
              images / "icon_256.png", images / "icon_64.png"):
        print(f"wrote {p.relative_to(here)}")


if __name__ == "__main__":
    write(build_master())
