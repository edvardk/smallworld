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
        let raw = self.request_body(method, path, body, content_type, timeout_s)?;
        std::str::from_utf8(&raw)
            .ok()
            .and_then(smallworld_core::pyjson::loads)
            .ok_or_else(|| IngestError {
                message: format!("{method} {path}: reply isn't JSON"),
                status: None,
            })
    }

    /// `request`'s reply body before `json.loads` (`b"{}"` for an empty one), for a caller that reads the reply
    /// in its own order (telemetry: the verdicts' player order reaches verdicts.tsv).
    ///
    /// # Errors
    /// As `request`, except "reply isn't JSON" (the caller's to decide).
    pub fn request_body(
        &self,
        method: &str,
        path: &str,
        body: Option<&[u8]>,
        content_type: &str,
        timeout_s: f64,
    ) -> Result<Vec<u8>, IngestError> {
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
        Ok(if resp.body.is_empty() {
            b"{}".to_vec()
        } else {
            resp.body
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    fn link_to(reply: &'static str) -> (Link, std::thread::JoinHandle<String>) {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", l.local_addr().unwrap());
        let h = std::thread::spawn(move || {
            let (mut s, _) = l.accept().unwrap();
            let mut buf = Vec::new();
            let mut c = [0u8; 4096];
            while !buf.windows(4).any(|w| w == b"\r\n\r\n") {
                let n = s.read(&mut c).unwrap();
                buf.extend_from_slice(&c[..n]);
            }
            s.write_all(reply.as_bytes()).unwrap();
            String::from_utf8_lossy(&buf).into_owned()
        });
        let t = "t".repeat(32);
        let env = [
            ("INGEST_URL", url.as_str()),
            ("AGENT_TOKEN", t.as_str()),
            ("COMMAND_HMAC_KEY", t.as_str()),
        ]
        .iter()
        .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
        .collect();
        (Link::new(Settings::from_env(&env).unwrap()), h)
    }

    #[test]
    fn requests_carry_the_token_and_errors_never_do() {
        let (link, h) =
            link_to("HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
        assert_eq!(
            link.request("GET", "/agent/commands", None, "application/json", 5.0),
            Ok(serde_json::json!({}))
        );
        let head = h.join().unwrap().to_ascii_lowercase();
        assert!(head.contains(&format!("authorization: bearer {}", "t".repeat(32))));
        assert!(head.contains("user-agent: smallworld mc-agent"));
        assert!(!head.contains("content-type"));

        let (link, h) = link_to(
            "HTTP/1.1 401 Unauthorized\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}",
        );
        let e = link
            .post_json("/ingest/job", &serde_json::json!({}))
            .unwrap_err();
        assert_eq!(
            e,
            IngestError {
                message: "POST /ingest/job: HTTP 401".into(),
                status: Some(401)
            }
        );
        h.join().unwrap();

        let (link, h) =
            link_to("HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\nnope");
        let e = link
            .request("GET", "/x", None, "application/json", 5.0)
            .unwrap_err();
        assert_eq!(e.message, "GET /x: reply isn't JSON");
        h.join().unwrap();
    }
}
