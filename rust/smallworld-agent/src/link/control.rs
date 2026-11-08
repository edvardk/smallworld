//! `Agent.answer`, `ws_session`, `poll_once`, `control_loop` (mc_agent.py 2493-2566). Owner: link.
//!
//! The WebSocket while it holds (hello, a text "ping" + a protocol ping after 45 s idle, dead after 120 s,
//! `{"type":"command"}` -> `{"type":"result",...}`), else `GET /agent/commands` every POLL_S (at most 20 per
//! poll) -> one `POST /agent/results`; the socket is tried again 5 s after a session that was up (or closed
//! cleanly), 300 s after a refusal or a failed round. Same requests as Python, no more: one poll after a lost
//! session, none while the socket holds.

use super::IngestError;
use super::command::handle_command;
use super::ws::{
    RecvError, WS_CONNECT_TIMEOUT_S, WS_DEAD_S, WS_PING_S, WS_RECV_TIMEOUT_S, WebSocket, WsError,
    WsSender,
};
use crate::daemon::{Daemon, lock, panic_text};
use crate::types::{AResult, AgentError, USER_AGENT, VERSION, Value};
use smallworld_core::pyjson;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};

/// How a session or a poll ended badly, by the `except` clause that catches it in `control_loop`.
#[derive(Debug)]
pub enum ControlError {
    /// `except (OSError, WebSocketError)` around the session.
    Ws(WsError),
    /// `except IngestError` around the poll.
    Ingest(IngestError),
    /// `except Exception` around the round.
    Other(AgentError),
}

impl From<AgentError> for ControlError {
    fn from(e: AgentError) -> Self {
        ControlError::Other(e)
    }
}

/// `{"type": ..., **result}` as `json.dumps` writes it (default separators, `type` first, then the result's
/// `id`, `ok`, `output`/`error`, `finished_at`).
fn tagged(kind: &str, result: &Value) -> String {
    let mut items = vec![format!("\"type\": {}", pyjson::string(kind, true))];
    if let Value::Object(o) = result {
        for k in ["id", "ok", "output", "error", "finished_at"] {
            if let Some(v) = o.get(k) {
                items.push(format!(
                    "{}: {}",
                    pyjson::string(k, true),
                    pyjson::dumps_sorted(v)
                ));
            }
        }
    }
    format!("{{{}}}", items.join(", "))
}

/// `Agent.answer(message)`.
///
/// # Errors
/// What `handle_command` doesn't catch.
pub fn answer(d: &Daemon, message: &Value) -> AResult<Value> {
    let mut nonces = lock(&d.nonces);
    let mut executor = lock(&d.executor);
    handle_command(message, &d.settings, &mut nonces, &mut executor)
}

/// `Agent.ws_session()`: one session until the server closes it, it goes quiet or `stop`.
///
/// # Errors
/// `Ws` for a refused/failed connection or a dead session; `Other` for what `answer` doesn't catch (except a
/// `ValueError`, which the session swallows like Python's `contextlib.suppress(ValueError)`).
pub fn ws_session(d: &Daemon) -> Result<(), ControlError> {
    let url = d
        .settings
        .ingest_url
        .replacen("https://", "wss://", 1)
        .replacen("http://", "ws://", 1)
        + "/agent/ws";
    let bearer = format!("Bearer {}", d.settings.token);
    let mut ws = WebSocket::connect(
        &url,
        &[("Authorization", &bearer), ("User-Agent", USER_AGENT)],
        WS_CONNECT_TIMEOUT_S,
    )
    .map_err(ControlError::Ws)?;
    let handle = ws.handle();
    *lock(&d.ws) = Some(handle.clone());
    d.ws_connected.store(true, Ordering::SeqCst);
    d.ws_established.store(true, Ordering::SeqCst);
    d.warn.ok("websocket");
    let result = (|| {
        let mut last_ping = Instant::now();
        handle.send_text(&format!(
            "{{\"type\": \"hello\", \"agent\": {}}}",
            pyjson::string(VERSION, true)
        ));
        while !d.stop.is_set() {
            let text = match ws.recv(Duration::from_secs_f64(WS_RECV_TIMEOUT_S)) {
                Ok(Some(t)) => t,
                Ok(None) => return Ok(()),
                Err(RecvError::Ws(e)) => return Err(ControlError::Ws(e)),
                Err(RecvError::Timeout) => {
                    let now = Instant::now();
                    let last_rx = ws.last_rx();
                    if now.duration_since(last_rx).as_secs_f64() > WS_DEAD_S {
                        return Err(ControlError::Ws(WsError::ws(format!(
                            "nothing received for {} s",
                            pyjson::float_repr(WS_DEAD_S)
                        ))));
                    }
                    if now.duration_since(last_rx.max(last_ping)).as_secs_f64() >= WS_PING_S {
                        handle.send_text("ping");
                        handle.ping();
                        last_ping = now;
                    }
                    continue;
                }
            };
            if text == "pong" {
                continue;
            }
            let Some(msg) = pyjson::loads(&text) else {
                continue;
            };
            if msg.get("type").and_then(Value::as_str) == Some("command") {
                match answer(d, &msg) {
                    Ok(result) => {
                        handle.send_text(&tagged("result", &result));
                    }
                    Err(AgentError::Value(..)) => {} // contextlib.suppress(ValueError)
                    Err(e) => return Err(ControlError::Other(e)),
                }
            }
        }
        Ok(())
    })();
    d.ws_connected.store(false, Ordering::SeqCst);
    *lock(&d.ws) = None;
    ws.close();
    result
}

/// `reply.get("commands", [])[:20]` with Python's view of odd replies: a string slices into characters; a
/// dict, number, bool or null raises `TypeError`.
fn first_commands(reply: &Value) -> AResult<Vec<Value>> {
    let Value::Object(o) = reply else {
        return Ok(Vec::new());
    };
    match o.get("commands") {
        None => Ok(Vec::new()),
        Some(Value::Array(a)) => Ok(a.iter().take(20).cloned().collect()),
        Some(Value::String(s)) => Ok(s
            .chars()
            .take(20)
            .map(|c| Value::String(c.into()))
            .collect()),
        Some(Value::Object(_)) => Err(AgentError::Crash(
            "TypeError: unhashable type: 'slice'".into(),
        )),
        Some(other) => Err(AgentError::Crash(format!(
            "TypeError: '{}' object is not subscriptable",
            crate::worlds::py_type(other)
        ))),
    }
}

/// `Agent.poll_once()`: the queued commands, answered; their results in one POST.
///
/// # Errors
/// `Ingest` when the Worker can't be reached; `Other` for what `answer` doesn't catch (the poll's results are
/// then lost, as in Python).
pub fn poll_once(d: &Daemon) -> Result<usize, ControlError> {
    let reply = d
        .link
        .request("GET", "/agent/commands", None, "application/json", 20.0)
        .map_err(ControlError::Ingest)?;
    let commands = first_commands(&reply)?;
    let mut results = Vec::with_capacity(commands.len());
    for c in &commands {
        results.push(answer(d, c)?);
    }
    if !results.is_empty() {
        d.link
            .post_json("/agent/results", &serde_json::json!({"results": results}))
            .map_err(ControlError::Ingest)?;
    }
    Ok(results.len())
}

/// One round of the control loop; returns when to try the socket next.
fn control_round(d: &Daemon, next_ws: Instant) -> Result<(Instant, bool), ControlError> {
    let mut next_ws = next_ws;
    if Instant::now() >= next_ws {
        d.ws_established.store(false, Ordering::SeqCst);
        match ws_session(d) {
            Ok(()) => return Ok((Instant::now() + Duration::from_secs(5), true)),
            Err(ControlError::Ws(e)) => {
                d.warn.fail(
                    "websocket",
                    &format!(
                        "unavailable ({}), polling every {} s",
                        e.class, d.settings.poll_s
                    ),
                    false,
                );
                let up = d.ws_established.load(Ordering::SeqCst);
                next_ws = Instant::now()
                    + if up {
                        Duration::from_secs(5)
                    } else {
                        Duration::from_mins(5)
                    };
            }
            Err(other) => return Err(other),
        }
    }
    match poll_once(d) {
        Ok(_) => d.warn.ok("commands"),
        Err(ControlError::Ingest(e)) => {
            d.warn.fail("commands", &format!("poll failed: {e}"), false);
        }
        Err(other) => return Err(other),
    }
    Ok((next_ws, false))
}

/// `Agent.control_loop()` (runs until `stop`).
///
/// # Errors
/// None in practice (every round's failure is logged, as in Python); the signature is the daemon's `LoopFn`.
#[allow(clippy::unnecessary_wraps)] // the daemon's `LoopFn` signature
pub fn control_loop(d: &Daemon) -> AResult<()> {
    let mut next_ws = Instant::now();
    while !d.stop.is_set() {
        let round = catch_unwind(AssertUnwindSafe(|| control_round(d, next_ws)));
        let failed = match round {
            Ok(Ok((next, skip_wait))) => {
                next_ws = next;
                if skip_wait {
                    continue; // a clean close: straight back to the top (poll once, then reconnect)
                }
                None
            }
            Ok(Err(ControlError::Other(e))) => Some(format!("{}: {e}", e.class_name())),
            Ok(Err(ControlError::Ws(e))) => Some(format!("{}: {e}", e.class)),
            Ok(Err(ControlError::Ingest(e))) => Some(format!("IngestError: {e}")),
            Err(p) => Some(format!("panic: {}", panic_text(&*p))),
        };
        if let Some(text) = failed {
            d.warn.fail("control", &text, false);
            next_ws = Instant::now() + Duration::from_mins(5);
        }
        #[allow(clippy::cast_precision_loss)]
        d.stop.wait(d.settings.poll_s as f64);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn result_messages_put_type_first() {
        let r = json!({"id": "c1", "ok": true, "output": "é", "finished_at": 5});
        assert_eq!(
            tagged("result", &r),
            r#"{"type": "result", "id": "c1", "ok": true, "output": "\u00e9", "finished_at": 5}"#
        );
    }

    #[test]
    fn odd_command_lists() {
        assert_eq!(first_commands(&json!([1])).unwrap(), Vec::<Value>::new());
        assert_eq!(first_commands(&json!({})).unwrap(), Vec::<Value>::new());
        let many: Vec<Value> = (0..30).map(|i| json!(i)).collect();
        assert_eq!(
            first_commands(&json!({"commands": many})).unwrap().len(),
            20
        );
        assert_eq!(
            first_commands(&json!({"commands": "ab"})).unwrap(),
            vec![json!("a"), json!("b")]
        );
        assert!(first_commands(&json!({"commands": null})).is_err());
        assert!(first_commands(&json!({"commands": {}})).is_err());
    }
}
