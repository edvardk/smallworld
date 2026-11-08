"""v1.4: smallworld-map (rust/smallworld-map) renders exactly what the reference renderer does.

tests/map_reference.py is the executable spec (a straight transcription of vanilla's MapItem.update). Here the
committed Rust binary for this platform (rust/dist/smallworld-map-<target>; SMALLWORLD_MAP_BIN points at another
build, e.g. `cargo build`'s) renders:

- the golden fixture world (tests/fixtures/world-26.3: trimmed real chunks + the synthetic edge-case dimension):
  every tile file byte for byte equal to the reference's, and to the committed golden sha256s;
- seeded synthetic worlds generated here (random real block states from the palette, string / compound / wrapped
  palette entries with missing properties, water, see-through blocks, zlib / gzip / none / external .mcc,
  statuses to skip): both implementations, byte for byte;
- the same worlds incrementally after changes (a region rewritten, one removed): equal to a fresh reference run.

The binary being the source's is scripts/build_rust.py check's job (ci.yml job rust); the crate's own unit, fuzz
and synthetic tests run under `cargo test`.
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import importlib.util
import json
import os
import platform
import random
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from conftest import REPO

RUST = REPO / "rust"
TARGETS = {"x86_64": "x86_64-unknown-linux-musl", "aarch64": "aarch64-unknown-linux-musl"}
REFERENCE = REPO / "tests" / "map_reference.py"
VERSION = "26.3"
PALETTE = REPO / "server-config" / "keys" / VERSION / "map-palette.tsv"
FIXTURE = REPO / "tests" / "fixtures" / f"world-{VERSION}"


def _binary() -> Path | None:
    if os.environ.get("SMALLWORLD_MAP_BIN"):
        return Path(os.environ["SMALLWORLD_MAP_BIN"])
    if sys.platform != "linux" or platform.machine() not in TARGETS:
        return None
    return RUST / "dist" / f"smallworld-map-{TARGETS[platform.machine()]}"


BINARY = _binary()
needs_binary = pytest.mark.skipif(
    BINARY is None or not BINARY.is_file(),
    reason="no smallworld-map binary for this platform (Linux x86_64/aarch64 only; or set SMALLWORLD_MAP_BIN)",
)
needs_reference = pytest.mark.skipif(
    not (REFERENCE.is_file() and PALETTE.is_file()),
    reason="the reference renderer and palette come with the map-prep branch (scripts/map_palette.py)",
)


def reference() -> ModuleType:
    spec = importlib.util.spec_from_file_location("map_reference", REFERENCE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["map_reference"] = mod
    spec.loader.exec_module(mod)
    return mod


def rust_render(world: Path, out: Path, dims: list[str], *extra: str) -> list[dict[str, Any]]:
    """Runs `smallworld-map render`; `dims` as ns/dim. Returns the JSON summary lines."""
    assert BINARY is not None
    argv = [str(BINARY), "render", "--world", str(world), "--out", str(out), "--palette", str(PALETTE)]
    argv += ["--dimensions", ",".join(d.replace("/", ":") for d in dims), "--rendered-at", "0", *extra]
    p = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)
    assert p.returncode == 0, p.stderr
    return [json.loads(line) for line in p.stdout.splitlines()]


def tile_files(out: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(out)): p.read_bytes() for p in sorted(out.rglob("*.swt")) if ".work" not in p.parts
    }


def reference_files(world: Path, dims: tuple[str, ...]) -> dict[str, bytes]:
    ref = reference()
    files, _ = ref.render_world(world, ref.Palette.load(PALETTE), dims, 0)
    return dict(files)


def assert_same(rust: dict[str, bytes], ref: dict[str, bytes]) -> None:
    assert sorted(rust) == sorted(ref)
    bad = [p for p in ref if rust[p] != ref[p]]
    assert not bad, (
        f"{len(bad)} tiles differ, first {bad[0]}: {sum(a != b for a, b in zip(rust[bad[0]], ref[bad[0]], strict=True))} bytes"
    )


# --- the golden fixture -------------------------------------------------------------------------------------------


@needs_binary
@needs_reference
def test_fixture_tiles_equal_the_reference(tmp_path: Path) -> None:
    ref = reference()
    dims = tuple(ref.FIXTURE_DIMENSIONS)
    summaries = rust_render(FIXTURE, tmp_path, list(dims))
    assert [s["dimension"] for s in summaries] == [d.replace("/", ":") for d in dims]
    assert all(s["unknown_states"] == 0 and s["full_render"] for s in summaries)
    rust = tile_files(tmp_path)
    assert rust, "the fixture renders tiles"
    assert_same(rust, reference_files(FIXTURE, dims))
    # meta.json (docs/reference.md "Directory layout"): what mc-brand checks before using the tiles.
    meta = json.loads((tmp_path / "minecraft/overworld/meta.json").read_text())
    st = FIXTURE.stat()
    assert meta["format"] == 1 and meta["generation"] == 1
    assert meta["world"] == {"dev": st.st_dev, "ino": st.st_ino}
    assert meta["palette_sha256"] == hashlib.sha256(PALETTE.read_bytes()).hexdigest()
    assert meta["data_version"] == ref.Palette.load(PALETTE).data_version and meta["rendered_at"] == 0


@needs_binary
@needs_reference
def test_fixture_tiles_equal_the_golden_hashes(tmp_path: Path) -> None:
    golden = json.loads((FIXTURE / "golden.json").read_text())
    assert golden["palette_sha256"] == hashlib.sha256(PALETTE.read_bytes()).hexdigest(), (
        "the palette changed: python tests/map_reference.py golden"
    )
    ref = reference()
    rust_render(FIXTURE, tmp_path, list(ref.FIXTURE_DIMENSIONS))
    got = {p: hashlib.sha256(b).hexdigest() for p, b in tile_files(tmp_path).items()}
    assert got == golden["tiles"]


# --- synthetic worlds -----------------------------------------------------------------------------------------------

END, BYTE, INT, LONG, STRING, LIST, COMPOUND, LONG_ARRAY = 0, 1, 3, 4, 8, 9, 10, 12


def _str(s: str) -> bytes:
    b = s.encode()
    return struct.pack(">H", len(b)) + b


def _tag(v: Any) -> int:
    if isinstance(v, tuple):
        return v[0]
    return {str: STRING, dict: COMPOUND, int: INT}[type(v)]


def _payload(v: Any) -> bytes:
    if isinstance(v, tuple):
        tag, val = v
        if tag == BYTE:
            return struct.pack(">b", val)
        if tag == LONG:
            return struct.pack(">q", val)
        if tag == LONG_ARRAY:
            return struct.pack(f">i{len(val)}q", len(val), *val)
        if tag == LIST:
            elem, items = val
            return struct.pack(">Bi", elem, len(items)) + b"".join(_payload(i) for i in items)
    if isinstance(v, str):
        return _str(v)
    if isinstance(v, int):
        return struct.pack(">i", v)
    assert isinstance(v, dict)
    return b"".join(bytes([_tag(x)]) + _str(k) + _payload(x) for k, x in v.items()) + b"\0"


def nbt(root: dict[str, Any]) -> bytes:
    return b"\x0a" + _str("") + _payload(root)


def pack(values: list[int], bits: int) -> list[int]:
    per = 64 // bits
    out = [0] * ((len(values) + per - 1) // per)
    for i, v in enumerate(values):
        out[i // per] |= v << ((i % per) * bits)
    return [x - (1 << 64) if x >= 1 << 63 else x for x in out]


def ceillog2(n: int) -> int:
    return (n - 1).bit_length() if n > 1 else 0


@functools.cache
def palette_states() -> tuple[list[tuple[str, dict[str, str]]], dict[str, dict[str, str]]]:
    """Every D and S state of the palette file, and each block's default properties."""
    states: list[tuple[str, dict[str, str]]] = []
    defaults: dict[str, dict[str, str]] = {}
    for line in PALETTE.read_text().splitlines():
        f = line.split("\t")
        if f[0] in ("D", "S"):
            props = dict(kv.split("=", 1) for kv in f[2].split(",")) if f[2] != "-" else {}
            states.append((f[1], props))
            if f[0] == "D":
                defaults[f[1]] = props
    return states, defaults


def entry(name: str, props: dict[str, str], defaults: dict[str, str], rng: random.Random) -> Any:
    """A 26.x palette entry: a bare id for a default state, else {id, properties} with the default-valued
    properties sometimes left out."""
    if props == defaults and rng.random() < 0.8:
        return name
    given = {k: v for k, v in props.items() if v != defaults.get(k) or rng.random() < 0.5}
    return {"id": name, "properties": given} if given else {"id": name}


def synthetic_chunk(cx: int, cz: int, rng: random.Random, status: str, dv: int) -> bytes:
    states, defaults = palette_states()
    min_section, sections = -4, 24
    water = ("minecraft:water", defaults["minecraft:water"])
    ground = [s for s in states if not s[0].endswith(("_air", ":air"))]
    surface = [*rng.sample(ground, 6), ("minecraft:glass", {}), water]
    base = 50 + rng.randrange(30)
    secs: list[Any] = [{"Y": (BYTE, min_section - 1)}]
    heights = [0] * 256
    columns = []
    for z in range(16):
        for x in range(16):
            top = base + (x * 3 + z * 5 + rng.randrange(4)) % 9
            kind = rng.randrange(10)
            col: dict[int, tuple[str, dict[str, str]]] = {}
            for y in range(-64, top):
                col[y] = ("minecraft:stone", {})
            if kind < 2:  # a lake column: water from top-depth to top
                for y in range(top - 1 - rng.randrange(12), top):
                    col[y] = water
            elif kind < 4:  # see-through block on top of the surface
                col[top] = ("minecraft:glass", {})
            elif kind < 7:
                col[top - 1] = rng.choice(surface)
            else:
                col[top - 1] = rng.choice(ground)
            columns.append(col)
            heights[x + z * 16] = max(col) - min_section * 16 + 1
    if rng.random() < 0.5:
        # one section with more than 256 distinct states (global-width packing)
        big = rng.sample(ground, 300)
        for i, col in enumerate(columns):
            for y in range(0, 16):
                if y in col:
                    col[y] = big[(i * 16 + y) % len(big)]
    for s in range(sections):
        used: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        idx = []
        for y in range(16):
            for z in range(16):
                for x in range(16):
                    wy = (min_section + s) * 16 + y
                    name, props = columns[x + z * 16].get(wy, ("minecraft:air", {}))
                    key = (name, tuple(sorted(props.items())))
                    if key not in used:
                        used.append(key)
                    idx.append(used.index(key))
        entries = [entry(n, dict(p), defaults.get(n, {}), rng) for n, p in used]
        if any(isinstance(e, dict) for e in entries) and any(isinstance(e, str) for e in entries):
            entries = [{"": e} if isinstance(e, str) else e for e in entries]  # 26.x heterogeneous list
        elem = COMPOUND if isinstance(entries[0], dict) else STRING
        bs: dict[str, Any] = {"palette": (LIST, (elem, entries))}
        if len(used) > 1:
            bs["data"] = (LONG_ARRAY, pack(idx, max(4, ceillog2(len(used)))))
        secs.append({"Y": (BYTE, min_section + s), "block_states": bs})
    secs.append({"Y": (BYTE, min_section + sections)})
    root = {
        "DataVersion": dv,
        "xPos": cx,
        "zPos": cz,
        "yPos": min_section,
        "Status": status,
        "LastUpdate": (LONG, 7),
        "Heightmaps": {"WORLD_SURFACE": (LONG_ARRAY, pack(heights, 9))},
        "sections": (LIST, (COMPOUND, secs)),
    }
    return nbt(root)


def write_region(region_dir: Path, rx: int, rz: int, rng: random.Random, dv: int, ts: int, n: int) -> None:
    """A region with `n` random chunks, mixed compressions, a few proto-chunks and one external .mcc chunk."""
    region_dir.mkdir(parents=True, exist_ok=True)
    header = bytearray(8192)
    body = bytearray()
    for idx in sorted(rng.sample(range(1024), n)):
        cx, cz = rx * 32 + (idx & 31), rz * 32 + (idx >> 5)
        status = "minecraft:full" if rng.random() < 0.85 else "minecraft:features"
        data = synthetic_chunk(cx, cz, rng, status, dv)
        compression = rng.choice([1, 2, 2, 3, 0x82])
        if compression == 0x82:
            (region_dir / f"c.{cx}.{cz}.mcc").write_bytes(zlib.compress(data))
            payload = b""
        else:
            payload = {1: gzip.compress(data, mtime=0), 2: zlib.compress(data), 3: data}[compression]
        sector = 2 + len(body) // 4096
        chunk = struct.pack(">IB", len(payload) + 1, compression) + payload
        chunk += bytes(-len(chunk) % 4096)
        header[idx * 4 : idx * 4 + 4] = struct.pack(">I", (sector << 8) | (len(chunk) // 4096))
        header[4096 + idx * 4 : 4100 + idx * 4] = struct.pack(">I", ts)
        body += chunk
    (region_dir / f"r.{rx}.{rz}.mca").write_bytes(bytes(header) + bytes(body))


@needs_binary
@needs_reference
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_synthetic_worlds_equal_the_reference(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 - reproducible test data
    dv = reference().Palette.load(PALETTE).data_version
    world = tmp_path / "world"
    region_dir = world / "dimensions/minecraft/overworld/region"
    for rx, rz in [(0, 0), (0, 1), (-1, 0), (-1, -1)]:
        write_region(region_dir, rx, rz, rng, dv, 100, 12)
    dims = ("minecraft/overworld",)
    out = tmp_path / "out"
    rust_render(world, out, list(dims))
    assert_same(tile_files(out), reference_files(world, dims))

    # Incremental: one region rewritten (new timestamps), one removed; equal to a fresh reference render.
    write_region(region_dir, 0, 0, rng, dv, 200, 12)
    (region_dir / "r.-1.-1.mca").unlink()
    summary = rust_render(world, out, list(dims))[0]
    assert not summary["full_render"]
    assert (summary["regions_rendered"], summary["regions_removed"]) == (1, 1)
    assert_same(tile_files(out), reference_files(world, dims))
    # Nothing changed: nothing rendered.
    assert rust_render(world, out, list(dims))[0]["regions_rendered"] == 0


@needs_binary
def test_cli_contract(tmp_path: Path) -> None:
    """Usage errors exit 2, a missing palette 1; the lock refuses a concurrent run; stats prints JSON."""
    assert BINARY is not None
    run = lambda *a: subprocess.run([str(BINARY), *a], capture_output=True, text=True, check=False)  # noqa: E731
    assert run("render", "--world", str(tmp_path)).returncode == 2
    assert run("render", "--world", str(tmp_path), "--out", str(tmp_path), "--threads", "0").returncode == 2
    assert (
        run("render", "--world", str(tmp_path), "--out", str(tmp_path), "--dimensions", "../x").returncode
        == 2
    )
    p = run(
        "render", "--world", str(tmp_path), "--out", str(tmp_path / "o"), "--palette", str(tmp_path / "none")
    )
    assert p.returncode == 1 and "none" in p.stderr
    pal = tmp_path / "p.tsv"
    pal.write_text("M\tformat\t1\nM\tdata_version\t5023\nD\tminecraft:bedrock\t-\t11\t0\t11\n")
    out = tmp_path / "o"
    p = run(
        "render",
        "--world",
        str(tmp_path),
        "--out",
        str(out),
        "--palette",
        str(pal),
        "--dimensions",
        "overworld",
    )
    assert p.returncode == 0, p.stderr
    s = json.loads(p.stdout)
    assert s["dimension"] == "minecraft:overworld" and s["regions"] == 0
    stats = run("stats", "--out", str(out), "--dimensions", "overworld")
    assert json.loads(stats.stdout)["source"] == "out"
    assert run("--help").returncode == 0


def test_crate_rules() -> None:
    """The crate follows the workspace's rules (tests/test_rust_rcon.py checks dependencies for every member)."""
    assert "smallworld-map" in (RUST / "Cargo.toml").read_text()
    for f in ("src/main.rs", "src/lib.rs"):
        assert "#![forbid(unsafe_code)]" in (RUST / "smallworld-map" / f).read_text()
    region = (RUST / "smallworld-map/src/region.rs").read_text()
    assert "read_exact_at" in region, "positioned reads only (never mmap: the server truncates region files)"
