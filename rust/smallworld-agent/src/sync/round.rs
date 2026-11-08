//! mc_panel.py `merge_sync` (291-312), `sync_pending` (722-814), `sync_world` (834-882), `revert_live`,
//! `expire`. Works on a snapshot of pending.json and merges back under the lock (panel/pending.rs `update`).
//! Owner: sync. Stub.

use crate::types::{AResult, AgentError, Obj};

/// `merge_sync(current, snap, done)`.
///
/// # Errors
/// `NotPorted`.
pub fn merge_sync(_current: &Obj, _snap: &Obj, _done: &Obj) -> AResult<Obj> {
    Err(AgentError::NotPorted("sync::round::merge_sync"))
}
