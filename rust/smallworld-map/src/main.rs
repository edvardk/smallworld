//! smallworld-map: the world map pre-renderer's command line.
//!
//! ```text
//! smallworld-map render --world DIR --out DIR [--palette FILE] [--dimensions overworld,end] [--radius N]
//!                       [--threads N] [--full]
//! smallworld-map stats  [--world DIR] [--out DIR] [--dimensions overworld,end]
//! ```
//!
//! `render` prints one JSON summary line per dimension (for the journal); exit status 0 when every dimension was
//! rendered (chunks that couldn't be read are counted in the summary and retried next run), 1 on an error (no
//! palette, an unwritable output, another render running), 2 on a usage error.

#![forbid(unsafe_code)]

use smallworld_map::palette::Palette;
use smallworld_map::region::{self, Region};
use smallworld_map::render::{self, Dimension, Options};
use smallworld_map::{nbt, tiles};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

const USAGE: &str = "usage:
  smallworld-map render --world DIR --out DIR [--palette FILE] [--dimensions overworld,end] [--radius N]
                        [--threads N] [--full]
  smallworld-map stats  [--world DIR] [--out DIR] [--dimensions overworld,end]";
/// Where the deploy installs the palette (ansible role `map`).
const DEFAULT_PALETTE: &str = "/opt/minecraft/share/map-palette.tsv";
const DEFAULT_DIMENSIONS: &str = "overworld,the_end";

#[derive(Debug, Default)]
struct Args {
    command: String,
    world: Option<PathBuf>,
    out: Option<PathBuf>,
    palette: Option<PathBuf>,
    dimensions: Vec<Dimension>,
    radius: Option<i64>,
    threads: usize,
    full: bool,
}

fn parse_args(argv: &[String]) -> Result<Args, String> {
    let mut a = Args {
        threads: 1,
        ..Args::default()
    };
    let mut it = argv.iter();
    a.command.clone_from(it.next().ok_or("no command")?);
    if !matches!(a.command.as_str(), "render" | "stats") {
        return Err(format!("unknown command {:?}", a.command));
    }
    let mut dims = DEFAULT_DIMENSIONS.to_owned();
    while let Some(flag) = it.next() {
        let (name, inline) = match flag.split_once('=') {
            Some((n, v)) if n.starts_with("--") => (n, Some(v.to_owned())),
            _ => (flag.as_str(), None),
        };
        if name == "--full" {
            a.full = true;
            continue;
        }
        let value = match inline {
            Some(v) => v,
            None => it.next().ok_or(format!("{name} needs a value"))?.clone(),
        };
        match name {
            "--world" => a.world = Some(value.into()),
            "--out" => a.out = Some(value.into()),
            "--palette" => a.palette = Some(value.into()),
            "--dimensions" => dims = value,
            "--radius" => {
                let r: i64 = value
                    .parse()
                    .map_err(|_| format!("--radius: not a number: {value:?}"))?;
                if !(1..=30_000_000).contains(&r) {
                    return Err("--radius must be 1..30000000".into());
                }
                a.radius = Some(r);
            }
            "--threads" => {
                a.threads = value
                    .parse()
                    .map_err(|_| format!("--threads: not a number: {value:?}"))?;
                if !(1..=64).contains(&a.threads) {
                    return Err("--threads must be 1..64".into());
                }
            }
            _ => return Err(format!("unknown option {name}")),
        }
    }
    for d in dims.split(',').filter(|d| !d.is_empty()) {
        let dim = Dimension::parse(d).ok_or(format!("--dimensions: bad dimension {d:?}"))?;
        if !a.dimensions.contains(&dim) {
            a.dimensions.push(dim);
        }
    }
    if a.dimensions.is_empty() {
        return Err("--dimensions: none given".into());
    }
    if a.command == "render" && (a.world.is_none() || a.out.is_none()) {
        return Err("render needs --world and --out".into());
    }
    if a.command == "stats" && a.world.is_none() && a.out.is_none() {
        return Err("stats needs --world and/or --out".into());
    }
    Ok(a)
}

fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs() as i64)
}

fn render(a: &Args) -> Result<(), String> {
    let palette_path = a.palette.clone().unwrap_or_else(|| DEFAULT_PALETTE.into());
    let text = fs::read_to_string(&palette_path)
        .map_err(|e| format!("{}: {e}", palette_path.display()))?;
    let palette = Palette::parse(&text).map_err(|e| format!("{}: {e}", palette_path.display()))?;
    let world = a.world.clone().expect("checked");
    let out = a.out.clone().expect("checked");
    if !world.is_dir() {
        return Err(format!("{}: not a directory", world.display()));
    }
    let work = out.join(".work");
    fs::create_dir_all(&work).map_err(|e| format!("{}: {e}", work.display()))?;
    let lock = fs::File::create(work.join("lock")).map_err(|e| format!("lock: {e}"))?;
    match lock.try_lock() {
        Ok(()) => {}
        Err(fs::TryLockError::WouldBlock) => {
            return Err("another render is running (lock held)".into());
        }
        Err(fs::TryLockError::Error(e)) => return Err(format!("lock: {e}")),
    }
    let opts = Options {
        world,
        out,
        palette: &palette,
        radius: a.radius,
        threads: a.threads,
        full: a.full,
        now: now(),
    };
    for dim in &a.dimensions {
        let start = Instant::now();
        let summary =
            render::render_dimension(&opts, dim).map_err(|e| format!("{}: {e}", dim.id()))?;
        println!("{}", summary.json(dim, start.elapsed().as_secs_f64()));
    }
    Ok(())
}

/// Chunk counts of a dimension's region files: by status and by compression.
fn world_stats(world: &Path, dim: &Dimension) -> String {
    let (dir, _, _) = render::paths(world, Path::new("/"), dim);
    let mut regions = 0;
    let mut statuses: BTreeMap<String, u64> = BTreeMap::new();
    let mut compressions: BTreeMap<String, u64> = BTreeMap::new();
    let mut failed = 0u64;
    let mut entries: Vec<_> = fs::read_dir(&dir)
        .map(|d| d.flatten().collect())
        .unwrap_or_default();
    entries.sort_by_key(fs::DirEntry::file_name);
    for e in entries {
        let Some((rx, rz)) = e.file_name().to_str().and_then(region::parse_name) else {
            continue;
        };
        let Ok(mut r) = Region::open(&e.path(), rx, rz) else {
            failed += 1;
            continue;
        };
        regions += 1;
        for idx in 0..1024 {
            match r.read_chunk(idx) {
                Ok(None) => {}
                Ok(Some(raw)) => {
                    *compressions.entry(raw.compression.to_string()).or_default() += 1;
                    let status = nbt::parse(&raw.nbt).ok().and_then(|root| {
                        root.string("Status")
                            .map(|s| String::from_utf8_lossy(s).into_owned())
                    });
                    *statuses
                        .entry(status.unwrap_or_else(|| "?".into()))
                        .or_default() += 1;
                }
                Err(region::ChunkError::Unsupported(v)) => {
                    *compressions.entry(v.to_string()).or_default() += 1;
                }
                Err(_) => failed += 1,
            }
        }
    }
    let map = |m: &BTreeMap<String, u64>| {
        m.iter()
            .map(|(k, v)| format!("\"{}\":{v}", k.replace(['"', '\\'], "")))
            .collect::<Vec<_>>()
            .join(",")
    };
    format!(
        "{{\"dimension\":\"{}\",\"source\":\"world\",\"regions\":{regions},\"statuses\":{{{}}},\"compressions\":{{{}}},\"failed\":{failed}}}",
        dim.id(),
        map(&statuses),
        map(&compressions)
    )
}

fn out_stats(out: &Path, dim: &Dimension) -> String {
    let (_, dim_out, _) = render::paths(Path::new("/"), out, dim);
    let counts = render::tile_counts(&dim_out);
    let bytes: u64 = (0..5)
        .filter_map(|s| fs::read_dir(dim_out.join(format!("s{s}"))).ok())
        .flat_map(Iterator::flatten)
        .filter_map(|e| e.metadata().ok())
        .map(|m| m.len())
        .sum();
    let mut lit = 0u64;
    for s in 0..5 {
        if let Ok(d) = fs::read_dir(dim_out.join(format!("s{s}"))) {
            for e in d
                .flatten()
                .filter(|e| s == 0 && e.file_name().to_str().is_some_and(|n| n.ends_with(".swt")))
            {
                if let Some(t) = fs::read(e.path())
                    .ok()
                    .and_then(|d| tiles::Tile::decode(&d))
                {
                    lit += t.pixels.iter().filter(|&&p| p != 0).count() as u64;
                }
            }
        }
    }
    let meta = fs::read_to_string(dim_out.join("meta.json")).unwrap_or_default();
    let generation = meta
        .split("\"generation\":")
        .nth(1)
        .and_then(|r| r.split(',').next())
        .unwrap_or("null")
        .trim()
        .to_owned();
    let tiles: Vec<String> = counts
        .iter()
        .enumerate()
        .map(|(s, n)| format!("\"{s}\":{n}"))
        .collect();
    format!(
        "{{\"dimension\":\"{}\",\"source\":\"out\",\"generation\":{generation},\"tiles\":{{{}}},\"bytes\":{bytes},\"pixels_s0\":{lit}}}",
        dim.id(),
        tiles.join(",")
    )
}

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    if argv.first().is_some_and(|a| a == "--help" || a == "-h") {
        println!("{USAGE}");
        return ExitCode::SUCCESS;
    }
    let args = match parse_args(&argv) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("smallworld-map: {e}\n{USAGE}");
            return ExitCode::from(2);
        }
    };
    let result = match args.command.as_str() {
        "render" => render(&args),
        _ => {
            for dim in &args.dimensions {
                if let Some(w) = &args.world {
                    println!("{}", world_stats(w, dim));
                }
                if let Some(o) = &args.out {
                    println!("{}", out_stats(o, dim));
                }
            }
            Ok(())
        }
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("smallworld-map: {e}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(s: &str) -> Result<Args, String> {
        parse_args(&s.split_whitespace().map(String::from).collect::<Vec<_>>())
    }

    #[test]
    fn parses_render() {
        let a = args("render --world w --out o --dimensions overworld,end,overworld --radius 5000 --threads 2 --full").unwrap();
        assert_eq!(a.dimensions.len(), 2);
        assert_eq!((a.radius, a.threads, a.full), (Some(5000), 2, true));
        assert!(args("render --world w").is_err());
        assert!(args("render --world w --out o --threads 0").is_err());
        assert!(args("render --world w --out o --dimensions ../x").is_err());
        assert!(args("bogus").is_err());
        assert!(args("stats").is_err());
        assert_eq!(args("stats --out=o").unwrap().dimensions.len(), 2);
    }
}
