//! The parts of scripts/health.py the agent uses in-process: `read_gc_log`, `parse_gc` (+ `GcStats`, `GcEvent`,
//! `gc_event`), `last_heap`, `parse_mspt`, `parse_tps`, `parse_proc_stat`, `steal_pct`, `disk_used_pct`.
//!
//! health.py's patterns have no `re.ASCII`: `\d` is any `Nd` digit (Rust's Unicode `\d`), and `int()`/`float()`
//! of such digits work in Python (`unicode::parse_decimal`, `py_number`). `\s` is Python's (`PY_SPACE`).

use super::{PY_SPACE, read_untrusted};
use crate::types::{AResult, AgentError};
use crate::unicode;
use regex::Regex;
use smallworld_core::pyjson::repr_str;
use smallworld_core::rcon::strip_formatting;
use std::path::Path;
use std::sync::LazyLock;

/// health.py `TRIPLE`.
pub static TRIPLE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)").expect("constant")
});
/// health.py `FLOAT`.
pub static FLOAT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\*?(\d+(?:\.\d+)?)").expect("constant"));
static GC_LINE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"\[(?P<uptime>\d+(?:\.\d+)?)s\].*?GC\(\d+\) (?P<kind>Pause .*?) (?P<before>\d+)M->(?P<after>\d+)M\((?P<cap>\d+)M\) (?P<ms>\d+(?:\.\d+)?)ms",
    )
    .expect("constant")
});
static ZGC_PAUSE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r"\[(?P<uptime>\d+(?:\.\d+)?)s\].*?(?:GC\(\d+\) [YyOo]: (?P<kind>Pause [A-Za-z ()]+?)|(?P<stall>Allocation Stall) \(.*\)) (?P<ms>\d+(?:\.\d+)?)ms{PY_SPACE}*$"
    ))
    .expect("constant")
});
static ZGC_CYCLE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"\[(?P<uptime>\d+(?:\.\d+)?)s\].*?GC\(\d+\) (?:Minor|Major) Collection \(.*?\) (?P<before>\d+)M\((?P<before_pct>\d+)%\)->(?P<after>\d+)M\((?P<after_pct>\d+)%\) \d+(?:\.\d+)?s",
    )
    .expect("constant")
});

/// `float(text)` for a `\d+(?:\.\d+)?` match (any `Nd` digits, as Python's `float()` accepts them).
pub fn py_number(text: &str) -> f64 {
    let ascii: String = text
        .chars()
        .map(|c| {
            if c == '.' {
                '.'
            } else {
                unicode::decimal_value(c)
                    .and_then(|d| char::from_digit(d, 10))
                    .unwrap_or(c)
            }
        })
        .collect();
    ascii.parse().unwrap_or(f64::NAN)
}

/// `int(text)` for a `\d+` match. Beyond 64 bits (Python: a big int) it saturates.
pub fn py_int(text: &str) -> i64 {
    unicode::parse_decimal(text).unwrap_or(i64::MAX)
}

/// health.py `parse_mspt(text)`: the 1-minute average (the last avg/min/max triple).
///
/// # Errors
/// `Value("ValueError", "unrecognised mspt output: {text!r}")`.
pub fn parse_mspt(text: &str) -> AResult<f64> {
    let clean = strip_formatting(text);
    match TRIPLE.captures_iter(&clean).last() {
        Some(c) => Ok(py_number(&c[1])),
        None => Err(AgentError::Value(
            "ValueError".into(),
            format!("unrecognised mspt output: {}", repr_str(text)),
        )),
    }
}

/// The `tps` reply split like health.py: (labels after the last "last", the values after the first colon), or
/// None without a colon.
pub fn tps_parts(text: &str) -> Option<(Vec<String>, Vec<f64>)> {
    let clean = strip_formatting(text);
    let (head, tail) = clean.split_once(':')?;
    let after = head.rsplit_once("last").map_or(head, |(_, a)| a);
    let labels = after
        .split(',')
        .map(|w| smallworld_core::pystr::strip_ws(w).to_string())
        .collect();
    let values = FLOAT
        .captures_iter(tail)
        .map(|c| py_number(&c[1]))
        .collect();
    Some((labels, values))
}

/// health.py `parse_tps(text)`: the 1-minute TPS.
///
/// # Errors
/// `Value("ValueError", "unrecognised tps output: {text!r}")`.
pub fn parse_tps(text: &str) -> AResult<f64> {
    if let Some((labels, values)) = tps_parts(text)
        && values.len() == labels.len()
        && let Some(i) = labels.iter().position(|l| l == "1m")
    {
        return Ok(values[i]);
    }
    Err(AgentError::Value(
        "ValueError".into(),
        format!("unrecognised tps output: {}", repr_str(text)),
    ))
}

/// health.py `GcStats` (the fields the agent reads).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct GcStats {
    pub max_pause_ms: f64,
    pub long_pauses: Vec<f64>,
    pub heap_after_pct: Option<f64>,
    pub last_uptime: f64,
    pub collector: String,
    pub stalls: Vec<f64>,
}

impl GcStats {
    /// `freeze_ms()`.
    pub fn freeze_ms(&self) -> f64 {
        self.max_pause_ms.max(self.stalls.iter().sum())
    }
}

/// health.py `GcEvent`.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct GcEvent {
    pub uptime: f64,
    pub pause_ms: Option<f64>,
    pub after_mb: Option<i64>,
    pub after_pct: Option<f64>,
    pub cap_mb: Option<i64>,
    pub stall: bool,
    pub zgc: bool,
}

/// health.py `gc_event(line)`.
pub fn gc_event(line: &str) -> Option<GcEvent> {
    if let Some(m) = GC_LINE.captures(line) {
        let (after, cap) = (py_int(&m["after"]), py_int(&m["cap"]));
        #[allow(clippy::cast_precision_loss)]
        let pct = if cap == 0 {
            None
        } else {
            Some(100.0 * after as f64 / cap as f64)
        };
        return Some(GcEvent {
            uptime: py_number(&m["uptime"]),
            pause_ms: Some(py_number(&m["ms"])),
            after_mb: Some(after),
            after_pct: pct,
            cap_mb: Some(cap),
            ..GcEvent::default()
        });
    }
    if let Some(m) = ZGC_PAUSE.captures(line) {
        return Some(GcEvent {
            uptime: py_number(&m["uptime"]),
            pause_ms: Some(py_number(&m["ms"])),
            stall: m.name("stall").is_some_and(|s| !s.as_str().is_empty()),
            zgc: true,
            ..GcEvent::default()
        });
    }
    if let Some(m) = ZGC_CYCLE.captures(line) {
        let pairs = [
            (py_int(&m["before"]), py_int(&m["before_pct"])),
            (py_int(&m["after"]), py_int(&m["after_pct"])),
        ];
        // max(pairs, key=pct): the first of equal maxima
        let (mb, pct) = if pairs[1].1 > pairs[0].1 {
            pairs[1]
        } else {
            pairs[0]
        };
        #[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation)]
        let cap = if pct == 0 {
            None
        } else {
            Some((mb as f64 * 100.0 / pct as f64).round_ties_even() as i64)
        };
        #[allow(clippy::cast_precision_loss)]
        return Some(GcEvent {
            uptime: py_number(&m["uptime"]),
            after_mb: Some(py_int(&m["after"])),
            after_pct: Some(py_int(&m["after_pct"]) as f64),
            cap_mb: cap,
            zgc: true,
            ..GcEvent::default()
        });
    }
    None
}

/// health.py `gc_events(lines)`.
pub fn gc_events(lines: &[String]) -> Vec<GcEvent> {
    lines.iter().filter_map(|l| gc_event(l)).collect()
}

/// health.py `parse_gc(lines, since_uptime, pause_alert_ms)`.
pub fn parse_gc(lines: &[String], since_uptime: f64, pause_alert_ms: f64) -> GcStats {
    let events = gc_events(lines);
    let mut since = since_uptime;
    if events.last().is_some_and(|e| e.uptime < since) {
        since = 0.0;
    }
    let mut stats = GcStats {
        last_uptime: since,
        ..GcStats::default()
    };
    let mut afters: Vec<f64> = Vec::new();
    if !events.is_empty() {
        stats.collector = if events.iter().any(|e| e.zgc) {
            "zgc"
        } else {
            "g1"
        }
        .into();
    }
    for e in &events {
        if let Some(p) = e.after_pct {
            afters.push(p);
        }
        if e.uptime <= since {
            continue;
        }
        stats.last_uptime = py_max(stats.last_uptime, e.uptime);
        let Some(pause) = e.pause_ms else { continue };
        if e.stall {
            stats.stalls.push(pause);
            continue;
        }
        stats.max_pause_ms = py_max(stats.max_pause_ms, pause);
        if pause > pause_alert_ms {
            stats.long_pauses.push(pause);
        }
    }
    if !afters.is_empty() {
        let recent = &afters[afters.len().saturating_sub(10)..];
        stats.heap_after_pct = recent.iter().copied().reduce(py_min);
    }
    stats
}

/// Python's `max(a, b)`: `b` only if `b > a` (keeps `a` on ties and NaN).
pub fn py_max(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// Python's `min(a, b)`.
pub fn py_min(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

/// health.py `last_heap(lines)`: (MB after the last collection, capacity MB).
pub fn last_heap(lines: &[String]) -> Option<(i64, i64)> {
    let heaps: Vec<GcEvent> = gc_events(lines)
        .into_iter()
        .filter(|e| e.after_mb.is_some() && e.cap_mb.is_some_and(|c| c != 0))
        .collect();
    let last = heaps.last()?;
    if last.pause_ms.is_some() {
        return Some((last.after_mb.unwrap_or(0), last.cap_mb.unwrap_or(0)));
    }
    let mut caps: Vec<i64> = heaps[heaps.len().saturating_sub(20)..]
        .iter()
        .filter(|e| e.pause_ms.is_none())
        .map(|e| e.cap_mb.unwrap_or(0))
        .collect();
    caps.sort_unstable();
    Some((last.after_mb.unwrap_or(0), caps[caps.len() / 2]))
}

/// health.py `GC_LOG_TMPFS`.
pub const GC_LOG_TMPFS: &str = "/run/mc-telemetry/gc.log";
/// health.py `GC_LOG_TAIL_BYTES`.
pub const GC_LOG_TAIL_BYTES: usize = 1 << 20;

/// health.py `read_gc_log(server_dir, max_lines=2000)`: the last lines, `keepends=True`.
pub fn read_gc_log(server_dir: &Path) -> Vec<String> {
    let tmpfs = Path::new(GC_LOG_TMPFS);
    let path = if smallworld_core::fsx::exists(tmpfs) {
        tmpfs.to_path_buf()
    } else {
        server_dir.join("logs").join("gc.log")
    };
    read_gc_log_at(&path, 2000)
}

/// `read_gc_log` on a given file.
pub fn read_gc_log_at(path: &Path, max_lines: usize) -> Vec<String> {
    let Ok(data) = read_untrusted(path, GC_LOG_TAIL_BYTES, true) else {
        return Vec::new();
    };
    let text = String::from_utf8_lossy(&data);
    let lines = splitlines_keepends(&text);
    let skip = lines.len().saturating_sub(max_lines);
    lines.into_iter().skip(skip).map(str::to_string).collect()
}

/// `str.splitlines(keepends=True)`.
pub fn splitlines_keepends(text: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut it = text.char_indices().peekable();
    while let Some((i, c)) = it.next() {
        if matches!(
            c,
            '\n' | '\r'
                | '\u{0b}'
                | '\u{0c}'
                | '\u{1c}'
                | '\u{1d}'
                | '\u{1e}'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        ) {
            let mut end = i + c.len_utf8();
            if c == '\r' && it.peek().is_some_and(|&(_, n)| n == '\n') {
                it.next();
                end += 1;
            }
            out.push(&text[start..end]);
            start = end;
        }
    }
    if start < text.len() {
        out.push(&text[start..]);
    }
    out
}

/// health.py `parse_proc_stat(text)`: the aggregate `cpu` counters.
///
/// # Errors
/// `Value("ValueError", ...)`: no aggregate line, or a field that isn't an integer.
pub fn parse_proc_stat(text: &str) -> AResult<Vec<i64>> {
    for line in crate::systemd::splitlines(text) {
        let parts = smallworld_core::pystr::split_ws(line);
        if parts.first() == Some(&"cpu") {
            return parts[1..].iter().map(|x| py_int_str(x)).collect();
        }
    }
    Err(AgentError::Value(
        "ValueError".into(),
        "no aggregate cpu line in /proc/stat".into(),
    ))
}

/// Python's `int(x)` for a whitespace-free token (sign, ASCII or `Nd` digits, `_` between digits).
///
/// # Errors
/// `Value("ValueError", "invalid literal for int() with base 10: 'x'")`.
pub fn py_int_str(x: &str) -> AResult<i64> {
    let err = || {
        AgentError::Value(
            "ValueError".into(),
            format!("invalid literal for int() with base 10: {}", repr_str(x)),
        )
    };
    let (neg, body) = match x.strip_prefix('-') {
        Some(b) => (true, b),
        None => (false, x.strip_prefix('+').unwrap_or(x)),
    };
    if body.is_empty() || body.starts_with('_') || body.ends_with('_') || body.contains("__") {
        return Err(err());
    }
    let digits: String = body.chars().filter(|&c| c != '_').collect();
    if !digits.chars().all(|c| unicode::decimal_value(c).is_some()) {
        return Err(err());
    }
    let v = unicode::parse_decimal(&digits).ok_or_else(err)?;
    Ok(if neg { -v } else { v })
}

/// health.py `steal_pct(prev, cur)`.
pub fn steal_pct(prev: &[i64], cur: &[i64]) -> Option<f64> {
    let (delta, total) = deltas(prev, cur)?;
    #[allow(clippy::cast_precision_loss)]
    Some(100.0 * delta[7] as f64 / total as f64)
}

/// The first 8 counters' deltas and their sum, None if unusable (`cpu_busy_pct`/`steal_pct`'s checks).
pub fn deltas(prev: &[i64], cur: &[i64]) -> Option<([i64; 8], i64)> {
    if prev.len() < 8 || cur.len() < 8 {
        return None;
    }
    let mut d = [0i64; 8];
    for i in 0..8 {
        d[i] = cur[i] - prev[i];
    }
    let total: i64 = d.iter().sum();
    if total <= 0 || d.iter().any(|&x| x < 0) {
        return None;
    }
    Some((d, total))
}

/// health.py `disk_used_pct(path)`.
pub fn disk_used_pct(path: &Path) -> Option<f64> {
    let (total, used, _) = crate::sys::disk_usage(path).ok()?;
    #[allow(clippy::cast_precision_loss)]
    if total == 0 {
        None
    } else {
        Some(100.0 * used as f64 / total as f64)
    }
}

#[cfg(test)]
#[allow(clippy::float_cmp)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn vectors() -> Value {
        serde_json::from_str(include_str!(
            "../../../../tests/agent_parity/vectors/parsers.json"
        ))
        .unwrap()
    }

    #[test]
    fn tps_and_mspt_vectors() {
        let v = vectors();
        for c in v["vectors"]["parse_tps"].as_array().unwrap() {
            let got = parse_tps(c["input"].as_str().unwrap());
            if let Some(ok) = c.get("ok") {
                assert_eq!(got, Ok(ok.as_f64().unwrap()), "{c}");
            } else {
                assert_eq!(
                    got,
                    Err(AgentError::Value(
                        "ValueError".into(),
                        c["error"].as_str().unwrap().into()
                    )),
                    "{c}"
                );
            }
        }
        for c in v["vectors"]["parse_mspt"].as_array().unwrap() {
            let got = parse_mspt(c["input"].as_str().unwrap());
            if let Some(ok) = c.get("ok") {
                assert_eq!(got, Ok(ok.as_f64().unwrap()), "{c}");
            } else {
                assert_eq!(
                    got.unwrap_err().to_string(),
                    c["error"].as_str().unwrap(),
                    "{c}"
                );
            }
        }
    }

    #[test]
    fn gc_vectors() {
        let v = vectors();
        for c in v["vectors"]["gc"].as_object().map(|o| vec![o]).unwrap() {
            let lines: Vec<String> = c["lines"]
                .as_array()
                .unwrap()
                .iter()
                .map(|l| l.as_str().unwrap().to_string())
                .collect();
            let want = c["last_heap"].as_array().unwrap();
            assert_eq!(
                last_heap(&lines),
                Some((want[0].as_i64().unwrap(), want[1].as_i64().unwrap()))
            );
            let gc = parse_gc(&lines, 0.0, 100.0);
            let max = gc.stalls.iter().copied().fold(gc.max_pause_ms, py_max);
            assert_eq!(max, c["max_pause_ms"].as_f64().unwrap());
        }
    }

    #[test]
    fn proc_stat_and_steal() {
        let v = vectors();
        let c = &v["vectors"]["parse_cores"];
        let want: Vec<i64> = c["proc_stat"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_i64().unwrap())
            .collect();
        assert_eq!(parse_proc_stat(c["input"].as_str().unwrap()), Ok(want));
        assert!(parse_proc_stat("intr 1\n").is_err());
        assert!(parse_proc_stat("cpu 1 x\n").is_err());
        assert_eq!(
            steal_pct(&[0, 0, 0, 0, 0, 0, 0, 0], &[1, 1, 1, 1, 1, 1, 1, 1]),
            Some(12.5)
        );
        assert_eq!(steal_pct(&[], &[1; 8]), None);
    }

    #[test]
    fn gc_restart_and_unicode_digits() {
        let lines = vec![
            "[200.0s][info][gc] GC(1) Pause Young (Normal) (G1 Evacuation Pause) 10M->5M(20M) 3.0ms\n".to_string(),
            "[\u{0661}0.0s][info][gc] GC(2) Pause Young (Normal) (G1 Evacuation Pause) 10M->4M(20M) 7.5ms\n".to_string(),
        ];
        let gc = parse_gc(&lines, 150.0, 5.0);
        // the last event is older than `since`: the JVM restarted, everything counts
        assert_eq!(gc.max_pause_ms, 7.5);
        assert_eq!(gc.long_pauses, vec![7.5]);
        assert_eq!(gc.last_uptime, 200.0);
        assert_eq!(gc.heap_after_pct, Some(20.0));
        assert_eq!(gc.collector, "g1");
        assert_eq!(last_heap(&lines), Some((4, 20)));
    }

    #[test]
    fn keepends() {
        assert_eq!(
            splitlines_keepends("a\nb\r\nc\rd"),
            vec!["a\n", "b\r\n", "c\r", "d"]
        );
    }
}
