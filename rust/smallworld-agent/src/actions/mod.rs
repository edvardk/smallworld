//! mc_agent.py's `Executor` (the action allowlist, 1390-1918), `Panel` (the state it needs), the argument
//! helpers (`_flag`, `_countdown`, `_one_line`) and the console allowlist (console.rs).
//!
//! Owner: actions. Each `do_<action>` lives in the file of its group, as an `impl Executor` block:
//!
//! | File | Actions | Status |
//! |---|---|---|
//! | power.rs | restart (+ `_restart_now`, `_backup_first`); stop, start, backup | restart ported (cli); stop/start/backup stubs |
//! | plugin.rs | plugin | ported (cli) |
//! | moderate.rs | moderate; whitelist, rejected_dismiss | moderate ported (cli); the others stubs |
//! | world.rs | world_create, world_switch, world_delete, `worlds_inventory` | ported (cli) |
//! | restore.rs | restore | stub |
//! | info.rs | status, selftest, lookup, console, tail, cancel, player_message | stubs |

pub mod console;
pub mod info;
pub mod moderate;
pub mod plugin;
pub mod power;
pub mod restore;
pub mod world;

use crate::jobs::{ClockFn, Jobs};
use crate::panel::pending::Pending;
use crate::panel::rejected::RejectedLogins;
use crate::types::{
    ACTIONS, AResult, AgentError, COUNTDOWNS, Command, Obj, Value, as_int, py_str, truthy,
};
use smallworld_core::paths;
use smallworld_core::pystr::{isspace, prefix_cp, split_ws};
use std::path::{Path, PathBuf};
use std::sync::Arc;

/// `changed()`: push the inventory now (the panel shows the new state without waiting 10 minutes).
pub type Changed = Arc<dyn Fn() + Send + Sync>;
/// `Agent.run_selftest(trigger)` (misc owns it).
pub type SelftestFn = Arc<dyn Fn(&str) -> AResult<Obj> + Send + Sync>;
/// `Lookup(name)` (misc owns it): the JSON reply for the panel.
pub type LookupFn = Arc<dyn Fn(&Value) -> AResult<Value> + Send + Sync>;
/// `collect(Counters())` (data owns it): the status sample.
pub type SampleFn = Arc<dyn Fn() -> AResult<Value> + Send + Sync>;
/// `ConsoleTail.request(seconds)` (data owns it).
pub type TailFn = Arc<dyn Fn(i64) -> AResult<String> + Send + Sync>;

/// mc_agent.py `Panel`: P10 state the executor needs. `None` paths mean the product defaults (paths.rs).
pub struct Panel {
    pub jobs: Arc<Jobs>,
    pub pending: Pending,
    pub rejected: RejectedLogins,
    pub changed: Changed,
    pub server_dir: Option<PathBuf>,
    pub plugins_lock: Option<PathBuf>,
    pub plugins_disabled: Option<PathBuf>,
    pub snapshots: Option<PathBuf>,
    pub worlds_state: Option<PathBuf>,
    pub worlds_library: Option<PathBuf>,
    pub world_request: Option<PathBuf>,
    pub clock: ClockFn,
    pub selftest: Option<SelftestFn>,
    pub lookup: Option<LookupFn>,
}

fn or_default<'a>(p: Option<&'a PathBuf>, default: &'a str) -> &'a Path {
    p.map_or(Path::new(default), PathBuf::as_path)
}

impl Panel {
    /// A panel with the product's default paths.
    pub fn new(jobs: Arc<Jobs>, state_dir: &Path) -> Self {
        Self {
            jobs,
            pending: Pending::new(state_dir.join("pending.json")),
            rejected: RejectedLogins::new(state_dir.join("rejected.json")),
            changed: Arc::new(|| {}),
            server_dir: None,
            plugins_lock: None,
            plugins_disabled: None,
            snapshots: None,
            worlds_state: None,
            worlds_library: None,
            world_request: None,
            clock: Arc::new(crate::sys::now),
            selftest: None,
            lookup: None,
        }
    }

    pub fn plugins_lock_path(&self) -> &Path {
        or_default(self.plugins_lock.as_ref(), paths::PLUGINS_LOCK_FILE)
    }
    pub fn plugins_disabled_path(&self) -> &Path {
        or_default(self.plugins_disabled.as_ref(), paths::PLUGINS_DISABLED_FILE)
    }
    pub fn snapshots_path(&self) -> &Path {
        or_default(self.snapshots.as_ref(), paths::SNAPSHOTS_FILE)
    }
    pub fn worlds_state_path(&self) -> &Path {
        or_default(self.worlds_state.as_ref(), paths::WORLDS_FILE)
    }
    pub fn worlds_library_path(&self) -> &Path {
        or_default(self.worlds_library.as_ref(), paths::WORLDS_LIBRARY_FILE)
    }
    pub fn world_request_path(&self) -> &Path {
        or_default(self.world_request.as_ref(), paths::WORLD_REQUEST_FILE)
    }
}

/// player_message's view of the telemetry sidecar (owner: actions, with the sidecar's online.json; PORTING.md).
pub trait Telemetry: Send + Sync {
    /// `t.online()`: uuid -> {u, n, tips, join}.
    fn online(&self) -> AResult<Obj>;
    /// `t.messages.check(to, kind, now, online)`: why not to send, or "".
    fn check(&self, to: &str, kind: &str, now: f64, online: &Obj) -> AResult<String>;
    /// `t.messages.add(id, to, kind, text, hover, now)`.
    fn add(&self, id: &str, to: &str, kind: &str, text: &str, hover: &str, now: f64)
    -> AResult<()>;
}

/// mc_agent.py `Executor`.
pub struct Executor {
    pub sample: SampleFn,
    pub tail: Option<TailFn>,
    pub panel: Option<Arc<Panel>>,
    pub telemetry: Option<Arc<dyn Telemetry>>,
    pub login: String,
    pub cmd_id: String,
}

impl Executor {
    pub fn new(
        sample: SampleFn,
        tail: Option<TailFn>,
        panel: Option<Arc<Panel>>,
        telemetry: Option<Arc<dyn Telemetry>>,
    ) -> Self {
        Self {
            sample,
            tail,
            panel,
            telemetry,
            login: String::new(),
            cmd_id: String::new(),
        }
    }

    /// `run(cmd)`: the action's output text. `Action` errors become `Rejected("{action}: {e}")`.
    ///
    /// # Errors
    /// `Rejected` for a refused command; `Os`/`Value`/`Key`/`Crash` as the Python action raises them.
    pub fn run(&mut self, cmd: &Command) -> AResult<String> {
        let action = cmd.action.as_str();
        if !ACTIONS.contains(&action) {
            return Err(AgentError::Rejected(format!("unknown action: {action}")));
        }
        self.login = prefix_cp(&cmd.login, 200).to_string();
        self.cmd_id.clone_from(&cmd.id);
        let args = &cmd.args;
        let r = match action {
            "status" => self.do_status(args),
            "selftest" => self.do_selftest(args),
            "lookup" => self.do_lookup(args),
            "console" => self.do_console(args),
            "stop" => self.do_stop(args),
            "start" => self.do_start(args),
            "restart" => self.do_restart(args),
            "backup" => self.do_backup(args),
            "tail" => self.do_tail(args),
            "cancel" => self.do_cancel(args),
            "whitelist" => self.do_whitelist(args),
            "rejected_dismiss" => self.do_rejected_dismiss(args),
            "moderate" => self.do_moderate(args),
            "plugin" => self.do_plugin(args),
            "player_message" => self.do_player_message(args),
            "restore" => self.do_restore(args),
            "world_create" => self.do_world_create(args),
            "world_switch" => self.do_world_switch(args),
            "world_delete" => self.do_world_delete(args),
            _ => Err(AgentError::Rejected(format!("unknown action: {action}"))),
        };
        match r {
            Err(AgentError::Action(e)) => Err(AgentError::Rejected(format!("{action}: {e}"))),
            other => other,
        }
    }

    /// `need_panel(action)`.
    ///
    /// # Errors
    /// `Rejected("{action}: not available in this agent")`.
    pub fn need_panel(&self, action: &str) -> AResult<Arc<Panel>> {
        self.panel
            .clone()
            .ok_or_else(|| AgentError::Rejected(format!("{action}: not available in this agent")))
    }
}

/// `_flag(args, key, default)`: a bool argument.
///
/// # Errors
/// `Rejected("{key} must be true or false")`.
pub fn flag(args: &Obj, key: &str, default: bool) -> AResult<bool> {
    match args.get(key) {
        None => Ok(default),
        Some(Value::Bool(b)) => Ok(*b),
        Some(_) => Err(AgentError::Rejected(format!("{key} must be true or false"))),
    }
}

/// `_countdown(args)`: one of `COUNTDOWNS`.
///
/// # Errors
/// `Rejected("countdown must be one of 0, 10, 60, 300 seconds")`.
pub fn countdown(args: &Obj) -> AResult<i64> {
    let v = args.get("countdown").map_or(Some(0), as_int);
    match v {
        Some(c) if COUNTDOWNS.contains(&c) => Ok(c),
        _ => Err(AgentError::Rejected(format!(
            "countdown must be one of {} seconds",
            COUNTDOWNS.map(|c| c.to_string()).join(", ")
        ))),
    }
}

/// `_one_line(text, limit)`: printable, one line (any whitespace becomes a space), collapsed, cut to `limit`.
pub fn one_line(text: &Value, limit: usize) -> String {
    let raw = if truthy(text) {
        py_str(text)
    } else {
        String::new()
    };
    let t = crate::unicode::nfkc(&raw);
    let t: String = t
        .chars()
        .map(|c| if isspace(c) { ' ' } else { c })
        .collect();
    let t: String = t
        .chars()
        .filter(|&c| !crate::unicode::is_bad_category(c))
        .collect();
    prefix_cp(&split_ws(&t).join(" "), limit).to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn obj(v: Value) -> Obj {
        v.as_object().unwrap().clone()
    }

    #[test]
    fn helpers_like_python() {
        assert_eq!(flag(&obj(json!({})), "backup", false), Ok(false));
        assert_eq!(
            flag(&obj(json!({"backup": true})), "backup", false),
            Ok(true)
        );
        assert!(flag(&obj(json!({"backup": null})), "backup", false).is_err());
        assert_eq!(countdown(&obj(json!({}))), Ok(0));
        assert_eq!(countdown(&obj(json!({"countdown": 60}))), Ok(60));
        for bad in [json!(5), json!(10.0), json!(true), json!("10")] {
            assert_eq!(
                countdown(&obj(json!({"countdown": bad}))),
                Err(AgentError::Rejected(
                    "countdown must be one of 0, 10, 60, 300 seconds".into()
                ))
            );
        }
        // whitespace (Python's isspace, U+001C included) becomes a space before controls are dropped
        assert_eq!(one_line(&json!("a\u{1c}b\u{7}c\n d"), 100), "a bc d");
        assert_eq!(one_line(&json!(null), 100), "");
        assert_eq!(one_line(&json!(12), 100), "12");
        assert_eq!(one_line(&json!("\u{ff21}bc"), 2), "Ab");
    }
}
