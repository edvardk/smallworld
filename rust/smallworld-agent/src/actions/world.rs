//! The world library (mc_agent.py 1747-1944): world_create, world_switch, world_delete, their job (a request file
//! for mc-world.service, the unit's steps followed), `_record_world` (pending.json) and `worlds_inventory` (the
//! panel's Worlds tab, the cli's `worlds` list). Ported (the cli runs them). scripts/worlds.py does the work.

use super::{Executor, Panel, countdown, flag};
use crate::jobs::{Event, Job};
use crate::systemd::{systemctl, unit_steps};
use crate::types::{
    AResult, AgentError, Obj, RESTORE_TIMEOUT_S, Value, as_py_int_incl_bool, get, py_iter, truthy,
};
use crate::worlds::{self, EntryError, Library, WorldsState, check_entry, is_world_name};
use serde_json::json;
use smallworld_core::paths::units;
use smallworld_core::pyjson::dumps_sorted;
use std::sync::Arc;

/// `_worlds(panel)`'s result: worlds.json, the library (pending change, else the deploy's), the active name.
/// `active` is whatever Python would hold (normally a world name; `Value` because a hand-edited pending.json
/// can put anything there, and Python carries it along).
pub struct WorldsView {
    pub state: WorldsState,
    pub lib_active: Value,
    pub library: Vec<Obj>,
    pub active: Value,
}

/// `a["name"]` (KeyError if missing, as in Python).
fn name_of(a: &Obj) -> AResult<&Value> {
    a.get("name")
        .ok_or_else(|| AgentError::Crash("KeyError: 'name'".into()))
}

/// `x == name` for a JSON value and a str.
fn eq_str(v: &Value, s: &str) -> bool {
    v.as_str() == Some(s)
}

fn opt_str(o: Option<String>) -> Value {
    o.map_or(Value::Null, Value::String)
}

/// `_worlds(panel)`.
///
/// # Errors
/// What Python raises on hand-broken state files (`Crash`), pending.json's errors.
pub fn worlds_view(panel: &Panel) -> AResult<WorldsView> {
    let state = worlds::load_state(panel.worlds_state_path())?;
    let Library {
        active: lib_active,
        library,
    } = worlds::read_library(panel.worlds_library_path())?;
    let mut lib_active = opt_str(lib_active);
    let mut library = library;
    let pend = panel
        .pending
        .load()?
        .get("world")
        .cloned()
        .filter(truthy)
        .unwrap_or_else(|| json!({}));
    if let Some(raw_lib) = pend.get("library").filter(|v| !v.is_null()) {
        let mut entries = Vec::new();
        for raw in py_iter(raw_lib)? {
            match check_entry(&raw) {
                Ok(e) => entries.push(e),
                Err(EntryError::World(_)) => {}
                Err(EntryError::Crash(c)) => return Err(c),
            }
        }
        let pa = pend.get("active").cloned().unwrap_or(Value::Null);
        lib_active = if truthy(&pa) { pa } else { lib_active };
        library = entries;
    }
    let active = match &state.active {
        Some(a) => Value::String(a.clone()),
        None if truthy(&lib_active) => lib_active.clone(),
        None => json!("world"),
    };
    Ok(WorldsView {
        state,
        lib_active,
        library,
        active,
    })
}

/// The job's follower: every 3 s, the unit's latest step becomes the job's step (until `done`).
pub fn follow_steps(job: Arc<Job>, unit: String, since: String, done: Arc<Event>) {
    let spawned = std::thread::Builder::new()
        .name("world-follow".into())
        .spawn(move || {
            let mut last = String::new();
            while !done.wait(3.0) {
                match unit_steps(&unit, &since) {
                    Ok(steps) => {
                        if let Some(s) = steps.last().filter(|s| **s != last) {
                            last.clone_from(s);
                            job.set_step(s);
                        }
                    }
                    Err(e) => {
                        // Python: the thread dies with a traceback.
                        eprintln!("Exception in thread world-follow: {e}");
                        return;
                    }
                }
            }
        });
    drop(spawned);
}

/// `sorted(library.values(), key=lambda e: e["name"])` (names are strings; anything else can't be ordered).
fn sorted_by_name(mut entries: Vec<Obj>) -> AResult<Vec<Obj>> {
    if entries.iter().any(|e| !get(e, "name").is_string()) && entries.len() > 1 {
        return Err(AgentError::Crash(
            "TypeError: '<' not supported between instances of 'str' and 'int'".into(),
        ));
    }
    entries.sort_by(|a, b| get(a, "name").as_str().cmp(&get(b, "name").as_str()));
    Ok(entries)
}

/// `{e["name"]: e ...}` as an insertion-ordered map keyed by the name's JSON value.
struct ByName(Vec<(Value, Obj)>);

impl ByName {
    fn get(&self, k: &Value) -> Option<&Obj> {
        self.0.iter().find(|(n, _)| n == k).map(|(_, e)| e)
    }
    fn set(&mut self, k: Value, v: Obj) {
        match self.0.iter_mut().find(|(n, _)| *n == k) {
            Some(slot) => slot.1 = v,
            None => self.0.push((k, v)),
        }
    }
    fn pop(&mut self, k: &Value) {
        self.0.retain(|(n, _)| n != k);
    }
}

/// `_record_world(panel, op, name, entry, old_active, lib, login)`: what the server now runs -> pending.json.
/// Decided by worlds.json after the unit: a switch that happened but whose start failed still counts.
fn record_world(
    panel: &Panel,
    op: &str,
    name: &str,
    entry: Option<&Obj>,
    old_active: &Value,
    library: &[Obj],
    login: &str,
) -> AResult<()> {
    let state = worlds::load_state(panel.worlds_state_path())?;
    let mut lib = ByName(Vec::new());
    for e in library {
        lib.set(name_of(e)?.clone(), e.clone());
    }
    let (what, active) = if op == "delete" {
        for a in &state.archives {
            if eq_str(name_of(a)?, name) {
                return Ok(()); // not deleted
            }
        }
        lib.pop(&json!(name));
        let active = state
            .active
            .clone()
            .map_or_else(|| old_active.clone(), Value::String);
        (format!("delete {name}"), active)
    } else {
        if state.active.as_deref() != Some(name) {
            return Ok(()); // the switch didn't happen
        }
        if lib.get(old_active).is_none() {
            let placeholder = json!({"name": old_active, "seed": "", "type": "default", "preset": null,
                                     "border_radius": null, "pregen": null});
            lib.set(
                old_active.clone(),
                placeholder.as_object().cloned().unwrap_or_default(),
            );
        }
        let entry = entry.cloned().unwrap_or_default();
        let new = if op == "switch" {
            lib.get(&json!(name)).cloned().unwrap_or(entry)
        } else {
            entry
        };
        lib.set(json!(name), new);
        let verb = if op == "create" {
            "create"
        } else {
            "switch to"
        };
        (format!("{verb} {name}"), json!(name))
    };
    let entries = sorted_by_name(lib.0.into_iter().map(|(_, e)| e).collect())?;
    panel
        .pending
        .record_world(&active, entries, &what, login, (panel.clock)())
}

impl Executor {
    /// `_world_name(args, action)`.
    fn world_name(args: &Obj, action: &str) -> AResult<String> {
        match args.get("name") {
            Some(Value::String(n)) if is_world_name(n) => Ok(n.clone()),
            _ => Err(AgentError::Rejected(format!(
                "{action}: a world name is 1-32 lowercase letters, digits, _ or -"
            ))),
        }
    }

    /// `_world_job(action, label, name, op, entry, countdown, args)`.
    #[allow(clippy::too_many_arguments)]
    fn world_job(
        &mut self,
        action: &str,
        label: &str,
        name: &str,
        op: &str,
        entry: Option<Obj>,
        countdown: i64,
        args: &Obj,
    ) -> AResult<String> {
        let panel = self.need_panel(action)?;
        let view = worlds_view(&panel)?;
        let login = self.login.clone();
        let skip = flag(args, "skip_if_empty", true)?;
        let (p, act, nm, opn) = (
            panel.clone(),
            action.to_string(),
            name.to_string(),
            op.to_string(),
        );
        let work = Box::new(move |job: &Arc<Job>| -> AResult<String> {
            let mut request = serde_json::Map::new();
            request.insert("op".into(), json!(opn));
            request.insert("name".into(), json!(nm));
            request.insert(
                "entry".into(),
                entry.clone().map_or(Value::Null, Value::Object),
            );
            request.insert("id".into(), json!(job.id));
            request.insert("login".into(), json!(login));
            request.insert("ts".into(), json!(crate::sys::now()));
            let text = dumps_sorted(&Value::Object(request)) + "\n";
            smallworld_core::fsx::atomic_write(p.world_request_path(), text.as_bytes(), 0o640)?;
            let unit = units::WORLD;
            let since = crate::sys::local_since(crate::sys::now());
            let done = Arc::new(Event::default());
            job.set_step(&format!("starting {unit}"));
            follow_steps(job.clone(), unit.to_string(), since.clone(), done.clone());
            let started = systemctl(&["start", unit], RESTORE_TIMEOUT_S);
            let failed = match started {
                Ok(()) => String::new(),
                Err(AgentError::Os(c)) if c == "CalledProcessError" => {
                    let steps = unit_steps(unit, &since);
                    done.set();
                    let steps = steps?;
                    steps
                        .last()
                        .cloned()
                        .unwrap_or_else(|| format!("see journalctl -u {unit}"))
                }
                Err(e) => {
                    done.set();
                    return Err(e);
                }
            };
            done.set();
            record_world(
                &p,
                &opn,
                &nm,
                entry.as_ref(),
                &view.active,
                &view.library,
                &login,
            )?;
            (p.changed)();
            if !failed.is_empty() {
                return Err(AgentError::Action(format!("{act} failed ({failed})")));
            }
            let steps = unit_steps(unit, &since)?;
            Ok(steps
                .last()
                .cloned()
                .unwrap_or_else(|| format!("{act} {nm}: done")))
        });
        let job = panel.jobs.start(
            &self.cmd_id,
            action,
            label,
            &self.login,
            countdown,
            work,
            skip,
        )?;
        Ok(format!(
            "{} {name} scheduled (countdown {countdown} s, job {})",
            action.replace('_', " "),
            job.id
        ))
    }

    /// `do_world_create(args)`: a new world from a seed, type and preset; the active one is archived first.
    pub(crate) fn do_world_create(&mut self, args: &Obj) -> AResult<String> {
        let panel = self.need_panel("world_create")?;
        let name = Self::world_name(args, "world_create")?;
        let view = worlds_view(&panel)?;
        let mut exists = eq_str(&view.active, &name);
        if !exists {
            for a in &view.state.archives {
                if eq_str(name_of(a)?, &name) {
                    exists = true;
                    break;
                }
            }
        }
        if exists {
            return Err(AgentError::Rejected(format!(
                "world_create: {name} already exists"
            )));
        }
        if view
            .state
            .deleted
            .iter()
            .any(|d| eq_str(get(d, "name"), &name))
        {
            return Err(AgentError::Rejected(format!(
                "world_create: {name} was deleted recently (its archive isn't forgotten yet): pick another"
            )));
        }
        let pick = |k: &str, d: Value| args.get(k).cloned().unwrap_or(d);
        let raw = json!({
            "name": name,
            "seed": pick("seed", json!("")),
            "type": pick("type", json!("default")),
            "preset": pick("preset", json!("survival_smp")),
            "border_radius": get(args, "border_radius"),
            "pregen": get(args, "pregen"),
        });
        let entry = match check_entry(&raw) {
            Ok(e) => e,
            Err(EntryError::World(m)) => {
                return Err(AgentError::Rejected(format!("world_create: {m}")));
            }
            Err(EntryError::Crash(c)) => return Err(c),
        };
        let cd = countdown(args)?;
        self.world_job(
            "world_create",
            "World switch",
            &name,
            "create",
            Some(entry),
            cd,
            args,
        )
    }

    /// `do_world_switch(args)`: switch to an archived world (its library settings).
    pub(crate) fn do_world_switch(&mut self, args: &Obj) -> AResult<String> {
        let panel = self.need_panel("world_switch")?;
        let name = Self::world_name(args, "world_switch")?;
        let view = worlds_view(&panel)?;
        if eq_str(&view.active, &name) {
            return Err(AgentError::Rejected(format!(
                "world_switch: {name} is already the active world"
            )));
        }
        let mut archive = None;
        for a in &view.state.archives {
            if eq_str(name_of(a)?, &name) {
                archive = Some(a);
                break;
            }
        }
        let Some(archive) = archive else {
            return Err(AgentError::Rejected(format!(
                "world_switch: {name} isn't an archived world (create it instead)"
            )));
        };
        let why = worlds::version_check(get(archive, "data_version"), &view.state.data_version)?;
        if !why.is_empty() {
            return Err(AgentError::Rejected(format!("world_switch: {why}")));
        }
        let mut entry = None;
        for e in &view.library {
            if eq_str(name_of(e)?, &name) {
                entry = Some(e.clone());
                break;
            }
        }
        let entry = match entry.filter(|e| !e.is_empty()) {
            Some(e) => e,
            None => match check_entry(&json!({"name": name})) {
                Ok(e) => e,
                Err(EntryError::World(m)) => {
                    return Err(AgentError::Crash(format!("WorldError: {m}")));
                }
                Err(EntryError::Crash(c)) => return Err(c),
            },
        };
        let cd = countdown(args)?;
        self.world_job(
            "world_switch",
            "World switch",
            &name,
            "switch",
            Some(entry),
            cd,
            args,
        )
    }

    /// `do_world_delete(args)`: forget an archived world (never the active one); the server isn't touched.
    pub(crate) fn do_world_delete(&mut self, args: &Obj) -> AResult<String> {
        let panel = self.need_panel("world_delete")?;
        let name = Self::world_name(args, "world_delete")?;
        let view = worlds_view(&panel)?;
        if eq_str(&view.active, &name) {
            return Err(AgentError::Rejected(format!(
                "world_delete: {name} is the active world; switch to another one first"
            )));
        }
        let mut found = false;
        for a in &view.state.archives {
            if eq_str(name_of(a)?, &name) {
                found = true;
                break;
            }
        }
        if !found {
            return Err(AgentError::Rejected(format!(
                "world_delete: {name} isn't an archived world"
            )));
        }
        let skip = json!({"skip_if_empty": true});
        self.world_job(
            "world_delete",
            "World delete",
            &name,
            "delete",
            None,
            0,
            skip.as_object().unwrap_or(&Obj::new()),
        )
    }
}

/// Python's `len(x)` for `a.get("snapshots") or []`.
fn py_len(v: &Value) -> AResult<usize> {
    match v {
        Value::Null | Value::Bool(false) => Ok(0),
        Value::Array(a) => Ok(a.len()),
        Value::Object(o) => Ok(o.len()),
        Value::String(s) => Ok(s.chars().count()),
        Value::Number(n) if n.as_f64() == Some(0.0) => Ok(0),
        other => Err(AgentError::Crash(format!(
            "TypeError: object of type '{}' has no len()",
            worlds::py_type(other)
        ))),
    }
}

/// `worlds_inventory(panel, executor)`: the panel's Worlds tab (the cli's `worlds` list).
///
/// # Errors
/// What Python raises on hand-broken state files.
pub fn worlds_inventory(panel: &Panel) -> AResult<Value> {
    let view = worlds_view(panel)?;
    let pend = panel
        .pending
        .load()?
        .get("world")
        .cloned()
        .filter(truthy)
        .unwrap_or_else(|| json!({}));
    let mut archives = Vec::new();
    for a in &view.state.archives {
        if get(a, "name") == &view.active {
            continue;
        }
        let mut o = Obj::new();
        for k in ["name", "short_id", "time", "size", "data_version"] {
            o.insert(k.into(), get(a, k).clone());
        }
        o.insert("count".into(), json!(py_len(get(a, "snapshots"))?));
        archives.push(o);
    }
    archives.truncate(100);
    let total: i64 = archives
        .iter()
        .filter_map(|a| as_py_int_incl_bool(get(a, "size")))
        .sum();
    let pending = if truthy(&pend) {
        let open_pr = pend
            .get("open_pr")
            .cloned()
            .filter(truthy)
            .unwrap_or_else(|| json!({}));
        let Value::Object(open_pr) = open_pr else {
            return Err(AgentError::Crash(format!(
                "AttributeError: '{}' object has no attribute 'get'",
                worlds::py_type(&open_pr)
            )));
        };
        let s = |v: &Value| {
            if truthy(v) {
                crate::types::py_str(v)
            } else {
                String::new()
            }
        };
        json!({
            "what": s(get(pend.as_object().unwrap_or(&Obj::new()), "what")),
            "error": s(get(pend.as_object().unwrap_or(&Obj::new()), "error")),
            "pr": get(&open_pr, "number"),
            "url": s(get(&open_pr, "url")),
        })
    } else {
        Value::Null
    };
    let library: Vec<Value> = view
        .library
        .iter()
        .take(100)
        .cloned()
        .map(Value::Object)
        .collect();
    let deleted: Vec<Value> = view
        .state
        .deleted
        .iter()
        .take(100)
        .map(|d| get(d, "name").clone())
        .collect();
    Ok(json!({
        "active": view.active,
        "since": view.state.since,
        "data_version": view.state.data_version,
        "archives": archives,
        "library": library,
        "deleted": deleted,
        "archive_bytes": total,
        "storage_note": total > worlds::STORAGE_NOTE_BYTES,
        "pending": pending,
    }))
}
