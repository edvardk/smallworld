//! mc_panel.py `merge_sync` (291-312), `revert_live` (705), `sync_pending` (722-814), `sync_world` (834-882).
//! A round works on a snapshot of pending.json (read under the lock), does its GitHub/RCON/Discord work without
//! the lock, then merges its result back into pending.json as it is by then (`Pending::update` + `merge_sync`):
//! what was recorded meanwhile wins. Owner: sync.

use super::alerts::{expired_alert, rejected_alert, world_rejected_alert};
use super::github::{GitHub, is_github_error};
use super::py::{exc_str, get_attr, is_type_error, obj_eq, py_eq, py_int, sub, type_error};
use crate::panel::pending::Pending;
use crate::panel::{NAME, PENDING_TTL_S};
use crate::types::{AResult, AgentError, Obj, Value, get, py_float, py_str, truthy};
use serde_json::json;
use smallworld_core::pyjson::py_repr;
use smallworld_core::pystr::prefix_cp;
use std::fmt::Write as _;

#[allow(clippy::cast_precision_loss)]
const TTL_S: f64 = PENDING_TTL_S as f64;

/// `merge_sync(current, snap, done)`: the round's result (`done`, worked out from `snap`) applied to pending.json
/// as it is now (`current`). An entry new or recorded again since the snapshot is kept as recorded; a world
/// change keeps its new state and takes the round's view of the open world PR; pr, error and expired are the
/// round's.
///
/// # Errors
/// Python's `KeyError`/`TypeError` for inputs that aren't `Pending.load()` shaped.
pub fn merge_sync(current: &Obj, snap: &Obj, done: &Obj) -> AResult<Obj> {
    let index = |o: &Obj| -> AResult<Vec<(Value, Value)>> {
        let mut m: Vec<(Value, Value)> = Vec::new();
        for e in crate::types::py_iter(sub(&Value::Object(o.clone()), "entries")?)? {
            let u = sub(&e, "uuid")?.clone();
            match m.iter_mut().find(|(k, _)| py_eq(k, &u)) {
                Some(slot) => slot.1 = e,
                None => m.push((u, e)),
            }
        }
        Ok(m)
    };
    let before = index(snap)?;
    let after = index(done)?;
    let find = |m: &[(Value, Value)], u: &Value| {
        m.iter().find(|(k, _)| py_eq(k, u)).map(|(_, v)| v.clone())
    };
    let mut entries = Vec::new();
    for e in crate::types::py_iter(sub(&Value::Object(current.clone()), "entries")?)? {
        let u = sub(&e, "uuid")?;
        if !find(&before, u).is_some_and(|b| py_eq(&b, &e)) {
            entries.push(e); // new, or recorded again since the snapshot
        } else if let Some(a) = find(&after, u) {
            entries.push(a);
        } // else: the round dropped it (expired, merged, rejected, already in Git)
    }
    let key = |o: &Obj, k: &str| -> AResult<Value> {
        o.get(k).cloned().ok_or_else(|| AgentError::Key(k.into()))
    };
    let mut out = current.clone();
    out.insert("entries".into(), Value::Array(entries));
    out.insert("pr".into(), key(done, "pr")?);
    out.insert("error".into(), key(done, "error")?);
    out.insert("expired".into(), key(done, "expired")?);
    let cur_world = key(current, "world")?;
    if py_eq(&cur_world, &key(snap, "world")?) {
        out.insert("world".into(), key(done, "world")?);
    } else if !cur_world.is_null() {
        let dw = key(done, "world")?;
        let open_pr = if truthy(&dw) {
            get_attr(&dw, "open_pr")?.clone()
        } else {
            Value::Null
        };
        let Value::Object(mut w) = cur_world else {
            return Err(type_error("'str' object is not a mapping"));
        };
        w.insert("open_pr".into(), open_pr);
        out.insert("world".into(), Value::Object(w));
    }
    Ok(out)
}

/// `revert_live(entries, rcon)`: undo panel whitelist changes on the running server; (reverted, failed).
///
/// # Errors
/// What `rcon` raises beyond `(OSError, RconError, KeyError, ValueError)`.
pub fn revert_live(
    entries: &[Value],
    rcon: &dyn Fn(&str) -> AResult<String>,
) -> AResult<(Vec<String>, Vec<String>)> {
    let mut reverted = Vec::new();
    let mut failed = Vec::new();
    for e in entries {
        let name = match get_attr(e, "name")? {
            Value::Null if e.get("name").is_none() => String::new(),
            v => py_str(v),
        };
        let op = get_attr(e, "op")?;
        if !NAME.is_match(&name) || !(op == "add" || op == "remove") {
            continue;
        }
        let undo = if op == "add" { "remove" } else { "add" };
        let ok = match rcon(&format!("whitelist {undo} {name}")) {
            Ok(_) => true,
            Err(err) if err.is_rcon_soft() => false,
            Err(err) => return Err(err),
        };
        if ok { &mut reverted } else { &mut failed }.push(format!("{undo} {name}"));
    }
    Ok((reverted, failed))
}

/// mc_panel.py `closed_error(number, failed)`: pending.json's `error` after a panel PR was closed unmerged.
pub fn closed_error(number: i64, failed: &[String]) -> String {
    let mut error =
        format!("pull request #{number} was closed without merging: reverted on the server");
    if !failed.is_empty() {
        let _ = write!(
            error,
            " (couldn't revert {}: the next deploy follows Git)",
            failed.join(", ")
        );
    }
    error
}

/// A panel PR the round found closed unmerged: its number and entries (reverted at the merge, v1.5).
type Closed = Option<(i64, Vec<Value>)>;
/// `revert_live`'s (reverted, failed).
type Reverted = (Vec<String>, Vec<String>);

/// What a round calls out to (`alert`, `log`, `rcon`) and its timings.
pub struct Round<'a> {
    /// `notify.discord`.
    pub alert: &'a dyn Fn(&str),
    /// `audit(message, action=..., result=...)`.
    pub log: &'a dyn Fn(&str, &str, &str),
    /// `rcon_command`.
    pub rcon: &'a dyn Fn(&str) -> AResult<String>,
    pub batch_s: f64,
    pub pr_check_s: f64,
}

/// The round's `except (GitHubError, ValueError, KeyError, TypeError)`.
fn caught(e: &AgentError) -> bool {
    is_github_error(e)
        || matches!(e, AgentError::Value(..) | AgentError::Key(_))
        || is_type_error(e)
}

fn or_zero(v: &Value) -> AResult<f64> {
    if truthy(v) { py_float(v) } else { Ok(0.0) }
}

/// `a + b` for the `expired` list, then `[-20:]`.
fn append_expired(data: &mut Obj, gone: Vec<Value>) -> AResult<()> {
    let mut list = match get(data, "expired") {
        Value::Array(a) => a.clone(),
        other => {
            return Err(type_error(&format!(
                "can only concatenate {} (not \"list\") to {}",
                crate::worlds::py_type(other),
                crate::worlds::py_type(other)
            )));
        }
    };
    list.extend(gone);
    let skip = list.len().saturating_sub(20);
    data.insert("expired".into(), Value::Array(list.split_off(skip)));
    Ok(())
}

fn entries_mut(data: &mut Obj) -> &mut Vec<Value> {
    if !matches!(data.get("entries"), Some(Value::Array(_))) {
        data.insert("entries".into(), Value::Array(Vec::new()));
    }
    match data.get_mut("entries") {
        Some(Value::Array(a)) => a,
        _ => unreachable!("just inserted"),
    }
}

fn entries(data: &Obj) -> Vec<Value> {
    match data.get("entries") {
        Some(Value::Array(a)) => a.clone(),
        _ => Vec::new(),
    }
}

fn pr_entry(e: &Value) -> AResult<Value> {
    Ok(get_attr(e, "pr")?.clone())
}

/// `sync_pending(pending, gh, now, alert, log, batch_s, pr_check_s, rcon)`: one round of the whitelist -> Git
/// flow. True if pending.json changed (push the inventory).
///
/// # Errors
/// What Python doesn't catch in the round (a broken `ts` in pending.json, an `AttributeError`, a non-soft RCON
/// error); the panel loop prints it.
pub fn sync_pending(
    pending: &Pending,
    gh: Option<&GitHub>,
    now: f64,
    r: &Round<'_>,
) -> AResult<bool> {
    // a snapshot under the lock, the round (GitHub, RCON, Discord) without it, then merged back
    let snap = pending.snapshot()?;
    let mut data = snap.clone();
    let mut old = Vec::new();
    for e in entries(&data) {
        if now - or_zero(get_attr(&e, "ts")?)? > TTL_S {
            old.push(e);
        }
    }
    if !old.is_empty() {
        entries_mut(&mut data).retain(|e| !old.iter().any(|o| py_eq(e, o)));
        let mut gone = Vec::new();
        let mut names = Vec::new();
        for e in &old {
            gone.push(json!({"name": sub(e, "name")?, "op": sub(e, "op")?, "ts": now}));
        }
        append_expired(&mut data, gone)?;
        for e in &old {
            names.push(format!(
                "{} {}",
                py_str(sub(e, "op")?),
                py_str(sub(e, "name")?)
            ));
        }
        let names = names.join(", ");
        (r.alert)(&expired_alert(&names));
        (r.log)(
            &format!("panel whitelist entries expired: {names}"),
            "whitelist-expire",
            "dropped",
        );
    }
    let mut closed: Closed = None;
    match gh {
        None => {
            data.insert(
                "error".into(),
                json!("no config repo for the panel (CONFIG_REPO): changes stay pending until they expire"),
            );
        }
        Some(gh) => {
            if let Err(e) = github_part(&mut data, gh, now, r, &mut closed) {
                if !caught(&e) {
                    return Err(e);
                }
                data.insert(
                    "error".into(),
                    json!(format!("GitHub: {}", prefix_cp(&exc_str(&e), 200))),
                );
            }
        }
    }
    if obj_eq(&data, &snap) {
        return Ok(false);
    }
    // v1.5 (the revert race): the revert runs here, under the pending lock (the panel's whitelist action holds
    // it across its RCON command and record), for the entries still as the snapshot had them only
    let (changed, outcome) = pending.update(|current| -> AResult<(bool, Option<Reverted>)> {
        let mut merged = merge_sync(current, &snap, &data)?;
        let mut outcome = None;
        if let Some((number, rejected)) = &closed {
            let now_entries = entries(current);
            let still: Vec<Value> = rejected
                .iter()
                .filter(|e| {
                    let uuid = e.get("uuid");
                    now_entries
                        .iter()
                        .rev()
                        .find(|c| c.get("uuid") == uuid)
                        .is_some_and(|c| py_eq(c, e))
                })
                .cloned()
                .collect();
            let (reverted, failed) = revert_live(&still, r.rcon)?;
            if get(&merged, "error") == &json!(closed_error(*number, &[])) {
                merged.insert("error".into(), json!(closed_error(*number, &failed)));
            }
            outcome = Some((reverted, failed));
        }
        let changed = !obj_eq(&merged, current);
        *current = merged;
        Ok((changed, outcome))
    })??;
    if let (Some((number, _)), Some((reverted, failed))) = (&closed, outcome) {
        (r.alert)(&rejected_alert(*number, &reverted, &failed));
        let list = |l: &[String]| py_repr(&json!(l));
        (r.log)(
            &format!(
                "panel PR #{number} closed unmerged; reverted {} failed {}",
                list(&reverted),
                list(&failed)
            ),
            "whitelist-pr",
            "reverted",
        );
    }
    Ok(changed)
}

/// The `try:` block of `sync_pending`.
fn github_part(
    data: &mut Obj,
    gh: &GitHub,
    now: f64,
    r: &Round<'_>,
    closed: &mut Closed,
) -> AResult<()> {
    let mut pr = get(data, "pr").clone();
    if truthy(&pr)
        && truthy(get_attr(&pr, "number")?)
        && now - or_zero(get_attr(&pr, "checked")?)? >= r.pr_check_s
    {
        let state = gh.pr_state(py_int(sub(&pr, "number")?)?)?;
        if let Value::Object(o) = &mut pr {
            o.insert("checked".into(), json!(now));
        }
        data.insert("pr".into(), pr.clone());
        let number_raw = sub(&pr, "number")?.clone();
        if state == "merged" {
            let mut kept = Vec::new();
            for e in entries(data) {
                if !py_eq(&pr_entry(&e)?, &number_raw) {
                    kept.push(e);
                }
            }
            *entries_mut(data) = kept;
            (r.log)(
                &format!("panel PR #{} merged", py_str(&number_raw)),
                "whitelist-pr",
                "merged",
            );
            data.insert("pr".into(), Value::Null);
            pr = Value::Null;
        } else if state == "closed" {
            let number = py_int(&number_raw)?;
            let nv = json!(number);
            let mut rejected = Vec::new();
            for e in entries(data) {
                if py_eq(&pr_entry(&e)?, &nv) {
                    rejected.push(e);
                }
            }
            let mut kept = Vec::new();
            for e in entries(data) {
                if !py_eq(&pr_entry(&e)?, &nv) {
                    kept.push(e);
                }
            }
            *entries_mut(data) = kept;
            // the revert's failures: when it runs (at the merge)
            data.insert("error".into(), json!(closed_error(number, &[])));
            let why = format!("PR #{number} closed");
            let mut gone = Vec::new();
            for e in &rejected {
                gone.push(
                    json!({"name": sub(e, "name")?, "op": sub(e, "op")?, "ts": now, "why": why}),
                );
            }
            append_expired(data, gone)?;
            *closed = Some((number, rejected));
            data.insert("pr".into(), Value::Null);
            pr = Value::Null;
        }
    }
    let mut fresh = Vec::new();
    for e in entries(data) {
        if pr_entry(&e)?.is_null() {
            fresh.push(e);
        }
    }
    let changed_ts = py_float(get(data, "changed"))?;
    if !fresh.is_empty() && !truthy(&pr) && now - changed_ts >= r.batch_s {
        let made = gh.open_pr(&fresh, now)?;
        match made.number {
            None => {
                entries_mut(data).retain(|e| !fresh.iter().any(|f| py_eq(e, f))); // Git already has them
            }
            Some(number) => {
                for e in entries_mut(data).iter_mut() {
                    if let Value::Object(o) = e
                        && get(o, "pr").is_null()
                    {
                        let u = get(o, "uuid").clone();
                        let v = if made.changed.iter().any(|c| py_eq(c, &u)) {
                            json!(number)
                        } else {
                            json!(0)
                        };
                        o.insert("pr".into(), v);
                    }
                }
                entries_mut(data)
                    .retain(|e| !py_eq(e.get("pr").unwrap_or(&Value::Null), &json!(0)));
                data.insert(
                    "pr".into(),
                    json!({"number": number, "url": made.url, "branch": made.branch, "created": now, "checked": now}),
                );
                (r.log)(
                    &format!("panel PR #{number} opened"),
                    "whitelist-pr",
                    "opened",
                );
            }
        }
        data.insert("error".into(), json!(""));
    }
    sync_world(data, gh, now, r)
}

fn world_mut(data: &mut Obj) -> Option<&mut Obj> {
    match data.get_mut("world") {
        Some(Value::Object(w)) => Some(w),
        _ => None,
    }
}

/// `sync_world(data, gh, now, alert, log, batch_s, pr_check_s)`: the world library -> server.yml in Git, one
/// pull request at a time.
///
/// # Errors
/// GitHub's, and Python's `ValueError`/`KeyError`/`TypeError`/`AttributeError` for a broken world record.
pub fn sync_world(data: &mut Obj, gh: &GitHub, now: f64, r: &Round<'_>) -> AResult<()> {
    let w = get(data, "world").clone();
    if !truthy(&w) {
        return Ok(());
    }
    let pr = get_attr(&w, "open_pr")?.clone();
    if truthy(&pr)
        && truthy(get_attr(&pr, "number")?)
        && now - or_zero(get_attr(&pr, "checked")?)? >= r.pr_check_s
    {
        let state = gh.pr_state(py_int(sub(&pr, "number")?)?)?;
        let number_raw = sub(&pr, "number")?.clone();
        if let Some(Value::Object(o)) = world_mut(data).and_then(|w| w.get_mut("open_pr")) {
            o.insert("checked".into(), json!(now));
        }
        let w_pr = get_attr(&w, "pr")?.clone();
        if state == "merged" {
            (r.log)(
                &format!("panel world PR #{} merged", py_str(&number_raw)),
                "world-pr",
                "merged",
            );
            if py_eq(&w_pr, &number_raw) {
                data.insert("world".into(), Value::Null);
                return Ok(());
            }
            if let Some(w) = world_mut(data) {
                w.insert("open_pr".into(), Value::Null);
            }
        } else if state == "closed" {
            (r.log)(
                &format!("panel world PR #{} closed unmerged", py_str(&number_raw)),
                "world-pr",
                "closed",
            );
            let active = py_str(get_attr(&w, "active")?);
            (r.alert)(&world_rejected_alert(py_int(&number_raw)?, &active));
            let neg = if py_eq(&w_pr, &number_raw) {
                Some(-py_int(&number_raw)?)
            } else {
                None
            };
            if let Some(w) = world_mut(data) {
                w.insert(
                    "error".into(),
                    json!(format!(
                        "pull request #{} was closed without merging; the live world still counts",
                        py_str(&number_raw)
                    )),
                );
                if let Some(n) = neg {
                    w.insert("pr".into(), json!(n)); // nothing new to propose until the next world change
                }
                w.insert("open_pr".into(), Value::Null);
            }
        }
    }
    let w = get(data, "world").clone();
    if get_attr(&w, "pr")?.is_null()
        && !truthy(get_attr(&w, "open_pr")?)
        && now - or_zero(get_attr(&w, "ts")?)? >= r.batch_s
    {
        let made = gh.open_world_pr(&w, now)?;
        let Some(number) = made.number else {
            data.insert("world".into(), Value::Null); // Git already says so
            return Ok(());
        };
        if let Some(w) = world_mut(data) {
            w.insert("pr".into(), json!(number));
            w.insert(
                "open_pr".into(),
                json!({"number": number, "url": made.url, "branch": made.branch, "created": now, "checked": now}),
            );
            w.insert("error".into(), json!(""));
        }
        (r.log)(
            &format!("panel world PR #{number} opened"),
            "world-pr",
            "opened",
        );
    }
    Ok(())
}

#[cfg(test)]
#[allow(clippy::many_single_char_names, clippy::semicolon_if_nothing_returned)]
mod tests {
    use super::super::github::FixedToken;
    use super::super::github::tests::{Fake, gh, players_routes};
    use super::*;
    use std::sync::{Arc, Mutex};

    #[test]
    fn merge_sync_matches_the_vectors() {
        let v: Value = serde_json::from_str(include_str!(
            "../../../../tests/agent_parity/vectors/merge_sync.json"
        ))
        .unwrap();
        let cases = v["vectors"].as_array().unwrap();
        assert_eq!(cases.len(), 8);
        for case in cases {
            let o = |k: &str| case[k].as_object().unwrap().clone();
            let got = merge_sync(&o("current"), &o("snap"), &o("done")).unwrap();
            assert_eq!(Value::Object(got), case["result"], "{}", case["case"]);
        }
    }

    const A: &str = "11111111-1111-4111-8111-111111111111";
    const B: &str = "22222222-2222-4222-8222-222222222222";

    fn dir(tag: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("swagent-sync-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    struct Calls {
        alerts: Mutex<Vec<String>>,
        logs: Mutex<Vec<(String, String, String)>>,
        rcon: Mutex<Vec<String>>,
    }

    fn calls() -> Arc<Calls> {
        Arc::new(Calls {
            alerts: Mutex::new(Vec::new()),
            logs: Mutex::new(Vec::new()),
            rcon: Mutex::new(Vec::new()),
        })
    }

    fn round(c: &Arc<Calls>, f: impl FnOnce(&Round<'_>)) {
        let alert = |m: &str| c.alerts.lock().unwrap().push(m.into());
        let log = |m: &str, a: &str, res: &str| {
            c.logs
                .lock()
                .unwrap()
                .push((m.into(), a.into(), res.into()))
        };
        let rcon = |l: &str| -> AResult<String> {
            c.rcon.lock().unwrap().push(l.into());
            if l.contains("Fail") {
                Err(AgentError::Os("ConnectionRefusedError".into()))
            } else {
                Ok(String::new())
            }
        };
        f(&Round {
            alert: &alert,
            log: &log,
            rcon: &rcon,
            batch_s: 60.0,
            pr_check_s: 120.0,
        });
    }

    /// A token source that runs `meanwhile` once, on the round's first GitHub call: what the panel does while
    /// the sync loop is out on the network.
    struct Meanwhile(Mutex<Option<Box<dyn FnOnce() + Send>>>);

    impl super::super::github::TokenSource for Meanwhile {
        fn token(&self) -> AResult<String> {
            if let Some(f) = self.0.lock().unwrap().take() {
                f();
            }
            Ok("t".into())
        }
        fn invalidate(&self) -> bool {
            false
        }
    }

    /// v1.5, the revert race (tests/test_panel_actions.py `test_the_whitelist_revert_race`): a player changed
    /// again in the panel while the round finds the PR closed keeps the newer change; an unchanged entry of the
    /// same PR is still reverted.
    #[test]
    fn the_whitelist_revert_race() {
        for again in ["add", "remove"] {
            let d = dir(&format!("race-{again}"));
            let p = Pending::new(d.join("pending.json"));
            p.record("add", "alice", A, "panel:a", 1.0).unwrap();
            p.record("add", "bob", B, "panel:b", 2.0).unwrap();
            let mut l = p.load().unwrap();
            for e in l.get_mut("entries").unwrap().as_array_mut().unwrap() {
                e["pr"] = json!(5);
            }
            l.insert("pr".into(), json!({"number": 5, "checked": 0.0}));
            p.update(|data| *data = l).unwrap();
            let fake = Fake::new();
            fake.route(
                "GET /repos/o/cfg/pulls/5",
                200,
                &json!({"state": "closed", "merged": false}),
            );
            let path = d.join("pending.json");
            let g = gh(
                &fake,
                Arc::new(Meanwhile(Mutex::new(Some(Box::new(move || {
                    Pending::new(path)
                        .record(again, "alice", A, "panel:a", 150.0)
                        .unwrap();
                }))))),
            );
            let c = calls();
            round(&c, |r| {
                let r = Round {
                    batch_s: 10_000.0,
                    ..*r
                };
                sync_pending(&p, Some(&g), 500.0, &r).unwrap();
            });
            assert_eq!(*c.rcon.lock().unwrap(), vec!["whitelist remove bob"]);
            let l = p.load().unwrap();
            let e = &l["entries"].as_array().unwrap()[..];
            assert_eq!(e.len(), 1);
            assert_eq!(
                (&e[0]["name"], &e[0]["op"], &e[0]["pr"]),
                (&json!("alice"), &json!(again), &Value::Null)
            );
            assert!(!c.alerts.lock().unwrap()[0].contains(" alice"));
            std::fs::remove_dir_all(d).unwrap();
        }
    }

    #[test]
    fn whitelist_round_trip_open_then_closed() {
        let d = dir("closed");
        let p = Pending::new(d.join("pending.json"));
        p.record("add", "alice", A, "panel:a", 1000.0).unwrap();
        p.record("remove", "Fail", B, "panel:b", 1000.0).unwrap();
        let fake = Fake::new();
        players_routes(
            &fake,
            &format!("whitelist:\n  - {{name: \"Fail\", uuid: \"{B}\"}}\nops: []\n"),
        );
        fake.route("POST /repos/o/cfg/git/refs", 201, &json!({}));
        fake.route("PUT /repos/o/cfg/contents/players.yml", 200, &json!({}));
        fake.route(
            "POST /repos/o/cfg/pulls",
            201,
            &json!({"number": 5, "html_url": "u"}),
        );
        let g = gh(&fake, Arc::new(FixedToken("t".into())));
        let c = calls();
        // within the batch window: nothing
        round(&c, |r| {
            assert!(!sync_pending(&p, Some(&g), 1030.0, r).unwrap())
        });
        round(&c, |r| {
            assert!(sync_pending(&p, Some(&g), 1060.0, r).unwrap())
        });
        let l = p.load().unwrap();
        assert_eq!(l["pr"]["number"], json!(5));
        assert_eq!(l["entries"][0]["pr"], json!(5));
        assert_eq!(l["error"], json!(""));
        // closed unmerged -> reverted live, dropped, Discord
        fake.route(
            "GET /repos/o/cfg/pulls/5",
            200,
            &json!({"state": "closed", "merged": false}),
        );
        round(&c, |r| {
            assert!(sync_pending(&p, Some(&g), 1200.0, r).unwrap())
        });
        let l = p.load().unwrap();
        assert_eq!(l["entries"], json!([]));
        assert_eq!(l["pr"], Value::Null);
        assert_eq!(
            l["error"],
            json!(
                "pull request #5 was closed without merging: reverted on the server (couldn't revert add Fail: the next deploy follows Git)"
            )
        );
        assert_eq!(l["expired"].as_array().unwrap().len(), 2);
        assert_eq!(
            *c.rcon.lock().unwrap(),
            vec!["whitelist remove alice", "whitelist add Fail"]
        );
        assert_eq!(
            c.alerts.lock().unwrap()[0],
            rejected_alert(5, &["remove alice".into()], &["add Fail".into()])
        );
        assert_eq!(
            c.logs.lock().unwrap().last().unwrap().0,
            "panel PR #5 closed unmerged; reverted ['remove alice'] failed ['add Fail']"
        );
        std::fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn expiry_and_no_config_repo() {
        let d = dir("expire");
        let p = Pending::new(d.join("pending.json"));
        p.record("add", "alice", A, "panel", 0.5).unwrap();
        let c = calls();
        round(&c, |r| {
            assert!(sync_pending(&p, None, 86_400.5 + 1.0, r).unwrap())
        });
        let l = p.load().unwrap();
        assert_eq!(l["entries"], json!([]));
        assert_eq!(
            l["expired"],
            json!([{"name": "alice", "op": "add", "ts": 86_401.5}])
        );
        assert_eq!(
            l["error"],
            json!(
                "no config repo for the panel (CONFIG_REPO): changes stay pending until they expire"
            )
        );
        assert_eq!(c.alerts.lock().unwrap()[0], expired_alert("add alice"));
        std::fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn a_github_error_lands_in_error_and_a_record_meanwhile_wins() {
        let d = dir("err");
        let p = Pending::new(d.join("pending.json"));
        p.record("add", "alice", A, "panel", 1000.0).unwrap();
        let fake = Fake::new(); // every route 404
        let g = gh(&fake, Arc::new(FixedToken("t".into())));
        let c = calls();
        round(&c, |r| {
            assert!(sync_pending(&p, Some(&g), 2000.0, r).unwrap())
        });
        assert_eq!(
            p.load().unwrap()["error"],
            json!("GitHub: GET /repos/o/cfg/git/ref/heads/main: HTTP 404")
        );
        std::fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn world_pr_opened_then_merged() {
        let d = dir("world");
        let p = Pending::new(d.join("pending.json"));
        let lib = vec![json!({"name": "main", "seed": "", "type": "default", "preset": null, "border_radius": null, "pregen": null}).as_object().unwrap().clone()];
        p.record_world(&json!("main"), lib, "switch to main", "panel:x", 1000.0)
            .unwrap();
        let fake = Fake::new();
        fake.route(
            "GET /repos/o/cfg/git/ref/heads/main",
            200,
            &json!({"object": {"sha": "h"}}),
        );
        let enc = base64::Engine::encode(&base64::engine::general_purpose::STANDARD, "name: x\n");
        fake.route(
            "GET /repos/o/cfg/contents/server.yml?ref=h",
            200,
            &json!({"content": enc, "sha": "s"}),
        );
        fake.route("POST /repos/o/cfg/git/refs", 201, &json!({}));
        fake.route("PUT /repos/o/cfg/contents/server.yml", 200, &json!({}));
        fake.route("POST /repos/o/cfg/pulls", 201, &json!({"number": 9}));
        let g = gh(&fake, Arc::new(FixedToken("t".into())));
        let c = calls();
        round(&c, |r| {
            assert!(sync_pending(&p, Some(&g), 1100.0, r).unwrap())
        });
        let w = p.load().unwrap()["world"].clone();
        assert_eq!(w["pr"], json!(9));
        assert_eq!(w["open_pr"]["number"], json!(9));
        assert_eq!(w["open_pr"]["url"], json!(""));
        let seen = fake.seen.lock().unwrap().clone();
        let put = seen
            .iter()
            .find(|s| s.0 == "PUT")
            .unwrap()
            .2
            .clone()
            .unwrap();
        assert_eq!(put["message"], "panel: world switch to main");
        fake.route("GET /repos/o/cfg/pulls/9", 200, &json!({"merged_at": "x"}));
        round(&c, |r| {
            assert!(sync_pending(&p, Some(&g), 1300.0, r).unwrap())
        });
        assert_eq!(p.load().unwrap()["world"], Value::Null);
        std::fs::remove_dir_all(d).unwrap();
    }
}
