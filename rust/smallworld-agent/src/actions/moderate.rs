//! moderate, whitelist (+ `_server_list`, `_whitelisted`, `read_json_list`) and rejected_dismiss (mc_agent.py
//! 1559-1627). `RejectedLogins` is data's (panel/rejected.rs).

use super::console::check_console;
use super::{Executor, one_line};
use crate::panel::{NAME, UUID};
use crate::types::{AResult, AgentError, Obj, Value, get, py_str, truthy};
use serde_json::json;
use smallworld_core::properties::{UNTRUSTED_MAX_BYTES, read_untrusted_limit};
use smallworld_core::pystr::{PyStr, prefix_cp};
use smallworld_core::rcon::strip_formatting;
use std::os::unix::ffi::OsStrExt;

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

    /// `_server_list(name)`: a JSON list from the server dir (`panel.server_dir`, else `$MC_SERVER_DIR`).
    fn server_list(&self, name: &str) -> Vec<Obj> {
        let dir = match self.panel.as_ref().and_then(|p| p.server_dir.as_ref()) {
            Some(d) => PyStr::from_os_bytes(d.as_os_str().as_bytes()),
            None => smallworld_core::mcrcon::server_dir(),
        };
        read_json_list(&smallworld_core::mcrcon::path_join(&dir, name))
    }

    /// `_whitelisted(name)`: the whitelist.json entry whose name matches (case-insensitive).
    fn whitelisted(&self, name: &str) -> Option<Obj> {
        let want = name.to_lowercase();
        self.server_list("whitelist.json")
            .into_iter()
            .find(|e| py_str(e.get("name").unwrap_or(&json!(""))).to_lowercase() == want)
    }

    /// `do_whitelist(args)`: live via RCON, confirmed in whitelist.json, then recorded for the config repo
    /// (pending.json -> pull request).
    pub(crate) fn do_whitelist(&mut self, args: &Obj) -> AResult<String> {
        let panel = self.need_panel("whitelist")?;
        let op = get(args, "op")
            .as_str()
            .filter(|o| ["add", "remove"].contains(o));
        let name = get(args, "name").as_str().filter(|n| NAME.is_match(n));
        let (Some(op), Some(name)) = (op, name) else {
            return Err(AgentError::Rejected(
                "whitelist: needs op add|remove and a Java name (3-16 letters, digits, _)".into(),
            ));
        };
        let before = self.whitelisted(name);
        if op == "remove" {
            let Some(b) = &before else {
                return Err(AgentError::Rejected(format!(
                    "whitelist: {name} isn't on the whitelist"
                )));
            };
            // `str(o.get("uuid")) == before.get("uuid")`: only a string uuid in `before` can be equal
            if let Some(Value::String(bu)) = b.get("uuid")
                && self
                    .server_list("ops.json")
                    .iter()
                    .any(|o| py_str(get(o, "uuid")) == *bu)
            {
                return Err(AgentError::Rejected(format!(
                    "whitelist: {name} is an operator; operators are managed in players.yml (Git)"
                )));
            }
        }
        let mut r = smallworld_core::mcrcon::connect()?;
        let line = format!("whitelist {op} {name}");
        let reply = prefix_cp(
            &strip_formatting(&r.command(&PyStr::from(line.as_str()), None, false)?),
            300,
        )
        .to_string();
        drop(r);
        let mut entry = None;
        for _ in 0..10 {
            entry = self.whitelisted(name);
            if entry.is_some() == (op == "add") {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(500));
        }
        let shown = if reply.is_empty() { "no reply" } else { &reply };
        if op == "add" && entry.is_none() {
            return Err(AgentError::Rejected(format!(
                "whitelist: the server didn't add {name} ({shown})"
            )));
        }
        if op == "remove" && entry.is_some() {
            return Err(AgentError::Rejected(format!(
                "whitelist: the server didn't remove {name} ({shown})"
            )));
        }
        let source = if op == "add" { entry } else { before }.unwrap_or_default();
        let uuid = py_str(source.get("uuid").unwrap_or(&json!("")));
        let real_v = get(&source, "name");
        let real = if truthy(real_v) {
            py_str(real_v)
        } else {
            name.to_string()
        };
        if !UUID.is_match(&uuid) {
            return Err(AgentError::Rejected(format!(
                "whitelist: no UUID for {name} in whitelist.json"
            )));
        }
        panel
            .pending
            .record(op, &real, &uuid, &self.login, (panel.clock)())?;
        panel.rejected.drop_name(&real)?;
        (panel.changed)();
        Ok(format!(
            "{reply} (live now; recorded for players.yml in Git)"
        ))
    }

    /// `do_rejected_dismiss(args)`: drops a name from the rejected-logins list.
    pub(crate) fn do_rejected_dismiss(&mut self, args: &Obj) -> AResult<String> {
        let Some(name) = get(args, "name").as_str().filter(|n| NAME.is_match(n)) else {
            return Err(AgentError::Rejected(
                "rejected_dismiss: needs a Java name".into(),
            ));
        };
        let panel = self.need_panel("rejected_dismiss")?;
        let found = panel.rejected.drop_name(name)?;
        (panel.changed)();
        Ok(if found {
            format!("{name} dismissed")
        } else {
            format!("{name} wasn't in the list")
        })
    }
}

/// mc_agent.py `INVENTORY_MAX`: entries per list.
pub const INVENTORY_MAX: usize = 1000;

/// mc_agent.py `read_json_list(path)`: the dicts of a JSON list in the server dir (no FIFO, no symlink, at most
/// `UNTRUSTED_MAX_BYTES`), at most `INVENTORY_MAX`; `[]` on any error. `json.loads(bytes)` accepts a UTF-8
/// BOM; its UTF-16/32 detection isn't reproduced (the server writes UTF-8).
pub fn read_json_list(path: &PyStr) -> Vec<Obj> {
    let Ok(bytes) = read_untrusted_limit(path, UNTRUSTED_MAX_BYTES) else {
        return Vec::new();
    };
    let bytes = bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(&bytes);
    let Ok(text) = std::str::from_utf8(bytes) else {
        return Vec::new();
    };
    match smallworld_core::pyjson::loads(text) {
        Some(Value::Array(items)) => items
            .into_iter()
            .filter_map(|d| match d {
                Value::Object(o) => Some(o),
                _ => None,
            })
            .take(INVENTORY_MAX)
            .collect(),
        _ => Vec::new(),
    }
}
