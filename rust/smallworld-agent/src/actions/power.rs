//! stop, start, restart, backup (mc_agent.py 1454-1541). Polkit pairs used: start/stop/restart
//! minecraft.service, start mc-backup.service (50-mc-ops.rules.j2).

use super::{Executor, countdown, flag, one_line};
use crate::jobs::{Job, rcon_command, wait_for_rcon};
use crate::notify;
use crate::systemd::systemctl;
use crate::types::{AResult, AgentError, Obj, RCON_WAIT_S, Value, get, truthy};
use serde_json::json;
use smallworld_core::fsx;
use smallworld_core::paths::{self, units};
use smallworld_core::pyjson::dumps_sorted;
use std::path::Path;
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

    /// `_stop_now(reason)`: the admin-stopped marker first (0644, so self-heal won't undo the stop), then
    /// `systemctl stop minecraft`.
    ///
    /// # Errors
    /// The marker's `OSError`, systemctl's error.
    pub(crate) fn stop_now(login: &str, reason: &str) -> AResult<String> {
        let mut record = Obj::new();
        record.insert(
            "since".into(),
            json!(crate::sys::utc_iso(crate::sys::now())),
        );
        record.insert("reason".into(), json!(reason));
        record.insert(
            "by".into(),
            json!(if login.is_empty() {
                "panel".to_string()
            } else {
                format!("panel ({login})")
            }),
        );
        let text = dumps_sorted(&Value::Object(record)) + "\n";
        fsx::replace_write(
            Path::new(paths::ADMIN_STOPPED_FILE),
            text.as_bytes(),
            Some(0o644),
        )?;
        systemctl(&["stop", units::MINECRAFT], 300.0)?;
        Ok("stopped (admin-stopped until start)".into())
    }

    /// `do_stop(args)`: now, or as a job (countdown, optional backup first).
    pub(crate) fn do_stop(&mut self, args: &Obj) -> AResult<String> {
        let default = json!("stopped from the panel");
        let raw = get(args, "reason");
        let reason = one_line(if truthy(raw) { raw } else { &default }, 200);
        let (countdown, backup) = (countdown(args)?, flag(args, "backup", false)?);
        let Some(panel) = self.panel.clone().filter(|_| countdown != 0 || backup) else {
            return Self::stop_now(&self.login, &reason);
        };
        let skip = flag(args, "skip_if_empty", true)?;
        let login = self.login.clone();
        let work = Box::new(move |job: &Arc<Job>| {
            if backup {
                backup_first(job);
            }
            job.set_step("stopping");
            Self::stop_now(&login, &reason)
        });
        let job = panel.jobs.start(
            &self.cmd_id,
            "stop",
            "Server stopping",
            &self.login,
            countdown,
            work,
            skip,
        )?;
        Ok(format!(
            "stop scheduled (countdown {countdown} s, job {})",
            job.id
        ))
    }

    /// `do_start(args)`: refused while a job runs or the box is fenced; removes the marker, then starts.
    pub(crate) fn do_start(&mut self, _args: &Obj) -> AResult<String> {
        if self.panel.as_ref().is_some_and(|p| p.jobs.busy().is_some()) {
            return Err(AgentError::Rejected(
                "start: another action is running".into(),
            ));
        }
        if notify::fenced() {
            return Err(AgentError::Rejected(
                "start: this box is fenced (mcctl migrate moved the world to another box)".into(),
            ));
        }
        fsx::unlink_missing_ok(Path::new(paths::ADMIN_STOPPED_FILE))?;
        systemctl(&["start", units::MINECRAFT], 300.0)?;
        Ok("started".into())
    }

    /// `do_backup(args)`: `systemctl start --no-block mc-backup`; with a panel, a thread waits for the oneshot
    /// (a blocking `start` on a running oneshot returns when it ends), then pushes the inventory.
    pub(crate) fn do_backup(&mut self, _args: &Obj) -> AResult<String> {
        systemctl(&["start", "--no-block", units::BACKUP], 300.0)?;
        if let Some(panel) = self.panel.clone() {
            let spawned = std::thread::Builder::new()
                .name("backup-wait".into())
                .spawn(move || {
                    match systemctl(&["start", units::BACKUP], 3600.0) {
                        Ok(()) | Err(AgentError::Os(_)) => {}
                        Err(e) => {
                            // Python: anything but OSError/SubprocessError kills the thread before changed().
                            eprintln!("Exception in thread backup-wait: {e}");
                            return;
                        }
                    }
                    (panel.changed)();
                });
            if let Err(e) = spawned {
                return Err(e.into());
            }
        }
        Ok("backup started".into())
    }
}
