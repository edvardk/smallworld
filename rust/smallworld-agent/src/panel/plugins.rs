//! mc_panel.py `read_lock`, `read_disabled`, `write_disabled`, `read_snapshots` (owner: actions; `apply_plugins`
//! is not the agent's: it runs as minecraft in mc-plugins-apply.service and stays Python).

use super::{JAR, PLUGIN_NAME, SNAPSHOT_ID};
use crate::types::{AResult, AgentError, Obj, Value, get, py_iter, py_str};
use serde_json::json;
use smallworld_core::pyjson::{dumps_sorted, read_json};
use smallworld_core::pystr::prefix_cp;
use std::collections::BTreeSet;
use std::path::Path;

/// One entry of the deployed plugins.lock.yml (written as JSON by the plugins role).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LockedPlugin {
    pub name: String,
    pub filename: String,
    pub version: String,
}

impl LockedPlugin {
    pub fn to_json(&self) -> Obj {
        let mut o = Obj::new();
        o.insert("name".into(), json!(self.name));
        o.insert("filename".into(), json!(self.filename));
        o.insert("version".into(), json!(self.version));
        o
    }
}

/// `read_lock(path)`.
///
/// # Errors
/// `Crash(KeyError)` where Python's `p["name"]` would raise (an entry with no `name` key but a valid filename:
/// `str(p.get("name"))` is `"None"`, which passes the name check).
pub fn read_lock(path: &Path) -> AResult<Vec<LockedPlugin>> {
    let data = read_json(path).unwrap_or_else(|| json!([]));
    let mut out = Vec::new();
    let Value::Array(items) = data else {
        return Ok(out);
    };
    for p in &items {
        let Value::Object(p) = p else { continue };
        if !(PLUGIN_NAME.is_match(&py_str(get(p, "name")))
            && JAR.is_match(&py_str(get(p, "filename"))))
        {
            continue;
        }
        let name = p
            .get("name")
            .ok_or_else(|| AgentError::Crash("KeyError: 'name'".into()))?;
        let filename = p
            .get("filename")
            .ok_or_else(|| AgentError::Crash("KeyError: 'filename'".into()))?;
        out.push(LockedPlugin {
            name: py_str(name),
            filename: py_str(filename),
            version: p.get("version").map_or_else(String::new, py_str),
        });
    }
    Ok(out)
}

/// `read_disabled(path)`: sorted, deduplicated, valid plugin names.
///
/// # Errors
/// `Crash(TypeError)` when `disabled` isn't iterable (a number, a bool, null).
pub fn read_disabled(path: &Path) -> AResult<Vec<String>> {
    let data = read_json(path).unwrap_or_else(|| json!({}));
    let names = match &data {
        Value::Object(d) => d.get("disabled").cloned().unwrap_or_else(|| json!([])),
        _ => json!([]),
    };
    let set: BTreeSet<String> = py_iter(&names)?
        .into_iter()
        .filter_map(|n| {
            n.as_str()
                .filter(|s| PLUGIN_NAME.is_match(s))
                .map(str::to_string)
        })
        .collect();
    Ok(set.into_iter().collect())
}

/// `write_disabled(names, login, path)`: `{"disabled": sorted(set(names)), "by": login[:200], "at": <UTC ISO>}`,
/// mode 0644 (the apply unit runs as minecraft and reads it).
///
/// # Errors
/// The `OSError` of `atomic_write`.
pub fn write_disabled(names: &BTreeSet<String>, login: &str, path: &Path, now: f64) -> AResult<()> {
    let record = json!({
        "disabled": names.iter().collect::<Vec<_>>(),
        "by": prefix_cp(login, 200),
        "at": crate::sys::utc_iso(now),
    });
    smallworld_core::fsx::atomic_write(path, (dumps_sorted(&record) + "\n").as_bytes(), 0o644)?;
    Ok(())
}

/// `read_snapshots(path)`: restic's snapshot list as backup.py saved it (newest first, at most 200).
///
/// # Errors
/// `Crash(TypeError)` where Python's `snaps[:200]` would raise (a dict, a number).
pub fn read_snapshots(path: &Path) -> AResult<Vec<Obj>> {
    let data = read_json(path).unwrap_or_else(|| json!({}));
    let snaps = match &data {
        Value::Object(d) => d.get("snapshots").cloned().unwrap_or_else(|| json!([])),
        _ => json!([]),
    };
    let items: Vec<Value> = match snaps {
        Value::Array(a) => a.into_iter().take(200).collect(),
        Value::String(_) => Vec::new(), // characters: never dicts
        Value::Object(_) => {
            return Err(AgentError::Crash(
                "TypeError: unhashable type: 'slice'".into(),
            ));
        }
        other => {
            return Err(AgentError::Crash(format!(
                "TypeError: '{}' object is not subscriptable",
                crate::worlds::py_type(&other)
            )));
        }
    };
    let mut out = Vec::new();
    for s in items {
        let Value::Object(s) = s else { continue };
        let id = s.get("id").cloned().unwrap_or_else(|| json!(""));
        if !SNAPSHOT_ID.is_match(&py_str(&id)) {
            continue;
        }
        let mut o = Obj::new();
        for k in ["id", "short_id", "time", "tags", "host", "size"] {
            if let Some(v) = s.get(k) {
                o.insert(k.into(), v.clone());
            }
        }
        out.push(o);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str, text: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("swagent-plugins-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let p = d.join(name);
        std::fs::write(&p, text).unwrap();
        p
    }

    #[test]
    fn read_lock_like_python() {
        let p = tmp(
            "lock.json",
            r#"[{"name": "chunky", "filename": "Chunky-1.4.40.jar", "version": "1.4.40"},
            {"name": "-x", "filename": "a.jar"}, {"name": "b", "filename": "b.zip"}, {"name": null, "filename": "n.jar"}, 5]"#,
        );
        let l = read_lock(&p).unwrap();
        assert_eq!(l.len(), 2);
        assert_eq!(l[1].name, "None");
        assert_eq!(l[1].version, "");
        let bad = tmp("lock2.json", r#"[{"filename": "n.jar"}]"#);
        assert_eq!(
            read_lock(&bad).unwrap_err(),
            AgentError::Crash("KeyError: 'name'".into())
        );
        assert!(read_lock(Path::new("/nonexistent/x")).unwrap().is_empty());
    }

    #[test]
    fn read_disabled_iterates_like_python() {
        assert_eq!(
            read_disabled(&tmp("d1.json", r#"{"disabled": ["b", "a", "b", 3, "-x"]}"#)).unwrap(),
            ["a", "b"]
        );
        assert_eq!(
            read_disabled(&tmp("d2.json", r#"{"disabled": "ab"}"#)).unwrap(),
            ["a", "b"]
        );
        assert!(read_disabled(&tmp("d3.json", r#"{"disabled": 5}"#)).is_err());
        assert!(read_disabled(&tmp("d4.json", "[]")).unwrap().is_empty());
    }

    #[test]
    fn write_disabled_like_python() {
        let p = tmp("w.json", "");
        let names: BTreeSet<String> = ["z".to_string(), "a".to_string()].into_iter().collect();
        write_disabled(&names, "mcctl:ed", &p, 0.0).unwrap();
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            "{\"at\": \"1970-01-01T00:00:00Z\", \"by\": \"mcctl:ed\", \"disabled\": [\"a\", \"z\"]}\n"
        );
    }
}
