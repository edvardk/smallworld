//! `ConsoleTail` (mc_agent.py 940-1005): the panel's live console. While a viewer asks for it (a signed `tail`,
//! renewed while the Console tab is open), follows minecraft.service's journal (`journalctl -f ... -o json`) and
//! sends its lines, scrubbed with ship's `Scrubber` (built per run, like `scrubber_for`), over link's
//! `WsSender` in messages of at most `TAIL_BATCH` lines. One thread at a time; it ends when the time is up, the
//! agent stops, journalctl ends or the WebSocket goes away.

use crate::jobs::Event;
use crate::link::ws::WsSender;
use crate::ship::journal::{LOG_ROW_MAX_CHARS, journal_entry};
use crate::ship::scrub::Scrubber;
use crate::types::{AResult, AgentError, Obj, Value};
use serde_json::json;
use smallworld_core::pystr::prefix_cp;
use std::io::Read;
use std::process::{Command, Stdio};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::{Arc, Mutex, PoisonError};
use std::thread::JoinHandle;
use std::time::Duration;

/// mc_agent.py `TAIL_MAX_S`.
pub const TAIL_MAX_S: i64 = 180;
/// mc_agent.py `TAIL_BATCH`.
pub const TAIL_BATCH: usize = 50;

/// The current WebSocket session (`agent.ws`; None while polling).
pub type WsFn = Arc<dyn Fn() -> Option<Arc<dyn WsSender>> + Send + Sync>;
/// `scrubber_for(s)`.
pub type ScrubberFn = Arc<dyn Fn() -> Scrubber + Send + Sync>;

struct Inner {
    until: f64,
    thread: Option<JoinHandle<()>>,
}

/// `ConsoleTail`.
pub struct ConsoleTail {
    ws: WsFn,
    scrubber: ScrubberFn,
    stop: Arc<Event>,
    clock: Arc<dyn Fn() -> f64 + Send + Sync>,
    argv: Vec<String>,
    inner: Arc<Mutex<Inner>>,
}

/// `ConsoleTail.argv()`.
pub fn argv() -> Vec<String> {
    [
        "journalctl",
        "-f",
        "-n",
        "50",
        "-o",
        "json",
        "--no-pager",
        "--unit=minecraft.service",
        "--output-fields=MESSAGE,PRIORITY",
    ]
    .iter()
    .map(|s| (*s).to_string())
    .collect()
}

/// `ConsoleTail.line(raw)`: {ts, prio, msg[:LOG_ROW_MAX_CHARS]} of one journal line, or None.
pub fn line(raw: &[u8]) -> Option<Obj> {
    let raw = raw.strip_prefix(b"\xef\xbb\xbf").unwrap_or(raw);
    let text = std::str::from_utf8(raw).ok()?;
    let Some(Value::Object(data)) = smallworld_core::pyjson::loads(text) else {
        return None;
    };
    let e = journal_entry(&data)?;
    let msg = e.get("msg").and_then(Value::as_str).unwrap_or("");
    let mut out = Obj::new();
    out.insert("ts".into(), e.get("ts").cloned().unwrap_or(Value::Null));
    out.insert("prio".into(), e.get("prio").cloned().unwrap_or(Value::Null));
    out.insert("msg".into(), json!(prefix_cp(msg, LOG_ROW_MAX_CHARS)));
    Some(out)
}

impl ConsoleTail {
    pub fn new(ws: WsFn, scrubber: ScrubberFn, stop: Arc<Event>) -> Self {
        Self {
            ws,
            scrubber,
            stop,
            clock: Arc::new(crate::sys::monotonic),
            argv: argv(),
            inner: Arc::new(Mutex::new(Inner {
                until: 0.0,
                thread: None,
            })),
        }
    }

    /// Replace the journalctl command line (tests).
    #[must_use]
    pub fn with_argv(mut self, argv: Vec<String>) -> Self {
        self.argv = argv;
        self
    }

    /// `request(seconds)`: keep the console live for at least `seconds` more.
    ///
    /// # Errors
    /// `Rejected` while the agent is polling (no WebSocket).
    pub fn request(&self, seconds: i64) -> AResult<String> {
        if (self.ws)().is_none() {
            return Err(AgentError::Rejected(
                "tail: the live console needs the WebSocket (the agent is polling)".into(),
            ));
        }
        let mut inner = self.inner.lock().unwrap_or_else(PoisonError::into_inner);
        #[allow(clippy::cast_precision_loss)]
        let until = (self.clock)() + seconds as f64;
        inner.until = inner.until.max(until);
        let alive = inner.thread.as_ref().is_some_and(|t| !t.is_finished());
        if !alive {
            let (ws, scrubber, stop, clock, argv, shared) = (
                self.ws.clone(),
                self.scrubber.clone(),
                self.stop.clone(),
                self.clock.clone(),
                self.argv.clone(),
                self.inner.clone(),
            );
            let until = move || shared.lock().unwrap_or_else(PoisonError::into_inner).until;
            let handle = std::thread::Builder::new()
                .name("console-tail".into())
                .spawn(move || run(&ws, &scrubber, &stop, &*clock, &until, &argv));
            inner.thread = handle.ok();
        }
        Ok(format!("live console for {seconds} s"))
    }

    /// `ConsoleTail.request` as the executor's `TailFn`.
    pub fn tail_fn(self: &Arc<Self>) -> crate::actions::TailFn {
        let t = self.clone();
        Arc::new(move |seconds| t.request(seconds))
    }
}

enum Chunk {
    Data(Vec<u8>),
    End,
}

/// `ConsoleTail.run()`.
fn run(
    ws: &WsFn,
    scrubber: &ScrubberFn,
    stop: &Event,
    clock: &(dyn Fn() -> f64 + Send + Sync),
    until: &dyn Fn() -> f64,
    argv: &[String],
) {
    let scrub = scrubber();
    let mut child = match Command::new(&argv[0])
        .args(&argv[1..])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    {
        Ok(c) => c,
        Err(e) => {
            eprintln!(
                "console tail: journalctl failed: {}",
                smallworld_core::errno::io_class(&e)
            );
            return;
        }
    };
    let mut stdout = child.stdout.take().expect("piped");
    let (tx, rx) = mpsc::channel::<Chunk>();
    let reader = std::thread::spawn(move || {
        let mut buf = vec![0u8; 65536];
        loop {
            match stdout.read(&mut buf) {
                Ok(0) | Err(_) => {
                    let _ = tx.send(Chunk::End);
                    return;
                }
                Ok(n) => {
                    if tx.send(Chunk::Data(buf[..n].to_vec())).is_err() {
                        return;
                    }
                }
            }
        }
    });
    let mut pending: Vec<u8> = Vec::new();
    while clock() < until() && !stop.is_set() {
        let chunk = match rx.recv_timeout(Duration::from_millis(500)) {
            Ok(Chunk::Data(d)) => d,
            Ok(Chunk::End) | Err(RecvTimeoutError::Disconnected) => break, // journalctl ended
            Err(RecvTimeoutError::Timeout) => Vec::new(),
        };
        pending.extend_from_slice(&chunk);
        let mut parts: Vec<Vec<u8>> = pending.split(|&b| b == b'\n').map(<[u8]>::to_vec).collect();
        let rest = parts.pop().unwrap_or_default();
        pending = rest[rest.len().saturating_sub(65536)..].to_vec();
        let mut batch: Vec<Value> = Vec::new();
        for raw in &parts {
            if let Some(mut e) = line(raw) {
                let msg = e
                    .get("msg")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                e.insert("msg".into(), json!(scrub.scrub(&msg)));
                batch.push(Value::Object(e));
            }
        }
        let Some(sender) = ws() else { break };
        let mut gone = false;
        for group in batch.chunks(TAIL_BATCH) {
            let text = smallworld_core::pyjson::dumps(
                &json!({"type": "console", "lines": group}),
                &smallworld_core::pyjson::Dumps::python(),
            );
            if !sender.send_text(&text) {
                eprintln!("console tail ended: WebSocketError");
                gone = true;
                break;
            }
        }
        if gone {
            break;
        }
    }
    let _ = child.kill();
    let _ = child.wait();
    drop(rx);
    let _ = reader.join();
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Collect(Mutex<Vec<String>>);
    impl WsSender for Collect {
        fn send_text(&self, text: &str) -> bool {
            self.0.lock().unwrap().push(text.to_string());
            true
        }
    }

    #[test]
    fn refused_without_a_websocket() {
        let t = ConsoleTail::new(
            Arc::new(|| None),
            Arc::new(|| Scrubber::new(Vec::new())),
            Arc::new(Event::default()),
        );
        assert_eq!(
            t.request(30),
            Err(AgentError::Rejected(
                "tail: the live console needs the WebSocket (the agent is polling)".into()
            ))
        );
    }

    #[test]
    fn follows_a_journal_until_it_ends() {
        let sink = Arc::new(Collect(Mutex::new(Vec::new())));
        let s2 = sink.clone();
        let t = ConsoleTail::new(
            Arc::new(move || Some(s2.clone() as Arc<dyn WsSender>)),
            Arc::new(|| Scrubber::new(Vec::new())),
            Arc::new(Event::default()),
        )
        .with_argv(vec![
            "sh".into(),
            "-c".into(),
            "printf 'not json\\n{\"MESSAGE\":\"hi\"}\\n'".into(),
        ]);
        assert_eq!(t.request(10).unwrap(), "live console for 10 s");
        let handle = t.inner.lock().unwrap().thread.take().unwrap();
        handle.join().unwrap();
        // every message is a console batch (its lines depend on ship's journal_entry)
        for m in sink.0.lock().unwrap().iter() {
            assert!(m.starts_with("{\"lines\": ["), "{m}");
        }
    }
}
