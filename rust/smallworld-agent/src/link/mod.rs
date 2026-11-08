//! The Worker link (owner: **link**): HTTP to the Worker (`request`, `post_json`, `IngestError`; mc_agent.py
//! 259-298), the WebSocket client (ws.rs; 1972-2149), command verification and nonces (verify.rs; 1112-1199),
//! `handle_command` (command.rs; 1946-1969) and the control loop (control.rs; 2493-2566). Not ported yet.

pub mod command;
pub mod control;
pub mod verify;
pub mod ws;

use crate::settings::Settings;
use crate::types::{USER_AGENT, Value};

/// mc_agent.py `IngestError`: never carries the token or a body.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IngestError {
    pub message: String,
    pub status: Option<u16>,
}

impl std::fmt::Display for IngestError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

/// The Worker's URL, token and HTTP options (cloned into every loop).
#[derive(Clone)]
pub struct Link {
    pub settings: Settings,
}

impl Link {
    pub fn new(settings: Settings) -> Self {
        Self { settings }
    }

    /// `request(s, method, path, body, content_type, timeout)`: one request to the Worker with
    /// `Authorization: Bearer`, `User-Agent: smallworld mc-agent`, a 1 MiB reply cap; the reply's JSON (`{}`
    /// for an empty body). Use crate::http::request.
    ///
    /// # Errors
    /// `IngestError("{method} {path}: HTTP {code}" | "{method} {path}: {class}" | "...: reply isn't JSON")`.
    pub fn request(
        &self,
        method: &str,
        path: &str,
        _body: Option<&[u8]>,
        _content_type: &str,
        _timeout_s: f64,
    ) -> Result<Value, IngestError> {
        let _ = USER_AGENT;
        Err(IngestError {
            message: format!("{method} {path}: not yet ported to Rust"),
            status: None,
        })
    }

    /// `post_json(s, path, payload)`: compact JSON (`separators=(",", ":")`).
    ///
    /// # Errors
    /// As `request`.
    pub fn post_json(&self, path: &str, payload: &Value) -> Result<Value, IngestError> {
        let body = smallworld_core::pyjson::dumps_compact(payload);
        self.request(
            "POST",
            path,
            Some(body.as_bytes()),
            "application/json",
            20.0,
        )
    }
}
