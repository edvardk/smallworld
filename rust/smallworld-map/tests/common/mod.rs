//! Test helpers: a tiny NBT writer, a region-file builder, a deterministic PRNG and a test palette.
#![allow(dead_code)]

use std::path::Path;

/// xorshift64*: deterministic, no dependencies.
pub struct Rng(pub u64);

impl Rng {
    pub fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    pub fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
}

/// An NBT value to write.
#[derive(Clone)]
pub enum T {
    Byte(i8),
    Int(i32),
    Long(i64),
    Str(String),
    List(u8, Vec<T>),
    Comp(Vec<(String, T)>),
    Longs(Vec<i64>),
}

impl T {
    pub fn id(&self) -> u8 {
        match self {
            T::Byte(_) => 1,
            T::Int(_) => 3,
            T::Long(_) => 4,
            T::Str(_) => 8,
            T::List(..) => 9,
            T::Comp(_) => 10,
            T::Longs(_) => 12,
        }
    }

    pub fn write(&self, o: &mut Vec<u8>) {
        match self {
            T::Byte(v) => o.push(*v as u8),
            T::Int(v) => o.extend_from_slice(&v.to_be_bytes()),
            T::Long(v) => o.extend_from_slice(&v.to_be_bytes()),
            T::Str(s) => {
                o.extend_from_slice(&(s.len() as u16).to_be_bytes());
                o.extend_from_slice(s.as_bytes());
            }
            T::List(id, items) => {
                o.push(*id);
                o.extend_from_slice(&(items.len() as i32).to_be_bytes());
                for i in items {
                    i.write(o);
                }
            }
            T::Comp(entries) => {
                for (k, v) in entries {
                    o.push(v.id());
                    T::Str(k.clone()).write(o);
                    v.write(o);
                }
                o.push(0);
            }
            T::Longs(v) => {
                o.extend_from_slice(&(v.len() as i32).to_be_bytes());
                for x in v {
                    o.extend_from_slice(&x.to_be_bytes());
                }
            }
        }
    }

    pub fn root(&self) -> Vec<u8> {
        let mut o = vec![10, 0, 0];
        self.write(&mut o);
        o
    }
}

pub fn comp(entries: Vec<(&str, T)>) -> T {
    T::Comp(
        entries
            .into_iter()
            .map(|(k, v)| (k.to_owned(), v))
            .collect(),
    )
}

/// Packs values into longs the SimpleBitStorage way (no spanning).
pub fn pack(values: &[u64], bits: u32) -> Vec<i64> {
    let per = (64 / bits) as usize;
    let mut out = vec![0u64; values.len().div_ceil(per)];
    for (i, v) in values.iter().enumerate() {
        out[i / per] |= v << ((i % per) as u32 * bits);
    }
    out.into_iter().map(|v| v as i64).collect()
}

pub fn ceil_log2(n: usize) -> u32 {
    if n <= 1 {
        0
    } else {
        usize::BITS - (n - 1).leading_zeros()
    }
}

/// A palette entry: a bare id, `{id, properties}`, or the wrapped `{"": id}` form.
#[derive(Clone)]
pub enum Entry {
    Id(&'static str),
    Props(&'static str, Vec<(&'static str, &'static str)>),
}

impl Entry {
    fn to_nbt(&self, wrapped: bool) -> T {
        match self {
            Entry::Id(id) if wrapped => comp(vec![("", T::Str((*id).into()))]),
            Entry::Id(id) => T::Str((*id).into()),
            Entry::Props(id, props) => {
                let p = props
                    .iter()
                    .map(|(k, v)| (*k, T::Str((*v).into())))
                    .collect();
                comp(vec![("id", T::Str((*id).into())), ("properties", comp(p))])
            }
        }
    }
}

/// A synthetic chunk: `blocks(x, y, z)` returns an index into `palette`; sections from `min_section` for
/// `sections` sections; the heightmap is WORLD_SURFACE computed from the blocks (`is_air` entries skipped).
pub struct ChunkSpec<'a> {
    pub x: i32,
    pub z: i32,
    pub status: &'a str,
    pub min_section: i32,
    pub sections: i32,
    pub palette: Vec<Entry>,
    pub air: Vec<bool>,
    pub blocks: &'a dyn Fn(usize, i32, usize) -> usize,
    pub data_version: i32,
}

pub fn chunk_nbt(c: &ChunkSpec<'_>) -> Vec<u8> {
    let min_y = c.min_section * 16;
    let height = (c.sections * 16) as usize;
    let mut secs = vec![comp(vec![("Y", T::Byte((c.min_section - 1) as i8))])]; // light-only section below
    let mut heights = vec![0u64; 256];
    for s in 0..c.sections {
        let mut used: Vec<usize> = Vec::new();
        let mut idx = vec![0u64; 4096];
        for y in 0..16 {
            for z in 0..16 {
                for x in 0..16 {
                    let wy = min_y + s * 16 + y as i32;
                    let b = (c.blocks)(x, wy, z);
                    if !c.air[b] {
                        let h = (wy - min_y + 1) as u64;
                        heights[x + z * 16] = heights[x + z * 16].max(h);
                    }
                    let pos = used.iter().position(|&u| u == b).unwrap_or_else(|| {
                        used.push(b);
                        used.len() - 1
                    });
                    idx[(y * 16 + z) * 16 + x] = pos as u64;
                }
            }
        }
        let mixed = used
            .iter()
            .any(|&u| matches!(c.palette[u], Entry::Props(..)));
        let has_string = used.iter().any(|&u| matches!(c.palette[u], Entry::Id(..)));
        let wrapped = mixed && has_string;
        let entries: Vec<T> = used.iter().map(|&u| c.palette[u].to_nbt(wrapped)).collect();
        let list_id = entries[0].id();
        let mut bs = vec![("palette", T::List(list_id, entries))];
        if used.len() > 1 {
            bs.push(("data", T::Longs(pack(&idx, ceil_log2(used.len()).max(4)))));
        }
        secs.push(comp(vec![
            ("Y", T::Byte((c.min_section + s) as i8)),
            ("block_states", comp(bs)),
        ]));
    }
    secs.push(comp(vec![(
        "Y",
        T::Byte((c.min_section + c.sections) as i8),
    )]));
    let hbits = ceil_log2(height + 1);
    comp(vec![
        ("DataVersion", T::Int(c.data_version)),
        ("xPos", T::Int(c.x)),
        ("zPos", T::Int(c.z)),
        ("yPos", T::Int(c.min_section)),
        ("Status", T::Str(c.status.into())),
        ("LastUpdate", T::Long(1)),
        (
            "Heightmaps",
            comp(vec![("WORLD_SURFACE", T::Longs(pack(&heights, hbits)))]),
        ),
        ("sections", T::List(10, secs)),
        ("block_entities", T::List(0, vec![])),
    ])
    .root()
}

/// A stored-only zlib stream (valid zlib that any inflater reads).
pub fn zlib_stored(data: &[u8]) -> Vec<u8> {
    let mut o = vec![0x78, 0x01];
    let chunks: Vec<&[u8]> = if data.is_empty() {
        vec![&[][..]]
    } else {
        data.chunks(65535).collect()
    };
    for (i, c) in chunks.iter().enumerate() {
        o.push(u8::from(i + 1 == chunks.len()));
        o.extend_from_slice(&(c.len() as u16).to_le_bytes());
        o.extend_from_slice(&(!(c.len() as u16)).to_le_bytes());
        o.extend_from_slice(c);
    }
    o.extend_from_slice(&smallworld_map::compress::adler32(data).to_be_bytes());
    o
}

/// A gzip member around stored deflate blocks.
pub fn gzip_stored(data: &[u8]) -> Vec<u8> {
    let z = zlib_stored(data);
    let mut o = vec![0x1f, 0x8b, 8, 0, 0, 0, 0, 0, 0, 255];
    o.extend_from_slice(&z[2..z.len() - 4]);
    o.extend_from_slice(&smallworld_map::compress::crc32(data).to_le_bytes());
    o.extend_from_slice(&(data.len() as u32).to_le_bytes());
    o
}

/// lz4-java's block stream: one LZ4 block (a greedy matcher) + the end block.
pub fn lz4_stream(data: &[u8]) -> Vec<u8> {
    let block = lz4_compress(data);
    let mut o = b"LZ4Block".to_vec();
    o.push(0x20);
    o.extend_from_slice(&(block.len() as i32).to_le_bytes());
    o.extend_from_slice(&(data.len() as i32).to_le_bytes());
    let check =
        smallworld_map::compress::xxh32(data, smallworld_map::compress::LZ4_SEED) & 0x0FFF_FFFF;
    o.extend_from_slice(&check.to_le_bytes());
    o.extend_from_slice(&block);
    o.extend_from_slice(b"LZ4Block");
    o.push(0x10);
    o.extend_from_slice(&[0; 12]);
    o
}

fn lz4_len(o: &mut Vec<u8>, mut n: usize) {
    while n >= 255 {
        o.push(255);
        n -= 255;
    }
    o.push(n as u8);
}

/// A simple LZ4 block compressor (4-byte hash, greedy), last 5 bytes as literals.
pub fn lz4_compress(src: &[u8]) -> Vec<u8> {
    let mut o = Vec::new();
    let mut table = std::collections::HashMap::new();
    let mut anchor = 0;
    let mut i = 0;
    let limit = src.len().saturating_sub(12);
    while i < limit {
        let key = &src[i..i + 4];
        if let Some(&j) = table.get(key)
            && i - j <= 65535
        {
            let mut m = 4;
            while i + m < src.len() - 5 && src[j + m] == src[i + m] {
                m += 1;
            }
            let lit = i - anchor;
            let ml = m - 4;
            o.push(((lit.min(15) as u8) << 4) | ml.min(15) as u8);
            if lit >= 15 {
                lz4_len(&mut o, lit - 15);
            }
            o.extend_from_slice(&src[anchor..i]);
            o.extend_from_slice(&((i - j) as u16).to_le_bytes());
            if ml >= 15 {
                lz4_len(&mut o, ml - 15);
            }
            table.insert(key, i);
            i += m;
            anchor = i;
            continue;
        }
        table.insert(key, i);
        i += 1;
    }
    let lit = src.len() - anchor;
    o.push((lit.min(15) as u8) << 4);
    if lit >= 15 {
        lz4_len(&mut o, lit - 15);
    }
    o.extend_from_slice(&src[anchor..]);
    o
}

/// One chunk in a region file to build.
pub struct Slot {
    pub idx: usize,
    pub version: u8,
    pub payload: Vec<u8>,
    pub timestamp: u32,
}

/// Builds a region file: header + sectors (payload: u32 length incl. the version byte, version, data).
pub fn region_file(slots: &[Slot]) -> Vec<u8> {
    let mut o = vec![0u8; 8192];
    let mut sector = 2u32;
    for s in slots {
        let mut body = ((s.payload.len() + 1) as u32).to_be_bytes().to_vec();
        body.push(s.version);
        body.extend_from_slice(&s.payload);
        let count = body.len().div_ceil(4096) as u32;
        body.resize(count as usize * 4096, 0);
        o[s.idx * 4..s.idx * 4 + 4].copy_from_slice(&((sector << 8) | count).to_be_bytes());
        o[4096 + s.idx * 4..4100 + s.idx * 4].copy_from_slice(&s.timestamp.to_be_bytes());
        o.extend_from_slice(&body);
        sector += count;
    }
    o
}

/// The palette used by the synthetic tests (a few real 26.3 lines).
pub const PALETTE: &str = "M\tformat\t1\nM\tdata_version\t5023\nM\tminecraft_version\t26.3\n\
D\tminecraft:air\t-\t0\t0\t0\n\
D\tminecraft:stone\t-\t11\t0\t11\n\
D\tminecraft:bedrock\t-\t11\t0\t11\n\
D\tminecraft:grass_block\tsnowy=false\t1\t0\t1\n\
S\tminecraft:grass_block\tsnowy=true\t1\t0\t1\n\
D\tminecraft:sand\t-\t2\t0\t2\n\
D\tminecraft:glass\t-\t0\t0\t0\n\
D\tminecraft:water\tlevel=0\t12\t1\t12\n\
D\tminecraft:oak_log\taxis=y\t13\t0\t13\n\
S\tminecraft:oak_log\taxis=x\t34\t0\t34\n\
S\tminecraft:oak_log\taxis=z\t34\t0\t34\n\
D\tminecraft:oak_slab\ttype=bottom,waterlogged=false\t13\t0\t13\n\
S\tminecraft:oak_slab\ttype=bottom,waterlogged=true\t13\t1\t12\n\
S\tminecraft:oak_slab\ttype=double,waterlogged=true\t13\t1\t13\n\
D\tminecraft:glass_pane\twaterlogged=false\t0\t0\t0\n\
S\tminecraft:glass_pane\twaterlogged=true\t0\t1\t12\n";

pub fn write(path: &Path, data: &[u8]) {
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, data).unwrap();
}

/// A fresh temporary directory (no tempfile crate), removed with everything in it when the guard is dropped,
/// so a run leaves nothing behind in the system temp directory (a failing test's assert unwinds through the drop
/// too).
pub struct TmpDir(std::path::PathBuf);

impl std::ops::Deref for TmpDir {
    type Target = Path;
    fn deref(&self) -> &Path {
        &self.0
    }
}

impl Drop for TmpDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

pub fn tmpdir(name: &str) -> TmpDir {
    let d = std::env::temp_dir().join(format!("smallworld-map-test-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    TmpDir(d)
}
