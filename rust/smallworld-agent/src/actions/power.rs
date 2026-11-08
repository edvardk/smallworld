//! stop, start, restart, backup (mc_agent.py 1454-1541). restart is ported (the cli runs it); stop, start and
//! backup are stubs for the actions owner (`_stop_now` and `do_start` need `fsx::replace_write` with 0644 and
//! `notify::fenced`; `do_backup` a thread that waits for the oneshot, then `panel.changed()`).

use super::{Executor, countdown, flag};
use crate::jobs::{Job, rcon_command, wait_for_rcon};
use crate::notify;
use crate::systemd::systemctl;
use crate::types::{AResult, AgentError, Obj, RCON_WAIT_S};
use smallworld_core::paths::units;
use std::sync::Arc;

/// `_backup_first(job)`: a backup before the work; a failed backup doesn't stop it.
pub fn backup_first(job: &Job) {
    job.set_step("backing up first (mc-backup.service)");
    if let Err(e @ AgentError::Os(_)) = systemctl(&["start", units::BACKUP], 1800.0) {
        job.set_step(&format!(
            "the backup failed ({}); continuing",
            e.class_name()
        ));
    }
}

/// `_restart_now(job, why)`: a maintenance window around `systemctl restart minecraft`, then (in a job) wait
/// for RCON. The window is closed whatever happens.
///
/// # Errors
/// systemctl's error; `Action` when RCON doesn't come back within 5 minutes; a `Crash` of the RCON check.
pub fn restart_now(job: Option<&Job>, why: &str) -> AResult<String> {
    notify::maintenance(15, why);
    let r = (|| {
        if let Some(j) = job {
            j.set_step("restarting minecraft.service");
        }
        systemctl(&["restart", units::MINECRAFT], 300.0)?;
        let Some(j) = job else {
            return Ok("restarted".to_string());
        };
        j.set_step("waiting for the server to answer RCON");
        if !wait_for_rcon(RCON_WAIT_S, &rcon_command, &|s| {
            std::thread::sleep(std::time::Duration::from_secs_f64(s));
        })? {
            return Err(AgentError::Action(
                "restarted, but RCON didn't answer within 5 minutes".into(),
            ));
        }
        Ok("restarted; the server answers again".to_string())
    })();
    notify::maintenance(0, why);
    r
}

impl Executor {
    /// `do_restart(args)`.
    pub(crate) fn do_restart(&mut self, args: &Obj) -> AResult<String> {
        if notify::admin_stopped() || notify::fenced() {
            return Err(AgentError::Rejected(
                "restart: the server is admin-stopped (use start)".into(),
            ));
        }
        let (countdown, backup) = (countdown(args)?, flag(args, "backup", false)?);
        let Some(panel) = self.panel.clone().filter(|_| countdown != 0 || backup) else {
            return restart_now(None, "panel restart");
        };
        let skip = flag(args, "skip_if_empty", true)?;
        let work = Box::new(move |job: &Arc<Job>| {
            if backup {
                backup_first(job);
            }
            restart_now(Some(job), "panel restart")
        });
        let job = panel.jobs.start(
            &self.cmd_id,
            "restart",
            "Restart",
            &self.login,
            countdown,
            work,
            skip,
        )?;
        Ok(format!(
            "restart scheduled (countdown {countdown} s, job {})",
            job.id
        ))
    }

    /// `do_stop(args)`: not ported yet (actions).
    pub(crate) fn do_stop(&mut self, _args: &Obj) -> AResult<String> {
        Err(AgentError::NotPorted("actions::power::do_stop"))
    }

    /// `do_start(args)`: not ported yet (actions).
    pub(crate) fn do_start(&mut self, _args: &Obj) -> AResult<String> {
        Err(AgentError::NotPorted("actions::power::do_start"))
    }

    /// `do_backup(args)`: not ported yet (actions).
    pub(crate) fn do_backup(&mut self, _args: &Obj) -> AResult<String> {
        Err(AgentError::NotPorted("actions::power::do_backup"))
    }
}
