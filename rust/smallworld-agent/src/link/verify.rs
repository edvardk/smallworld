//! mc_agent.py `Nonces`, `sign`, `verify` (1112-1199). Owner: link. Stub.
//!
//! - HMAC-SHA256 hex over the payload text, compared in constant time (`hmac::Mac::verify_slice` after hex
//!   decoding, or a constant-time compare of the lowercased hex), sig lowercased first.
//! - Field shapes; `iat` (ms, int or float, not bool) within ±60 s; id `[A-Za-z0-9_-]{1,64}`; nonce
//!   `[A-Za-z0-9_-]{8,64}`; payload ≤ 8192 code points.
//! - Nonces: 10 min TTL, persisted (`nonces.json`, `tmp.write_text` + `os.replace`); an unreadable store refuses
//!   everything for 600 s and audits `fail-closed`; an unwritable one refuses the command.

use crate::types::{AResult, AgentError, Command, Value};
use std::path::PathBuf;

/// mc_agent.py `MAX_AGE_S`.
pub const MAX_AGE_S: f64 = 60.0;
/// mc_agent.py `NONCE_TTL_S`.
pub const NONCE_TTL_S: f64 = 600.0;

/// mc_agent.py `Nonces`.
pub struct Nonces {
    pub path: Option<PathBuf>,
}

impl Nonces {
    /// `Nonces(path, now)`: loads the store (fail closed on an unreadable one).
    pub fn new(path: Option<PathBuf>, _now: f64) -> Self {
        Self { path }
    }

    /// `use(nonce, now)`: false if seen already.
    ///
    /// # Errors
    /// `Rejected` while failing closed or when the store can't be written.
    pub fn use_nonce(&mut self, _nonce: &str, _now: f64) -> AResult<bool> {
        Err(AgentError::NotPorted("link::verify::Nonces::use"))
    }
}

/// `sign(key, payload)`: lowercase hex HMAC-SHA256.
pub fn sign(_key: &[u8], _payload: &str) -> String {
    String::new()
}

/// `verify(message, key, nonces, now)`: the command, or why not.
///
/// # Errors
/// `Rejected` with Python's text.
pub fn verify(_message: &Value, _key: &[u8], _nonces: &mut Nonces, _now: f64) -> AResult<Command> {
    Err(AgentError::NotPorted("link::verify::verify"))
}
