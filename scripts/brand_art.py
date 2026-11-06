#!/usr/bin/env python3
"""Branding art (design §8 P17): the product's neutral server-list icon and pack art, from pixel grids.

    brand_art.py build   # writes the files below
    brand_art.py check   # rebuilds in memory; the committed files and the group_vars pin must match

Outputs (all committed):
    plugin/mc-brand/dist/server-icon.png     64x64 server-list icon (the drink grid at 3x on a slate tile);
                                             also the pack's pack.png unless the config repo has its own icon
    plugin/mc-brand/art/drink.png            16x16 item texture of the drink (branding.drink.texture: null)
    plugin/mc-brand/art/glyph.png            8x8 {glyph} icon (branding.resource_pack.glyph: null)
    docs/images/brand-server-icon.png        the icon enlarged 8x (512x512, nearest neighbour; for review)
    docs/images/brand-drink.png              the item texture enlarged 8x (128x128)

v1.2: the resource pack itself is no longer committed: `mcctl render` builds it from the config repo's art or
these files (mcctl/brandpack.py, byte-reproducible; its sha1 is rendered as mc_brand_pack_sha1). A deployment
with its own look commits its own PNGs to its config repo; nothing here is any deployment's art.

Stdlib only. The icon and the art are byte-reproducible: PNGs use stored (uncompressed) deflate blocks
written here (zlib's own stored-block sizes differ between versions). The previews are compressed (they are
only looked at), so `check` compares their pixels, not their bytes. ansible/group_vars/all/main.yml pins
mc_brand_icon_sha256; `check` (ci.yml job plugin, tests/test_brand.py) fails when a grid changed without a
rebuild.
"""

from __future__ import annotations

import argparse
import hashlib
import struct
import sys
import zlib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mcctl import brandpack, schema  # noqa: E402

ICON = brandpack.NEUTRAL_ART["icon"]
TEXTURE = brandpack.NEUTRAL_ART["texture"]
GLYPH = brandpack.NEUTRAL_ART["glyph"]
LEGACY_PACK_DIR = REPO_ROOT / "watchdog" / "panel" / "pack"  # v1.1's committed pack: must stay gone
PREVIEW_ICON = REPO_ROOT / "docs" / "images" / "brand-server-icon.png"
PREVIEW_DRINK = REPO_ROOT / "docs" / "images" / "brand-drink.png"
GROUP_VARS = REPO_ROOT / "ansible" / "group_vars" / "all" / "main.yml"

RGBA = tuple[int, int, int, int]


def hexc(color: str, alpha: int = 255) -> RGBA:
    return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16), alpha)


# Neutral: a clear flask of blue (the default drink colour #3A6EA5 and accent #55AAFF), no gold anywhere.
PALETTE: dict[str, RGBA] = {
    ".": (0, 0, 0, 0),
    "o": hexc("#C8D0D8"),  # glass edge (blue-grey: the default subtle colour)
    "h": hexc("#FFFFFF"),  # glint
    "g": hexc("#EEF6F9", 110),  # glass, see-through
    "r": hexc("#55AAFF"),  # liquid surface
    "w": hexc("#3A6EA5"),  # liquid
    "W": hexc("#24507A"),  # liquid, shade
    "c": hexc("#A57B52"),  # cork
    "C": hexc("#7E5A3A"),  # cork, shade
}

# 16x16 item texture (art/drink.png); the server icon is this flask at 3x.
DRINK_GRID = [
    "......cccc......",
    "......cCCc......",
    ".....oooooo.....",
    "......ohgo......",
    "......ohgo......",
    ".....ohgggo.....",
    "....ohgggggo....",
    "...orrrrrrrro...",
    "...ohwwwwwwWo...",
    "..ohwwwwwwwwWo..",
    "..owwwwwwwwwWo..",
    "..owwwwwwwwwWo..",
    "..owwwwwwwwWWo..",
    "...owwwwwwWWo...",
    "....oWWWWWWo....",
    ".....oooooo.....",
]

# The 8x8 chat/HUD glyph (font mcbrand:icons, U+E001 = Texts.GLYPH_CHAR): the same flask reduced. Minecraft's
# fonts have no emoji.
GLYPH_GRID = [
    "...cc...",
    "..oooo..",
    "...hg...",
    "..ohgo..",
    ".orrrro.",
    ".ohwwWo.",
    ".owwWWo.",
    "..oooo..",
]

# Server icon (64x64): slate tile, 2-px border (the default secondary colour), a subtle glow, the flask x3 at
# (8, 8).
ICON_BG, ICON_BORDER, ICON_GLOW = "#1E2630", "#2E6FB0", "#27313D"
ICON_SCALE, ICON_OFFSET = 3, (8, 8)


def pixels(grid: list[str], scale: int) -> list[list[RGBA]]:
    size = len(grid)
    if any(len(row) != size for row in grid):
        raise ValueError("grid is not square")
    return [[PALETTE[ch] for ch in row for _ in range(scale)] for row in grid for _ in range(scale)]


def enlarge(px: list[list[RGBA]], k: int) -> list[list[RGBA]]:
    return [[p for p in row for _ in range(k)] for row in px for _ in range(k)]


def icon_pixels() -> list[list[RGBA]]:
    """The 64x64 server icon: opaque (the glass's see-through pixels are blended over the tile)."""
    t = [[hexc(ICON_BG) for _ in range(64)] for _ in range(64)]
    for i in range(64):
        for j in (0, 1, 62, 63):
            t[i][j] = t[j][i] = hexc(ICON_BORDER)
    for y in range(14, 54):
        for x in range(12, 52):
            if (x - 32) ** 2 / 19**2 + (y - 34) ** 2 / 19**2 <= 1:
                t[y][x] = hexc(ICON_GLOW)
    ox, oy = ICON_OFFSET
    for y, row in enumerate(pixels(DRINK_GRID, ICON_SCALE)):
        for x, (r, g, b, a) in enumerate(row):
            if a == 0:
                continue
            br, bg, bb, _ = t[oy + y][ox + x]
            f = a / 255
            t[oy + y][ox + x] = (
                round(r * f + br * (1 - f)),
                round(g * f + bg * (1 - f)),
                round(b * f + bb * (1 - f)),
                255,
            )
    return t


def _stored_zlib(data: bytes) -> bytes:
    """A zlib stream of stored deflate blocks (deterministic: no compressor involved)."""
    out = bytearray(b"\x78\x01")
    view = memoryview(data)
    for i in range(0, max(len(data), 1), 65535):
        chunk = view[i : i + 65535]
        final = 1 if i + 65535 >= len(data) else 0
        out += struct.pack("<BHH", final, len(chunk), 0xFFFF ^ len(chunk)) + chunk
    return bytes(out + struct.pack(">I", zlib.adler32(data)))


def png(px: list[list[RGBA]], *, compress: bool = False) -> bytes:
    """An 8-bit RGBA PNG (filter 0 on every row)."""
    h, w = len(px), len(px[0])
    raw = b"".join(b"\x00" + bytes(c for p in row for c in p) for row in px)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    idat = zlib.compress(raw, 9) if compress else _stored_zlib(raw)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def decode_png(data: bytes) -> tuple[int, int, bytes]:
    """(width, height, raw RGBA rows with filter bytes) of a PNG written by png() (filter 0 only)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, w, h = 8, b"", 0, 0
    while pos < len(data):
        (n,) = struct.unpack(">I", data[pos : pos + 4])
        kind, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + n]
        if kind == b"IHDR":
            w, h = struct.unpack(">II", body[:8])
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    return w, h, zlib.decompress(idat)


def outputs() -> tuple[dict[Path, bytes], dict[Path, bytes]]:
    """(byte-exact files, previews compared by pixels)."""
    exact = {
        ICON: png(icon_pixels()),
        TEXTURE: png(pixels(DRINK_GRID, 1)),
        GLYPH: png(pixels(GLYPH_GRID, 1)),
    }
    previews = {
        PREVIEW_ICON: png(enlarge(icon_pixels(), 8), compress=True),
        PREVIEW_DRINK: png(pixels(DRINK_GRID, 8), compress=True),
    }
    return exact, previews


def pins() -> dict[str, str]:
    exact, _ = outputs()
    return {"mc_brand_icon_sha256": hashlib.sha256(exact[ICON]).hexdigest()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["build", "check"])
    args = ap.parse_args(argv)
    exact, previews = outputs()
    gv = yaml.safe_load(GROUP_VARS.read_text())
    problems = [f"{k} in ansible/group_vars/all/main.yml != {v}" for k, v in pins().items() if gv.get(k) != v]
    if "mc_brand_pack_sha1" in gv:
        problems.append("mc_brand_pack_sha1 in ansible/group_vars/all/main.yml: v1.2 renders it (remove it)")
    if args.command == "build":
        for path, data in {**exact, **previews}.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            print(f"{path.relative_to(REPO_ROOT)}: {len(data)} bytes")
        for p in problems:
            print("update " + p.replace(" != ", ": "))
        return 0
    for path, data in exact.items():
        if not path.is_file() or path.read_bytes() != data:
            problems.append(f"{path.relative_to(REPO_ROOT)} differs from the grids: run brand_art.py build")
    if LEGACY_PACK_DIR.exists():
        problems.append("watchdog/panel/pack/ exists: v1.2 renders the pack (mcctl render), remove it")
    for path, data in previews.items():
        if not path.is_file() or decode_png(path.read_bytes()) != decode_png(data):
            problems.append(f"{path.relative_to(REPO_ROOT)} differs from the grids: run brand_art.py build")
    # the neutral pack builds (the product's art fits mcctl render's checks)
    for name, (path, size) in {"icon": (ICON, 64), "texture": (TEXTURE, 16), "glyph": (GLYPH, 8)}.items():
        if decode_png(exact[path])[:2] != (size, size):
            problems.append(f"{name} is not {size}x{size}")
    for p in problems:
        print(p, file=sys.stderr)
    if not problems:
        neutral = brandpack.sha1(brandpack.pack_zip({}, schema.BrandingResourcePack().description))
        print("branding art: reproducible, " + ", ".join(f"{k} {v}" for k, v in pins().items())
              + f"; neutral pack sha1 {neutral}")  # fmt: skip
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
