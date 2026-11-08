//! mc_agent.py `handle_command` (1946-1969): verify, check, run, audit; a result object
//! `{id, ok, output|error, finished_at}`. Audit: `agent action={action or '?'} ok={True|False}
//! login={login or '?'}` with MC_ACTION/LOGIN/ARGS (`json.dumps(args, sort_keys=True)[:500]` of the *unverified*
//! payload's args, as Python computes it)/RESULT. A `NotPorted` from the Executor is answered as
//! `Rejected("{action}: not yet ported to Rust")`. Owner: link.
//!
//! Python catches `Rejected`, `OSError`, `SubprocessError` and `RconError` only; anything else (`ValueError`,
//! `KeyError`, `TypeError`: `AgentError::Value`/`Key`/`Crash`) leaves `handle_command` without an audit entry or
//! a result, and the caller decides (the WebSocket session swallows a `ValueError`, everything else ends the
//! round). That is the `Err` here.

use super::verify::{Nonces, verify};
use crate::actions::Executor;
use crate::settings::Settings;
use crate::types::{AResult, AgentError, Obj, Value};
use smallworld_core::pyjson;
use smallworld_core::pystr::prefix_cp;

/// `json.dumps(json.loads(message["payload"]).get("args", {}), sort_keys=True)[:500]`, "" on any exception.
fn audit_args(message: &Value) -> String {
    let Some(Value::String(payload)) = message.get("payload") else {
        return String::new();
    };
    let Some(Value::Object(cmd)) = pyjson::loads(payload) else {
        return String::new();
    };
    let args = cmd
        .get("args")
        .cloned()
        .unwrap_or_else(|| Value::Object(Obj::new()));
    prefix_cp(&pyjson::dumps_sorted(&args), 500).to_string()
}

/// `int(time.time() * 1000)`.
pub fn now_ms() -> i64 {
    #[allow(clippy::cast_possible_truncation)]
    let ms = (crate::sys::now() * 1000.0) as i64;
    ms
}

/// `handle_command(message, s, nonces, executor)`.
///
/// # Errors
/// What Python doesn't catch there (see the module comment).
pub fn handle_command(
    message: &Value,
    s: &Settings,
    nonces: &mut Nonces,
    executor: &mut Executor,
) -> AResult<Value> {
    handle_at(message, s, nonces, executor, crate::sys::now())
}

/// `handle_command` at a given time (tests).
///
/// # Errors
/// As `handle_command`.
pub fn handle_at(
    message: &Value,
    s: &Settings,
    nonces: &mut Nonces,
    executor: &mut Executor,
    now: f64,
) -> AResult<Value> {
    let (mut cmd_id, mut login, mut action) = (String::new(), String::new(), String::new());
    let run = (|| {
        let cmd = verify(message, &s.hmac_key, nonces, now)?;
        cmd_id.clone_from(&cmd.id);
        login = prefix_cp(&cmd.login, 200).to_string();
        action.clone_from(&cmd.action);
        executor.run(&cmd)
    })();
    let outcome: Result<String, String> = match run {
        Ok(output) => Ok(output),
        Err(AgentError::Rejected(m)) => Err(m),
        Err(AgentError::Os(class)) => Err(format!("{action} failed: {class}")),
        Err(AgentError::NotPorted(_)) => Err(format!("{action}: not yet ported to Rust")),
        Err(other) => return Err(other),
    };
    let ok = outcome.is_ok();
    let mut result = Obj::new();
    result.insert("id".into(), Value::String(cmd_id));
    result.insert("ok".into(), Value::Bool(ok));
    let result_text = match &outcome {
        Ok(output) => {
            result.insert("output".into(), Value::String(output.clone()));
            "ok".to_string()
        }
        Err(error) => {
            result.insert("error".into(), Value::String(error.clone()));
            error.clone()
        }
    };
    let args = audit_args(message);
    let shown = |v: &str| {
        if v.is_empty() {
            "?".to_string()
        } else {
            v.to_string()
        }
    };
    smallworld_core::audit::audit(
        &format!(
            "agent action={} ok={} login={}",
            shown(&action),
            if ok { "True" } else { "False" },
            shown(&login)
        ),
        &[
            ("action", action.as_str()),
            ("login", login.as_str()),
            ("args", args.as_str()),
            ("result", result_text.as_str()),
        ],
    );
    result.insert("finished_at".into(), Value::from(now_ms()));
    Ok(Value::Object(result))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::link::verify::sign;
    use serde_json::json;

    fn settings() -> Settings {
        let t = "t".repeat(32);
        let env = [
            ("INGEST_URL", "https://hb.example.org"),
            ("AGENT_TOKEN", t.as_str()),
            ("COMMAND_HMAC_KEY", t.as_str()),
        ]
        .iter()
        .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
        .collect();
        Settings::from_env(&env).unwrap()
    }

    fn executor() -> Executor {
        Executor::new(std::sync::Arc::new(|| Ok(json!({}))), None, None, None)
    }

    #[test]
    fn results_and_refusals() {
        let s = settings();
        let mut n = Nonces::new(None, 0.0);
        let mut ex = executor();
        let now = crate::sys::now();
        let payload = format!(
            r#"{{"id":"c1","action":"frobnicate","args":{{"b":[1],"a":"é"}},"login":"{}","iat":{},"nonce":"nonce-0001"}}"#,
            "L".repeat(250),
            now_ms()
        );
        let msg = json!({"payload": payload, "sig": sign(&s.hmac_key, &payload)});
        let r = handle_at(&msg, &s, &mut n, &mut ex, now).unwrap();
        assert_eq!(r["id"], "c1");
        assert_eq!(r["ok"], false);
        assert_eq!(r["error"], "unknown action: frobnicate");
        assert!(r["finished_at"].is_i64());
        let bad = handle_at(&json!("x"), &s, &mut n, &mut ex, now).unwrap();
        assert_eq!(
            bad,
            json!({"id": "", "ok": false, "error": "not a command object", "finished_at": bad["finished_at"]})
        );
        assert_eq!(audit_args(&msg), r#"{"a": "\u00e9", "b": [1]}"#);
        assert_eq!(audit_args(&json!({"payload": "{}"})), "{}");
        assert_eq!(audit_args(&json!({"payload": "[1]"})), "");
        let crash = handle_at(
            &json!({"payload": "{}", "sig": "é"}),
            &s,
            &mut n,
            &mut ex,
            now,
        );
        assert!(matches!(crash, Err(AgentError::Crash(_))));
    }
}
