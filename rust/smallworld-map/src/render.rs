//! The render pipeline for one dimension (docs/reference.md "World map"):
//!
//! 1. List `region/r.X.Z.mca` (within `--radius` of 0,0) and compare each with its saved state: unchanged mtime and
//!    size, or unchanged chunk timestamps, means nothing to do. A world folder with another identity, a different
//!    palette or radius, or `--full` starts over from an empty output.
//! 2. Workers (`--threads`, `std::thread::scope`) take the changed regions in north-to-south order, read every
//!    chunk, walk the columns and aggregate the five scales. The top row is shaded with the northern region's
//!    bottom row: the one just rendered when it is ready, the saved one otherwise (fixed in step 4).
//! 3. The main thread patches each region's pixel blocks into its tiles (a scale-s tile holds 2^s × 2^s regions)
//!    and writes a tile as soon as every region it waits for has arrived, so only a few tiles are in memory. A
//!    chunk that couldn't be read keeps its previous pixels and leaves the region incomplete (retried next run).
//! 4. Top rows whose northern neighbour changed after they were shaded are re-shaded from the saved aggregates.
//! 5. Region states, the dimension state (generation + 1) and `meta.json` are written last, so a crash only means
//!    redoing work.

use crate::chunk::{self, Outcome};
use crate::palette::{Palette, fnv1a};
use crate::region::{self, ChunkError, Region};
use crate::scale::{self, REGION, RegionColumns, SCALES};
use crate::state::{DimState, Edges, RegionState, region_state_path};
use crate::tiles::{self, SIDE, Tile};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Mutex, mpsc};

/// Bump when the rendering changes: every cached state is then stale.
pub const RENDERER_VERSION: u64 = 1;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Dimension {
    pub namespace: String,
    pub name: String,
}

impl Dimension {
    /// `overworld`, `end`/`the_end`, `nether`/`the_nether` or a namespaced id (`minecraft:the_end`).
    #[must_use]
    pub fn parse(s: &str) -> Option<Dimension> {
        let (ns, name) = s.split_once(':').unwrap_or(("minecraft", s));
        let name = match (ns, name) {
            ("minecraft", "end") => "the_end",
            ("minecraft", "nether") => "the_nether",
            _ => name,
        };
        let ok = |p: &str| {
            !p.is_empty()
                && p.bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"_-.".contains(&b))
        };
        (ok(ns) && ok(name) && name != "." && name != "..").then(|| Dimension {
            namespace: ns.into(),
            name: name.into(),
        })
    }

    #[must_use]
    pub fn id(&self) -> String {
        format!("{}:{}", self.namespace, self.name)
    }
}

pub struct Options<'a> {
    pub world: PathBuf,
    pub out: PathBuf,
    pub palette: &'a Palette,
    pub radius: Option<i64>,
    pub threads: usize,
    pub full: bool,
    pub now: i64,
}

#[derive(Default, Debug, Clone)]
pub struct Summary {
    pub regions: usize,
    pub regions_rendered: usize,
    pub regions_removed: usize,
    pub chunks: u64,
    pub chunks_full: u64,
    pub chunks_not_full: u64,
    pub chunks_failed: u64,
    pub chunks_unsupported: u64,
    pub chunks_other_version: u64,
    pub unknown_states: u64,
    pub tiles_written: u64,
    pub tiles_empty: u64,
    pub generation: u64,
    pub full_render: bool,
}

impl Summary {
    #[must_use]
    pub fn json(&self, dim: &Dimension, seconds: f64) -> String {
        format!(
            "{{\"dimension\":\"{}\",\"full_render\":{},\"regions\":{},\"regions_rendered\":{},\"regions_removed\":{},\
             \"chunks\":{},\"chunks_full\":{},\"chunks_not_full\":{},\"chunks_failed\":{},\"chunks_unsupported\":{},\
             \"chunks_other_version\":{},\"unknown_states\":{},\"tiles_written\":{},\"tiles_empty\":{},\"generation\":{},\
             \"seconds\":{:.3}}}",
            dim.id(),
            self.full_render,
            self.regions,
            self.regions_rendered,
            self.regions_removed,
            self.chunks,
            self.chunks_full,
            self.chunks_not_full,
            self.chunks_failed,
            self.chunks_unsupported,
            self.chunks_other_version,
            self.unknown_states,
            self.tiles_written,
            self.tiles_empty,
            self.generation,
            seconds
        )
    }
}

/// `(region dir, output dir, work dir)` of a dimension.
#[must_use]
pub fn paths(world: &Path, out: &Path, dim: &Dimension) -> (PathBuf, PathBuf, PathBuf) {
    (
        world
            .join("dimensions")
            .join(&dim.namespace)
            .join(&dim.name)
            .join("region"),
        out.join(&dim.namespace).join(&dim.name),
        out.join(".work").join(&dim.namespace).join(&dim.name),
    )
}

/// Does the block range `[lo, lo + len)` meet `[-r, r]`?
fn within(lo: i64, len: i64, r: Option<i64>) -> bool {
    r.is_none_or(|r| lo <= r && lo + len > -r)
}

struct Present {
    path: PathBuf,
    mtime_ns: i64,
    size: u64,
}

/// What a worker hands to the writer.
struct Done {
    rx: i32,
    rz: i32,
    /// Per scale: the region's pixel block.
    pixels: Vec<Vec<u8>>,
    /// Chunks (index `x + z * 32`) that keep their previous pixels.
    keep: Vec<usize>,
    state: RegionState,
    /// The top row was shaded with an out-of-date northern edge.
    reshade: bool,
    counts: Summary,
}

fn list_regions(dir: &Path, radius: Option<i64>) -> io::Result<HashMap<(i32, i32), Present>> {
    let mut out = HashMap::new();
    let entries = match fs::read_dir(dir) {
        Ok(e) => e,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(out),
        Err(e) => return Err(e),
    };
    for entry in entries {
        let entry = entry?;
        let Some((rx, rz)) = entry.file_name().to_str().and_then(region::parse_name) else {
            continue;
        };
        if !within(i64::from(rx) * 512, 512, radius) || !within(i64::from(rz) * 512, 512, radius) {
            continue;
        }
        let Ok(meta) = entry.metadata() else { continue };
        if !meta.is_file() {
            continue;
        }
        let mtime_ns = meta
            .mtime()
            .saturating_mul(1_000_000_000)
            .saturating_add(meta.mtime_nsec());
        out.insert(
            (rx, rz),
            Present {
                path: entry.path(),
                mtime_ns,
                size: meta.len(),
            },
        );
    }
    Ok(out)
}

fn known_states(work: &Path) -> HashSet<(i32, i32)> {
    let mut out = HashSet::new();
    if let Ok(entries) = fs::read_dir(work) {
        for e in entries.flatten() {
            let name = e.file_name();
            let Some(n) = name.to_str() else { continue };
            if let Some(rest) = n.strip_suffix(".state").and_then(|r| r.strip_prefix("r."))
                && let Some((x, z)) = rest.split_once('.')
                && let (Ok(x), Ok(z)) = (x.parse(), z.parse())
            {
                out.insert((x, z));
            }
        }
    }
    out
}

/// Reads every chunk of a region into columns.
fn read_region(
    path: &Path,
    rx: i32,
    rz: i32,
    opts: &Options<'_>,
    counts: &mut Summary,
) -> io::Result<(RegionColumns, Vec<usize>, Vec<u32>, i32)> {
    let mut region = Region::open(path, rx, rz)?;
    let mut cols = RegionColumns::default();
    let mut keep = Vec::new();
    let mut stamps = vec![0u32; 1024];
    let mut max_dv = 0;
    for idx in 0..1024 {
        let (cx, cz) = (idx & 31, idx >> 5);
        let bx = i64::from(rx) * 512 + cx as i64 * 16;
        let bz = i64::from(rz) * 512 + cz as i64 * 16;
        stamps[idx] = region.timestamp(idx);
        if !within(bx, 16, opts.radius) || !within(bz, 16, opts.radius) {
            continue;
        }
        match region.read_chunk(idx) {
            Ok(None) => {}
            Ok(Some(raw)) => {
                stamps[idx] = raw.timestamp;
                counts.chunks += 1;
                match chunk::decode(opts.palette, &raw.nbt) {
                    Outcome::Full {
                        columns,
                        data_version,
                        unknown,
                        x,
                        z,
                    } => {
                        counts.unknown_states += u64::from(unknown);
                        if x == rx * 32 + cx as i32 && z == rz * 32 + cz as i32 {
                            counts.chunks_full += 1;
                            max_dv = max_dv.max(data_version);
                            cols.set_chunk(cx, cz, Some(&columns));
                        } else {
                            // A stale sector the header no longer points at consistently: retry next run.
                            counts.chunks_failed += 1;
                            keep.push(idx);
                        }
                    }
                    Outcome::NotFull { .. } => counts.chunks_not_full += 1,
                    Outcome::OtherVersion(_) => counts.chunks_other_version += 1,
                    Outcome::Invalid(_) => {
                        counts.chunks_failed += 1;
                        keep.push(idx);
                    }
                }
            }
            Err(ChunkError::Unsupported(_)) => {
                counts.chunks += 1;
                counts.chunks_unsupported += 1;
            }
            Err(_) => {
                counts.chunks += 1;
                counts.chunks_failed += 1;
                keep.push(idx);
            }
        }
    }
    Ok((cols, keep, stamps, max_dv))
}

/// Patches region `(rx, rz)`'s scale-s pixel block into `tile`, keeping the old pixels of the `keep` chunks.
fn patch_region(tile: &mut Tile, rx: i32, rz: i32, s: u32, block: &[u8], keep: &[usize]) {
    let n = REGION >> s;
    let mask = (1i32 << s) - 1;
    let ox = (rx & mask) as usize * n;
    let oz = (rz & mask) as usize * n;
    if keep.is_empty() {
        tile.patch(ox, oz, n, block);
        return;
    }
    let c = 16 >> s; // pixels per chunk side at this scale (>= 1)
    let kept: HashSet<usize> = keep.iter().copied().collect();
    for z in 0..n {
        for x in 0..n {
            let chunk_idx = (x / c) + (z / c) * 32;
            if !kept.contains(&chunk_idx) {
                tile.pixels[(oz + z) * SIDE + ox + x] = block[z * n + x];
            }
        }
    }
}

/// Patches region `(rx, rz)`'s top pixel row at scale s.
fn patch_top(tile: &mut Tile, rx: i32, rz: i32, s: u32, row: &[u8], keep: &[usize]) {
    let n = REGION >> s;
    let mask = (1i32 << s) - 1;
    let ox = (rx & mask) as usize * n;
    let oz = (rz & mask) as usize * n;
    let c = 16 >> s;
    for x in 0..n {
        if !keep.contains(&(x / c)) {
            tile.pixels[oz * SIDE + ox + x] = row[x];
        }
    }
}

fn tile_key(rx: i32, rz: i32, s: u32) -> (u32, i32, i32) {
    (s, rx >> s, rz >> s)
}

/// The world folder's identity (`st_dev`, `st_ino`): a world switch replaces the folder.
///
/// # Errors
/// The folder can't be stat'ed.
pub fn world_identity(world: &Path) -> io::Result<(u64, u64)> {
    let m = fs::metadata(world)?;
    Ok((m.dev(), m.ino()))
}

/// Renders one dimension.
///
/// # Errors
/// I/O errors on the output (unreadable input is counted, not fatal).
pub fn render_dimension(opts: &Options<'_>, dim: &Dimension) -> io::Result<Summary> {
    let (region_dir, dim_out, work) = paths(&opts.world, &opts.out, dim);
    let (dev, ino) = world_identity(&opts.world)?;
    let mut fp_src = Vec::new();
    fp_src.extend_from_slice(&opts.palette.fingerprint.to_le_bytes());
    fp_src.extend_from_slice(&RENDERER_VERSION.to_le_bytes());
    fp_src.extend_from_slice(format!("{:?}", opts.radius).as_bytes());
    let fingerprint = fnv1a(&fp_src);

    let previous = DimState::load(&work);
    let full = opts.full
        || previous
            .as_ref()
            .is_none_or(|p| p.fingerprint != fingerprint || p.dev != dev || p.ino != ino);
    if full {
        for d in [&dim_out, &work] {
            match fs::remove_dir_all(d) {
                Ok(()) => {}
                Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                Err(e) => return Err(e),
            }
        }
    }
    fs::create_dir_all(&dim_out)?;
    fs::create_dir_all(&work)?;
    let mut summary = Summary {
        full_render: full,
        generation: previous.as_ref().map_or(0, |p| p.generation),
        ..Summary::default()
    };

    let present = list_regions(&region_dir, opts.radius)?;
    summary.regions = present.len();
    let known = known_states(&work);

    // 1. What changed.
    let mut old_states: HashMap<(i32, i32), RegionState> = HashMap::new();
    let mut changed: Vec<(i32, i32)> = Vec::new();
    let mut touched: Vec<((i32, i32), RegionState)> = Vec::new();
    for (&key, p) in &present {
        let Some(st) = RegionState::load(&work, key.0, key.1, fingerprint) else {
            changed.push(key);
            continue;
        };
        if st.complete && st.mtime_ns == p.mtime_ns && st.size == p.size {
            old_states.insert(key, st);
            continue;
        }
        let same = st.complete
            && Region::open(&p.path, key.0, key.1).is_ok_and(|r| r.timestamps() == st.timestamps);
        if same {
            let mut st2 = st.clone();
            st2.mtime_ns = p.mtime_ns;
            st2.size = p.size;
            touched.push((key, st2));
        } else {
            changed.push(key);
        }
        old_states.insert(key, st);
    }
    let removed: Vec<(i32, i32)> = known
        .iter()
        .filter(|k| !present.contains_key(k))
        .copied()
        .collect();
    for &k in &removed {
        if let Some(st) = RegionState::load(&work, k.0, k.1, fingerprint) {
            old_states.insert(k, st);
        }
    }
    changed.sort_by_key(|&(x, z)| (z, x));
    summary.regions_rendered = changed.len();
    summary.regions_removed = removed.len();

    // Tiles wait for every region of this run that lands in them.
    let mut waiting: HashMap<(u32, i32, i32), usize> = HashMap::new();
    for &(rx, rz) in changed.iter().chain(&removed) {
        for s in 0..SCALES {
            *waiting.entry(tile_key(rx, rz, s)).or_default() += 1;
        }
    }
    let changed_set: HashSet<(i32, i32)> = changed.iter().copied().collect();
    let removed_set: HashSet<(i32, i32)> = removed.iter().copied().collect();

    // 2 + 3. Workers render, the main thread writes.
    let finished: Mutex<HashMap<(i32, i32), Vec<Edges>>> = Mutex::new(HashMap::new());
    let next = AtomicUsize::new(0);
    let threads = opts.threads.clamp(1, 64).min(changed.len().max(1));
    let mut cache: HashMap<(u32, i32, i32), Tile> = HashMap::new();
    let mut new_states: HashMap<(i32, i32), RegionState> = HashMap::new();
    let mut reshade: Vec<(i32, i32)> = Vec::new();
    let mut keeps: HashMap<(i32, i32), Vec<usize>> = HashMap::new();
    let mut io_error: Option<io::Error> = None;

    let mut write_block = |cache: &mut HashMap<(u32, i32, i32), Tile>,
                           summary: &mut Summary,
                           rx: i32,
                           rz: i32,
                           s: u32,
                           block: &[u8],
                           keep: &[usize],
                           dv: i32|
     -> io::Result<()> {
        let key = tile_key(rx, rz, s);
        let tile = cache.entry(key).or_insert_with(|| {
            if full {
                Tile::empty(s as u8, key.1, key.2)
            } else {
                tiles::load(&dim_out, s, key.1, key.2)
            }
        });
        patch_region(tile, rx, rz, s, block, keep);
        tile.data_version = tile.data_version.max(dv);
        tile.rendered_at = opts.now;
        let left = waiting.get_mut(&key).expect("tile counted");
        *left -= 1;
        if *left == 0 {
            let t = cache.remove(&key).expect("tile cached");
            if tiles::store(&dim_out, &t)? {
                summary.tiles_written += 1;
            } else {
                summary.tiles_empty += 1;
            }
        }
        Ok(())
    };

    // Removed regions: blank blocks.
    for &(rx, rz) in &removed {
        for s in 0..SCALES {
            let n = REGION >> s;
            if let Err(e) = write_block(
                &mut cache,
                &mut summary,
                rx,
                rz,
                s,
                &vec![0u8; n * n],
                &[],
                0,
            ) {
                io_error.get_or_insert(e);
            }
        }
    }

    std::thread::scope(|scope| {
        let (tx, rx_done) = mpsc::sync_channel::<io::Result<Done>>(threads * 2);
        for _ in 0..threads {
            let tx = tx.clone();
            let (changed, present, finished, next, old_states) =
                (&changed, &present, &finished, &next, &old_states);
            let (changed_set, removed_set) = (&changed_set, &removed_set);
            scope.spawn(move || {
                loop {
                    let i = next.fetch_add(1, Ordering::Relaxed);
                    let Some(&(rx, rz)) = changed.get(i) else {
                        break;
                    };
                    let mut counts = Summary::default();
                    let p = &present[&(rx, rz)];
                    let result = read_region(&p.path, rx, rz, opts, &mut counts).map(
                        |(cols, keep, stamps, dv)| {
                            let north_key = (rx, rz - 1);
                            let (north, stale) = if removed_set.contains(&north_key) {
                                (None, false)
                            } else if changed_set.contains(&north_key) {
                                match finished.lock().expect("lock").get(&north_key) {
                                    Some(e) => (Some(e.clone()), false),
                                    None => {
                                        (old_states.get(&north_key).map(|s| s.edges.clone()), true)
                                    }
                                }
                            } else {
                                (old_states.get(&north_key).map(|s| s.edges.clone()), false)
                            };
                            let mut pixels = Vec::new();
                            let mut edges = Vec::new();
                            for s in 0..SCALES {
                                let r = scale::render(
                                    &cols,
                                    north.as_ref().map(|e| e[s as usize].bottom.as_slice()),
                                    rx,
                                    rz,
                                    s,
                                );
                                pixels.push(r.pixels);
                                edges.push(Edges {
                                    top: r.top,
                                    bottom: r.bottom,
                                });
                            }
                            finished
                                .lock()
                                .expect("lock")
                                .insert((rx, rz), edges.clone());
                            let state = RegionState {
                                fingerprint,
                                mtime_ns: p.mtime_ns,
                                size: p.size,
                                complete: keep.is_empty(),
                                data_version: dv,
                                timestamps: stamps,
                                edges,
                            };
                            Done {
                                rx,
                                rz,
                                pixels,
                                keep,
                                state,
                                reshade: stale,
                                counts,
                            }
                        },
                    );
                    if tx.send(result).is_err() {
                        break;
                    }
                }
            });
        }
        drop(tx);
        for result in rx_done {
            let done = match result {
                Ok(d) => d,
                Err(e) => {
                    io_error.get_or_insert(e);
                    continue;
                }
            };
            let c = &done.counts;
            summary.chunks += c.chunks;
            summary.chunks_full += c.chunks_full;
            summary.chunks_not_full += c.chunks_not_full;
            summary.chunks_failed += c.chunks_failed;
            summary.chunks_unsupported += c.chunks_unsupported;
            summary.chunks_other_version += c.chunks_other_version;
            summary.unknown_states += c.unknown_states;
            for s in 0..SCALES {
                if let Err(e) = write_block(
                    &mut cache,
                    &mut summary,
                    done.rx,
                    done.rz,
                    s,
                    &done.pixels[s as usize],
                    &done.keep,
                    done.state.data_version,
                ) {
                    io_error.get_or_insert(e);
                }
            }
            if done.reshade {
                reshade.push((done.rx, done.rz));
            }
            keeps.insert((done.rx, done.rz), done.keep);
            new_states.insert((done.rx, done.rz), done.state);
        }
    });
    if let Some(e) = io_error {
        return Err(e);
    }

    // 4. Top rows whose northern neighbour changed after they were shaded (or, for unchanged regions, at all).
    let final_edges = |k: (i32, i32)| -> Option<&Vec<Edges>> {
        if removed_set.contains(&k) {
            return None;
        }
        new_states
            .get(&k)
            .or_else(|| old_states.get(&k))
            .map(|s| &s.edges)
    };
    for &(rx, rz) in changed.iter().chain(&removed) {
        let south = (rx, rz + 1);
        if changed_set.contains(&south)
            || removed_set.contains(&south)
            || !present.contains_key(&south)
        {
            continue;
        }
        let before = old_states.get(&(rx, rz)).map(|s| &s.edges);
        let after = final_edges((rx, rz));
        let same = match (before, after) {
            (Some(b), Some(a)) => b.iter().zip(a).all(|(x, y)| x.bottom == y.bottom),
            (None, None) => true,
            _ => false,
        };
        if !same && old_states.contains_key(&south) {
            reshade.push(south);
        }
    }
    reshade.sort_by_key(|&(x, z)| (z, x));
    reshade.dedup();
    let mut patched: HashMap<(u32, i32, i32), Tile> = HashMap::new();
    for &(rx, rz) in &reshade {
        let Some(own) = final_edges((rx, rz)) else {
            continue;
        };
        let north = final_edges((rx, rz - 1));
        let keep = keeps.get(&(rx, rz)).map_or(&[][..], Vec::as_slice);
        let keep_top: Vec<usize> = keep.iter().copied().filter(|&i| i < 32).collect();
        for s in 0..SCALES {
            let row = scale::shade_top(
                &own[s as usize].top,
                north.map(|e| e[s as usize].bottom.as_slice()),
                rx,
                rz,
                s,
            );
            let key = tile_key(rx, rz, s);
            let tile = patched
                .entry(key)
                .or_insert_with(|| tiles::load(&dim_out, s, key.1, key.2));
            patch_top(tile, rx, rz, s, &row, &keep_top);
        }
    }
    for t in patched.values() {
        if tiles::store(&dim_out, t)? {
            summary.tiles_written += 1;
        } else {
            summary.tiles_empty += 1;
        }
    }

    // 5. States last.
    for (k, st) in &new_states {
        st.save(&work, k.0, k.1)?;
    }
    for (k, st) in &touched {
        st.save(&work, k.0, k.1)?;
    }
    for k in &removed {
        let _ = fs::remove_file(region_state_path(&work, k.0, k.1));
    }
    let wrote = summary.tiles_written + summary.tiles_empty > 0 || full || !removed.is_empty();
    if wrote {
        summary.generation += 1;
    }
    DimState {
        fingerprint,
        dev,
        ino,
        generation: summary.generation,
    }
    .save(&work)?;
    if wrote || !dim_out.join("meta.json").exists() {
        write_meta(
            &dim_out,
            dim,
            opts,
            summary.generation,
            (dev, ino),
            summary.regions,
        )?;
    }
    Ok(summary)
}

/// Counts the tiles per scale on disk.
#[must_use]
pub fn tile_counts(dim_out: &Path) -> Vec<usize> {
    (0..SCALES)
        .map(|s| {
            fs::read_dir(dim_out.join(format!("s{s}")))
                .map(|d| {
                    d.flatten()
                        .filter(|e| {
                            e.file_name()
                                .to_str()
                                .is_some_and(|n| n.ends_with(".swt") && !n.starts_with('.'))
                        })
                        .count()
                })
                .unwrap_or(0)
        })
        .collect()
}

fn write_meta(
    dim_out: &Path,
    dim: &Dimension,
    opts: &Options<'_>,
    generation: u64,
    world: (u64, u64),
    regions: usize,
) -> io::Result<()> {
    let counts = tile_counts(dim_out);
    let tiles: Vec<String> = counts
        .iter()
        .enumerate()
        .map(|(s, n)| format!("\"{s}\":{n}"))
        .collect();
    let radius = opts
        .radius
        .map_or_else(|| "null".to_owned(), |r| r.to_string());
    let text = format!(
        "{{\"format\":1,\"dimension\":\"{}\",\"generation\":{generation},\"world\":{{\"dev\":{},\"ino\":{}}},\
         \"minecraft_version\":\"{}\",\"data_version\":{},\"rendered_at\":{},\"radius\":{radius},\"regions\":{regions},\
         \"scales\":[0,1,2,3,4],\"tiles\":{{{}}}}}\n",
        dim.id(),
        world.0,
        world.1,
        opts.palette.minecraft_version.replace(['"', '\\'], ""),
        opts.palette.data_version,
        opts.now,
        tiles.join(",")
    );
    tiles::atomic_write(&dim_out.join("meta.json"), text.as_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dimension_names() {
        assert_eq!(Dimension::parse("end").unwrap().id(), "minecraft:the_end");
        assert_eq!(
            Dimension::parse("overworld").unwrap().id(),
            "minecraft:overworld"
        );
        assert_eq!(Dimension::parse("mydp:sky").unwrap().id(), "mydp:sky");
        assert!(Dimension::parse("../x").is_none());
        assert!(Dimension::parse("..").is_none());
        assert!(Dimension::parse("").is_none());
    }

    #[test]
    fn radius_filter() {
        assert!(within(-512, 512, Some(1)));
        assert!(within(0, 512, Some(1)));
        assert!(!within(512, 512, Some(500)));
        assert!(!within(-1024, 512, Some(512)));
        assert!(within(-1024, 512, None));
    }
}
