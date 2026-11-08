#!/usr/bin/env python3
"""Reference renderer for the world map tiles (v1.4; the spec is docs/reference.md "World map").

The Rust renderer (`smallworld-map`) must produce tiles that are byte-for-byte equal to this module's, as
mc-brand's Wayfinding is checked against Python in tests/test_wayfinding.py. It is deliberately simple and slow:
a straight transcription of vanilla's `MapItem.update` (Purpur/Paper 26.3, read from the pinned jar's bytecode)
over a whole dimension held in memory, with no incremental state. Stdlib only.

    python tests/map_reference.py render WORLD OUT [--dimension minecraft/overworld ...] [--rendered-at N]
    python tests/map_reference.py golden    # rewrites tests/fixtures/world-26.3/golden.json from the fixture

Pipeline, as the spec numbers it:

1. Region files (`dimensions/<ns>/<dim>/region/r.X.Z.mca`): 8 KiB header, zlib/gzip/none payloads, `.mcc`
   external chunks (`read_region`).
2. Chunk NBT at the palette's DataVersion, `Status` `minecraft:full` only (`parse_nbt`, `chunk_columns`).
3. Block states: 26.x palette entries are a bare id (the default state; `{"": id}` inside a mixed list) or
   `{id, properties?}` with missing properties taken from the default state (`Palette.lookup`), looked up in
   server-config/keys/<version>/map-palette.tsv (scripts/map_palette.py).
4. Per block column: vanilla's walk from WORLD_SURFACE down through NONE-coloured blocks, water depth, the
   fluid surface rule (`walk_column`).
5. Per scale s (0..4, i = 2**s blocks per pixel): the i*i footprint's most frequent colour (first seen wins a
   tie, x outer, z inner), mean height, mean depth; brightness from the northern pixel at the same scale and the
   parity (gx + gz) & 1 (`render_columns`, `brightness`).
6. Tiles of 512 x 512 pixels per scale, 32-byte header + one packed colour id per pixel (`tile_bytes`).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
import struct
import sys
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "world-26.3"
GOLDEN = FIXTURE / "golden.json"

# --- tile format (docs/reference.md "World map": "Tile file") -----------------------------------------------
MAGIC = b"SWMAPT\x00\x01"
TILE_PX = 512
HEADER = struct.Struct("<8sBBHiiIq")  # magic, scale, flags, reserved, tx, tz, max DataVersion, rendered_at
assert HEADER.size == 32
SCALES = range(5)
FLAG_PROVISIONAL = 1

# --- vanilla constants (MapItem / MapColor bytecode, 26.3) --------------------------------------------------
WATER = 12  # MapColor.WATER.id: the only colour shaded by depth
LOW, NORMAL, HIGH = 0, 1, 2  # MapColor.Brightness ids (LOWEST = 3 never appears on maps)
MULTIPLIER = (180, 220, 255, 135)
FULL = "minecraft:full"
PROVISIONAL = frozenset(
    {"minecraft:features", "minecraft:initialize_light", "minecraft:light", "minecraft:spawn"}
)
DEFAULT_DIMENSIONS = (
    "minecraft/overworld",
    "minecraft/the_end",
)  # the Nether is never rendered: maps are noise
FIXTURE_DIMENSIONS = (*DEFAULT_DIMENSIONS, "smallworld/synthetic")  # + scripts/map_fixture.py's edge cases

# =====================================================================================================================
# NBT (Java edition, big-endian, uncompressed)
# =====================================================================================================================
END, BYTE, SHORT, INT, LONG, FLOAT, DOUBLE, BYTE_ARRAY, STRING, LIST, COMPOUND, INT_ARRAY, LONG_ARRAY = range(
    13
)
MAX_DEPTH = 512  # vanilla's NbtAccounter depth limit


class NbtError(ValueError):
    pass


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data, self.pos = data, 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise NbtError(f"truncated NBT at {self.pos} (+{n})")
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def unpack(self, fmt: str) -> Any:
        s = struct.Struct(">" + fmt)
        return s.unpack(self.take(s.size))[0]

    def string(self) -> str:
        return self.take(self.unpack("H")).decode(
            "utf-8", "surrogateescape"
        )  # modified UTF-8 is close enough

    def payload(self, tag: int, depth: int) -> Any:
        if depth > MAX_DEPTH:
            raise NbtError("NBT nested too deep")
        if tag == BYTE:
            return self.unpack("b")
        if tag == SHORT:
            return self.unpack("h")
        if tag == INT:
            return self.unpack("i")
        if tag == LONG:
            return self.unpack("q")
        if tag == FLOAT:
            return self.unpack("f")
        if tag == DOUBLE:
            return self.unpack("d")
        if tag == BYTE_ARRAY:
            return self.take(self._length())
        if tag == STRING:
            return self.string()
        if tag == LIST:
            elem, n = self.unpack("B"), self._length()
            if n and elem == END:
                raise NbtError("non-empty list of TAG_End")
            return [self.payload(elem, depth + 1) for _ in range(n)]
        if tag == COMPOUND:
            out: dict[str, Any] = {}
            while (t := self.unpack("B")) != END:
                name = self.string()
                out[name] = self.payload(t, depth + 1)
            return out
        if tag == INT_ARRAY:
            n = self._length()
            return list(struct.unpack(f">{n}i", self.take(4 * n)))
        if tag == LONG_ARRAY:
            n = self._length()
            return list(struct.unpack(f">{n}q", self.take(8 * n)))
        raise NbtError(f"unknown tag {tag}")

    def _length(self) -> int:
        n = self.unpack("i")
        if n < 0:
            raise NbtError("negative length")
        return int(n)


def parse_nbt(data: bytes) -> tuple[str, Any]:
    """(root name, root value) of one uncompressed NBT document. Lists stay Python lists; 26.x mixed lists
    keep their wrapper compounds ({"": value}): `unwrap` removes them where it matters (block palettes)."""
    r = _Reader(data)
    tag = r.unpack("B")
    if tag == END:
        return "", None
    name = r.string()
    return name, r.payload(tag, 0)


def unwrap(value: Any) -> Any:
    """26.x heterogeneous lists store each element as a compound; a non-compound element is wrapped as the
    compound's single empty-named entry (ListTag.wrapElement / tryUnwrap)."""
    if isinstance(value, dict) and len(value) == 1 and "" in value:
        return value[""]
    return value


# =====================================================================================================================
# Region files
# =====================================================================================================================
SECTOR = 4096


@dataclass
class RawChunk:
    index: int  # (x & 31) + (z & 31) * 32
    timestamp: int
    nbt: dict[str, Any] | None
    error: str | None = None


def decompress(compression: int, data: bytes) -> bytes:
    if compression == 1:
        return gzip.decompress(data)
    if compression == 2:
        return zlib.decompress(data)
    if compression == 3:
        return data
    raise NbtError(f"unsupported compression {compression}")  # 4 = LZ4Block, 127 = custom: counted, skipped


def read_region(
    data: bytes, rx: int, rz: int, external: Callable[[int, int], bytes | None] = lambda cx, cz: None
) -> Iterator[RawChunk]:
    """Every present chunk of a region file, in index order. A chunk that can't be read comes back with
    `error` set (the renderer skips it); `external(cx, cz)` returns the c.<cx>.<cz>.mcc file's bytes."""
    if len(data) < 2 * SECTOR:
        return
    for idx in range(1024):
        loc = struct.unpack_from(">I", data, idx * 4)[0]
        ts = struct.unpack_from(">I", data, SECTOR + idx * 4)[0]
        offset, count = loc >> 8, loc & 0xFF
        if offset == 0:
            continue
        cx, cz = rx * 32 + (idx & 31), rz * 32 + (idx >> 5)
        try:
            if offset < 2 or count == 0:
                raise NbtError("chunk sectors out of bounds")
            p = offset * SECTOR
            length = struct.unpack_from(">I", data, p)[0]
            if length < 1 or p + 4 + length > len(data) or length > count * SECTOR:
                raise NbtError("chunk length out of bounds")
            version = data[p + 4]
            if version & 0x80:
                if length != 1:
                    raise NbtError("both internal and external streams")
                ext = external(cx, cz)
                if ext is None:
                    raise NbtError("external chunk file missing")
                raw = decompress(version & 0x7F, ext)
            else:
                raw = decompress(version, data[p + 5 : p + 4 + length])
            _, nbt = parse_nbt(raw)
            if not isinstance(nbt, dict):
                raise NbtError("chunk root is not a compound")
            if (nbt.get("xPos"), nbt.get("zPos")) != (cx, cz):
                raise NbtError("chunk position does not match its slot")
            yield RawChunk(idx, ts, nbt)
        except (NbtError, OSError, zlib.error, EOFError, struct.error) as e:
            yield RawChunk(idx, ts, None, str(e))


# =====================================================================================================================
# The palette (server-config/keys/<version>/map-palette.tsv)
# =====================================================================================================================
@dataclass(frozen=True)
class BlockInfo:
    colour: int  # getMapColor id; 0 = NONE (the walk looks through it)
    fluid: bool
    surface: int  # the colour recorded when the walk stops here (fluid surface rule applied)


AIR = BlockInfo(0, False, 0)


@dataclass
class Palette:
    data_version: int
    defaults: dict[str, tuple[dict[str, str], BlockInfo]]
    states: dict[tuple[str, str], BlockInfo]
    colours: list[int] = field(default_factory=list)  # id -> 0xRRGGBB
    unknown: dict[str, int] = field(default_factory=dict)  # state key -> palette entries seen

    @classmethod
    def load(cls, palette: Path, colours: Path | None = None) -> Palette:
        defaults: dict[str, tuple[dict[str, str], BlockInfo]] = {}
        states: dict[tuple[str, str], BlockInfo] = {}
        meta: dict[str, str] = {}
        for line in palette.read_text().splitlines():
            f = line.split("\t")
            if f[0] == "M":
                meta[f[1]] = f[2]
            elif f[0] in ("D", "S"):
                info = BlockInfo(int(f[3]), f[4] == "1", int(f[5]))
                if f[0] == "D":
                    props = dict(kv.split("=", 1) for kv in f[2].split(",")) if f[2] != "-" else {}
                    defaults[f[1]] = (props, info)
                else:
                    states[(f[1], f[2])] = info
        if meta.get("format") != "1":
            raise ValueError(f"{palette}: unsupported palette format {meta.get('format')}")
        colours = colours or palette.with_name("map-colors.tsv")
        rgb = [int(h, 16) for line in colours.read_text().splitlines() if not line.startswith("#")
               for _, h in [line.split("\t")]]  # fmt: skip
        return cls(int(meta["data_version"]), defaults, states, rgb)

    def key(self, entry: Any) -> tuple[str, str]:
        """(block id, sorted "k=v,..." or "-") of a 26.x palette entry, missing properties defaulted."""
        entry = unwrap(entry)
        if isinstance(entry, str):
            name, given = entry, {}
        elif isinstance(entry, dict) and isinstance(entry.get("id"), str):
            name, given = entry["id"], entry.get("properties") or {}
        else:
            raise NbtError(f"unsupported palette entry {entry!r}")
        full = dict(self.defaults[name][0]) if name in self.defaults else {}
        full.update({str(k): str(v) for k, v in given.items()})
        return name, ",".join(f"{k}={full[k]}" for k in sorted(full)) or "-"

    def lookup(self, entry: Any) -> BlockInfo:
        """The map's view of a palette entry. Unknown blocks or properties count as NONE (see-through) and are
        tallied in `unknown`."""
        name, props = self.key(entry)
        if name not in self.defaults or (props != "-" and not _valid(props, self.defaults[name][0])):
            k = f"{name}[{props}]"
            self.unknown[k] = self.unknown.get(k, 0) + 1
            return AIR
        return self.states.get((name, props), self.defaults[name][1])

    @property
    def bedrock(self) -> int:
        """Vanilla draws a column whose surface is at most minY + 1 (also: empty) as Blocks.BEDROCK."""
        return self.defaults["minecraft:bedrock"][1].colour


def _valid(props: str, default: dict[str, str]) -> bool:
    return {kv.split("=", 1)[0] for kv in props.split(",")} == set(default)


def palette_for(version: str) -> Palette:
    return Palette.load(REPO / "server-config" / "keys" / version / "map-palette.tsv")


# =====================================================================================================================
# Chunk decoding
# =====================================================================================================================
def ceillog2(n: int) -> int:
    return (n - 1).bit_length() if n > 1 else 0


def unpack_bits(longs: list[int], bits: int, count: int) -> list[int]:
    """SimpleBitStorage: floor(64 / bits) values per long, LSB first; values never span two longs."""
    per = 64 // bits
    if len(longs) != (count + per - 1) // per:
        raise NbtError(f"{len(longs)} longs for {count} values of {bits} bits")
    mask = (1 << bits) - 1
    return [((longs[i // per] & 0xFFFFFFFFFFFFFFFF) >> ((i % per) * bits)) & mask for i in range(count)]


def heightmap_bits(longs: list[int], height: int) -> int:
    """Bits per heightmap entry: ceillog2(height + 1); the long count settles it except 11 vs 12 bits (both 52
    longs), where the dimension's height decides."""
    fits = [b for b in range(1, 33) if len(longs) == (256 + 64 // b - 1) // (64 // b)]
    if len(fits) == 1:
        return fits[0]
    want = ceillog2(height + 1)
    if want in fits:
        return want
    raise NbtError(f"heightmap of {len(longs)} longs")


@dataclass(frozen=True)
class Column:
    colour: int  # MapColor id the map records for this block column
    y: int  # vanilla's `w` after the walk (feeds the height shading)
    depth: int  # water depth counted below the surface


Section = tuple[list[BlockInfo], list[int] | None]


def decode_sections(chunk: dict[str, Any], palette: Palette) -> dict[int, Section]:
    """Section Y -> (palette as BlockInfo, 4096 indices or None for a single-entry palette). Sections without
    block_states (26.x stores light-only sections above and below the world) are absent: air."""
    out: dict[int, Section] = {}
    for sec in chunk.get("sections") or []:
        bs = sec.get("block_states")
        if not isinstance(bs, dict):
            continue
        pal = [palette.lookup(e) for e in bs.get("palette") or []]
        if not pal:
            raise NbtError("empty block palette")
        if len(pal) == 1:
            out[int(sec["Y"])] = (pal, None)
            continue
        idx = unpack_bits(bs["data"], max(4, ceillog2(len(pal))), 4096)
        if max(idx) >= len(pal):
            raise NbtError("block index outside the palette")
        out[int(sec["Y"])] = (pal, idx)
    return out


def walk_column(at: Callable[[int], BlockInfo], surface_y: int, min_y: int, bedrock: int) -> Column:
    """MapItem.update for one block column. `surface_y` = WORLD_SURFACE height + 1 (the first free y)."""
    w = surface_y
    if w <= min_y + 1:
        return Column(bedrock, w, 0)
    while True:  # do { --w } while (getMapColor == NONE && w > minY)
        w -= 1
        state = at(w)
        if not (state.colour == 0 and w > min_y):
            break
    depth = 0
    colour = state.colour
    if w > min_y and state.fluid:
        k = w - 1
        while True:  # do { get(k--); ++depth } while (k > minY && fluid)
            below = at(k)
            k -= 1
            depth += 1
            if not (k > min_y and below.fluid):
                break
        colour = state.surface  # getCorrectStateForFluidBlock
    return Column(colour, w, depth)


def chunk_columns(chunk: dict[str, Any], palette: Palette) -> list[Column]:
    """The 256 columns of a chunk, index x + z * 16."""
    min_y = int(chunk["yPos"]) * 16
    sections = decode_sections(chunk, palette)
    top = max(sections, default=int(chunk["yPos"]))
    height = (top - int(chunk["yPos"]) + 1) * 16
    longs = chunk["Heightmaps"]["WORLD_SURFACE"]
    heights = unpack_bits(longs, heightmap_bits(longs, height), 256)
    out = []
    for z in range(16):
        for x in range(16):

            def at(y: int, x: int = x, z: int = z) -> BlockInfo:
                sec = sections.get(y >> 4)
                if sec is None:
                    return AIR
                pal, idx = sec
                return pal[0] if idx is None else pal[idx[((y & 15) * 16 + z) * 16 + x]]

            out.append(walk_column(at, heights[x + z * 16] + min_y, min_y, palette.bedrock))
    return out


# =====================================================================================================================
# Pixels and tiles
# =====================================================================================================================
def brightness(colour: int, d1: float, d0: float, depth: int, scale: int, parity: int) -> int:
    """MapItem.update's shade: water by mean depth, the rest by the height step from the northern pixel."""
    if colour == WATER:
        d2 = depth * 0.1 + parity * 0.2
        return HIGH if d2 < 0.5 else LOW if d2 > 0.9 else NORMAL
    d3 = (d1 - d0) * 4.0 / ((1 << scale) + 4) + (parity - 0.5) * 0.4
    return HIGH if d3 > 0.6 else LOW if d3 < -0.6 else NORMAL


@dataclass
class Pixel:
    colour: int
    d1: float  # sum of y / (i*i) over the footprint (exact: i*i is a power of two)
    depth: int  # sum of depths // (i*i)


def aggregate(columns: list[Column], scale: int) -> Pixel:
    """One pixel from its footprint's columns, in vanilla's order (x outer, z inner)."""
    n = 1 << (2 * scale)
    counts: dict[int, int] = {}
    d1, depth = 0.0, 0
    for c in columns:
        d1 += c.y / n
        depth += c.depth
        counts[c.colour] = counts.get(c.colour, 0) + 1
    best = max(counts.values())
    colour = next(k for k, v in counts.items() if v == best)  # LinkedHashMultiset: first inserted wins a tie
    return Pixel(colour, d1, depth // n)


def pixels(chunks: dict[tuple[int, int], list[Column]], scale: int) -> dict[tuple[int, int], Pixel]:
    """Every pixel (gx, gz) = (floor(x / i), floor(z / i)) of the given chunks at `scale` (i <= 16, so a
    footprint never leaves its chunk)."""
    i = 1 << scale
    out: dict[tuple[int, int], Pixel] = {}
    for (cx, cz), cols in chunks.items():
        for pz in range(16 // i):
            for px in range(16 // i):
                foot = [cols[(px * i + dx) + (pz * i + dz) * 16] for dx in range(i) for dz in range(i)]
                out[((cx * 16 >> scale) + px, (cz * 16 >> scale) + pz)] = aggregate(foot, scale)
    return out


def packed(pix: dict[tuple[int, int], Pixel], gx: int, gz: int, scale: int) -> int:
    """The packed colour id of pixel (gx, gz) as a vanilla map with an even-pixel centre draws it. NONE and
    missing pixels are 0. A missing northern pixel (chunk not rendered) shades as flat ground."""
    p = pix[(gx, gz)]
    if p.colour == 0:
        return 0
    north = pix.get((gx, gz - 1))
    d0 = north.d1 if north is not None else p.d1
    return p.colour * 4 + brightness(p.colour, p.d1, d0, p.depth, scale, (gx + gz) & 1)


@dataclass
class Rendered:
    tiles: dict[tuple[int, int, int], bytearray]  # (scale, tx, tz) -> 512*512 packed ids
    data_version: dict[tuple[int, int, int], int]
    chunks: int = 0
    skipped: dict[str, int] = field(default_factory=dict)


def render_columns(
    chunks: dict[tuple[int, int], list[Column]], versions: dict[tuple[int, int], int]
) -> Rendered:
    out = Rendered({}, {}, len(chunks))
    for scale in SCALES:
        pix = pixels(chunks, scale)
        for gx, gz in pix:
            v = packed(pix, gx, gz, scale)
            key = (scale, gx // TILE_PX, gz // TILE_PX)
            tile = out.tiles.setdefault(key, bytearray(TILE_PX * TILE_PX))
            tile[(gz % TILE_PX) * TILE_PX + gx % TILE_PX] = v
            cv = versions[((gx << scale) >> 4, (gz << scale) >> 4)]
            out.data_version[key] = max(out.data_version.get(key, 0), cv)
    for key in [k for k, t in out.tiles.items() if not any(t)]:  # all-zero tiles are not written
        del out.tiles[key], out.data_version[key]
    return out


def load_dimension(
    region_dir: Path, palette: Palette, statuses: frozenset[str] = frozenset({FULL})
) -> tuple[dict[tuple[int, int], list[Column]], dict[tuple[int, int], int], dict[str, int]]:
    """(chunk -> columns, chunk -> DataVersion, skip reasons) for every renderable chunk of a dimension."""
    chunks: dict[tuple[int, int], list[Column]] = {}
    versions: dict[tuple[int, int], int] = {}
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for path in sorted(region_dir.glob("r.*.*.mca")):
        m = re.fullmatch(r"r\.(-?\d+)\.(-?\d+)\.mca", path.name)
        if not m:
            continue
        rx, rz = int(m.group(1)), int(m.group(2))

        def external(cx: int, cz: int) -> bytes | None:
            p = region_dir / f"c.{cx}.{cz}.mcc"
            return p.read_bytes() if p.is_file() else None

        for raw in read_region(path.read_bytes(), rx, rz, external):
            if raw.nbt is None:
                skip("unreadable")
                continue
            c = raw.nbt
            if c.get("Status") not in statuses:
                skip("status")
                continue
            if c.get("DataVersion") != palette.data_version:
                skip("data_version")
                continue
            try:
                cols = chunk_columns(c, palette)
            except (NbtError, KeyError, TypeError, ValueError):
                skip("malformed")
                continue
            chunks[(c["xPos"], c["zPos"])] = cols
            versions[(c["xPos"], c["zPos"])] = int(c["DataVersion"])
    return chunks, versions, skipped


def render_dimension(region_dir: Path, palette: Palette, provisional: bool = False) -> Rendered:
    statuses = frozenset({FULL}) | (PROVISIONAL if provisional else frozenset())
    chunks, versions, skipped = load_dimension(region_dir, palette, statuses)
    out = render_columns(chunks, versions)
    out.skipped = skipped
    return out


def tile_bytes(
    scale: int, tx: int, tz: int, ids: bytes, data_version: int, rendered_at: int, flags: int = 0
) -> bytes:
    if len(ids) != TILE_PX * TILE_PX:
        raise ValueError("a tile is 512 x 512 pixels")
    return HEADER.pack(MAGIC, scale, flags, 0, tx, tz, data_version, rendered_at) + bytes(ids)


def parse_tile(data: bytes) -> tuple[dict[str, int], bytes]:
    """(header fields, 262144 packed ids). Raises on anything but a well-formed tile."""
    if len(data) != HEADER.size + TILE_PX * TILE_PX:
        raise ValueError("wrong tile size")
    magic, scale, flags, reserved, tx, tz, dv, at = HEADER.unpack_from(data)
    if magic != MAGIC or scale not in SCALES or reserved != 0:
        raise ValueError("bad tile header")
    return {"scale": scale, "flags": flags, "tx": tx, "tz": tz, "data_version": dv, "rendered_at": at}, data[
        32:
    ]


def tile_path(namespace: str, dim: str, scale: int, tx: int, tz: int) -> str:
    return f"{namespace}/{dim}/s{scale}/{tx}.{tz}.swt"


def render_world(
    world: Path, palette: Palette, dimensions: tuple[str, ...] = DEFAULT_DIMENSIONS, rendered_at: int = 0,
    provisional: bool = False,
) -> tuple[dict[str, bytes], dict[str, Any]]:  # fmt: skip
    """{relative tile path: tile file bytes}, summary."""
    files: dict[str, bytes] = {}
    summary: dict[str, Any] = {}
    for dim_id in dimensions:
        ns, dim = dim_id.split("/")
        r = render_dimension(world / "dimensions" / ns / dim / "region", palette, provisional)
        flags = FLAG_PROVISIONAL if provisional else 0
        for (scale, tx, tz), ids in sorted(r.tiles.items()):
            dv = r.data_version[(scale, tx, tz)]
            files[tile_path(ns, dim, scale, tx, tz)] = tile_bytes(scale, tx, tz, ids, dv, rendered_at, flags)
        summary[dim_id] = {
            "chunks": r.chunks,
            "skipped": dict(sorted(r.skipped.items())),
            "tiles": len(r.tiles),
        }
    summary["unknown_states"] = sorted(palette.unknown)
    return files, summary


# =====================================================================================================================
# Pocket maps: what a vanilla map with a given centre draws (for the centre-snap rule and its tests)
# =====================================================================================================================
def pocket_centre(coord: float, scale: int) -> int:
    """The pocket map's centre: a multiple of 2 * 2**scale blocks (Wayfinding.snap(coord, scale + 1)), so its
    pixel parity equals the tiles' (gx + gz) & 1."""
    step = 2 << scale
    return math.floor(coord) // step * step


def jdiv(a: int, b: int) -> int:
    """Java int division (truncates toward zero)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def vanilla_window(pix: dict[tuple[int, int], Pixel], cx: int, cz: int, scale: int) -> list[int | None]:
    """The 128 x 128 packed ids MapItem.update writes for a map centred on (cx, cz), each pixel computed with
    vanilla's own k1/l1 parity and running d0 (None where the chunk isn't loaded or the north pixel is missing,
    which the renderer handles by its own rule)."""
    i = 1 << scale
    out: list[int | None] = [None] * (128 * 128)
    for k1 in range(128):
        for l1 in range(128):
            gx, gz = jdiv(cx, i) + k1 - 64, jdiv(cz, i) + l1 - 64
            p, north = pix.get((gx, gz)), pix.get((gx, gz - 1))
            if p is None or north is None:
                continue
            b = brightness(p.colour, p.d1, north.d1, p.depth, scale, (k1 + l1) & 1)
            out[l1 * 128 + k1] = 0 if p.colour == 0 else p.colour * 4 + b
    return out


def tile_window(tiles: dict[tuple[int, int, int], bytes], cx: int, cz: int, scale: int) -> list[int]:
    """The 128 x 128 window mc-brand cuts from the tiles for a map centred on (cx, cz) (at most 2 x 2 tiles)."""
    i = 1 << scale
    out = [0] * (128 * 128)
    for l1 in range(128):
        for k1 in range(128):
            gx, gz = jdiv(cx, i) + k1 - 64, jdiv(cz, i) + l1 - 64
            t = tiles.get((scale, gx // TILE_PX, gz // TILE_PX))
            if t is not None:
                out[l1 * 128 + k1] = t[(gz % TILE_PX) * TILE_PX + gx % TILE_PX]
    return out


def rgba(packed_id: int, colours: list[int]) -> tuple[int, int, int, int]:
    """The colour a map shows for a packed id (MapColor.calculateARGBColor); 0..3 are transparent."""
    if packed_id < 4:
        return (0, 0, 0, 0)
    c, m = colours[packed_id >> 2], MULTIPLIER[packed_id & 3]
    return ((c >> 16 & 255) * m // 255, (c >> 8 & 255) * m // 255, (c & 255) * m // 255, 255)


# =====================================================================================================================
# CLI
# =====================================================================================================================
def golden(world: Path = FIXTURE) -> dict[str, Any]:
    version = world.name.removeprefix("world-")
    palette = palette_for(version)
    files, summary = render_world(world, palette, FIXTURE_DIMENSIONS)
    pal_file = REPO / "server-config" / "keys" / version / "map-palette.tsv"
    return {
        "comment": "tests/map_reference.py golden: sha256 of every tile the reference renders from this fixture "
        "(rendered_at 0, FIXTURE_DIMENSIONS, full chunks only). The Rust renderer must match.",
        "palette_sha256": hashlib.sha256(pal_file.read_bytes()).hexdigest(),
        "rendered_at": 0,
        "summary": summary,
        "tiles": {p: hashlib.sha256(b).hexdigest() for p, b in sorted(files.items())},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="render a world's tiles into OUT/<ns>/<dim>/s<scale>/<tx>.<tz>.swt")
    r.add_argument("world", type=Path)
    r.add_argument("out", type=Path)
    r.add_argument("--version", default="26.3", help="the palette's minecraft_version")
    r.add_argument("--dimension", action="append", help="ns/dim (default: overworld and the End)")
    r.add_argument("--rendered-at", type=int, default=0)
    r.add_argument("--provisional", action="store_true")
    sub.add_parser("golden", help=f"rewrite {GOLDEN.relative_to(REPO)}")
    args = ap.parse_args(argv)
    if args.cmd == "golden":
        GOLDEN.write_text(json.dumps(golden(), indent=2) + "\n")
        print(f"wrote {GOLDEN.relative_to(REPO)}")
        return 0
    dims = tuple(args.dimension or DEFAULT_DIMENSIONS)
    files, summary = render_world(
        args.world, palette_for(args.version), dims, args.rendered_at, args.provisional
    )
    for rel, data in files.items():
        p = args.out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
