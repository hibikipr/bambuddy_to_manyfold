#!/usr/bin/env python3
"""
generate_icons.py — one-off generator for the web app's PWA icon set.

Renders the same isometric Bambu-green cube used as the Tkinter GUI's window
icon (see `_make_icon_png` in bambuddy_to_manyfold_gui.py), scaled up to the
sizes a PWA manifest needs. Pure stdlib (struct + zlib), no Pillow.

Run once at dev time; the output PNGs are committed to static/icons/ and are
not regenerated at container build/run time.

    python3 scripts/generate_icons.py

Copyright (C) 2026 Victor Manuel (hibikipr)
SPDX-License-Identifier: AGPL-3.0-or-later
"""

import struct
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "static" / "icons"

# Bambu-green cube faces (top light, left mid, right dark) — same palette as
# the Tkinter GUI's window icon and the web UI's inline SVG header logo.
FACE_COLORS = [(0, 198, 77), (0, 174, 66), (0, 148, 56)]
BG_COLOR = (26, 26, 26)  # #1a1a1a, matches the app's theme background


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


def render_cube(size: int, scale: float = 1.0, opaque_bg: tuple[int, int, int] | None = None) -> bytearray:
    """Render the isometric cube as RGBA pixels.

    ``scale`` shrinks the cube toward the center (used for the maskable icon,
    whose "safe zone" is roughly the middle 80% of the canvas). ``opaque_bg``
    fills everywhere outside the cube with a solid color instead of leaving it
    transparent (needed for maskable icons and Apple touch icons, which don't
    render transparency the way a regular favicon does).
    """
    s = size
    if opaque_bg is not None:
        r, g, b = opaque_bg
        px = bytearray((r, g, b, 255) * (s * s))
        # bytearray(tuple * n) above doesn't interleave correctly; build properly instead.
        px = bytearray()
        for _ in range(s * s):
            px.extend((r, g, b, 255))
    else:
        px = bytearray(s * s * 4)  # transparent RGBA

    # Cube faces as parallelograms (origin + two edge vectors), in a 64-unit
    # design grid, then scaled to `size` and re-centered by `scale`.
    f = (s / 64.0) * scale
    off = (s - s * scale) / 2.0
    T = (32 * f + off, 8 * f + off)
    L = (8 * f + off, 22 * f + off)
    R = (56 * f + off, 22 * f + off)
    B = (32 * f + off, 36 * f + off)
    side = 24 * f

    faces = [
        (T, (L[0] - T[0], L[1] - T[1]), (R[0] - T[0], R[1] - T[1]), FACE_COLORS[0]),
        (L, (0, side), (B[0] - L[0], B[1] - L[1]), FACE_COLORS[1]),
        (R, (0, side), (B[0] - R[0], B[1] - R[1]), FACE_COLORS[2]),
    ]

    for y in range(s):
        for x in range(s):
            cx, cy = x + 0.5, y + 0.5
            for (ox, oy), (ux, uy), (vx, vy), (cr, cg, cb) in faces:
                det = ux * vy - uy * vx
                if det == 0:
                    continue
                a = ((cx - ox) * vy - (cy - oy) * vx) / det
                b = (ux * (cy - oy) - uy * (cx - ox)) / det
                if -0.02 <= a <= 1.02 and -0.02 <= b <= 1.02:
                    i = (y * s + x) * 4
                    px[i], px[i + 1], px[i + 2], px[i + 3] = cr, cg, cb, 255
                    break

    return px


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Full-bleed, transparent background — for the manifest's "any" purpose icons.
    _write_png(OUT_DIR / "icon-192.png", 192, render_cube(192, scale=1.0))
    _write_png(OUT_DIR / "icon-512.png", 512, render_cube(512, scale=1.0))

    # Maskable: opaque background, cube shrunk into the ~80% safe zone so OS
    # icon masks (circle, squircle, etc.) don't clip it.
    _write_png(OUT_DIR / "maskable-512.png", 512, render_cube(512, scale=0.62, opaque_bg=BG_COLOR))

    # Apple touch icon: iOS renders transparency as black, so use an opaque
    # background at the standard 180x180 size.
    _write_png(OUT_DIR / "apple-touch-icon.png", 180, render_cube(180, scale=0.82, opaque_bg=BG_COLOR))

    print(f"Wrote icons to {OUT_DIR}")


if __name__ == "__main__":
    main()
