//! mc_panel.py `parse_players`, `dump_players`, `apply_entries` (117-189). Owner: sync. Stub.

use crate::types::{AResult, AgentError, Obj};

/// `parse_players(text)`: `{whitelist: [...], ops: [...]}` from the canonical form.
///
/// # Errors
/// `Value` with Python's text for a file not in canonical form; `NotPorted` until ported.
pub fn parse_players(_text: &str) -> AResult<Obj> {
    Err(AgentError::NotPorted("sync::players::parse_players"))
}

/// `dump_players(players)`: the canonical text. Stub.
pub fn dump_players(_players: &Obj) -> String {
    String::new()
}
