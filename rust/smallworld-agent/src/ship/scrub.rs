//! The secret scrubber (mc_agent.py 640-725): `SECRET_PATTERNS` (8 regexes, 3 with look-around, rewritten for
//! the `regex` crate as patterns + manual boundary checks, pinned by vectors), `Scrubber` (known values first:
//! `known_secrets`, incl. the RCON password; then the shapes), `known_secrets`, `scrubber_for`. Owner: ship.
//!
//! A security control: SECURITY.md's claims must stay true word for word. Port what the code does (the
//! docstring's "the panel's GitHub token" isn't in `known_secrets`; installation tokens are caught by the
//! `gh[pousr]_` shape). Stub: until ported, `scrub` redacts everything (never leaks).

use crate::settings::Settings;

/// mc_agent.py `SECRET_SUFFIXES`.
pub const SECRET_SUFFIXES: [&str; 5] = ["_TOKEN", "_KEY", "_SECRET", "_PASSWORD", "_WEBHOOK_URL"];

pub struct Scrubber {
    known: Vec<String>,
}

impl Scrubber {
    /// `Scrubber(known)`: longest values first, empty and short ones dropped as Python does.
    pub fn new(known: Vec<String>) -> Self {
        Self { known }
    }

    /// `scrubber(text)`. Stub: everything is redacted.
    pub fn scrub(&self, _text: &str) -> String {
        let _ = &self.known;
        "[redacted: scrubber not yet ported]".into()
    }
}

/// `known_secrets(s, env)`. Stub.
pub fn known_secrets(_s: &Settings, _env: &crate::settings::Env) -> Vec<String> {
    Vec::new()
}
