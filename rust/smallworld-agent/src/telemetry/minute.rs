//! Minute rows (telemetry.py 238-391): `_median`, `_r`, `summarize_server`, `summarize_players`, `lag_events`,
//! `Sessions`. Float results follow Python's operations one for one: `round()` is data's `py_round`, `min`/`max`
//! keep the first of equal values, `sum()` is CPython >= 3.12's (compensated for floats, `py_sum`).

use super::json::{J, OMap};
use super::snapshot::{Player, Snapshot, World, merge_hot};
use crate::data::metrics::{py_round, py_round_int};

pub const MAX_LAG_EVENTS: usize = 20;
pub const LAG_CONTEXT_PLAYERS: usize = 3;
pub const SESSION_GAP_MS: i64 = 60_000;

/// A number as Python holds it (an int or a float), for `sum()` over mixed items.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Num {
    I(i64),
    F(f64),
}

impl Num {
    /// `a * b`.
    #[allow(clippy::cast_precision_loss)]
    pub fn mul(self, other: Num) -> Num {
        match (self, other) {
            (Num::I(a), Num::I(b)) => Num::I(a.saturating_mul(b)),
            (Num::I(a), Num::F(b)) => Num::F(a as f64 * b),
            (Num::F(a), Num::I(b)) => Num::F(a * b as f64),
            (Num::F(a), Num::F(b)) => Num::F(a * b),
        }
    }

    #[allow(clippy::cast_precision_loss)]
    pub fn as_f64(self) -> f64 {
        match self {
            Num::I(n) => n as f64,
            Num::F(x) => x,
        }
    }
}

/// CPython (>= 3.12) `sum(items)` with the int start 0: ints add exactly until the first float, which is added
/// with `int + float`; from then on floats add with Neumaier's compensation and ints as plain `f += (double)v`;
/// the compensation is added at the end when it is non-zero and finite.
#[allow(clippy::cast_precision_loss)]
pub fn py_sum(items: impl IntoIterator<Item = Num>) -> Num {
    let mut it = items.into_iter();
    let mut i_result: i64 = 0;
    let mut f = loop {
        match it.next() {
            None => return Num::I(i_result),
            Some(Num::I(n)) => i_result = i_result.saturating_add(n),
            Some(Num::F(x)) => break i_result as f64 + x,
        }
    };
    let mut c = 0.0_f64;
    for item in it {
        match item {
            Num::F(x) => {
                let t = f + x;
                if f.abs() >= x.abs() {
                    c += (f - t) + x;
                } else {
                    c += (x - t) + f;
                }
                f = t;
            }
            Num::I(n) => f += n as f64,
        }
    }
    if c != 0.0 && c.is_finite() {
        f += c;
    }
    Num::F(f)
}

/// `sum(floats)` (a float list; an empty one is the int 0, returned as 0.0 here: callers `int()` it).
pub fn py_fsum(items: &[f64]) -> f64 {
    py_sum(items.iter().map(|x| Num::F(*x))).as_f64()
}

/// Python's `max(values, default=None)` (the first of equal values).
pub fn py_max(values: &[f64]) -> Option<f64> {
    values
        .iter()
        .copied()
        .reduce(|best, x| if x > best { x } else { best })
}

/// Python's `min(values, default=None)`.
pub fn py_min(values: &[f64]) -> Option<f64> {
    values
        .iter()
        .copied()
        .reduce(|best, x| if x < best { x } else { best })
}

/// telemetry.py `_median(values)`.
pub fn median(values: &[f64]) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    let mut s = values.to_vec();
    // sorted() compares with `<`; NaN never occurs (`_num` refuses it)
    s.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let mid = s.len() / 2;
    // `(a + b) / 2` as written (f64::midpoint avoids an overflow Python has)
    #[allow(clippy::manual_midpoint)]
    Some(if s.len() % 2 == 1 {
        s[mid]
    } else {
        (s[mid - 1] + s[mid]) / 2.0
    })
}

/// telemetry.py `_r(v, nd)`.
pub fn r(v: Option<f64>, nd: usize) -> J {
    v.map(|x| py_round(x, nd)).into()
}

/// telemetry.py `summarize_server(snaps)`.
pub fn summarize_server(snaps: &[Snapshot]) -> OMap {
    #[allow(clippy::cast_possible_truncation)]
    let ticks = snaps.iter().fold(0_i64, |acc, s| {
        acc.saturating_add(s.srv.get("ticks").map_or(0, |t| t as i64))
    });
    let vals = |k: &str| -> Vec<f64> { snaps.iter().filter_map(|s| s.srv.get(k)).collect() };
    let or0 = |v: Option<f64>| v.filter(|x| *x != 0.0).map_or(Num::I(0), Num::F);
    let weighted = py_sum(
        snaps
            .iter()
            .map(|s| or0(s.srv.get("mean")).mul(or0(s.srv.get("ticks")))),
    );
    #[allow(clippy::cast_precision_loss)]
    let mean = (ticks != 0).then(|| weighted.as_f64() / ticks as f64);
    let empty: Vec<World> = Vec::new();
    let last_worlds = snaps.last().map_or(&empty, |s| &s.srv.worlds);
    let sum_w = |f: fn(&World) -> i64| {
        last_worlds
            .iter()
            .fold(0_i64, |a, w| a.saturating_add(f(w)))
    };
    let spikes: usize = snaps.iter().map(|s| s.srv.spikes.len()).sum();
    #[allow(clippy::cast_possible_truncation)]
    let int_sum = |k: &str| py_fsum(&vals(k)) as i64;
    OMap::new()
        .with("ticks", ticks)
        .with("tps", r(py_min(&vals("tps")), 2))
        .with("mean", r(mean, 2))
        .with("p50", r(median(&vals("p50")), 2))
        .with("p95", r(py_max(&vals("p95")), 2))
        .with("p99", r(py_max(&vals("p99")), 2))
        .with("p999", r(py_max(&vals("p999")), 2))
        .with("max", r(py_max(&vals("max")), 2))
        .with("spikes", spikes)
        .with("chunks", sum_w(|w| w.chunks))
        .with("entities", sum_w(|w| w.entities))
        .with("tiles", sum_w(|w| w.ticking))
        .with(
            "worlds",
            J::List(last_worlds.iter().map(World::to_j).collect()),
        )
        .with(
            "hot",
            J::List(
                merge_hot(snaps)
                    .iter()
                    .map(super::snapshot::Hot::to_j)
                    .collect(),
            ),
        )
        .with("heap_used_mb", vals("heap_used_mb").last().copied())
        .with("heap_max_mb", vals("heap_max_mb").last().copied())
        .with("self_p99_us", r(py_max(&vals("self_p99_us")), 1))
        .with("self_max_us", r(py_max(&vals("self_max_us")), 1))
        .with("budget_skips", int_sum("budget_skips"))
        .with("lost_windows", int_sum("lost_windows"))
        .with("samples", snaps.len())
}

/// `round(sum(ints) / len(ints))`.
#[allow(clippy::cast_precision_loss)]
fn round_mean(v: &[i64]) -> i64 {
    let total = v.iter().fold(0_i64, |a, x| a.saturating_add(*x));
    py_round_int(total as f64 / v.len() as f64)
}

/// telemetry.py `summarize_players(snaps)`.
pub fn summarize_players(snaps: &[Snapshot]) -> Vec<J> {
    struct Acc {
        last: Player,
        pings: Vec<i64>,
        fpss: Vec<i64>,
        glide: bool,
        ent: i64,
    }
    let mut by: Vec<Acc> = Vec::new();
    for s in snaps {
        for p in &s.pl {
            let i = if let Some(i) = by.iter().position(|a| a.last.u == p.u) {
                i
            } else {
                by.push(Acc {
                    last: p.clone(),
                    pings: Vec::new(),
                    fpss: Vec::new(),
                    glide: false,
                    ent: 0,
                });
                by.len() - 1
            };
            let acc = &mut by[i];
            acc.pings.push(p.ping);
            if let Some(f) = p.fps {
                acc.fpss.push(f);
            }
            acc.glide = acc.glide || p.glide;
            acc.ent = acc.ent.max(p.ent);
            acc.last = p.clone();
        }
    }
    let mut out: Vec<(String, J)> = by
        .into_iter()
        .map(|a| {
            let p = &a.last;
            let mut m = OMap::new()
                .with("u", p.u.as_str())
                .with("glide", a.glide)
                .with("ent", a.ent)
                .with("n", p.n.as_str())
                .with("brand", p.brand.as_str())
                .with("view", p.view)
                .with("send", p.send)
                .with("loc", p.loc.as_str())
                .with("w", p.w.as_str())
                .with("cx", p.cx)
                .with("cz", p.cz)
                .with("tips", p.tips)
                .with("join", p.join)
                .with("ping", round_mean(&a.pings))
                .with("ping_max", a.pings.iter().copied().max().unwrap_or(0))
                .with(
                    "pings",
                    J::List(a.pings.iter().map(|x| J::Int(*x)).collect()),
                )
                .with("samples", a.pings.len());
            if !a.fpss.is_empty() {
                m.insert("fps", round_mean(&a.fpss));
                m.insert("fps_min", a.fpss.iter().copied().min().unwrap_or(0));
            }
            (p.n.to_ascii_lowercase(), J::Obj(m))
        })
        .collect();
    out.sort_by(|a, b| a.0.cmp(&b.0));
    out.into_iter().map(|(_, j)| j).collect()
}

/// telemetry.py `lag_events(snaps, running)`.
pub fn lag_events(snaps: &[Snapshot], running: &[String]) -> Vec<J> {
    let mut out: Vec<(i64, f64, J)> = Vec::new();
    for s in snaps {
        if s.srv.spikes.is_empty() {
            continue;
        }
        let mut near: Vec<&Player> = s.pl.iter().collect();
        near.sort_by_key(|p| std::cmp::Reverse(p.ent));
        near.truncate(LAG_CONTEXT_PLAYERS);
        let mut worlds: Vec<&World> = s.srv.worlds.iter().collect();
        worlds.sort_by_key(|w| std::cmp::Reverse(w.entities));
        worlds.truncate(3);
        let ctx: J = OMap::new()
            .with(
                "worlds",
                J::List(
                    worlds
                        .iter()
                        .map(|w| {
                            OMap::new()
                                .with("n", w.n.as_str())
                                .with("entities", w.entities)
                                .with("chunks", w.chunks)
                                .into()
                        })
                        .collect(),
                ),
            )
            .with(
                "near",
                J::List(
                    near.iter()
                        .map(|p| {
                            OMap::new()
                                .with("n", p.n.as_str())
                                .with("u", p.u.as_str())
                                .with("ent", p.ent)
                                .with("w", p.w.as_str())
                                .with("cx", p.cx)
                                .with("cz", p.cz)
                                .with("glide", p.glide)
                                .into()
                        })
                        .collect(),
                ),
            )
            .with(
                "running",
                J::List(running.iter().map(|x| J::Str(x.clone())).collect()),
            )
            .with("players", s.pl.len())
            .into();
        for (ts, ms) in &s.srv.spikes {
            let ms = py_round(*ms, 1);
            out.push((*ts, ms, ctx.clone()));
        }
    }
    out.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    out.truncate(MAX_LAG_EVENTS);
    out.sort_by_key(|e| e.0);
    out.into_iter()
        .map(|(t, ms, ctx)| {
            OMap::new()
                .with("t", t)
                .with("ms", ms)
                .with("ctx", ctx)
                .into()
        })
        .collect()
}

#[derive(Clone, Debug)]
#[allow(clippy::struct_field_names)]
struct Seen {
    n: String,
    join: i64,
    seen: i64,
    last: i64,
}

/// telemetry.py `Sessions`: join/leave events from the online set of consecutive snapshots (the plugin's join
/// time is the session's key; a gap > `SESSION_GAP_MS` closes sessions at their last-seen time).
#[derive(Debug, Default)]
pub struct Sessions {
    online: Vec<(String, Seen)>,
    pub last_ts: Option<i64>,
}

fn event(k: &str, t: i64, u: &str, n: &str, join: i64) -> J {
    OMap::new()
        .with("k", k)
        .with("t", t)
        .with("u", u)
        .with("n", n)
        .with("join", join)
        .into()
}

impl Sessions {
    /// `feed(snaps)`: the events, in Python's order.
    pub fn feed(&mut self, snaps: &[Snapshot]) -> Vec<J> {
        let mut events = Vec::new();
        for s in snaps {
            let gap = self.last_ts.is_some_and(|l| s.ts - l > SESSION_GAP_MS);
            // `{p["u"]: p for p in s["pl"]}`: the first position, the last value
            let mut now: Vec<(&str, &Player)> = Vec::new();
            for p in &s.pl {
                if let Some(slot) = now.iter_mut().find(|(u, _)| *u == p.u) {
                    slot.1 = p;
                } else {
                    now.push((p.u.as_str(), p));
                }
            }
            let find = |u: &str| now.iter().find(|(k, _)| *k == u).map(|(_, p)| *p);
            let leave = |u: &str, p: &Seen, gap: bool| {
                let join = if p.join != 0 { p.join } else { p.seen };
                event("leave", if gap { p.last } else { s.ts }, u, &p.n, join)
            };
            for (u, p) in &self.online {
                match find(u) {
                    None => events.push(leave(u, p, gap)),
                    Some(cur) if p.join != 0 && cur.join != 0 && cur.join != p.join => {
                        events.push(leave(u, p, true));
                    }
                    Some(_) => {}
                }
            }
            for (u, p) in &now {
                let prev = self.online.iter().find(|(k, _)| k == u).map(|(_, v)| v);
                if prev.is_none_or(|prev| prev.join != 0 && p.join != 0 && p.join != prev.join) {
                    let at = if p.join != 0 { p.join } else { s.ts };
                    events.push(event("join", at, u, &p.n, at));
                }
            }
            let online = now
                .iter()
                .map(|(u, p)| {
                    let prev = self.online.iter().find(|(k, _)| k == u).map(|(_, v)| v);
                    let seen = match prev {
                        Some(prev) if prev.join == p.join => prev.seen,
                        _ => s.ts,
                    };
                    (
                        (*u).to_string(),
                        Seen {
                            n: p.n.clone(),
                            join: p.join,
                            seen,
                            last: s.ts,
                        },
                    )
                })
                .collect();
            self.online = online;
            self.last_ts = Some(s.ts);
        }
        events
    }

    /// `names()`.
    pub fn names(&self) -> Vec<String> {
        let mut v: Vec<String> = self.online.iter().map(|(_, p)| p.n.clone()).collect();
        v.sort();
        v
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sum_like_cpython_312() {
        // python3.13 -c 'print(sum([1e16, 1, 1]), sum([1e16, 1.0, 1.0]), sum([-0.0, -0.0]), sum([0.1]*10))'
        let f = |v: &[Num]| py_sum(v.iter().copied());
        assert_eq!(f(&[Num::F(1e16), Num::I(1), Num::I(1)]), Num::F(1e16));
        assert_eq!(
            f(&[Num::F(1e16), Num::F(1.0), Num::F(1.0)]),
            Num::F(1.000_000_000_000_000_2e16)
        );
        let z = f(&[Num::F(-0.0), Num::F(-0.0)]).as_f64();
        assert!(z == 0.0 && z.is_sign_positive());
        assert_eq!(f(&[Num::F(0.1); 10]), Num::F(1.0));
        assert_eq!(f(&[Num::I(2), Num::I(3)]), Num::I(5));
        assert_eq!(
            f(&[Num::F(1e308), Num::F(1e308), Num::F(-1e308)]),
            Num::F(f64::INFINITY)
        );
    }

    #[test]
    fn median_and_extremes() {
        assert_eq!(median(&[3.0, 1.0, 2.0, 10.0]), Some(2.5));
        assert_eq!(median(&[]), None);
        let m = py_max(&[0.0, -0.0]).unwrap();
        assert!(m.is_sign_positive());
        let m = py_max(&[-0.0, 0.0]).unwrap();
        assert!(m.is_sign_negative());
    }
}
