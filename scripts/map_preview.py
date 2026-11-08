#!/usr/bin/env python3
"""Show a world map tile as a PNG, for humans (v1.4; the tile format is docs/reference.md "World map").

    map_preview.py TILE.swt OUT.png [--colors FILE] [--zoom N]
    map_preview.py --world tests/fixtures/world-26.3 [--dimension NS/DIM] [--scale 0] [--tile 0,0] OUT.png

The first form reads a tile file (the Rust renderer's or tests/map_reference.py's); the second renders the
tile with the Python reference renderer first. The PNG is indexed (PLTE + tRNS, packed ids 0..3 transparent),
written with zlib from the stdlib: the 248 map colours, shaded as vanilla's MapColor does.
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
import map_reference as ref  # noqa: E402 - the reference renderer lives with its tests


def png(ids: bytes, width: int, colours: list[int], zoom: int = 1) -> bytes:
    """An 8-bit indexed PNG of packed colour ids, `zoom` x nearest-neighbour."""
    height = len(ids) // width
    plte = b"".join(bytes(ref.rgba(i, colours)[:3]) for i in range(len(colours) * 4))
    trns = bytes(ref.rgba(i, colours)[3] for i in range(len(colours) * 4))
    rows = bytearray()
    for z in range(height):
        row = b"".join(bytes([v]) * zoom for v in ids[z * width : (z + 1) * width])
        rows += (b"\0" + row) * zoom

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", width * zoom, height * zoom, 8, 3, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"PLTE", plte)
        + chunk(b"tRNS", trns)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+", type=Path, help="[TILE.swt] OUT.png")
    ap.add_argument(
        "--world", type=Path, help="render from this world (e.g. the fixture) instead of a tile file"
    )
    ap.add_argument("--dimension", default="minecraft/overworld")
    ap.add_argument("--scale", type=int, default=0, choices=list(ref.SCALES))
    ap.add_argument("--tile", default="0,0", help="tx,tz")
    ap.add_argument("--version", default="26.3", help="the palette's minecraft_version (with --world)")
    ap.add_argument("--colors", type=Path, help="map-colors.tsv (default: the pinned version's)")
    ap.add_argument("--zoom", type=int, default=1)
    args = ap.parse_args(argv)
    colours_file = args.colors or REPO / "server-config" / "keys" / args.version / "map-colors.tsv"
    colours = [
        int(line.split("\t")[1], 16) for line in colours_file.read_text().splitlines() if line[:1] != "#"
    ]
    if args.world:
        if len(args.paths) != 1:
            ap.error("--world takes one path: OUT.png")
        tx, tz = (int(v) for v in args.tile.split(","))
        ns, dim = args.dimension.split("/")
        r = ref.render_dimension(
            args.world / "dimensions" / ns / dim / "region", ref.palette_for(args.version)
        )
        ids = r.tiles.get((args.scale, tx, tz))
        if ids is None:
            print(f"no data for {args.dimension} scale {args.scale} tile {tx},{tz}", file=sys.stderr)
            return 1
        out = args.paths[0]
    else:
        if len(args.paths) != 2:
            ap.error("give TILE.swt OUT.png")
        _, ids = ref.parse_tile(args.paths[0].read_bytes())
        out = args.paths[1]
    out.write_bytes(png(bytes(ids), ref.TILE_PX, colours, args.zoom))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
