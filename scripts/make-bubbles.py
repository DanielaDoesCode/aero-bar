#!/usr/bin/env python3
"""Render the seamless bubble tiles used as the animated waybar background.

Each tile is TILE_H tall (the bar's height) and repeats in both directions,
so bubbles that cross a tile edge are also drawn on the opposite edge.
Re-run after changing anything here:  python3 scripts/make-bubbles.py
"""
import math
import os

import cairo

TILE_H = 34
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bubbles")

# name: (tile width, opacity, [(x, y, radius), ...])
LAYERS = {
    "near": (240, 1.00, [(30, 22, 5.5), (128, 9, 3.5), (190, 26, 2.5)]),
    "mid":  (310, 0.80, [(90, 17, 7.0), (215, 29, 3.0), (270, 7, 2.0)]),
    "far":  (380, 0.60, [(60, 6, 2.5), (250, 15, 4.5), (330, 30, 2.0)]),
}


def bubble(ctx, x, y, r, alpha):
    # body: almost clear in the middle, thickening into a bright rim
    body = cairo.RadialGradient(x, y, 0, x, y, r)
    body.add_color_stop_rgba(0.00, 0.85, 0.96, 1.00, 0.04 * alpha)
    body.add_color_stop_rgba(0.60, 0.80, 0.94, 1.00, 0.12 * alpha)
    body.add_color_stop_rgba(0.88, 1.00, 1.00, 1.00, 0.75 * alpha)
    body.add_color_stop_rgba(1.00, 1.00, 1.00, 1.00, 0.00)
    ctx.set_source(body)
    ctx.arc(x, y, r, 0, 2 * math.pi)
    ctx.fill()

    # specular highlight, upper-left
    hx, hy, hr = x - r * 0.38, y - r * 0.40, max(r * 0.32, 0.9)
    spec = cairo.RadialGradient(hx, hy, 0, hx, hy, hr)
    spec.add_color_stop_rgba(0.0, 1, 1, 1, 0.95 * alpha)
    spec.add_color_stop_rgba(1.0, 1, 1, 1, 0.00)
    ctx.set_source(spec)
    ctx.arc(hx, hy, hr, 0, 2 * math.pi)
    ctx.fill()

    # faint secondary reflection, lower-right
    if r >= 4:
        rx, ry, rr = x + r * 0.45, y + r * 0.45, r * 0.22
        refl = cairo.RadialGradient(rx, ry, 0, rx, ry, rr)
        refl.add_color_stop_rgba(0.0, 1, 1, 1, 0.45 * alpha)
        refl.add_color_stop_rgba(1.0, 1, 1, 1, 0.00)
        ctx.set_source(refl)
        ctx.arc(rx, ry, rr, 0, 2 * math.pi)
        ctx.fill()


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, (w, alpha, bubbles) in LAYERS.items():
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, TILE_H)
        ctx = cairo.Context(surf)
        for x, y, r in bubbles:
            # draw wrapped copies so the tile is seamless at every edge
            for dx in (-w, 0, w):
                for dy in (-TILE_H, 0, TILE_H):
                    bubble(ctx, x + dx, y + dy, r, alpha)
        path = os.path.join(OUT, f"{name}.png")
        surf.write_to_png(path)
        print("wrote", os.path.normpath(path))


if __name__ == "__main__":
    main()
