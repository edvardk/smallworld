//! The data plane (owner: **data**): metrics + spool (metrics.rs; mc_agent.py 299-595, health.py's parsers in
//! health.rs), the persistent RCON shared by metrics and `status` (persistent_rcon.rs; mc_rcon.PersistentRcon),
//! inventory (inventory.rs; 871-937, 1107, 2405-2431), the console tail (tail.rs; 940-1005), /modpack client codes
//! (client_codes.rs; 2153-2274) and the rejected-logins feed (panel/rejected.rs). The loops (`metrics_loop`,
//! `inventory_loop`; 2450-2472) take what they need explicitly, so the daemon (link) wires them.

pub mod client_codes;
pub mod health;
pub mod inventory;
pub mod metrics;
pub mod persistent_rcon;
pub mod tail;

use crate::jobs::Event;
use crate::link::Link;
use crate::misc::warnings::Warnings;
use crate::types::{AResult, Obj, Value};
use std::fs::OpenOptions;
use std::io::{self, Read, Seek, SeekFrom};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Instant;

/// Python's `\s` in a str pattern (`str.isspace()`: includes U+001C..U+001F, which Rust's `\s` lacks), as a
/// regex class.
pub const PY_SPACE: &str = r"[\t\n\x0B\x0C\r\x1C-\x20\x{85}\x{A0}\x{1680}\x{2000}-\x{200A}\x{2028}\x{2029}\x{202F}\x{205F}\x{3000}]";

/// mc_rcon.py `UNTRUSTED_MAX_BYTES`.
pub const UNTRUSTED_MAX_BYTES: usize = 16 << 20;

/// mc_rcon.py `read_untrusted(path, limit, tail)`: a file in a directory minecraft can write. Opened
/// `O_NONBLOCK|O_NOFOLLOW|O_NOCTTY` (std adds `O_CLOEXEC`), refused unless a regular file (EINVAL), at most
/// `limit` bytes (the last ones with `tail`).
///
/// # Errors
/// The `OSError`.
pub fn read_untrusted(path: &Path, limit: usize, tail: bool) -> io::Result<Vec<u8>> {
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK | libc::O_NOFOLLOW | libc::O_NOCTTY)
        .open(path)?;
    let meta = file.metadata()?;
    if !meta.is_file() {
        return Err(io::Error::from_raw_os_error(libc::EINVAL));
    }
    let limit64 = u64::try_from(limit).unwrap_or(u64::MAX);
    if tail && meta.len() > limit64 {
        file.seek(SeekFrom::Start(meta.len() - limit64))?;
    }
    let mut data = Vec::new();
    file.take(limit64).read_to_end(&mut data)?;
    Ok(data)
}

/// mc_rcon.py `SERVER_DIR` ($MC_SERVER_DIR, default /opt/minecraft/server).
pub fn server_dir() -> PathBuf {
    use std::os::unix::ffi::OsStrExt;
    let dir = smallworld_core::mcrcon::server_dir().to_os_bytes();
    PathBuf::from(std::ffi::OsStr::from_bytes(&dir))
}

/// `Agent.ws_connected` as the metrics loop reads it.
pub type WsFlag = Arc<dyn Fn() -> bool + Send + Sync>;

/// `Agent.panel_state()` for the inventory (data's `inventory::panel_state`, with the daemon's GitHub/product/
/// self-test parts bound).
pub type PanelStateFn = Arc<dyn Fn() -> AResult<Obj> + Send + Sync>;

/// The metrics loop's state (`Agent.counters`, `Agent.spool`).
pub struct Metrics {
    pub counters: Mutex<metrics::Counters>,
    pub spool: metrics::Spool,
}

impl Metrics {
    /// `Counters()` and `Spool(state_dir / "spool.jsonl")`.
    pub fn new(state_dir: &Path) -> Self {
        Self {
            counters: Mutex::new(metrics::Counters::default()),
            spool: metrics::Spool::new(state_dir.join("spool.jsonl")),
        }
    }
}

/// `Agent.metrics_loop` (runs until `stop`): one sample every `METRICS_INTERVAL_S`, measured from the round's
/// start, at least 1 s apart; a failed round is printed and the loop goes on.
pub fn metrics_loop(link: &Link, m: &Metrics, warn: &Warnings, ws: &WsFlag, stop: &Event) {
    let interval = link.settings.metrics_interval_s;
    while !stop.is_set() {
        let started = Instant::now();
        let round = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| -> AResult<()> {
            let mut sample = {
                let mut c = m
                    .counters
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
                metrics::collect(&mut c)?
            };
            if let Value::Object(o) = &mut sample {
                o.insert("ws".into(), Value::Bool(ws()));
            }
            metrics::push_metrics(link, &m.spool, &sample, warn)?;
            Ok(())
        }));
        match round {
            Ok(Ok(())) => {}
            Ok(Err(e)) => eprintln!("metrics round failed: {}: {e}", e.class_name()),
            Err(p) => eprintln!("metrics round failed: {}", panic_text(&p)),
        }
        #[allow(clippy::cast_precision_loss)]
        let wait = (interval as f64 - started.elapsed().as_secs_f64()).max(1.0);
        stop.wait(wait);
    }
}

/// `Executor.sample` = `lambda: collect(Counters())` (the `status` action: fresh counters, the kept RCON).
pub fn status_sample() -> crate::actions::SampleFn {
    Arc::new(|| metrics::collect(&mut metrics::Counters::default()))
}

/// `Agent.observe_logs(entries)`: the log round's entries (journal_entry dicts) into the rejected-logins feed;
/// true if the list changed (the daemon then sets `inventory_due`).
///
/// # Errors
/// `RejectedLogins::feed`'s.
pub fn observe_logs(
    rejected: &crate::panel::rejected::RejectedLogins,
    entries: &[Obj],
) -> AResult<bool> {
    let lines: Vec<(i64, String)> = entries
        .iter()
        .map(|e| {
            let ts = e.get("ts").and_then(Value::as_i64).unwrap_or(0);
            let msg = e
                .get("msg")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            (ts, msg)
        })
        .collect();
    rejected.feed(&lines)
}

/// mc_agent.py `INVENTORY_S`.
pub const INVENTORY_S: f64 = 600.0;
/// mc_agent.py `INVENTORY_MIN_GAP_S`.
pub const INVENTORY_MIN_GAP_S: f64 = 5.0;

/// `Agent.inventory_loop` (runs until `stop`): the inventory every `INVENTORY_S` or when `due` is set (at most
/// one push per `INVENTORY_MIN_GAP_S`); 60 s after an `IngestError`.
pub fn inventory_loop(
    link: &Link,
    server_dir: Option<&Path>,
    panel_state: &PanelStateFn,
    warn: &Warnings,
    due: &Event,
    stop: &Event,
) {
    while !stop.is_set() {
        let mut wait = INVENTORY_S;
        due.clear();
        let round = std::panic::catch_unwind(std::panic::AssertUnwindSafe(
            || -> AResult<Result<(), crate::link::IngestError>> {
                let extra = panel_state()?;
                Ok(inventory::push_inventory(link, server_dir, &extra))
            },
        ));
        match round {
            Ok(Ok(Ok(()))) => warn.ok("inventory"),
            Ok(Ok(Err(e))) => {
                warn.fail("inventory", &e.to_string(), false);
                wait = 60.0;
            }
            Ok(Err(e)) => eprintln!("inventory round failed: {}: {e}", e.class_name()),
            Err(p) => eprintln!("inventory round failed: {}", panic_text(&p)),
        }
        due.wait(wait);
        stop.wait(INVENTORY_MIN_GAP_S);
    }
}

/// A caught panic's message, as the `except Exception as e` line would print it.
pub fn panic_text(p: &Box<dyn std::any::Any + Send>) -> String {
    let msg = p
        .downcast_ref::<&str>()
        .map(|s| (*s).to_string())
        .or_else(|| p.downcast_ref::<String>().cloned())
        .unwrap_or_default();
    format!("Exception: {msg}")
}
