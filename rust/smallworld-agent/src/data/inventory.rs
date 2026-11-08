//! `read_json_obj`, `read_json_list`, `_clip`, `collect_inventory`, `push_inventory`, `Agent.panel_state`
//! (mc_agent.py 871-937, 1107, 2405-2431). The server dir is minecraft's: every file is read with
//! `read_untrusted` (no FIFO, no symlink, capped).

use super::read_untrusted;
use crate::actions::Panel;
use crate::link::{IngestError, Link};
use crate::types::{AResult, AgentError, Obj, VERSION, Value, get, truthy};
use serde_json::json;
use smallworld_core::pystr::prefix_cp;
use std::collections::HashSet;
use std::path::Path;

/// mc_agent.py `INVENTORY_MAX`.
pub const INVENTORY_MAX: usize = 1000;
/// mc_panel.py `PLAYERS_STATE_FILE`.
pub const PLAYERS_STATE_FILE: &str = "whitelist-managed.json";

fn read_untrusted_json(path: &Path) -> Option<Value> {
    let data = read_untrusted(path, super::UNTRUSTED_MAX_BYTES, false).ok()?;
    // json.loads(bytes) decodes UTF-8 (a BOM or invalid UTF-8 is a ValueError: the default)
    let data = data.strip_prefix(b"\xef\xbb\xbf").unwrap_or(&data);
    let text = std::str::from_utf8(data).ok()?;
    smallworld_core::pyjson::loads(text)
}

/// `read_json_obj(path)`: a JSON object, `{}` otherwise.
pub fn read_json_obj(path: &Path) -> Obj {
    match read_untrusted_json(path) {
        Some(Value::Object(o)) => o,
        _ => Obj::new(),
    }
}

/// `read_json_list(path)`: the objects of a JSON list (at most `INVENTORY_MAX`), `[]` otherwise.
pub fn read_json_list(path: &Path) -> Vec<Obj> {
    match read_untrusted_json(path) {
        Some(Value::Array(items)) => items
            .into_iter()
            .filter_map(|v| match v {
                Value::Object(o) => Some(o),
                _ => None,
            })
            .take(INVENTORY_MAX)
            .collect(),
        _ => Vec::new(),
    }
}

/// `_clip(entry, keys)`: ints and bools as they are, strings cut to 200 characters, the rest dropped.
pub fn clip(entry: &Obj, keys: &[&str]) -> Obj {
    let mut out = Obj::new();
    for k in keys {
        match entry.get(*k) {
            Some(v @ Value::Bool(_)) => {
                out.insert((*k).into(), v.clone());
            }
            Some(Value::Number(n)) if n.is_i64() || n.is_u64() => {
                out.insert((*k).into(), Value::Number(n.clone()));
            }
            Some(Value::String(s)) => {
                out.insert((*k).into(), json!(prefix_cp(s, 200)));
            }
            _ => {}
        }
    }
    out
}

/// `Path(name).stem`.
fn stem(name: &str) -> &str {
    match name.rfind('.') {
        Some(i) if i > 0 && i < name.len() - 1 => &name[..i],
        _ => name,
    }
}

/// The `*.jar` names in a directory, sorted (pathlib's glob: hidden names too), at most `INVENTORY_MAX`.
fn jars(dir: &Path) -> Vec<String> {
    let Ok(rd) = std::fs::read_dir(dir) else {
        return Vec::new();
    };
    let mut names: Vec<String> = rd
        .filter_map(Result::ok)
        .map(|e| e.file_name().to_string_lossy().into_owned())
        // pathlib's glob is case-sensitive on Linux too
        .filter(|n| {
            #[allow(clippy::case_sensitive_file_extension_comparisons)]
            let jar = n.ends_with(".jar");
            jar
        })
        .collect();
    names.sort();
    names.truncate(INVENTORY_MAX);
    names
}

/// `collect_inventory(server_dir)`: whitelist, ops, bans, plugin jars.
pub fn collect_inventory(server_dir: Option<&Path>) -> Value {
    let default = super::server_dir();
    let sdir = server_dir.unwrap_or(&default);
    let mut plugins = Vec::new();
    for (sub, enabled) in [("plugins", true), ("plugins/disabled", false)] {
        for name in jars(&sdir.join(sub)) {
            plugins.push(json!({
                "file": prefix_cp(&name, 200),
                "name": prefix_cp(stem(&name), 100),
                "enabled": enabled,
            }));
        }
    }
    let kept: HashSet<String> = match read_untrusted_json(&sdir.join(PLAYERS_STATE_FILE)) {
        Some(Value::Object(g)) => match g.get("kept") {
            Some(Value::Array(l)) => l
                .iter()
                .filter_map(|u| u.as_str().map(str::to_string))
                .collect(),
            _ => HashSet::new(),
        },
        _ => HashSet::new(),
    };
    let whitelist: Vec<Value> = read_json_list(&sdir.join("whitelist.json"))
        .iter()
        .map(|e| {
            let mut item = clip(e, &["name", "uuid"]);
            if let Some(Value::String(u)) = e.get("uuid")
                && kept.contains(&u.to_lowercase())
            {
                item.insert("unmanaged".into(), Value::Bool(true));
            }
            Value::Object(item)
        })
        .collect();
    let list = |file: &str, keys: &[&str]| -> Vec<Value> {
        read_json_list(&sdir.join(file))
            .iter()
            .map(|e| Value::Object(clip(e, keys)))
            .collect()
    };
    #[allow(clippy::cast_possible_truncation)]
    let ts = (crate::sys::now() * 1000.0).trunc() as i64;
    json!({
        "ts": ts,
        "whitelist": whitelist,
        "ops": list("ops.json", &["name", "uuid", "level"]),
        "bans": list("banned-players.json", &["name", "uuid", "created", "source", "expires", "reason"]),
        "ip_bans": list("banned-ips.json", &["ip", "created", "source", "expires", "reason"]),
        "plugins": plugins,
    })
}

/// `push_inventory(s, server_dir, extra)`: the inventory with `extra` merged over it.
///
/// # Errors
/// The `IngestError`.
pub fn push_inventory(
    link: &Link,
    server_dir: Option<&Path>,
    extra: &Obj,
) -> Result<(), IngestError> {
    let mut inventory = match collect_inventory(server_dir) {
        Value::Object(o) => o,
        _ => Obj::new(),
    };
    for (k, v) in extra {
        inventory.insert(k.clone(), v.clone());
    }
    link.post_json(
        "/ingest/inventory",
        &json!({"agent": VERSION, "inventory": inventory}),
    )
    .map(|_| ())
}

fn type_error(msg: String) -> AgentError {
    AgentError::Crash(format!("TypeError: {msg}"))
}

/// `Agent.panel_state()`: pending whitelist changes and their PR, rejected logins, the plugin lock and what is
/// disabled, the snapshots, the current job, worlds, product and self-test (`product`, `selftest`: misc's).
///
/// # Errors
/// What Python raises on hand-broken state files (`Crash`).
pub fn panel_state(
    p: &Panel,
    github_configured: bool,
    product: Value,
    selftest: Value,
) -> AResult<Obj> {
    let pend = p.pending.load()?;
    let entries = match get(&pend, "entries") {
        Value::Array(a) => a.clone(),
        other => crate::types::py_iter(other)?,
    };
    let mut pending = Vec::new();
    for e in &entries {
        let Value::Object(e) = e else {
            return Err(AgentError::Crash(format!(
                "AttributeError: '{}' object has no attribute 'get'",
                crate::worlds::py_type(e)
            )));
        };
        let mut o = Obj::new();
        for k in ["uuid", "name", "op", "ts", "login", "pr"] {
            o.insert(k.into(), get(e, k).clone());
        }
        pending.push(Value::Object(o));
    }
    let pr = get(&pend, "pr");
    let expired = match pend.get("expired") {
        None => json!([]),
        Some(Value::Array(a)) => json!(a[a.len().saturating_sub(10)..]),
        Some(Value::String(s)) => {
            let n = smallworld_core::pystr::len_cp(s);
            json!(smallworld_core::pystr::slice_cp(
                s,
                n.saturating_sub(10),
                None
            ))
        }
        Some(other) => {
            return Err(type_error(format!(
                "'{}' object is not subscriptable",
                crate::worlds::py_type(other)
            )));
        }
    };
    let mut snapshots = Vec::new();
    for s in crate::panel::plugins::read_snapshots(p.snapshots_path())? {
        let tags = get(&s, "tags");
        let items = if truthy(tags) {
            crate::types::py_iter(tags)?
        } else {
            Vec::new()
        };
        let mut parts = Vec::new();
        for (i, t) in items.iter().enumerate() {
            match t {
                Value::String(t) => parts.push(t.clone()),
                other => {
                    return Err(type_error(format!(
                        "sequence item {i}: expected str instance, {} found",
                        crate::worlds::py_type(other)
                    )));
                }
            }
        }
        let mut s = s.clone();
        s.insert("tags".into(), json!(parts.join(",")));
        snapshots.push(Value::Object(s));
    }
    let lock: Vec<Value> = crate::panel::plugins::read_lock(p.plugins_lock_path())?
        .iter()
        .map(|l| Value::Object(l.to_json()))
        .collect();
    let mut out = Obj::new();
    out.insert("pending".into(), Value::Array(pending));
    out.insert(
        "whitelist_sync".into(),
        json!({
            "configured": github_configured,
            "pr": if truthy(pr) { pr.clone() } else { Value::Null },
            "error": pend.get("error").cloned().unwrap_or_else(|| json!("")),
            "expired": expired,
        }),
    );
    out.insert(
        "rejected".into(),
        Value::Array(
            p.rejected
                .entries()
                .into_iter()
                .map(Value::Object)
                .collect(),
        ),
    );
    out.insert("plugins_lock".into(), Value::Array(lock));
    out.insert(
        "plugins_disabled".into(),
        json!(crate::panel::plugins::read_disabled(
            p.plugins_disabled_path()
        )?),
    );
    out.insert("snapshots".into(), Value::Array(snapshots));
    out.insert(
        "job".into(),
        p.jobs.current().map_or(Value::Null, |j| j.public()),
    );
    out.insert("worlds".into(), crate::actions::world::worlds_inventory(p)?);
    out.insert("product".into(), product);
    out.insert("selftest".into(), selftest);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("sw-inv-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn inventory_from_server_files() {
        let d = tmp("files");
        std::fs::create_dir_all(d.join("plugins/disabled")).unwrap();
        for f in [
            "plugins/spark-1.10.jar",
            "plugins/Chunky-1.4.40.jar",
            "plugins/readme.txt",
            "plugins/disabled/x.y.jar",
            "plugins/.jar",
        ] {
            std::fs::write(d.join(f), "jar").unwrap();
        }
        std::fs::write(
            d.join("whitelist.json"),
            r#"[{"uuid":"22222222-2222-4222-8222-222222222222","name":"Bob"},{"uuid":"AB","name":"x"},3,{"name":1.5}]"#,
        )
        .unwrap();
        std::fs::write(d.join(PLAYERS_STATE_FILE), r#"{"kept":["ab",3]}"#).unwrap();
        std::fs::write(
            d.join("ops.json"),
            r#"[{"name":"A","level":4,"bypassesPlayerLimit":false}]"#,
        )
        .unwrap();
        std::os::unix::fs::symlink(d.join("ops.json"), d.join("banned-players.json")).unwrap();
        std::fs::write(d.join("banned-ips.json"), "{}").unwrap();
        let inv = collect_inventory(Some(&d));
        assert_eq!(
            inv["plugins"],
            json!([
                {"file": ".jar", "name": ".jar", "enabled": true},
                {"file": "Chunky-1.4.40.jar", "name": "Chunky-1.4.40", "enabled": true},
                {"file": "spark-1.10.jar", "name": "spark-1.10", "enabled": true},
                {"file": "x.y.jar", "name": "x.y", "enabled": false},
            ])
        );
        assert_eq!(
            inv["whitelist"],
            json!([
                {"name": "Bob", "uuid": "22222222-2222-4222-8222-222222222222"},
                {"name": "x", "uuid": "AB", "unmanaged": true},
                {},
            ])
        );
        assert_eq!(inv["ops"], json!([{"name": "A", "level": 4}]));
        assert_eq!(inv["bans"], json!([])); // a symlink is never followed
        assert_eq!(inv["ip_bans"], json!([]));
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn clip_like_python() {
        let e: Obj = serde_json::from_str(&format!(
            r#"{{"a":true,"b":7,"c":"{}","d":null,"e":[1]}}"#,
            "é".repeat(250)
        ))
        .unwrap();
        let c = clip(&e, &["a", "b", "c", "d", "e", "f"]);
        assert_eq!(c["a"], json!(true));
        assert_eq!(c["b"], json!(7));
        assert_eq!(c["c"].as_str().unwrap().chars().count(), 200);
        assert_eq!(c.len(), 3);
    }
}
