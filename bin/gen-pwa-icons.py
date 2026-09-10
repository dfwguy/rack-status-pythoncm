#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
#
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""Rasterise the NVIS Greenlight "lit rack" mark into the PNG app icons the manifest needs.

Writes ``frontend/public/icons/greenlight-{180,192,512}.png``. Those files are **committed**,
like the vendored Kaizen fonts: an installable app needs raster icons at fixed sizes, the mark
changes about once a year, and neither the CI image nor an air-gapped host should have to
rasterise anything at build time.

**It renders the three-bar rack mark, not the signal mark, and that is the decision rather than
a default.** Greenlight ships a two-mark family: ``greenlight-mark-signal.svg`` (three lamps) is
the in-app mark used by ``GreenlightMark.tsx``, and ``greenlight-mark-rack.svg`` (a cabinet with
three trays) owns every context that is small and outside the app -- the browser tab, and now the
installed app icon. The signal mark lost that role on measurement: in a 16px box it shrinks to a
~7.5px sliver and its unlit lamps merge into the cabinet wall, where the rack mark stays legible.
An OS dock or Start menu is the same job as a favicon, so it gets the same mark.

Standard library only, and deliberately so. The obvious tool is ``sharp``, but that is a ~10 MB
platform-specific native module with install scripts -- which this repo gates behind
``allowScripts`` in ``frontend/package.json`` -- pulled in for one job that runs when the branding
changes. The mark is four rounded rectangles, so an analytic rasteriser is shorter than the
dependency discussion.

Run it after editing ``frontend/public/brand/greenlight-mark-rack.svg``::

    bin/gen-pwa-icons.py

**The geometry below is a transcription of that SVG, not a parse of it.** Keep the two in step by
hand; there is no SVG parser here and a silent drift shows up only as an icon that no longer
matches the favicon.

Icons are **opaque**: one with a transparent background gets a black or white plate from whichever
OS surface draws it, so it would be legible on a dock and wrong on a light Start menu.
"""

from __future__ import annotations

import struct
import sys
import zlib
from math import hypot
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "frontend" / "public" / "icons"

#: Sizes to emit. 192 and 512 are Chromium's installability floor; 180 is what Safari's
#: ``apple-touch-icon`` uses for "Add to Dock" on macOS, which ignores the manifest entirely.
SIZES = (180, 192, 512)

#: ``electron-builder`` derives every platform artifact -- ``.icns``, ``.ico``, the Linux
#: hicolor set -- from one PNG, and warns below 512. 1024 because macOS renders the app icon at
#: 512@2x, and an upscale there is visible in the dock.
#:
#: Emitted from this script rather than a second one so both consumers of the mark rasterise
#: through the same code: a separate desktop generator is how the two would drift, and the
#: divergence would only show up as "the dock icon looks slightly different".
DESKTOP_ICON = REPO_ROOT / "desktop" / "resources" / "icon.png"
DESKTOP_ICON_SIZE = 1024

#: Sub-samples per axis. 4x4 = 16 samples per pixel, which is enough to keep the 1.5-unit
#: stroke smooth at 180px; the whole run is a few seconds, so there is no reason to tune it.
SUPERSAMPLE = 4

# --- Palette -------------------------------------------------------------------------------
# The SVG paints the cabinet with ``currentColor`` so it can invert with the theme. A file on a
# dock cannot, so the icon is pinned to the dark treatment. Note this is a *brighter* grey than
# `favicon.svg`'s fixed #8C8C8C, and deliberately: that value has to survive an unknown tab strip,
# whereas here the near-black plate is part of the image, so the cabinet can be lighter.
BACKGROUND = (0x0D, 0x0D, 0x0D)
CABINET_COLOUR = (0xE8, 0xE8, 0xE8)
TRAY_COLOUR = (0x76, 0xB9, 0x00)

# --- Geometry, transcribed from greenlight-mark-rack.svg (viewBox 0 0 24 24) ----------------
VIEWBOX = 24.0
CABINET = {"x": 4.6, "y": 2.6, "w": 14.8, "h": 18.8, "rx": 2.6, "stroke": 1.5, "opacity": 0.75}
TRAYS = tuple(
    {"x": 7.6, "y": y, "w": 8.8, "h": 2.4, "rx": 0.9} for y in (5.6, 10.8, 16.0)
)

#: Extent of the drawn art in viewBox units, stroke included: the cabinet path is the outermost
#: shape and its stroke straddles that path by half its width on each side.
ART_W = CABINET["w"] + CABINET["stroke"]
ART_H = CABINET["h"] + CABINET["stroke"]

#: Android and ChromeOS may crop a ``maskable`` icon to a circle of this radius, as a fraction of
#: the canvas. Anything outside is not guaranteed to survive.
MASKABLE_SAFE_RADIUS = 0.40
#: Slack inside that circle, so the art does not sit exactly on the boundary.
MASKABLE_MARGIN = 0.96

#: viewBox units per pixel of canvas, derived rather than chosen.
#:
#: **The binding constraint is the art's diagonal, not its height** -- the safe area is a circle,
#: so the corners are what leave it first. Deriving the scale here means the constraint survives a
#: geometry edit: the rack mark is meaningfully wider than the signal mark this script first drew,
#: and simply reusing that mark's hand-tuned 66%-of-height would have pushed the corners to 0.423
#: of the canvas -- outside the safe circle, and invisibly so until some launcher cropped them.
SCALE_PER_PX = (MASKABLE_SAFE_RADIUS * MASKABLE_MARGIN) / hypot(ART_W / 2.0, ART_H / 2.0)


def _rounded_rect_distance(px: float, py: float, rect: dict[str, float]) -> float:
    """Signed distance from *(px, py)* to *rect*'s rounded path, in viewBox units.

    Negative inside, positive outside. A filled tray is then every point at distance <= 0, and
    the cabinet's stroke is every point within half a stroke width of the path -- which is what
    makes an outline cheap to rasterise analytically instead of walking a path.
    """
    half_w = rect["w"] / 2.0
    half_h = rect["h"] / 2.0
    radius = min(rect["rx"], half_w, half_h)
    qx = abs(px - (rect["x"] + half_w)) - (half_w - radius)
    qy = abs(py - (rect["y"] + half_h)) - (half_h - radius)
    return hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - radius


def _coverage(size: int) -> tuple[list[list[float]], list[list[float]]]:
    """Per-pixel coverage in 0..1 for the cabinet stroke and the three trays.

    Two separate masks rather than one composited buffer because they are painted at different
    opacities, and compositing has to happen in colour space once coverage is known -- averaging
    colours and opacities together haloes the stroke where a tray abuts it.
    """
    scale = SCALE_PER_PX * size
    centre = size / 2.0
    # The art is centred on the cabinet, which is not the viewBox centre for this mark.
    art_cx = CABINET["x"] + CABINET["w"] / 2.0
    art_cy = CABINET["y"] + CABINET["h"] / 2.0
    half_stroke = CABINET["stroke"] / 2.0
    step = 1.0 / SUPERSAMPLE
    weight = 1.0 / (SUPERSAMPLE * SUPERSAMPLE)

    cabinet = [[0.0] * size for _ in range(size)]
    trays = [[0.0] * size for _ in range(size)]

    for row in range(size):
        for col in range(size):
            for sy in range(SUPERSAMPLE):
                # Sub-pixel sample centre, mapped back into viewBox units about the art centre.
                vy = (row + (sy + 0.5) * step - centre) / scale + art_cy
                for sx in range(SUPERSAMPLE):
                    vx = (col + (sx + 0.5) * step - centre) / scale + art_cx
                    if abs(_rounded_rect_distance(vx, vy, CABINET)) <= half_stroke:
                        cabinet[row][col] += weight
                    for tray in TRAYS:
                        if _rounded_rect_distance(vx, vy, tray) <= 0.0:
                            trays[row][col] += weight
                            break
    return cabinet, trays


def _render(size: int) -> bytes:
    """Composite the masks over the background into raw RGB scanline bytes."""
    cabinet, trays = _coverage(size)
    out = bytearray()
    for row in range(size):
        out.append(0)  # PNG filter type 0 (None) for this scanline
        for col in range(size):
            r, g, b = BACKGROUND
            for coverage, colour, opacity in (
                (cabinet[row][col], CABINET_COLOUR, CABINET["opacity"]),
                (trays[row][col], TRAY_COLOUR, 1.0),
            ):
                alpha = coverage * opacity
                if alpha <= 0.0:
                    continue
                r = r + (colour[0] - r) * alpha
                g = g + (colour[1] - g) * alpha
                b = b + (colour[2] - b) * alpha
            out += bytes((round(r), round(g), round(b)))
    return bytes(out)


def _png(size: int, raw: bytes) -> bytes:
    """Wrap raw RGB scanlines as an 8-bit truecolour PNG."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    # width, height, bit depth 8, colour type 2 (truecolour), deflate, no filter, no interlace
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    # Cheap guard on the derivation above: if a geometry edit ever pushes the art outside the
    # maskable safe circle, say so here rather than shipping corners a launcher will crop.
    half_diagonal = hypot(ART_W / 2.0, ART_H / 2.0) * SCALE_PER_PX
    if half_diagonal > MASKABLE_SAFE_RADIUS:
        print(
            f"art half-diagonal is {half_diagonal:.4f} of the canvas, outside the "
            f"{MASKABLE_SAFE_RADIUS} maskable safe radius",
            file=sys.stderr,
        )
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    outputs = [(OUT_DIR / f"greenlight-{size}.png", size) for size in SIZES]
    # Skipped rather than created when `desktop/` is absent: this script's first job is the web
    # manifest, and a checkout without the desktop shell should not grow a stray directory.
    if DESKTOP_ICON.parent.parent.is_dir():
        DESKTOP_ICON.parent.mkdir(parents=True, exist_ok=True)
        outputs.append((DESKTOP_ICON, DESKTOP_ICON_SIZE))

    for path, size in outputs:
        path.write_bytes(_png(size, _render(size)))
        print(f"wrote {path.relative_to(REPO_ROOT)} ({size}px, {path.stat().st_size:,} bytes)")
    print(
        f"art occupies {ART_H * SCALE_PER_PX:.1%} of canvas height; "
        f"half-diagonal {half_diagonal:.1%} (safe radius {MASKABLE_SAFE_RADIUS:.0%})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
