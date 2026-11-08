//! Player statistics (telemetry.py 508-555): the few vanilla statistics the panel shows, from
//! `world/stats/<uuid>.json` (minecraft writes them: `read_untrusted`, 4 MiB).

use super::json::{J, OMap, loads_bytes};
use super::snapshot::UUID;
use crate::types::{AResult, AgentError};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

/// telemetry.py `STAT_KEYS`, in its order.
pub const STAT_KEYS: [(&str, &str); 7] = [
    ("play_ticks", "minecraft:play_time"),
    ("deaths", "minecraft:deaths"),
    ("mob_kills", "minecraft:mob_kills"),
    ("walk_cm", "minecraft:walk_one_cm"),
    ("sprint_cm", "minecraft:sprint_one_cm"),
    ("fly_cm", "minecraft:aviate_one_cm"),
    ("jumps", "minecraft:jump"),
];

/// `x.get(k)` on a value Python requires to be a dict when truthy (`(data or {}).get(...)`).
fn get_or_empty<'a>(v: Option<&'a J>, key: &str) -> AResult<Option<&'a J>> {
    match v {
        Some(J::Obj(m)) => Ok(m.get(key)),
        Some(other) if other.truthy() => Err(AgentError::Crash(format!(
            "AttributeError: '{}' object has no attribute 'get'",
            other.py_type()
        ))),
        _ => Ok(None),
    }
}

/// telemetry.py `read_stats(path)`: None when unreadable, not JSON or `minecraft:custom` isn't an object.
///
/// # Errors
/// `Crash` where Python raises an uncaught `AttributeError` (a truthy non-object at `data` or `stats`).
pub fn read_stats(path: &Path) -> AResult<Option<OMap>> {
    let Ok(data) = crate::data::read_untrusted(path, 4 << 20, false) else {
        return Ok(None);
    };
    let Some(data) = loads_bytes(&data) else {
        return Ok(None);
    };
    let stats = get_or_empty(Some(&data), "stats")?;
    let custom = get_or_empty(stats, "minecraft:custom")?;
    let empty = J::Obj(OMap::new());
    let custom = match custom {
        Some(c) if c.truthy() => c,
        _ => &empty,
    };
    let Some(custom) = custom.as_obj() else {
        return Ok(None);
    };
    let mut out = OMap::new();
    for (k, v) in STAT_KEYS {
        match custom.get(v) {
            None => out.insert(k, 0_i64),
            Some(x) => {
                if let Some(n) = x.as_py_int() {
                    out.insert(k, n);
                }
            }
        }
    }
    Ok(Some(out))
}

/// telemetry.py `StatsReader`: the stats files that changed (by mtime) since the last read.
#[derive(Debug)]
pub struct StatsReader {
    pub directory: PathBuf,
    mtimes: HashMap<String, SystemTime>,
}

impl StatsReader {
    pub fn new(directory: impl Into<PathBuf>) -> Self {
        Self {
            directory: directory.into(),
            mtimes: HashMap::new(),
        }
    }

    /// `changed(uuids)`, in the order given.
    ///
    /// # Errors
    /// As `read_stats`.
    pub fn changed(&mut self, uuids: &[String]) -> AResult<OMap> {
        let mut out = OMap::new();
        for u in uuids {
            if !UUID.is_match(u) {
                continue;
            }
            let p = self.directory.join(format!("{u}.json"));
            let Ok(m) = std::fs::metadata(&p).and_then(|m| m.modified()) else {
                continue;
            };
            if self.mtimes.get(u) == Some(&m) {
                continue;
            }
            if let Some(stats) = read_stats(&p)? {
                self.mtimes.insert(u.clone(), m);
                out.insert(u.as_str(), stats);
            }
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stats_like_python() {
        let dir = std::env::temp_dir().join(format!("sw-tel-stats-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let f = dir.join("s.json");
        std::fs::write(
            &f,
            r#"{"stats": {"minecraft:custom": {"minecraft:deaths": 7, "minecraft:jump": true, "minecraft:walk_one_cm": 1.5}}}"#,
        )
        .unwrap();
        assert_eq!(
            J::Obj(read_stats(&f).unwrap().unwrap()).dumps_compact(),
            r#"{"play_ticks":0,"deaths":7,"mob_kills":0,"sprint_cm":0,"fly_cm":0,"jumps":1}"#
        );
        std::fs::write(&f, "[1]").unwrap();
        assert!(read_stats(&f).is_err());
        std::fs::write(&f, r#"{"stats": {"minecraft:custom": [1]}}"#).unwrap();
        assert_eq!(read_stats(&f).unwrap(), None);
        std::fs::write(&f, "[]").unwrap();
        assert_eq!(
            read_stats(&f).unwrap(),
            Some(
                OMap::new()
                    .with("play_ticks", 0_i64)
                    .with("deaths", 0_i64)
                    .with("mob_kills", 0_i64)
                    .with("walk_cm", 0_i64)
                    .with("sprint_cm", 0_i64)
                    .with("fly_cm", 0_i64)
                    .with("jumps", 0_i64)
            )
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
