//! `Counters`, `parse_net_dev`, `parse_meminfo`, `cpu_busy_pct`, `unit_memory_mb`, `unit_uptime_s`,
//! `parse_cores`, `parse_meminfo_mb`, `parse_tps_all`, `player_names`, `rcon_stats`, `collect`, `Spool`,
//! `push_metrics` (mc_agent.py 299-595).
//!
//! Python's `round(x, n)` is `py_round`: `format!("{:.n$}")` rounds the exact binary value half-even, as CPython's
//! `round` does, then the text is read back (pinned by the round vectors and a 200 000-case tie corpus).
//! Arithmetic keeps Python's order (`100.0 * int / int` is f64 * f64 / f64 in both).

use super::health::{self, py_int, py_int_str};
use super::persistent_rcon::{RconFailure, stats_rcon};
use crate::link::{IngestError, Link};
use crate::misc::warnings::Warnings;
use crate::types::{AResult, AgentError, Map, Obj, VERSION, Value, as_py_int_incl_bool};
use regex::Regex;
use serde_json::json;
use smallworld_core::pystr::{split_ws, strip_ws};
use smallworld_core::rcon::strip_formatting;
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, Mutex, PoisonError};
use std::time::Duration;

/// mc_agent.py `MAX_CORES`.
pub const MAX_CORES: usize = 32;
/// mc_agent.py `METRICS_BATCH`.
pub const METRICS_BATCH: usize = 200;
/// mc_agent.py `SPOOL_MAX_AGE_S`.
pub const SPOOL_MAX_AGE_S: f64 = 86400.0;
/// mc_agent.py `UNIT`.
pub const UNIT: &str = "minecraft.service";

/// mc_agent.py `PLAYERS` (no `re.ASCII`: `\d` is any `Nd` digit).
pub static PLAYERS: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"There are (\d+) of a max(?: of)? (\d+)").expect("constant"));
static PLAYER_NAME: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9_]{1,16}$").expect("constant"));

/// Python's `round(x, n)` for a float (n >= 0).
pub fn py_round(x: f64, n: usize) -> f64 {
    format!("{x:.n$}").parse().unwrap_or(x)
}

/// Python's `round(x)` (no digits: an int, half-even).
#[allow(clippy::cast_possible_truncation)]
pub fn py_round_int(x: f64) -> i64 {
    x.round_ties_even() as i64
}

/// `f"{x:.0f}"`.
pub fn format_f0(x: f64) -> String {
    format!("{x:.0}")
}

/// `Counters`: the previous sample's raw counters (rates between samples).
#[derive(Debug, Default, Clone, PartialEq)]
pub struct Counters {
    pub cpu: Vec<i64>,
    pub cores: Vec<Vec<i64>>,
    pub net: Option<(i64, i64)>,
    pub at: f64,
    pub gc_uptime: f64,
}

fn value_error(msg: &str) -> AgentError {
    AgentError::Value("ValueError".into(), msg.into())
}

/// `str.partition(":")`'s halves (None when there is no colon).
fn partition_colon(line: &str) -> (&str, Option<&str>) {
    match line.split_once(':') {
        Some((a, b)) => (a, Some(b)),
        None => (line, None),
    }
}

/// `parse_net_dev(text)`: total rx/tx bytes over every interface but lo.
///
/// # Errors
/// `ValueError` from `int()`.
pub fn parse_net_dev(text: &str) -> AResult<(i64, i64)> {
    let (mut rx, mut tx) = (0i64, 0i64);
    for line in crate::systemd::splitlines(text).into_iter().skip(2) {
        let (name, rest) = partition_colon(line);
        let cols = split_ws(rest.unwrap_or(""));
        if strip_ws(name) == "lo" || cols.len() < 9 {
            continue;
        }
        rx += py_int_str(cols[0])?;
        tx += py_int_str(cols[8])?;
    }
    Ok((rx, tx))
}

fn meminfo(text: &str) -> AResult<Vec<(String, i64)>> {
    let mut info: Vec<(String, i64)> = Vec::new();
    for line in crate::systemd::splitlines(text) {
        let (key, rest) = partition_colon(line);
        let cols = split_ws(rest.unwrap_or(""));
        if let Some(first) = cols.first() {
            let v = py_int_str(first)?;
            match info.iter_mut().find(|(k, _)| k == key) {
                Some(e) => e.1 = v,
                None => info.push((key.to_string(), v)),
            }
        }
    }
    Ok(info)
}

fn total_avail(text: &str) -> AResult<Option<(i64, i64)>> {
    let info = meminfo(text)?;
    let get = |k: &str| info.iter().find(|(n, _)| n == k).map(|(_, v)| *v);
    Ok(match (get("MemTotal"), get("MemAvailable")) {
        (Some(t), Some(a)) if t != 0 => Some((t, a)),
        _ => None,
    })
}

/// `parse_meminfo(text)`: memory used % (MemTotal - MemAvailable), 1 decimal.
///
/// # Errors
/// `ValueError` from `int()`.
pub fn parse_meminfo(text: &str) -> AResult<Option<f64>> {
    #[allow(clippy::cast_precision_loss)]
    Ok(total_avail(text)?.map(|(t, a)| py_round(100.0 * (t - a) as f64 / t as f64, 1)))
}

/// `parse_meminfo_mb(text)`: (used, total) MB.
///
/// # Errors
/// `ValueError` from `int()`.
pub fn parse_meminfo_mb(text: &str) -> AResult<Option<(i64, i64)>> {
    Ok(total_avail(text)?.map(|(t, a)| ((t - a).div_euclid(1024), t.div_euclid(1024))))
}

/// `cpu_busy_pct(prev, cur)`: busy % (minus idle and iowait), 1 decimal.
pub fn cpu_busy_pct(prev: &[i64], cur: &[i64]) -> Option<f64> {
    let (d, total) = health::deltas(prev, cur)?;
    #[allow(clippy::cast_precision_loss)]
    Some(py_round(
        100.0 * (total - d[3] - d[4]) as f64 / total as f64,
        1,
    ))
}

/// `parse_cores(text)`: the cpuN lines of /proc/stat.
///
/// # Errors
/// `ValueError` from `int()`.
pub fn parse_cores(text: &str) -> AResult<Vec<Vec<i64>>> {
    let mut cores = Vec::new();
    for line in crate::systemd::splitlines(text) {
        let parts = split_ws(line);
        if let Some(head) = parts.first()
            && let Some(n) = head.strip_prefix("cpu")
            && is_py_digit(n)
        {
            cores.push(
                parts[1..]
                    .iter()
                    .map(|x| py_int_str(x))
                    .collect::<AResult<Vec<_>>>()?,
            );
        }
    }
    Ok(cores)
}

/// `str.isdigit()` for the decimal digits (`Nd`); non-empty.
fn is_py_digit(s: &str) -> bool {
    !s.is_empty()
        && s.chars()
            .all(|c| crate::unicode::decimal_value(c).is_some())
}

/// `parse_tps_all(text)`: label -> value pairs in order (a repeated label keeps its first place and its last
/// value, as `dict(zip(...))`), empty unless every label has a value.
pub fn parse_tps_all(text: &str) -> Vec<(String, f64)> {
    let Some((labels, values)) = health::tps_parts(text) else {
        return Vec::new();
    };
    if labels.len() != values.len() {
        return Vec::new();
    }
    let mut out: Vec<(String, f64)> = Vec::new();
    for (l, v) in labels.into_iter().zip(values) {
        match out.iter_mut().find(|(k, _)| *k == l) {
            Some(e) => e.1 = v,
            None => out.push((l, v)),
        }
    }
    out
}

/// `player_names(list_reply)`: the names after the colon (at most 100).
pub fn player_names(list_reply: &str) -> Vec<String> {
    let clean = strip_formatting(list_reply);
    let (_, tail) = partition_colon(&clean);
    let Some(tail) = tail else { return Vec::new() };
    tail.split(',')
        .map(strip_ws)
        .filter(|n| PLAYER_NAME.is_match(n))
        .take(100)
        .map(str::to_string)
        .collect()
}

/// `systemctl show ...` stdout (`check=True`, `text=True`, 10 s): None on an `OSError`/`SubprocessError`.
fn systemctl_show(args: &[&str]) -> AResult<Option<String>> {
    let mut argv = vec!["systemctl", "show"];
    argv.extend_from_slice(args);
    match crate::systemd::run(&argv, Duration::from_secs(10)) {
        Ok((Some(0), out)) => String::from_utf8(out).map(Some).map_err(|_| {
            AgentError::Value(
                "UnicodeDecodeError".into(),
                "'utf-8' codec can't decode".into(),
            )
        }),
        Ok(_) | Err(AgentError::Os(_)) => Ok(None),
        Err(e) => Err(e),
    }
}

/// `unit_memory_mb(unit)`: MemoryCurrent in MB.
///
/// # Errors
/// Python's uncaught `UnicodeDecodeError` (a reply that isn't UTF-8).
pub fn unit_memory_mb(unit: &str) -> AResult<Option<i64>> {
    let Some(out) = systemctl_show(&["-p", "MemoryCurrent", "--value", unit])? else {
        return Ok(None);
    };
    let out = strip_ws(&out);
    if !is_py_digit(out) {
        return Ok(None);
    }
    let digits: String = out
        .chars()
        .filter_map(|c| crate::unicode::decimal_value(c).and_then(|d| char::from_digit(d, 10)))
        .collect();
    Ok(digits
        .parse::<u128>()
        .ok()
        .map(|b| i64::try_from(b >> 20).unwrap_or(i64::MAX)))
}

/// `unit_uptime_s(unit)`: seconds since the unit became active (systemd's monotonic clock); None if not active.
///
/// # Errors
/// Python's uncaught `UnicodeDecodeError`.
pub fn unit_uptime_s(unit: &str) -> AResult<Option<i64>> {
    let Some(out) = systemctl_show(&[
        "-p",
        "ActiveState",
        "-p",
        "ActiveEnterTimestampMonotonic",
        unit,
    ])?
    else {
        return Ok(None);
    };
    let mut state = None;
    let mut usec = String::new();
    for line in crate::systemd::splitlines(&out) {
        if let Some((k, v)) = line.split_once('=') {
            match k {
                "ActiveState" => state = Some(v.to_string()),
                "ActiveEnterTimestampMonotonic" => usec = v.to_string(),
                _ => {}
            }
        }
    }
    if state.as_deref() != Some("active") || !is_py_digit(&usec) {
        return Ok(None);
    }
    let us = py_int(&usec);
    if us == 0 {
        return Ok(None);
    }
    #[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation)]
    let up = (crate::sys::monotonic() - us as f64 / 1e6).trunc() as i64;
    Ok(Some(up.max(0)))
}

/// `read(path)`: the file's text, "" on an `OSError` (`read_text` is strict UTF-8: a decode error propagates).
fn read(path: &str) -> AResult<String> {
    match std::fs::read(path) {
        Ok(b) => String::from_utf8(b).map_err(|_| {
            AgentError::Value(
                "UnicodeDecodeError".into(),
                "'utf-8' codec can't decode".into(),
            )
        }),
        Err(_) => Ok(String::new()),
    }
}

/// `rcon_stats(server_dir)`: liveness, MSPT, TPS, players over the kept connection (None where unknown).
///
/// # Errors
/// An exception Python's `rcon_stats` doesn't catch (`Crash`/`Value`).
pub fn rcon_stats(server_dir: &Path) -> AResult<Obj> {
    let mut out = Obj::new();
    for k in [
        "up",
        "mspt",
        "tps",
        "players",
        "max_players",
        "tps5",
        "tps15",
    ] {
        out.insert(k.into(), Value::Null);
    }
    out.insert("players_online".into(), json!([]));
    let texts = match stats_rcon(server_dir).commands(&["mspt", "tps", "list"], None) {
        Ok(t) => t,
        Err(RconFailure::Settings(_)) => return Ok(out),
        Err(RconFailure::Auth | RconFailure::Reply(_)) => {
            out.insert("up".into(), Value::Bool(true));
            return Ok(out);
        }
        Err(RconFailure::Down(_)) => {
            out.insert("up".into(), Value::Bool(false));
            return Ok(out);
        }
        Err(RconFailure::Other(e)) => return Err(e.into()),
    };
    out.insert("up".into(), Value::Bool(true));
    if let Ok(v) = health::parse_mspt(&texts[0]) {
        out.insert("mspt".into(), json!(py_round(v, 2)));
    }
    if let Ok(v) = health::parse_tps(&texts[1]) {
        out.insert("tps".into(), json!(py_round(v, 2)));
    }
    let tps = parse_tps_all(&texts[1]);
    let get = |k: &str| {
        tps.iter()
            .find(|(l, _)| l == k)
            .map_or(Value::Null, |(_, v)| json!(v))
    };
    out.insert("tps5".into(), get("5m"));
    out.insert("tps15".into(), get("15m"));
    let list = strip_formatting(&texts[2]);
    if let Some(m) = PLAYERS.captures(&list) {
        out.insert("players".into(), json!(py_int(&m[1])));
        out.insert("max_players".into(), json!(py_int(&m[2])));
        out.insert("players_online".into(), json!(player_names(&texts[2])));
    }
    Ok(out)
}

fn opt_f(v: Option<f64>) -> Value {
    v.map_or(Value::Null, |x| json!(x))
}

/// `collect(counters)`: one wide sample now, for the default server dir.
///
/// # Errors
/// What Python's `collect` doesn't catch.
pub fn collect(counters: &mut Counters) -> AResult<Value> {
    collect_at(counters, crate::sys::now(), &super::server_dir())
}

/// `collect(counters, now, server_dir)`. Updates `counters`.
///
/// # Errors
/// What Python's `collect` doesn't catch (a `ValueError` from a /proc field, an undecodable reply).
pub fn collect_at(counters: &mut Counters, now: f64, sdir: &Path) -> AResult<Value> {
    let mut s = Obj::new();
    #[allow(clippy::cast_possible_truncation)]
    s.insert("ts".into(), json!((now * 1000.0).trunc() as i64));
    s.extend(rcon_stats(sdir)?);
    let lines = health::read_gc_log(sdir);
    let (used, max) =
        health::last_heap(&lines).map_or((Value::Null, Value::Null), |(a, b)| (json!(a), json!(b)));
    s.insert("heap_used_mb".into(), used);
    s.insert("heap_max_mb".into(), max);
    let gc = health::parse_gc(&lines, counters.gc_uptime, 100.0);
    counters.gc_uptime = gc.last_uptime;
    let pause = gc
        .stalls
        .iter()
        .copied()
        .fold(gc.max_pause_ms, health::py_max);
    s.insert("gc_max_pause_ms".into(), json!(py_round(pause, 1)));
    s.insert(
        "jvm_mem_mb".into(),
        unit_memory_mb(UNIT)?.map_or(Value::Null, |v| json!(v)),
    );
    let up = s.get("up").is_some_and(crate::types::truthy);
    let uptime = if up { unit_uptime_s(UNIT)? } else { None };
    s.insert("uptime_s".into(), uptime.map_or(Value::Null, |v| json!(v)));
    let stat = read("/proc/stat")?;
    let cpu = health::parse_proc_stat(&stat).unwrap_or_default();
    let cores = parse_cores(&stat)?;
    s.insert(
        "cpu_cores".into(),
        if cores.is_empty() {
            Value::Null
        } else {
            json!(cores.len())
        },
    );
    for (i, core) in cores.iter().take(MAX_CORES).enumerate() {
        let prev: &[i64] = counters.cores.get(i).map_or(&[], Vec::as_slice);
        s.insert(format!("cpu{i}_pct"), opt_f(cpu_busy_pct(prev, core)));
    }
    counters.cores = cores;
    s.insert("cpu_pct".into(), opt_f(cpu_busy_pct(&counters.cpu, &cpu)));
    s.insert(
        "steal_pct".into(),
        opt_f(health::steal_pct(&counters.cpu, &cpu).map(|v| py_round(v, 1))),
    );
    counters.cpu = cpu;
    let loadavg = read("/proc/loadavg")?;
    let load = split_ws(&loadavg);
    let lf = |i: usize| -> AResult<Value> {
        let t = load[i];
        t.parse::<f64>().map(|v| json!(v)).map_err(|_| {
            value_error(&format!(
                "could not convert string to float: {}",
                smallworld_core::pyjson::repr_str(t)
            ))
        })
    };
    s.insert(
        "load1".into(),
        if load.is_empty() { Value::Null } else { lf(0)? },
    );
    s.insert(
        "load5".into(),
        if load.len() > 2 { lf(1)? } else { Value::Null },
    );
    s.insert(
        "load15".into(),
        if load.len() > 2 { lf(2)? } else { Value::Null },
    );
    let mem = read("/proc/meminfo")?;
    s.insert("mem_pct".into(), opt_f(parse_meminfo(&mem)?));
    let (mu, mt) =
        parse_meminfo_mb(&mem)?.map_or((Value::Null, Value::Null), |(u, t)| (json!(u), json!(t)));
    s.insert("mem_used_mb".into(), mu);
    s.insert("mem_total_mb".into(), mt);
    s.insert(
        "disk_pct".into(),
        opt_f(health::disk_used_pct(sdir).map(|v| py_round(v, 1))),
    );
    if let Ok((total, used, _)) = crate::sys::disk_usage(sdir) {
        #[allow(clippy::cast_precision_loss)]
        let gb = |b: u64| json!(py_round(b as f64 / f64::from(1u32 << 30), 2));
        s.insert("disk_used_gb".into(), gb(used));
        s.insert("disk_total_gb".into(), gb(total));
    } else {
        s.insert("disk_used_gb".into(), Value::Null);
        s.insert("disk_total_gb".into(), Value::Null);
    }
    let net = parse_net_dev(&read("/proc/net/dev")?)?;
    let dt = now - counters.at;
    match counters.net {
        Some(prev) if counters.at != 0.0 && dt > 0.0 && net.0 >= prev.0 && net.1 >= prev.1 => {
            #[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation)]
            let rate = |a: i64, b: i64| json!(((a - b) as f64 / dt).trunc() as i64);
            s.insert("net_rx_bps".into(), rate(net.0, prev.0));
            s.insert("net_tx_bps".into(), rate(net.1, prev.1));
        }
        _ => {
            s.insert("net_rx_bps".into(), Value::Null);
            s.insert("net_tx_bps".into(), Value::Null);
        }
    }
    counters.net = Some(net);
    counters.at = now;
    s.insert(
        "admin_stopped".into(),
        Value::Bool(crate::notify::admin_stopped()),
    );
    Ok(Value::Object(s))
}

/// `Spool`: undelivered samples, one JSON object per line, oldest first, at most `SPOOL_MAX_AGE_S` old.
pub struct Spool {
    pub path: PathBuf,
    lock: Mutex<()>,
}

impl Spool {
    pub fn new(path: PathBuf) -> Self {
        Self {
            path,
            lock: Mutex::new(()),
        }
    }

    /// `_load()`: the valid lines (objects with an integer `ts`).
    fn load(&self) -> AResult<Vec<(i64, Value)>> {
        let Ok(bytes) = std::fs::read(&self.path) else {
            return Ok(Vec::new());
        };
        let text = String::from_utf8(bytes).map_err(|_| {
            AgentError::Value(
                "UnicodeDecodeError".into(),
                "'utf-8' codec can't decode".into(),
            )
        })?;
        let mut out = Vec::new();
        for line in crate::systemd::splitlines(&text) {
            if let Some(item @ Value::Object(_)) = smallworld_core::pyjson::loads(line)
                && let Some(ts) = item.get("ts").and_then(as_py_int_incl_bool)
            {
                out.push((ts, item));
            }
        }
        Ok(out)
    }

    /// `_save(items)`: `<name>.tmp` then `os.replace` (umask 077: 0600).
    fn save(&self, items: &[(i64, Value)]) -> AResult<()> {
        let text: String = items
            .iter()
            .map(|(_, i)| smallworld_core::pyjson::dumps_compact(i) + "\n")
            .collect();
        smallworld_core::fsx::replace_write(&self.path, text.as_bytes(), None)?;
        Ok(())
    }

    fn cutoff(now: f64) -> i64 {
        #[allow(clippy::cast_possible_truncation)]
        let c = ((now - SPOOL_MAX_AGE_S) * 1000.0).trunc() as i64;
        c
    }

    /// `add(sample, now)`.
    ///
    /// # Errors
    /// The `OSError` of the write.
    pub fn add(&self, sample: &Value, now: f64) -> AResult<()> {
        let _g = self.lock.lock().unwrap_or_else(PoisonError::into_inner);
        let cutoff = Self::cutoff(now);
        let mut items: Vec<(i64, Value)> = self
            .load()?
            .into_iter()
            .filter(|(ts, _)| *ts >= cutoff)
            .collect();
        let ts = sample.get("ts").and_then(as_py_int_incl_bool).unwrap_or(0);
        items.push((ts, sample.clone()));
        self.save(&items)
    }

    /// `flush(send, now)`: everything in batches, oldest first, stopping at the first failure; the rest stays.
    ///
    /// # Errors
    /// The first `send` error (after saving what is left), or the write's `OSError` (`Err(Ok(..))`).
    pub fn flush(
        &self,
        send: &mut dyn FnMut(&[Value]) -> Result<(), IngestError>,
        now: f64,
    ) -> Result<usize, Result<AgentError, IngestError>> {
        let _g = self.lock.lock().unwrap_or_else(PoisonError::into_inner);
        let cutoff = Self::cutoff(now);
        let items: Vec<(i64, Value)> = self
            .load()
            .map_err(Ok)?
            .into_iter()
            .filter(|(ts, _)| *ts >= cutoff)
            .collect();
        let mut sent = 0;
        let mut failure = None;
        while sent < items.len() {
            let end = (sent + METRICS_BATCH).min(items.len());
            let batch: Vec<Value> = items[sent..end].iter().map(|(_, v)| v.clone()).collect();
            if let Err(e) = send(&batch) {
                failure = Some(e);
                break;
            }
            sent = end;
        }
        // `finally:` save what is left (a save error replaces the send error, as in Python)
        if sent < items.len() || self.path.exists() {
            self.save(&items[sent..]).map_err(Ok)?;
        }
        match failure {
            Some(e) => Err(Err(e)),
            None => Ok(sent),
        }
    }

    /// `len(spool)`.
    ///
    /// # Errors
    /// A decode error.
    pub fn len(&self) -> AResult<usize> {
        let _g = self.lock.lock().unwrap_or_else(PoisonError::into_inner);
        Ok(self.load()?.len())
    }
}

/// `push_metrics(s, spool, sample)`: queue the sample, deliver the spool; how many went out (0: buffered).
///
/// # Errors
/// A spool write/decode error (Python: uncaught; the metrics loop prints it).
pub fn push_metrics(link: &Link, spool: &Spool, sample: &Value, warn: &Warnings) -> AResult<usize> {
    spool.add(sample, crate::sys::now())?;
    let mut send = |batch: &[Value]| -> Result<(), IngestError> {
        let mut body = Map::new();
        body.insert("agent".into(), json!(VERSION));
        body.insert("samples".into(), Value::Array(batch.to_vec()));
        link.post_json("/ingest/metrics", &Value::Object(body))
            .map(|_| ())
    };
    match spool.flush(&mut send, crate::sys::now()) {
        Ok(n) => {
            warn.ok("metrics");
            Ok(n)
        }
        Err(Err(e)) => {
            warn.fail("metrics", &format!("buffering on disk: {e}"), false);
            Ok(0)
        }
        Err(Ok(e)) => Err(e),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn vectors(name: &str) -> Value {
        let text = match name {
            "parsers" => include_str!("../../../../tests/agent_parity/vectors/parsers.json"),
            "round" => include_str!("../../../../tests/agent_parity/vectors/round.json"),
            "constants" => include_str!("../../../../tests/agent_parity/vectors/constants.json"),
            _ => unreachable!(),
        };
        serde_json::from_str::<Value>(text).unwrap()["vectors"].clone()
    }

    fn ints(v: &Value) -> Vec<i64> {
        v.as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_i64().unwrap())
            .collect()
    }

    #[test]
    fn round_vectors() {
        for c in vectors("round").as_array().unwrap() {
            let x = c["x"].as_f64().unwrap();
            let Some(n) = c["n"].as_u64() else {
                assert_eq!(json!(py_round_int(x)), c["round"], "round({x})");
                continue;
            };
            let want = c["round"].as_f64().unwrap();
            let got = py_round(x, usize::try_from(n).unwrap());
            assert_eq!(got.to_bits(), want.to_bits(), "round({x}, {n})");
        }
    }

    #[test]
    fn round_ties() {
        // the design's tie corpus (half-even on the exact binary value)
        for (x, n, want) in [
            (0.125, 2, 0.12),
            (2.5, 0, 2.0),
            (2.675, 2, 2.67),
            (12.345, 2, 12.35),
            (-0.05, 1, -0.1),
            (-0.04, 1, -0.0),
        ] {
            assert_eq!(py_round(x, n).to_bits(), f64::to_bits(want), "{x} {n}");
        }
        assert!(py_round(f64::INFINITY, 1).is_infinite());
        assert!(py_round(f64::NAN, 1).is_nan());
    }

    #[test]
    fn tps_players_vectors() {
        let v = vectors("parsers");
        for c in v["parse_tps_all"].as_array().unwrap() {
            let got: Map<String, Value> = parse_tps_all(c["input"].as_str().unwrap())
                .into_iter()
                .map(|(k, v)| (k, json!(v)))
                .collect();
            assert_eq!(Value::Object(got), c["output"], "{c}");
        }
        for c in v["player_names"].as_array().unwrap() {
            assert_eq!(
                json!(player_names(c["input"].as_str().unwrap())),
                c["output"],
                "{c}"
            );
        }
        for c in v["players_regex"].as_array().unwrap() {
            let text = strip_formatting(c["input"].as_str().unwrap());
            let got = PLAYERS
                .captures(&text)
                .map(|m| json!([m[1].to_string(), m[2].to_string()]));
            assert_eq!(got.unwrap_or(Value::Null), c["match"], "{c}");
        }
    }

    #[test]
    fn proc_vectors() {
        let v = vectors("parsers");
        let c = &v["parse_net_dev"];
        let (rx, tx) = parse_net_dev(c["input"].as_str().unwrap()).unwrap();
        assert_eq!(vec![rx, tx], ints(&c["output"]));
        let c = &v["parse_meminfo"];
        assert_eq!(
            parse_meminfo(c["input"].as_str().unwrap()).unwrap(),
            c["pct"].as_f64()
        );
        let (u, t) = parse_meminfo_mb(c["input"].as_str().unwrap())
            .unwrap()
            .unwrap();
        assert_eq!(vec![u, t], ints(&c["mb"]));
        let c = &v["parse_cores"];
        let got = parse_cores(c["input"].as_str().unwrap()).unwrap();
        let want: Vec<Vec<i64>> = c["output"].as_array().unwrap().iter().map(ints).collect();
        assert_eq!(got, want);
        for c in v["cpu_busy_pct"].as_array().unwrap() {
            assert_eq!(
                cpu_busy_pct(&ints(&c["prev"]), &ints(&c["cur"])),
                c["pct"].as_f64(),
                "{c}"
            );
        }
        assert!(parse_net_dev("a\nb\n eth0: x 0 0 0 0 0 0 0 1\n").is_err());
        assert_eq!(parse_meminfo("MemTotal: 0 kB\n").unwrap(), None);
    }

    /// /proc captured on a build box (sanitised: interface names and counters only).
    #[test]
    fn captured_proc_fixtures() {
        let stat = "cpu  2255 34 2290 22625563 6290 127 456 0 0 0\ncpu0 1132 34 1441 11311718 4500 111 227 0 0 0\ncpu1 1123 0 849 11313845 1790 16 229 0 0 0\nintr 114930548 113199788 3 0 5 263 0 4 [...]\nctxt 1990473\nbtime 1062191376\nprocesses 2915\nprocs_running 1\nprocs_blocked 0\nsoftirq 183433 0 21755 12 39 1137 231 21459 2263\n";
        assert_eq!(parse_cores(stat).unwrap().len(), 2);
        assert_eq!(
            health::parse_proc_stat(stat).unwrap(),
            vec![2255, 34, 2290, 22_625_563, 6290, 127, 456, 0, 0, 0]
        );
        let netdev = "Inter-|   Receive                                                |  Transmit\n face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n    lo:  884204    9532    0    0    0     0          0         0   884204    9532    0    0    0     0       0          0\n  eth0: 1287931904 1032442    0    0    0     0          0         0 51840217  412911    0    0    0     0       0          0\n";
        assert_eq!(parse_net_dev(netdev).unwrap(), (1_287_931_904, 51_840_217));
        let meminfo = "MemTotal:        8131924 kB\nMemFree:          288436 kB\nMemAvailable:    5619792 kB\nBuffers:          201432 kB\nHugePages_Total:       0\n";
        assert_eq!(parse_meminfo(meminfo).unwrap(), Some(30.9));
        assert_eq!(parse_meminfo_mb(meminfo).unwrap(), Some((2453, 7941)));
    }

    #[test]
    fn constants() {
        let c = vectors("constants");
        assert_eq!(c["MAX_CORES"], json!(MAX_CORES));
        assert_eq!(c["METRICS_BATCH"], json!(METRICS_BATCH));
        assert_eq!(c["SPOOL_MAX_AGE_S"], json!(86400));
        assert_eq!(
            c["RCON_TIMEOUT_S"],
            json!(super::super::persistent_rcon::RCON_TIMEOUT_S)
        );
        assert_eq!(c["INVENTORY_S"], json!(600));
        assert_eq!(c["INVENTORY_MIN_GAP_S"], json!(5));
        assert_eq!(
            c["INVENTORY_MAX"],
            json!(super::super::inventory::INVENTORY_MAX)
        );
        assert_eq!(c["TAIL_MAX_S"], json!(super::super::tail::TAIL_MAX_S));
        assert_eq!(c["TAIL_BATCH"], json!(super::super::tail::TAIL_BATCH));
        let cc = super::super::client_codes::constants();
        for (k, v) in cc.as_object().unwrap() {
            assert_eq!(&c[k], v, "{k}");
        }
        assert_eq!(c["REJECTED_KEEP"], json!(crate::panel::REJECTED_KEEP));
    }

    /// Differential check against a corpus Python generated (`SW_DATA_CORPUS=path cargo test -- --ignored`):
    /// `{"mask": [[addr, out]], "tps": [[text, tps_all, parse_tps, parse_mspt, names, players]]}`.
    #[test]
    #[ignore = "needs a corpus generated from the Python (SW_DATA_CORPUS)"]
    fn differential_corpus() {
        let path = std::env::var("SW_DATA_CORPUS").expect("SW_DATA_CORPUS");
        let c: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        let mut bad = 0;
        for m in c["mask"].as_array().unwrap() {
            let got = crate::panel::rejected::mask_address(m[0].as_str().unwrap());
            if got != m[1].as_str().unwrap() {
                bad += 1;
                eprintln!("mask_address({}) = {got:?}, python {}", m[0], m[1]);
            }
        }
        for t in c["tps"].as_array().unwrap() {
            let text = t[0].as_str().unwrap();
            let all: Vec<Value> = parse_tps_all(text)
                .into_iter()
                .map(|(k, v)| json!([k, v]))
                .collect();
            let pt = health::parse_tps(text).map_or_else(|e| json!(e.to_string()), |v| json!(v));
            let pm = health::parse_mspt(text).map_or_else(|e| json!(e.to_string()), |v| json!(v));
            let clean = strip_formatting(text);
            let pl = PLAYERS
                .captures(&clean)
                .map_or(Value::Null, |m| json!([py_int(&m[1]), py_int(&m[2])]));
            let got = json!([text, all, pt, pm, player_names(text), pl]);
            if &got != t {
                bad += 1;
                eprintln!("{got} != python {t}");
            }
        }
        for r in c["reason"].as_array().map_or(&[][..], Vec::as_slice) {
            let got = crate::panel::rejected::whitelist_reason(r[0].as_str().unwrap());
            if json!(got) != r[1] {
                bad += 1;
                eprintln!("WHITELIST_REASON({}) = {got}, python {}", r[0], r[1]);
            }
        }
        assert_eq!(bad, 0);
    }

    #[test]
    fn spool_keeps_order_drops_old_and_resumes_after_a_failure() {
        let dir = std::env::temp_dir().join(format!("sw-spool-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("spool.jsonl");
        let _ = std::fs::remove_file(&path);
        std::fs::write(&path, "{\"ts\":1}\nnot json\n[1]\n{\"ts\":\"x\"}\n").unwrap();
        let spool = Spool::new(path.clone());
        let now = 1_000_000.0;
        for k in 0..450i64 {
            spool
                .add(&json!({"ts": 999_000_000 + k, "k": k}), now)
                .unwrap();
        }
        assert_eq!(spool.len().unwrap(), 450); // the 1970 sample and the junk are gone
        let mut calls = 0;
        let mut send = |b: &[Value]| {
            calls += 1;
            if calls == 2 {
                Err(IngestError {
                    message: "POST /ingest/metrics: HTTP 503".into(),
                    status: Some(503),
                })
            } else {
                assert!(b.len() <= METRICS_BATCH);
                Ok(())
            }
        };
        match spool.flush(&mut send, now) {
            Err(Err(e)) => assert_eq!(e.status, Some(503)),
            other => panic!("{other:?}"),
        }
        assert_eq!(spool.len().unwrap(), 250);
        let first = std::fs::read_to_string(&path).unwrap();
        assert!(
            first.starts_with("{\"k\":200,\"ts\":999000200}\n"),
            "{first}"
        );
        assert_eq!(spool.flush(&mut |_| Ok(()), now).unwrap(), 250);
        assert_eq!(std::fs::read(&path).unwrap(), b"");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
