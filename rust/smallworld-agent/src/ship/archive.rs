//! The logs state (`$STATE/logs.json`: `{cursor, day, rows}`), the gzip NDJSON archive (`flate2`, rust backend)
//! and D1 rows with the per-day cap (`select_rows`), the round (`ship_logs`, up to `LOG_CATCHUP_BATCHES` batches
//! back to back). Owner: ship. Stub.

use crate::types::{AResult, AgentError, Obj};

/// `gzip` of the NDJSON lines (Python: `gzip.compress(..., mtime=0)` semantics where it matters: the Worker
/// only gunzips). Stub.
///
/// # Errors
/// `NotPorted`.
pub fn archive_body(_entries: &[Obj]) -> AResult<Vec<u8>> {
    Err(AgentError::NotPorted("ship::archive::archive_body"))
}
