//! mc_panel.py `Pending`: pending.json in the agent's state dir, `{"entries": [{uuid, name, op, ts, login, pr}],
//! "pr", "error", "changed", "expired", "world"}`. Two processes write it (the daemon and the cli), so every
//! read-modify-write holds pending.json.lock (flock, interlocking with Python's) and reads the file again under
//! it; writes are atomic (tmp + rename, 0600, `json.dumps(sort_keys=True, indent=1)`). Nobody holds the lock
//! across a network call (sync works on a snapshot and merges back: `merge_sync`, owner sync).
//!
//! Owner: sync (the round, `merge_sync`); `record` and `record_world` are ported (the cli's world actions and
//! the panel's whitelist action use them).

use super::UUID;
use crate::types::{AResult, Obj, Value, get, py_float, py_iter, py_str, truthy};
use serde_json::json;
use smallworld_core::flock::ProcessFileLock;
use smallworld_core::pyjson::{Dumps, dumps, dumps_sorted, read_json};
use smallworld_core::pystr::prefix_cp;
use std::path::{Path, PathBuf};

pub struct Pending {
    path: PathBuf,
    lock: ProcessFileLock,
}

impl Pending {
    /// `Pending(path)`: the lock file is `<path>.lock` next to it.
    pub fn new(path: impl Into<PathBuf>) -> Self {
        let path = path.into();
        let mut lock = path.clone().into_os_string();
        lock.push(".lock");
        Self {
            path,
            lock: ProcessFileLock::new(PathBuf::from(lock)),
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// `load()`: a consistent read without the lock (writes are atomic renames), normalised to the six keys.
    ///
    /// # Errors
    /// What Python's `load` would raise on a hand-broken file: `entries: null` (`TypeError`), a `changed` that
    /// isn't a number (`ValueError`/`TypeError`).
    pub fn load(&self) -> AResult<Obj> {
        let data = read_json(&self.path).unwrap_or_else(|| json!({}));
        let empty = Obj::new();
        let d = data.as_object().unwrap_or(&empty);
        let mut entries = Vec::new();
        for e in py_iter(d.get("entries").unwrap_or(&json!([])))? {
            if let Value::Object(o) = &e
                && UUID.is_match(&py_str(get(o, "uuid")))
            {
                entries.push(e);
            }
        }
        let changed = get(d, "changed");
        let changed = if truthy(changed) {
            py_float(changed)?
        } else {
            0.0
        };
        let error = get(d, "error");
        let expired = get(d, "expired");
        let world = get(d, "world");
        let mut out = Obj::new();
        out.insert("entries".into(), Value::Array(entries));
        out.insert("pr".into(), get(d, "pr").clone());
        out.insert(
            "error".into(),
            json!(if truthy(error) {
                py_str(error)
            } else {
                String::new()
            }),
        );
        out.insert("changed".into(), json!(changed));
        out.insert(
            "expired".into(),
            if truthy(expired) {
                expired.clone()
            } else {
                json!([])
            },
        );
        out.insert(
            "world".into(),
            if world.is_object() {
                world.clone()
            } else {
                Value::Null
            },
        );
        Ok(out)
    }

    /// `with pending.locked(): snap = pending.load()`: the sync round's snapshot (the lock is released before
    /// any network call; the result is merged back with `update` + `sync::round::merge_sync`).
    ///
    /// # Errors
    /// `load`'s.
    pub fn snapshot(&self) -> AResult<Obj> {
        let _held = self.lock.lock();
        self.load()
    }

    /// `save(data)`: only under the lock (`update` does both).
    ///
    /// # Errors
    /// The `OSError` of the atomic write.
    pub fn save(&self, data: &Obj) -> AResult<()> {
        let text = dumps(&Value::Object(data.clone()), &Dumps::indented(1));
        smallworld_core::fsx::atomic_write(&self.path, text.as_bytes(), 0o600)?;
        Ok(())
    }

    /// `with update() as data:` — pending.json as it is now (read under the lock), saved if `f` changed it.
    ///
    /// # Errors
    /// `load`'s and `save`'s errors (the lock is released either way).
    pub fn update<R>(&self, f: impl FnOnce(&mut Obj) -> R) -> AResult<R> {
        let _held = self.lock.lock();
        let mut data = self.load()?;
        let before = dumps_sorted(&Value::Object(data.clone()));
        let r = f(&mut data);
        if dumps_sorted(&Value::Object(data.clone())) != before {
            self.save(&data)?;
        }
        Ok(r)
    }

    /// `record_world(active, library, what, login, now)`: the world library as it now is on the server. The
    /// latest state wins; it stays until a pull request with it is merged (it never expires).
    ///
    /// # Errors
    /// `update`'s.
    pub fn record_world(
        &self,
        active: &Value,
        library: Vec<Obj>,
        what: &str,
        login: &str,
        now: f64,
    ) -> AResult<()> {
        self.update(|data| {
            let prev = data
                .get("world")
                .cloned()
                .filter(truthy)
                .unwrap_or_else(|| json!({}));
            let open_pr = prev.get("open_pr").cloned().unwrap_or(Value::Null);
            let world = json!({
                "active": active,
                "library": library,
                "what": prefix_cp(what, 200),
                "ts": now,
                "login": prefix_cp(login, 200),
                "pr": null,
                "open_pr": open_pr,
                "error": "",
            });
            data.insert("world".into(), world);
            if !truthy(get(data, "changed")) {
                data.insert("changed".into(), json!(now));
            }
        })
    }

    /// `record(op, name, uuid, login, now)`: the latest change per player wins (one in an open PR is replaced).
    ///
    /// # Errors
    /// `update`'s.
    pub fn record(&self, op: &str, name: &str, uuid: &str, login: &str, now: f64) -> AResult<()> {
        self.update(|data| {
            let mut entries: Vec<Value> = data
                .get("entries")
                .and_then(Value::as_array)
                .map(|a| a.iter().filter(|e| e.get("uuid").and_then(Value::as_str) != Some(uuid)).cloned().collect())
                .unwrap_or_default();
            entries.push(json!({"uuid": uuid, "name": name, "op": op, "ts": now, "login": login, "pr": null}));
            data.insert("entries".into(), Value::Array(entries));
            data.insert("changed".into(), json!(now));
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::MetadataExt;

    fn dir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("swagent-pending-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn load_normalises_like_python() {
        let d = dir("load");
        let p = Pending::new(d.join("pending.json"));
        assert_eq!(
            Value::Object(p.load().unwrap()),
            json!({"entries": [], "pr": null, "error": "", "changed": 0.0, "expired": [], "world": null})
        );
        std::fs::write(
            p.path(),
            r#"{"entries": [{"uuid": "x"}, {"uuid": "0f0f0f0f-0f0f-0f0f-0f0f-0f0f0f0f0f0f"}, 3],
            "changed": "12.5", "error": 0, "world": [], "junk": 1}"#,
        )
        .unwrap();
        let l = p.load().unwrap();
        assert_eq!(l["entries"].as_array().unwrap().len(), 1);
        assert_eq!(l["changed"], json!(12.5));
        assert_eq!(l["error"], json!(""));
        assert_eq!(l["world"], Value::Null);
        std::fs::write(p.path(), r#"{"entries": null}"#).unwrap();
        assert!(p.load().is_err());
        std::fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn record_world_writes_like_python() {
        let d = dir("rw");
        let p = Pending::new(d.join("pending.json"));
        let lib = vec![json!({"name": "main"}).as_object().unwrap().clone()];
        p.record_world(
            &json!("main"),
            lib,
            "switch to main",
            "mcctl:ed",
            1_700_000_000.5,
        )
        .unwrap();
        let text = std::fs::read_to_string(p.path()).unwrap();
        // json.dumps(..., sort_keys=True, indent=1) of what mc_panel.py writes
        assert_eq!(
            text,
            "{\n \"changed\": 1700000000.5,\n \"entries\": [],\n \"error\": \"\",\n \"expired\": [],\n \"pr\": null,\n \
             \"world\": {\n  \"active\": \"main\",\n  \"error\": \"\",\n  \"library\": [\n   {\n    \"name\": \"main\"\n   }\n  ],\n  \
             \"login\": \"mcctl:ed\",\n  \"open_pr\": null,\n  \"pr\": null,\n  \"ts\": 1700000000.5,\n  \"what\": \"switch to main\"\n }\n}"
        );
        assert_eq!(std::fs::metadata(p.path()).unwrap().mode() & 0o777, 0o600);
        p.record(
            "add",
            "Steve",
            "0f0f0f0f-0f0f-0f0f-0f0f-0f0f0f0f0f0f",
            "panel",
            5.0,
        )
        .unwrap();
        p.record(
            "remove",
            "Steve",
            "0f0f0f0f-0f0f-0f0f-0f0f-0f0f0f0f0f0f",
            "panel",
            6.0,
        )
        .unwrap();
        let l = p.load().unwrap();
        assert_eq!(l["entries"].as_array().unwrap().len(), 1);
        assert_eq!(l["entries"][0]["op"], json!("remove"));
        assert_eq!(l["changed"], json!(6.0));
        std::fs::remove_dir_all(d).unwrap();
    }
}
