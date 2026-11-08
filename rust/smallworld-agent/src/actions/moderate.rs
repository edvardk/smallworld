//! moderate (ported: the cli runs it), whitelist and rejected_dismiss (stubs for the actions owner; they need
//! `RejectedLogins` from data and `Pending::record`, which is ported).

use super::console::check_console;
use super::{Executor, one_line};
use crate::types::{AResult, AgentError, Obj, Value, get};
use serde_json::json;
use smallworld_core::pystr::{PyStr, prefix_cp};
use smallworld_core::rcon::strip_formatting;

impl Executor {
    /// `do_moderate(args)`: kick/ban/pardon (players), ban-ip/pardon-ip, through the console allowlist's shapes.
    pub(crate) fn do_moderate(&mut self, args: &Obj) -> AResult<String> {
        let verb = get(args, "verb")
            .as_str()
            .filter(|v| ["kick", "ban", "pardon", "ban-ip", "pardon-ip"].contains(v));
        let (Some(verb), Some(Value::String(target))) = (verb, args.get("target")) else {
            return Err(AgentError::Rejected(
                "moderate: verb kick|ban|pardon|ban-ip|pardon-ip and a target".into(),
            ));
        };
        let reason = one_line(get(args, "reason"), 100);
        let with_reason = !reason.is_empty() && ["kick", "ban", "ban-ip"].contains(&verb);
        let line = check_console(&json!(format!(
            "{verb} {target}{}",
            if with_reason {
                format!(" {reason}")
            } else {
                String::new()
            }
        )))?;
        let mut r = smallworld_core::mcrcon::connect()?;
        let reply = prefix_cp(
            &strip_formatting(&r.command(&PyStr::from(line.as_str()), None, false)?),
            300,
        )
        .to_string();
        drop(r);
        if let Some(p) = &self.panel {
            (p.changed)();
        }
        Ok(if reply.is_empty() {
            format!("{verb} {target}")
        } else {
            reply
        })
    }

    /// `do_whitelist(args)`: not ported yet (actions).
    pub(crate) fn do_whitelist(&mut self, _args: &Obj) -> AResult<String> {
        Err(AgentError::NotPorted("actions::moderate::do_whitelist"))
    }

    /// `do_rejected_dismiss(args)`: not ported yet (actions).
    pub(crate) fn do_rejected_dismiss(&mut self, _args: &Obj) -> AResult<String> {
        Err(AgentError::NotPorted(
            "actions::moderate::do_rejected_dismiss",
        ))
    }
}
