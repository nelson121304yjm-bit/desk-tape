#!/usr/bin/env python3.12
"""Render the Desk Tape app icon (AppIcon.icns) from geometry — no binary assets.

Design: a near-black squircle, one amber bar (the brand mark from the UI logo)
standing in front of a faint five-session market line. Reads at 16px as
"black tile + amber bar", which is all a Dock/Spotlight icon needs.
"""

from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024
SCALE = 2  # supersample for smooth edges

BG_TOP = (22, 24, 27)
BG_BOTTOM = (5, 6, 7)
AMBER = (255, 176, 0)
PAPER = (244, 241, 232)


def gradient(size: int) -> Image.Image:
    image = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(image)
    for y in range(size):
        ratio = y / (size - 1)
        eased = ratio ** 0.85
        color = tuple(round(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * eased) for i in range(3))
        draw.line([(0, y), (size, y)], fill=color)
    return image


def squircle_mask(size: int, margin: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [margin, margin, size - margin, size - margin], radius=radius, fill=255
    )
    return mask


def glow(size: int, center: tuple[int, int], radius: int, color: tuple[int, int, int], peak: int) -> Image.Image:
    layer = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(layer)
    steps = 26
    for step in range(steps, 0, -1):
        ratio = step / steps
        alpha = round(peak * (1 - ratio) ** 2.1)
        r = round(radius * ratio)
        draw.ellipse([center[0] - r, center[1] - r, center[0] + r, center[1] + r], fill=alpha)
    layer = layer.filter(ImageFilter.GaussianBlur(size * 0.02))
    tint = Image.new("RGB", (size, size), color)
    return tint, layer


def build() -> Image.Image:
    canvas = SIZE * SCALE
    base = gradient(canvas)
    mask = squircle_mask(canvas, margin=96 * SCALE, radius=210 * SCALE)

    # Amber bloom behind the bar, so the tile is not a flat black square.
    tint, alpha = glow(canvas, (512 * SCALE, 470 * SCALE), 430 * SCALE, AMBER, 52)
    glow_layer = tint.convert("RGBA")
    glow_layer.putalpha(alpha)
    composed = Image.alpha_composite(base.convert("RGBA"), glow_layer)

    # Faint five-session line behind the bar.
    line = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(line)
    points = [
        (205, 585), (300, 560), (368, 592), (432, 520), (505, 545),
        (566, 470), (640, 505), (706, 455), (790, 500), (826, 470),
    ]
    scaled = [(x * SCALE, y * SCALE) for x, y in points]
    draw.line(scaled, fill=PAPER + (96,), width=16 * SCALE, joint="curve")
    draw.line([(190 * SCALE, 646 * SCALE), (834 * SCALE, 646 * SCALE)], fill=PAPER + (26,), width=5 * SCALE)
    tip = (826 * SCALE, 470 * SCALE)
    draw.ellipse(
        [tip[0] - 15 * SCALE, tip[1] - 15 * SCALE, tip[0] + 15 * SCALE, tip[1] + 15 * SCALE],
        fill=AMBER + (255,),
    )

    composed = Image.alpha_composite(composed, line)

    # The brand mark: one amber bar with a rounded cap.
    bar = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    bar_draw = ImageDraw.Draw(bar)
    bar_draw.rounded_rectangle(
        [452 * SCALE, 300 * SCALE, 572 * SCALE, 724 * SCALE], radius=30 * SCALE, fill=AMBER + (255,)
    )
    composed = Image.alpha_composite(composed, bar)

    out = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    out.paste(composed, (0, 0), mask)
    return out.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    build_dir = root / "build"
    build_dir.mkdir(exist_ok=True)
    icon = build()
    icon.save(build_dir / "icon-preview.png")

    iconset = build_dir / "AppIcon.iconset"
    iconset.mkdir(exist_ok=True)
    for name, size in (
        ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
        ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
        ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
        ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
        ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
    ):
        icon.resize((size, size), Image.LANCZOS).save(iconset / name)

    result = subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(build_dir / "AppIcon.icns")],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(result.stderr.strip() or "iconutil failed", file=sys.stderr)
        return result.returncode
    print(build_dir / "AppIcon.icns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
