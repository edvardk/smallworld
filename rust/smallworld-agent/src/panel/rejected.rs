//! mc_panel.py `RejectedLogins` and `mask_address` (panel:885-980): the last REJECTED_KEEP whitelist rejections,
//! in the agent's state dir (rejected.json), fed from the minecraft journal (`observe`, called by ship's log
//! loop via the daemon) and trimmed by the panel (`drop`). Owner: **data**. Not ported yet.

use crate::types::{AResult, AgentError, Obj};
use std::path::PathBuf;

pub struct RejectedLogins {
    path: PathBuf,
}

impl RejectedLogins {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    pub fn path(&self) -> &std::path::Path {
        &self.path
    }

    /// `load()`: the kept entries (name, uuid, count, first, last, address), newest last.
    ///
    /// # Errors
    /// `NotPorted` until data ports it.
    pub fn load(&self) -> AResult<Vec<Obj>> {
        Err(AgentError::NotPorted(
            "panel::rejected::RejectedLogins::load",
        ))
    }

    /// `drop(name)`: removes a name (case-insensitive); true if it was there.
    ///
    /// # Errors
    /// `NotPorted` until data ports it.
    pub fn drop_name(&self, _name: &str) -> AResult<bool> {
        Err(AgentError::NotPorted(
            "panel::rejected::RejectedLogins::drop",
        ))
    }

    /// `observe(lines, now)`: records rejections found in console lines; returns the names newly seen (for the
    /// Discord alert, `alerts::rejected_alert`).
    ///
    /// # Errors
    /// `NotPorted` until data ports it.
    pub fn observe(&self, _lines: &[String], _now: f64) -> AResult<Vec<String>> {
        Err(AgentError::NotPorted(
            "panel::rejected::RejectedLogins::observe",
        ))
    }
}

impl RejectedLogins {
    /// `feed(lines)`: (ts ms, message) lines of minecraft.service; true if the list changed. (ship's logs loop
    /// calls it; data's port defines the same signature.)
    ///
    /// # Errors
    /// `NotPorted` until data ports it.
    pub fn feed(&self, _lines: &[(i64, String)]) -> AResult<bool> {
        Err(AgentError::NotPorted(
            "panel::rejected::RejectedLogins::feed",
        ))
    }
}

/// mc_panel.py `mask_address(addr)`: "/203.0.113.7:51234" -> "203.0.113.x"; IPv6 -> its /48; else "".
/// Not ported yet (data); returns "" (reveals nothing) until then.
pub fn mask_address(_addr: &str) -> String {
    String::new()
}
