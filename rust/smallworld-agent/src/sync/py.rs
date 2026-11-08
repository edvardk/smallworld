//! Python's view of JSON values where the sync round depends on it: `==` (`1 == 1.0 == True`), `int(x)`,
//! `x[key]` and `x.get(key)` with the exceptions Python raises, and how the loop prints an exception. Owner: sync.

use crate::types::{AResult, AgentError, Obj, Value};
use crate::worlds::py_type;
use smallworld_core::pyjson::repr_str;
use smallworld_core::pystr::strip_ws;

/// `TypeError(msg)`: a `Crash` line (Python's `except TypeError` clauses in the round catch it: `is_type_error`).
pub fn type_error(msg: &str) -> AgentError {
    AgentError::Crash(format!("TypeError: {msg}"))
}

pub fn is_type_error(e: &AgentError) -> bool {
    matches!(e, AgentError::Crash(l) if l.starts_with("TypeError: "))
}

/// `str(e)` for an exception the round catches (the `TypeError: ` prefix of a crash line is the class).
pub fn exc_str(e: &AgentError) -> String {
    match e {
        AgentError::Crash(l) => l.split_once(": ").map_or(l.clone(), |(_, m)| m.to_string()),
        other => other.to_string(),
    }
}

/// `f"{type(e).__name__}: {e}"`.
pub fn exc_line(e: &AgentError) -> String {
    match e {
        AgentError::Crash(l) => l.clone(),
        other => format!("{}: {other}", other.class_name()),
    }
}

/// `x[key]` for a str key.
///
/// # Errors
/// `KeyError(key)`, or the `TypeError` Python raises for a value that isn't a dict.
pub fn sub<'a>(v: &'a Value, key: &str) -> AResult<&'a Value> {
    match v {
        Value::Object(o) => o.get(key).ok_or_else(|| AgentError::Key(key.to_string())),
        Value::String(_) => Err(type_error("string indices must be integers, not 'str'")),
        Value::Array(_) => Err(type_error(
            "list indices must be integers or slices, not str",
        )),
        other => Err(type_error(&format!(
            "'{}' object is not subscriptable",
            py_type(other)
        ))),
    }
}

/// `x.get(key)` (`None` when missing).
///
/// # Errors
/// `AttributeError` (a crash nothing in the round catches) for a value that isn't a dict.
pub fn get_attr<'a>(v: &'a Value, key: &str) -> AResult<&'a Value> {
    static NULL: Value = Value::Null;
    match v {
        Value::Object(o) => Ok(o.get(key).unwrap_or(&NULL)),
        other => Err(AgentError::Crash(format!(
            "AttributeError: '{}' object has no attribute 'get'",
            py_type(other)
        ))),
    }
}

/// `x.get(key)` on a dict.
pub fn oget<'a>(o: &'a Obj, key: &str) -> &'a Value {
    crate::types::get(o, key)
}

fn num(v: &Value) -> Option<f64> {
    match v {
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        Value::Number(n) => n.as_f64(),
        _ => None,
    }
}

/// Python `a == b` for values from `json.loads` (`True == 1 == 1.0`; dicts by keys and values).
pub fn py_eq(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Null, Value::Null) => true,
        (Value::String(x), Value::String(y)) => x == y,
        (Value::Array(x), Value::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(p, q)| py_eq(p, q))
        }
        (Value::Object(x), Value::Object(y)) => obj_eq(x, y),
        (Value::Number(x), Value::Number(y)) if x.is_i64() && y.is_i64() => {
            x.as_i64() == y.as_i64()
        }
        _ => match (num(a), num(b)) {
            (Some(x), Some(y)) => x == y,
            _ => false,
        },
    }
}

pub fn obj_eq(x: &Obj, y: &Obj) -> bool {
    x.len() == y.len() && x.iter().all(|(k, v)| y.get(k).is_some_and(|w| py_eq(v, w)))
}

/// `int(x)`.
///
/// # Errors
/// Python's `ValueError`, `OverflowError` (a crash) or `TypeError`.
pub fn py_int(v: &Value) -> AResult<i64> {
    match v {
        Value::Bool(b) => Ok(i64::from(*b)),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                return Ok(i);
            }
            let f = n.as_f64().unwrap_or(0.0);
            if f.is_nan() {
                Err(AgentError::Value(
                    "ValueError".into(),
                    "cannot convert float NaN to integer".into(),
                ))
            } else if f.is_infinite() {
                Err(AgentError::Crash(
                    "OverflowError: cannot convert float infinity to integer".into(),
                ))
            } else {
                #[allow(clippy::cast_possible_truncation)]
                Ok(f.trunc() as i64)
            }
        }
        Value::String(s) => parse_int_str(s).ok_or_else(|| {
            AgentError::Value(
                "ValueError".into(),
                format!("invalid literal for int() with base 10: {}", repr_str(s)),
            )
        }),
        other => Err(type_error(&format!(
            "int() argument must be a string, a bytes-like object or a real number, not '{}'",
            py_type(other)
        ))),
    }
}

/// `int(s)` for a str: whitespace, a sign, decimal digits (any `Nd`) with single underscores between them.
fn parse_int_str(s: &str) -> Option<i64> {
    let t = strip_ws(s);
    let (neg, digits) = match t.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, t.strip_prefix('+').unwrap_or(t)),
    };
    if digits.starts_with('_') || digits.ends_with('_') || digits.contains("__") {
        return None;
    }
    let n = crate::unicode::parse_decimal(&digits.replace('_', ""))?;
    Some(if neg { -n } else { n })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn python_equality_and_int() {
        assert!(py_eq(&json!({"a": [1, true]}), &json!({"a": [1.0, 1]})));
        assert!(!py_eq(&json!({"a": 1}), &json!({"a": 1, "b": null})));
        assert!(!py_eq(&json!("1"), &json!(1)));
        assert_eq!(py_int(&json!(7.9)).unwrap(), 7);
        assert_eq!(py_int(&json!(" -1_2 ")).unwrap(), -12);
        assert_eq!(
            py_int(&json!("x")).unwrap_err().to_string(),
            "invalid literal for int() with base 10: 'x'"
        );
        assert!(is_type_error(&py_int(&json!(null)).unwrap_err()));
        assert_eq!(
            exc_str(&sub(&json!(null), "a").unwrap_err()),
            "'NoneType' object is not subscriptable"
        );
        assert_eq!(
            exc_line(&sub(&json!({}), "a").unwrap_err()),
            "KeyError: 'a'"
        );
    }
}
