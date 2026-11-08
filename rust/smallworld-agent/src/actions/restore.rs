//! `do_restore` (mc_agent.py 1690-1745): a snapshot from `snapshots.json`, the archive-tag check, then a job that
//! starts `mc-restore@<id>.service` (7200 s) while a follower reports its `STEP` lines. Owner: actions. Stub.
//! The world job's follower (world.rs `follow_steps`) is the same loop; reuse it.

use super::Executor;
use crate::types::{AResult, AgentError, Obj};

impl Executor {
    pub(crate) fn do_restore(&mut self, _args: &Obj) -> AResult<String> {
        Err(AgentError::NotPorted("actions::restore::do_restore"))
    }
}
