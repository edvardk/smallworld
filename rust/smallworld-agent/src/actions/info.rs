//! status, selftest, lookup, console, tail, cancel, player_message (mc_agent.py 1419-1452, 1543-1565,
//! 1662-1688). Owner: actions. Stubs: each needs a piece another module owns (`Executor.sample` from data,
//! `Panel.selftest`/`lookup` from misc, `Executor.tail` from data, `Jobs::cancel` (ported), `Telemetry`).

use super::Executor;
use crate::types::{AResult, AgentError, Obj};

impl Executor {
    pub(crate) fn do_status(&mut self, _args: &Obj) -> AResult<String> {
        Err(AgentError::NotPorted("actions::info::do_status"))
    }
    pub(crate) fn do_selftest(&mut self, _args: &Obj) -> AResult<String> {
        Err(AgentError::NotPorted("actions::info::do_selftest"))
    }
    pub(crate) fn do_lookup(&mut self, _args: &Obj) -> AResult<String> {
        Err(AgentError::NotPorted("actions::info::do_lookup"))
    }
    pub(crate) fn do_console(&mut self, _args: &Obj) -> AResult<String> {
        Err(AgentError::NotPorted("actions::info::do_console"))
    }
    pub(crate) fn do_tail(&mut self, _args: &Obj) -> AResult<String> {
        Err(AgentError::NotPorted("actions::info::do_tail"))
    }
    pub(crate) fn do_cancel(&mut self, _args: &Obj) -> AResult<String> {
        Err(AgentError::NotPorted("actions::info::do_cancel"))
    }
    pub(crate) fn do_player_message(&mut self, _args: &Obj) -> AResult<String> {
        Err(AgentError::NotPorted("actions::info::do_player_message"))
    }
}
