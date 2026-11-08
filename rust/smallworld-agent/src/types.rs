//! Types and constants shared by every module (PORTING.md "Shared"). Change them only in a commit of their own,
//! announced to the other module owners.

use smallworld_core::error::PyError;
use std::fmt;

pub use serde_json::{Map, Value};

/// A JSON object (Python `dict[str, Any]` from `json.loads`).
pub type Obj = Map<String, Value>;

/// The exceptions the Python code raises and catches, by how they are caught. Python's `except` clauses decide
/// by class; each variant is one row of that table:
///
/// | Variant | Python | Caught by |
/// |---|---|---|
/// | `Rejected` | mc_agent.py `Rejected` | `handle_command`, `cli_main` (refused) |
/// | `Action` | mc_panel.py `ActionError` | `Executor.run` (→ `Rejected("{action}: {e}")`), `Jobs._work` (failed) |
/// | `Os` | `OSError` and subclasses, `subprocess.SubprocessError`, `mc_rcon.RconError` | `handle_command`, `cli_main`, `Jobs._work`: reported as `"{action} failed: {class}"` |
/// | `Value` | `ValueError` and subclasses (`JSONDecodeError`, `UnicodeDecodeError`) | `Jobs._work` only |
/// | `Key` | `KeyError` | `Jobs.players`/`announce`/`wait_for_rcon` only |
/// | `Crash` | anything else (`TypeError`, ...) | nothing: the process (cli) or thread ends with a traceback |
/// | `NotPorted` | — | the Rust stub of a module not ported yet |
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AgentError {
    Rejected(String),
    Action(String),
    /// The exception's class name (`type(e).__name__`): `CalledProcessError`, `ConnectionRefusedError`, ...
    Os(String),
    /// Class name and message.
    Value(String, String),
    Key(String),
    /// The traceback's last line (`TypeError: '>' not supported ...`).
    Crash(String),
    /// What isn't ported yet (`module::function`).
    NotPorted(&'static str),
}

pub type AResult<T> = Result<T, AgentError>;

impl AgentError {
    /// `type(e).__name__` as the "failed: {class}" texts print it.
    pub fn class_name(&self) -> String {
        match self {
            AgentError::Rejected(_) => "Rejected".into(),
            AgentError::Action(_) => "ActionError".into(),
            AgentError::Os(c) | AgentError::Value(c, _) => c.clone(),
            AgentError::Key(_) => "KeyError".into(),
            AgentError::Crash(line) => line.split(':').next().unwrap_or("Exception").into(),
            AgentError::NotPorted(_) => "NotPorted".into(),
        }
    }

    /// `except (OSError, mc_rcon.RconError, KeyError, ValueError)`: what `Jobs.players`, `Jobs.announce` and
    /// `wait_for_rcon` swallow.
    pub fn is_rcon_soft(&self) -> bool {
        matches!(
            self,
            AgentError::Os(_) | AgentError::Key(_) | AgentError::Value(..)
        )
    }
}

impl fmt::Display for AgentError {
    /// `str(e)`.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            AgentError::Rejected(m) | AgentError::Action(m) | AgentError::Value(_, m) => {
                f.write_str(m)
            }
            AgentError::Os(c) => f.write_str(c),
            AgentError::Key(k) => write!(f, "'{k}'"),
            AgentError::Crash(line) => f.write_str(line),
            AgentError::NotPorted(what) => write!(f, "{what}: not yet ported to Rust"),
        }
    }
}

impl From<PyError> for AgentError {
    /// smallworld-core's RCON/properties errors, sorted into the table above.
    fn from(e: PyError) -> Self {
        if e.is_caught() {
            return AgentError::Os(e.class_name());
        }
        match e {
            PyError::Key(k) => AgentError::Key(k.display()),
            PyError::Value(m) => AgentError::Value("ValueError".into(), m),
            PyError::UnicodeDecode(m) => AgentError::Value("UnicodeDecodeError".into(), m),
            PyError::UnicodeEncode(m) => AgentError::Value("UnicodeEncodeError".into(), m),
            other => AgentError::Crash(other.traceback_line()),
        }
    }
}

impl From<std::io::Error> for AgentError {
    fn from(e: std::io::Error) -> Self {
        AgentError::Os(smallworld_core::errno::io_class(&e).into())
    }
}

/// A verified command (mc_agent.py `verify`'s dict, or the cli's): `{id, action, args, login}`.
#[derive(Debug, Clone, PartialEq)]
pub struct Command {
    pub id: String,
    pub action: String,
    pub args: Obj,
    pub login: String,
}

/// mc_agent.py `VERSION` (the protocol version the Worker sees, not the product's).
pub const VERSION: &str = "1";
/// mc_agent.py `USER_AGENT`.
pub const USER_AGENT: &str = "smallworld mc-agent";

/// mc_agent.py `ACTIONS`, in Python's order (tests/agent_parity compares them).
pub const ACTIONS: [&str; 19] = [
    "status",
    "console",
    "start",
    "stop",
    "restart",
    "backup",
    "tail",
    "cancel",
    "whitelist",
    "rejected_dismiss",
    "moderate",
    "plugin",
    "restore",
    "player_message",
    "world_create",
    "world_switch",
    "world_delete",
    "selftest",
    "lookup",
];

/// mc_panel.py `COUNTDOWNS`.
pub const COUNTDOWNS: [i64; 4] = [0, 10, 60, 300];
/// mc_panel.py `ANNOUNCE_AT`.
pub const ANNOUNCE_AT: [i64; 12] = [300, 240, 180, 120, 60, 30, 10, 5, 4, 3, 2, 1];
/// mc_agent.py `RESTORE_TIMEOUT_S` (also the world unit's timeout).
pub const RESTORE_TIMEOUT_S: f64 = 7200.0;
/// mc_panel.py `RCON_WAIT_S`.
pub const RCON_WAIT_S: f64 = 300.0;

/// Python's `isinstance(v, int) and not isinstance(v, bool)` for a JSON value: an integer literal.
pub fn as_int(v: &Value) -> Option<i64> {
    match v {
        Value::Number(n) => n.as_i64(),
        _ => None,
    }
}

/// `isinstance(v, int)` in Python, where `bool` is an `int` (True == 1): for the few checks that forgot to
/// exclude bools (worlds.py `load_state`'s `data_version`). Returns the value as Python would compare it.
pub fn as_py_int_incl_bool(v: &Value) -> Option<i64> {
    match v {
        Value::Bool(b) => Some(i64::from(*b)),
        other => as_int(other),
    }
}

/// `str(x)` for a Python value that came from JSON (messages that interpolate unchecked values).
pub fn py_str(v: &Value) -> String {
    smallworld_core::pyjson::py_str(v)
}

/// `bool(x)` (truthiness) for a JSON value.
pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// A Python `for x in value` over what `json.loads` returned: list items, dict keys (as strings), the characters
/// of a string; `None`, bools and numbers raise `TypeError` (`Crash`).
///
/// # Errors
/// `Crash("TypeError: '<type>' object is not iterable")`.
pub fn py_iter(v: &Value) -> AResult<Vec<Value>> {
    match v {
        Value::Array(a) => Ok(a.clone()),
        Value::Object(o) => Ok(o.keys().map(|k| Value::String(k.clone())).collect()),
        Value::String(s) => Ok(s.chars().map(|c| Value::String(c.to_string())).collect()),
        other => Err(AgentError::Crash(format!(
            "TypeError: '{}' object is not iterable",
            crate::worlds::py_type(other)
        ))),
    }
}

/// `float(x)` for a value from `json.loads` (numbers, bools, numeric strings).
///
/// # Errors
/// `Value("ValueError")` for a string that isn't a number, `Crash(TypeError)` for lists, dicts, None.
pub fn py_float(v: &Value) -> AResult<f64> {
    match v {
        Value::Bool(b) => Ok(if *b { 1.0 } else { 0.0 }),
        Value::Number(n) => Ok(n.as_f64().unwrap_or(0.0)),
        Value::String(s) => {
            let t = smallworld_core::pystr::strip_ws(s).replace('_', "");
            let lower = t.to_ascii_lowercase();
            let parsed = match lower.trim_start_matches(['+', '-']) {
                "inf" | "infinity" | "nan" => lower.parse::<f64>().ok(),
                _ if t
                    .chars()
                    .all(|c| c.is_ascii_digit() || matches!(c, '+' | '-' | '.' | 'e' | 'E')) =>
                {
                    t.parse::<f64>().ok()
                }
                _ => None,
            };
            parsed.ok_or_else(|| {
                AgentError::Value(
                    "ValueError".into(),
                    format!(
                        "could not convert string to float: {}",
                        smallworld_core::pyjson::repr_str(s)
                    ),
                )
            })
        }
        other => Err(AgentError::Crash(format!(
            "TypeError: float() argument must be a string or a real number, not '{}'",
            crate::worlds::py_type(other)
        ))),
    }
}

/// `d.get(key)` that treats a missing key as `None` (JSON null).
pub fn get<'a>(obj: &'a Obj, key: &str) -> &'a Value {
    static NULL: Value = Value::Null;
    obj.get(key).unwrap_or(&NULL)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn errors_sort_like_python_except_clauses() {
        let e: AgentError = PyError::Rcon("x".into()).into();
        assert_eq!(e, AgentError::Os("RconError".into()));
        let k: AgentError =
            PyError::Key(smallworld_core::pystr::PyStr::from("rcon.password")).into();
        assert_eq!(k.to_string(), "'rcon.password'");
        assert!(k.is_rcon_soft());
        let s: AgentError = PyError::Struct("unpack requires a buffer of 8 bytes".into()).into();
        assert_eq!(s.class_name(), "struct.error");
        assert!(!s.is_rcon_soft());
    }

    #[test]
    fn int_checks_follow_isinstance() {
        assert_eq!(as_int(&json!(5)), Some(5));
        assert_eq!(as_int(&json!(true)), None);
        assert_eq!(as_int(&json!(5.0)), None);
        assert_eq!(as_py_int_incl_bool(&json!(true)), Some(1));
        assert!(!truthy(&json!("")) && truthy(&json!([0])) && !truthy(&json!(0.0)));
    }
}
