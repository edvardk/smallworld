//! The telemetry parity harness's vectors (tests/telemetry_parity/vectors/*.json, written from scripts/telemetry.py
//! by scripts/telemetry_vectors.py): every group replayed against the port. The files are read with `json::loads`
//! (Python's key order, which several outputs depend on); outputs are compared as their compact JSON text, so the
//! key order and the int/float spelling count too. Floats JSON can't hold are `{"float": "Infinity"}` there
//! (`plain`).

use super::backfill::{Backfill, Done, Pacer, in_window, quiet};
use super::json::{J, OMap, loads};
use super::messages::Messages;
use super::minute::{Sessions, lag_events, median, r, summarize_players, summarize_server};
use super::overhead::{Overhead, Stats, consumed_cpu_ms, parse_show};
use super::segments::{Segments, read_segment, segment_bytes};
use super::snapshot::{
    Hot, Player, SNAP_NAME, SRV_KEYS, Snapshot, Srv, World, clean_text, fps, merge_hot, parse_hot,
    parse_snapshot,
};
use super::stats::read_stats;
use super::{Config, join_items, load_identity, save_identity, verdicts_tsv};
use crate::settings::Env;
use crate::types::{Obj, Value};
use std::cell::Cell;
use std::io::Read;
use std::path::{Path, PathBuf};

fn vectors(text: &str) -> OMap {
    let file = loads(text).expect("a vector file is JSON");
    file.as_obj()
        .and_then(|m| m.get("vectors"))
        .and_then(J::as_obj)
        .cloned()
        .expect("vectors")
}

macro_rules! vector_file {
    ($name:literal) => {
        vectors(include_str!(concat!(
            "../../../../tests/telemetry_parity/vectors/",
            $name,
            ".json"
        )))
    };
}

fn group<'a>(v: &'a OMap, k: &str) -> &'a J {
    v.get(k).unwrap_or_else(|| panic!("vector group {k}"))
}

fn list<'a>(v: &'a OMap, k: &str) -> &'a [J] {
    group(v, k)
        .as_list()
        .unwrap_or_else(|| panic!("{k} is a list"))
}

fn obj(v: &J) -> &OMap {
    v.as_obj().expect("an object")
}

fn get<'a>(v: &'a J, k: &str) -> &'a J {
    obj(v).get(k).unwrap_or(&J::Null)
}

/// parity_vectors.plain: non-finite floats as `{"float": ...}`.
fn plain(v: &J) -> J {
    match v {
        J::Float(x) if x.is_nan() => OMap::new().with("float", "NaN").into(),
        J::Float(x) if x.is_infinite() => OMap::new()
            .with("float", if *x > 0.0 { "Infinity" } else { "-Infinity" })
            .into(),
        J::List(items) => J::List(items.iter().map(plain).collect()),
        J::Obj(m) => J::Obj(OMap(m.iter().map(|(k, x)| (k.clone(), plain(x))).collect())),
        other => other.clone(),
    }
}

/// The inverse of `plain` for one number.
fn num(v: &J) -> Option<f64> {
    #[allow(clippy::cast_precision_loss)]
    match v {
        J::Int(n) => Some(*n as f64),
        J::Float(x) => Some(*x),
        J::Obj(m) => match m.get("float").and_then(J::as_str) {
            Some("NaN") => Some(f64::NAN),
            Some("Infinity") => Some(f64::INFINITY),
            Some("-Infinity") => Some(f64::NEG_INFINITY),
            _ => None,
        },
        _ => None,
    }
}

fn int(v: &J) -> i64 {
    #[allow(clippy::cast_possible_truncation)]
    v.as_py_int()
        .unwrap_or_else(|| num(v).map_or(0, |x| x as i64))
}

fn text(v: &J) -> String {
    v.as_str().unwrap_or_default().to_string()
}

#[track_caller]
fn same(got: &J, want: &J, what: &str) {
    assert_eq!(plain(got).dumps_compact(), want.dumps_compact(), "{what}");
}

// --- builders from the vectors' parsed snapshots ------------------------------------------------------------

fn world_of(w: &J) -> World {
    World {
        n: text(get(w, "n")),
        chunks: int(get(w, "chunks")),
        entities: int(get(w, "entities")),
        tiles: int(get(w, "tiles")),
        ticking: int(get(w, "ticking")),
        players: int(get(w, "players")),
    }
}

fn hot_of(c: &J) -> Hot {
    Hot {
        w: text(get(c, "w")),
        cx: get(c, "cx").clone(),
        cz: get(c, "cz").clone(),
        counts: [
            int(get(c, "ent")),
            int(get(c, "tiles")),
            int(get(c, "vil")),
            int(get(c, "items")),
        ],
    }
}

fn srv_of(s: &J) -> Srv {
    let mut srv = Srv::default();
    for (i, k) in SRV_KEYS.iter().enumerate() {
        srv.nums[i] = num(get(s, k));
    }
    let l = |k: &str| get(s, k).as_list().cloned().unwrap_or_default();
    srv.spikes = l("spikes")
        .iter()
        .map(|p| {
            let p = p.as_list().expect("a spike");
            (int(&p[0]), num(&p[1]).unwrap_or(0.0))
        })
        .collect();
    srv.worlds = l("worlds").iter().map(world_of).collect();
    srv.scanned = int(get(s, "scanned"));
    srv.hot = l("hot").iter().map(hot_of).collect();
    srv
}

fn player_of(p: &J) -> Player {
    Player {
        u: text(get(p, "u")),
        n: text(get(p, "n")),
        ping: int(get(p, "ping")),
        brand: text(get(p, "brand")),
        view: int(get(p, "view")),
        send: int(get(p, "send")),
        loc: text(get(p, "loc")),
        w: text(get(p, "w")),
        cx: int(get(p, "cx")),
        cz: int(get(p, "cz")),
        ent: int(get(p, "ent")),
        glide: get(p, "glide") == &J::Bool(true),
        tips: get(p, "tips") != &J::Bool(false),
        join: int(get(p, "join")),
        fps: get(p, "fps").as_py_int(),
    }
}

fn snapshot(ts: i64, srv: Srv, pl: Vec<Player>) -> Snapshot {
    Snapshot {
        seq: 0,
        ts,
        win_ms: 0,
        srv,
        pl,
    }
}

fn players(v: &J) -> Vec<Player> {
    v.as_list()
        .map_or_else(Vec::new, |l| l.iter().map(player_of).collect())
}

/// A temporary directory removed when dropped.
struct Tmp(PathBuf);

impl Tmp {
    fn new(tag: &str) -> Self {
        use std::sync::atomic::{AtomicUsize, Ordering};
        static N: AtomicUsize = AtomicUsize::new(0);
        let d = std::env::temp_dir().join(format!(
            "sw-tel-vec-{tag}-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        Self(d)
    }
}

impl Drop for Tmp {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

thread_local! {
    static CLOCK: Cell<f64> = const { Cell::new(0.0) };
}

fn clock() -> f64 {
    CLOCK.with(Cell::get)
}

fn set_clock(t: f64) {
    CLOCK.with(|c| c.set(t));
}

// --- text ---------------------------------------------------------------------------------------------------

#[test]
fn text_vectors() {
    let v = vector_file!("text");
    for c in list(&v, "clean_text") {
        let limit = usize::try_from(int(get(c, "limit"))).unwrap();
        let got = clean_text(Some(get(c, "in")), limit);
        assert_eq!(
            got,
            text(get(c, "out")),
            "clean_text({:?}, {limit})",
            get(c, "in")
        );
    }
    for c in list(&v, "join") {
        let input = get(c, "in");
        assert_eq!(
            join_items(Some(input)),
            text(get(c, "out")),
            "_join({input:?})"
        );
    }
    let in_ranges = |ranges: &[J], cp: u32| {
        ranges.iter().any(|r| {
            let r = r.as_list().unwrap();
            i64::from(cp) >= int(&r[0]) && i64::from(cp) <= int(&r[1])
        })
    };
    let space = list(&v, "isspace_ranges");
    let not_printable = list(&v, "not_printable_ranges");
    let mut table_diffs = Vec::new();
    for c in (0..=0x10_ffff_u32).filter_map(char::from_u32) {
        let cp = u32::from(c);
        assert_eq!(
            smallworld_core::pystr::isspace(c),
            in_ranges(space, cp),
            "isspace(U+{cp:04X})"
        );
        let ours = crate::unicode::is_bad_category(c) && !smallworld_core::pystr::isspace(c);
        if ours != in_ranges(not_printable, cp) {
            table_diffs.push(cp);
        }
    }
    // The vectors come from the generating Python's Unicode tables (15.1), the port uses the pinned crate's
    // (PORTING.md "Divergences": parity over code points assigned in both). A difference may only be a code point
    // one of the two tables doesn't assign: the crate's newer assignment (Python: unprintable), or a crate Cn.
    for cp in &table_diffs {
        let c = char::from_u32(*cp).unwrap();
        let ours = crate::unicode::category(c);
        assert!(
            in_ranges(not_printable, *cp) || ours == "Cn",
            "isprintable(U+{cp:04X}) differs on a code point both tables assign ({ours})"
        );
    }
}

// --- snapshots ----------------------------------------------------------------------------------------------

#[test]
fn snapshot_vectors() {
    let v = vector_file!("snapshot");
    for c in list(&v, "parse_snapshot")
        .iter()
        .chain(list(&v, "bad_snapshot"))
    {
        let label = text(get(c, "label"));
        let mut input = text(get(c, "json"));
        if label == "players" {
            // `generated`: pl += [pl(UC, f'P{i}', 1) for i in range(600)]; the output keeps the first 10
            let mut raw = obj(&loads(&input).unwrap()).clone();
            let Some(J::List(pl)) = raw.get_mut("pl") else {
                panic!("players")
            };
            for i in 0..600 {
                pl.push(player_j(&format!("P{i}")));
            }
            input = J::Obj(raw).dumps();
        }
        let out = get(c, "out");
        let parsed = match parse_snapshot(input.as_bytes()) {
            Err(e) => {
                // BadSnapshot (v1.5): the same field and text
                assert_eq!(
                    (e.class.as_str(), e.text.as_str()),
                    (
                        text(get(out, "type")).as_str(),
                        text(get(out, "error")).as_str()
                    ),
                    "{label}"
                );
                continue;
            }
            Ok(p) => p,
        };
        assert_eq!(get(out, "error"), &J::Null, "{label}: Python raises");
        let mut got = parsed.map_or(J::Null, |s| s.to_j());
        if label == "players"
            && let J::Obj(m) = &mut got
            && let Some(J::List(pl)) = m.get_mut("pl")
        {
            pl.truncate(10);
        }
        same(&got, get(out, "ok"), &label);
    }
    for c in list(&v, "fps") {
        let input = get(c, "in");
        let parsed = match num(input) {
            Some(x) if input.as_obj().is_some() => J::Float(x),
            _ => input.clone(),
        };
        let out = get(c, "out");
        match fps(Some(&parsed)) {
            Ok(n) => same(&J::from(n), get(out, "ok"), "_fps"),
            Err(e) => assert_eq!(e.text, text(get(out, "error")), "_fps {input:?}"),
        }
    }
    for c in list(&v, "parse_hot") {
        let got = J::List(
            parse_hot(Some(get(c, "in")))
                .unwrap()
                .iter()
                .map(Hot::to_j)
                .collect(),
        );
        same(&got, get(c, "out"), "parse_hot");
    }
    for c in list(&v, "merge_hot") {
        let snaps: Vec<Snapshot> = get(c, "in")
            .as_list()
            .unwrap()
            .iter()
            .map(|hot| {
                let srv = Srv {
                    hot: parse_hot(Some(hot)).unwrap(),
                    ..Srv::default()
                };
                snapshot(0, srv, Vec::new())
            })
            .collect();
        let got = J::List(merge_hot(&snaps).iter().map(Hot::to_j).collect());
        same(&got, get(c, "out"), "merge_hot");
    }
    for c in list(&v, "snap_names") {
        let name = text(get(c, "name"));
        assert_eq!(
            SNAP_NAME.is_match(&name),
            get(c, "match") == &J::Bool(true),
            "SNAP_NAME {name:?}"
        );
    }
    let k = obj(group(&v, "constants"));
    assert_eq!(
        int(k.get("MAX_SNAPSHOT_BYTES").unwrap()),
        i64::try_from(super::snapshot::MAX_SNAPSHOT_BYTES).unwrap()
    );
    assert_eq!(
        int(k.get("MAX_HOT").unwrap()),
        i64::try_from(super::snapshot::MAX_HOT).unwrap()
    );
    assert_eq!(
        int(k.get("TEXT_MAX").unwrap()),
        i64::try_from(super::snapshot::TEXT_MAX).unwrap()
    );
    assert_eq!(int(k.get("MAX_TS_MS").unwrap()), super::snapshot::MAX_TS_MS);
}

/// scripts/telemetry_vectors.py `pl(UC, name, 1)`.
fn player_j(name: &str) -> J {
    OMap::new()
        .with("u", "cccccccc-3333-4333-8333-333333333333")
        .with("n", name)
        .with("ping", 40_i64)
        .with("brand", "vanilla")
        .with("view", 10_i64)
        .with("send", 10_i64)
        .with("loc", "en_us")
        .with("w", "world")
        .with("cx", 0_i64)
        .with("cz", 0_i64)
        .with("ent", 10_i64)
        .with("glide", false)
        .with("tips", true)
        .with("join", 1_i64)
        .into()
}

// --- minute rows --------------------------------------------------------------------------------------------

#[test]
fn minute_vectors() {
    let v = vector_file!("minute");
    for c in list(&v, "median") {
        let input: Vec<f64> = get(c, "in")
            .as_list()
            .unwrap()
            .iter()
            .filter_map(num)
            .collect();
        same(&J::from(median(&input)), get(c, "out"), "_median");
    }
    for c in list(&v, "r") {
        let nd = usize::try_from(int(get(c, "nd"))).unwrap();
        same(&r(num(get(c, "x")), nd), get(c, "out"), "_r");
    }
    for c in list(&v, "summarize_server") {
        let snaps: Vec<Snapshot> = get(c, "snapshots")
            .as_list()
            .unwrap()
            .iter()
            .map(|s| snapshot(0, srv_of(s), Vec::new()))
            .collect();
        same(
            &J::Obj(summarize_server(&snaps).unwrap()),
            get(c, "out"),
            "summarize_server",
        );
    }
    for c in list(&v, "summarize_players") {
        let snaps: Vec<Snapshot> = get(c, "snapshots")
            .as_list()
            .unwrap()
            .iter()
            .map(|pl| snapshot(0, Srv::default(), players(pl)))
            .collect();
        same(
            &J::List(summarize_players(&snaps)),
            get(c, "out"),
            "summarize_players",
        );
    }
    for c in list(&v, "lag_events") {
        let snaps: Vec<Snapshot> = get(c, "snapshots")
            .as_list()
            .unwrap()
            .iter()
            .map(|s| snapshot(0, srv_of(get(s, "srv")), players(get(s, "pl"))))
            .collect();
        let running: Vec<String> = get(c, "running")
            .as_list()
            .unwrap()
            .iter()
            .map(text)
            .collect();
        same(
            &J::List(lag_events(&snaps, &running)),
            get(c, "out"),
            "lag_events",
        );
    }
    let sess = group(&v, "sessions");
    assert_eq!(
        int(get(sess, "SESSION_GAP_MS")),
        super::minute::SESSION_GAP_MS
    );
    let mut sessions = Sessions::default();
    for (i, step) in get(sess, "steps").as_list().unwrap().iter().enumerate() {
        let feed: Vec<Snapshot> = get(step, "feed")
            .as_list()
            .unwrap()
            .iter()
            .map(|s| {
                let pl = get(s, "pl")
                    .as_list()
                    .unwrap()
                    .iter()
                    .map(|p| {
                        let p = p.as_list().unwrap();
                        player_of(
                            &OMap::new()
                                .with("u", p[0].clone())
                                .with("n", p[1].clone())
                                .with("join", p[2].clone())
                                .into(),
                        )
                    })
                    .collect();
                snapshot(int(get(s, "ts")), Srv::default(), pl)
            })
            .collect();
        same(
            &J::List(sessions.feed(&feed)),
            get(step, "events"),
            &format!("Sessions step {i}"),
        );
        let names = J::List(sessions.names().into_iter().map(J::Str).collect());
        same(
            &names,
            get(step, "names"),
            &format!("Sessions.names step {i}"),
        );
    }
}

// --- overhead -----------------------------------------------------------------------------------------------

fn stats_j(stats: &Stats) -> J {
    #[allow(clippy::cast_precision_loss)]
    J::Obj(OMap(
        stats
            .iter()
            .map(|(u, props)| {
                let m = OMap(
                    props
                        .iter()
                        .map(|(k, v)| {
                            let j = match v {
                                None => J::Null,
                                Some(n) => i64::try_from(*n).map_or(J::Float(*n as f64), J::Int),
                            };
                            (k.clone(), j)
                        })
                        .collect(),
                );
                (u.clone(), J::Obj(m))
            })
            .collect(),
    ))
}

fn stats_of(v: &J) -> Stats {
    #[allow(clippy::cast_possible_truncation)]
    obj(v)
        .iter()
        .map(|(u, props)| {
            let p = obj(props)
                .iter()
                .map(|(k, x)| {
                    let n = match x {
                        J::Int(n) => Some(i128::from(*n)),
                        J::Float(f) => Some(*f as i128),
                        _ => None,
                    };
                    (k.clone(), n)
                })
                .collect();
            (u.clone(), p)
        })
        .collect()
}

fn entries(v: &J) -> Vec<Obj> {
    v.as_list()
        .unwrap()
        .iter()
        .map(|e| match e.to_value() {
            Value::Object(o) => o,
            _ => Obj::new(),
        })
        .collect()
}

#[test]
fn overhead_vectors() {
    let v = vector_file!("overhead");
    for c in list(&v, "parse_show") {
        same(
            &stats_j(&parse_show(&text(get(c, "in")))),
            get(get(c, "out"), "ok"),
            "parse_show",
        );
    }
    for c in list(&v, "consumed_cpu_ms") {
        let got = consumed_cpu_ms(&text(get(c, "in")))
            .map_or(J::Null, |(ms, mb)| J::List(vec![J::Float(ms), J::from(mb)]));
        same(&got, get(c, "out"), &text(get(c, "in")));
    }
    let o = Overhead::default();
    for (i, step) in get(group(&v, "overhead"), "steps")
        .as_list()
        .unwrap()
        .iter()
        .enumerate()
    {
        o.observe_journal(&entries(get(step, "journal_before")));
        let got = o.sample(stats_of(get(step, "stats")), num(get(step, "now")).unwrap());
        same(
            &J::Obj(got),
            get(step, "out"),
            &format!("Overhead step {i}"),
        );
    }
    let argv: Vec<String> = list(&v, "unit_stats_argv").iter().map(text).collect();
    assert_eq!(
        argv.join(" "),
        "systemctl show -p Id -p CPUUsageNSec -p MemoryCurrent -p MemoryPeak -p IOReadBytes -p IOWriteBytes"
    );
}

// --- segments -----------------------------------------------------------------------------------------------

#[test]
fn segment_vectors() {
    let v = vector_file!("segments");
    for c in list(&v, "segment_bytes") {
        let rows = get(c, "rows").as_list().unwrap();
        let gz = segment_bytes(rows).unwrap();
        let mut text_out = String::new();
        flate2::read::GzDecoder::new(&gz[..])
            .read_to_string(&mut text_out)
            .unwrap();
        assert_eq!(text_out, text(get(c, "ndjson")));
    }
    for (k, c) in list(&v, "write").iter().enumerate() {
        let tmp = Tmp::new("write");
        let seg = Segments::new(tmp.0.join(format!("n{k}")), 30, 200, || 2e9);
        let acked: Vec<String> = get(c, "acked")
            .as_list()
            .unwrap()
            .iter()
            .map(text)
            .collect();
        let name = seg
            .write(
                get(c, "rows").as_list().unwrap(),
                &acked,
                &text(get(c, "src")),
            )
            .unwrap();
        same(&J::from(name), get(c, "name"), "Segments.write name");
        same(&J::Obj(seg.load()), get(c, "state"), "Segments.write state");
    }
    for c in list(&v, "prune") {
        let tmp = Tmp::new("prune");
        let now = num(get(c, "now")).unwrap();
        let mut state = OMap::new();
        for f in get(c, "files").as_list().unwrap() {
            let f = f.as_list().unwrap();
            let p = tmp.0.join(format!("{}.ndjson.gz", text(&f[0])));
            let file = std::fs::File::create(&p).unwrap();
            file.set_len(u64::try_from(int(&f[1])).unwrap()).unwrap();
            let at = now - num(&f[2]).unwrap() * 86400.0;
            file.set_modified(std::time::UNIX_EPOCH + std::time::Duration::from_secs_f64(at))
                .unwrap();
            state.insert(
                p.file_name().unwrap().to_string_lossy().to_string(),
                OMap::new().with("todo", J::List(vec![])).with("r2", true),
            );
        }
        state.insert(
            text(get(c, "hand_deleted")),
            OMap::new()
                .with("todo", J::List(vec![J::Int(0)]))
                .with("r2", false),
        );
        CLOCK.with(|cl| cl.set(now));
        let seg = Segments::new(&tmp.0, int(get(c, "days")), int(get(c, "cap_mb")), clock);
        seg.save(&state).unwrap();
        let dropped = seg.prune().unwrap();
        same(
            &J::List(dropped.into_iter().map(J::Str).collect()),
            get(c, "dropped"),
            "prune dropped",
        );
        same(&J::Obj(seg.load()), get(c, "state_after"), "prune state");
    }
    let tmp = Tmp::new("stats");
    let p = tmp.0.join("stats.json");
    for c in get(group(&v, "read_stats"), "cases").as_list().unwrap() {
        std::fs::write(&p, text(get(c, "file"))).unwrap();
        let out = get(c, "out");
        match read_stats(&p) {
            Ok(got) => same(&J::from(got), get(out, "ok"), "read_stats"),
            Err(e) => assert_eq!(
                e.to_string(),
                format!("AttributeError: {}", text(get(out, "error"))),
                "read_stats"
            ),
        }
    }
}

// --- quiet time, Pacer, Backfill ----------------------------------------------------------------------------

fn latest_of(v: &J) -> Option<Snapshot> {
    let m = v.as_obj()?;
    let srv = m.get("srv").map(srv_of).unwrap_or_default();
    let pl = m
        .get("pl")
        .and_then(J::as_list)
        .map_or_else(Vec::new, |l| l.iter().map(player_of).collect());
    Some(snapshot(0, srv, pl))
}

fn hm(c: &J, k: &str) -> u32 {
    u32::try_from(int(get(c, k))).unwrap()
}

fn done_j(d: Done) -> J {
    OMap::new()
        .with("rows", d.rows)
        .with("segments", d.segments)
        .with("requests", d.requests)
        .with("stopped", d.stopped)
        .with("failed", d.failed)
        .with("given_up", d.given_up)
        .into()
}

#[test]
#[allow(clippy::too_many_lines)]
fn backfill_vectors() {
    let v = vector_file!("backfill");
    for c in list(&v, "in_window") {
        assert_eq!(
            in_window(&text(get(c, "window")), hm(c, "hh"), hm(c, "mm")),
            get(c, "out") == &J::Bool(true),
            "in_window {c:?}"
        );
    }
    for c in list(&v, "quiet") {
        let latest = latest_of(get(c, "latest"));
        assert_eq!(
            quiet(latest.as_ref(), &text(get(c, "window")), hm(c, "hh"), 0),
            get(c, "out") == &J::Bool(true),
            "quiet {c:?}"
        );
    }
    let k = obj(group(&v, "constants"));
    assert_eq!(
        int(k.get("BACKFILL_BATCH").unwrap()),
        i64::try_from(super::backfill::BACKFILL_BATCH).unwrap()
    );
    assert_eq!(
        int(k.get("BACKFILL_ROW_GIVE_UP").unwrap()),
        i64::from(super::backfill::BACKFILL_ROW_GIVE_UP)
    );
    assert_eq!(
        int(k.get("BACKFILL_GROW_AFTER").unwrap()),
        i64::from(super::backfill::BACKFILL_GROW_AFTER)
    );
    let not_row: Vec<String> = k
        .get("NOT_ROW_ERRORS")
        .and_then(J::as_list)
        .unwrap()
        .iter()
        .map(text)
        .collect();
    assert_eq!(not_row, super::backfill::NOT_ROW_ERRORS);
    // the Pacer's decision trace (injected clock)
    let pv = group(&v, "pacer");
    set_clock(num(get(pv, "start")).unwrap());
    let mut pacer = Pacer::new(clock);
    for (i, step) in get(pv, "steps").as_list().unwrap().iter().enumerate() {
        let arg = get(step, "arg");
        let out = match text(get(step, "op")).as_str() {
            "tick" => {
                set_clock(clock() + num(arg).unwrap());
                J::Null
            }
            "ready" => J::Bool(pacer.ready()),
            "succeeded" => {
                pacer.succeeded();
                J::Null
            }
            _ => {
                let a = arg.as_list().unwrap();
                J::Bool(pacer.failed(&text(&a[0]), usize::try_from(int(&a[1])).unwrap()))
            }
        };
        let got = OMap::new()
            .with("op", get(step, "op").clone())
            .with("arg", arg.clone())
            .with("now", clock())
            .with("out", out)
            .with("size", pacer.size)
            .with("failures", i64::from(pacer.failures))
            .with("row_failures", i64::from(pacer.row_failures))
            .with("successes", i64::from(pacer.successes))
            .with("retry_at", pacer.retry_at);
        same(&J::Obj(got), step, &format!("Pacer step {i}"));
    }
    // Backfill.run_once traces
    for case in list(&v, "run_once") {
        let label = text(get(case, "label"));
        let tmp = Tmp::new("bf");
        set_clock(5000.0);
        let seg = Segments::new(&tmp.0, 30, 200, clock);
        let mut names = Vec::new();
        let r2s = get(case, "r2").as_list().unwrap();
        for (k, s) in get(case, "segments").as_list().unwrap().iter().enumerate() {
            let s = s.as_list().unwrap();
            let n = int(&s[0]);
            let base = i64::try_from(k).unwrap() * 100;
            let rows: Vec<J> = (0..n)
                .map(|i| {
                    OMap::new()
                        .with("id", format!("0badc0de:{}", base + i))
                        .with("ts", 1_768_471_200_000 + (base + i) * 60000)
                        .with("seq", base + i)
                        .into()
                })
                .collect();
            let acked: Vec<String> = s[1]
                .as_list()
                .unwrap()
                .iter()
                .map(|i| format!("0badc0de:{}", base + int(i)))
                .collect();
            let name = seg.write(&rows, &acked, "0badc0de").unwrap().unwrap();
            seg.mark(&name, &[], Some(r2s[k] == J::Bool(true))).unwrap();
            names.push(name);
        }
        if let Some(J::List(corrupt)) = obj(case).get("corrupt") {
            for k in corrupt {
                std::fs::write(
                    tmp.0.join(&names[usize::try_from(int(k)).unwrap()]),
                    b"not gzip",
                )
                .unwrap();
            }
        }
        let requests = std::cell::RefCell::new(Vec::<J>::new());
        let mut row_replies: Vec<J> = get(case, "rows").as_list().unwrap().clone();
        let mut put_replies: Vec<J> = get(case, "puts").as_list().unwrap().clone();
        let may: std::cell::RefCell<Option<Vec<J>>> =
            std::cell::RefCell::new(get(case, "may_run").as_list().cloned());
        let logs = std::cell::RefCell::new(Vec::<String>::new());
        let mut pacer = Pacer::new(clock);
        let mut rounds = Vec::new();
        // the case's own `rounds` (a count) is overwritten by the trace's (one entry per round)
        let n_rounds = get(case, "rounds").as_list().map_or(1, Vec::len);
        let dir = tmp.0.clone();
        for _ in 0..n_rounds {
            let mut send_rows = |rows: Vec<J>| -> Result<(), String> {
                requests.borrow_mut().push(
                    OMap::new()
                        .with(
                            "rows",
                            J::List(rows.iter().map(|r| get(r, "id").clone()).collect()),
                        )
                        .with(
                            "backfill",
                            rows.iter().all(|r| get(r, "backfill") == &J::Bool(true)),
                        )
                        .into(),
                );
                let err = if row_replies.is_empty() {
                    J::Null
                } else {
                    row_replies.remove(0)
                };
                if err.truthy() {
                    Err(text(&err))
                } else {
                    Ok(())
                }
            };
            let mut send_segment = |name: &str, _data: Vec<u8>| -> Result<(), String> {
                let n = read_segment(&dir.join(name)).map_or(0, |r| r.len());
                requests
                    .borrow_mut()
                    .push(OMap::new().with("segment", name).with("rows", n).into());
                let err = if put_replies.is_empty() {
                    J::Null
                } else {
                    put_replies.remove(0)
                };
                if err.truthy() {
                    Err(text(&err))
                } else {
                    Ok(())
                }
            };
            let may_run = || match may.borrow_mut().as_mut() {
                None => true,
                Some(m) if m.is_empty() => false,
                Some(m) => m.remove(0) == J::Bool(true),
            };
            let sleep = |s: f64| set_clock(clock() + s);
            let log = |m: &str| logs.borrow_mut().push(m.to_string());
            let done = Backfill {
                segments: &seg,
                send_rows: &mut send_rows,
                send_segment: &mut send_segment,
                may_run: &may_run,
                pause_s: 2.0,
                sleep: &sleep,
                pacer: &mut pacer,
                log: &log,
            }
            .run_once(usize::try_from(int(get(case, "max_requests"))).unwrap())
            .unwrap();
            rounds.push(J::Obj(
                OMap::new()
                    .with("now", clock())
                    .with("done", done_j(done))
                    .with("retry_at", pacer.retry_at)
                    .with("size", pacer.size),
            ));
            set_clock(clock().max(pacer.retry_at));
        }
        same(
            &J::List(names.into_iter().map(J::Str).collect()),
            get(case, "names"),
            &format!("{label}: names"),
        );
        same(
            &J::List(requests.into_inner()),
            get(case, "requests"),
            &format!("{label}: requests"),
        );
        same(
            &J::List(rounds),
            get(case, "rounds"),
            &format!("{label}: rounds"),
        );
        same(
            &J::List(logs.into_inner().into_iter().map(J::Str).collect()),
            get(case, "log"),
            &format!("{label}: log"),
        );
        same(
            &J::Obj(seg.load()),
            get(case, "state"),
            &format!("{label}: state"),
        );
    }
}

// --- verdicts and messages ----------------------------------------------------------------------------------

#[test]
fn verdict_vectors() {
    let v = vector_file!("verdicts");
    for c in list(&v, "verdicts_tsv") {
        let got = verdicts_tsv(obj(get(c, "verdicts")), int(get(c, "now_ms"))).unwrap();
        assert_eq!(got, text(get(c, "out")));
    }
    let m = group(&v, "messages");
    let limits = get(m, "limits");
    let tmp = Tmp::new("msg");
    let mut msgs = Messages::new(&tmp.0);
    msgs.per_player_s = int(get(limits, "per_player_s"));
    msgs.broadcast_s = int(get(limits, "broadcast_s"));
    msgs.join_grace_s = int(get(limits, "join_grace_s"));
    msgs.per_hour = int(get(limits, "per_hour"));
    assert!(
        (num(get(limits, "EXPIRE_S")).unwrap() - super::messages::EXPIRE_S).abs() < f64::EPSILON
    );
    let Value::Object(online) = get(m, "online").to_value() else {
        panic!("online")
    };
    for (i, step) in get(m, "steps").as_list().unwrap().iter().enumerate() {
        let (to, kind, now) = (
            text(get(step, "to")),
            text(get(step, "kind")),
            num(get(step, "now")).unwrap(),
        );
        let why = msgs.check(&to, &kind, now, &online).unwrap();
        if why.is_empty() {
            msgs.add(
                &format!("m{i}"),
                &to,
                &kind,
                &text(get(step, "text")),
                "hover\ttext",
                now,
            )
            .unwrap();
            assert_eq!(get(step, "refused"), &J::Null, "step {i}");
            assert_eq!(
                std::fs::read_to_string(tmp.0.join("messages.tsv")).unwrap(),
                text(get(step, "messages_tsv")),
                "step {i}"
            );
        } else {
            assert_eq!(why, text(get(step, "refused")), "step {i}");
        }
    }
}

// --- identity and settings ----------------------------------------------------------------------------------

#[test]
fn identity_vectors() {
    let v = vector_file!("identity");
    let tmp = Tmp::new("id");
    let p = tmp.0.join("telemetry-id.json");
    for c in list(&v, "load_identity") {
        let file = get(c, "file");
        match file {
            J::Null => {
                let _ = std::fs::remove_file(&p);
            }
            f => std::fs::write(&p, text(f)).unwrap(),
        }
        let (src, seq) = load_identity(&p);
        let out = get(c, "out");
        if get(out, "new_random_src") == &J::Bool(true) {
            assert!(
                src.len() == 8 && src.bytes().all(|b| b.is_ascii_hexdigit()),
                "{file:?}"
            );
            assert!(!text(file).contains(&src), "{file:?}: a new id");
        } else {
            assert_eq!(src, text(get(out, "src")), "{file:?}");
        }
        assert_eq!(seq, int(get(out, "seq")), "{file:?}");
    }
    let s = group(&v, "save_identity");
    save_identity(&p, &text(get(s, "src")), int(get(s, "seq"))).unwrap();
    assert_eq!(std::fs::read_to_string(&p).unwrap(), text(get(s, "file")));
    for c in list(&v, "config_from_env") {
        let env: Env = obj(get(c, "env"))
            .iter()
            .map(|(k, x)| (k.clone(), text(x)))
            .collect();
        let out = get(c, "out");
        match Config::from_env(&env, Path::new("/opt/minecraft/server"), "world") {
            Ok(cfg) => {
                let p = |x: &Path| J::Str(x.to_string_lossy().to_string());
                let got = OMap::new()
                    .with("snap_dir", p(&cfg.snap_dir))
                    .with("verdict_dir", p(&cfg.verdict_dir))
                    .with("segment_dir", p(&cfg.segment_dir))
                    .with("stats_dir", cfg.stats_dir.as_deref().map(p))
                    .with("segment_days", cfg.segment_days)
                    .with("segment_cap_mb", cfg.segment_cap_mb)
                    .with("backfill_window", cfg.backfill_window.as_str())
                    .with("backfill_pause_s", cfg.backfill_pause_s)
                    .with("read_s", cfg.read_s)
                    .with("segment_s", cfg.segment_s);
                same(&J::Obj(got), get(out, "ok"), &format!("Config {env:?}"));
            }
            Err(e) => assert_eq!(e, text(get(out, "error")), "Config {env:?}"),
        }
    }
}
