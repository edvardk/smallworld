//! The plugin's snapshots (telemetry.py 72-235): `clean_text`, `_num`, `parse_hot`, `merge_hot`,
//! `parse_snapshot`, `_fps`, `SnapshotReader`. A snapshot is a hand-off from a less privileged process
//! (minecraft writes `/run/mc-telemetry`): only known fields, sane types, read with `read_untrusted` (no FIFO, no
//! symlink, capped).
//!
//! Python keeps the parsed snapshot as a dict; here it is typed. v1.5 (both implementations): a bool is never an
//! int (`v`, `ts`, a hot chunk's `cx`/`cz`, spike times), `ts` is within 0..=`MAX_TS_MS`, and a number no field
//! can hold (`NaN`, an infinity, `1e400`, an integer field beyond 64 bits) makes the snapshot a `BadSnapshot`:
//! skipped and logged by the reader, never half-read.

use super::json::{J, OMap, loads_bytes};
use super::{PyExc, TResult};
use regex::Regex;
use smallworld_core::pystr::{isspace, prefix_cp, split_ws};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

pub const MAX_SNAPSHOT_BYTES: usize = 1 << 20;
pub const MAX_HOT: usize = 10;
pub const TEXT_MAX: usize = 200;
/// telemetry.py `MAX_TS_MS`: 9999-12-31T23:59:59.999Z, the last instant `gmtime`/`strftime` handle.
pub const MAX_TS_MS: i64 = 253_402_300_799_999;
/// 2^63 as a float: an integer field's value must be below it (and at least its negative).
const INT_LIMIT: f64 = 9_223_372_036_854_775_808.0;

/// telemetry.py `UUID.match(s)`: `$` without MULTILINE also matches before one final "\n".
pub static UUID: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\n?\z").expect("UUID")
});
/// telemetry.py `NAME.match(s)`.
pub static NAME: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9_]{1,16}\n?\z").expect("NAME"));
/// telemetry.py `SNAP_NAME` (`\d` is any `Nd` digit: Python's str pattern without re.ASCII).
pub static SNAP_NAME: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^snap-(\d{1,12})\.json\n?\z").expect("SNAP_NAME"));

/// telemetry.py `clean_text(text, limit)` for a str: whitespace (U+001C..U+001F too) to spaces, what
/// `isprintable()` refuses dropped (after the first step exactly the bad categories), collapsed, cut.
pub fn clean_str(text: &str, limit: usize) -> String {
    let t: String = text
        .chars()
        .map(|c| if isspace(c) { ' ' } else { c })
        .filter(|&c| !crate::unicode::is_bad_category(c))
        .collect();
    prefix_cp(&split_ws(&t).join(" "), limit).to_string()
}

/// `clean_text(value, limit)`: `str(text or "")` first.
pub fn clean_text(v: Option<&J>, limit: usize) -> String {
    match v {
        Some(v) if v.truthy() => clean_str(&v.py_str(), limit),
        _ => String::new(),
    }
}

/// telemetry.py `BadSnapshot(text)`.
pub fn bad_snapshot(text: String) -> PyExc {
    PyExc::new("BadSnapshot", text)
}

/// telemetry.py `_num(v, what)`: a float for an int or float (not a bool), else None; `BadSnapshot` for a value
/// that isn't finite (`NaN`, an infinity, `1e400`, an integer beyond a float: json.rs reads it as a float).
///
/// # Errors
/// `BadSnapshot("{what} is not a finite number")`.
pub fn num(v: Option<&J>, what: &str) -> TResult<Option<f64>> {
    #[allow(clippy::cast_precision_loss)]
    let x = match v {
        Some(J::Int(n)) => *n as f64,
        Some(J::Float(x)) => *x,
        _ => return Ok(None),
    };
    if x.is_finite() {
        Ok(Some(x))
    } else {
        Err(bad_snapshot(format!("{what} is not a finite number")))
    }
}

/// telemetry.py `_is_int(v)`: a JSON integer, not a bool (json.rs holds 64 bits; a bigger one is a float).
pub fn strict_int(v: Option<&J>) -> Option<i64> {
    match v {
        Some(J::Int(n)) => Some(*n),
        _ => None,
    }
}

/// Python's `int(x)` for a float: truncation toward zero (beyond 64 bits it saturates); `NaN` is a `ValueError`,
/// an infinity an `OverflowError`.
///
/// # Errors
/// Python's exception for a non-finite value.
#[allow(clippy::cast_possible_truncation)]
pub fn py_int(x: f64) -> TResult<i64> {
    if x.is_nan() {
        Err(PyExc::new(
            "ValueError",
            "cannot convert float NaN to integer",
        ))
    } else if x.is_infinite() {
        Err(PyExc::new(
            "OverflowError",
            "cannot convert float infinity to integer",
        ))
    } else {
        Ok(x as i64)
    }
}

/// telemetry.py `_int(v, what)`: `int(_num(v) or 0)` for an integer field, within 64 bits.
///
/// # Errors
/// `BadSnapshot` (not finite, or out of range).
#[allow(clippy::cast_possible_truncation)]
pub fn int_num(v: Option<&J>, what: &str) -> TResult<i64> {
    match num(v, what)? {
        None => Ok(0),
        Some(x) if (-INT_LIMIT..INT_LIMIT).contains(&x) => Ok(x as i64),
        Some(_) => Err(bad_snapshot(format!("{what} is out of range"))),
    }
}

/// telemetry.py `_fps(v)`: 0..10 000 or None.
///
/// # Errors
/// `BadSnapshot` for a value that isn't finite.
pub fn fps(v: Option<&J>) -> TResult<Option<i64>> {
    let Some(n) = num(v, "pl.fps")? else {
        return Ok(None);
    };
    if n < 0.0 {
        Ok(None)
    } else {
        Ok(Some(py_int(n)?.min(10_000)))
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Hot {
    pub w: String,
    /// `cx`/`cz` as the plugin wrote them (integers: a bool is refused since v1.5).
    pub cx: J,
    pub cz: J,
    /// ent, tiles, vil, items (`HOT_KEYS`).
    pub counts: [i64; 4],
}

impl Hot {
    pub fn ent(&self) -> i64 {
        self.counts[0]
    }
    pub fn tiles(&self) -> i64 {
        self.counts[1]
    }
    pub fn to_j(&self) -> J {
        OMap::new()
            .with("w", self.w.as_str())
            .with("cx", self.cx.clone())
            .with("cz", self.cz.clone())
            .with("ent", self.counts[0])
            .with("tiles", self.counts[1])
            .with("vil", self.counts[2])
            .with("items", self.counts[3])
            .into()
    }
}

/// telemetry.py `parse_hot(raw)`.
///
/// # Errors
/// `BadSnapshot` (a count that isn't finite or is out of range).
pub fn parse_hot(raw: Option<&J>) -> TResult<Vec<Hot>> {
    let Some(J::List(items)) = raw else {
        return Ok(Vec::new());
    };
    let mut out = Vec::new();
    for c in items.iter().take(MAX_HOT) {
        let Some(c) = c.as_obj() else { continue };
        let Some(J::Str(w)) = c.get("w") else {
            continue;
        };
        let (Some(cx), Some(cz)) = (c.get("cx"), c.get("cz")) else {
            continue;
        };
        if strict_int(Some(cx)).is_none() || strict_int(Some(cz)).is_none() {
            continue;
        }
        let count = |k: &str| int_num(c.get(k), &format!("srv.hot.{k}")).map(|n| n.max(0));
        out.push(Hot {
            w: clean_str(w, 64),
            cx: cx.clone(),
            cz: cz.clone(),
            counts: [
                count("ent")?,
                count("tiles")?,
                count("vil")?,
                count("items")?,
            ],
        });
    }
    Ok(out)
}

/// telemetry.py `merge_hot(snaps)`.
pub fn merge_hot(snaps: &[Snapshot]) -> Vec<Hot> {
    let mut by: Vec<Hot> = Vec::new();
    for s in snaps {
        for c in &s.srv.hot {
            let i = if let Some(i) = by.iter().position(|a| {
                a.w == c.w
                    && a.cx.as_py_int() == c.cx.as_py_int()
                    && a.cz.as_py_int() == c.cz.as_py_int()
            }) {
                i
            } else {
                by.push(Hot {
                    w: c.w.clone(),
                    cx: c.cx.clone(),
                    cz: c.cz.clone(),
                    counts: [0; 4],
                });
                by.len() - 1
            };
            for k in 0..4 {
                by[i].counts[k] = by[i].counts[k].max(c.counts[k]);
            }
        }
    }
    let idx: Vec<usize> = (0..by.len()).collect();
    let key = |i: &usize| (by[*i].ent(), by[*i].tiles());
    let mut ranked = idx.clone();
    ranked.sort_by_key(|i| std::cmp::Reverse(key(i)));
    let mut tiles = idx;
    tiles.sort_by(|a, b| by[*b].tiles().cmp(&by[*a].tiles()));
    let mut keep: Vec<usize> = ranked.iter().take(MAX_HOT / 2).copied().collect();
    for &c in tiles.iter().chain(ranked.iter()) {
        if keep.len() >= MAX_HOT {
            break;
        }
        if !keep.contains(&c) && (by[c].ent() != 0 || by[c].tiles() != 0) {
            keep.push(c);
        }
    }
    keep.sort_by_key(|i| std::cmp::Reverse(key(i)));
    keep.into_iter().map(|i| by[i].clone()).collect()
}

#[derive(Clone, Debug, PartialEq)]
pub struct World {
    pub n: String,
    pub chunks: i64,
    pub entities: i64,
    pub tiles: i64,
    pub ticking: i64,
    pub players: i64,
}

impl World {
    pub fn to_j(&self) -> J {
        OMap::new()
            .with("n", self.n.as_str())
            .with("chunks", self.chunks)
            .with("entities", self.entities)
            .with("tiles", self.tiles)
            .with("ticking", self.ticking)
            .with("players", self.players)
            .into()
    }
}

/// The 14 numeric server fields, in parse order.
pub const SRV_KEYS: [&str; 14] = [
    "tps",
    "ticks",
    "mean",
    "p50",
    "p95",
    "p99",
    "p999",
    "max",
    "self_p99_us",
    "self_max_us",
    "budget_skips",
    "lost_windows",
    "heap_used_mb",
    "heap_max_mb",
];

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Srv {
    /// `SRV_KEYS` values (`_num`).
    pub nums: [Option<f64>; 14],
    pub spikes: Vec<(i64, f64)>,
    pub worlds: Vec<World>,
    pub scanned: i64,
    pub hot: Vec<Hot>,
}

impl Srv {
    pub fn get(&self, key: &str) -> Option<f64> {
        SRV_KEYS
            .iter()
            .position(|k| *k == key)
            .and_then(|i| self.nums[i])
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Player {
    pub u: String,
    pub n: String,
    pub ping: i64,
    pub brand: String,
    pub view: i64,
    pub send: i64,
    pub loc: String,
    pub w: String,
    pub cx: i64,
    pub cz: i64,
    pub ent: i64,
    pub glide: bool,
    pub tips: bool,
    pub join: i64,
    pub fps: Option<i64>,
}

impl Player {
    /// The player's dict (`Telemetry.online()`'s values).
    pub fn to_j(&self) -> J {
        OMap::new()
            .with("u", self.u.as_str())
            .with("n", self.n.as_str())
            .with("ping", self.ping)
            .with("brand", self.brand.as_str())
            .with("view", self.view)
            .with("send", self.send)
            .with("loc", self.loc.as_str())
            .with("w", self.w.as_str())
            .with("cx", self.cx)
            .with("cz", self.cz)
            .with("ent", self.ent)
            .with("glide", self.glide)
            .with("tips", self.tips)
            .with("join", self.join)
            .with("fps", self.fps)
            .into()
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Snapshot {
    pub seq: i64,
    pub ts: i64,
    pub win_ms: i64,
    pub srv: Srv,
    pub pl: Vec<Player>,
}

impl Snapshot {
    /// The dict `parse_snapshot` returns, in its key order.
    pub fn to_j(&self) -> J {
        let mut srv = OMap::new();
        for (i, k) in SRV_KEYS.iter().enumerate() {
            srv.insert(*k, self.srv.nums[i]);
        }
        srv.insert(
            "spikes",
            J::List(
                self.srv
                    .spikes
                    .iter()
                    .map(|(t, ms)| J::List(vec![J::Int(*t), J::Float(*ms)]))
                    .collect(),
            ),
        );
        srv.insert(
            "worlds",
            J::List(self.srv.worlds.iter().map(World::to_j).collect()),
        );
        srv.insert("scanned", self.srv.scanned);
        srv.insert("hot", J::List(self.srv.hot.iter().map(Hot::to_j).collect()));
        OMap::new()
            .with("seq", self.seq)
            .with("ts", self.ts)
            .with("win_ms", self.win_ms)
            .with("srv", srv)
            .with("pl", J::List(self.pl.iter().map(Player::to_j).collect()))
            .into()
    }
}

/// telemetry.py `parse_snapshot(data)`: `Ok(None)` where Python returns None.
///
/// # Errors
/// `BadSnapshot` (v1.5): a number no field can hold.
pub fn parse_snapshot(data: &[u8]) -> TResult<Option<Snapshot>> {
    let Some(raw) = loads_bytes(data) else {
        return Ok(None);
    };
    let Some(raw) = raw.as_obj() else {
        return Ok(None);
    };
    if strict_int(raw.get("v")) != Some(1) {
        return Ok(None);
    }
    let Some(ts) = strict_int(raw.get("ts")).filter(|t| (0..=MAX_TS_MS).contains(t)) else {
        return Ok(None);
    };
    let empty = OMap::new();
    let srv = raw.get("srv").and_then(J::as_obj).unwrap_or(&empty);
    let mut out = Srv::default();
    for (i, k) in SRV_KEYS.iter().enumerate() {
        out.nums[i] = num(srv.get(k), &format!("srv.{k}"))?;
    }
    if let Some(J::List(spikes)) = srv.get("spikes") {
        for s in spikes.iter().take(32) {
            if let J::List(pair) = s
                && pair.len() == 2
                && let Some(t) = strict_int(Some(&pair[0]))
                && let Some(ms) = num(Some(&pair[1]), "srv.spikes")?
            {
                out.spikes.push((t, ms));
            }
        }
    }
    if let Some(J::List(worlds)) = srv.get("worlds") {
        for w in worlds.iter().take(16) {
            if let Some(w) = w.as_obj()
                && let Some(J::Str(n)) = w.get("n")
            {
                out.worlds.push(World {
                    n: clean_str(n, 64),
                    chunks: int_num(w.get("chunks"), "srv.worlds.chunks")?,
                    entities: int_num(w.get("entities"), "srv.worlds.entities")?,
                    tiles: int_num(w.get("tiles"), "srv.worlds.tiles")?,
                    ticking: int_num(w.get("ticking"), "srv.worlds.ticking")?,
                    players: int_num(w.get("players"), "srv.worlds.players")?,
                });
            }
        }
    }
    out.scanned = int_num(srv.get("scanned"), "srv.scanned")?;
    out.hot = parse_hot(srv.get("hot"))?;
    let mut players = Vec::new();
    if let Some(J::List(pl)) = raw.get("pl") {
        for p in pl.iter().take(500) {
            let Some(p) = p.as_obj() else { continue };
            let (Some(J::Str(u)), Some(J::Str(n))) = (p.get("u"), p.get("n")) else {
                continue;
            };
            if !UUID.is_match(u) || !NAME.is_match(n) {
                continue;
            }
            players.push(Player {
                u: u.clone(),
                n: n.clone(),
                ping: int_num(p.get("ping"), "pl.ping")?,
                brand: clean_text(p.get("brand"), 32),
                view: int_num(p.get("view"), "pl.view")?,
                send: int_num(p.get("send"), "pl.send")?,
                loc: clean_text(p.get("loc"), 16),
                w: clean_text(p.get("w"), 64),
                cx: int_num(p.get("cx"), "pl.cx")?,
                cz: int_num(p.get("cz"), "pl.cz")?,
                ent: int_num(p.get("ent"), "pl.ent")?,
                glide: p.get("glide") == Some(&J::Bool(true)),
                tips: p.get("tips") != Some(&J::Bool(false)),
                join: int_num(p.get("join"), "pl.join")?,
                fps: fps(p.get("fps"))?,
            });
        }
    }
    Ok(Some(Snapshot {
        seq: int_num(raw.get("seq"), "seq")?,
        ts,
        win_ms: int_num(raw.get("win_ms"), "win_ms")?,
        srv: out,
        pl: players,
    }))
}

/// telemetry.py `SnapshotReader`: new snapshots since the last read, by sequence number; a restarted plugin (seq
/// back at 0, a newer ts) is noticed and followed.
#[derive(Debug)]
pub struct SnapshotReader {
    pub directory: PathBuf,
    pub last_seq: i64,
    pub last_ts: i64,
    /// Bad snapshots already logged (forgotten once they leave the directory).
    pub skipped: std::collections::HashSet<String>,
}

impl SnapshotReader {
    pub fn new(directory: impl Into<PathBuf>) -> Self {
        Self {
            directory: directory.into(),
            last_seq: -1,
            last_ts: 0,
            skipped: std::collections::HashSet::new(),
        }
    }

    fn names(dir: &Path) -> Option<Vec<(i64, String)>> {
        let rd = std::fs::read_dir(dir).ok()?;
        let mut names = Vec::new();
        for e in rd {
            let Ok(e) = e else { return None };
            let Ok(name) = e.file_name().into_string() else {
                continue;
            };
            if let Some(m) = SNAP_NAME.captures(&name) {
                // \d{1,12}: at most 12 digits, always within i64
                let seq = crate::unicode::parse_decimal(&m[1]).unwrap_or(i64::MAX);
                names.push((seq, name));
            }
        }
        Some(names)
    }

    /// `read()`: a `BadSnapshot` is logged once and skipped (v1.5); within one read, a snapshot older than the
    /// one before it is dropped (v1.5).
    ///
    /// # Errors
    /// None since v1.5 (the signature is kept for the callers).
    pub fn read(&mut self) -> TResult<Vec<Snapshot>> {
        let Some(mut names) = Self::names(&self.directory) else {
            return Ok(Vec::new());
        };
        names.sort();
        self.skipped.retain(|n| names.iter().any(|(_, m)| m == n));
        let mut out: Vec<Snapshot> = Vec::new();
        let mut last_ts = self.last_ts;
        for (seq, name) in &names {
            if *seq <= self.last_seq {
                continue;
            }
            let Ok(data) =
                crate::data::read_untrusted(&self.directory.join(name), MAX_SNAPSHOT_BYTES, false)
            else {
                continue;
            };
            match parse_snapshot(&data) {
                Ok(Some(snap)) if snap.ts > last_ts => {
                    last_ts = snap.ts;
                    out.push(snap);
                }
                Ok(_) => {}
                Err(e) => {
                    if self.skipped.insert(name.clone()) {
                        println!("telemetry: skipped {name}: {}", e.text);
                    }
                }
            }
        }
        if out.is_empty()
            && let Some((newest, _)) = names.iter().max()
            && *newest < self.last_seq
        {
            self.last_seq = -1;
            return self.read();
        }
        if let Some(last) = out.last() {
            self.last_seq = last.seq;
            self.last_ts = last.ts;
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clean_text_like_python() {
        assert_eq!(
            clean_str("evil\tbrand\n  x\u{1f}y\u{7}z\u{ad}", 200),
            "evil brand x yz"
        );
        assert_eq!(clean_text(Some(&J::Int(5)), 8), "5");
        assert_eq!(clean_text(Some(&J::Int(0)), 8), "");
        assert_eq!(clean_text(Some(&J::List(vec![J::Bool(true)])), 8), "[True]");
        assert_eq!(clean_str("abcdef", 3), "abc");
    }

    #[test]
    fn nums_like_python() {
        assert_eq!(num(Some(&J::Bool(true)), "x"), Ok(None));
        assert_eq!(num(Some(&J::Int(3)), "x"), Ok(Some(3.0)));
        assert_eq!(int_num(Some(&J::Float(-2.7)), "x"), Ok(-2));
        assert_eq!(fps(Some(&J::Float(12345.0))), Ok(Some(10_000)));
        assert_eq!(fps(Some(&J::Int(-1))), Ok(None));
        let bad = |r: TResult<Option<i64>>| r.unwrap_err();
        assert_eq!(
            bad(fps(Some(&J::Float(f64::NEG_INFINITY)))),
            bad_snapshot("pl.fps is not a finite number".into())
        );
        assert_eq!(
            int_num(Some(&J::Float(f64::NAN)), "pl.ping").unwrap_err(),
            bad_snapshot("pl.ping is not a finite number".into())
        );
        assert_eq!(
            int_num(Some(&J::Float(1e19)), "seq").unwrap_err(),
            bad_snapshot("seq is out of range".into())
        );
        assert_eq!(int_num(Some(&J::Float(-INT_LIMIT)), "seq"), Ok(i64::MIN));
    }

    /// v1.5: a number no field can hold makes the snapshot a `BadSnapshot` (float fields too: never sent as
    /// non-standard JSON); a bool is not an int; `ts` stays in range.
    #[test]
    fn bad_numbers_bools_and_times_refuse_the_snapshot() {
        let raw = r#"{"v": 1, "ts": 5, "srv": {"tps": 20}, "pl": [{"u": "3f2a0000-0000-4000-8000-000000000c41", "n": "Em_K", "ping": Infinity}]}"#;
        let e = parse_snapshot(raw.as_bytes()).unwrap_err();
        assert_eq!(
            (e.class.as_str(), e.text.as_str()),
            ("BadSnapshot", "pl.ping is not a finite number")
        );
        let e =
            parse_snapshot(br#"{"v": 1, "ts": 5, "srv": {"heap_used_mb": 1e400}}"#).unwrap_err();
        assert_eq!(e.text, "srv.heap_used_mb is not a finite number");
        for raw in [
            r#"{"v": true, "ts": 5}"#,
            r#"{"v": 1.0, "ts": 5}"#,
            r#"{"v": 1, "ts": true}"#,
            r#"{"v": 1, "ts": -1}"#,
            r#"{"v": 1, "ts": 253402300800000}"#,
        ] {
            assert_eq!(parse_snapshot(raw.as_bytes()), Ok(None), "{raw}");
        }
        let s = parse_snapshot(
            br#"{"v": 1, "ts": 253402300799999, "srv": {"spikes": [[true, 5], [7, 5]], "hot": [{"w": "w", "cx": true, "cz": 1}, {"w": "w", "cx": 1, "cz": 1}]}}"#,
        )
        .unwrap()
        .unwrap();
        assert_eq!((s.srv.spikes.len(), s.srv.hot.len()), (1, 1));
    }

    /// v1.5: within one read a snapshot older than the one before it is dropped; a bad one is skipped (logged
    /// once) and the reader moves on.
    #[test]
    fn the_reader_keeps_time_order_and_skips_bad_snapshots() {
        let dir = std::env::temp_dir().join(format!("sw-snapreader-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let write = |seq: i64, body: &str| {
            std::fs::write(dir.join(format!("snap-{seq}.json")), body).unwrap();
        };
        write(1, r#"{"v": 1, "seq": 1, "ts": 1000}"#);
        write(2, r#"{"v": 1, "seq": 2, "ts": 500}"#);
        write(3, r#"{"v": 1, "seq": 3, "ts": 2000, "srv": {"p99": NaN}}"#);
        write(4, r#"{"v": 1, "seq": 4, "ts": 3000}"#);
        let mut r = SnapshotReader::new(&dir);
        let got: Vec<i64> = r.read().unwrap().iter().map(|s| s.seq).collect();
        assert_eq!(got, [1, 4]);
        assert!(r.skipped.contains("snap-3.json"));
        std::fs::remove_file(dir.join("snap-3.json")).unwrap();
        assert!(r.read().unwrap().is_empty());
        assert!(r.skipped.is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
