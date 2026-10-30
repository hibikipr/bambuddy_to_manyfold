#!/usr/bin/env python3
"""
generate_icons.py — generator for the web app's PWA icon set.

An isometric model cube inside a two-way sync loop, on a dark rounded tile in
Bambu green: "models flow from Bambuddy into Manyfold". Shares its tile and
palette with filament_to_bambuddy's icon so the two helpers read as a family.

Writes the SVG masters (icon.svg, maskable.svg, apple-touch-icon.svg) to
static/icons/ and renders the PNGs from them with headless Chrome (set CHROME
to point at another Chrome/Chromium binary).

    python3 scripts/generate_icons.py

Copyright (C) 2026 Victor Manuel (hibikipr)
SPDX-License-Identifier: AGPL-3.0-or-later
"""

import os
import subprocess
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "static" / "icons"
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

TILE = "#1a1a1a"   # app theme background
EDGE = "#3d3d3d"   # Bambuddy border colour
INK = "#f5f5f5"
ACCENT, ACCENT_LIGHT, ACCENT_DARK = "#00AE42", "#00C64D", "#009438"  # Bambu green

GLYPH = (
    # sync arrows: top arc left->right, bottom arc right->left
    f'<path d="M16.2 37.7 A36 36 0 0 1 83.8 37.7" fill="none" stroke="{INK}" stroke-width="4.5" stroke-linecap="round"/>'
    f'<polygon points="85.9,43.3 88.5,36.0 79.1,39.4" fill="{INK}" stroke="{INK}" stroke-width="2" stroke-linejoin="round"/>'
    f'<path d="M83.8 62.3 A36 36 0 0 1 16.2 62.3" fill="none" stroke="{INK}" stroke-width="4.5" stroke-linecap="round"/>'
    f'<polygon points="14.1,56.7 11.5,64.0 20.9,60.6" fill="{INK}" stroke="{INK}" stroke-width="2" stroke-linejoin="round"/>'
    # isometric cube
    f'<polygon points="50,30 67.3,40 50,50 32.7,40" fill="{ACCENT_LIGHT}"/>'
    f'<polygon points="32.7,40 50,50 50,70 32.7,60" fill="{ACCENT}"/>'
    f'<polygon points="50,50 67.3,40 67.3,60 50,70" fill="{ACCENT_DARK}"/>'
    f'<path d="M50 30 L67.3 40 L67.3 60 L50 70 L32.7 60 L32.7 40 Z M32.7 40 L50 50 L67.3 40 M50 50 L50 70" '
    f'fill="none" stroke="{TILE}" stroke-width="1.2" stroke-linejoin="round"/>'
)


def _variant(kind: str) -> str:
    """Wrap GLYPH (drawn on a 100x100 grid) for one icon variant."""
    if kind == "any":
        # Rounded dark tile, transparent outside it.
        bg = (f'<rect width="100" height="100" rx="22" fill="{TILE}"/>'
              f'<rect x="0.5" y="0.5" width="99" height="99" rx="21.5" fill="none" stroke="{EDGE}"/>')
        glyph = GLYPH
    elif kind == "maskable":
        # Full-bleed background; glyph shrunk into the ~80% maskable safe zone.
        bg = f'<rect width="100" height="100" fill="{TILE}"/>'
        glyph = f'<g transform="translate(50 50) scale(0.75) translate(-50 -50)">{GLYPH}</g>'
    else:  # apple: full-bleed square, iOS rounds the corners itself
        bg = f'<rect width="100" height="100" fill="{TILE}"/>'
        glyph = GLYPH
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
            f"{bg}{glyph}</svg>\n")


def _render(svg: Path, png: Path, size: int):
    html = svg.with_suffix(".render.html")
    html.write_text(f'<html><body style="margin:0;background:transparent">'
                    f'<img src="{svg.name}" style="display:block;width:{size}px;height:{size}px"></body></html>')
    try:
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        "--default-background-color=00000000", f"--window-size={size},{size}",
                        f"--screenshot={png}", html.as_uri()],
                       check=True, capture_output=True)
    finally:
        html.unlink()


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sources = {
        "any": OUT_DIR / "icon.svg",
        "maskable": OUT_DIR / "maskable.svg",
        "apple": OUT_DIR / "apple-touch-icon.svg",
    }
    for kind, path in sources.items():
        path.write_text(_variant(kind))

    _render(sources["any"], OUT_DIR / "icon-192.png", 192)
    _render(sources["any"], OUT_DIR / "icon-512.png", 512)
    _render(sources["maskable"], OUT_DIR / "maskable-512.png", 512)
    _render(sources["apple"], OUT_DIR / "apple-touch-icon.png", 180)

    print(f"Wrote icons to {OUT_DIR}")


if __name__ == "__main__":
    main()
