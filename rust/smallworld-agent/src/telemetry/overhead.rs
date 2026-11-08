//! Product overhead (telemetry.py 394-505): `parse_show`, `unit_stats` (`systemctl show` of the long-running
//! units' cgroup counters), `consumed_cpu_ms` (systemd's end-of-run "Consumed ..." line of a oneshot unit) and
//! `Overhead` (per-minute deltas plus the oneshots seen since the last minute). The oneshots are fed from the
//! logs thread (`observe_journal`, as the Python agent's `ship_logs(observe_all=...)` does), so they sit behind a
//! lock; everything else belongs to the telemetry thread.

#[cfg(test)]
use super::json::J;
use super::json::OMap;
use crate::data::metrics::py_round;
use crate::types::Obj;
use regex::Regex;
use std::sync::{LazyLock, Mutex};
use std::time::Duration;

/// telemetry.py `OVERHEAD_UNITS`.
pub const OVERHEAD_UNITS: [&str; 2] = ["minecraft.service", "mc-agent.service"];

/// `ONESHOT_UNITS.match(unit)` (`$`: also before one final "\n").
static ONESHOT_UNITS: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(mc-[a-z0-9@._-]+\.service)\n?\z").expect("ONESHOT_UNITS"));
/// `CONSUMED` (`\d`: any `Nd` digit, as Python's str pattern).
static CONSUMED: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"Consumed (?:(\d+)min )?(\d+(?:\.\d+)?)(ms|s) CPU time").expect("CONSUMED")
});
static MEMORY_PEAK: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(\d+(?:\.\d+)?)([KMG]) memory peak").expect("MEMORY_PEAK"));

/// One unit's counters: property -> `int(v) if v.isdigit() else None`, in `systemctl show`'s order.
pub type UnitStats = Vec<(String, Option<i128>)>;
/// `{unit: counters}` in output order.
pub type Stats = Vec<(String, UnitStats)>;

fn stat(u: &UnitStats, k: &str) -> Option<i128> {
    u.iter().find(|(name, _)| name == k).and_then(|(_, v)| *v)
}

/// `str.isdigit()` then `int()`: decimal digits (any `Nd`). A value beyond i128 reads as None (it can't occur:
/// systemd prints u64).
fn digits_value(v: &str) -> Option<i128> {
    if v.is_empty() {
        return None;
    }
    let mut n: i128 = 0;
    for c in v.chars() {
        let d = crate::unicode::decimal_value(c)?;
        n = n.checked_mul(10)?.checked_add(i128::from(d))?;
    }
    Some(n)
}

/// telemetry.py `parse_show(text)`.
pub fn parse_show(text: &str) -> Stats {
    let mut out: Stats = Vec::new();
    for block in smallworld_core::pystr::strip_ws(text).split("\n\n") {
        let mut props: Vec<(String, String)> = Vec::new();
        for line in crate::systemd::splitlines(block) {
            if let Some((k, v)) = line.split_once('=') {
                if let Some(slot) = props.iter_mut().find(|(name, _)| name == k) {
                    slot.1 = v.to_string();
                } else {
                    props.push((k.to_string(), v.to_string()));
                }
            }
        }
        let unit = props
            .iter()
            .position(|(k, _)| k == "Id")
            .map(|i| props.remove(i).1)
            .unwrap_or_default();
        if !unit.is_empty() {
            let vals = props
                .into_iter()
                .map(|(k, v)| {
                    let n = digits_value(&v);
                    (k, n)
                })
                .collect();
            if let Some(slot) = out.iter_mut().find(|(u, _)| *u == unit) {
                slot.1 = vals;
            } else {
                out.push((unit, vals));
            }
        }
    }
    out
}

/// What `unit_stats` can fail with that Python doesn't catch: `text=True`'s `UnicodeDecodeError`.
#[derive(Debug)]
pub struct NotUtf8;

/// telemetry.py `unit_stats(units)`: `{}` on an `OSError`/`SubprocessError` (a non-zero exit included).
///
/// # Errors
/// `NotUtf8` (Python: an uncaught `UnicodeDecodeError`).
pub fn unit_stats(units: &[&str]) -> Result<Stats, NotUtf8> {
    let mut argv = vec![
        "systemctl",
        "show",
        "-p",
        "Id",
        "-p",
        "CPUUsageNSec",
        "-p",
        "MemoryCurrent",
        "-p",
        "MemoryPeak",
        "-p",
        "IOReadBytes",
        "-p",
        "IOWriteBytes",
    ];
    argv.extend_from_slice(units);
    let Ok((code, out)) = crate::systemd::run(&argv, Duration::from_secs(10)) else {
        return Ok(Vec::new());
    };
    if code != Some(0) {
        return Ok(Vec::new());
    }
    let text = String::from_utf8(out).map_err(|_| NotUtf8)?;
    Ok(parse_show(&text))
}

/// `float(text)` of a `\d+(?:\.\d+)?` match.
fn py_float(text: &str) -> f64 {
    crate::data::health::py_number(text)
}

/// telemetry.py `consumed_cpu_ms(msg)`: (CPU ms, memory peak MB or None), None if it isn't the line.
pub fn consumed_cpu_ms(msg: &str) -> Option<(f64, Option<f64>)> {
    let m = CONSUMED.captures(msg)?;
    let ms = py_float(&m[2]) * (if &m[3] == "s" { 1000.0 } else { 1.0 })
        + m.get(1).map_or(0.0, |min| {
            #[allow(clippy::cast_precision_loss)]
            let minutes = crate::data::health::py_int(min.as_str()) as f64;
            minutes * 60000.0
        });
    let mb = MEMORY_PEAK.captures(msg).map(|p| {
        let factor = match &p[2] {
            "K" => 1.0 / 1024.0,
            "M" => 1.0,
            _ => 1024.0,
        };
        py_float(&p[1]) * factor
    });
    Some((ms, mb))
}

#[derive(Clone, Debug, Default)]
struct Oneshot {
    cpu_ms: f64,
    runs: i64,
    mem_peak_mb: f64,
}

/// telemetry.py `Overhead`.
#[derive(Debug, Default)]
pub struct Overhead {
    /// The last sample's counters and time (the telemetry thread's).
    prev: Mutex<(Stats, f64)>,
    oneshots: Mutex<Vec<(String, Oneshot)>>,
}

fn py_str_field(v: Option<&crate::types::Value>) -> String {
    match v {
        None | Some(crate::types::Value::Null) => String::new(),
        Some(v) if !crate::types::truthy(v) => String::new(),
        Some(v) => smallworld_core::pyjson::py_str(v),
    }
}

impl Overhead {
    /// `observe_journal(entries)`: systemd's end-of-run lines of our oneshot units (entries as journal_entry
    /// builds them: `ident`, `sd_unit`, `msg`).
    pub fn observe_journal(&self, entries: &[Obj]) {
        for e in entries {
            let unit = py_str_field(e.get("sd_unit"));
            if e.get("ident").and_then(|v| v.as_str()) != Some("systemd")
                || !ONESHOT_UNITS.is_match(&unit)
            {
                continue;
            }
            let msg = match e.get("msg") {
                None => String::new(),
                Some(v) => smallworld_core::pyjson::py_str(v),
            };
            let Some((ms, mb)) = consumed_cpu_ms(&msg) else {
                continue;
            };
            let key = unit.split('@').next().unwrap_or_default().to_string();
            let mut shots = crate::daemon::lock(&self.oneshots);
            let i = if let Some(i) = shots.iter().position(|(k, _)| *k == key) {
                i
            } else {
                shots.push((key, Oneshot::default()));
                shots.len() - 1
            };
            let acc = &mut shots[i].1;
            acc.cpu_ms += ms;
            acc.runs += 1;
            let peak = mb.unwrap_or(0.0);
            if peak > acc.mem_peak_mb {
                acc.mem_peak_mb = peak;
            }
        }
    }

    /// `sample(stats, now)`: the minute's `ovh` object.
    #[allow(clippy::cast_precision_loss)]
    pub fn sample(&self, stats: Stats, now: f64) -> OMap {
        let mut out = OMap::new();
        let mut prev = crate::daemon::lock(&self.prev);
        let dt = if prev.1 == 0.0 { 0.0 } else { now - prev.1 };
        let empty: UnitStats = Vec::new();
        for (unit, cur) in &stats {
            let old = prev
                .0
                .iter()
                .find(|(u, _)| u == unit)
                .map_or(&empty, |(_, s)| s);
            let mb = |v: Option<i128>| v.map(|b| py_round(b as f64 / 1_048_576.0, 1));
            let mut row = OMap::new()
                .with("mem_mb", mb(stat(cur, "MemoryCurrent")))
                .with("mem_peak_mb", mb(stat(cur, "MemoryPeak")));
            let (c0, c1) = (stat(old, "CPUUsageNSec"), stat(cur, "CPUUsageNSec"));
            let cpu = match (c0, c1) {
                (Some(c0), Some(c1)) if dt > 0.0 && c1 >= c0 => {
                    Some(py_round(100.0 * (c1 - c0) as f64 / 1e9 / dt, 3))
                }
                _ => None,
            };
            row.insert("cpu_pct", cpu);
            for (k, name) in [
                ("IOReadBytes", "io_read_kb"),
                ("IOWriteBytes", "io_write_kb"),
            ] {
                let v = match (stat(old, k), stat(cur, k)) {
                    (Some(a), Some(b)) if b >= a => Some(py_round((b - a) as f64 / 1024.0, 1)),
                    _ => None,
                };
                row.insert(name, v);
            }
            out.insert(unit.as_str(), row);
        }
        {
            let mut shots = crate::daemon::lock(&self.oneshots);
            for (unit, acc) in shots.iter() {
                out.insert(
                    unit.as_str(),
                    OMap::new()
                        .with("cpu_ms", py_round(acc.cpu_ms, 1))
                        .with("runs", acc.runs)
                        .with("mem_peak_mb", py_round(acc.mem_peak_mb, 1)),
                );
            }
            shots.clear();
        }
        *prev = (stats, now);
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn show_and_consumed_like_python() {
        let s = parse_show(
            "Id=minecraft.service\nCPUUsageNSec=1000000000\nMemoryCurrent=[not set]\nMemoryPeak=18446744073709551615\n\nId=mc-agent.service\nCPUUsageNSec=5\n",
        );
        assert_eq!(s[0].0, "minecraft.service");
        assert_eq!(stat(&s[0].1, "MemoryCurrent"), None);
        assert_eq!(
            stat(&s[0].1, "MemoryPeak"),
            Some(18_446_744_073_709_551_615)
        );
        assert_eq!(
            consumed_cpu_ms("mc-backup.service: Consumed 1min 2.5s CPU time, 12.0M memory peak."),
            Some((62500.0, Some(12.0)))
        );
        assert_eq!(
            consumed_cpu_ms("Consumed 900ms CPU time, 512K memory peak"),
            Some((900.0, Some(0.5)))
        );
        assert_eq!(consumed_cpu_ms("nothing"), None);
        let o = Overhead::default();
        o.observe_journal(&[
            json!({"ident": "systemd", "sd_unit": "mc-restore@x.service",
            "msg": "mc-restore@x.service: Consumed 2.25s CPU time"})
            .as_object()
            .unwrap()
            .clone(),
        ]);
        let first = o.sample(s.clone(), 100.0);
        let mut later = s;
        later[0].1[0].1 = Some(4_000_000_000);
        let second = o.sample(later, 160.0);
        assert_eq!(
            J::Obj(first).dumps_compact(),
            r#"{"minecraft.service":{"mem_mb":null,"mem_peak_mb":17592186044416.0,"cpu_pct":null,"io_read_kb":null,"io_write_kb":null},"mc-agent.service":{"mem_mb":null,"mem_peak_mb":null,"cpu_pct":null,"io_read_kb":null,"io_write_kb":null},"mc-restore":{"cpu_ms":2250.0,"runs":1,"mem_peak_mb":0.0}}"#
        );
        assert_eq!(
            second
                .get("minecraft.service")
                .and_then(J::as_obj)
                .and_then(|r| r.get("cpu_pct")),
            Some(&J::Float(5.0))
        );
        assert!(second.get("mc-restore").is_none());
    }
}
