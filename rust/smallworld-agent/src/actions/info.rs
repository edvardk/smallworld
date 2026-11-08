//! status, selftest, lookup, console, tail, cancel, player_message (mc_agent.py 1419-1452, 1543-1565,
//! 1662-1688). Owner: actions. The pieces other modules own come in through `Executor`/`Panel` fields:
//! `Executor.sample` (data's `collect` over the persistent RCON), `Executor.tail` (data's `ConsoleTail`),
//! `Panel.selftest`/`Panel.lookup` (misc), `Executor.telemetry` (telemetry.rs, the sidecar's view).

use super::console::check_console;
use super::{Executor, one_line};
use crate::data::tail::TAIL_MAX_S;
use crate::types::{AResult, AgentError, Obj, Value, get, py_str, truthy};
use regex::Regex;
use smallworld_core::pyjson::dumps_compact;
use smallworld_core::pystr::{PyStr, prefix_cp};
use std::sync::LazyLock;

/// telemetry.py `UUID` used with `.match`: `^...$` without MULTILINE also matches before one final newline.
static TELEMETRY_UUID: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\n?\z").unwrap()
});
/// do_player_message's id shape (`re.fullmatch`, so no newline allowance).
static MESSAGE_ID: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-z]{3,6}-[0-9a-f]{8,32}\z").unwrap());

fn rejected(m: impl Into<String>) -> AgentError {
    AgentError::Rejected(m.into())
}

/// `r.get(key)` for a value that may not be a dict (Python would raise AttributeError: a crash).
fn steps_text(r: &Obj) -> AResult<String> {
    let steps = get(r, "steps");
    if !truthy(steps) {
        return Ok(String::new());
    }
    let mut names = Vec::new();
    for s in crate::types::py_iter(steps)? {
        match s.get("step") {
            Some(Value::String(n)) => names.push(n.clone()),
            Some(other) => {
                return Err(AgentError::Crash(format!(
                    "TypeError: sequence item {}: expected str instance, {} found",
                    names.len(),
                    crate::worlds::py_type(other)
                )));
            }
            None => return Err(AgentError::Crash("KeyError: 'step'".into())),
        }
    }
    Ok(names.join(", "))
}

impl Executor {
    /// `do_status`: one sample as compact JSON.
    pub(crate) fn do_status(&mut self, _args: &Obj) -> AResult<String> {
        Ok(dumps_compact(&(self.sample)()?))
    }

    /// `do_selftest`: the Panel -> Git self-test now (cached for a minute by misc); one line for the toast.
    pub(crate) fn do_selftest(&mut self, _args: &Obj) -> AResult<String> {
        let panel = self.need_panel("selftest")?;
        let Some(selftest) = panel.selftest.clone() else {
            return Err(rejected("selftest: not available in this agent"));
        };
        let r = selftest("panel")?;
        (panel.changed)();
        if truthy(get(&r, "ok")) {
            let steps = steps_text(&r)?;
            let head = if truthy(get(&r, "cached")) {
                "passed (an earlier run: one per minute)"
            } else {
                "passed"
            };
            return Ok(format!("{head}: {steps}"));
        }
        let error = get(&r, "error");
        Ok(format!(
            "failed at {}",
            if truthy(error) {
                py_str(error)
            } else {
                "an unknown step".into()
            }
        ))
    }

    /// `do_lookup`: a Java name -> UUID through Mojang, from this box; compact JSON.
    pub(crate) fn do_lookup(&mut self, args: &Obj) -> AResult<String> {
        let panel = self.need_panel("lookup")?;
        let Some(lookup) = panel.lookup.clone() else {
            return Err(rejected("lookup: not available in this agent"));
        };
        Ok(dumps_compact(&lookup(get(args, "name"))?))
    }

    /// `do_console`: an allowlisted line over a fresh RCON connection; the reply cut to 4000 code points.
    pub(crate) fn do_console(&mut self, args: &Obj) -> AResult<String> {
        let line = check_console(get(args, "command"))?;
        let mut r = smallworld_core::mcrcon::connect()?;
        let reply = r.command(&PyStr::from(line.as_str()), None, false)?;
        Ok(prefix_cp(&reply, 4000).to_string())
    }

    /// `do_tail`: the live console over the WebSocket for 10-180 s.
    pub(crate) fn do_tail(&mut self, args: &Obj) -> AResult<String> {
        let seconds = match args.get("seconds") {
            None => 120,
            // an int (not a bool, not a float); one beyond i64 is clamped to the maximum like any big int
            Some(Value::Number(n)) if n.is_i64() => n.as_i64().unwrap_or(0),
            Some(Value::Number(n)) if n.is_u64() => TAIL_MAX_S,
            Some(_) => return Err(rejected("tail: seconds must be an integer")),
        };
        let Some(tail) = self.tail.clone() else {
            return Err(rejected("tail: the live console needs the WebSocket"));
        };
        tail(TAIL_MAX_S.min(seconds.max(10)))
    }

    /// `do_cancel`: cancels the job in countdown.
    pub(crate) fn do_cancel(&mut self, _args: &Obj) -> AResult<String> {
        let panel = self.need_panel("cancel")?;
        panel.jobs.cancel(&self.login).map_err(|e| match e {
            AgentError::Action(m) => rejected(format!("cancel: {m}")),
            other => other,
        })
    }

    /// `do_player_message`: a message the Worker's verdict engine decided on, re-checked against the live
    /// state and queued for the mc-telemetry plugin.
    pub(crate) fn do_player_message(&mut self, args: &Obj) -> AResult<String> {
        let Some(t) = self.telemetry.clone() else {
            return Err(rejected(
                "player_message: experience telemetry is off on this server",
            ));
        };
        let to = match args.get("to") {
            Some(Value::String(s)) if s == "*" || TELEMETRY_UUID.is_match(s) => s.clone(),
            _ => return Err(rejected("player_message: to must be a player UUID or *")),
        };
        let kind = match get(args, "kind").as_str() {
            Some(k @ ("lag" | "server" | "join" | "tip")) => k.to_string(),
            _ => {
                return Err(rejected(
                    "player_message: kind must be lag, server, join or tip",
                ));
            }
        };
        let msg_id = match args.get("id") {
            Some(Value::String(s)) if MESSAGE_ID.is_match(s) => s.clone(),
            _ => return Err(rejected("player_message: bad id")),
        };
        let text = one_line(get(args, "text"), 256);
        let hover = one_line(get(args, "hover"), 256);
        if text.is_empty() {
            return Err(rejected("player_message: empty text"));
        }
        let now = crate::sys::now();
        let online = t.online()?;
        let why = t.check(&to, &kind, now, &online)?;
        if !why.is_empty() {
            return Err(rejected(format!("player_message: not sent ({why})")));
        }
        t.add(&msg_id, &to, &kind, &text, &hover, now)?;
        if to == "*" {
            return Ok("queued for everyone".into());
        }
        // `online[to]["n"]`: check() only passes a player who is online.
        let who = online
            .get(&to)
            .and_then(|p| p.get("n"))
            .ok_or_else(|| AgentError::Crash("KeyError: 'n'".into()))?;
        Ok(format!("queued for {}", py_str(who)))
    }
}
