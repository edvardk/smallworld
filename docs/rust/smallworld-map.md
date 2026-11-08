# smallworld-map: a Rust walkthrough for Python programmers

`smallworld-map` (v1.4) is the product's second Rust component: the nightly world map pre-renderer. It reads the
world's region files and writes, for every 512 × 512-block region and every map scale, the exact pixels a vanilla
map would show, so mc-brand can paint terrain into pocket maps where nobody has been yet and onto the spawn map
room. Where `smallworld-rcon` (read [its walkthrough](smallworld-rcon.md) first) is about a protocol and Python
compatibility, this crate is about **binary formats, an algorithm that must match Java to the last bit, and
threads**. Like the first crate it has no dependencies: std only, `unsafe` forbidden.

The Python reference renderer `tests/map_reference.py` does the same job in about 700 lines; reading the two side
by side is the quickest way in. `tests/test_rust_map.py` proves they write the same bytes.

## Contents

- [The pipeline in one picture](#the-pipeline-in-one-picture)
- [How the crate is laid out](#how-the-crate-is-laid-out)
- [NBT: a tree that borrows its input](#nbt-a-tree-that-borrows-its-input)
- [Region files and torn reads](#region-files-and-torn-reads)
- [Decompression without a crate](#decompression-without-a-crate)
- [Chunks: palettes and packed longs](#chunks-palettes-and-packed-longs)
- [The colour algorithm](#the-colour-algorithm)
- [Floating point that matches Java and Python](#floating-point-that-matches-java-and-python)
- [Incremental renders](#incremental-renders)
- [Threads: scoped workers and a channel](#threads-scoped-workers-and-a-channel)
- [Errors: counting instead of crashing](#errors-counting-instead-of-crashing)
- [Tests: synthetic worlds, fuzzing, parity](#tests-synthetic-worlds-fuzzing-parity)
- [Build, test and run it yourself](#build-test-and-run-it-yourself)
- [Exercises](#exercises)

## The pipeline in one picture

```
region/r.X.Z.mca ─► header (1024 locations + timestamps)
                 ─► per chunk: sectors ─► decompress (zlib / gzip / none / LZ4Block, or c.X.Z.mcc)
                                       ─► NBT tree ─► DataVersion, Status, yPos, Heightmaps, sections
                                       ─► 256 columns: (colour, y, water depth)   [chunk.rs: walk]
              512 × 512 columns per region
                 ─► per scale s = 0..4: (512 >> s)² pixels, colour * 4 + brightness   [scale.rs]
                 ─► patched into 512 × 512 tiles (a scale-s tile holds 2^s × 2^s regions)  [tiles.rs]
```

## How the crate is laid out

`src/lib.rs` declares the modules; `src/main.rs` is only the command line, so the integration tests in `tests/`
can call the library directly (`use smallworld_map::render`), which a binary-only crate can't offer.

| File | What it does | Python counterpart in map_reference.py |
|---|---|---|
| `nbt.rs` | parses NBT into a borrowed tree | `_Reader`, `parse_nbt`, `unwrap` |
| `compress.rs` | DEFLATE, zlib, gzip, LZ4Block, Adler-32, CRC-32, XXH32 | `zlib`/`gzip` modules (no LZ4) |
| `region.rs` | region header, sectors, `.mcc`, retries on torn reads | `read_region` |
| `palette.rs` | `map-palette.tsv` → `StateInfo` lookups | `Palette` |
| `chunk.rs` | sections, bit unpacking, heightmap, the column walk | `decode_sections`, `walk_column`, `chunk_columns` |
| `scale.rs` | footprint majority, mean height/depth, brightness | `aggregate`, `brightness`, `packed` |
| `tiles.rs` | the tile file format, atomic writes | `tile_bytes`, `parse_tile` |
| `state.rs` | the incremental state files | (none: the reference always renders everything) |
| `render.rs` | the pipeline, threads, tile writing | `render_world` |
| `sha256.rs` | `meta.json`'s `palette_sha256` | `hashlib` |

## NBT: a tree that borrows its input

NBT is Minecraft's binary JSON: a tag byte, a name, a payload; compounds (dicts) and lists nest. In Python,
`parse_nbt` builds dicts of `str` and lists of `int`. Copying every string and every long array of a chunk (the
block data is ~4,000 longs) would be most of the work, so `nbt.rs` returns values that **point into the input**:

```rust
pub enum Value<'a> {
    Int(i32),
    String(&'a [u8]),      // a slice of the decompressed chunk, not a copy
    LongArray(&'a [u8]),   // the raw big-endian bytes; nbt::long_at(raw, i) reads one
    Compound(Compound<'a>),
    // ...
}
```

The `'a` is a **lifetime**: it says "this value lives no longer than the bytes it borrows". The compiler enforces
it, so a `Value` can never outlive the `Vec<u8>` it came from (in Python that bug can't happen because everything
is copied; in C it is a use-after-free). `chunk::decode` parses, picks what it needs and drops the tree before the
bytes go away.

Every read goes through `Reader::take(n)`, which returns `Err(NbtError::Truncated)` when fewer than `n` bytes are
left: no index can panic. A list's element count is checked against the bytes left **before** allocating, and
nesting stops at 512 (vanilla's limit), so a crafted chunk can neither exhaust memory nor the stack.

26.x added a twist: a list that mixes strings and compounds (a block palette with both forms) stores every element
as a compound and wraps a string `s` as `{"": s}`; `unwrap_list_element` undoes it, like the reference's `unwrap`.

## Region files and torn reads

A region file starts with 1024 big-endian `u32` locations (`sector << 8 | count`, sectors of 4096 bytes) and 1024
timestamps. A chunk's payload is a length, a compression byte and the compressed NBT; bit `0x80` of the compression
byte means the stream lives in its own file, `c.<x>.<z>.mcc`.

The server keeps writing while the renderer reads, and it truncates and extends region files. Two rules follow:

- **Positioned reads only** (`std::os::unix::fs::FileExt::read_exact_at`, i.e. `pread`), never `mmap`: a mapped
  page of a file that shrank raises SIGBUS, which kills the process.
- **Read, then check.** After reading a payload, `Region::read_chunk` reads that chunk's header entry again. If
  it changed, the chunk was rewritten while we read it: retry, at most three times, then report `ChunkError::Torn`.
  The caller keeps the chunk's old pixels and marks the region incomplete, so tomorrow's run tries again.

## Decompression without a crate

The workspace rule is "std only" (CLAUDE.md), and Rust's std has no zlib. `compress.rs` therefore carries its own
DEFLATE decoder (RFC 1951): Huffman tables built from code lengths, a 10-bit lookup table for the common short
codes with a bit-by-bit fallback for long ones (the method of zlib's `puff.c`), stored/fixed/dynamic blocks, and
back-references copied byte by byte when they overlap. About 300 lines, and it is the part most likely to hide a
bug, which is why **every stream's checksum is verified**: zlib ends with an Adler-32 of the output, gzip with a
CRC-32 and the length, lz4-java's blocks with an XXH32 masked to 28 bits. A decoder bug shows up as
`CompressError::Checksum` (the chunk is skipped and counted), never as wrong pixels on a map.

Region compression 4 is lz4-java's `LZ4BlockOutputStream` format: blocks with the magic `LZ4Block`, a method nibble
(raw or LZ4), lengths and a checksum. Not the LZ4 *frame* format, so the decoder is hand-written too (~60 lines).
The product's servers never write it (the default is zlib), but imported worlds may.

## Chunks: palettes and packed longs

A chunk is 16 × 16 columns, split into 16-block-high sections. Each section stores a **palette** (the distinct
block states in it) and **indices** into it, 4096 of them, packed into longs: with `b` bits per index,
`64 / b` indices fit in a long, lowest bits first, and an index never spans two longs. `chunk.rs`'s `Bits` reads
index `i` straight from the long array without unpacking all 4096:

```rust
fn get(&self, i: usize) -> usize {
    let v = long_at(self.raw, i / self.per_long) >> ((i % self.per_long) as u32 * self.bits);
    (v & ((1u64 << self.bits) - 1)) as usize
}
```

Most columns stop after one or two blocks of the walk, so decoding only what is read is a large saving. The
heightmap (`WORLD_SURFACE`: one above the top non-air block per column) uses the same packing with
`ceil(log2(height + 1))` bits.

Palette entries are 26.x's: a bare block id means the block's default state, `{id, properties}` names a state,
with missing properties taking the default's values. `palette.rs` turns both into a `StateInfo`: the map colour
(0 = NONE, see-through), whether the state holds a fluid, and the colour recorded when the walk stops on it.
These come from `map-palette.tsv`, dumped from the pinned server jar at build time, so the renderer contains no
block knowledge of its own.

## The colour algorithm

Vanilla's `MapItem.update`, read from the 26.3 bytecode (docs/reference.md "The algorithm"). Per column
(`chunk::walk`):

```rust
let mut y = top;                       // WORLD_SURFACE height + minimum Y
if y <= min_y + 1 { return bedrock }   // empty columns (End void) are STONE grey
loop {                                 // vanilla's do { --y } while (NONE && y > minY)
    y -= 1;
    state = at(y);
    if !(state.colour == 0 && y > min_y) { break; }
}
if y > min_y && state.fluid { /* count the water below; record `surface` (fluid rule) */ }
```

Rust has no `do … while`, so it is a `loop` with the negated condition and `break`, exactly as the Python's
`while True … if not (…): break`.

Per pixel at scale `s` (`scale.rs`): the `2^s × 2^s` footprint's most frequent colour (ties go to the colour seen
first: Guava's `LinkedHashMultiset` + `copyHighestCountFirst`, emulated with a count array and an insertion-order
list), the mean height `d1` and the mean water depth. Brightness compares `d1` with the pixel to the north and
dithers on the pixel's grid parity. Since a footprint never crosses a chunk, and a region is a whole number of
pixels at every scale, each region can be rendered on its own; only its top row needs the region to the north.

## Floating point that matches Java and Python

The brightness thresholds are compared in `f64`: `(d1 - d0) * 4.0 / (i + 4) as f64 + (parity - 0.5) * 0.4 > 0.6`.
Java, Python and Rust all use IEEE-754 doubles with round-to-nearest, so **the same operations in the same order
give the same bits**. Reordering does not: `7 * 0.1 + 0.2` is `0.9000000000000001`, which is *above* 0.9 (a water
pixel at depth 7 on an odd pixel is LOW, not NORMAL). So `brightness` is written exactly as vanilla's expression,
not "simplified". `d1` is a sum of `y / (i*i)`; because `i*i` is a power of two every term and every partial sum
is exact, so summing integers first and dividing once gives the identical double.

## Incremental renders

Rendering a 10,000-block world takes minutes; the next night, players have touched a handful of regions.
`render.rs` keeps per region a small state file (`state.rs`): the file's mtime and size, its 1024 chunk timestamps,
whether every chunk was read, and the **edge rows** at every scale (the top row's colour/height/depth, the bottom
row's height). A run:

1. skips regions whose mtime and size are unchanged, and regions whose timestamps are unchanged;
2. re-renders the others whole, using the northern region's saved bottom row for the top row's shading;
3. re-shades the top row of the region south of a changed one, from its saved top-row aggregates, without reading
   that region at all.

`tests/synthetic.rs::incremental_equals_a_full_render` changes, removes and adds regions and compares every tile
with a fresh full render: the incremental result must be indistinguishable.

## Threads: scoped workers and a channel

```rust
std::thread::scope(|scope| {
    let (tx, rx) = mpsc::sync_channel::<io::Result<Done>>(threads * 2);
    for _ in 0..threads {
        let tx = tx.clone();
        scope.spawn(move || loop {
            let i = next.fetch_add(1, Ordering::Relaxed);   // take the next region
            let Some(&(rx, rz)) = changed.get(i) else { break };
            /* read, decode, aggregate */
            tx.send(result).ok();
        });
    }
    drop(tx);
    for result in rx { /* main thread: patch tiles, write the complete ones */ }
});
```

- `thread::scope` lets the workers **borrow** local data (the palette, the region list) because the scope
  guarantees they finish before it returns; plain `thread::spawn` would need `Arc` and `'static` data.
- An `AtomicUsize` hands out work (like `itertools.count()` shared between threads, but lock-free).
- A **bounded** channel (`sync_channel`) gives back-pressure: workers block when the writer falls behind, so a fast
  machine can't fill memory with finished regions.
- `Mutex<HashMap>` holds the edge rows of regions finished in this run, so a worker can shade its top row with its
  northern neighbour's fresh bottom row.

The writer keeps a tile in memory only until every region of this run that lands in it has arrived (a counter
per tile), and regions are handed out north to south, so a handful of tiles are open at any time.

Rust's compiler checks the sharing: a `HashMap` used from two threads without a `Mutex` doesn't compile ("cannot
be shared between threads safely"). In Python, the GIL hides such bugs; here the type system rules them out.

## Errors: counting instead of crashing

A release build is compiled with `panic = "abort"`: a panic kills the process with no cleanup. So nothing that
reads world data may panic. Two kinds of error:

- **Fatal** (`io::Error` on the output, a bad palette, another run holding the lock): `?` propagates them to
  `main`, which prints them and exits 1.
- **Per chunk** (`ChunkError`, `NbtError`, `CompressError`, `Outcome::Invalid`): matched and counted in the
  summary (`chunks_failed`, `chunks_unsupported`, ...), and the run goes on.

`Option`/`Result` with `let … else` keep this readable:

```rust
let Some(y_pos) = root.int("yPos") else { return Outcome::Invalid("no yPos") };
```

## Tests: synthetic worlds, fuzzing, parity

- **Unit tests** (`#[cfg(test)] mod tests` in each file): checksums against known values, brightness thresholds,
  majority ties, the walk's edge cases, the state file round trip.
- **Synthetic worlds** (`tests/synthetic.rs`, with a small NBT and region *writer* in `tests/common/mod.rs`):
  every compression gives the same tiles; every palette bit width from 4 to the global width; damaged regions are
  counted; four threads give the same bytes as one; incremental = full.
- **Fuzzing** (`tests/fuzz.rs`): 20,000 random inputs and thousands of truncated and bit-flipped chunks, streams
  and region files through every parser. They must not panic. A seeded PRNG makes failures reproducible. The first
  run found an overflow (`yPos * 16` with a crafted `yPos`) and an unbounded walk (a huge heightmap value); both
  are now range-checked.
- **Parity with the reference** (`tests/test_rust_map.py`): the golden fixture and seeded generated worlds,
  rendered by the binary and by `map_reference.py`, compared byte for byte (headers included, with
  `--rendered-at 0`), plus the committed golden sha256s.

## Build, test and run it yourself

```sh
cd rust
cargo test -p smallworld-map                 # unit, synthetic, fuzz
cargo clippy --all-targets -- -D warnings
cargo build --release -p smallworld-map
./target/release/smallworld-map render --world ../tests/fixtures/world-26.3 --out /tmp/map \
    --palette ../server-config/keys/26.3/map-palette.tsv \
    --dimensions overworld,end,smallworld:synthetic --rendered-at 0
./target/release/smallworld-map stats --out /tmp/map
python ../scripts/map_preview.py --help      # a tile as a PNG
cd .. && SMALLWORLD_MAP_BIN=rust/target/release/smallworld-map pytest -q tests/test_rust_map.py
python scripts/build_rust.py build           # after changing the source: rebuild dist/, update the pins
```

## Exercises

### 1. A PNG export

Add `smallworld-map png --tile FILE --colors map-colors.tsv --out FILE.png`: an indexed PNG (`PLTE` with the
248 shaded colours, `tRNS` making 0..3 transparent). You have CRC-32 already (`compress::crc32`); PNG's `IDAT` is a
zlib stream, and the simplest valid one uses *stored* blocks (see `tests/common/mod.rs::zlib_stored`). Check your
output against `scripts/map_preview.py`.

### 2. Render a provisional status

The spec reserves tile flag bit 0 for "provisional chunks drawn" (`features` … `spawn`, chunks not yet `full`).
Add `--provisional`: `chunk::decode` accepts those statuses, the tiles get the flag, and the state fingerprint
includes it (why must it?). Add the same to `map_reference.py`'s call in `tests/test_rust_map.py` and keep the
parity test green.

### 3. Per-chunk incrementality

Today a changed region is re-read whole. Keep a compressed column cache per region (`.work/<ns>/<dim>/r.X.Z.col`)
and re-decode only the chunks whose timestamps changed. You will need a DEFLATE *compressor* (or a simple run-length
encoding of the column arrays: measure which is smaller). The existing
`incremental_equals_a_full_render` test must still pass; add one that changes a single chunk and checks that only
that chunk was decoded (count it in `Summary`).
