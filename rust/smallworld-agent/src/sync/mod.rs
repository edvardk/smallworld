//! Whitelist -> Git (owner: **sync**; mc_panel.py 117-189, 291-501, 639-884): players.yml canonical form
//! (players.rs; must equal mcctl.schema.dump_players), the GitHub REST client (github.rs; contents + pulls, one
//! retry with a fresh token on 401), the sync round (round.rs: snapshot, expire after 24 h, PR check, open a
//! PR after BATCH_S, `sync_world` with worlds.py's `set_world_section`, `merge_sync`, `revert_live`) and the
//! Discord texts (alerts.rs; byte-exact `alerts.render`). Not ported yet.

pub mod alerts;
pub mod github;
pub mod players;
pub mod round;

use crate::types::{AResult, AgentError};

/// `Agent.panel_loop` (every PANEL_SYNC_S, runs until `stop`).
///
/// # Errors
/// `NotPorted`.
pub fn panel_loop(_daemon: &crate::daemon::Daemon) -> AResult<()> {
    Err(AgentError::NotPorted("sync::panel_loop"))
}
