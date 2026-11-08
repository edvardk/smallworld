//! `player_message`'s view of experience telemetry (PORTING.md "actions"; design §1.3): telemetry.py stays a
//! Python sidecar (`mc-telemetry.service`) in rust mode, so the agent ports only `Messages` (telemetry.py
//! 848-900: the rate limits and messages.tsv for the mc-telemetry plugin) and reads who is online from the
//! sidecar's `online.json`.
//!
//! `online.json` (the sidecar's side of the contract): a JSON object `{uuid: {"u", "n", "tips", "join"}}`, the
//! map `Telemetry.online()` returns, rewritten every poll. A file older than `ONLINE_STALE_S` (by mtime), missing
//! or unreadable means nobody is online (every private message is refused as "player isn't online").

use super::Telemetry;
use crate::settings::{Env, int_env};
use crate::types::{AResult, AgentError, Obj, Value, truthy};
use smallworld_core::pystr::{isspace, prefix_cp, split_ws};
use std::collections::HashMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::UNIX_EPOCH;

/// telemetry.py `VERDICT_DIR` (messages.tsv lives there; setgid minecraft).
pub const VERDICT_DIR: &str = "/run/mc-verdicts";
/// Where the telemetry sidecar keeps the latest online map.
pub const ONLINE_FILE: &str = "/var/lib/mc-telemetry/online.json";
/// An online map older than this is "nobody online".
pub const ONLINE_STALE_S: f64 = 30.0;
/// telemetry.py `Messages.EXPIRE_S`.
pub const EXPIRE_S: f64 = 120.0;

/// telemetry.py `clean_text(text, limit)`: one line for the plugin's TSV files. Every whitespace character
/// (U+001C..U+001F too) becomes a space, then what `str.isprintable()` refuses goes: after the first step that
/// is exactly the bad categories (every `Zs` is whitespace), then collapsed and cut to `limit` code points.
pub fn clean_text(text: &str, limit: usize) -> String {
    let t: String = text
        .chars()
        .map(|c| if isspace(c) { ' ' } else { c })
        .filter(|&c| !crate::unicode::is_bad_category(c))
        .collect();
    prefix_cp(&split_ws(&t).join(" "), limit).to_string()
}

#[derive(Debug, Clone)]
struct Queued {
    id: String,
    to: String,
    expires: i64,
    text: String,
    hover: String,
}

#[derive(Debug, Default)]
struct State {
    queue: Vec<Queued>,
    last: HashMap<String, f64>,
    sent: Vec<f64>,
}

/// telemetry.py `Messages` with the agent's settings, plus the sidecar's online map.
pub struct Sidecar {
    pub directory: PathBuf,
    pub online_file: PathBuf,
    pub per_player_s: i64,
    pub broadcast_s: i64,
    pub join_grace_s: i64,
    pub per_hour: i64,
    state: Mutex<State>,
}

impl Sidecar {
    pub fn new(directory: impl Into<PathBuf>, online_file: impl Into<PathBuf>) -> Self {
        Self {
            directory: directory.into(),
            online_file: online_file.into(),
            per_player_s: 1800,
            broadcast_s: 1800,
            join_grace_s: 60,
            per_hour: 20,
            state: Mutex::new(State::default()),
        }
    }

    /// mc_agent.py `make_telemetry`'s `Messages(...)`: `VERDICT_DIR`, `MESSAGE_PER_PLAYER_S` (>= 60),
    /// `MESSAGE_BROADCAST_S` (>= 60), `MESSAGE_PER_HOUR` (>= 1).
    ///
    /// # Errors
    /// `int()`'s `ValueError` text for a malformed number.
    pub fn from_env(e: &Env) -> Result<Self, String> {
        let dir = e
            .get("VERDICT_DIR")
            .filter(|d| !d.is_empty())
            .map_or(VERDICT_DIR, String::as_str);
        let mut s = Self::new(dir, ONLINE_FILE);
        s.per_player_s = int_env(e, "MESSAGE_PER_PLAYER_S", 1800)?.max(60);
        s.broadcast_s = int_env(e, "MESSAGE_BROADCAST_S", 1800)?.max(60);
        s.per_hour = int_env(e, "MESSAGE_PER_HOUR", 20)?.max(1);
        Ok(s)
    }

    fn state(&self) -> std::sync::MutexGuard<'_, State> {
        self.state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// The online map, or empty when the file is missing, unreadable, not an object or stale.
    pub fn online_at(&self, now: f64) -> Obj {
        let fresh = fs::metadata(&self.online_file)
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
            .is_some_and(|t| now - t.as_secs_f64() <= ONLINE_STALE_S);
        if !fresh {
            return Obj::new();
        }
        match smallworld_core::pyjson::read_json(&self.online_file) {
            Some(Value::Object(m)) => m.into_iter().filter(|(_, p)| p.is_object()).collect(),
            _ => Obj::new(),
        }
    }

    fn write(&self, st: &State) -> AResult<()> {
        let mut lines = vec!["mc-messages 1".to_string()];
        for m in &st.queue {
            lines.push(["M", &m.id, &m.to, &m.expires.to_string(), &m.text, &m.hover].join("\t"));
        }
        atomic_text(
            &self.directory.join("messages.tsv"),
            &(lines.join("\n") + "\n"),
        )?;
        Ok(())
    }
}

/// telemetry.py `_atomic_text`: `.NAME.tmp`, 0640 (the plugin, group minecraft, reads it), `os.replace`.
fn atomic_text(path: &Path, text: &str) -> std::io::Result<()> {
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    let tmp = path.with_file_name(format!(".{name}.tmp"));
    fs::write(&tmp, text)?;
    fs::set_permissions(&tmp, fs::Permissions::from_mode(0o640))?;
    fs::rename(&tmp, path)
}

#[allow(clippy::cast_precision_loss)]
impl Telemetry for Sidecar {
    fn online(&self) -> AResult<Obj> {
        Ok(self.online_at(crate::sys::now()))
    }

    fn check(&self, to: &str, kind: &str, now: f64, online: &Obj) -> AResult<String> {
        let mut st = self.state();
        st.sent.retain(|t| now - t < 3600.0);
        if i64::try_from(st.sent.len()).unwrap_or(i64::MAX) >= self.per_hour {
            return Ok("hourly message limit reached".into());
        }
        let last = |k: &str| st.last.get(k).copied().unwrap_or(0.0);
        if to == "*" {
            if now - last("*") < self.broadcast_s as f64 {
                return Ok("a broadcast was sent recently".into());
            }
            return Ok(String::new());
        }
        let Some(p) = online.get(to).and_then(Value::as_object) else {
            return Ok("player isn't online".into());
        };
        if !p.get("tips").is_none_or(truthy) {
            return Ok("player turned tips off".into());
        }
        if kind != "join"
            && let Some(join) = p.get("join").filter(|j| truthy(j))
        {
            let Some(join) = join
                .as_f64()
                .filter(|_| !join.is_boolean())
                .or_else(|| join.as_bool().map(f64::from))
            else {
                return Err(AgentError::Crash(format!(
                    "TypeError: unsupported operand type(s) for -: 'float' and '{}'",
                    crate::worlds::py_type(join)
                )));
            };
            if now * 1000.0 - join < (self.join_grace_s * 1000) as f64 {
                return Ok("player joined less than a minute ago".into());
            }
        }
        if now - last(to) < self.per_player_s as f64 {
            return Ok("player got a message recently".into());
        }
        Ok(String::new())
    }

    #[allow(clippy::cast_possible_truncation)]
    fn add(
        &self,
        id: &str,
        to: &str,
        kind: &str,
        text: &str,
        hover: &str,
        now: f64,
    ) -> AResult<()> {
        let _ = kind;
        let mut st = self.state();
        st.queue.retain(|m| (m.expires as f64) > now * 1000.0);
        st.queue.push(Queued {
            id: id.to_string(),
            to: to.to_string(),
            expires: ((now + EXPIRE_S) * 1000.0) as i64,
            text: clean_text(text, 256),
            hover: clean_text(hover, 256),
        });
        st.last.insert(to.to_string(), now);
        st.sent.push(now);
        self.write(&st)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn sidecar(dir: &Path) -> Sidecar {
        Sidecar::new(dir, dir.join("online.json"))
    }

    fn online(v: Value) -> Obj {
        v.as_object().unwrap().clone()
    }

    #[test]
    fn rate_limits_like_python() {
        let tmp = std::env::temp_dir().join(format!("sw-msg-{}", std::process::id()));
        fs::create_dir_all(&tmp).unwrap();
        let s = sidecar(&tmp);
        let u = "11111111-2222-4333-8444-555555555555";
        let now = 1_000_000.0;
        let on =
            online(json!({u: {"u": u, "n": "Bob", "tips": true, "join": (now - 600.0) * 1000.0}}));
        assert_eq!(s.check("*", "lag", now, &on).unwrap(), "");
        assert_eq!(
            s.check("22222222-2222-4333-8444-555555555555", "lag", now, &on)
                .unwrap(),
            "player isn't online"
        );
        let off = online(json!({u: {"u": u, "n": "Bob", "tips": false}}));
        assert_eq!(
            s.check(u, "lag", now, &off).unwrap(),
            "player turned tips off"
        );
        let fresh = online(json!({u: {"n": "Bob", "join": (now - 10.0) * 1000.0}}));
        assert_eq!(
            s.check(u, "lag", now, &fresh).unwrap(),
            "player joined less than a minute ago"
        );
        assert_eq!(s.check(u, "join", now, &fresh).unwrap(), "");
        s.add("lag-0123abcd", u, "lag", "hi\tthere\u{1f}x", "", now)
            .unwrap();
        assert_eq!(
            fs::read_to_string(tmp.join("messages.tsv")).unwrap(),
            format!("mc-messages 1\nM\tlag-0123abcd\t{u}\t1000120000\thi there x\t\n")
        );
        let mode = fs::metadata(tmp.join("messages.tsv"))
            .unwrap()
            .permissions()
            .mode();
        assert_eq!(mode & 0o777, 0o640);
        assert_eq!(
            s.check(u, "lag", now + 1.0, &on).unwrap(),
            "player got a message recently"
        );
        s.add("srv-0123abcd", "*", "server", "all", "", now)
            .unwrap();
        assert_eq!(
            s.check("*", "server", now + 5.0, &on).unwrap(),
            "a broadcast was sent recently"
        );
        for k in 0..18 {
            s.add("tip-0123abcd", &format!("x{k}"), "tip", "t", "", now)
                .unwrap();
        }
        assert_eq!(
            s.check("*", "server", now + 5.0, &on).unwrap(),
            "hourly message limit reached"
        );
        assert_eq!(s.check("*", "server", now + 4000.0, &on).unwrap(), "");
        // stale or missing online map: nobody
        assert!(s.online_at(now).is_empty());
        fs::write(
            tmp.join("online.json"),
            json!({u: {"n": "Bob"}, "x": 5}).to_string(),
        )
        .unwrap();
        let t = crate::sys::now();
        assert_eq!(s.online_at(t).len(), 1);
        assert!(s.online_at(t + 31.0).is_empty());
        fs::remove_dir_all(&tmp).unwrap();
    }

    #[test]
    fn from_env_clamps() {
        let mut e = Env::new();
        e.insert("MESSAGE_PER_PLAYER_S".into(), "5".into());
        e.insert("MESSAGE_PER_HOUR".into(), " 0 ".into());
        let s = Sidecar::from_env(&e).unwrap();
        assert_eq!((s.per_player_s, s.broadcast_s, s.per_hour), (60, 1800, 1));
        assert_eq!(s.directory, PathBuf::from(VERDICT_DIR));
        e.insert("MESSAGE_BROADCAST_S".into(), "x".into());
        assert!(Sidecar::from_env(&e).is_err());
    }
}
