"""v1.4 world map: the Python reference renderer (tests/map_reference.py), its golden fixture
(tests/fixtures/world-26.3/) and the palette (scripts/map_palette.py, server-config/keys/<version>/map-*.tsv).

The reference renderer is the oracle the Rust renderer must match byte for byte (docs/reference.md "World
map"); these tests pin its pieces to vanilla's algorithm (MapItem.update, read from the pinned jar's bytecode)
on synthetic inputs, and its output on the fixture to golden.json.
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path

import pytest
import yaml
from conftest import REPO

sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "scripts"))
import map_fixture as fx
import map_reference as ref

LOCK = yaml.safe_load((REPO / "template/server.lock.yml").read_text())
MC_VERSION = str(LOCK["minecraft_version"])
KEYS = REPO / "server-config" / "keys" / MC_VERSION
FIXTURE_VERSION = ref.FIXTURE.name.removeprefix("world-")


@pytest.fixture(scope="module")
def palette() -> ref.Palette:
    return ref.palette_for(MC_VERSION)


@pytest.fixture(scope="module")
def rendered() -> tuple[dict[str, bytes], dict]:
    return ref.render_world(ref.FIXTURE, ref.palette_for(FIXTURE_VERSION), ref.FIXTURE_DIMENSIONS)


# --- the palette --------------------------------------------------------------------------------------------
def test_palette_exists_for_pinned_version() -> None:
    """Like the config keys: a version bump must regenerate the palette (scripts/map_palette.py dump)."""
    assert (KEYS / "map-palette.tsv").is_file(), f"run scripts/map_palette.py dump for {MC_VERSION}"
    assert (KEYS / "map-colors.tsv").is_file(), f"run scripts/map_palette.py dump for {MC_VERSION}"
    meta = dict(
        line.split("\t")[1:]
        for line in (KEYS / "map-palette.tsv").read_text().splitlines()
        if line[:2] == "M\t"
    )
    assert meta["format"] == "1"
    assert meta["minecraft_version"] == MC_VERSION
    assert meta["server"] == LOCK["server"]["filename"]
    # client/pack.yml pins the same version's DataVersion (checked against the jar by build_plugin.py check)
    pack = yaml.safe_load((REPO / "client/pack.yml").read_text())
    assert int(meta["data_version"]) == pack["data_versions"][MC_VERSION]


def test_palette_contents(palette: ref.Palette) -> None:
    text = (KEYS / "map-palette.tsv").read_text().splitlines()
    meta = dict(line.split("\t")[1:] for line in text if line[:2] == "M\t")
    assert int(meta["blocks"]) == len(palette.defaults)
    d_lines = [line for line in text if line[:2] == "D\t"]
    s_lines = [line for line in text if line[:2] == "S\t"]
    assert len(d_lines) == len(palette.defaults) and len(s_lines) == len(palette.states)
    assert all(len(line.split("\t")) == 6 for line in d_lines + s_lines)
    # Every S line follows its own block's D line (registry order) and differs from it.
    current = None
    for line in text:
        f = line.split("\t")
        if f[0] == "D":
            current = f
        elif f[0] == "S":
            assert current is not None and f[1] == current[1] and f[3:] != current[3:]
    # The vanilla facts the renderer relies on.
    assert palette.lookup("minecraft:air") == ref.AIR
    assert palette.lookup("minecraft:water") == ref.BlockInfo(ref.WATER, True, ref.WATER)
    assert palette.lookup("minecraft:lava") == ref.BlockInfo(4, True, 4)  # FIRE
    assert palette.lookup("minecraft:glass").colour == 0  # see-through
    assert palette.bedrock == 11  # STONE
    assert (
        palette.colours[ref.WATER] == 0x4040FF
        and palette.colours[1] == 0x7FB238
        and len(palette.colours) == 62
    )
    intrinsic = {
        name for name, (_, info) in palette.defaults.items() if info.fluid and "waterlogged" not in _
    }
    assert {"minecraft:water", "minecraft:lava", "minecraft:kelp", "minecraft:seagrass"} <= intrinsic


def test_palette_waterlogging(palette: ref.Palette) -> None:
    """getCorrectStateForFluidBlock: a waterlogged state counts as water unless its top face is sturdy."""

    def st(name: str, **props: str) -> ref.BlockInfo:
        return palette.lookup({"id": f"minecraft:{name}", "properties": props})

    oak = 13
    assert st("oak_stairs", waterlogged="true") == ref.BlockInfo(oak, True, ref.WATER)  # bottom half
    assert st("oak_stairs", half="top", waterlogged="true") == ref.BlockInfo(oak, True, oak)
    assert st("oak_slab", waterlogged="true").surface == ref.WATER  # bottom slab
    assert st("oak_slab", type="double", waterlogged="true").surface == oak
    assert st("oak_stairs") == ref.BlockInfo(oak, False, oak)


def test_palette_check_is_wired_into_ci() -> None:
    """map_palette.py check runs where the jar-based checks run (ci.yml job plugin, the same cached jars), and
    the job runs when its inputs change."""
    ci = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text())
    steps = [s.get("run", "") for s in ci["jobs"]["plugin"]["steps"]]
    assert 'python scripts/map_palette.py check --cache "$RUNNER_TEMP/build-plugin"' in steps
    sys.path.insert(0, str(REPO / "scripts"))
    import ci_changes

    groups = ci_changes.GROUPS["plugin"]
    for path in ("scripts/map_palette.py", "scripts/map_palette/Dump.java", "scripts/config_keys.py",
                 f"server-config/keys/{MC_VERSION}/map-palette.tsv"):  # fmt: skip
        assert any(__import__("fnmatch").fnmatchcase(path, g) for g in groups), path


def test_palette_text_builder() -> None:
    """map_palette.palette_text: D line per block, S lines only for states that differ, sorted properties."""
    import map_palette

    states = "\n".join([
        "Block{minecraft:air}\t0\t0\t0\t1",
        "Block{minecraft:oak_slab}[type=top,waterlogged=false]\t13\t0\t13\t0",
        "Block{minecraft:oak_slab}[type=bottom,waterlogged=false]\t13\t0\t13\t1",
        "Block{minecraft:oak_slab}[type=bottom,waterlogged=true]\t13\t1\t12\t0",
    ])  # fmt: skip
    text, stats = map_palette.palette_text(
        states, {"minecraft_version": "x", "data_version": "1", "server": "s"}
    )
    body = [line for line in text.splitlines() if not line.startswith("#")]
    assert body == [
        "M\tformat\t1", "M\tminecraft_version\tx", "M\tdata_version\t1", "M\tserver\ts", "M\tblocks\t2",
        "M\tstates\t4", "D\tminecraft:air\t-\t0\t0\t0", "D\tminecraft:oak_slab\ttype=bottom,waterlogged=false\t13\t0\t13",
        "S\tminecraft:oak_slab\ttype=bottom,waterlogged=true\t13\t1\t12",
    ]  # fmt: skip
    assert stats["exceptions"] == 1 and stats["see_through_blocks"] == 1
    with pytest.raises(SystemExit):
        map_palette.palette_text("Block{minecraft:x}[b=1,a=2]\t0\t0\t0\t1", {})


# --- NBT ----------------------------------------------------------------------------------------------------
def test_nbt_round_trip_every_tag() -> None:
    doc = {
        "b": fx.byte(-3), "s": fx.Tag(ref.SHORT, 300), "i": fx.int_(-70000), "l": fx.long(-(2**40)),
        "f": fx.Tag(ref.FLOAT, 0.5), "d": fx.Tag(ref.DOUBLE, -1.25), "ba": fx.Tag(ref.BYTE_ARRAY, [1, 2]),
        "str": "héllo", "list": fx.list_(ref.INT, [fx.int_(1), fx.int_(2)]), "empty": fx.list_(ref.END, []),
        "c": {"nested": {"x": fx.int_(1)}}, "ia": fx.Tag(ref.INT_ARRAY, [1, -1]),
        "la": fx.long_array([-1, 2**62]),
    }  # fmt: skip
    name, v = ref.parse_nbt(fx.nbt_bytes(doc, "root"))
    assert name == "root"
    assert v == {
        "b": -3, "s": 300, "i": -70000, "l": -(2**40), "f": 0.5, "d": -1.25, "ba": b"\x01\x02", "str": "héllo",
        "list": [1, 2], "empty": [], "c": {"nested": {"x": 1}}, "ia": [1, -1], "la": [-1, 2**62],
    }  # fmt: skip


def test_nbt_rejects_malformed() -> None:
    good = fx.nbt_bytes({"x": fx.int_(1)})
    for bad in (good[:-2], b"\x0a\x00\x00\x63", b"\x0a\x00\x00\x07\x00\x01a\xff\xff\xff\xff"):
        with pytest.raises(ref.NbtError):
            ref.parse_nbt(bad)
    n = ref.MAX_DEPTH + 2  # compounds nested n deep
    with pytest.raises(ref.NbtError, match="deep"):
        ref.parse_nbt(b"\x0a\x00\x00" + b"\x0a\x00\x01c" * n + b"\x00" * (n + 1))
    n = ref.MAX_DEPTH - 1
    ref.parse_nbt(b"\x0a\x00\x00" + b"\x0a\x00\x01c" * n + b"\x00" * (n + 1))


def test_heterogeneous_list_unwrap() -> None:
    """26.x: a list mixing strings and compounds is a list of compounds, strings wrapped as {"": value}."""
    doc = {"palette": fx.list_(ref.COMPOUND, [{"": "minecraft:stone"}, {"id": "minecraft:oak_log"}])}
    _, v = ref.parse_nbt(fx.nbt_bytes(doc))
    assert [ref.unwrap(e) for e in v["palette"]] == ["minecraft:stone", {"id": "minecraft:oak_log"}]
    assert ref.unwrap({"": 1, "x": 2}) == {"": 1, "x": 2}


# --- palette entries ----------------------------------------------------------------------------------------
def test_palette_entries_26x(palette: ref.Palette) -> None:
    log = "minecraft:oak_log"
    assert palette.key(log) == (log, "axis=y")  # bare id: the default state
    assert palette.key({"": log}) == (log, "axis=y")  # wrapped in a mixed list
    assert palette.key({"id": log, "properties": {"axis": "x"}}) == (log, "axis=x")
    assert palette.key({"id": log}) == (log, "axis=y")  # properties are optional
    # missing properties take the default state's values (StateDefinition: fieldOf(...).orElseGet(default))
    assert palette.key({"id": "minecraft:oak_stairs", "properties": {"waterlogged": "true"}}) == (
        "minecraft:oak_stairs",
        "facing=north,half=bottom,shape=straight,waterlogged=true",
    )
    assert palette.lookup({"id": log, "properties": {"axis": "x"}}).colour != palette.lookup(log).colour
    assert palette.key("minecraft:stone") == ("minecraft:stone", "-")


def test_unknown_states_are_see_through_and_counted() -> None:
    p = ref.palette_for(MC_VERSION)
    assert p.lookup("minecraft:no_such_block") == ref.AIR
    assert p.lookup({"id": "minecraft:stone", "properties": {"bogus": "1"}}) == ref.AIR
    assert p.unknown == {"minecraft:no_such_block[-]": 1, "minecraft:stone[bogus=1]": 1}
    with pytest.raises(ref.NbtError):
        p.key({"Name": "minecraft:stone"})  # pre-26 entries are not this DataVersion's format


# --- bit storage, heightmaps --------------------------------------------------------------------------------
def test_bit_storage_never_spans_longs() -> None:
    values = [(i * 7) % 29 for i in range(4096)]
    longs = fx.pack_bits(values, 5)  # 12 per long, 4 bits unused
    assert len(longs) == 342
    assert ref.unpack_bits(longs, 5, 4096) == values
    assert ref.unpack_bits([-1], 4, 16) == [15] * 16  # signed longs
    with pytest.raises(ref.NbtError):
        ref.unpack_bits(longs[:-1], 5, 4096)


@pytest.mark.parametrize(
    ("height", "bits", "longs"), [(384, 9, 37), (256, 9, 37), (64, 7, 29), (4064, 12, 52)]
)
def test_heightmap_bits(height: int, bits: int, longs: int) -> None:
    assert ref.ceillog2(height + 1) == bits
    packed = fx.pack_bits([height] * 256, bits)
    assert len(packed) == longs
    assert ref.heightmap_bits(packed, height) == bits
    assert ref.unpack_bits(packed, bits, 256) == [height] * 256


def test_heightmap_11_vs_12_bits_takes_the_height() -> None:
    assert ref.heightmap_bits([0] * 52, 1500) == 11
    assert ref.heightmap_bits([0] * 52, 3000) == 12


def test_block_state_bits_and_sections(palette: ref.Palette) -> None:
    """max(4, ceillog2(n)) bits; a single entry has no data; a section without block_states is air."""
    entries = ["minecraft:stone", "minecraft:dirt", "minecraft:glass"]
    idx = [i % 3 for i in range(4096)]
    chunk = {"sections": [
        {"Y": -5},
        {"Y": 0, "block_states": {"palette": entries, "data": fx.pack_bits(idx, 4)}},
        {"Y": 1, "block_states": {"palette": ["minecraft:water"]}},
    ]}  # fmt: skip
    secs = ref.decode_sections(chunk, palette)
    assert set(secs) == {0, 1}
    assert secs[1][1] is None and secs[1][0][0].fluid
    assert [p.colour for p in secs[0][0]] == [11, 10, 0]


# --- the column walk ----------------------------------------------------------------------------------------
STONE, GRASS = ref.BlockInfo(11, False, 11), ref.BlockInfo(1, False, 1)
GLASS = ref.BlockInfo(0, False, 0)
WATER_B = ref.BlockInfo(ref.WATER, True, ref.WATER)
LOGGED_STAIR = ref.BlockInfo(13, True, ref.WATER)


def column(blocks: dict[int, ref.BlockInfo], min_y: int = 0) -> ref.Column:
    top = max((y for y, b in blocks.items() if b is not ref.AIR), default=min_y - 1)  # GLASS == AIR by value
    return ref.walk_column(lambda y: blocks.get(y, ref.AIR), top + 1, min_y, 11)


def test_walk_sees_through_none_blocks() -> None:
    c = column({5: STONE, 6: GRASS, 7: GLASS, 8: GLASS})
    assert c == ref.Column(1, 6, 0)


@pytest.mark.parametrize("depth", [1, 4, 5, 9, 10, 15])
def test_walk_water_depth(depth: int) -> None:
    blocks = {y: WATER_B for y in range(20 - depth + 1, 21)} | {20 - depth: STONE}
    assert column(blocks) == ref.Column(ref.WATER, 20, depth)


def test_walk_waterlogged_and_bottom() -> None:
    assert column({10: STONE, 11: LOGGED_STAIR}) == ref.Column(ref.WATER, 11, 1)
    assert column({10: STONE, 11: ref.BlockInfo(13, True, 13)}) == ref.Column(13, 11, 1)  # sturdy top: oak
    assert column({}) == ref.Column(11, 0, 0)  # empty: bedrock
    assert column({0: GRASS}) == ref.Column(11, 1, 0)  # surface at minY + 1: bedrock
    assert column({0: STONE, 1: GRASS}) == ref.Column(1, 1, 0)
    assert column({y: GLASS for y in range(6)}) == ref.Column(0, 0, 0)  # NONE down to minY
    assert column({y: WATER_B for y in range(8)}) == ref.Column(ref.WATER, 7, 6)  # y 6..1: k stops at minY
    assert column({0: WATER_B, 1: GLASS, 2: GLASS}) == ref.Column(ref.WATER, 0, 0)  # at minY: raw colour
    assert column({-64: STONE, -63: GRASS}, min_y=-64) == ref.Column(1, -63, 0)


# --- shading and scales -------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("depth", "parity", "want"),
    [(0, 0, ref.HIGH), (4, 0, ref.HIGH), (5, 0, ref.NORMAL), (2, 1, ref.HIGH), (3, 1, ref.NORMAL),
     (9, 0, ref.NORMAL), (10, 0, ref.LOW), (6, 1, ref.NORMAL), (7, 1, ref.LOW)],
)  # fmt: skip
def test_water_shading(depth: int, parity: int, want: int) -> None:
    """d2 = depth * 0.1 + parity * 0.2 in doubles, as Java computes it: 7 * 0.1 + 0.2 = 0.9000000000000001 > 0.9
    (LOW) and 3 * 0.1 + 0.2 = 0.5000000000000001 (not HIGH). Implementations must not use exact decimals."""
    assert ref.brightness(ref.WATER, 0.0, 0.0, depth, 0, parity) == want


@pytest.mark.parametrize(
    ("d1", "d0", "scale", "parity", "want"),
    [(64, 64, 0, 0, ref.NORMAL), (64, 64, 0, 1, ref.NORMAL), (65, 64, 0, 0, ref.HIGH), (65, 64, 0, 1, ref.HIGH),
     (63, 64, 0, 0, ref.LOW), (63, 64, 0, 1, ref.LOW), (65, 64, 4, 1, ref.NORMAL), (66, 64, 4, 1, ref.HIGH),
     (64.5, 64, 0, 1, ref.HIGH), (64.5, 64, 0, 0, ref.NORMAL)],
)  # fmt: skip
def test_height_shading(d1: float, d0: float, scale: int, parity: int, want: int) -> None:
    """d3 = (d1 - d0) * 4 / (i + 4) + (parity - 0.5) * 0.4: HIGH above 0.6, LOW below -0.6."""
    assert ref.brightness(1, d1, d0, 0, scale, parity) == want


def test_scale_aggregation_majority_tie_and_means() -> None:
    cols = [ref.Column(8, 10, 0), ref.Column(28, 11, 3), ref.Column(28, 12, 0), ref.Column(8, 13, 2)]
    p = ref.aggregate(cols, 1)  # tie 2:2, colour 8 inserted first
    assert p.colour == 8 and p.d1 == (10 + 11 + 12 + 13) / 4 and p.depth == 5 // 4
    assert ref.aggregate(cols[1:], 1).colour == 28
    many = [ref.Column(1, y, 0) for y in range(-64, 192)]
    assert ref.aggregate(many, 4).d1 == sum(range(-64, 192)) / 256  # exact: i*i is a power of two


def test_footprint_order_is_x_outer_z_inner() -> None:
    cols = [ref.Column(0, 0, 0)] * 256
    cols = list(cols)
    cols[1] = ref.Column(5, 0, 0)  # x=1, z=0
    cols[16] = ref.Column(6, 0, 0)  # x=0, z=1
    cols[0], cols[17] = ref.Column(5, 0, 0), ref.Column(6, 0, 0)
    # footprint order (0,0) (0,1) (1,0) (1,1): 5, 6, 5, 6 -> tie, 5 first
    assert ref.pixels({(0, 0): cols}, 1)[(0, 0)].colour == 5
    cols[0], cols[1] = ref.Column(6, 0, 0), ref.Column(5, 0, 0)  # 6, 6, 5, 5 -> tie, 6 first
    cols[16], cols[17] = ref.Column(6, 0, 0), ref.Column(5, 0, 0)
    assert ref.pixels({(0, 0): cols}, 1)[(0, 0)].colour == 6


def test_missing_north_shades_flat_and_none_is_zero() -> None:
    pix = {(0, 0): ref.Pixel(1, 100.0, 0), (0, 1): ref.Pixel(1, 101.0, 0), (1, 0): ref.Pixel(0, 5.0, 0)}
    assert ref.packed(pix, 0, 0, 0) == 1 * 4 + ref.NORMAL  # no north pixel: flat
    assert ref.packed(pix, 0, 1, 0) == 1 * 4 + ref.HIGH
    assert ref.packed(pix, 1, 0, 0) == 0


def test_negative_coordinates_land_in_the_right_tile() -> None:
    cols = {(-1, -1): [ref.Column(1, 64, 0)] * 256}
    r = ref.render_columns(cols, {(-1, -1): 5023})
    assert set(r.tiles) == {(s, -1, -1) for s in ref.SCALES}
    t0 = r.tiles[(0, -1, -1)]
    assert t0[511 * 512 + 511] and t0[496 * 512 + 496] and not t0[495 * 512 + 495]
    assert r.tiles[(4, -1, -1)][511 * 512 + 511] and sum(map(bool, r.tiles[(4, -1, -1)])) == 1


# --- the even-pixel centre snap -----------------------------------------------------------------------------
@pytest.mark.parametrize(("coord", "scale", "want"), [(0, 0, 0), (1.5, 0, 0), (3, 0, 2), (-0.5, 0, -2), (-1, 2, -8),
                                                      (17, 3, 16), (31.9, 4, 0), (32, 4, 32), (-33, 4, -64)])  # fmt: skip
def test_pocket_centre_is_wayfinding_snap_scale_plus_one(coord: float, scale: int, want: int) -> None:
    import math

    assert ref.pocket_centre(coord, scale) == want
    blocks = 1 << (scale + 1)  # Wayfinding.snap(coord, scale + 1)
    assert want == math.floor(coord) // blocks * blocks
    assert ref.jdiv(want, 1 << scale) % 2 == 0  # centre/i is even: parity k1 + l1 == gx + gz


def test_tiles_equal_vanilla_for_even_centres_only(rendered: tuple[dict[str, bytes], dict]) -> None:
    """The tiles are what vanilla draws on a map with an even-pixel centre (every pixel whose own and northern
    footprint are loaded); with an odd centre the dither and the water checkerboard flip."""
    files, _ = rendered
    pal = ref.palette_for(FIXTURE_VERSION)
    chunks, _, _ = ref.load_dimension(ref.FIXTURE / "dimensions/minecraft/overworld/region", pal)
    tiles = {}
    for rel, data in files.items():
        if rel.startswith("minecraft/overworld/"):
            h, ids = ref.parse_tile(data)
            tiles[(h["scale"], h["tx"], h["tz"])] = ids
    for scale in ref.SCALES:
        pix = ref.pixels(chunks, scale)
        for x, z in ((100, -30), (500, 500), (-3, 7)):
            cx, cz = ref.pocket_centre(x, scale), ref.pocket_centre(z, scale)
            want, got = ref.vanilla_window(pix, cx, cz, scale), ref.tile_window(tiles, cx, cz, scale)
            compared = [(w, g) for w, g in zip(want, got, strict=True) if w is not None]
            assert all(w == g for w, g in compared), (scale, x, z)
            if scale == 0 and (x, z) == (100, -30):
                assert len(compared) > 1000
                odd = ref.vanilla_window(pix, cx + 1, cz, scale)
                shifted = ref.tile_window(tiles, cx + 1, cz, scale)
                assert any(w is not None and w != g for w, g in zip(odd, shifted, strict=True))


# --- tiles --------------------------------------------------------------------------------------------------
def test_tile_format() -> None:
    ids = bytes(range(256)) * 1024
    data = ref.tile_bytes(3, -2, 7, ids, 5023, 1_700_000_000, ref.FLAG_PROVISIONAL)
    assert len(data) == 32 + 512 * 512
    assert data[:8] == b"SWMAPT\x00\x01" and data[8] == 3 and data[9] == 1 and data[10:12] == b"\0\0"
    assert struct.unpack_from("<iiIq", data, 12) == (-2, 7, 5023, 1_700_000_000)
    assert data[32:] == ids
    header, body = ref.parse_tile(data)
    assert header == {
        "scale": 3,
        "flags": 1,
        "tx": -2,
        "tz": 7,
        "data_version": 5023,
        "rendered_at": 1_700_000_000,
    }
    assert body == ids
    for bad in (data[:-1], b"X" + data[1:], data[:8] + b"\x05" + data[9:]):
        with pytest.raises(ValueError, match="tile"):
            ref.parse_tile(bad)
    assert ref.tile_path("minecraft", "overworld", 2, -1, 0) == "minecraft/overworld/s2/-1.0.swt"


def test_rgba_shading() -> None:
    colours = [0, 0x7FB238]
    assert ref.rgba(1, colours) == (0, 0, 0, 0)
    assert [ref.rgba(4 + b, colours)[:3] for b in range(4)] == [(89, 125, 39), (109, 153, 48), (127, 178, 56),
                                                               (67, 94, 29)]  # fmt: skip


# --- region files -------------------------------------------------------------------------------------------
def test_region_compressions_external_and_bad_slots(palette: ref.Palette) -> None:
    def chunk(cx: int, cz: int, x_pos: int | None = None) -> bytes:
        return fx.nbt_bytes({"xPos": fx.int_(cx if x_pos is None else x_pos), "zPos": fx.int_(cz)})

    chunks = {
        0: (fx.stored(chunk(0, 0), 1), 1),
        1: (fx.stored(chunk(1, 0), 2), 2),
        2: (fx.stored(chunk(2, 0), 3), 3),
        3: (struct.pack(">IB", 1, 0x82), 4),
        4: (fx.stored(chunk(4, 0, x_pos=9), 2), 5),
        5: (struct.pack(">IB", 5, 4) + b"LZ4Bl", 6),
        6: (struct.pack(">IB", 1, 0x82), 7),  # external file missing
    }
    data = fx.region_bytes(chunks)
    ext = {(3, 0): zlib.compress(chunk(3, 0))}
    got = list(ref.read_region(data, 0, 0, lambda cx, cz: ext.get((cx, cz))))
    assert [(c.index, c.timestamp, c.nbt is not None) for c in got] == [
        (0, 1, True), (1, 2, True), (2, 3, True), (3, 4, True), (4, 5, False), (5, 6, False), (6, 7, False)
    ]  # fmt: skip
    assert "slot" in str(got[4].error) and "compression 4" in str(got[5].error)
    truncated = data[: 2 * 4096 + 10]
    assert all(c.nbt is None for c in ref.read_region(truncated, 0, 0))
    assert list(ref.read_region(b"", 0, 0)) == []


# --- the golden fixture -------------------------------------------------------------------------------------
def test_fixture_is_small_and_private() -> None:
    files = [p for p in ref.FIXTURE.rglob("*") if p.is_file()]
    assert sum(p.stat().st_size for p in files) <= 5 * 1024 * 1024
    names = {p.relative_to(ref.FIXTURE).as_posix() for p in files}
    assert not [
        n for n in names if not (n.endswith((".mca", ".mcc")) or n in ("README.md", "golden.json"))
    ], names
    assert not any(n.startswith(("players", "playerdata", "level.dat", "entities", "data/")) for n in names)
    for p in files:
        if p.suffix == ".mca":
            rx, rz = (int(v) for v in p.name.split(".")[1:3])
            for raw in ref.read_region(p.read_bytes(), rx, rz, lambda cx, cz, d=p.parent: (
                    (d / f"c.{cx}.{cz}.mcc").read_bytes() if (d / f"c.{cx}.{cz}.mcc").is_file() else None)):  # fmt: skip
                if raw.nbt is not None:
                    assert not fx.identifying_keys(raw.nbt) and not raw.nbt.get("entities"), (p, raw.index)


def test_fixture_matches_golden(rendered: tuple[dict[str, bytes], dict]) -> None:
    files, summary = rendered
    golden = json.loads(ref.GOLDEN.read_text())
    pal = REPO / "server-config" / "keys" / FIXTURE_VERSION / "map-palette.tsv"
    assert golden["palette_sha256"] == hashlib.sha256(pal.read_bytes()).hexdigest(), (
        "palette changed: re-golden"
    )
    assert {p: hashlib.sha256(b).hexdigest() for p, b in files.items()} == golden["tiles"]
    assert summary == golden["summary"]


def test_fixture_coverage(rendered: tuple[dict[str, bytes], dict]) -> None:
    """The fixture exercises what the design asks for: water at several depths, slopes, snow, both dimensions,
    no unknown states, and the synthetic edge cases."""
    files, summary = rendered
    assert summary["unknown_states"] == []
    assert summary["minecraft/overworld"]["chunks"] == 50 and summary["minecraft/the_end"]["chunks"] == 1
    assert summary["smallworld/synthetic"]["skipped"] == {"data_version": 1, "status": 1, "unreadable": 1}
    ow = [ref.parse_tile(b)[1] for p, b in files.items() if p.startswith("minecraft/overworld/s0/")]
    seen = set().union(*(set(ids) for ids in ow))
    assert {ref.WATER * 4 + b for b in (0, 1, 2)} <= seen  # water at every shade
    assert {1 * 4 + b for b in (0, 1, 2)} <= seen or {7 * 4 + b for b in (0, 1, 2)} <= seen  # slopes
    assert 8 * 4 + 1 in seen or 8 * 4 + 2 in seen  # snow
    syn = ref.parse_tile(files["smallworld/synthetic/s0/0.0.swt"])[1]
    assert syn[0] == 1 * 4 + ref.LOW  # x=0: through the glass onto grass, north chunk 6 blocks higher
    assert syn[1:4] == bytes([ref.WATER * 4 + ref.HIGH] * 3)  # depth 1
    assert syn[16 + 3 * 16 + 0] == 0  # chunk (1, 0) column kind 3: glass to the bottom, NONE
    assert syn[16] == 11 * 4 + ref.NORMAL  # chunk (1, 0) empty column: bedrock grey (STONE)


def test_provisional_flag_renders_features_chunks() -> None:
    pal = ref.palette_for(FIXTURE_VERSION)
    files, summary = ref.render_world(ref.FIXTURE, pal, ("smallworld/synthetic",), 7, provisional=True)
    assert summary["smallworld/synthetic"]["chunks"] == 7
    h, ids = ref.parse_tile(files["smallworld/synthetic/s0/0.0.swt"])
    assert h["flags"] == ref.FLAG_PROVISIONAL and h["rendered_at"] == 7 and ids[4 * 16]


def test_render_cli_writes_the_layout(tmp_path: Path) -> None:
    assert ref.main(["render", str(ref.FIXTURE), str(tmp_path), "--dimension", "minecraft/the_end"]) == 0
    got = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.swt"))
    assert got == [f"minecraft/the_end/s{s}/0.0.swt" for s in ref.SCALES]


def test_preview_png(tmp_path: Path) -> None:
    import map_preview

    out = tmp_path / "t.png"
    assert map_preview.main(["--world", str(ref.FIXTURE), "--dimension", "minecraft/the_end", str(out)]) == 0
    data = out.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and b"PLTE" in data and b"tRNS" in data
    tile = tmp_path / "x.swt"
    tile.write_bytes(ref.tile_bytes(0, 0, 0, bytes(512 * 512), 1, 0))
    assert map_preview.main([str(tile), str(tmp_path / "y.png"), "--zoom", "2"]) == 0
    width, height = struct.unpack(">II", (tmp_path / "y.png").read_bytes()[16:24])
    assert (width, height) == (1024, 1024)
