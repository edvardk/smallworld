//! `smallworld-agent cli LOGIN ACTION [ARGS_JSON]`: mc_agent.py `cli_main` (2639-2766), what mcctl runs over
//! Tailscale SSH (`sudo mc-run -/etc/minecraft/notify.env <bin> cli mcctl:USER ACTION [ARGS_JSON]`,
//! mcctl/actions.py `agent_command`). As the ops user in mc-run's sandbox, so systemd actions go through the
//! same polkit rules as the daemon's. No signature: the Executor's own checks, the job runner (foreground, one
//! job at a time across processes: job.lock, shared with the Python daemon) and the audit entry are the panel's.
//! Destructive world and plugin actions also need the typed confirmation (`cli_confirm`).
//!
//! Output: the job's progress (`countdown: ...`, `running: ...`), then `RESULT ok|failed|refused: <text>`.
//! Exit 0 done, 1 failed/cancelled/refused (or an exception Python wouldn't catch: its last traceback line on
//! stderr, no RESULT), 2 usage.

use crate::actions::world::worlds_inventory;
use crate::actions::{Executor, Panel};
use crate::jobs::{Jobs, Report};
use crate::panel::plugins::{read_disabled, read_lock};
use crate::types::{AgentError, Command, Obj, Value, get};
use regex::Regex;
use serde_json::json;
use smallworld_core::audit::audit;
use smallworld_core::paths;
use smallworld_core::pyjson::{dumps_sorted, loads};
use smallworld_core::pystr::prefix_cp;
use std::collections::BTreeSet;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock, Mutex};

/// mc_agent.py `CLI_LOGIN`.
static CLI_LOGIN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^mcctl:[A-Za-z0-9._@-]{1,64}$").unwrap());
/// mc_agent.py `CLI_ACTIONS`.
pub const CLI_ACTIONS: [&str; 6] = [
    "restart",
    "plugin",
    "moderate",
    "world_create",
    "world_switch",
    "world_delete",
];
/// mc_agent.py `CLI_LISTS`.
pub const CLI_LISTS: [&str; 2] = ["worlds", "plugins"];

/// mc_agent.py `CLI_USAGE` (it names the Python script: mcctl and the docs quote it).
pub fn usage() -> String {
    format!(
        "usage: mc_agent.py cli mcctl:USER {{{}}} [ARGS_JSON] | mc_agent.py cli mcctl:USER {{{}}}",
        CLI_ACTIONS.join("|"),
        CLI_LISTS.join("|")
    )
}

/// `cli_confirm(action, args)`: the typed confirmation an action needs.
pub fn cli_confirm(action: &str, args: &Obj) -> Option<String> {
    let name = crate::types::py_str(get(args, "name"));
    match action {
        "world_create" => Some(format!("create {name}")),
        "world_switch" => Some(format!("switch to {name}")),
        "world_delete" => Some(format!("delete {name}")),
        "plugin" => Some(format!(
            "{} {name}",
            if get(args, "enabled") == &Value::Bool(true) {
                "enable"
            } else {
                "disable"
            }
        )),
        _ => None,
    }
}

/// Where the cli prints (stdout, flushed per line; tests capture it).
pub type Out = Arc<dyn Fn(&str) + Send + Sync>;

fn stdout_out() -> Out {
    Arc::new(|line: &str| {
        let mut o = std::io::stdout().lock();
        let _ = writeln!(o, "{line}");
        let _ = o.flush();
    })
}

/// The process entry: `cli_main(sys.argv[2:])`.
pub fn main(argv: &[String]) -> u8 {
    run(
        argv,
        None,
        Path::new(paths::AGENT_STATE_DIR),
        &stdout_out(),
        true,
    )
}

/// `cli_panel(state_dir, report)`: the agent's Panel (default paths), with a job runner in the foreground.
pub fn cli_panel(state_dir: &Path, report: Report) -> Panel {
    let jobs = Jobs::new(report, Some(PathBuf::from(paths::JOB_LOCK_FILE)), false);
    Panel::new(jobs, state_dir)
}

/// `cli_main(argv, panel, state_dir, out)`. `signals`: install the SIGTERM/SIGHUP/SIGINT handlers (the process
/// does; tests don't).
pub fn run(
    argv: &[String],
    panel: Option<Panel>,
    state_dir: &Path,
    say: &Out,
    signals: bool,
) -> u8 {
    if !matches!(argv.len(), 2 | 3) || !CLI_LOGIN.is_match(&argv[0]) {
        say(&usage());
        return 2;
    }
    let (login, action) = (argv[0].as_str(), argv[1].as_str());
    let raw = if argv.len() == 3 {
        loads(&argv[2])
    } else {
        Some(json!({}))
    };
    let Some(Value::Object(raw)) = raw else {
        say(&usage());
        return 2;
    };
    if !CLI_ACTIONS.contains(&action) && !CLI_LISTS.contains(&action) {
        say(&usage());
        return 2;
    }

    let last = Arc::new(Mutex::new(String::new()));
    let report: Report = {
        let (say, last) = (say.clone(), last.clone());
        Arc::new(move |job: &Value| {
            let step = job
                .get("step")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty());
            let what = step.map_or_else(
                || crate::types::py_str(job.get("label").unwrap_or(&Value::Null)),
                str::to_string,
            );
            let line = format!(
                "{}: {what}",
                crate::types::py_str(job.get("state").unwrap_or(&Value::Null))
            );
            let mut l = last
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            if *l != line {
                l.clone_from(&line);
                say(&line);
            }
        })
    };
    let panel = Arc::new(match panel {
        Some(p) => {
            p.jobs.set_report(report);
            p
        }
        None => cli_panel(state_dir, report),
    });
    let mut executor = Executor::new(Arc::new(|| Ok(json!({}))), None, Some(panel.clone()), None);

    // An exception Python doesn't catch: its traceback's last line, exit 1 (no RESULT, no audit).
    let crash = |e: &AgentError| -> u8 {
        eprintln!(
            "Traceback (most recent call last):\n  (smallworld-agent)\n{}",
            crash_line(e)
        );
        1
    };

    if action == "plugins" {
        let listed = (|| -> Result<Value, AgentError> {
            let disabled: BTreeSet<String> = read_disabled(panel.plugins_disabled_path())?
                .into_iter()
                .collect();
            let lock = read_lock(panel.plugins_lock_path())?;
            Ok(Value::Array(
                lock.iter()
                    .map(|p| {
                        let mut o = p.to_json();
                        o.insert("enabled".into(), json!(!disabled.contains(&p.name)));
                        Value::Object(o)
                    })
                    .collect(),
            ))
        })();
        return match listed {
            Ok(v) => {
                say(&dumps_sorted(&v));
                0
            }
            Err(e) => crash(&e),
        };
    }
    if action == "worlds" {
        return match worlds_inventory(&panel) {
            Ok(v) => {
                say(&dumps_sorted(&v));
                0
            }
            Err(e) => crash(&e),
        };
    }

    let mut args = raw.clone();
    args.remove("confirm");
    let shown = prefix_cp(&dumps_sorted(&Value::Object(args.clone())), 500).to_string();
    let finish = |ok: bool, kind: &str, text: &str| -> u8 {
        let result = if ok {
            "ok".to_string()
        } else {
            prefix_cp(text, 300).to_string()
        };
        audit(
            &format!(
                "mcctl action={action} ok={} login={login}",
                if ok { "True" } else { "False" }
            ),
            &[
                ("action", action),
                ("login", login),
                ("args", &shown),
                ("via", "mcctl"),
                ("result", &result),
            ],
        );
        say(&format!("RESULT {kind}: {text}"));
        u8::from(!ok)
    };

    if let Some(want) = cli_confirm(action, &args)
        && raw.get("confirm") != Some(&Value::String(want.clone()))
    {
        return finish(
            false,
            "refused",
            &format!("{action}: type \"{want}\" to confirm"),
        );
    }
    if action.starts_with("world_") && !state_dir.is_dir() {
        let why = "the world library records its changes through mc-agent (panel.mode: cloudflare): not here";
        return finish(false, "refused", &format!("{action}: {why}"));
    }

    if signals {
        // mcctl's session ended (Ctrl-C, ssh gone): cancel a countdown; ignored once the work runs.
        let jobs = panel.jobs.clone();
        crate::sys::handle_signals(&crate::sys::CLI_SIGNALS, move |_| {
            if let Some(job) = jobs.current()
                && job.state() == "countdown"
            {
                job.cancel.set();
            }
        });
    }

    #[allow(clippy::cast_possible_truncation)]
    let cmd = Command {
        id: format!("mcctl-{}", crate::sys::now().trunc() as i64),
        action: action.into(),
        args: args.clone(),
        login: login.into(),
    };
    let text = match executor.run(&cmd) {
        Ok(t) => t,
        Err(AgentError::Rejected(m)) => return finish(false, "refused", &m),
        Err(e @ AgentError::Os(_)) => {
            return finish(
                false,
                "failed",
                &format!("{action} failed: {}", e.class_name()),
            );
        }
        Err(AgentError::NotPorted(what)) => {
            return finish(false, "failed", &format!("{what}: not yet ported to Rust"));
        }
        Err(e) => return crash(&e),
    };
    let job = panel.jobs.current();
    match job {
        Some(job) if action != "moderate" && job.id == executor.cmd_id => {
            let (state, step) = (job.state(), job.step());
            if state == "done" {
                finish(true, "ok", &step)
            } else {
                let kind = if state == "failed" {
                    "failed".to_string()
                } else {
                    state.clone()
                };
                finish(false, &kind, if step.is_empty() { &state } else { &step })
            }
        }
        _ => finish(true, "ok", &text), // no job: done at once (moderation, a restart without countdown)
    }
}

/// The last line of the traceback Python would print for an uncaught error.
fn crash_line(e: &AgentError) -> String {
    match e {
        AgentError::Crash(line) => line.clone(),
        AgentError::Key(k) => format!("KeyError: '{k}'"),
        AgentError::Value(class, m) => format!("{class}: {m}"),
        other => format!("{}: {other}", other.class_name()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn capture() -> (Out, Arc<Mutex<Vec<String>>>) {
        let lines = Arc::new(Mutex::new(Vec::new()));
        let l = lines.clone();
        (
            Arc::new(move |s: &str| l.lock().unwrap().push(s.to_string())),
            lines,
        )
    }

    fn args(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| (*s).to_string()).collect()
    }

    #[test]
    fn usage_errors() {
        let (out, lines) = capture();
        for argv in [
            vec![],
            vec!["mcctl:ed"],
            vec!["ed", "restart"],
            vec!["mcctl:ed", "stop"],
            vec!["mcctl:ed", "restart", "[]"],
            vec!["mcctl:ed", "restart", "{nope"],
            vec!["mcctl:ed", "restart", "{}", "x"],
        ] {
            assert_eq!(
                run(&args(&argv), None, Path::new("/nonexistent"), &out, false),
                2,
                "{argv:?}"
            );
        }
        assert!(lines.lock().unwrap().iter().all(|l| *l == usage()));
        assert_eq!(
            usage(),
            "usage: mc_agent.py cli mcctl:USER {restart|plugin|moderate|world_create|world_switch|world_delete} \
             [ARGS_JSON] | mc_agent.py cli mcctl:USER {worlds|plugins}"
        );
    }

    #[test]
    fn confirmations_like_python() {
        let o = |v: Value| v.as_object().unwrap().clone();
        assert_eq!(
            cli_confirm("world_switch", &o(json!({"name": "a"}))),
            Some("switch to a".into())
        );
        assert_eq!(
            cli_confirm("plugin", &o(json!({"name": "x", "enabled": true}))),
            Some("enable x".into())
        );
        assert_eq!(
            cli_confirm("plugin", &o(json!({"name": "x", "enabled": 1}))),
            Some("disable x".into())
        );
        assert_eq!(
            cli_confirm("world_delete", &o(json!({}))),
            Some("delete None".into())
        );
        assert_eq!(cli_confirm("restart", &o(json!({}))), None);
    }

    #[test]
    fn refusals_print_result() {
        let (out, lines) = capture();
        let code = run(
            &args(&["mcctl:ed", "world_delete", r#"{"name": "a"}"#]),
            None,
            Path::new("/nonexistent"),
            &out,
            false,
        );
        assert_eq!(code, 1);
        assert_eq!(
            lines.lock().unwrap().last().unwrap(),
            r#"RESULT refused: world_delete: type "delete a" to confirm"#
        );
        let code = run(
            &args(&[
                "mcctl:ed",
                "world_delete",
                r#"{"name": "a", "confirm": "delete a"}"#,
            ]),
            None,
            Path::new("/nonexistent"),
            &out,
            false,
        );
        assert_eq!(code, 1);
        assert!(
            lines
                .lock()
                .unwrap()
                .last()
                .unwrap()
                .contains("panel.mode: cloudflare")
        );
    }
}
