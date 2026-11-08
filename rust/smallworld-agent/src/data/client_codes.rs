//! /modpack one-time codes (mc_agent.py 2153-2274): `parse_client_request`, `ClientCodes`. mc-brand (minecraft)
//! writes `CLIENT_REQUEST_DIR/<uuid>.req`; the agent POSTs `/agent/client-code {uuid, name}` and writes the
//! answer to `CLIENT_CODE_FILE` (`mc-client-codes 1` + `C`/`E` lines, mode 0640 in the setgid verdict dir: the
//! plugin reads it). State in memory only. **The code is never logged**: the audit line names player and uuid,
//! the warnings carry `IngestError` texts (never a body).

use crate::jobs::Event;
use crate::link::{IngestError, Link};
use crate::misc::warnings::Warnings;
use crate::types::{AResult, AgentError, Value, as_py_int_incl_bool, py_str, truthy};
use regex::Regex;
use serde_json::json;
use std::path::PathBuf;
use std::sync::{Arc, LazyLock};

static CLIENT_UUID: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$").expect("constant")
});
static CLIENT_NAME: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9_]{1,16}$").expect("constant"));
static CLIENT_NONCE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[0-9a-f]{32}$").expect("constant"));
static CLIENT_CODE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[0-9]{6}$").expect("constant"));

pub const CLIENT_POLL_S: f64 = 1.0;
pub const CLIENT_KEEP_S: f64 = 120.0;
pub const CLIENT_MAX_REQUESTS: usize = 50;
pub const CLIENT_PER_HOUR: usize = 20;
const REASON_429: &str = "you asked a few times already, try again in a while";
const REASON_404: &str = "the client page isn't set up on this server yet";

/// `CLIENT_REASONS.get(status, default)`.
fn reason(status: u16) -> &'static str {
    match status {
        429 => REASON_429,
        404 => REASON_404,
        _ => "the server couldn't reach its web side, try again soon",
    }
}

/// `CLIENT_FIXES.get(status, default)`.
fn fix(status: u16) -> &'static str {
    match status {
        404 => {
            "the Worker has no client set up (client.enabled needs `mcctl apply`; `mcctl doctor` checks it)"
        }
        401 => "the Worker refused the agent token (a deploy gives the server the current one)",
        429 => "the Worker's rate limit for codes",
        _ => "the Worker is unreachable or failing",
    }
}

/// The constants as the parity vectors name them.
pub fn constants() -> Value {
    json!({
        "CLIENT_POLL_S": CLIENT_POLL_S,
        "CLIENT_KEEP_S": 120,
        "CLIENT_MAX_REQUESTS": CLIENT_MAX_REQUESTS,
        "CLIENT_PER_HOUR": CLIENT_PER_HOUR,
        "CLIENT_REASONS": {"429": REASON_429, "404": REASON_404},
        "CLIENT_FIXES": {"404": fix(404), "401": fix(401), "429": fix(429)},
    })
}

/// `parse_client_request(text)`: (uuid, name, nonce), or None.
pub fn parse_client_request(text: &str) -> Option<(String, String, String)> {
    let lines = crate::systemd::splitlines(text);
    if lines.len() != 2 || lines[0] != "mc-client-request 1" {
        return None;
    }
    let f: Vec<&str> = lines[1].split('\t').collect();
    if f.len() != 4
        || !CLIENT_UUID.is_match(f[0])
        || !CLIENT_NAME.is_match(f[1])
        || !CLIENT_NONCE.is_match(f[2])
    {
        return None;
    }
    Some((f[0].into(), f[1].into(), f[2].into()))
}

/// `post(path, payload)` (the Worker; `post_json` in the daemon).
pub type PostFn = Arc<dyn Fn(&str, &Value) -> Result<Value, IngestError> + Send + Sync>;
/// `audit(message, **fields)`.
pub type AuditFn = Arc<dyn Fn(&str, &[(&str, &str)]) + Send + Sync>;

type Key = (String, String);

/// `ClientCodes`.
pub struct ClientCodes {
    pub request_dir: PathBuf,
    pub code_file: PathBuf,
    pub post: PostFn,
    pub clock: Arc<dyn Fn() -> f64 + Send + Sync>,
    pub audit: AuditFn,
    answered: Vec<(Key, f64)>,
    /// insertion-ordered like Python's dict: (uuid, nonce) -> (line, when)
    lines: Vec<(Key, (String, f64))>,
    sent: Vec<f64>,
}

impl ClientCodes {
    pub fn new(request_dir: PathBuf, code_file: PathBuf, post: PostFn) -> Self {
        Self {
            request_dir,
            code_file,
            post,
            clock: Arc::new(crate::sys::now),
            audit: Arc::new(|m: &str, f: &[(&str, &str)]| smallworld_core::audit::audit(m, f)),
            answered: Vec::new(),
            lines: Vec::new(),
            sent: Vec::new(),
        }
    }

    /// The daemon's instance: the settings' paths, `link.post_json`.
    pub fn for_link(link: &Link) -> Self {
        let l = link.clone();
        Self::new(
            link.settings.client_request_dir.clone(),
            link.settings.client_code_file.clone(),
            Arc::new(move |path: &str, payload: &Value| l.post_json(path, payload)),
        )
    }

    /// `scan()`: one pass; how many requests were answered.
    ///
    /// # Errors
    /// What Python's `scan` doesn't catch (an odd Worker reply: `Crash`).
    pub fn scan(&mut self, warn: &Warnings) -> AResult<usize> {
        let now = (self.clock)();
        self.answered.retain(|(_, v)| now - v < 600.0);
        self.lines.retain(|(_, (_, w))| now - w < CLIENT_KEEP_S);
        self.sent.retain(|t| now - t < 3600.0);
        let mut names: Vec<String> = match std::fs::read_dir(&self.request_dir) {
            Ok(rd) => rd
                .filter_map(Result::ok)
                .map(|e| e.file_name().to_string_lossy().into_owned())
                .collect(),
            Err(_) => Vec::new(),
        };
        names.sort();
        names.truncate(CLIENT_MAX_REQUESTS);
        let mut done = 0;
        for name in names {
            // Python's endswith is case-sensitive too
            #[allow(clippy::case_sensitive_file_extension_comparisons)]
            let skip = !name.ends_with(".req") || name.starts_with('.');
            if skip {
                continue;
            }
            let path = self.request_dir.join(&name);
            // is_file()/stat() follow symlinks, as pathlib's do
            let Ok(meta) = std::fs::metadata(&path) else {
                continue;
            };
            if !meta.is_file() || meta.len() > 4096 {
                continue;
            }
            let Ok(bytes) = std::fs::read(&path) else {
                continue;
            };
            let Ok(text) = String::from_utf8(bytes) else {
                continue;
            };
            let Some((uuid, player, nonce)) = parse_client_request(&text) else {
                continue;
            };
            let key = (uuid.clone(), nonce.clone());
            if format!("{uuid}.req") != name || self.answered.iter().any(|(k, _)| *k == key) {
                continue;
            }
            self.answered.push((key.clone(), now));
            let line = self.answer(&uuid, &player, &nonce, now, warn)?;
            match self.lines.iter_mut().find(|(k, _)| *k == key) {
                Some(slot) => slot.1 = (line, now),
                None => self.lines.push((key, (line, now))),
            }
            done += 1;
        }
        if done > 0
            || self
                .lines
                .iter()
                .any(|(_, (_, w))| now - w >= CLIENT_KEEP_S - CLIENT_POLL_S)
        {
            self.write(warn);
        }
        Ok(done)
    }

    /// `answer(uuid, player, nonce, now)`: the code file line.
    fn answer(
        &mut self,
        uuid: &str,
        player: &str,
        nonce: &str,
        now: f64,
        warn: &Warnings,
    ) -> AResult<String> {
        if self.sent.len() >= CLIENT_PER_HOUR {
            return Ok(format!("E\t{uuid}\t{nonce}\t{REASON_429}"));
        }
        self.sent.push(now);
        let reply = match (self.post)("/agent/client-code", &json!({"uuid": uuid, "name": player}))
        {
            Ok(r) => r,
            Err(e) => {
                let status = e.status.unwrap_or(0);
                let (reason, fix) = (reason(status), fix(status));
                let text =
                    format!("/modpack code request failed ({e}): {fix}; players see: {reason}");
                warn.fail("client", &text, true);
                return Ok(format!("E\t{uuid}\t{nonce}\t{reason}"));
            }
        };
        // `(reply or {}).get(...)`: a falsy reply is {}, a truthy non-dict raises AttributeError
        let empty = crate::types::Obj::new();
        let obj = match &reply {
            Value::Object(o) => o,
            other if !truthy(other) => &empty,
            other => {
                return Err(AgentError::Crash(format!(
                    "AttributeError: '{}' object has no attribute 'get'",
                    crate::worlds::py_type(other)
                )));
            }
        };
        let code = obj.get("code").map_or_else(String::new, py_str);
        let expires = obj.get("expires_ms").and_then(|v| {
            as_py_int_incl_bool(v).map(|n| match v {
                Value::Bool(b) => if *b { "True" } else { "False" }.to_string(),
                _ => n.to_string(),
            })
        });
        let Some(expires) = expires.filter(|_| CLIENT_CODE.is_match(&code)) else {
            return Ok(format!(
                "E\t{uuid}\t{nonce}\tthe web side gave an odd answer, try again soon"
            ));
        };
        warn.ok("client");
        (self.audit)("client code issued", &[("player", player), ("uuid", uuid)]); // never the code
        Ok(format!("C\t{uuid}\t{nonce}\t{code}\t{expires}"))
    }

    /// `write()`: `.<name>.tmp`, chmod 0640 (the setgid dir: mc-brand may read it), `os.replace`.
    fn write(&self, warn: &Warnings) {
        let mut text = String::from("mc-client-codes 1\n");
        for (_, (line, _)) in &self.lines {
            text.push_str(line);
            text.push('\n');
        }
        let path = &self.code_file;
        let name = path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        let tmp = path.with_file_name(format!(".{name}.tmp"));
        let r = (|| -> std::io::Result<()> {
            use std::os::unix::fs::PermissionsExt;
            std::fs::write(&tmp, text.as_bytes())?;
            std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o640))?;
            std::fs::rename(&tmp, path)
        })();
        if let Err(e) = r {
            warn.fail(
                "client-file",
                &format!(
                    "can't write {name}: {}",
                    smallworld_core::errno::io_class(&e)
                ),
                false,
            );
        }
    }

    /// `run(stop)`: a pass every `CLIENT_POLL_S`; a failing pass is a warning, never the end of the loop.
    pub fn run(&mut self, warn: &Warnings, stop: &Event) {
        while !stop.is_set() {
            let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| self.scan(warn)));
            match r {
                Ok(Ok(_)) => {}
                Ok(Err(e)) => warn.fail("client", &format!("{}: {e}", e.class_name()), false),
                Err(p) => warn.fail("client", &super::panic_text(&p), false),
            }
            stop.wait(CLIENT_POLL_S);
        }
    }
}

/// `ClientCodes(s, post_json).run(stop)` for `daemon::run` (started when `CLIENT=1`).
///
/// # Errors
/// None (the loop runs until `stop`).
#[allow(clippy::unnecessary_wraps)] // daemon::LoopFn
pub fn client_loop(d: &crate::daemon::Daemon) -> AResult<()> {
    ClientCodes::for_link(&d.link).run(d.warn, &d.stop);
    Ok(())
}

#[cfg(test)]
#[allow(clippy::items_after_statements, clippy::map_unwrap_or)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    #[test]
    fn parse_vectors() {
        let v: Value = serde_json::from_str(include_str!(
            "../../../../tests/agent_parity/vectors/parsers.json"
        ))
        .unwrap();
        for c in v["vectors"]["parse_client_request"].as_array().unwrap() {
            let got = parse_client_request(c["input"].as_str().unwrap())
                .map(|(a, b, n)| json!([a, b, n]))
                .unwrap_or(Value::Null);
            assert_eq!(got, c["output"], "{c}");
        }
    }

    #[test]
    fn codes_file_errors_limit_and_never_the_code_in_logs() {
        let d = std::env::temp_dir().join(format!("sw-cc-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        let req = d.join("req");
        std::fs::create_dir_all(&req).unwrap();
        for k in 0..23u32 {
            let uuid = format!("{k:08x}-0000-4000-8000-000000000000");
            std::fs::write(
                req.join(format!("{uuid}.req")),
                format!("mc-client-request 1\n{uuid}\tPlayer{k}\t{k:032x}\t1\n"),
            )
            .unwrap();
        }
        std::fs::write(req.join("junk.req"), "x").unwrap();
        let replies = Arc::new(Mutex::new(vec![
            Ok(json!({"code": "123456", "expires_ms": 5})),
            Err(IngestError {
                message: "POST /agent/client-code: HTTP 404".into(),
                status: Some(404),
            }),
            Ok(json!({"code": 654_321, "expires_ms": true})),
            Ok(json!({"code": "12x456", "expires_ms": 1})),
        ]));
        let r2 = replies.clone();
        let audits = Arc::new(Mutex::new(Vec::<String>::new()));
        let a2 = audits.clone();
        let mut cc = ClientCodes::new(
            req,
            d.join("client-codes.tsv"),
            Arc::new(move |_p: &str, _b: &Value| {
                let mut r = r2.lock().unwrap();
                if r.is_empty() {
                    Ok(json!({"code": "000000", "expires_ms": 9}))
                } else {
                    r.remove(0)
                }
            }),
        );
        cc.clock = Arc::new(|| 1000.0);
        cc.audit = Arc::new(move |m: &str, f: &[(&str, &str)]| {
            a2.lock().unwrap().push(format!("{m} {f:?}"));
        });
        let warn = Warnings::default();
        assert_eq!(cc.scan(&warn).unwrap(), 23);
        let text = std::fs::read_to_string(d.join("client-codes.tsv")).unwrap();
        let lines: Vec<&str> = text.lines().collect();
        assert_eq!(lines.len(), 24);
        assert_eq!(lines[0], "mc-client-codes 1");
        assert_eq!(
            lines[1],
            "C\t00000000-0000-4000-8000-000000000000\t00000000000000000000000000000000\t123456\t5"
        );
        assert!(lines[2].ends_with("\tthe client page isn't set up on this server yet"));
        assert!(lines[3].ends_with("\t654321\tTrue"));
        assert!(lines[4].ends_with("\tthe web side gave an odd answer, try again soon"));
        assert!(lines[21].ends_with(&format!("\t{REASON_429}")));
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(d.join("client-codes.tsv"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o640
        );
        for a in audits.lock().unwrap().iter() {
            assert!(!a.contains("123456") && !a.contains("654321"), "{a}");
        }
        // answered requests aren't asked again
        assert_eq!(cc.scan(&warn).unwrap(), 0);
        let _ = std::fs::remove_dir_all(&d);
    }
}
