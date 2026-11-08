//! Player-experience telemetry (owner: **telemetry**; `scripts/telemetry.py` and mc_agent.py `make_telemetry`),
//! in-process in the Rust agent as in the Python one: the mc-telemetry plugin's snapshots (`/run/mc-telemetry`,
//! every 10 s) become one minute row per minute, shipped live (`POST /ingest/experience`, the reply's verdicts
//! written to `/run/mc-verdicts/verdicts.tsv` for the plugin's /lag), buffered into gzip NDJSON segments
//! (`/var/lib/mc-telemetry/segments`, `PUT /ingest/segment`) and backfilled in quiet time.
//!
//! | File | Ports (telemetry.py) |
//! |---|---|
//! | json.rs | Python's dict order and `json.loads`/`json.dumps` for the documents telemetry reads and writes |
//! | snapshot.rs | `clean_text`, `_num`, `parse_hot`, `merge_hot`, `parse_snapshot`, `_fps`, `SnapshotReader` |
//! | minute.rs | `_median`, `_r`, `summarize_server`, `summarize_players`, `lag_events`, `Sessions` |
//! | overhead.rs | `parse_show`, `unit_stats`, `consumed_cpu_ms`, `Overhead` |
//! | stats.rs | `STAT_KEYS`, `read_stats`, `StatsReader` |
//! | segments.rs | `segment_bytes`, `read_segment`, `Segments` |
//! | backfill.rs | `in_window`, `quiet`, `Pacer`, `Backfill` |
//! | messages.rs | `Messages` (player_message's rate limits, messages.tsv) |
//! | mod.rs | `verdicts_tsv`, `write_verdicts`, `_atomic_text`, `Config`, `load_identity`, `save_identity`, `Telemetry`, mc_agent.py `make_telemetry` |
//!
//! Threads (as Python's): the telemetry loop owns the reader, the minute, the buffer, segments and backfill
//! (`Core`, behind a mutex only it takes); `player_message` (the control thread) reads `online()` from the
//! latest snapshot, an `Arc` swapped under a short lock and never held across I/O (P13 review L4); the logs
//! thread feeds systemd's end-of-run lines into `Overhead`.

pub mod backfill;
pub mod json;
pub mod messages;
pub mod minute;
pub mod overhead;
pub mod segments;
pub mod sidecar;
pub mod snapshot;
pub mod stats;
#[cfg(test)]
mod vectors;

use crate::daemon::lock;
use crate::jobs::Event;
use crate::settings::{Env, Settings};
use crate::types::{AResult, AgentError, Obj};
use backfill::{Backfill, Pacer, quiet};
use json::{J, OMap, loads};
use messages::Messages;
use overhead::{NotUtf8, Overhead, Stats};
use regex::Regex;
use segments::Segments;
use snapshot::{Snapshot, SnapshotReader, UUID, clean_text};
use stats::StatsReader;
use std::fmt::Write as _;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock, Mutex};

pub const SNAP_DIR: &str = "/run/mc-telemetry";
pub const VERDICT_DIR: &str = messages::VERDICT_DIR;
pub const SEGMENT_DIR: &str = "/var/lib/mc-telemetry/segments";
pub const MINUTE_MS: i64 = 60_000;
pub const SEGMENT_S: i64 = 300;

/// A Python exception as telemetry's loop prints it (`f"{type(e).__name__}: {e}"`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PyExc {
    pub class: String,
    pub text: String,
}

pub type TResult<T> = Result<T, PyExc>;

impl PyExc {
    pub fn new(class: &str, text: impl Into<String>) -> Self {
        Self {
            class: class.into(),
            text: text.into(),
        }
    }

    /// An `OSError` as Python words it: `[Errno N] strerror: 'path'` (no path: `[Errno N] strerror`).
    pub fn from_io(e: &std::io::Error, path: Option<&Path>) -> Self {
        let class = smallworld_core::errno::io_class(e);
        let text = match e.raw_os_error() {
            Some(n) => {
                let base = format!("[Errno {n}] {}", smallworld_core::errno::strerror(n));
                match path {
                    Some(p) => format!(
                        "{base}: {}",
                        smallworld_core::pyjson::repr_str(&p.to_string_lossy())
                    ),
                    None => base,
                }
            }
            None => e.to_string(),
        };
        Self::new(class, text)
    }

    /// `os.replace(src, dst)`'s error: `[Errno N] strerror: 'src' -> 'dst'`.
    pub fn from_io2(e: &std::io::Error, src: &Path, dst: &Path) -> Self {
        let mut x = Self::from_io(e, Some(src));
        if e.raw_os_error().is_some() {
            x.text = format!(
                "{} -> {}",
                x.text,
                smallworld_core::pyjson::repr_str(&dst.to_string_lossy())
            );
        }
        x
    }
}

impl From<AgentError> for PyExc {
    fn from(e: AgentError) -> Self {
        let class = e.class_name();
        let text = match &e {
            AgentError::Crash(line) => line
                .split_once(": ")
                .map_or_else(String::new, |(_, t)| t.to_string()),
            other => other.to_string(),
        };
        Self { class, text }
    }
}

/// `path.write_text(text)` then `os.replace(tmp, path)`, with Python's error texts.
fn replace_text(tmp: &Path, path: &Path, text: &str) -> TResult<()> {
    std::fs::write(tmp, text).map_err(|e| PyExc::from_io(&e, Some(tmp)))?;
    std::fs::rename(tmp, path).map_err(|e| PyExc::from_io2(&e, tmp, path))
}

// --- verdicts (agent -> plugin) --------------------------------------------------------------------------

/// telemetry.py `_atomic_text(path, text)`: `.NAME.tmp`, chmod 0640 (the setgid directory: the plugin, group
/// minecraft, reads it), `os.replace`.
///
/// # Errors
/// The `OSError`.
pub fn atomic_text(path: &Path, text: &str) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    let tmp = path.with_file_name(format!(".{name}.tmp"));
    std::fs::write(&tmp, text)?;
    std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o640))?;
    std::fs::rename(&tmp, path)
}

/// telemetry.py `_join(items)`.
fn join_items(items: Option<&J>) -> String {
    let Some(J::List(items)) = items else {
        return String::new();
    };
    items
        .iter()
        .take(8)
        .map(|i| clean_text(Some(i), snapshot::TEXT_MAX))
        .filter(|t| !t.is_empty())
        .collect::<Vec<_>>()
        .join("\u{1f}")
}

/// telemetry.py `verdicts_tsv(verdicts, now_ms)`.
///
/// # Errors
/// Python's uncaught `AttributeError` for a truthy `players` that isn't an object.
pub fn verdicts_tsv(verdicts: &OMap, now_ms: i64) -> TResult<String> {
    let mut lines = vec![format!("mc-verdicts 1 {now_ms}")];
    let text = |m: &OMap, k: &str, limit: usize| clean_text(m.get(k), limit);
    if let Some(J::Obj(srv)) = verdicts.get("server") {
        lines.push(
            [
                "S".to_string(),
                text(srv, "verdict", 8),
                text(srv, "headline", snapshot::TEXT_MAX),
                join_items(srv.get("reasons")),
                join_items(srv.get("tips")),
            ]
            .join("\t"),
        );
    }
    match verdicts.get("players") {
        Some(J::Obj(players)) => {
            for (u, v) in players.iter() {
                if let J::Obj(v) = v
                    && UUID.is_match(u)
                {
                    lines.push(
                        [
                            "P".to_string(),
                            u.clone(),
                            text(v, "verdict", 8),
                            text(v, "headline", snapshot::TEXT_MAX),
                            join_items(v.get("reasons")),
                            join_items(v.get("tips")),
                        ]
                        .join("\t"),
                    );
                }
            }
        }
        Some(other) if other.truthy() => {
            return Err(PyExc::new(
                "AttributeError",
                format!("'{}' object has no attribute 'items'", other.py_type()),
            ));
        }
        _ => {}
    }
    Ok(lines.join("\n") + "\n")
}

/// telemetry.py `write_verdicts(directory, verdicts, now_ms)`: `Ok(Err(..))` is the write's `OSError` (the
/// caller suppresses it), `Err` what Python doesn't catch.
///
/// # Errors
/// As `verdicts_tsv`.
pub fn write_verdicts(dir: &Path, verdicts: &OMap, now_ms: i64) -> TResult<std::io::Result<()>> {
    let text = verdicts_tsv(verdicts, now_ms)?;
    Ok(atomic_text(&dir.join("verdicts.tsv"), &text))
}

// --- config and identity -----------------------------------------------------------------------------------

/// `(\d\d:\d\d-\d\d:\d\d)?` with `re.fullmatch` (`\d`: any `Nd`).
static WINDOW: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\A(?:\d\d:\d\d-\d\d:\d\d)?\z").expect("WINDOW"));
static SRC: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\A[0-9a-f]{8}\z").expect("SRC"));

/// `float(text)`: Python's spelling (surrounding whitespace, `_` between digits, any `Nd` digit, `inf`, `nan`).
///
/// # Errors
/// `ValueError`'s text.
pub fn py_float(raw: &str) -> Result<f64, String> {
    let bad = || {
        format!(
            "could not convert string to float: {}",
            smallworld_core::pyjson::repr_str(raw)
        )
    };
    let t = smallworld_core::pystr::strip_ws(raw);
    let chars: Vec<char> = t.chars().collect();
    let mut ascii = String::new();
    for (i, &c) in chars.iter().enumerate() {
        if c == '_' {
            let digit =
                |j: Option<&char>| j.is_some_and(|d| crate::unicode::decimal_value(*d).is_some());
            if !(i > 0 && digit(chars.get(i - 1)) && digit(chars.get(i + 1))) {
                return Err(bad());
            }
            continue;
        }
        match crate::unicode::decimal_value(c) {
            Some(d) => ascii.push(char::from_digit(d, 10).unwrap_or('0')),
            None if c.is_ascii() => ascii.push(c),
            None => return Err(bad()),
        }
    }
    let lower = ascii.to_ascii_lowercase();
    let body = lower.trim_start_matches(['+', '-']);
    let named = matches!(body, "inf" | "infinity" | "nan");
    if !named
        && !body
            .chars()
            .all(|c| c.is_ascii_digit() || matches!(c, '.' | 'e' | '+' | '-'))
    {
        return Err(bad());
    }
    ascii.parse::<f64>().map_err(|_| bad())
}

/// telemetry.py `Config`.
#[derive(Clone, Debug, PartialEq)]
pub struct Config {
    pub snap_dir: PathBuf,
    pub verdict_dir: PathBuf,
    pub segment_dir: PathBuf,
    pub stats_dir: Option<PathBuf>,
    pub segment_days: i64,
    pub segment_cap_mb: i64,
    pub backfill_window: String,
    pub backfill_pause_s: f64,
    pub read_s: f64,
    pub segment_s: i64,
}

impl Config {
    /// `Config.from_env(env, server_dir, level)`.
    ///
    /// # Errors
    /// `int()`/`float()`'s `ValueError` text.
    pub fn from_env(env: &Env, server_dir: &Path, level: &str) -> Result<Config, String> {
        let get = |k: &str| env.get(k).map(String::as_str).filter(|v| !v.is_empty());
        let window = env.get("BACKFILL_WINDOW").cloned().unwrap_or_default();
        let days = crate::settings::int_env(env, "SEGMENT_DAYS", 30)?.max(1);
        let cap = crate::settings::int_env(env, "SEGMENT_CAP_MB", 200)?.max(10);
        let window = if WINDOW.is_match(&window) {
            window
        } else {
            String::new()
        };
        let pause = match get("BACKFILL_PAUSE_S") {
            Some(v) => py_float(v)?,
            None => 2.0,
        };
        let pause = if pause > 0.0 { pause } else { 0.0 };
        let segment_s = crate::settings::int_env(env, "SEGMENT_S", SEGMENT_S)?.max(30);
        Ok(Config {
            snap_dir: PathBuf::from(get("TELEMETRY_DIR").unwrap_or(SNAP_DIR)),
            verdict_dir: PathBuf::from(get("VERDICT_DIR").unwrap_or(VERDICT_DIR)),
            segment_dir: PathBuf::from(get("SEGMENT_DIR").unwrap_or(SEGMENT_DIR)),
            stats_dir: Some(server_dir.join(level).join("stats")),
            segment_days: days,
            segment_cap_mb: cap,
            backfill_window: window,
            backfill_pause_s: pause,
            read_s: 5.0,
            segment_s,
        })
    }
}

/// 4 random bytes as hex (`secrets.token_hex(4)`), from ring's system RNG (the one rustls uses).
fn token_hex4() -> String {
    let mut buf = [0u8; 4];
    if rustls::crypto::ring::default_provider()
        .secure_random
        .fill(&mut buf)
        .is_err()
    {
        use std::hash::{BuildHasher, Hasher};
        let mut h = std::collections::hash_map::RandomState::new().build_hasher();
        h.write_u64(std::process::id().into());
        buf.copy_from_slice(&h.finish().to_le_bytes()[..4]);
    }
    buf.iter().fold(String::new(), |mut acc, b| {
        let _ = write!(acc, "{b:02x}");
        acc
    })
}

/// `int(v)` for a JSON value as `load_identity` calls it (None: Python's `ValueError`/`TypeError`, caught).
fn py_int_of(v: &J) -> Option<i64> {
    match v {
        J::Int(n) => Some(*n),
        J::Bool(b) => Some(i64::from(*b)),
        #[allow(clippy::cast_possible_truncation)]
        J::Float(x) if x.is_finite() => Some(*x as i64),
        J::Str(s) => crate::settings::int_env(&Env::from([("v".to_string(), s.clone())]), "v", 0)
            .ok()
            .filter(|_| !s.is_empty()),
        _ => None,
    }
}

/// telemetry.py `load_identity(path)`: this installation's source id and the next row sequence number; a new
/// random id at 0 when the file is missing or invalid.
pub fn load_identity(path: &Path) -> (String, i64) {
    let parsed = std::fs::read(path)
        .ok()
        .and_then(|b| String::from_utf8(b).ok())
        .and_then(|t| loads(&t));
    if let Some(J::Obj(data)) = parsed
        && let Some(src) = data.get("src")
        && let Some(seq) = data.get("seq").and_then(py_int_of)
    {
        let src = src.py_str();
        if SRC.is_match(&src) {
            return (src, seq);
        }
    }
    (token_hex4(), 0)
}

/// telemetry.py `save_identity(path, src, seq)`: `{"src": ..., "seq": ...}` (default separators).
///
/// # Errors
/// The `OSError`.
pub fn save_identity(path: &Path, src: &str, seq: i64) -> TResult<()> {
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    let tmp = path.with_file_name(format!("{name}.tmp"));
    let text = J::Obj(OMap::new().with("src", src).with("seq", seq)).dumps();
    replace_text(&tmp, path, &text)
}

// --- the loop -----------------------------------------------------------------------------------------------

/// `post(path, body)`: the reply, or `str(e)`.
pub type PostFn = dyn Fn(&str, &J) -> Result<J, String> + Send;
/// `put(name, data)`.
pub type PutFn = dyn Fn(&str, Vec<u8>) -> Result<(), String> + Send;

/// What the telemetry talks to (mc_agent.py's `post`, `put`, `running_units`, `unit_stats`, the GC `extra`):
/// the Worker through `Link`, systemd, the GC log; fakes in tests.
pub struct Io {
    /// `post(path, body)`: the reply, or `str(e)` of the failure.
    pub post: Box<PostFn>,
    /// `put(name, data)`.
    pub put: Box<PutFn>,
    pub running: Box<dyn Fn() -> Vec<String> + Send>,
    pub stats: Box<dyn Fn() -> Result<Stats, NotUtf8> + Send>,
    pub extra: Box<dyn FnMut() -> OMap + Send>,
}

/// The telemetry thread's state (telemetry.py `Telemetry`'s attributes but `latest` and `messages`).
pub struct Core {
    pub cfg: Config,
    io: Io,
    reader: SnapshotReader,
    sessions: minute::Sessions,
    stats_reader: Option<StatsReader>,
    pub segments: Segments,
    pub id_file: PathBuf,
    pub src: String,
    pub seq: i64,
    minute_snaps: Vec<Arc<Snapshot>>,
    minute: Option<i64>,
    pending_events: Vec<J>,
    pub buffer: Vec<J>,
    acked: Vec<String>,
    segment_start: f64,
    last_stats: f64,
    pub ok: bool,
    pub pacer: Pacer,
}

/// telemetry.py `Telemetry` (with `Messages`, which `player_message` uses through `actions::Telemetry`).
pub struct Telemetry {
    pub messages: Messages,
    latest: Mutex<Option<Arc<Snapshot>>>,
    pub overhead: Overhead,
    pub core: Mutex<Core>,
    pub stop: Arc<Event>,
    pub clock: fn() -> f64,
}

/// `urllib.parse.quote(name)` (safe "/").
fn quote(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || b"_.-~/".contains(&b) {
            out.push(char::from(b));
        } else {
            let _ = write!(out, "%{b:02X}");
        }
    }
    out
}

/// mc_agent.py `make_telemetry(s)`'s `post`/`put` over the Worker link, `running_units`, `unit_stats` of
/// `OVERHEAD_UNITS` and the GC readings.
pub fn link_io(link: crate::link::Link, server_dir: PathBuf) -> Io {
    let put_link = link.clone();
    let mut gc_uptime = 0.0_f64;
    Io {
        post: Box::new(move |path, body| {
            let raw = link
                .request_body(
                    "POST",
                    path,
                    Some(body.dumps_compact().as_bytes()),
                    "application/json",
                    20.0,
                )
                .map_err(|e| e.to_string())?;
            std::str::from_utf8(&raw)
                .ok()
                .and_then(loads)
                .ok_or_else(|| format!("POST {path}: reply isn't JSON"))
        }),
        put: Box::new(move |name, data| {
            put_link
                .request(
                    "PUT",
                    &format!("/ingest/segment?name={}", quote(name)),
                    Some(&data),
                    "application/gzip",
                    60.0,
                )
                .map(|_| ())
                .map_err(|e| e.to_string())
        }),
        running: Box::new(crate::systemd::running_units),
        stats: Box::new(|| overhead::unit_stats(&overhead::OVERHEAD_UNITS)),
        extra: Box::new(move || {
            use crate::data::health;
            let lines = health::read_gc_log(&server_dir);
            let heap = health::last_heap(&lines);
            let gc = health::parse_gc(&lines, gc_uptime, 100.0);
            gc_uptime = gc.last_uptime;
            let pause = gc
                .stalls
                .iter()
                .copied()
                .fold(gc.max_pause_ms, health::py_max);
            OMap::new()
                .with("gc_heap_after_mb", heap.map(|h| h.0))
                .with("gc_max_pause_ms", crate::data::metrics::py_round(pause, 1))
        }),
    }
}

/// `{p["u"]: p for p in snapshot["pl"]}` in Python's order (a repeated uuid: first position, last value).
fn online_of(s: Option<&Snapshot>) -> OMap {
    let mut m = OMap::new();
    if let Some(s) = s {
        for p in &s.pl {
            m.insert(p.u.as_str(), p.to_j());
        }
    }
    m
}

impl Telemetry {
    /// `Telemetry(cfg, state_dir, post=..., put=..., ...)`: reads (or makes) the identity, starts no thread.
    pub fn new(
        cfg: Config,
        state_dir: &Path,
        io: Io,
        messages: Messages,
        clock: fn() -> f64,
    ) -> Self {
        let id_file = state_dir.join("telemetry-id.json");
        let (src, seq) = load_identity(&id_file);
        let core = Core {
            reader: SnapshotReader::new(&cfg.snap_dir),
            sessions: minute::Sessions::default(),
            stats_reader: cfg.stats_dir.as_ref().map(StatsReader::new),
            segments: Segments::new(
                &cfg.segment_dir,
                cfg.segment_days,
                cfg.segment_cap_mb,
                clock,
            ),
            id_file,
            src,
            seq,
            minute_snaps: Vec::new(),
            minute: None,
            pending_events: Vec::new(),
            buffer: Vec::new(),
            acked: Vec::new(),
            segment_start: clock(),
            last_stats: 0.0,
            ok: true,
            pacer: Pacer::new(clock),
            io,
            cfg,
        };
        Self {
            messages,
            latest: Mutex::new(None),
            overhead: Overhead::default(),
            core: Mutex::new(core),
            stop: Arc::new(Event::default()),
            clock,
        }
    }

    /// `online()`: who is online by the latest snapshot. Lock-free in Python (`latest` is replaced, never
    /// changed); here an `Arc` cloned under a lock nobody holds across I/O.
    pub fn online_map(&self) -> OMap {
        let latest = lock(&self.latest).clone();
        online_of(latest.as_deref())
    }

    fn latest(&self) -> Option<Arc<Snapshot>> {
        lock(&self.latest).clone()
    }

    /// `poll()`: read snapshots; when a minute closed, build and ship its row.
    ///
    /// # Errors
    /// What Python's `poll` raises (the loop logs it).
    pub fn poll(&self, c: &mut Core) -> TResult<Option<J>> {
        let snaps = c.reader.read()?;
        let mut row = None;
        for s in snaps {
            let s = Arc::new(s);
            *lock(&self.latest) = Some(Arc::clone(&s));
            let minute = s.ts.div_euclid(MINUTE_MS) * MINUTE_MS;
            if c.minute.is_some_and(|m| m != minute) && !c.minute_snaps.is_empty() {
                row = Some(self.close_minute(c)?);
            }
            c.minute = Some(minute);
            c.minute_snaps.push(Arc::clone(&s));
            let ev = c.sessions.feed(std::slice::from_ref(&*s));
            c.pending_events.extend(ev);
        }
        #[allow(clippy::cast_precision_loss)]
        if row.is_none()
            && !c.minute_snaps.is_empty()
            && (self.clock)() * 1000.0 - c.minute.unwrap_or(0) as f64 >= (MINUTE_MS + 15000) as f64
        {
            row = Some(self.close_minute(c)?); // the plugin went quiet (server stopped): close it anyway
        }
        #[allow(clippy::cast_precision_loss)]
        if (self.clock)() - c.segment_start >= c.cfg.segment_s as f64 {
            self.flush_segment(c)?;
        }
        Ok(row)
    }

    /// `close_minute()`.
    ///
    /// # Errors
    /// What Python's `close_minute` raises.
    pub fn close_minute(&self, c: &mut Core) -> TResult<J> {
        let snaps: Vec<Snapshot> = std::mem::take(&mut c.minute_snaps)
            .into_iter()
            .map(|s| (*s).clone())
            .collect();
        let running = (c.io.running)();
        let raw: Vec<J> = snaps
            .iter()
            .map(|s| {
                let mut pl = OMap::new();
                for p in &s.pl {
                    pl.insert(p.u.as_str(), J::List(vec![J::Int(p.ping), J::Int(p.ent)]));
                }
                OMap::new()
                    .with("ts", s.ts)
                    .with("tps", s.srv.get("tps"))
                    .with("p50", s.srv.get("p50"))
                    .with("p99", s.srv.get("p99"))
                    .with("p999", s.srv.get("p999"))
                    .with("max", s.srv.get("max"))
                    .with("pl", pl)
                    .into()
            })
            .collect();
        let srv = minute::summarize_server(&snaps)?;
        let pl = minute::summarize_players(&snaps);
        let ev = c.pending_events.clone();
        let lag = minute::lag_events(&snaps, &running);
        let stats = (c.io.stats)().map_err(|NotUtf8| {
            PyExc::new(
                "UnicodeDecodeError",
                "'utf-8' codec can't decode systemctl's output",
            )
        })?;
        let ovh = self.overhead.sample(stats, (self.clock)());
        let backlog = c.segments.backlog();
        let mut row = OMap::new()
            .with("v", 1_i64)
            .with("id", format!("{}:{}", c.src, c.seq))
            .with("src", c.src.as_str())
            .with("seq", c.seq)
            .with("ts", c.minute.unwrap_or(0))
            .with("srv", srv)
            .with("pl", J::List(pl))
            .with("raw", J::List(raw))
            .with("ev", J::List(ev))
            .with("lag", J::List(lag))
            .with(
                "running",
                J::List(running.iter().map(|r| J::Str(r.clone())).collect()),
            )
            .with("ovh", ovh)
            .with("backlog", backlog);
        let extra = (c.io.extra)();
        if let Some(J::Obj(srv)) = row.get_mut("srv") {
            for (k, v) in extra.0 {
                srv.insert(k, v);
            }
        }
        c.pending_events.clear();
        let events: Vec<(String, String)> = match row.get("ev") {
            Some(J::List(ev)) => ev
                .iter()
                .filter_map(J::as_obj)
                .map(|e| {
                    (
                        e.get("k")
                            .and_then(J::as_str)
                            .unwrap_or_default()
                            .to_string(),
                        e.get("u")
                            .and_then(J::as_str)
                            .unwrap_or_default()
                            .to_string(),
                    )
                })
                .collect(),
            _ => Vec::new(),
        };
        if c.stats_reader.is_some()
            && (events.iter().any(|(k, _)| k == "leave") || (self.clock)() - c.last_stats > 3600.0)
        {
            c.last_stats = (self.clock)();
            // `online().keys() | {e["u"] for e in ev}`: a set; sorted here (Python iterates it in hash order)
            let mut uuids: Vec<String> = self.online_map().keys().cloned().collect();
            uuids.extend(events.into_iter().map(|(_, u)| u));
            uuids.sort();
            uuids.dedup();
            if let Some(reader) = c.stats_reader.as_mut() {
                let changed = reader.changed(&uuids).map_err(PyExc::from)?;
                row.insert("stats", changed);
            }
        }
        c.seq += 1;
        save_identity(&c.id_file, &c.src, c.seq)?;
        let row = J::Obj(row);
        c.buffer.push(row.clone());
        self.ship_live(c, &row)?;
        Ok(row)
    }

    /// `ship_live(row)`.
    ///
    /// # Errors
    /// Python's uncaught `AttributeError` for verdicts with a bad `players`.
    pub fn ship_live(&self, c: &mut Core, row: &J) -> TResult<()> {
        let body: J = OMap::new()
            .with("agent", "1")
            .with("live", true)
            .with("rows", J::List(vec![row.clone()]))
            .into();
        let Ok(reply) = (c.io.post)("/ingest/experience", &body) else {
            c.ok = false; // the Worker is unreachable: the row waits in the segment for backfill
            return Ok(());
        };
        c.ok = true;
        if let Some(id) = row.as_obj().and_then(|m| m.get("id")).and_then(J::as_str)
            && !c.acked.iter().any(|a| a == id)
        {
            c.acked.push(id.to_string());
        }
        if let Some(J::Obj(verdicts)) = reply.as_obj().and_then(|m| m.get("verdicts")) {
            #[allow(clippy::cast_possible_truncation)]
            let now_ms = ((self.clock)() * 1000.0) as i64;
            let _ = write_verdicts(&c.cfg.verdict_dir, verdicts, now_ms)?; // OSError suppressed
        }
        Ok(())
    }

    /// `flush_segment()`.
    ///
    /// # Errors
    /// The `OSError` of writing the segment (the rows are dropped, as in Python).
    pub fn flush_segment(&self, c: &mut Core) -> TResult<Option<String>> {
        let rows = std::mem::take(&mut c.buffer);
        c.segment_start = (self.clock)();
        let name = c.segments.write(&rows, &c.acked, &c.src)?;
        let ids: Vec<String> = rows
            .iter()
            .filter_map(|r| r.as_obj().and_then(|m| m.get("id")).and_then(J::as_str))
            .map(str::to_string)
            .collect();
        c.acked.retain(|a| !ids.contains(a));
        if let Some(name) = &name
            && c.ok
        {
            let path = c.segments.directory.join(name);
            let sent = std::fs::read(&path)
                .map_err(|e| PyExc::from_io(&e, Some(&path)).text)
                .and_then(|data| (c.io.put)(name, data))
                .and_then(|()| {
                    c.segments
                        .mark(name, &[], Some(true))
                        .map_err(|e| PyExc::from_io(&e, None).text)
                });
            if sent.is_err() {
                c.ok = false;
            }
        }
        Ok(name)
    }

    /// `may_backfill()`: quiet time by the latest snapshot.
    pub fn may_backfill(&self, c: &Core) -> bool {
        if self.stop.is_set() {
            return false;
        }
        let tm = crate::sys::localtime((self.clock)());
        quiet(
            self.latest().as_deref(),
            &c.cfg.backfill_window,
            tm.hour,
            tm.minute,
        )
    }

    /// `backfill(max_requests)`.
    ///
    /// # Errors
    /// What `Backfill.run_once` doesn't catch.
    pub fn backfill(&self, c: &mut Core, max_requests: usize) -> TResult<backfill::Done> {
        let window = c.cfg.backfill_window.clone();
        let pause = c.cfg.backfill_pause_s;
        let Core {
            io,
            segments,
            pacer,
            ..
        } = c;
        let post = &io.post;
        let put = &io.put;
        let may_run = || {
            if self.stop.is_set() {
                return false;
            }
            let tm = crate::sys::localtime((self.clock)());
            quiet(self.latest().as_deref(), &window, tm.hour, tm.minute)
        };
        let mut send_rows = |rows: Vec<J>| -> Result<(), String> {
            let body: J = OMap::new()
                .with("agent", "1")
                .with("live", false)
                .with("rows", J::List(rows))
                .into();
            post("/ingest/experience", &body).map(|_| ())
        };
        let mut send_segment = |name: &str, data: Vec<u8>| put(name, data);
        let sleep = |s: f64| {
            self.stop.wait(s);
        };
        let log = |m: &str| println!("{m}");
        Backfill {
            segments,
            send_rows: &mut send_rows,
            send_segment: &mut send_segment,
            may_run: &may_run,
            pause_s: pause,
            sleep: &sleep,
            pacer,
            log: &log,
        }
        .run_once(max_requests)
    }

    /// One round of `run()`.
    ///
    /// # Errors
    /// What the round raises (the loop logs it and sets `ok = False`).
    pub fn round(&self, c: &mut Core, after_poll: &dyn Fn()) -> TResult<()> {
        let polled = self.poll(c);
        after_poll(); // SidecarTelemetry.poll's `finally:` (online.json)
        polled?;
        if c.ok && c.pacer.ready() && !c.segments.pending().is_empty() && self.may_backfill(c) {
            self.backfill(c, 4)?;
        }
        Ok(())
    }

    /// `run()`: the telemetry thread until `stop`.
    pub fn run(&self) {
        self.run_with(&|| ());
    }

    /// `run()` with something done after every poll (the sidecar's online.json).
    pub fn run_with(&self, after_poll: &dyn Fn()) {
        while !self.stop.is_set() {
            let mut c = lock(&self.core);
            let outcome = catch_unwind(AssertUnwindSafe(|| self.round(&mut c, after_poll)));
            let failed = match outcome {
                Ok(Ok(())) => None,
                Ok(Err(e)) => Some(format!("{}: {}", e.class, e.text)),
                Err(p) => Some(format!("panic: {}", crate::daemon::panic_text(&*p))),
            };
            if let Some(msg) = failed {
                c.ok = false;
                println!("telemetry round failed: {msg}");
            }
            let wait = c.cfg.read_s;
            drop(c);
            self.stop.wait(wait);
        }
    }
}

impl crate::actions::Telemetry for Telemetry {
    fn online(&self) -> AResult<Obj> {
        match J::Obj(self.online_map()).to_value() {
            crate::types::Value::Object(o) => Ok(o),
            _ => Ok(Obj::new()),
        }
    }

    fn check(&self, to: &str, kind: &str, now: f64, online: &Obj) -> AResult<String> {
        self.messages.check(to, kind, now, online)
    }

    fn add(
        &self,
        id: &str,
        to: &str,
        kind: &str,
        text: &str,
        hover: &str,
        now: f64,
    ) -> AResult<()> {
        self.messages.add(id, to, kind, text, hover, now)
    }
}

/// mc_agent.py `make_telemetry(s)`: `Config.from_env` (TELEMETRY_DIR, VERDICT_DIR, SEGMENT_*, BACKFILL_*,
/// LEVEL_NAME), `Messages` (MESSAGE_*), the Worker link, `running_units`, `unit_stats(OVERHEAD_UNITS)`, the GC
/// readings; the identity in the agent's state dir.
///
/// # Errors
/// `int()`/`float()`'s `ValueError` text (Python's `Agent(s)` raises it: the agent doesn't start).
pub fn make_telemetry(
    s: &Settings,
    env: &Env,
    link: crate::link::Link,
) -> Result<Telemetry, String> {
    make_telemetry_with(s, env, link, || {
        overhead::unit_stats(&overhead::OVERHEAD_UNITS)
    })
}

/// `make_telemetry(s, stats=...)`.
///
/// # Errors
/// As `make_telemetry`.
pub fn make_telemetry_with(
    s: &Settings,
    env: &Env,
    link: crate::link::Link,
    stats: fn() -> Result<Stats, NotUtf8>,
) -> Result<Telemetry, String> {
    let server_dir = crate::data::server_dir();
    let level = env
        .get("LEVEL_NAME")
        .filter(|v| !v.is_empty())
        .map_or("world", String::as_str);
    let cfg = Config::from_env(env, &server_dir, level)?;
    let mut msgs = Messages::from_env(env)?;
    msgs.directory.clone_from(&cfg.verdict_dir);
    let mut io = link_io(link, server_dir);
    io.stats = Box::new(stats);
    Ok(Telemetry::new(cfg, &s.state_dir, io, msgs, crate::sys::now))
}
