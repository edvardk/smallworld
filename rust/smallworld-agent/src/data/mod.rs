//! The data plane (owner: **data**): metrics + spool (metrics.rs; mc_agent.py 299-595, health.py's parsers in
//! health.rs), the persistent RCON shared by metrics and `status` (persistent_rcon.rs; mc_rcon.PersistentRcon),
//! inventory (inventory.rs; 871-937, 2405-2431), the console tail (tail.rs; 940-1005), /modpack client codes
//! (client_codes.rs; 2153-2274) and the rejected-logins feed (panel/rejected.rs). Not ported yet.

pub mod client_codes;
pub mod health;
pub mod inventory;
pub mod metrics;
pub mod persistent_rcon;
pub mod tail;

use crate::types::{AResult, AgentError};

/// `Agent.metrics_loop` (runs until `stop`).
///
/// # Errors
/// `NotPorted`.
pub fn metrics_loop(_daemon: &crate::daemon::Daemon) -> AResult<()> {
    Err(AgentError::NotPorted("data::metrics_loop"))
}

/// `Agent.inventory_loop` (runs until `stop`).
///
/// # Errors
/// `NotPorted`.
pub fn inventory_loop(_daemon: &crate::daemon::Daemon) -> AResult<()> {
    Err(AgentError::NotPorted("data::inventory_loop"))
}
