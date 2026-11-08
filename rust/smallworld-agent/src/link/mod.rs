//! The Worker link (owner: **link**): HTTP to the Worker (`request`, `post_json`, `IngestError`; mc_agent.py
//! 259-298), the WebSocket client (ws.rs; 1972-2149), command verification and nonces (verify.rs; 1112-1199),
//! `handle_command` (command.rs; 1946-1969) and the control loop (control.rs; 2486-2566).

pub mod command;
pub mod control;
pub mod verify;
pub mod ws;

use crate::http::HttpError;
use crate::settings::Settings;
use crate::types::{USER_AGENT, Value};
use std::time::Duration;

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
    /// `Authorization: Bearer`, `User-Agent: smallworld mc-agent` (and `Content-Type` with a body), a 1 MiB reply
    /// cap; the reply's JSON (`{}` for an empty body).
    ///
    /// # Errors
    /// `IngestError("{method} {path}: HTTP {code}" | "{method} {path}: {class}" | "...: reply isn't JSON")`;
    /// never the token or a body.
    pub fn request(
        &self,
        method: &str,
        path: &str,
        body: Option<&[u8]>,
        content_type: &str,
        timeout_s: f64,
    ) -> Result<Value, IngestError> {
        let url = format!("{}{path}", self.settings.ingest_url);
        let auth = format!("Bearer {}", self.settings.token);
        let mut headers = vec![("Authorization", auth.as_str()), ("User-Agent", USER_AGENT)];
        if body.is_some() {
            headers.push(("Content-Type", content_type));
        }
        let resp = crate::http::request(
            method,
            &url,
            &headers,
            body,
            Duration::from_secs_f64(timeout_s),
            crate::http::MAX_REPLY,
        )
        .map_err(|e| match e {
            HttpError::Status(code, _) => IngestError {
                message: format!("{method} {path}: HTTP {code}"),
                status: Some(code),
            },
            HttpError::Transport(class) => IngestError {
                message: format!("{method} {path}: {class}"),
                status: None,
            },
        })?;
        let raw: &[u8] = if resp.body.is_empty() {
            b"{}"
        } else {
            &resp.body
        };
        std::str::from_utf8(raw)
            .ok()
            .and_then(smallworld_core::pyjson::loads)
            .ok_or_else(|| IngestError {
                message: format!("{method} {path}: reply isn't JSON"),
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
