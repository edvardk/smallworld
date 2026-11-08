//! `do_restore` (mc_agent.py 1690-1745): a snapshot from `snapshots.json`, the archive-tag check, then a job that
//! starts `mc-restore@<id>.service` (7200 s; polkit allows `start` on `mc-restore@[0-9a-f]{8,64}.service`) while
//! a follower (world.rs `follow_steps`) reports its `STEP` lines. Owner: actions.

use super::world::{follow_steps, worlds_view};
use super::{Executor, countdown, flag};
use crate::jobs::{Event, Job};
use crate::panel::SNAPSHOT_ID;
use crate::panel::plugins::read_snapshots;
use crate::systemd::{systemctl, unit_steps};
use crate::types::{
    AResult, AgentError, Obj, RESTORE_TIMEOUT_S, Value, get, py_iter, py_str, truthy,
};
use crate::worlds::{ARCHIVE_TAG, tag_value};
use std::sync::Arc;

impl Executor {
    /// `do_restore(args)`.
    pub(crate) fn do_restore(&mut self, args: &Obj) -> AResult<String> {
        let panel = self.need_panel("restore")?;
        let Some(snap) = get(args, "snapshot")
            .as_str()
            .filter(|s| SNAPSHOT_ID.is_match(s))
            .map(str::to_string)
        else {
            return Err(AgentError::Rejected(
                "restore: snapshot must be a restic snapshot id (8-64 hex)".into(),
            ));
        };
        let known = read_snapshots(panel.snapshots_path())?;
        // `next(s for s in known if s["id"].startswith(snap) or s.get("short_id") == snap)`: an id that
        // passed read_snapshots' `str()` check but isn't a string (a number) crashes Python here
        let mut found = None;
        for s in known {
            let by_id = match get(&s, "id") {
                Value::String(id) => id.starts_with(&snap),
                other => {
                    return Err(AgentError::Crash(format!(
                        "AttributeError: '{}' object has no attribute 'startswith'",
                        crate::worlds::py_type(other)
                    )));
                }
            };
            if by_id || get(&s, "short_id").as_str() == Some(snap.as_str()) {
                found = Some(s);
                break;
            }
        }
        let Some(found) = found else {
            return Err(AgentError::Rejected(format!(
                "restore: {snap} isn't in the snapshot list (refresh it with a backup)"
            )));
        };
        let tags_v = get(&found, "tags");
        let tags: Vec<String> = if truthy(tags_v) {
            py_iter(tags_v)?.iter().map(py_str).collect()
        } else {
            Vec::new()
        };
        let archived = if tags.iter().any(|t| t == ARCHIVE_TAG) {
            tag_value(&tags, "world:").map(str::to_string)
        } else {
            None
        };
        if let Some(a) = archived.filter(|a| !a.is_empty()) {
            let active = worlds_view(&panel)?.active;
            if active.as_str() != Some(a.as_str()) {
                return Err(AgentError::Rejected(format!(
                    "restore: {snap} is the archive of the world {a}; switch in the Worlds tab"
                )));
            }
        }
        let cd = countdown(args)?;
        let skip = flag(args, "skip_if_empty", true)?;
        let unit = format!("mc-restore@{snap}.service");
        let (p, s) = (panel.clone(), snap.clone());
        let work = Box::new(move |job: &Arc<Job>| -> AResult<String> {
            let since = crate::sys::local_since(crate::sys::now());
            let done = Arc::new(Event::default());
            job.set_step(&format!("starting {unit}"));
            follow_steps(job.clone(), unit.clone(), since.clone(), done.clone());
            match systemctl(&["start", &unit], RESTORE_TIMEOUT_S) {
                Ok(()) => done.set(),
                Err(AgentError::Os(c)) if c == "CalledProcessError" => {
                    let steps = unit_steps(&unit, &since);
                    done.set();
                    let last = steps?
                        .last()
                        .cloned()
                        .unwrap_or_else(|| format!("see journalctl -u {unit}"));
                    return Err(AgentError::Action(format!("restore failed ({last})")));
                }
                Err(e) => {
                    done.set();
                    return Err(e);
                }
            }
            (p.changed)();
            let steps = unit_steps(&unit, &since)?;
            Ok(steps
                .last()
                .cloned()
                .unwrap_or_else(|| format!("restored {s}")))
        });
        let job = panel.jobs.start(
            &self.cmd_id,
            "restore",
            "Restore",
            &self.login,
            cd,
            work,
            skip,
        )?;
        Ok(format!(
            "restore of {snap} scheduled (countdown {cd} s, job {})",
            job.id
        ))
    }
}
