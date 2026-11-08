//! The parts of scripts/worlds.py the agent runs in-process to check world actions (the work itself stays in
//! worlds.py behind mc-world.service): library entries (`check_entry`), the deploy's library (`read_library`),
//! the archive state (`load_state`), `version_check`, `tag_value`. Owner: actions (world_*); sync uses
//! `set_world_section`/`library_line` (not ported yet: sync's job).

use crate::types::{
    AResult, AgentError, Obj, Value, as_int, as_py_int_incl_bool, get, py_iter, py_str, truthy,
};
use regex::Regex;
use serde_json::json;
use smallworld_core::pyjson::read_json;
use std::path::Path;
use std::sync::LazyLock;

/// worlds.py `WORLD_NAME` (= mcctl.schema WORLD_NAME).
pub static WORLD_NAME: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-z0-9][a-z0-9_-]{0,31}$").unwrap());
/// worlds.py `SEED` (= mcctl.schema SEED; "" = random).
pub static SEED: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(?:-?[0-9]{1,19})?$").unwrap());
/// worlds.py `ARCHIVE_TAG`.
pub const ARCHIVE_TAG: &str = "world-archive";
/// worlds.py `STORAGE_NOTE_BYTES` (R2's free storage).
pub const STORAGE_NOTE_BYTES: i64 = 10_000_000_000;
/// worlds.py `TYPES`' keys, in order (the messages list them).
pub const TYPES: [&str; 7] = [
    "default",
    "large_biomes",
    "amplified",
    "single_biome",
    "flat_classic",
    "flat_void",
    "flat_redstone",
];
/// worlds.py `PRESETS`' keys, in order.
pub const PRESETS: [&str; 5] = [
    "survival_smp",
    "creative_build",
    "hardcore",
    "farm_test",
    "peaceful",
];
/// worlds.py `DIMENSIONS`.
pub const DIMENSIONS: [&str; 3] = [
    "minecraft:overworld",
    "minecraft:the_nether",
    "minecraft:the_end",
];

/// `WORLD_NAME.fullmatch(s)`.
pub fn is_world_name(s: &str) -> bool {
    WORLD_NAME.is_match(s)
}

/// Python's `x in some_dict` for a JSON value: unhashable values (lists, dicts) raise `TypeError`.
fn hashable_in(v: &Value, keys: &[&str]) -> AResult<bool> {
    match v {
        Value::Array(_) => Err(AgentError::Crash(
            "TypeError: unhashable type: 'list'".into(),
        )),
        Value::Object(_) => Err(AgentError::Crash(
            "TypeError: unhashable type: 'dict'".into(),
        )),
        Value::String(s) => Ok(keys.contains(&s.as_str())),
        _ => Ok(false),
    }
}

/// worlds.py `WorldError`'s message, or a crash Python wouldn't catch.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EntryError {
    World(String),
    Crash(AgentError),
}

impl From<AgentError> for EntryError {
    fn from(e: AgentError) -> Self {
        EntryError::Crash(e)
    }
}

/// worlds.py `check_entry(raw)`: a library entry `{name, seed, type, preset, border_radius, pregen}`.
///
/// # Errors
/// `EntryError::World` with worlds.py's text; `Crash` where Python raises something else (`TypeError`).
pub fn check_entry(raw: &Value) -> Result<Obj, EntryError> {
    let world = |m: &str| Err(EntryError::World(m.into()));
    let Value::Object(raw) = raw else {
        return world("a world entry must be an object");
    };
    let name = get(raw, "name");
    let seed = raw.get("seed").cloned().unwrap_or_else(|| json!(""));
    let Some(name) = name.as_str().filter(|n| is_world_name(n)) else {
        return world(
            "a world name is 1-32 lowercase letters, digits, _ or - (starting with a letter or digit)",
        );
    };
    let Some(seed) = seed.as_str().filter(|s| SEED.is_match(s)) else {
        return world("the seed must be a whole number (or empty for a random one)");
    };
    let wtype = raw.get("type").cloned().unwrap_or_else(|| json!("default"));
    let preset = get(raw, "preset").clone();
    if !hashable_in(&wtype, &TYPES)? {
        return world(&format!("type must be one of {}", TYPES.join(", ")));
    }
    if !preset.is_null() && !hashable_in(&preset, &PRESETS)? {
        return world(&format!("preset must be one of {}", PRESETS.join(", ")));
    }
    let border = get(raw, "border_radius");
    let border_out = if border.is_null() {
        Value::Null
    } else {
        let Value::Object(b) = border else {
            return world("border_radius maps minecraft:overworld/the_nether/the_end to a radius");
        };
        if !b.keys().all(|k| DIMENSIONS.contains(&k.as_str())) {
            return world("border_radius maps minecraft:overworld/the_nether/the_end to a radius");
        }
        for v in b.values() {
            if !v.is_null() && !as_int(v).is_some_and(|r| (0..=29_999_984).contains(&r)) {
                return world("a border radius is a whole number of blocks (0-29999984) or null");
            }
        }
        Value::Object(
            DIMENSIONS
                .iter()
                .map(|d| ((*d).to_string(), get(b, d).clone()))
                .collect(),
        )
    };
    let pregen = get(raw, "pregen");
    if !pregen.is_null() && !pregen.is_boolean() {
        return world("pregen must be true, false or null");
    }
    let mut out = Obj::new();
    out.insert("name".into(), json!(name));
    out.insert("seed".into(), json!(seed));
    out.insert("type".into(), wtype);
    out.insert("preset".into(), preset);
    out.insert("border_radius".into(), border_out);
    out.insert("pregen".into(), pregen.clone());
    Ok(out)
}

/// worlds.py `read_library(path)`.
#[derive(Debug, Clone, PartialEq)]
pub struct Library {
    pub active: Option<String>,
    pub library: Vec<Obj>,
}

/// `read_library(path)`: the deploy's view (server.yml world.active/library with the panel's pending change).
pub fn read_library(path: &Path) -> AResult<Library> {
    let data = read_json(path).unwrap_or_else(|| json!({}));
    let mut entries = Vec::new();
    if let Value::Object(d) = &data
        && let Some(items) = d.get("library")
    {
        for raw in py_iter(items)? {
            match check_entry(&raw) {
                Ok(e) => entries.push(e),
                Err(EntryError::World(_)) => {}
                Err(EntryError::Crash(c)) => return Err(c),
            }
        }
    }
    let active = match &data {
        Value::Object(d) => d
            .get("active")
            .and_then(Value::as_str)
            .filter(|a| is_world_name(a))
            .map(str::to_string),
        _ => None,
    };
    Ok(Library {
        active,
        library: entries,
    })
}

/// worlds.py `load_state(path)`: /var/lib/mc-snapshots/worlds.json, checked.
#[derive(Debug, Clone, PartialEq)]
pub struct WorldsState {
    pub active: Option<String>,
    pub since: String,
    /// An int (or a bool: Python's `isinstance(x, int)` accepts both) or Null.
    pub data_version: Value,
    pub archives: Vec<Obj>,
    pub deleted: Vec<Obj>,
    pub updated: String,
}

/// `str(x or "")`.
fn str_or_empty(v: &Value) -> String {
    if truthy(v) { py_str(v) } else { String::new() }
}

/// `x or []` iterated, keeping only dicts (`[a for a in x or [] if isinstance(a, dict)]`).
fn dicts_of(v: &Value) -> AResult<Vec<Obj>> {
    if !truthy(v) {
        return Ok(Vec::new());
    }
    Ok(py_iter(v)?
        .into_iter()
        .filter_map(|a| a.as_object().cloned())
        .collect())
}

/// `load_state(path)`.
pub fn load_state(path: &Path) -> AResult<WorldsState> {
    let data = read_json(path).unwrap_or_else(|| json!({}));
    let empty = Obj::new();
    let d = data.as_object().unwrap_or(&empty);
    let active = d
        .get("active")
        .and_then(Value::as_str)
        .filter(|a| is_world_name(a))
        .map(str::to_string);
    let deleted = dicts_of(get(d, "deleted"))?
        .into_iter()
        .filter(|x| is_world_name(&py_str(get(x, "name"))))
        .collect();
    let dv = get(d, "data_version");
    Ok(WorldsState {
        active,
        since: str_or_empty(get(d, "since")),
        data_version: if as_py_int_incl_bool(dv).is_some() {
            dv.clone()
        } else {
            Value::Null
        },
        archives: dicts_of(get(d, "archives"))?,
        deleted,
        updated: str_or_empty(get(d, "updated")),
    })
}

/// worlds.py `tag_value(tags, prefix)`: the rest of the first tag that starts with `prefix`.
pub fn tag_value<'a>(tags: &'a [String], prefix: &str) -> Option<&'a str> {
    tags.iter().find_map(|t| t.strip_prefix(prefix))
}

/// A number as Python compares it (`int`, `float`, `bool`), or None for other types.
fn py_number(v: &Value) -> Option<f64> {
    match v {
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        Value::Number(n) => n.as_f64(),
        _ => None,
    }
}

/// worlds.py `version_check(target_dv, current_dv)`: "" when fine, else why a switch is refused.
///
/// # Errors
/// `Crash(TypeError)` where Python's `>` would raise (a string archive version against an int).
pub fn version_check(target: &Value, current: &Value) -> AResult<String> {
    if target.is_null() || current.is_null() {
        return Ok(String::new());
    }
    let newer = match (py_number(target), py_number(current), target, current) {
        (Some(t), Some(c), _, _) => t > c,
        (_, _, Value::String(t), Value::String(c)) => t > c,
        _ => {
            return Err(AgentError::Crash(format!(
                "TypeError: '>' not supported between instances of '{}' and '{}'",
                py_type(target),
                py_type(current)
            )));
        }
    };
    if !newer {
        return Ok(String::new());
    }
    Ok(format!(
        "the archive was saved by a newer Minecraft (DataVersion {}) than this server runs ({}); a server never \
         loads a newer world",
        py_str(target),
        py_str(current)
    ))
}

/// `type(x).__name__` for a value from `json.loads`.
pub fn py_type(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.is_f64() => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn world_err(v: Value) -> String {
        match check_entry(&v) {
            Err(EntryError::World(m)) => m,
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn check_entry_like_python() {
        let e = check_entry(&json!({"name": "main"})).unwrap();
        assert_eq!(
            Value::Object(e),
            json!({"name": "main", "seed": "", "type": "default", "preset": null, "border_radius": null, "pregen": null})
        );
        let e = check_entry(&json!({"name": "a", "seed": "-12", "border_radius": {"minecraft:overworld": 5}, "pregen": true}))
            .unwrap();
        assert_eq!(
            e["border_radius"],
            json!({"minecraft:overworld": 5, "minecraft:the_nether": null, "minecraft:the_end": null})
        );
        assert_eq!(world_err(json!([])), "a world entry must be an object");
        assert!(world_err(json!({"name": "Main"})).starts_with("a world name is"));
        assert!(world_err(json!({"name": "a", "seed": "1.5"})).starts_with("the seed"));
        assert_eq!(
            world_err(json!({"name": "a", "type": "x"})),
            format!("type must be one of {}", TYPES.join(", "))
        );
        assert_eq!(
            world_err(json!({"name": "a", "border_radius": {"x": 1}})),
            "border_radius maps minecraft:overworld/the_nether/the_end to a radius"
        );
        assert!(
            world_err(json!({"name": "a", "border_radius": {"minecraft:the_end": true}}))
                .starts_with("a border radius")
        );
        assert!(
            world_err(json!({"name": "a", "border_radius": {"minecraft:the_end": 1.0}}))
                .starts_with("a border radius")
        );
        assert_eq!(
            world_err(json!({"name": "a", "pregen": 1})),
            "pregen must be true, false or null"
        );
        assert!(matches!(
            check_entry(&json!({"name": "a", "type": []})),
            Err(EntryError::Crash(_))
        ));
    }

    #[test]
    fn version_check_like_python() {
        assert_eq!(version_check(&json!(4000), &json!(4671)).unwrap(), "");
        assert!(
            version_check(&json!(5000), &json!(4671))
                .unwrap()
                .contains("DataVersion 5000) than this server runs (4671)")
        );
        assert_eq!(version_check(&Value::Null, &json!(1)).unwrap(), "");
        assert!(
            version_check(&json!(2.5), &json!(true))
                .unwrap()
                .contains("(DataVersion 2.5) than this server runs (True)")
        );
        assert!(version_check(&json!("x"), &json!(1)).is_err());
    }

    #[test]
    fn load_state_filters_like_python() {
        let d = std::env::temp_dir().join(format!("swagent-worlds-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let p = d.join("worlds.json");
        std::fs::write(
            &p,
            r#"{"active": "Bad", "since": 5, "data_version": true, "archives": [1, {"name": "a"}],
            "deleted": [{"name": 123}, {"name": null}, "x"]}"#,
        )
        .unwrap();
        let s = load_state(&p).unwrap();
        assert_eq!(s.active, None);
        assert_eq!(s.since, "5");
        assert_eq!(s.data_version, json!(true));
        assert_eq!(s.archives.len(), 1);
        assert_eq!(s.deleted.len(), 1);
        std::fs::write(&p, r#"{"archives": 5}"#).unwrap();
        assert!(load_state(&p).is_err()); // TypeError: 'int' object is not iterable
        std::fs::remove_dir_all(d).unwrap();
        assert_eq!(
            load_state(Path::new("/nonexistent/w.json"))
                .unwrap()
                .archives
                .len(),
            0
        );
    }
}
