//! Synthetic worlds: every compression, palette form and bit width, damaged regions, and the incremental render
//! against a fresh full render.

mod common;

use common::*;
use smallworld_map::chunk::{self, Outcome};
use smallworld_map::palette::Palette;
use smallworld_map::render::{self, Dimension, Options};
use smallworld_map::tiles::Tile;
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

fn palette() -> Palette {
    Palette::parse(PALETTE).unwrap()
}

const AIR: usize = 0;
const STONE: usize = 1;
const GRASS: usize = 2;
const SAND: usize = 3;
const GLASS: usize = 4;
const WATER: usize = 5;
const LOG_X: usize = 6;
const SLAB_WET: usize = 7;
const PANE_WET: usize = 8;
const GRASS_SNOWY: usize = 9;

fn entries() -> (Vec<Entry>, Vec<bool>) {
    let e = vec![
        Entry::Id("minecraft:air"),
        Entry::Id("minecraft:stone"),
        Entry::Id("minecraft:grass_block"),
        Entry::Id("minecraft:sand"),
        Entry::Id("minecraft:glass"),
        Entry::Id("minecraft:water"),
        Entry::Props("minecraft:oak_log", vec![("axis", "x")]),
        // Only `waterlogged` given: `type` defaults to bottom.
        Entry::Props("minecraft:oak_slab", vec![("waterlogged", "true")]),
        Entry::Props("minecraft:glass_pane", vec![("waterlogged", "true")]),
        Entry::Props("minecraft:grass_block", vec![("snowy", "true")]),
    ];
    let mut air = vec![false; e.len()];
    air[AIR] = true;
    (e, air)
}

/// A varied landscape: hills, a lake with depth, glass, logs, waterlogged slabs and panes.
fn landscape(seed: i32) -> impl Fn(i32, i32, usize, i32, usize) -> usize {
    move |cx, cz, x, y, z| {
        let wx = cx * 16 + x as i32;
        let wz = cz * 16 + z as i32;
        let h = 62
            + ((wx * 7 + wz * 3 + seed).rem_euclid(13) - 6) / 2
            + (wx / 9 + wz / 11).rem_euclid(3);
        let lake = (wx - 20).pow(2) + (wz - 20).pow(2) < 90;
        if y == -64 {
            return STONE;
        }
        if lake {
            let floor = 55 + (wx + wz).rem_euclid(4);
            return if y < floor {
                SAND
            } else if y < 63 {
                WATER
            } else {
                AIR
            };
        }
        if y < h {
            return if (wx + wz + seed).rem_euclid(17) == 0 {
                SAND
            } else {
                STONE
            };
        }
        if y == h {
            return match (wx * 31 + wz * 17 + seed).rem_euclid(23) {
                0 => GLASS,
                1 => LOG_X,
                2 => SLAB_WET,
                3 => PANE_WET,
                4 => GRASS_SNOWY,
                _ => GRASS,
            };
        }
        if y == h + 1 && (wx * 13 + wz).rem_euclid(29) == 0 {
            return GLASS; // a see-through block above the surface
        }
        AIR
    }
}

fn chunk_bytes(cx: i32, cz: i32, seed: i32, status: &str) -> Vec<u8> {
    let (palette, air) = entries();
    let land = landscape(seed);
    let blocks = move |x: usize, y: i32, z: usize| land(cx, cz, x, y, z);
    chunk_nbt(&ChunkSpec {
        x: cx,
        z: cz,
        status,
        min_section: -4,
        sections: 24,
        palette,
        air,
        blocks: &blocks,
        data_version: 5023,
    })
}

/// Writes region (rx, rz) with chunks `chunks` (local indices), compressed with `version`.
fn write_region(world: &Path, rx: i32, rz: i32, chunks: &[usize], version: u8, seed: i32, ts: u32) {
    let dir = world.join("dimensions/minecraft/overworld/region");
    let mut slots = Vec::new();
    for &idx in chunks {
        let (cx, cz) = (rx * 32 + (idx & 31) as i32, rz * 32 + (idx >> 5) as i32);
        let status = if idx % 7 == 3 {
            "minecraft:features"
        } else {
            "minecraft:full"
        };
        let nbt = chunk_bytes(cx, cz, seed, status);
        let payload = match version {
            1 => gzip_stored(&nbt),
            2 => zlib_stored(&nbt),
            3 => nbt,
            4 => lz4_stream(&nbt),
            0x82 => {
                write(&dir.join(format!("c.{cx}.{cz}.mcc")), &zlib_stored(&nbt));
                Vec::new()
            }
            _ => unreachable!(),
        };
        slots.push(Slot {
            idx,
            version,
            payload,
            timestamp: ts,
        });
    }
    write(&dir.join(format!("r.{rx}.{rz}.mca")), &region_file(&slots));
}

fn render(world: &Path, out: &Path, threads: usize, full: bool) -> render::Summary {
    let p = palette();
    let opts = Options {
        world: world.into(),
        out: out.into(),
        palette: &p,
        radius: None,
        threads,
        full,
        now: 1,
    };
    render::render_dimension(&opts, &Dimension::parse("overworld").unwrap()).unwrap()
}

/// Every tile's pixels by path (headers differ only in rendered_at).
fn tiles(out: &Path) -> BTreeMap<String, Vec<u8>> {
    let mut m = BTreeMap::new();
    for s in 0..5 {
        let dir = out.join(format!("minecraft/overworld/s{s}"));
        let Ok(rd) = fs::read_dir(&dir) else { continue };
        for e in rd.flatten() {
            let t = Tile::decode(&fs::read(e.path()).unwrap()).expect("valid tile");
            m.insert(
                format!("s{s}/{}", e.file_name().to_string_lossy()),
                t.pixels,
            );
        }
    }
    m
}

#[test]
fn every_compression_gives_the_same_tiles() {
    let chunks: Vec<usize> = (0..1024).step_by(11).collect();
    let mut results = Vec::new();
    for version in [1u8, 2, 3, 4, 0x82] {
        let base = tmpdir(&format!("comp{version}"));
        write_region(&base.join("w"), 0, 0, &chunks, version, 1, 100);
        let s = render(&base.join("w"), &base.join("o"), 1, false);
        assert_eq!(s.chunks_failed, 0, "compression {version}");
        assert!(s.chunks_full > 50);
        results.push(tiles(&base.join("o")));
    }
    assert!(!results[0].is_empty());
    for r in &results[1..] {
        assert_eq!(r, &results[0]);
    }
}

#[test]
fn columns_match_the_walk_for_every_bit_width() {
    let p = palette();
    for distinct in [1usize, 2, 5, 9, 16, 17, 33, 65, 129, 257, 300] {
        // `distinct` palette entries: stone and sand under made-up property values (ignored: still the default).
        let names = [
            "minecraft:stone",
            "minecraft:sand",
            "minecraft:glass",
            "minecraft:water",
        ];
        let mut palette_entries = vec![Entry::Id("minecraft:air")];
        for i in 0..distinct {
            let v: &'static str = Box::leak(i.to_string().into_boxed_str());
            palette_entries.push(Entry::Props(names[i % names.len()], vec![("bogus", v)]));
        }
        let mut air = vec![false; palette_entries.len()];
        air[0] = true;
        let n = palette_entries.len();
        let blocks = move |x: usize, y: i32, z: usize| {
            if y > 40 + (x as i32 + z as i32) % 7 {
                0
            } else {
                1 + (x * 7 + z * 3 + y.rem_euclid(97) as usize) % (n - 1)
            }
        };
        let nbt = chunk_nbt(&ChunkSpec {
            x: 3,
            z: -2,
            status: "minecraft:full",
            min_section: -4,
            sections: 24,
            palette: palette_entries.clone(),
            air: air.clone(),
            blocks: &blocks,
            data_version: 5023,
        });
        let Outcome::Full {
            columns, unknown, ..
        } = chunk::decode(&p, &nbt)
        else {
            panic!("not full: {distinct}")
        };
        assert_eq!(unknown, 0);
        let info = |b: usize| match &palette_entries[b] {
            Entry::Id(id) | Entry::Props(id, _) => p.default_state(id).unwrap(),
        };
        for z in 0..16 {
            for x in 0..16 {
                let top = (-64..320)
                    .rev()
                    .find(|&y| !air[blocks(x, y, z)])
                    .map_or(-64, |y| y + 1);
                let want = chunk::walk(top, -64, 11, |y| {
                    if (-64..320).contains(&y) {
                        info(blocks(x, y, z))
                    } else {
                        smallworld_map::palette::StateInfo::default()
                    }
                });
                assert_eq!(columns[x + z * 16], want, "distinct={distinct} x={x} z={z}");
            }
        }
    }
}

#[test]
fn damaged_regions_are_counted_not_fatal() {
    let base = tmpdir("damaged");
    let world = base.join("w");
    let dir = world.join("dimensions/minecraft/overworld/region");
    let good = zlib_stored(&chunk_bytes(0, 0, 1, "minecraft:full"));
    let wrong_pos = zlib_stored(&chunk_bytes(5, 5, 1, "minecraft:full"));
    let mut corrupt = good.clone();
    let mid = corrupt.len() / 2;
    corrupt[mid] ^= 0xff;
    let mut file = region_file(&[
        Slot {
            idx: 0,
            version: 2,
            payload: good,
            timestamp: 1,
        },
        Slot {
            idx: 1,
            version: 2,
            payload: wrong_pos,
            timestamp: 1,
        },
        Slot {
            idx: 2,
            version: 2,
            payload: corrupt,
            timestamp: 1,
        },
        Slot {
            idx: 3,
            version: 127,
            payload: b"\x00\x03abc".to_vec(),
            timestamp: 1,
        },
        Slot {
            idx: 4,
            version: 0x82,
            payload: Vec::new(),
            timestamp: 1,
        }, // .mcc missing
    ]);
    // Chunk 5 points past the end of the file, chunk 6 into the header.
    file[20..24].copy_from_slice(&((9999u32 << 8) | 1).to_be_bytes());
    file[24..28].copy_from_slice(&((1u32 << 8) | 1).to_be_bytes());
    write(&dir.join("r.0.0.mca"), &file);
    write(&dir.join("r.1.0.mca"), b"short"); // shorter than a header: no chunks
    let s = render(&world, &base.join("o"), 1, false);
    assert_eq!(s.chunks_full, 1);
    assert_eq!(s.chunks_unsupported, 1);
    assert_eq!(s.chunks_failed, 5);
    // The failed chunks make the region incomplete: it is read again next time.
    let again = render(&world, &base.join("o"), 1, false);
    assert_eq!(again.regions_rendered, 1);
}

#[test]
fn threads_do_not_change_the_result() {
    let base = tmpdir("threads");
    let world = base.join("w");
    let chunks: Vec<usize> = (0..1024)
        .filter(|i| *i < 64 || *i >= 960 || i % 13 == 0)
        .collect();
    for (rx, rz) in [(-1, -1), (0, -1), (-1, 0), (0, 0), (0, 1), (1, 1)] {
        write_region(&world, rx, rz, &chunks, 2, rx * 3 + rz, 50);
    }
    render(&world, &base.join("one"), 1, false);
    render(&world, &base.join("four"), 4, false);
    let one = tiles(&base.join("one"));
    assert_eq!(one.len(), 6 + 4 * 4); // s0: one per region; s1..s4: the four quadrants around 0,0
    assert_eq!(one, tiles(&base.join("four")));
}

#[test]
fn incremental_equals_a_full_render() {
    let base = tmpdir("incremental");
    let world = base.join("w");
    let out = base.join("o");
    // The first and last chunk rows (they carry the shading across region edges) and a sample in between.
    let all: Vec<usize> = (0..1024)
        .filter(|i| *i < 64 || *i >= 960 || i % 9 == 0)
        .collect();
    for (rx, rz) in [(0, -1), (0, 0), (0, 1), (1, 0), (-1, 0)] {
        write_region(&world, rx, rz, &all, 2, 7, 10);
    }
    let first = render(&world, &out, 2, false);
    assert!(first.full_render && first.regions_rendered == 5);
    // Nothing changed: nothing rendered, nothing written.
    let idle = render(&world, &out, 2, false);
    assert_eq!(
        (idle.regions_rendered, idle.tiles_written, idle.generation),
        (0, 0, first.generation)
    );

    // The middle region changes (its bottom row moves, so (0, 1)'s top row must be re-shaded), (-1, 0) disappears
    // and (2, 2) appears.
    write_region(&world, 0, 0, &all, 2, 8, 20);
    fs::remove_file(world.join("dimensions/minecraft/overworld/region/r.-1.0.mca")).unwrap();
    write_region(&world, 2, 2, &all[..100], 2, 9, 20);
    let inc = render(&world, &out, 1, false);
    assert!(!inc.full_render);
    assert_eq!((inc.regions_rendered, inc.regions_removed), (2, 1));
    assert_eq!(inc.generation, first.generation + 1);

    let fresh = base.join("fresh");
    render(&world, &fresh, 1, true);
    assert_eq!(tiles(&out), tiles(&fresh));

    // A rewrite with new timestamps but the same content changes nothing in the tiles; same timestamps (only the
    // mtime moved) doesn't even re-render.
    let before = tiles(&out);
    write_region(&world, 0, 1, &all, 2, 7, 10);
    assert_eq!(render(&world, &out, 1, false).regions_rendered, 0);
    write_region(&world, 0, 1, &all, 2, 7, 30);
    assert_eq!(render(&world, &out, 1, false).regions_rendered, 1);
    assert_eq!(tiles(&out), before);
}

#[test]
fn a_new_world_folder_starts_over() {
    let base = tmpdir("identity");
    let world = base.join("w");
    write_region(&world, 0, 0, &[0, 1, 2], 2, 1, 1);
    let out = base.join("o");
    render(&world, &out, 1, false);
    fs::rename(&world, base.join("old")).unwrap();
    write_region(&world, 3, 3, &[0], 2, 1, 1);
    let s = render(&world, &out, 1, false);
    assert!(s.full_render);
    let t = tiles(&out);
    assert!(
        t.keys().all(|k| !k.starts_with("s0/0.0")),
        "{t:?}",
        t = t.keys().collect::<Vec<_>>()
    );
    let meta = fs::read_to_string(out.join("minecraft/overworld/meta.json")).unwrap();
    assert!(meta.contains("\"format\":1") && meta.contains("\"generation\":"));
}
