//! `Agent.ws_session`, `poll_once`, `control_loop` (mc_agent.py 2493-2566): the WebSocket while it holds
//! (hello, "ping" + protocol ping after 45 s idle, dead after 120 s, `{"type":"command"}` ->
//! `{"type":"result",...}`), else `GET /agent/commands` every POLL_S (at most 20 per poll) ->
//! `POST /agent/results`; WS retry 5 s after a session that was up, 300 s after a refusal. Owner: link. Stub.

use crate::types::{AResult, AgentError};

/// The control loop's entry (runs until `stop`).
///
/// # Errors
/// `NotPorted`.
pub fn control_loop(_daemon: &crate::daemon::Daemon) -> AResult<()> {
    Err(AgentError::NotPorted("link::control::control_loop"))
}
