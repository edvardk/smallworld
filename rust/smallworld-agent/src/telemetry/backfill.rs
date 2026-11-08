//! Quiet time and backfill (telemetry.py 701-822): `in_window`, `quiet`, `Pacer` (adaptive batch size and
//! back-off, kept across rounds) and `Backfill.run_once` (undelivered segments, oldest first, a batch at a time,
//! the quiet condition checked before every request, progress saved after every batch).

use super::json::J;
use super::segments::{Segments, read_segment};
use super::snapshot::Snapshot;
use super::{PyExc, TResult};
use regex::Regex;
use std::sync::LazyLock;

pub const BACKFILL_BATCH: usize = 3;
pub const BACKFILL_BACKOFF_S: f64 = 60.0;
pub const BACKFILL_BACKOFF_MAX_S: f64 = 3600.0;
pub const BACKFILL_ROW_GIVE_UP: u32 = 5;
pub const BACKFILL_GROW_AFTER: u32 = 20;
/// `SERVER_ERROR.search(str(err))` (Unicode `\b` and `\d`, as Python's str pattern).
static SERVER_ERROR: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\bHTTP 5\d\d\b").expect("SERVER_ERROR"));

/// telemetry.py `in_window(window, now_local)`: `HH:MM-HH:MM` local time (may wrap midnight); empty = never.
/// The window was checked by `Config.from_env` (`(\d\d:\d\d-\d\d:\d\d)?`, any `Nd` digits).
pub fn in_window(window: &str, hour: u32, minute: u32) -> bool {
    if window.is_empty() {
        return false;
    }
    let Some((start, end)) = window.split_once('-') else {
        return false;
    };
    let hm = |s: &str| -> i64 {
        let (h, m) = s.split_once(':').unwrap_or((s, "0"));
        crate::unicode::parse_decimal(h).unwrap_or(0) * 60
            + crate::unicode::parse_decimal(m).unwrap_or(0)
    };
    let (t, a, b) = (i64::from(hour) * 60 + i64::from(minute), hm(start), hm(end));
    if a <= b {
        a <= t && t < b
    } else {
        t >= a || t < b
    }
}

/// telemetry.py `quiet(latest, window, now_local)`: nobody online and the last window healthy (p99 < 50 ms), or
/// inside the window, never while anyone is online.
pub fn quiet(latest: Option<&Snapshot>, window: &str, hour: u32, minute: u32) -> bool {
    let Some(latest) = latest else {
        return in_window(window, hour, minute);
    };
    if !latest.pl.is_empty() {
        return false;
    }
    let healthy = latest.srv.get("p99").is_none_or(|p| p < 50.0);
    healthy || in_window(window, hour, minute)
}

/// telemetry.py `Pacer`.
#[derive(Debug, Clone)]
pub struct Pacer {
    pub clock: fn() -> f64,
    pub size: usize,
    pub failures: u32,
    pub row_failures: u32,
    pub successes: u32,
    pub retry_at: f64,
}

impl Pacer {
    pub fn new(clock: fn() -> f64) -> Self {
        Self {
            clock,
            size: BACKFILL_BATCH,
            failures: 0,
            row_failures: 0,
            successes: 0,
            retry_at: 0.0,
        }
    }

    pub fn ready(&self) -> bool {
        (self.clock)() >= self.retry_at
    }

    pub fn succeeded(&mut self) {
        self.failures = 0;
        self.row_failures = 0;
        self.successes += 1;
        if self.size < BACKFILL_BATCH && self.successes >= BACKFILL_GROW_AFTER {
            self.size += 1;
            self.successes = 0;
        }
    }

    /// `failed(err, batch_len)`: true when the (single) row should be given up.
    pub fn failed(&mut self, err: &str, batch_len: usize) -> bool {
        self.successes = 0;
        let server = SERVER_ERROR.is_match(err);
        if server && batch_len > 1 {
            self.size = (batch_len / 2).max(1);
            return false;
        }
        self.failures += 1;
        if server {
            self.row_failures += 1;
        }
        let exp = (self.failures - 1).min(10);
        let wait = BACKFILL_BACKOFF_MAX_S.min(BACKFILL_BACKOFF_S * f64::from(1_u32 << exp));
        self.retry_at = (self.clock)() + wait;
        if self.row_failures >= BACKFILL_ROW_GIVE_UP {
            self.row_failures = 0;
            return true;
        }
        false
    }
}

/// What one `run_once` did (Python's `done` dict).
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct Done {
    pub rows: usize,
    pub segments: usize,
    pub requests: usize,
    pub stopped: usize,
    pub failed: usize,
    pub given_up: usize,
}

/// `send_segment(name, data)`.
pub type SendSegment<'a> = dyn FnMut(&str, Vec<u8>) -> Result<(), String> + 'a;

/// telemetry.py `Backfill`'s callables.
pub struct Backfill<'a> {
    pub segments: &'a Segments,
    /// `send_rows(rows)`: `Err(str(e))` on failure.
    pub send_rows: &'a mut dyn FnMut(Vec<J>) -> Result<(), String>,
    /// `send_segment(name, data)`.
    pub send_segment: &'a mut SendSegment<'a>,
    pub may_run: &'a dyn Fn() -> bool,
    pub pause_s: f64,
    pub sleep: &'a dyn Fn(f64),
    pub pacer: &'a mut Pacer,
    pub log: &'a dyn Fn(&str),
}

fn os(e: &std::io::Error) -> PyExc {
    PyExc::from_io(e, None)
}

impl Backfill<'_> {
    /// `run_once(max_requests)`.
    ///
    /// # Errors
    /// What Python doesn't catch there: an `OSError` saving the delivery state.
    pub fn run_once(&mut self, max_requests: usize) -> TResult<Done> {
        let mut done = Done::default();
        let stop = |done: &mut Done, pacer: &Pacer| -> bool {
            if done.requests >= max_requests || !(self.may_run)() || !pacer.ready() {
                done.stopped = 1;
                return true;
            }
            false
        };
        for (name, st) in self.segments.pending() {
            let path = self.segments.directory.join(&name);
            let st_todo: Vec<J> = st
                .as_obj()
                .and_then(|m| m.get("todo"))
                .and_then(J::as_list)
                .cloned()
                .unwrap_or_default();
            let r2 = st.as_obj().and_then(|m| m.get("r2")).is_some_and(J::truthy);
            let Some(rows) = read_segment(&path) else {
                // unreadable: give up on it (a gap)
                self.segments
                    .mark(&name, &st_todo, Some(true))
                    .map_err(|e| os(&e))?;
                continue;
            };
            let mut todo: Vec<J> = st_todo
                .into_iter()
                .filter(|i| {
                    i.as_py_int()
                        .is_some_and(|i| usize::try_from(i).map_or(i < 0, |u| u < rows.len()))
                })
                .collect();
            while !todo.is_empty() {
                if stop(&mut done, self.pacer) {
                    return Ok(done);
                }
                let n = self.pacer.size.min(todo.len());
                let batch: Vec<J> = todo[..n].to_vec();
                done.requests += 1;
                let sent = batch
                    .iter()
                    .map(|i| {
                        let k = i.as_py_int().unwrap_or(0);
                        let row = if k < 0 {
                            usize::try_from(i64::try_from(rows.len()).unwrap_or(0) + k)
                                .ok()
                                .and_then(|u| rows.get(u))
                        } else {
                            usize::try_from(k).ok().and_then(|u| rows.get(u))
                        };
                        match row {
                            Some(J::Obj(m)) => Ok(J::Obj(m.clone().with("backfill", true))),
                            Some(other) => Err(format!(
                                "unsupported operand type(s) for |: '{}' and 'dict'",
                                other.py_type()
                            )),
                            None => Err("list index out of range".to_string()),
                        }
                    })
                    .collect::<Result<Vec<J>, String>>()
                    .and_then(|rows| (self.send_rows)(rows));
                if let Err(e) = sent {
                    done.failed += 1;
                    if self.pacer.failed(&e, batch.len()) {
                        self.segments
                            .mark(&name, &batch, None)
                            .map_err(|e| os(&e))?;
                        todo.drain(..n);
                        done.given_up += batch.len();
                        let first = batch[0].as_py_int().and_then(|k| usize::try_from(k).ok());
                        let id = first
                            .and_then(|k| rows.get(k))
                            .and_then(J::as_obj)
                            .and_then(|m| m.get("id"))
                            .map_or_else(|| "None".to_string(), J::py_str);
                        (self.log)(&format!(
                            "backfill: gave up row {id} after repeated server errors"
                        ));
                    }
                    continue;
                }
                self.pacer.succeeded();
                self.segments
                    .mark(&name, &batch, None)
                    .map_err(|e| os(&e))?;
                todo.drain(..n);
                done.rows += batch.len();
                (self.sleep)(self.pause_s);
            }
            if !r2 {
                if stop(&mut done, self.pacer) {
                    return Ok(done);
                }
                done.requests += 1;
                let sent = match std::fs::read(&path) {
                    Ok(data) => (self.send_segment)(&name, data),
                    Err(e) => Err(PyExc::from_io(&e, Some(&path)).text),
                };
                if let Err(e) = sent {
                    done.failed += 1;
                    self.pacer.failed(&e, 0);
                    continue;
                }
                self.segments
                    .mark(&name, &[], Some(true))
                    .map_err(|e| os(&e))?;
                (self.sleep)(self.pause_s);
            }
            done.segments += 1;
        }
        Ok(done)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_and_pacer_like_python() {
        assert!(in_window("22:00-06:00", 23, 0));
        assert!(in_window("22:00-06:00", 5, 59));
        assert!(!in_window("22:00-06:00", 6, 0));
        assert!(in_window("01:00-02:00", 1, 30));
        assert!(!in_window("", 1, 30));
        assert!(in_window("\u{661}\u{660}:00-11:00", 10, 5));
        let mut p = Pacer::new(|| 1000.0);
        assert!(!p.failed("POST /ingest/experience: HTTP 503", 3));
        assert_eq!(p.size, 1);
        for k in 0..4 {
            assert!(!p.failed("POST /x: HTTP 500", 1), "{k}");
        }
        assert!(p.failed("POST /x: HTTP 500", 1));
        assert!((p.retry_at - 1000.0 - 960.0).abs() < 1e-9);
        p.failed("POST /x: URLError", 1);
        p.succeeded();
        assert_eq!((p.failures, p.successes), (0, 1));
        let mut q = Pacer::new(|| 0.0);
        for _ in 0..12 {
            q.failed("x", 1);
        }
        assert!((q.retry_at - 3600.0).abs() < 1e-9);
    }
}
