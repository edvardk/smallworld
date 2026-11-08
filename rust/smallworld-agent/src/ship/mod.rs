//! Journal shipping (owner: **ship**; mc_agent.py 596-870, 2444-2490): the journal since the saved cursor,
//! streamed with only the kept fields (journal.rs), filtered (`for_d1`, `for_archive`), scrubbed (scrub.rs),
//! shipped as gzipped NDJSON to R2 (`POST /ingest/archive?batch=`) and capped per UTC day to D1
//! (`POST /ingest/logs`); the cursor moves only once both are delivered (archive.rs, `ship_logs`).
//!
//! Not ported on purpose: `ship_logs`'s `observe_all` (telemetry's overhead card). In rust mode telemetry is
//! the Python sidecar, which reads systemd's resource lines from the journal itself (design §1.3).

pub mod archive;
pub mod journal;
pub mod scrub;

use crate::link::{IngestError, Link};
use crate::misc::warnings::Warnings;
use crate::settings::Settings;
use crate::types::{AgentError, Obj, VERSION, Value};
use archive::{LogState, archive_body, select_rows};
use journal::{
    LOG_BATCH_MAX_BYTES, LOG_BATCH_MAX_ENTRIES, LOG_CATCHUP_BATCHES, Slicer, UNIT, read_journal,
};
use scrub::{Scrubber, scrubber_for};
use sha2::{Digest, Sha256};
use std::fmt::Write as _;
use std::path::Path;

/// What a log round can fail with: the Worker or journalctl (`IngestError`: the journal keeps the entries) or
/// anything else (Python's `except Exception` in the loop).
#[derive(Debug)]
pub enum ShipError {
    Ingest(IngestError),
    Agent(AgentError),
}

impl From<IngestError> for ShipError {
    fn from(e: IngestError) -> Self {
        ShipError::Ingest(e)
    }
}

impl From<AgentError> for ShipError {
    fn from(e: AgentError) -> Self {
        ShipError::Agent(e)
    }
}

/// The two Worker calls shipping makes (`request`, `post_json`); `Link` in the daemon, a fake in tests.
pub trait Ingest {
    /// `request(s, method, path, body, content_type, timeout)`.
    ///
    /// # Errors
    /// `IngestError`.
    fn request(
        &self,
        method: &str,
        path: &str,
        body: Option<&[u8]>,
        content_type: &str,
        timeout_s: f64,
    ) -> Result<Value, IngestError>;

    /// `post_json(s, path, payload)`.
    ///
    /// # Errors
    /// `IngestError`.
    fn post_json(&self, path: &str, payload: &Value) -> Result<Value, IngestError>;
}

impl Ingest for Link {
    fn request(
        &self,
        method: &str,
        path: &str,
        body: Option<&[u8]>,
        content_type: &str,
        timeout_s: f64,
    ) -> Result<Value, IngestError> {
        Link::request(self, method, path, body, content_type, timeout_s)
    }

    fn post_json(&self, path: &str, payload: &Value) -> Result<Value, IngestError> {
        Link::post_json(self, path, payload)
    }
}

/// `observe(entries)`: what the daemon does with minecraft.service's delivered entries (`Agent.observe_logs`:
/// the rejected-logins feed). Errors fail the round as Python's exceptions do.
pub type Observe<'a> = &'a dyn Fn(&[Obj]) -> Result<(), AgentError>;

/// `hashlib.sha256((cursor or "").encode()).hexdigest()[:32]`: the archive's idempotency key.
///
/// # Errors
/// `Crash` for a truthy cursor that isn't a str (Python: `AttributeError`, the round fails).
pub fn batch_id(cursor: &Value) -> Result<String, AgentError> {
    let text = match cursor {
        Value::String(s) => s.as_str(),
        v if !crate::types::truthy(v) => "",
        v => {
            let class = match v {
                Value::Bool(_) => "bool",
                Value::Number(n) if n.is_f64() => "float",
                Value::Number(_) => "int",
                Value::Array(_) => "list",
                _ => "dict",
            };
            return Err(AgentError::Crash(format!(
                "AttributeError: '{class}' object has no attribute 'encode'"
            )));
        }
    };
    let digest = Sha256::digest(text.as_bytes());
    let hex = digest.iter().fold(String::new(), |mut acc, b| {
        let _ = write!(acc, "{b:02x}");
        acc
    });
    Ok(hex[..32].to_string())
}

/// The inputs of one `ship_logs` call that tests replace (Python's keyword arguments).
pub struct ShipParts<'a> {
    /// `read_fn(cursor)`.
    pub read: &'a mut dyn FnMut(&Value) -> Result<journal::Batch, ShipError>,
    /// `scrub` (default: `scrubber_for(s)`, built per batch: known values are re-read every time).
    pub scrub: Option<&'a Scrubber>,
    pub observe: Option<Observe<'a>>,
    pub cede: &'a mut dyn FnMut(),
}

/// `ship_logs(s, path, ...)`: one batch: archive the product's units, D1 the important ones, both scrubbed.
/// Returns the entries shipped and whether more are waiting. The cursor moves only after delivery; `observe`
/// sees minecraft.service's entries (unscrubbed copies) once they are delivered.
///
/// # Errors
/// `ShipError::Ingest` (journalctl, the Worker), `ShipError::Agent` (anything Python wouldn't catch).
pub fn ship_logs(
    s: &Settings,
    ingest: &dyn Ingest,
    path: &Path,
    parts: ShipParts<'_>,
) -> Result<(usize, bool), ShipError> {
    let st = LogState::load(path);
    let batch = (parts.read)(&st.cursor)?;
    let (mut entries, cursor, more) = (batch.entries, batch.cursor, batch.full);
    let seen: Vec<Obj> = if parts.observe.is_some() {
        entries
            .iter()
            .filter(|e| e.unit == UNIT)
            .map(journal::Entry::to_obj)
            .collect()
    } else {
        Vec::new()
    };
    let own;
    let scrub = if let Some(sc) = parts.scrub {
        sc
    } else {
        own = scrubber_for(s);
        &own
    };
    let mut kept = Vec::new();
    for mut e in entries.drain(..) {
        (parts.cede)();
        if e.for_archive() {
            e.msg = scrub.scrub(&e.msg);
            kept.push(e);
        }
    }
    if kept.is_empty() {
        if cursor != st.cursor {
            let mut st = st;
            st.cursor = cursor;
            st.save(path)?;
        }
        if let Some(observe) = parts.observe
            && !seen.is_empty()
        {
            observe(&seen)?;
        }
        return Ok((0, more));
    }
    let id = batch_id(&cursor)?;
    ingest.request(
        "POST",
        &format!("/ingest/archive?batch={id}"),
        Some(&archive_body(&kept)?),
        "application/gzip",
        60.0,
    )?;
    let mut trial = st.clone();
    let rows = select_rows(&kept, &mut trial, s.log_max_rows_per_day);
    if !rows.is_empty() {
        let mut payload = Obj::new();
        payload.insert("agent".into(), VERSION.into());
        payload.insert(
            "rows".into(),
            Value::Array(rows.into_iter().map(Value::Object).collect()),
        );
        ingest.post_json("/ingest/logs", &Value::Object(payload))?;
    }
    trial.cursor = cursor;
    trial.save(path)?;
    if let Some(observe) = parts.observe
        && !seen.is_empty()
    {
        observe(&seen)?;
    }
    Ok((kept.len(), more))
}

/// One round of `Agent.logs_loop`: up to `LOG_CATCHUP_BATCHES` batches back to back (a backlog drains batch
/// by batch, cursor per batch).
fn logs_round(
    s: &Settings,
    ingest: &dyn Ingest,
    stop: &crate::jobs::Event,
    observe: Option<Observe<'_>>,
) -> Result<(), ShipError> {
    let path = s.state_dir.join("logs.json");
    for _ in 0..LOG_CATCHUP_BATCHES {
        let mut read_cede = Slicer::default();
        let mut cede = Slicer::default();
        let mut read = |cursor: &Value| {
            read_journal(
                cursor,
                LOG_BATCH_MAX_ENTRIES,
                LOG_BATCH_MAX_BYTES,
                None,
                &mut || read_cede.cede(),
            )
        };
        let (_, more) = ship_logs(
            s,
            ingest,
            &path,
            ShipParts {
                read: &mut read,
                scrub: None,
                observe,
                cede: &mut || cede.cede(),
            },
        )?;
        if !more || stop.is_set() {
            break;
        }
    }
    Ok(())
}

/// `Agent.logs_loop`: every `LOG_BATCH_S` until `stop`, one round; failures logged as Python logs them (an
/// `IngestError` once per outage through `Warnings`, anything else per round), a panic contained to its round.
pub fn run_logs_loop(
    s: &Settings,
    ingest: &dyn Ingest,
    stop: &crate::jobs::Event,
    warn: &Warnings,
    observe: Option<Observe<'_>>,
) {
    #[allow(clippy::cast_precision_loss)]
    let wait = s.log_batch_s as f64;
    while !stop.wait(wait) {
        let round = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            logs_round(s, ingest, stop, observe)
        }));
        match round {
            Ok(Ok(())) => warn.ok("logs"),
            Ok(Err(ShipError::Ingest(e))) => {
                warn.fail(
                    "logs",
                    &format!("deferred, the journal keeps them: {e}"),
                    false,
                );
            }
            Ok(Err(ShipError::Agent(e))) => {
                eprintln!("log round failed: {}: {e}", e.class_name());
            }
            Err(panic) => {
                let msg = panic
                    .downcast_ref::<String>()
                    .cloned()
                    .or_else(|| panic.downcast_ref::<&str>().map(|s| (*s).to_string()))
                    .unwrap_or_default();
                eprintln!("log round failed: panic: {msg}");
            }
        }
    }
}

/// `Agent.observe_logs(entries)`: minecraft.service's delivered lines into the rejected-logins feed; a change
/// makes the inventory due.
///
/// # Errors
/// What the feed raises (the log round fails, as in Python).
pub fn observe_logs(daemon: &crate::daemon::Daemon, entries: &[Obj]) -> Result<(), AgentError> {
    let lines: Vec<(i64, String)> = entries
        .iter()
        .map(|e| {
            let ts = e.get("ts").and_then(Value::as_i64).unwrap_or(0);
            let msg = e.get("msg").and_then(Value::as_str).unwrap_or("");
            (ts, msg.to_string())
        })
        .collect();
    if daemon.panel.rejected.feed(&lines)? {
        daemon.inventory_due.set();
    }
    Ok(())
}

/// The daemon's logs loop (`Agent.logs_loop`, a `daemon::LoopFn`; runs until `stop`): `run_logs_loop` with the
/// daemon's link, `WARN` and `observe_logs`. Never returns an error: each round's failure is logged there.
///
/// # Errors
/// None (the signature is the daemon's loop type).
#[allow(clippy::unnecessary_wraps)] // the daemon's LoopFn type
pub fn logs_loop(daemon: &crate::daemon::Daemon) -> Result<(), AgentError> {
    let observe = |entries: &[Obj]| observe_logs(daemon, entries);
    run_logs_loop(
        &daemon.settings,
        &daemon.link,
        &daemon.stop,
        daemon.warn,
        Some(&observe),
    );
    Ok(())
}

/// The hidden `selftest-scrub` subcommand, for differential tests only (tests/test_rust_scrub_fuzz.py drives
/// it against the Python functions): reads one JSON object from stdin, `{"known": [str], "texts": [str],
/// "journal": [object]}` (each key optional), and prints `{"texts": [Scrubber(known)(text)...], "journal":
/// [journal_entry(raw) | null | {"error": str}...]}`. It is a pure function of its input: it reads no
/// environment, settings, file or server.properties, so it can only print (scrubbed) what it was given.
/// Exit 2 on input that isn't such an object.
pub fn selftest_scrub(input: &mut dyn std::io::Read, out: &mut dyn std::io::Write) -> u8 {
    let mut text = String::new();
    let Some(Value::Object(req)) = input
        .read_to_string(&mut text)
        .ok()
        .and_then(|_| serde_json::from_str::<Value>(&text).ok())
    else {
        eprintln!("selftest-scrub: stdin must be a JSON object");
        return 2;
    };
    let strings = |key: &str| -> Option<Vec<String>> {
        match req.get(key) {
            None => Some(Vec::new()),
            Some(Value::Array(items)) => {
                items.iter().map(|v| v.as_str().map(String::from)).collect()
            }
            Some(_) => None,
        }
    };
    let (Some(known), Some(texts)) = (strings("known"), strings("texts")) else {
        eprintln!("selftest-scrub: known and texts must be lists of strings");
        return 2;
    };
    let scrubber = Scrubber::new(known);
    let texts: Vec<Value> = texts.iter().map(|t| scrubber.scrub(t).into()).collect();
    let journal: Vec<Value> = match req.get("journal") {
        Some(Value::Array(items)) => items
            .iter()
            .map(
                |raw| match raw.as_object().map(journal::try_journal_entry) {
                    Some(Ok(Some(e))) => Value::Object(e.to_obj()),
                    Some(Ok(None)) | None => Value::Null,
                    Some(Err(e)) => {
                        serde_json::json!({"error": format!("{}: {e}", e.class_name())})
                    }
                },
            )
            .collect(),
        _ => Vec::new(),
    };
    let reply = serde_json::json!({"texts": texts, "journal": journal});
    if writeln!(out, "{reply}").is_err() {
        return 1;
    }
    0
}

#[cfg(test)]
mod tests {
    use super::*;
    use journal::{Batch, try_journal_entry};
    use std::cell::RefCell;

    struct FakeWorker {
        calls: RefCell<Vec<(String, Vec<u8>)>>,
        fail: Option<&'static str>,
    }

    impl Ingest for FakeWorker {
        fn request(
            &self,
            method: &str,
            path: &str,
            body: Option<&[u8]>,
            _ct: &str,
            _t: f64,
        ) -> Result<Value, IngestError> {
            if self.fail.is_some_and(|f| path.starts_with(f)) {
                return Err(IngestError {
                    message: format!("{method} {path}: HTTP 503"),
                    status: Some(503),
                });
            }
            self.calls
                .borrow_mut()
                .push((path.into(), body.unwrap_or_default().to_vec()));
            Ok(Value::Object(Obj::new()))
        }

        fn post_json(&self, path: &str, payload: &Value) -> Result<Value, IngestError> {
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

    fn settings(dir: &Path, cap: i64) -> Settings {
        let mut env = crate::settings::Env::new();
        env.insert("INGEST_URL".into(), "https://worker.example".into());
        env.insert(
            "AGENT_TOKEN".into(),
            "AgentTokenShip-0123456789abcdefABCDEF".into(),
        );
        env.insert("COMMAND_HMAC_KEY".into(), "k".repeat(32));
        env.insert("STATE_DIRECTORY".into(), dir.display().to_string());
        env.insert("LOG_MAX_ROWS_PER_DAY".into(), cap.to_string());
        Settings::from_env(&env).unwrap()
    }

    fn entry(unit: &str, ident: &str, msg: &str, us: i64) -> journal::Entry {
        let raw = serde_json::json!({"__REALTIME_TIMESTAMP": us.to_string(), "_SYSTEMD_UNIT": unit,
                                     "SYSLOG_IDENTIFIER": ident, "MESSAGE": msg});
        try_journal_entry(raw.as_object().unwrap())
            .unwrap()
            .unwrap()
    }

    #[test]
    fn the_cursor_moves_only_after_both_deliveries() {
        let dir = std::env::temp_dir().join(format!("ship-round-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("logs.json");
        let s = settings(&dir, 2);
        let scrub = Scrubber::new(vec![s.token.clone()]);
        let batch = || Batch {
            entries: vec![
                entry(
                    "minecraft.service",
                    "minecraft",
                    &format!("tok {}", s.token),
                    1_794_009_600_000_000,
                ),
                entry("sshd.service", "sshd", "private", 1_794_009_600_000_000),
                entry("mc-backup.service", "mc-backup", "b", 1_794_009_600_000_000),
                entry("mc-world@x.service", "mc-world", "w", 1_794_009_600_000_000),
            ],
            cursor: "s=1".into(),
            full: true,
        };
        let seen = RefCell::new(Vec::new());
        let observe = |e: &[Obj]| {
            seen.borrow_mut().extend(e.iter().cloned());
            Ok(())
        };
        for fail in ["/ingest/archive", "/ingest/logs"] {
            let w = FakeWorker {
                calls: RefCell::default(),
                fail: Some(fail),
            };
            let r = ship_logs(
                &s,
                &w,
                &path,
                ShipParts {
                    read: &mut |_| Ok(batch()),
                    scrub: Some(&scrub),
                    observe: Some(&observe),
                    cede: &mut || (),
                },
            );
            assert!(matches!(r, Err(ShipError::Ingest(_))));
            assert_eq!(LogState::load(&path), LogState::default(), "{fail}");
            assert!(seen.borrow().is_empty());
        }
        let w = FakeWorker {
            calls: RefCell::default(),
            fail: None,
        };
        let r = ship_logs(
            &s,
            &w,
            &path,
            ShipParts {
                read: &mut |_| Ok(batch()),
                scrub: Some(&scrub),
                observe: Some(&observe),
                cede: &mut || (),
            },
        )
        .unwrap();
        assert_eq!(r, (3, true));
        let st = LogState::load(&path);
        assert_eq!(
            (st.cursor.as_str(), st.day.as_str(), st.rows),
            (Some("s=1"), "20261107", 2)
        );
        let calls = w.calls.borrow();
        assert_eq!(
            calls[0].0,
            format!("/ingest/archive?batch={}", batch_id(&"s=1".into()).unwrap())
        );
        let mut text = String::new();
        std::io::Read::read_to_string(
            &mut flate2::read::GzDecoder::new(&calls[0].1[..]),
            &mut text,
        )
        .unwrap();
        assert!(
            !text.contains(&s.token)
                && !text.contains("private")
                && text.contains("tok <redacted>")
        );
        assert_eq!(text.lines().count(), 3);
        let logs: Value = serde_json::from_slice(&calls[1].1).unwrap();
        assert_eq!(logs["agent"], "1");
        assert_eq!(logs["rows"].as_array().unwrap().len(), 2);
        // observe got minecraft.service's entry, unscrubbed, after delivery.
        assert_eq!(seen.borrow().len(), 1);
        assert!(seen.borrow()[0]["msg"].as_str().unwrap().contains(&s.token));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn nothing_to_archive_saves_the_cursor_only() {
        let dir = std::env::temp_dir().join(format!("ship-empty-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("logs.json");
        let s = settings(&dir, 5);
        let w = FakeWorker {
            calls: RefCell::default(),
            fail: Some("/"),
        };
        let r = ship_logs(
            &s,
            &w,
            &path,
            ShipParts {
                read: &mut |_| {
                    Ok(Batch {
                        entries: vec![entry("sshd.service", "sshd", "x", 0)],
                        cursor: "c2".into(),
                        full: false,
                    })
                },
                scrub: Some(&Scrubber::new(vec![])),
                observe: None,
                cede: &mut || (),
            },
        )
        .unwrap();
        assert_eq!(r, (0, false));
        assert_eq!(LogState::load(&path).cursor, "c2");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn selftest_scrub_echoes_only_scrubbed_input() {
        let mut out = Vec::new();
        let input = r#"{"known": ["AgentTokenShip-0123"], "texts": ["a AgentTokenShip-0123 b", "pass=x"],
                        "journal": [{"__REALTIME_TIMESTAMP": "5000"}, {"x": 1},
                                    {"__REALTIME_TIMESTAMP": "0", "PRIORITY": "\u00b2"}, 5]}"#;
        assert_eq!(selftest_scrub(&mut input.as_bytes(), &mut out), 0);
        let reply: Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(
            reply["texts"],
            serde_json::json!(["a <redacted> b", "pass=<redacted>"])
        );
        assert_eq!(reply["journal"][0]["ts"], 5);
        assert_eq!(reply["journal"][1], Value::Null);
        assert_eq!(
            reply["journal"][2]["error"],
            "ValueError: invalid literal for int() with base 10: '\u{b2}'"
        );
        assert_eq!(reply["journal"][3], Value::Null);
        for bad in ["", "[]", r#"{"texts": [1]}"#, r#"{"known": "x"}"#] {
            assert_eq!(
                selftest_scrub(&mut bad.as_bytes(), &mut Vec::new()),
                2,
                "{bad}"
            );
        }
    }

    #[test]
    fn batch_ids_like_python() {
        let v: Value = serde_json::from_str(include_str!(
            "../../../../tests/agent_parity/vectors/parsers.json"
        ))
        .unwrap();
        for case in v["vectors"]["archive_batch_id"].as_array().unwrap() {
            assert_eq!(
                batch_id(&case["cursor"]).unwrap(),
                case["batch"].as_str().unwrap()
            );
        }
        assert_eq!(
            batch_id(&Value::Null).unwrap(),
            batch_id(&"".into()).unwrap()
        );
        assert_eq!(
            batch_id(&5.into()).unwrap_err().to_string(),
            "AttributeError: 'int' object has no attribute 'encode'"
        );
    }
}
