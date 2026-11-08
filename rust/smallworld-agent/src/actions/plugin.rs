//! `do_plugin` (mc_agent.py 1628-1660): toggle a locked plugin as a countdown job (write plugins-disabled.json,
//! `systemctl start mc-plugins-apply.service`, restart unless admin-stopped). Ported (the cli runs it).

use super::power::restart_now;
use super::{Executor, countdown, flag};
use crate::jobs::Job;
use crate::notify;
use crate::panel::plugins::{read_disabled, read_lock, write_disabled};
use crate::systemd::systemctl;
use crate::types::{AResult, AgentError, Obj, Value};
use smallworld_core::paths::units;
use std::collections::BTreeSet;
use std::sync::Arc;

impl Executor {
    pub(crate) fn do_plugin(&mut self, args: &Obj) -> AResult<String> {
        let panel = self.need_panel("plugin")?;
        let lock = read_lock(panel.plugins_lock_path())?;
        let name = match args.get("name") {
            Some(Value::String(n)) if lock.iter().any(|p| &p.name == n) => n.clone(),
            _ => {
                return Err(AgentError::Rejected(
                    "plugin: not a plugin in plugins.lock.yml".into(),
                ));
            }
        };
        let Some(Value::Bool(enabled)) = args.get("enabled").cloned() else {
            return Err(AgentError::Rejected(
                "plugin: enabled must be true or false".into(),
            ));
        };
        let disabled: BTreeSet<String> = read_disabled(panel.plugins_disabled_path())?
            .into_iter()
            .collect();
        if disabled.contains(&name) != enabled {
            let state = if enabled { "enabled" } else { "disabled" };
            return Err(AgentError::Rejected(format!(
                "plugin: {name} is already {state}"
            )));
        }
        let countdown = countdown(args)?;
        let mut want = disabled;
        if enabled {
            want.remove(&name);
        } else {
            want.insert(name.clone());
        }
        let skip = flag(args, "skip_if_empty", true)?;
        let (login, p, n) = (self.login.clone(), panel.clone(), name.clone());
        let work = Box::new(move |job: &Arc<Job>| {
            write_disabled(&want, &login, p.plugins_disabled_path(), (p.clock)())?;
            job.set_step(&format!("moving the {n} jar (mc-plugins-apply.service)"));
            systemctl(&["start", units::PLUGINS_APPLY], 300.0)?;
            (p.changed)();
            let what = if enabled { "enabled" } else { "disabled" };
            if notify::admin_stopped() {
                return Ok(format!(
                    "{n} {what}; the server is stopped (applies at start)"
                ));
            }
            restart_now(Some(job), &format!("plugin {n}"))?;
            (p.changed)();
            Ok(format!("{n} {what}; the server restarted"))
        });
        let job = panel.jobs.start(
            &self.cmd_id,
            "plugin",
            "Restart",
            &self.login,
            countdown,
            work,
            skip,
        )?;
        let verb = if enabled { "enable" } else { "disable" };
        Ok(format!(
            "plugin {name} {verb} scheduled (countdown {countdown} s, job {})",
            job.id
        ))
    }
}
