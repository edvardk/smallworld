#!/usr/bin/env python3
"""Build the world map's golden fixture, tests/fixtures/world-<version>/ (v1.4; README.md there).

    map_fixture.py trim WORLD [--out DIR]   # the real chunks: copy WORLD's full chunks (overworld, End)
    map_fixture.py synthetic [--out DIR]    # the edge cases: the generated `smallworld/synthetic` dimension

`trim` keeps, per region file of the overworld and the End that has any `minecraft:full` chunk, every full
chunk plus one chunk of each other status (the renderer must skip those), byte for byte (compressed payload
and timestamp as the server wrote them), in new, densely packed region files. Nothing else is copied: no
level.dat (the renderer reads the dimension's minimum Y from each chunk), no player data, no entities, no
poi; a kept chunk with anything that names or identifies a player or an entity (UUIDs, names, owners) is
refused.

`synthetic` writes the cases a fresh world lacks (see-through blocks, waterlogged stairs and slabs, water
depths at every shading threshold, bedrock/void and see-through-to-the-bottom columns, colour ties, a palette
over 256 entries, mixed string/compound palettes with missing properties, gzip / none / external .mcc chunks,
chunks the renderer must skip) as region files of a made-up dimension, from the pinned palette.

After either, regenerate the goldens: `python tests/map_reference.py golden`.
"""

from __future__ import annotations

import argparse
import gzip
import re
import struct
import sys
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
import map_reference as ref  # noqa: E402 - the reference renderer lives with its tests

DIMENSIONS = ("minecraft/overworld", "minecraft/the_end")
SYNTHETIC = "smallworld/synthetic"
# NBT keys that would identify a player or an entity; a kept chunk must have none (trim refuses it).
IDENTIFYING = re.compile(
    r"^(UUID.*|.*Uuid.*|.*UUID|Owner.*|CustomName|LastOutput|lastKnownName|Bukkit\..*|PublicBukkitValues|Paper\..*"
    r"|ChunkBukkitValues|text|messages|front_text|back_text|Book|profile|custom_name|lock)$"
)
TIMESTAMP = 1_700_000_000  # the synthetic chunks' region timestamps (fixed: the files are reproducible)


# --- NBT and region writing ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class Tag:
    """An NBT value whose tag isn't implied by its Python type (dict = compound, str = string)."""

    tag: int
    value: Any


def byte(v: int) -> Tag:
    return Tag(ref.BYTE, v)


def int_(v: int) -> Tag:
    return Tag(ref.INT, v)


def long(v: int) -> Tag:
    return Tag(ref.LONG, v)


def long_array(v: list[int]) -> Tag:
    return Tag(ref.LONG_ARRAY, v)


def list_(tag: int, items: list[Any]) -> Tag:
    return Tag(ref.LIST, (tag, items))


def _tag(v: Any) -> int:  # noqa: ANN401 - any NBT value
    if isinstance(v, Tag):
        return v.tag
    if isinstance(v, dict):
        return ref.COMPOUND
    if isinstance(v, str):
        return ref.STRING
    raise TypeError(f"untyped NBT value {v!r}")


def _str(s: str) -> bytes:
    b = s.encode()
    return struct.pack(">H", len(b)) + b


def _payload(v: Any) -> bytes:  # noqa: ANN401 - any NBT value
    if isinstance(v, dict):
        return b"".join(bytes([_tag(x)]) + _str(k) + _payload(x) for k, x in v.items()) + b"\0"
    if isinstance(v, str):
        return _str(v)
    fmt = {ref.BYTE: "b", ref.SHORT: "h", ref.INT: "i", ref.LONG: "q", ref.FLOAT: "f", ref.DOUBLE: "d"}
    if v.tag in fmt:
        return struct.pack(">" + fmt[v.tag], v.value)
    if v.tag == ref.LONG_ARRAY:
        return struct.pack(f">i{len(v.value)}q", len(v.value), *v.value)
    if v.tag == ref.INT_ARRAY:
        return struct.pack(f">i{len(v.value)}i", len(v.value), *v.value)
    if v.tag == ref.BYTE_ARRAY:
        return struct.pack(">i", len(v.value)) + bytes(v.value)
    if v.tag == ref.LIST:
        elem, items = v.value
        return bytes([elem]) + struct.pack(">i", len(items)) + b"".join(_payload(x) for x in items)
    raise TypeError(f"tag {v.tag}")


def nbt_bytes(root: dict[str, Any], name: str = "") -> bytes:
    """An uncompressed NBT document with a compound root."""
    return bytes([ref.COMPOUND]) + _str(name) + _payload(root)


def region_bytes(chunks: dict[int, tuple[bytes, int]]) -> bytes:
    """A region file from {index: (stored payload = u32 length + version byte + data, timestamp)}, sectors
    packed densely in index order, every payload padded to whole sectors."""
    head, stamps, body = bytearray(4096), bytearray(4096), bytearray()
    sector = 2
    for idx in sorted(chunks):
        payload, ts = chunks[idx]
        count = (len(payload) + 4095) // 4096
        if count > 255:
            raise ValueError("chunk needs an external .mcc file")
        struct.pack_into(">I", head, idx * 4, sector << 8 | count)
        struct.pack_into(">I", stamps, idx * 4, ts)
        body += payload + bytes(count * 4096 - len(payload))
        sector += count
    return bytes(head + stamps + body)


def stored(nbt: bytes, compression: int) -> bytes:
    """A chunk payload as the region file stores it (compression 1 gzip, 2 zlib, 3 none)."""
    data = {1: lambda b: gzip.compress(b, mtime=0), 2: zlib.compress, 3: lambda b: b}[compression](nbt)
    return struct.pack(">IB", len(data) + 1, compression) + data


# --- trim ---------------------------------------------------------------------------------------------------
def identifying_keys(value: Any, path: str = "") -> list[str]:  # noqa: ANN401 - any NBT value
    out = []
    if isinstance(value, dict):
        for k, v in value.items():
            if IDENTIFYING.match(k):
                out.append(f"{path}/{k}")
            out += identifying_keys(v, f"{path}/{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out += identifying_keys(v, f"{path}[{i}]")
    return out


def trim_region(data: bytes, rx: int, rz: int) -> tuple[dict[int, tuple[bytes, int]], dict[str, int]]:
    """({index: (stored payload, timestamp)} to keep, kept chunks per status)."""
    keep: dict[int, tuple[bytes, int]] = {}
    statuses: dict[str, int] = {}
    for raw in ref.read_region(data, rx, rz):
        if raw.nbt is None:
            raise SystemExit(f"r.{rx}.{rz}.mca chunk {raw.index}: {raw.error}")
        status = str(raw.nbt.get("Status"))
        if status != ref.FULL and status in statuses:
            continue
        bad = identifying_keys(raw.nbt) + (["/entities"] if raw.nbt.get("entities") else [])
        if bad:
            raise SystemExit(f"r.{rx}.{rz}.mca chunk {raw.index} has identifying data: {bad[:5]}")
        loc = struct.unpack_from(">I", data, raw.index * 4)[0]
        p = (loc >> 8) * 4096
        length = struct.unpack_from(">I", data, p)[0]
        keep[raw.index] = (data[p : p + 4 + length], raw.timestamp)
        statuses[status] = statuses.get(status, 0) + 1
    if statuses.get(ref.FULL, 0) == 0:
        return {}, {}
    return keep, statuses


def trim(world: Path, out: Path) -> None:
    for dim in DIMENSIONS:
        src = world / "dimensions" / dim / "region"
        dst = out / "dimensions" / dim / "region"
        for old in dst.glob("*.mca") if dst.is_dir() else []:
            old.unlink()
        for path in sorted(src.glob("r.*.*.mca")):
            rx, rz = (int(v) for v in path.name.split(".")[1:3])
            keep, statuses = trim_region(path.read_bytes(), rx, rz)
            if keep:
                dst.mkdir(parents=True, exist_ok=True)
                (dst / path.name).write_bytes(region_bytes(keep))
                print(f"{dim}/{path.name}: {statuses}")


# --- synthetic ----------------------------------------------------------------------------------------------
Block = str | dict[str, Any]  # a palette entry: a bare id (default state) or {"id", "properties"?}
AIR = "minecraft:air"
SYN_MIN_Y, SYN_SECTIONS = 0, 4  # a 64-block-tall dimension: 7-bit heightmaps (29 longs)


def pack_bits(values: list[int], bits: int) -> list[int]:
    per = 64 // bits
    longs = [0] * ((len(values) + per - 1) // per)
    for i, v in enumerate(values):
        longs[i // per] |= v << ((i % per) * bits)
    return [x - (1 << 64) if x >= 1 << 63 else x for x in longs]


def _key(b: Block) -> str:
    return b if isinstance(b, str) else repr(sorted(b.items()))


def _entry(b: Block, mixed: bool) -> Any:  # noqa: ANN401 - an NBT palette entry
    if isinstance(b, str):
        return {"": b} if mixed else b
    props = b.get("properties")
    return {"id": b["id"], **({"properties": dict(props)} if props else {})}


def synthetic_chunk(
    cx: int, cz: int, block: Callable[[int, int, int], Block], status: str = ref.FULL, data_version: int = 0,
    x_pos: int | None = None,
) -> bytes:  # fmt: skip
    """The NBT of a chunk whose blocks are block(x, y, z) (local x, z; absolute y)."""
    sections: list[dict[str, Any]] = [{"Y": byte(SYN_MIN_Y // 16 - 1)}]  # light-only, as 26.x writes
    heights = [0] * 256
    for sy in range(SYN_MIN_Y // 16, SYN_MIN_Y // 16 + SYN_SECTIONS):
        order: dict[str, tuple[int, Block]] = {}
        idx = []
        for y in range(sy * 16, sy * 16 + 16):
            for z in range(16):
                for x in range(16):
                    b = block(x, y, z)
                    if b != AIR:
                        heights[x + z * 16] = y + 1 - SYN_MIN_Y
                    idx.append(order.setdefault(_key(b), (len(order), b))[0])
        entries = [b for _, b in order.values()]
        mixed = any(isinstance(b, dict) for b in entries) and any(isinstance(b, str) for b in entries)
        tag = ref.COMPOUND if mixed or all(isinstance(b, dict) for b in entries) else ref.STRING
        bs: dict[str, Any] = {"palette": list_(tag, [_entry(b, mixed) for b in entries])}
        if len(entries) > 1:
            bs["data"] = long_array(pack_bits(idx, max(4, ref.ceillog2(len(entries)))))
        sections.append({"Y": byte(sy), "block_states": bs})
    sections.append({"Y": byte(SYN_MIN_Y // 16 + SYN_SECTIONS)})
    hm_bits = ref.ceillog2(SYN_SECTIONS * 16 + 1)
    return nbt_bytes({
        "DataVersion": int_(data_version),
        "xPos": int_(cx if x_pos is None else x_pos),
        "zPos": int_(cz),
        "yPos": int_(SYN_MIN_Y // 16),
        "Status": status,
        "Heightmaps": {"WORLD_SURFACE": long_array(pack_bits(heights, hm_bits))},
        "sections": list_(ref.COMPOUND, sections),
        "block_entities": list_(ref.END, []),
    })  # fmt: skip


def _s(name: str, **props: str) -> Block:
    return {"id": f"minecraft:{name}", "properties": props} if props else f"minecraft:{name}"


GROUND = 10  # the synthetic flat ground's top block


def features_block(x: int, y: int, z: int) -> Block:
    """Chunk (0, 0): one feature per x strip, varying with z."""
    if y > GROUND + 16:
        return AIR
    floor = _s("stone") if y < GROUND else _s("grass_block") if y == GROUND else AIR
    if x == 0:  # see-through: glass, a pane, a torch and a rail over the ground
        top = [_s("glass"), _s("glass_pane"), _s("torch"), _s("rail"), _s("glass")][z % 5]
        return top if y in (GROUND + 1, GROUND + 2) and z % 5 != 4 else floor if y <= GROUND else (
            _s("glass") if y == GROUND + 1 else AIR)  # fmt: skip
    if 1 <= x <= 3:  # water of depth z + 1 (1..16) over sand: every shading threshold (4/5, 9/10)
        depth = z + 1
        if GROUND - depth < y <= GROUND:
            return _s("water")
        return _s("sand") if y == GROUND - depth else _s("stone") if y < GROUND - depth else AIR
    if x == 4:  # waterlogged stairs and slabs over water (top face sturdy or not), given with partial props
        kinds = [
            {"id": "minecraft:oak_stairs", "properties": {"waterlogged": "true"}},  # bottom half: WATER
            {"id": "minecraft:oak_stairs", "properties": {"half": "top", "waterlogged": "true"}},  # keeps oak
            {"id": "minecraft:oak_slab", "properties": {"waterlogged": "true"}},  # bottom slab: WATER
            {"id": "minecraft:oak_slab", "properties": {"type": "top", "waterlogged": "true"}},  # keeps oak
            _s("seagrass"),
            _s("kelp_plant"),
            {"id": "minecraft:lava", "properties": {"level": "0"}},
            _s("bubble_column"),
        ]
        if y == GROUND:
            return kinds[z % len(kinds)]
        return _s("water") if GROUND - 3 < y < GROUND else floor
    if 5 <= x <= 8:  # colour ties at scales >= 1: a 2 x 2 checker of two colours
        if y < GROUND:
            return _s("stone")
        if y == GROUND:
            return _s("white_wool") if (x + z) % 2 else _s("red_wool")
        return AIR
    if 9 <= x <= 11:  # slopes: up two blocks per z (HIGH), then down (LOW), then flat
        h = GROUND + (z if z < 8 else 16 - z) * 2 if x != 11 else GROUND + 3
        return _s("stone") if y < h else _s("snow_block") if y == h else AIR
    if x == 12:  # snow layers, powder snow and a lantern on a fence (non-full blocks)
        top = [_s("snow", layers="3"), _s("powder_snow"), _s("oak_fence"), _s("lantern")][z % 4]
        return top if y == GROUND + 1 else floor
    return floor


def edges_block(x: int, y: int, z: int) -> Block:
    """Chunk (1, 0) (gzip): bedrock/void columns and see-through to the bottom of the world."""
    kind = (x + 16 * z) % 6
    if kind == 0:  # empty column: vanilla draws bedrock
        return AIR
    if kind == 1:  # a single block at minY: surface y = minY + 1, still bedrock
        return _s("stone") if y == SYN_MIN_Y else AIR
    if kind == 2:  # top block at minY + 1: walked
        return _s("dirt") if y <= SYN_MIN_Y + 1 else AIR
    if kind == 3:  # glass all the way down: the walk stops at minY on NONE
        return _s("glass") if y <= 5 else AIR
    if kind == 4:  # water down to minY
        return _s("water") if y <= 7 else AIR
    return _s("stone") if y < 3 else _s("white_concrete") if y == 3 else AIR


def big_palette_states(n: int = 300) -> list[Block]:
    """The first `n` states of the palette file, as {id, properties} entries: a section over 256 entries."""
    out: list[Block] = []
    path = REPO / "server-config" / "keys" / ref_version() / "map-palette.tsv"
    for line in path.read_text().splitlines():
        f = line.split("\t")
        if f[0] in ("D", "S") and f[1] not in (AIR, "minecraft:cave_air", "minecraft:void_air"):
            props = dict(kv.split("=", 1) for kv in f[2].split(",")) if f[2] != "-" else {}
            out.append({"id": f[1], "properties": props} if props else f[1])
            if len(out) == n:
                break
    return out


def ref_version() -> str:
    return ref.FIXTURE.name.removeprefix("world-")


def synthetic(out: Path) -> None:
    pal = ref.palette_for(ref_version())
    dv = pal.data_version
    big = big_palette_states()

    def big_block(x: int, y: int, z: int) -> Block:
        if y < 16:  # the bottom section holds all 300 states (9 bits, global path)
            return big[(x + 16 * z + 256 * y) % len(big)]
        return AIR

    def flat(height: int) -> Callable[[int, int, int], Block]:
        return lambda x, y, z: _s("stone") if y < height else _s("grass_block") if y == height else AIR

    regions: dict[tuple[int, int], dict[int, tuple[bytes, int]]] = {}
    external: dict[str, bytes] = {}

    def put(cx: int, cz: int, nbt: bytes, compression: int = 2, ext: bool = False) -> None:
        idx = (cx & 31) + (cz & 31) * 32
        if ext:
            external[f"c.{cx}.{cz}.mcc"] = zlib.compress(nbt)
            payload = struct.pack(">IB", 1, 0x80 | 2)
        else:
            payload = stored(nbt, compression)
        regions.setdefault((cx >> 5, cz >> 5), {})[idx] = (payload, TIMESTAMP)

    put(0, 0, synthetic_chunk(0, 0, features_block, data_version=dv))
    put(1, 0, synthetic_chunk(1, 0, edges_block, data_version=dv), compression=1)
    put(2, 0, synthetic_chunk(2, 0, big_block, data_version=dv), compression=3)
    put(3, 0, synthetic_chunk(3, 0, flat(GROUND + 1), data_version=dv), ext=True)
    put(0, 1, synthetic_chunk(0, 1, flat(GROUND - 2), data_version=dv))  # south of (0, 0): a step down
    put(0, -1, synthetic_chunk(0, -1, flat(GROUND + 6), data_version=dv))  # region r.0.-1, north of (0, 0)
    put(
        4, 0, synthetic_chunk(4, 0, flat(GROUND), status="minecraft:features", data_version=dv)
    )  # provisional
    put(5, 0, synthetic_chunk(5, 0, flat(GROUND), data_version=dv - 1))  # another DataVersion: skipped
    put(6, 0, synthetic_chunk(6, 0, flat(GROUND), data_version=dv, x_pos=7))  # wrong slot: unreadable
    dst = out / "dimensions" / SYNTHETIC / "region"
    dst.mkdir(parents=True, exist_ok=True)
    for old in [*dst.glob("*.mca"), *dst.glob("*.mcc")]:
        old.unlink()
    for (rx, rz), chunks in sorted(regions.items()):
        (dst / f"r.{rx}.{rz}.mca").write_bytes(region_bytes(chunks))
    for name, data in external.items():
        (dst / name).write_bytes(data)
    print(f"{SYNTHETIC}: {sum(map(len, regions.values()))} chunks in {len(regions)} region files")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("trim", help="copy a world's full chunks into the fixture")
    t.add_argument("world", type=Path)
    t.add_argument("--out", type=Path, default=ref.FIXTURE)
    s = sub.add_parser("synthetic", help="write the synthetic dimension")
    s.add_argument("--out", type=Path, default=ref.FIXTURE)
    args = ap.parse_args(argv)
    if args.cmd == "trim":
        trim(args.world, args.out)
    else:
        synthetic(args.out)
    print("now: python tests/map_reference.py golden")
    return 0


if __name__ == "__main__":
    sys.exit(main())
