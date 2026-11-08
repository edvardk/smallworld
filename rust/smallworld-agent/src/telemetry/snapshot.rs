//! The plugin's snapshots (telemetry.py 72-235): `clean_text`, `_num`, `parse_hot`, `merge_hot`,
//! `parse_snapshot`, `_fps`, `SnapshotReader`. A snapshot is a hand-off from a less privileged process
//! (minecraft writes `/run/mc-telemetry`): only known fields, sane types, read with `read_untrusted` (no FIFO, no
//! symlink, capped).
//!
//! Python keeps the parsed snapshot as a dict; here it is typed. Where Python echoes a raw JSON value, a
//! boolean `ts` (an `int` to Python) reads as 0/1, and an integer beyond 64 bits saturates or (`ts`) is refused
//! (PORTING.md "Divergences"; the plugin writes neither). A hot chunk's boolean `cx`/`cz` is echoed as written.

use super::json::{J, OMap, loads_bytes};
use super::{PyExc, TResult};
use regex::Regex;
use smallworld_core::pystr::{isspace, prefix_cp, split_ws};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

pub const MAX_SNAPSHOT_BYTES: usize = 1 << 20;
pub const MAX_HOT: usize = 10;
pub const TEXT_MAX: usize = 200;

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

/// telemetry.py `_num(v)`: a float for an int or float (not a bool), else None. `NaN` and the infinities pass
/// (Python's json accepts them); `int()` of one raises later, as in Python (a known Python issue, kept for parity:
/// the telemetry round fails at that snapshot).
pub fn num(v: Option<&J>) -> Option<f64> {
    #[allow(clippy::cast_precision_loss)]
    match v? {
        J::Int(n) => Some(*n as f64),
        J::Float(x) => Some(*x),
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

/// `int(_num(v) or 0)` (`NaN` is truthy: it reaches `int()`).
///
/// # Errors
/// As `py_int`.
pub fn int_num(v: Option<&J>) -> TResult<i64> {
    match num(v) {
        None | Some(0.0) => Ok(0),
        Some(x) => py_int(x),
    }
}

/// telemetry.py `_fps(v)`: 0..10 000 or None.
///
/// # Errors
/// As `py_int` (`NaN` and `Infinity` reach `int()`).
pub fn fps(v: Option<&J>) -> TResult<Option<i64>> {
    let Some(n) = num(v) else { return Ok(None) };
    if n < 0.0 {
        Ok(None)
    } else {
        Ok(Some(py_int(n)?.min(10_000)))
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Hot {
    pub w: String,
    /// `cx`/`cz` as the plugin wrote them: an int, or a bool (an int to Python, echoed as written).
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
/// As `py_int`.
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
        if cx.as_py_int().is_none() || cz.as_py_int().is_none() {
            continue;
        }
        let count = |k: &str| int_num(c.get(k)).map(|n| n.max(0));
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
/// What Python raises (`int()` of `NaN` or an infinity).
pub fn parse_snapshot(data: &[u8]) -> TResult<Option<Snapshot>> {
    let Some(raw) = loads_bytes(data) else {
        return Ok(None);
    };
    let Some(raw) = raw.as_obj() else {
        return Ok(None);
    };
    let v_ok = matches!(raw.get("v"), Some(J::Int(1) | J::Bool(true)))
        || matches!(raw.get("v"), Some(J::Float(x)) if x.to_bits() == 1.0_f64.to_bits());
    let Some(ts) = raw.get("ts").and_then(J::as_py_int) else {
        return Ok(None);
    };
    if !v_ok {
        return Ok(None);
    }
    let empty = OMap::new();
    let srv = raw.get("srv").and_then(J::as_obj).unwrap_or(&empty);
    let mut out = Srv::default();
    for (i, k) in SRV_KEYS.iter().enumerate() {
        out.nums[i] = num(srv.get(k));
    }
    if let Some(J::List(spikes)) = srv.get("spikes") {
        for s in spikes.iter().take(32) {
            if let J::List(pair) = s
                && pair.len() == 2
                && let Some(t) = pair[0].as_py_int()
                && let Some(ms) = num(Some(&pair[1]))
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
                    chunks: int_num(w.get("chunks"))?,
                    entities: int_num(w.get("entities"))?,
                    tiles: int_num(w.get("tiles"))?,
                    ticking: int_num(w.get("ticking"))?,
                    players: int_num(w.get("players"))?,
                });
            }
        }
    }
    out.scanned = int_num(srv.get("scanned"))?;
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
                ping: int_num(p.get("ping"))?,
                brand: clean_text(p.get("brand"), 32),
                view: int_num(p.get("view"))?,
                send: int_num(p.get("send"))?,
                loc: clean_text(p.get("loc"), 16),
                w: clean_text(p.get("w"), 64),
                cx: int_num(p.get("cx"))?,
                cz: int_num(p.get("cz"))?,
                ent: int_num(p.get("ent"))?,
                glide: p.get("glide") == Some(&J::Bool(true)),
                tips: p.get("tips") != Some(&J::Bool(false)),
                join: int_num(p.get("join"))?,
                fps: fps(p.get("fps"))?,
            });
        }
    }
    Ok(Some(Snapshot {
        seq: int_num(raw.get("seq"))?,
        ts,
        win_ms: int_num(raw.get("win_ms"))?,
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
}

impl SnapshotReader {
    pub fn new(directory: impl Into<PathBuf>) -> Self {
        Self {
            directory: directory.into(),
            last_seq: -1,
            last_ts: 0,
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

    /// `read()`.
    ///
    /// # Errors
    /// What `parse_snapshot` raises (the reader doesn't move: the next round reads the same files).
    pub fn read(&mut self) -> TResult<Vec<Snapshot>> {
        let Some(mut names) = Self::names(&self.directory) else {
            return Ok(Vec::new());
        };
        names.sort();
        let mut out: Vec<Snapshot> = Vec::new();
        for (seq, name) in &names {
            if *seq <= self.last_seq {
                continue;
            }
            let Ok(data) =
                crate::data::read_untrusted(&self.directory.join(name), MAX_SNAPSHOT_BYTES, false)
            else {
                continue;
            };
            if let Some(snap) = parse_snapshot(&data)?
                && snap.ts > self.last_ts
            {
                out.push(snap);
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
        assert_eq!(num(Some(&J::Bool(true))), None);
        assert_eq!(num(Some(&J::Int(3))), Some(3.0));
        assert_eq!(int_num(Some(&J::Float(-2.7))), Ok(-2));
        assert_eq!(fps(Some(&J::Float(12345.0))), Ok(Some(10_000)));
        assert_eq!(fps(Some(&J::Int(-1))), Ok(None));
        assert_eq!(fps(Some(&J::Float(f64::NEG_INFINITY))), Ok(None));
        assert_eq!(
            int_num(Some(&J::Float(f64::NAN))).unwrap_err().class,
            "ValueError"
        );
        assert_eq!(
            fps(Some(&J::Float(f64::INFINITY))).unwrap_err().class,
            "OverflowError"
        );
    }

    /// Python raises from `int()` for `NaN`/`Infinity` in an int field (kept for parity, PORTING.md "telemetry").
    #[test]
    fn non_finite_ints_raise_like_python() {
        let raw = r#"{"v": 1, "ts": 5, "srv": {"tps": NaN}, "pl": [{"u": "3f2a0000-0000-4000-8000-000000000c41", "n": "Em_K", "ping": Infinity}]}"#;
        let e = parse_snapshot(raw.as_bytes()).unwrap_err();
        assert_eq!(
            (e.class.as_str(), e.text.as_str()),
            ("OverflowError", "cannot convert float infinity to integer")
        );
        let ok =
            parse_snapshot(br#"{"v": 1, "ts": 5, "srv": {"tps": NaN, "heap_used_mb": 1e400}}"#)
                .unwrap()
                .unwrap();
        assert!(
            ok.srv.get("tps").unwrap().is_nan()
                && ok.srv.get("heap_used_mb") == Some(f64::INFINITY)
        );
    }
}
