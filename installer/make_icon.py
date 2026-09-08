"""Draw installer/GamechangerTalker.ico.

    python installer/make_icon.py

The icon is generated rather than committed as an opaque binary, for the same
reason the Warudo scene is exported by a tool: a 100 KB blob nobody can read
is a thing that can never be adjusted, only replaced. Change a number here and
run it again.

No Pillow. The repo has numpy and nothing else that draws, and adding an image
library to ship one icon is a poor trade -- so the shapes are evaluated
analytically with supersampling, and the ICO container is written by hand. It
is about eighty lines of arithmetic and it has no dependencies at all.

WHAT IT DRAWS, AND WHY IT IS THIS SIMPLE
A gold speech bubble on a dark square, with three rising bars inside it. The
subject is a thing that talks about a market, so: a bubble and a chart.

The constraint that decides everything is 16x16. That is the size Windows uses
in the taskbar, in Explorer's detail view and in the Start Menu search
results, and it is where most icons turn to mush. So the silhouette does the
work -- a bubble shape in one strong colour against a dark ground reads at
16px even when the line inside it has smeared to two pixels -- and the detail
is there for the sizes that can hold it.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

# Windows asks for these, in this order, in different places: 16 in the
# taskbar and Explorer lists, 32 on the desktop, 48 in Explorer's medium view,
# 64 for high-DPI versions of those, 256 for the extra-large view and for the
# icon Add/Remove Programs shows.
SIZES = (16, 32, 48, 64, 128, 256)
#: Sub-samples per axis. 4 means sixteen point tests per pixel, which is
#: enough antialiasing that the bubble's curve does not stair-step at 32px.
SUPERSAMPLE = 4

INK = (0x10, 0x14, 0x1A)  # the dark ground, and the chart line on the gold
GOLD = (0xF0, 0xB4, 0x29)  # the bubble. It is a gold narrator; the colour is the point.

# Everything below is in 0..1 of the icon's width, so one set of numbers
# describes every size.
CORNER = 0.20  # of the dark square
BUBBLE = (0.13, 0.15, 0.87, 0.63)  # x0, y0, x1, y1
BUBBLE_CORNER = 0.15
TAIL = ((0.30, 0.60), (0.50, 0.60), (0.29, 0.87))
#: Three rising bars rather than a line. A stroked polyline is the obvious
#: drawing and it is the wrong one at 16px: a shallow segment smears across
#: six pixels while a steep one covers two, so the same stroke reads as a
#: wedge at one end and a hairline at the other. Axis-aligned rectangles
#: land on the pixel grid at every size and cannot do that.
BARS = (
    (0.26, 0.44, 0.39, 0.53),
    (0.44, 0.35, 0.57, 0.53),
    (0.62, 0.25, 0.75, 0.53),
)


def rounded_rect(x: float, y: float, box: tuple, radius: float) -> bool:
    """Inside a rectangle whose corners are quarter-circles."""
    x0, y0, x1, y1 = box
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    # Only the four corner squares need the circle test; the cross in the
    # middle is inside by definition.
    cx = min(max(x, x0 + radius), x1 - radius)
    cy = min(max(y, y0 + radius), y1 - radius)
    return math.hypot(x - cx, y - cy) <= radius


def in_triangle(x: float, y: float, tri: tuple) -> bool:
    (ax, ay), (bx, by), (cx, cy) = tri

    def side(px, py, qx, qy):
        return (qx - px) * (y - py) - (qy - py) * (x - px)

    d1, d2, d3 = side(ax, ay, bx, by), side(bx, by, cx, cy), side(cx, cy, ax, ay)
    return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))


def in_any_bar(x: float, y: float, bars: tuple) -> bool:
    return any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in bars)


def sample(x: float, y: float) -> tuple[int, int, int, int] | None:
    """The colour at a point in 0..1 space, or None for transparent."""
    if not rounded_rect(x, y, (0.0, 0.0, 1.0, 1.0), CORNER):
        return None
    on_bubble = rounded_rect(x, y, BUBBLE, BUBBLE_CORNER) or in_triangle(x, y, TAIL)
    if on_bubble:
        if in_any_bar(x, y, BARS):
            return (*INK, 255)
        return (*GOLD, 255)
    return (*INK, 255)


def render(size: int) -> bytearray:
    """One layer, as RGBA rows top to bottom.

    Averaging the sub-samples in straight (non-premultiplied) space is fine
    here because the only transparency is the outer corners, where the
    neighbouring colour is a single flat ink rather than a gradient.
    """
    step = 1.0 / (size * SUPERSAMPLE)
    pixels = bytearray(size * size * 4)
    for py in range(size):
        for px in range(size):
            r = g = b = a = 0
            for sy in range(SUPERSAMPLE):
                y = (py * SUPERSAMPLE + sy + 0.5) * step
                for sx in range(SUPERSAMPLE):
                    x = (px * SUPERSAMPLE + sx + 0.5) * step
                    got = sample(x, y)
                    if got is not None:
                        r += got[0]
                        g += got[1]
                        b += got[2]
                        a += 255
            hits = a // 255
            at = (py * size + px) * 4
            if hits:
                pixels[at] = r // hits
                pixels[at + 1] = g // hits
                pixels[at + 2] = b // hits
            pixels[at + 3] = a // (SUPERSAMPLE * SUPERSAMPLE)
    return pixels


def as_png(size: int, rgba: bytearray) -> bytes:
    """A PNG, for the 256 entry. Windows Vista and later read these, and a
    256x256 uncompressed DIB would be 256 KB on its own."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    raw = bytearray()
    for row in range(size):
        raw.append(0)  # filter type 0; the shapes are flat and filtering buys nothing
        raw += rgba[row * size * 4 : (row + 1) * size * 4]
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def as_dib(size: int, rgba: bytearray) -> bytes:
    """A 32-bit BMP in the shape an ICO wants: a doubled height in the header,
    rows bottom-up, BGRA, and an AND mask after the colour data.

    The mask is all zeros because the alpha channel already carries the
    transparency, but it cannot be omitted -- some Windows shell paths still
    read it, and an ICO without one renders as a black square in exactly the
    places nobody tests.
    """
    header = struct.pack(
        "<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, size * size * 4, 0, 0, 0, 0
    )
    body = bytearray()
    for row in range(size - 1, -1, -1):
        for col in range(size):
            at = (row * size + col) * 4
            body += bytes((rgba[at + 2], rgba[at + 1], rgba[at], rgba[at + 3]))
    mask_row = ((size + 31) // 32) * 4
    return header + bytes(body) + bytes(mask_row * size)


def build(path: Path) -> None:
    entries: list[tuple[int, bytes]] = []
    for size in SIZES:
        rgba = render(size)
        # PNG above 64: an uncompressed 256x256 DIB is a quarter of a megabyte
        # and this whole icon should not be.
        entries.append((size, as_png(size, rgba) if size > 64 else as_dib(size, rgba)))
        print(f"  {size:>3}x{size:<3} {len(entries[-1][1]):>7,} bytes")

    offset = 6 + 16 * len(entries)
    directory = bytearray()
    for size, payload in entries:
        directory += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,  # 0 means 256 in an ICO directory
            0 if size >= 256 else size,
            0,
            0,
            1,
            32,
            len(payload),
            offset,
        )
        offset += len(payload)

    path.write_bytes(
        struct.pack("<HHH", 0, 1, len(entries))
        + bytes(directory)
        + b"".join(payload for _size, payload in entries)
    )
    print(f"\nwrote {path}  ({path.stat().st_size:,} bytes, {len(entries)} sizes)")


if __name__ == "__main__":
    build(Path(__file__).with_name("GamechangerTalker.ico"))
