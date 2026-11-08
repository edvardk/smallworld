//! Fuzz-style robustness: random, truncated and bit-flipped NBT, compressed streams and region files must give
//! errors or counts, never a panic (a panic aborts the release binary). Deterministic (seeded PRNG), so a failure
//! reproduces.

mod common;

use common::*;
use smallworld_map::palette::Palette;
use smallworld_map::render::{self, Dimension, Options};
use smallworld_map::{chunk, compress, nbt, region};

fn sample_chunk() -> Vec<u8> {
    let palette = vec![
        Entry::Id("minecraft:air"),
        Entry::Id("minecraft:stone"),
        Entry::Props("minecraft:oak_slab", vec![("waterlogged", "true")]),
        Entry::Id("minecraft:water"),
    ];
    let air = vec![true, false, false, false];
    let blocks = |x: usize, y: i32, z: usize| {
        if y < 60 {
            1
        } else if y < 62 + (x % 3) as i32 {
            3
        } else if y == 62 && z == 4 {
            2
        } else {
            0
        }
    };
    chunk_nbt(&ChunkSpec {
        x: 0,
        z: 0,
        status: "minecraft:full",
        min_section: -4,
        sections: 24,
        palette,
        air,
        blocks: &blocks,
        data_version: 5023,
    })
}

#[test]
fn random_bytes_never_panic() {
    let p = Palette::parse(PALETTE).unwrap();
    let mut rng = Rng(0x5eed);
    for _ in 0..20_000 {
        let len = rng.below(200) as usize;
        let mut data: Vec<u8> = (0..len).map(|_| rng.next() as u8).collect();
        if !data.is_empty() && rng.below(2) == 0 {
            data[0] = 10; // make it look like a compound more often
        }
        let _ = nbt::parse(&data);
        let _ = chunk::decode(&p, &data);
        let _ = compress::zlib(&data);
        let _ = compress::gzip(&data);
        let _ = compress::lz4_stream(&data);
        let _ = compress::inflate(&data, 1 << 16);
    }
}

#[test]
fn truncated_and_flipped_chunks_never_panic() {
    let p = Palette::parse(PALETTE).unwrap();
    let good = sample_chunk();
    assert!(matches!(
        chunk::decode(&p, &good),
        chunk::Outcome::Full { .. }
    ));
    let mut rng = Rng(42);
    for cut in (0..good.len()).step_by(7) {
        let _ = chunk::decode(&p, &good[..cut]);
    }
    for _ in 0..3000 {
        let mut d = good.clone();
        for _ in 0..=rng.below(4) {
            let i = rng.below(d.len() as u64) as usize;
            d[i] ^= 1 << rng.below(8);
        }
        let _ = chunk::decode(&p, &d);
    }
    // Compressed forms, truncated and flipped.
    for packed in [zlib_stored(&good), gzip_stored(&good), lz4_stream(&good)] {
        for cut in (0..packed.len()).step_by(97) {
            for v in 1..=4u8 {
                let _ = region::decompress(v, &packed[..cut]);
            }
        }
        for _ in 0..300 {
            let mut d = packed.clone();
            let i = rng.below(d.len() as u64) as usize;
            d[i] ^= 1 << rng.below(8);
            for v in 1..=4u8 {
                if let Ok(out) = region::decompress(v, &d) {
                    let _ = chunk::decode(&p, &out);
                }
            }
        }
    }
}

#[test]
fn damaged_region_files_never_panic() {
    let p = Palette::parse(PALETTE).unwrap();
    let nbt = sample_chunk();
    let slots: Vec<Slot> = (0..6)
        .map(|i| Slot {
            idx: i * 33,
            version: [1, 2, 3, 4, 2, 2][i],
            payload: match i {
                0 => gzip_stored(&nbt),
                3 => lz4_stream(&nbt),
                2 => nbt.clone(),
                _ => zlib_stored(&nbt),
            },
            timestamp: 9,
        })
        .collect();
    let good = region_file(&slots);
    let mut rng = Rng(7);
    let base = tmpdir("fuzz-region");
    for round in 0..120 {
        let mut d = good.clone();
        match round % 4 {
            0 => d.truncate(rng.below(d.len() as u64) as usize),
            1 => {
                for _ in 0..8 {
                    let i = rng.below(8192) as usize; // header damage
                    d[i] = rng.next() as u8;
                }
            }
            2 => {
                for _ in 0..16 {
                    let i = rng.below(d.len() as u64) as usize;
                    d[i] ^= 1 << rng.below(8);
                }
            }
            _ => {
                let i = 8192 + rng.below((d.len() - 8192) as u64) as usize;
                d.truncate(i);
                d.extend((0..rng.below(9000)).map(|_| rng.next() as u8));
            }
        }
        let world = base.join(format!("w{round}"));
        write(
            &world.join("dimensions/minecraft/overworld/region/r.0.0.mca"),
            &d,
        );
        let opts = Options {
            world: world.clone(),
            out: base.join(format!("o{round}")),
            palette: &p,
            radius: None,
            threads: 1,
            full: false,
            now: 0,
        };
        render::render_dimension(&opts, &Dimension::parse("overworld").unwrap())
            .expect("render survives");
    }
}
