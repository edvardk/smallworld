//! Journal shipping (owner: **ship**; mc_agent.py 596-870): the journal since the saved cursor, streamed with
//! only the kept fields (journal.rs), filtered (`for_d1`, `for_archive`), scrubbed (scrub.rs), shipped as gzipped
//! NDJSON to R2 (`POST /ingest/archive?batch=`) and capped per UTC day to D1 (`POST /ingest/logs`); the cursor
//! moves only once both are delivered (archive.rs, the logs loop). Not ported yet.

pub mod archive;
pub mod journal;
pub mod scrub;

use crate::types::{AResult, AgentError};

/// The logs loop's entry (`Agent.logs_loop`, runs until `stop`; feeds `RejectedLogins::observe`).
///
/// # Errors
/// `NotPorted`.
pub fn logs_loop(_daemon: &crate::daemon::Daemon) -> AResult<()> {
    Err(AgentError::NotPorted("ship::logs_loop"))
}
