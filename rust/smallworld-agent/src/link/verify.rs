//! mc_agent.py `Nonces`, `sign`, `verify` (1112-1199). Owner: link.
//!
//! - HMAC-SHA256 hex over the payload text, compared in constant time with the signature lowercased
//!   (`hmac.compare_digest(sign(key, payload), sig.lower())`). Python's `compare_digest` raises `TypeError` for a
//!   non-ASCII string; nobody catches it, so that case is a `Crash` here too (the caller propagates it).
//! - Field shapes; `iat` (ms, int or float, not bool) within ±60 s; id `[A-Za-z0-9_-]{1,64}`; nonce
//!   `[A-Za-z0-9_-]{8,64}`; payload ≤ 8192 code points.
//! - `json.loads` accepts the bare literals `NaN`, `Infinity` and `-Infinity`; serde_json doesn't. A payload that
//!   only parses with them reaches the field checks when they stand for the top-level `iat`, which then refuses
//!   it as a bad field (v1.5: a non-finite `iat`, `NaN` included, is "missing or bad field: iat" in both); anywhere
//!   else it reads as "payload isn't JSON" (PORTING.md "Divergences").
//! - Nonces: 10 min TTL, persisted (`nonces.json`, `tmp.write_text` + `os.replace`, insertion order like the
//!   Python dict); an unreadable store refuses everything for 600 s and audits `fail-closed`; an unwritable one
//!   refuses the command.

use crate::types::{AResult, AgentError, Command, Obj, Value, py_float};
use hmac::{Hmac, Mac};
use sha2::Sha256;
use smallworld_core::pyjson;
use smallworld_core::pystr::len_cp;
use std::fmt::Write as _;
use std::path::PathBuf;

/// mc_agent.py `MAX_AGE_S`.
pub const MAX_AGE_S: f64 = 60.0;
/// mc_agent.py `NONCE_TTL_S`.
pub const NONCE_TTL_S: f64 = 600.0;
/// The longest payload accepted (code points).
pub const MAX_PAYLOAD: usize = 8192;

fn rejected(msg: &str) -> AgentError {
    AgentError::Rejected(msg.into())
}

/// mc_agent.py `Nonces`.
pub struct Nonces {
    pub path: Option<PathBuf>,
    /// nonce -> when it was used, in insertion order (a Python dict).
    seen: Vec<(String, f64)>,
    blocked_until: f64,
}

/// Why `Nonces::new` couldn't read the store: `type(e).__name__`.
fn load_store(path: &std::path::Path) -> Result<Option<Vec<(String, f64)>>, &'static str> {
    let raw = match std::fs::read(path) {
        Ok(raw) => raw,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(smallworld_core::errno::io_class(&e)),
    };
    let text = String::from_utf8(raw).map_err(|_| "UnicodeDecodeError")?;
    let data = pyjson::loads(&text).ok_or("JSONDecodeError")?;
    let Value::Object(map) = data else {
        return Err("ValueError"); // raise ValueError("not an object")
    };
    let mut seen = Vec::with_capacity(map.len());
    for (k, v) in &map {
        let t = py_float(v).map_err(|e| match e {
            AgentError::Value(..) => "ValueError",
            _ => "TypeError",
        })?;
        seen.push((k.clone(), t));
    }
    Ok(Some(seen))
}

impl Nonces {
    /// `Nonces(path, now)`: loads the store (fail closed on an unreadable one: refuse for `NONCE_TTL_S`, print and
    /// audit). serde_json's map yields the file's keys sorted, not in file order; only the rewritten file's key
    /// order can differ from Python's (nothing reads it by order).
    pub fn new(path: Option<PathBuf>, now: f64) -> Self {
        let mut n = Self {
            path,
            seen: Vec::new(),
            blocked_until: 0.0,
        };
        let Some(path) = n.path.clone() else {
            return n;
        };
        match load_store(&path) {
            Ok(Some(seen)) => n.seen = seen,
            Ok(None) => {}
            Err(class) => {
                n.blocked_until = now + NONCE_TTL_S;
                let name = path.file_name().unwrap_or_default().to_string_lossy();
                let msg = format!(
                    "nonce store {name} unreadable ({class}): commands refused {NONCE_TTL_S:.0} s"
                );
                eprintln!("mc-agent: {msg}");
                smallworld_core::audit::audit(
                    &msg,
                    &[("action", "nonces"), ("result", "fail-closed")],
                );
            }
        }
        n
    }

    /// `json.dumps(self.seen)`: default separators, insertion order, floats as `repr`.
    fn dump(&self) -> String {
        let items: Vec<String> = self
            .seen
            .iter()
            .map(|(k, t)| format!("{}: {}", pyjson::string(k, true), pyjson::float_repr(*t)))
            .collect();
        format!("{{{}}}", items.join(", "))
    }

    /// `use(nonce, now)`: records the nonce; false if seen already.
    ///
    /// # Errors
    /// `Rejected` while failing closed or when the store can't be written (the nonce then stays recorded in
    /// memory, as in Python).
    pub fn use_nonce(&mut self, nonce: &str, now: f64) -> AResult<bool> {
        if now < self.blocked_until {
            return Err(rejected(
                "nonce store was unreadable: refusing commands until the replay window has passed",
            ));
        }
        self.seen.retain(|(_, t)| now - t < NONCE_TTL_S);
        if self.seen.iter().any(|(n, _)| n == nonce) {
            return Ok(false);
        }
        self.seen.push((nonce.to_string(), now));
        if let Some(path) = &self.path {
            smallworld_core::fsx::replace_write(path, self.dump().as_bytes(), None).map_err(
                |e| {
                    AgentError::Rejected(format!(
                        "nonce store can't be written ({})",
                        smallworld_core::errno::io_class(&e)
                    ))
                },
            )?;
        }
        Ok(true)
    }
}

/// `sign(key, payload)`: lowercase hex HMAC-SHA256.
pub fn sign(key: &[u8], payload: &str) -> String {
    let mut mac = Hmac::<Sha256>::new_from_slice(key).expect("HMAC takes keys of any length");
    mac.update(payload.as_bytes());
    let mut out = String::with_capacity(64);
    for b in mac.finalize().into_bytes() {
        let _ = write!(out, "{b:02x}");
    }
    out
}

/// `hmac.compare_digest(a, b)` for two ASCII strings: time depends on the lengths only.
fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// Marker a non-finite literal (`NaN`, `Infinity`, `-Infinity`) becomes for serde_json: a string no legitimate
/// payload starts with (NUL is never in a command field).
const NONFINITE: &str = "\u{0}nonfinite:";

/// The text with Python's non-finite literals (outside strings) replaced by marker strings, or `None` if there
/// are none.
fn mark_nonfinite(text: &str) -> Option<String> {
    let mut out = String::with_capacity(text.len() + 16);
    let mut found = false;
    let mut in_str = false;
    let mut escaped = false;
    let mut rest = text;
    while let Some(c) = rest.chars().next() {
        if in_str {
            if escaped {
                escaped = false;
            } else if c == '\\' {
                escaped = true;
            } else if c == '"' {
                in_str = false;
            }
        } else if c == '"' {
            in_str = true;
        } else {
            let lit = ["NaN", "Infinity", "-Infinity"]
                .into_iter()
                .find(|l| rest.starts_with(l));
            if let Some(l) = lit {
                out.push_str("\"\\u0000nonfinite:");
                out.push_str(l);
                out.push('"');
                rest = &rest[l.len()..];
                found = true;
                continue;
            }
        }
        out.push(c);
        rest = &rest[c.len_utf8()..];
    }
    found.then_some(out)
}

fn has_marker(v: &Value) -> bool {
    match v {
        Value::String(s) => s.starts_with(NONFINITE),
        Value::Array(a) => a.iter().any(has_marker),
        Value::Object(o) => o
            .iter()
            .any(|(k, v)| k.starts_with(NONFINITE) || has_marker(v)),
        _ => false,
    }
}

/// `json.loads(payload)` as `verify` needs it: the value, plus the top-level `iat` when it was a non-finite
/// literal. `None`: Python would raise (or a non-finite literal elsewhere: see the module comment).
fn loads_payload(payload: &str) -> Option<(Value, Option<f64>)> {
    if let Some(v) = pyjson::loads(payload) {
        return Some((v, None));
    }
    let marked = mark_nonfinite(payload)?;
    let mut v = pyjson::loads(&marked)?;
    let mut iat = None;
    if let Value::Object(o) = &mut v
        && let Some(Value::String(s)) = o.get("iat")
    {
        iat = match s.strip_prefix(NONFINITE) {
            Some("NaN") => Some(f64::NAN),
            Some("Infinity") => Some(f64::INFINITY),
            Some("-Infinity") => Some(f64::NEG_INFINITY),
            _ => None,
        };
        if iat.is_some() {
            o.insert("iat".into(), Value::Null);
        }
    }
    if has_marker(&v) {
        return None;
    }
    Some((v, iat))
}

/// `re.fullmatch(r"[A-Za-z0-9_-]{min,max}", s)`.
fn token_ok(s: &str, min: usize, max: usize) -> bool {
    let n = s.len();
    (min..=max).contains(&n)
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

fn str_field(cmd: &Obj, name: &str) -> AResult<String> {
    match cmd.get(name) {
        Some(Value::String(s)) => Ok(s.clone()),
        _ => Err(AgentError::Rejected(format!(
            "missing or bad field: {name}"
        ))),
    }
}

/// `verify(message, key, nonces, now)`: the command, or why not.
///
/// # Errors
/// `Rejected` with Python's text; `Crash` where Python raises `TypeError` (a non-ASCII signature).
pub fn verify(message: &Value, key: &[u8], nonces: &mut Nonces, now: f64) -> AResult<Command> {
    let Value::Object(message) = message else {
        return Err(rejected("not a command object"));
    };
    let (Some(Value::String(payload)), Some(Value::String(sig))) =
        (message.get("payload"), message.get("sig"))
    else {
        return Err(rejected("malformed command"));
    };
    if len_cp(payload) > MAX_PAYLOAD {
        return Err(rejected("malformed command"));
    }
    let sig = sig.to_lowercase();
    if !sig.is_ascii() {
        return Err(AgentError::Crash(
            "TypeError: comparing strings with non-ASCII characters is not supported".into(),
        ));
    }
    if !ct_eq(sign(key, payload).as_bytes(), sig.as_bytes()) {
        return Err(rejected("bad signature"));
    }
    let Some((cmd, nonfinite_iat)) = loads_payload(payload) else {
        return Err(rejected("payload isn't JSON"));
    };
    let Value::Object(cmd) = cmd else {
        return Err(rejected("payload isn't an object"));
    };
    let id = str_field(&cmd, "id")?;
    let action = str_field(&cmd, "action")?;
    let login = str_field(&cmd, "login")?;
    let nonce = str_field(&cmd, "nonce")?;
    let Some(Value::Object(args)) = cmd.get("args") else {
        return Err(rejected("missing or bad field: args"));
    };
    let iat = match (nonfinite_iat, cmd.get("iat")) {
        (Some(f), _) => f,
        (None, Some(Value::Number(n))) => n.as_f64().unwrap_or(f64::NAN),
        _ => return Err(rejected("missing or bad field: iat")),
    };
    if !iat.is_finite() {
        return Err(rejected("missing or bad field: iat")); // v1.5: NaN, an infinity, beyond a float
    }
    if (now - iat / 1000.0).abs() > MAX_AGE_S {
        return Err(rejected("too old (or from the future)"));
    }
    if !token_ok(&nonce, 8, 64) || !token_ok(&id, 1, 64) {
        return Err(rejected("bad id or nonce"));
    }
    if !nonces.use_nonce(&nonce, now)? {
        return Err(rejected("replayed nonce"));
    }
    Ok(Command {
        id,
        action,
        args: args.clone(),
        login,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const VECTORS: &str = include_str!("../../../../tests/agent_parity/vectors/verify.json");

    /// The vector's expected `ok` object, as the fields `Command` keeps.
    fn expected_command(ok: &Value) -> Command {
        let o = ok.as_object().unwrap();
        Command {
            id: o["id"].as_str().unwrap().into(),
            action: o["action"].as_str().unwrap().into(),
            args: o["args"].as_object().unwrap().clone(),
            login: o["login"].as_str().unwrap().into(),
        }
    }

    #[test]
    fn verify_vectors() {
        let v: Value = serde_json::from_str(VECTORS).unwrap();
        let cases = v["vectors"].as_array().unwrap();
        assert!(cases.len() >= 30);
        for case in cases {
            let name = case["case"].as_str().unwrap();
            let key = case["key"].as_str().unwrap().as_bytes();
            let now = case["now"].as_f64().unwrap();
            let mut nonces = Nonces::new(None, now);
            let messages = case["messages"].as_array().unwrap();
            let results = case["results"].as_array().unwrap();
            for (m, want) in messages.iter().zip(results) {
                let got = verify(m, key, &mut nonces, now);
                if let Some(ok) = want.get("ok") {
                    assert_eq!(got, Ok(expected_command(ok)), "case {name}");
                } else {
                    let err = want["error"].as_str().unwrap();
                    assert_eq!(want["type"], "Rejected", "case {name}");
                    assert_eq!(got, Err(AgentError::Rejected(err.into())), "case {name}");
                }
            }
        }
    }

    #[test]
    fn non_finite_iat_is_a_bad_field() {
        let key = b"k".repeat(32);
        let mut n = Nonces::new(None, 1000.0);
        let msg = |p: &str| json!({"payload": p, "sig": sign(&key, p)});
        let base = r#"{"id":"c","action":"s","args":{},"login":"l","nonce":"nonce-0001","iat":"#;
        for lit in ["NaN", "Infinity", "-Infinity"] {
            let r = verify(&msg(&format!("{base}{lit}}}")), &key, &mut n, 1000.0);
            assert_eq!(r, Err(rejected("missing or bad field: iat")), "{lit}");
        }
        let elsewhere = r#"{"id":"c","action":"s","args":{"x":NaN},"login":"l","nonce":"nonce-0002","iat":1000000}"#;
        let r = verify(&msg(elsewhere), &key, &mut n, 1000.0);
        assert_eq!(r, Err(rejected("payload isn't JSON")));
        // literals inside strings are text
        let s =
            r#"{"id":"c","action":"NaN","args":{},"login":"l","nonce":"nonce-0003","iat":1000000}"#;
        assert_eq!(verify(&msg(s), &key, &mut n, 1000.0).unwrap().action, "NaN");
    }

    #[test]
    fn non_ascii_signature_is_an_uncaught_type_error() {
        let key = b"k".repeat(32);
        let mut n = Nonces::new(None, 0.0);
        let r = verify(&json!({"payload": "{}", "sig": "é"}), &key, &mut n, 0.0);
        assert!(matches!(r, Err(AgentError::Crash(ref l)) if l.starts_with("TypeError")));
        // KELVIN SIGN lowercases to ASCII "k": compared, and wrong
        let r = verify(
            &json!({"payload": "{}", "sig": "\u{212A}"}),
            &key,
            &mut n,
            0.0,
        );
        assert_eq!(r, Err(rejected("bad signature")));
    }

    fn tmpdir(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("sw-nonces-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn nonces_persist_prune_and_fail_closed() {
        let d = tmpdir("persist");
        let p = d.join("nonces.json");
        let mut n = Nonces::new(Some(p.clone()), 100.0);
        assert_eq!(n.use_nonce("nonce-aaaa", 100.0), Ok(true));
        assert_eq!(n.use_nonce("nonce-bbbb", 100.5), Ok(true));
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            r#"{"nonce-aaaa": 100.0, "nonce-bbbb": 100.5}"#
        );
        let mut again = Nonces::new(Some(p.clone()), 200.0);
        assert_eq!(again.use_nonce("nonce-aaaa", 200.0), Ok(false));
        // pruned after the TTL
        assert_eq!(again.use_nonce("nonce-aaaa", 701.0), Ok(true));
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            r#"{"nonce-aaaa": 701.0}"#
        );

        std::fs::write(&p, "{broken").unwrap();
        let mut closed = Nonces::new(Some(p.clone()), 1000.0);
        let refused = closed.use_nonce("nonce-cccc", 1599.0).unwrap_err();
        assert!(
            refused
                .to_string()
                .starts_with("nonce store was unreadable")
        );
        assert_eq!(closed.use_nonce("nonce-cccc", 1600.0), Ok(true));

        for (text, _class) in [
            ("[1]", "ValueError"),
            (r#"{"a": "x"}"#, "ValueError"),
            (r#"{"a": null}"#, "TypeError"),
        ] {
            std::fs::write(&p, text).unwrap();
            assert!(
                Nonces::new(Some(p.clone()), 0.0)
                    .use_nonce("nonce-dddd", 1.0)
                    .is_err(),
                "{text}"
            );
        }
        std::fs::write(&p, r#"{"a": "1.5", "b": true}"#).unwrap();
        assert!(
            Nonces::new(Some(p.clone()), 0.0)
                .use_nonce("nonce-dddd", 1.0)
                .is_ok()
        );

        std::fs::remove_file(&p).unwrap();
        std::fs::create_dir(d.join("nonces.json.tmp")).unwrap();
        let mut unwritable = Nonces::new(Some(p.clone()), 0.0);
        assert_eq!(
            unwritable.use_nonce("nonce-eeee", 1.0),
            Err(AgentError::Rejected(
                "nonce store can't be written (IsADirectoryError)".into()
            ))
        );
        // recorded in memory all the same (Python adds it before writing)
        assert_eq!(unwritable.use_nonce("nonce-eeee", 2.0), Ok(false));
        let _ = std::fs::remove_dir_all(&d);
    }
}
