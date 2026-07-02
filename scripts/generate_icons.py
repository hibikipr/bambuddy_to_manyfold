#!/usr/bin/env python3
"""
generate_icons.py — one-off generator for the web app's PWA icon set.

Same isometric cube geometry/size as the original Bambu-green icon (and as
filament_to_bambuddy's icon), but recolored with Manyfold's blue-to-violet
faceted-gem palette instead of Bambu green, so bambuddy_to_manyfold reads as
"talks to Manyfold" while keeping the familiar large, bold cube silhouette.

Pure stdlib (struct + zlib), no Pillow.

    python3 scripts/generate_icons.py

Copyright (C) 2026 Victor Manuel (hibikipr)
SPDX-License-Identifier: AGPL-3.0-or-later
"""

import struct
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "static" / "icons"

BG_COLOR = (26, 26, 26)  # #1a1a1a, matches the app's theme background

# Manyfold-style blue -> violet gradient, one pair of stops per cube face.
TOP_A, TOP_B = (110, 231, 255), (139, 122, 255)     # #6ee7ff -> #8b7aff (light, top face)
LEFT_A, LEFT_B = (79, 139, 255), (139, 92, 246)      # #4f8bff -> #8b5cf6 (mid, left face)
RIGHT_A, RIGHT_B = (124, 79, 224), (192, 132, 252)   # #7c4fe0 -> #c084fc (deep, right face)


def _png_chunk(typ: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)


def _write_png(path: Path, size: int, px: bytearray):
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter type 0 per scanline
        raw.extend(px[y * size * 4:(y + 1) * size * 4])
    idat = zlib.compress(bytes(raw), 9)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    data = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")
    path.write_bytes(data)


def _lerp3(c1, c2, t):
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def render_cube(size: int, scale: float = 1.0, opaque_bg=None) -> bytearray:
    """Render the isometric cube as RGBA pixels, faces filled with a gradient.

    ``scale`` shrinks the cube toward the center (used for the maskable icon,
    whose "safe zone" is roughly the middle 80% of the canvas). ``opaque_bg``
    fills everywhere outside the cube with a solid color instead of leaving it
    transparent (needed for maskable icons and Apple touch icons).
    """
    s = size
    if opaque_bg is not None:
        r, g, b = opaque_bg
        px = bytearray()
        for _ in range(s * s):
            px.extend((r, g, b, 255))
    else:
        px = bytearray(s * s * 4)  # transparent RGBA

    # Same 64-unit design grid as the original cube generator.
    f = (s / 64.0) * scale
    off = (s - s * scale) / 2.0
    T = (32 * f + off, 8 * f + off)
    L = (8 * f + off, 22 * f + off)
    R = (56 * f + off, 22 * f + off)
    B = (32 * f + off, 36 * f + off)
    side = 24 * f

    faces = [
        (T, (L[0] - T[0], L[1] - T[1]), (R[0] - T[0], R[1] - T[1]), TOP_A, TOP_B),
        (L, (0, side), (B[0] - L[0], B[1] - L[1]), LEFT_A, LEFT_B),
        (R, (0, side), (B[0] - R[0], B[1] - R[1]), RIGHT_A, RIGHT_B),
    ]

    for y in range(s):
        for x in range(s):
            cx, cy = x + 0.5, y + 0.5
            for (ox, oy), (ux, uy), (vx, vy), col_a, col_b in faces:
                det = ux * vy - uy * vx
                if det == 0:
                    continue
                a = ((cx - ox) * vy - (cy - oy) * vx) / det
                b = (ux * (cy - oy) - uy * (cx - ox)) / det
                if -0.02 <= a <= 1.02 and -0.02 <= b <= 1.02:
                    # gradient along the face's "b" axis (top-to-bottom of each facet)
                    t = max(0.0, min(1.0, b))
                    cr, cg, cb = _lerp3(col_a, col_b, t)
                    i = (y * s + x) * 4
                    px[i], px[i + 1], px[i + 2], px[i + 3] = cr, cg, cb, 255
                    break

    return px


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    _write_png(OUT_DIR / "icon-192.png", 192, render_cube(192, scale=1.0))
    _write_png(OUT_DIR / "icon-512.png", 512, render_cube(512, scale=1.0))

    _write_png(OUT_DIR / "maskable-512.png", 512, render_cube(512, scale=0.62, opaque_bg=BG_COLOR))

    _write_png(OUT_DIR / "apple-touch-icon.png", 180, render_cube(180, scale=0.82, opaque_bg=BG_COLOR))

    print(f"Wrote icons to {OUT_DIR}")


if __name__ == "__main__":
    main()
