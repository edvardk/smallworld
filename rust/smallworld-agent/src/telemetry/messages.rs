//! telemetry.py `Messages` (848-900): the in-game messages the Worker asked for (signed `player_message`
//! commands), rate-limited again here (per player, join grace, tips off, broadcasts, per hour), queued in
//! `messages.tsv` (0640, the setgid verdict directory) for the mc-telemetry plugin. State in memory, as in
//! Python. `player_message` gets the online map from the in-process telemetry (`Telemetry::online`, mod.rs).

use super::snapshot::clean_str;
use crate::settings::{Env, int_env};
use crate::types::{AResult, AgentError, Obj, Value, truthy};
use std::collections::HashMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

/// telemetry.py `VERDICT_DIR` (messages.tsv lives there; setgid minecraft).
pub const VERDICT_DIR: &str = "/run/mc-verdicts";
/// telemetry.py `Messages.EXPIRE_S`.
pub const EXPIRE_S: f64 = 120.0;

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

/// telemetry.py `Messages` with the agent's settings.
pub struct Messages {
    pub directory: PathBuf,
    pub per_player_s: i64,
    pub broadcast_s: i64,
    pub join_grace_s: i64,
    pub per_hour: i64,
    state: Mutex<State>,
}

impl Messages {
    pub fn new(directory: impl Into<PathBuf>) -> Self {
        Self {
            directory: directory.into(),
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
        let mut s = Self::new(dir);
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
impl Messages {
    /// `check(to, kind, now, online)`: why not to send, or "".
    ///
    /// # Errors
    /// `Crash` where Python raises (a `join` that isn't a number).
    pub fn check(&self, to: &str, kind: &str, now: f64, online: &Obj) -> AResult<String> {
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
    /// `add(msg_id, to, kind, text, hover, now)`.
    ///
    /// # Errors
    /// The `OSError` of writing messages.tsv.
    pub fn add(
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
            text: clean_str(text, 256),
            hover: clean_str(hover, 256),
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

    fn sidecar(dir: &Path) -> Messages {
        Messages::new(dir)
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
        fs::remove_dir_all(&tmp).unwrap();
    }

    #[test]
    fn from_env_clamps() {
        let mut e = Env::new();
        e.insert("MESSAGE_PER_PLAYER_S".into(), "5".into());
        e.insert("MESSAGE_PER_HOUR".into(), " 0 ".into());
        let s = Messages::from_env(&e).unwrap();
        assert_eq!((s.per_player_s, s.broadcast_s, s.per_hour), (60, 1800, 1));
        assert_eq!(s.directory, PathBuf::from(VERDICT_DIR));
        e.insert("MESSAGE_BROADCAST_S".into(), "x".into());
        assert!(Messages::from_env(&e).is_err());
    }
}
