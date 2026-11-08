//! JSON exactly as Python writes and reads it, where the bytes matter (feature `json`).
//!
//! `json.dumps` byte for byte: the audit's `MC_ARGS` and the cli's sorted lists (`sort_keys=True`, default
//! separators `", "`/`": "`), compact bodies (`separators=(",", ":")`), pending.json (`indent=1`, which makes
//! the item separator `","`), `ensure_ascii` (everything outside U+0020..U+007E escaped as lowercase `\uXXXX`,
//! surrogate pairs above U+FFFF), and floats as `repr(float)` (`1.0`, `1e+16`, `1.5e-05`).
//!
//! **Objects come out sorted by key.** serde_json's `Map` is a `BTreeMap` (the `preserve_order` feature is never
//! enabled: a unit test checks), so `dumps` always matches `sort_keys=True`. Where Python dumps an object in
//! insertion order and the bytes matter (the job announcements' `tellraw`/`title` components), build the text
//! from `string()` pieces instead.
//!
//! Reading: `read_json` is mc_panel.py's `read_json(path, default)`: `None` (the caller's default) on any read,
//! decode or parse error. serde_json is stricter than Python's `json` on input nobody legitimate writes
//! (`NaN`/`Infinity`, lone surrogate escapes); such a file reads as the default. Integers beyond 64 bits become
//! floats (Python keeps them exact). Both are documented divergences (rust/smallworld-agent/PORTING.md).

use serde_json::Value;
use std::fmt::Write as _;
use std::path::Path;

/// `json.dumps` options. `Dumps::python()` is Python's defaults (`ensure_ascii=True`, `", "`, `": "`).
#[derive(Clone, Copy, Debug)]
pub struct Dumps {
    pub indent: Option<usize>,
    pub item_sep: &'static str,
    pub key_sep: &'static str,
    pub ensure_ascii: bool,
}

impl Dumps {
    /// `json.dumps(v, sort_keys=True)`.
    pub const fn python() -> Self {
        Self {
            indent: None,
            item_sep: ", ",
            key_sep: ": ",
            ensure_ascii: true,
        }
    }

    /// `json.dumps(v, separators=(",", ":"))`.
    pub const fn compact() -> Self {
        Self {
            indent: None,
            item_sep: ",",
            key_sep: ":",
            ensure_ascii: true,
        }
    }

    /// `json.dumps(v, sort_keys=True, indent=n)` (the item separator becomes "," with an indent).
    pub const fn indented(n: usize) -> Self {
        Self {
            indent: Some(n),
            item_sep: ",",
            key_sep: ": ",
            ensure_ascii: true,
        }
    }
}

/// `json.dumps(value, ...)` with sorted keys.
pub fn dumps(value: &Value, opts: &Dumps) -> String {
    let mut out = String::new();
    write_value(&mut out, value, opts, 0);
    out
}

/// `json.dumps(value, sort_keys=True)`.
pub fn dumps_sorted(value: &Value) -> String {
    dumps(value, &Dumps::python())
}

/// `json.dumps(value, separators=(",", ":"))` (keys sorted: see the module comment).
pub fn dumps_compact(value: &Value) -> String {
    dumps(value, &Dumps::compact())
}

fn newline(out: &mut String, opts: &Dumps, level: usize) {
    if let Some(n) = opts.indent {
        out.push('\n');
        out.extend(std::iter::repeat_n(' ', n * level));
    }
}

fn write_value(out: &mut String, value: &Value, opts: &Dumps, level: usize) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                let _ = write!(out, "{i}");
            } else if let Some(u) = n.as_u64() {
                let _ = write!(out, "{u}");
            } else {
                out.push_str(&float_repr(n.as_f64().unwrap_or(f64::NAN)));
            }
        }
        Value::String(s) => out.push_str(&string(s, opts.ensure_ascii)),
        Value::Array(items) => {
            if items.is_empty() {
                out.push_str("[]");
                return;
            }
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(opts.item_sep);
                }
                newline(out, opts, level + 1);
                write_value(out, item, opts, level + 1);
            }
            newline(out, opts, level);
            out.push(']');
        }
        Value::Object(map) => {
            if map.is_empty() {
                out.push_str("{}");
                return;
            }
            out.push('{');
            for (i, (k, v)) in map.iter().enumerate() {
                if i > 0 {
                    out.push_str(opts.item_sep);
                }
                newline(out, opts, level + 1);
                out.push_str(&string(k, opts.ensure_ascii));
                out.push_str(opts.key_sep);
                write_value(out, v, opts, level + 1);
            }
            newline(out, opts, level);
            out.push('}');
        }
    }
}

/// A JSON string literal as `json.dumps(s, ensure_ascii=...)` writes it.
pub fn string(s: &str, ensure_ascii: bool) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if u32::from(c) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", u32::from(c));
            }
            c if ensure_ascii && !(' '..='~').contains(&c) => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// `repr(x)` / `json.dumps(x)` for a finite float: the shortest digits that round-trip, positional when the
/// decimal exponent is in -4..16, else scientific with a sign and at least two exponent digits.
pub fn float_repr(x: f64) -> String {
    if x.is_nan() {
        return "NaN".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "Infinity" } else { "-Infinity" }.into();
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0.0" } else { "0.0" }.into();
    }
    // Rust's `{:e}` prints the shortest round-trip digits: "-1.2345e3".
    let e = format!("{x:e}");
    let (mantissa, exp) = e.split_once('e').unwrap_or((&e, "0"));
    let exp: i32 = exp.parse().unwrap_or(0);
    let (neg, mantissa) = mantissa
        .strip_prefix('-')
        .map_or((false, mantissa), |m| (true, m));
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let ndigits = i32::try_from(digits.len()).unwrap_or(i32::MAX);
    let decpt = exp + 1; // digits "d1d2..." times 10^(decpt - ndigits)
    let mut out = String::new();
    if neg {
        out.push('-');
    }
    if (-4 < decpt) && (decpt <= 16) {
        if decpt <= 0 {
            out.push_str("0.");
            out.extend(std::iter::repeat_n(
                '0',
                usize::try_from(-decpt).unwrap_or(0),
            ));
            out.push_str(&digits);
        } else if decpt >= ndigits {
            out.push_str(&digits);
            out.extend(std::iter::repeat_n(
                '0',
                usize::try_from(decpt - ndigits).unwrap_or(0),
            ));
            out.push_str(".0");
        } else {
            let split = usize::try_from(decpt).unwrap_or(0);
            out.push_str(&digits[..split]);
            out.push('.');
            out.push_str(&digits[split..]);
        }
    } else {
        out.push_str(&digits[..1]);
        if digits.len() > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        let _ = write!(out, "e{}{:02}", if exp < 0 { '-' } else { '+' }, exp.abs());
    }
    out
}

/// `json.loads(text)`, or `None` where Python would raise (or where serde_json is stricter; see above).
pub fn loads(text: &str) -> Option<Value> {
    serde_json::from_str(text).ok()
}

/// mc_panel.py `read_json(path, default)`: `None` means "the default" (any `OSError` or `ValueError`).
pub fn read_json(path: &Path) -> Option<Value> {
    loads(&crate::fsx::read_text(path)?)
}

/// `str(value)` for a JSON value as Python prints it after `json.loads` (`True`, `None`, `1.5`, `[1, 2]`, ...):
/// for messages that interpolate a value of unchecked type.
pub fn py_str(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::String(s) => s.clone(),
        other => py_repr(other),
    }
}

/// `repr(value)` for a JSON value after `json.loads`.
pub fn py_repr(value: &Value) -> String {
    match value {
        Value::Null | Value::Bool(_) => py_str(value),
        Value::Number(_) => dumps(value, &Dumps::python()),
        Value::String(s) => repr_str(s),
        Value::Array(items) => {
            let parts: Vec<String> = items.iter().map(py_repr).collect();
            format!("[{}]", parts.join(", "))
        }
        Value::Object(map) => {
            // json.loads keeps the file's key order; serde_json sorts. Only used in messages.
            let parts: Vec<String> = map
                .iter()
                .map(|(k, v)| format!("{}: {}", repr_str(k), py_repr(v)))
                .collect();
            format!("{{{}}}", parts.join(", "))
        }
    }
}

/// `repr(s)` for a str (via the PyStr implementation, which has Python's quoting and escapes).
pub fn repr_str(s: &str) -> String {
    crate::pystr::PyStr::from(s).repr()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn dumps_matches_python_defaults() {
        // python3 -c 'import json; print(json.dumps({"b": [1, 2.5, None], "a": "é\n\"x\" 😀\x7f"}, sort_keys=True))'
        let v = json!({"b": [1, 2.5, null], "a": "é\n\"x\" 😀\u{7f}"});
        assert_eq!(
            dumps_sorted(&v),
            concat!(
                r#"{"a": ""#,
                r"\",
                "u00e9",
                r#"\n\"x\" "#,
                r"\",
                "ud83d",
                r"\",
                "ude00",
                r"\",
                r#"u007f", "b": [1, 2.5, null]}"#
            )
        );
        assert_eq!(
            dumps_compact(&json!({"x": [true, false], "a": {}})),
            r#"{"a":{},"x":[true,false]}"#
        );
        assert_eq!(dumps_sorted(&json!({})), "{}");
        assert_eq!(dumps_sorted(&json!([])), "[]");
    }

    #[test]
    fn indent_matches_python() {
        // json.dumps({"entries": [{"a": 1}], "pr": None, "e": []}, sort_keys=True, indent=1)
        let v = json!({"entries": [{"a": 1}], "pr": null, "e": []});
        assert_eq!(
            dumps(&v, &Dumps::indented(1)),
            "{\n \"e\": [],\n \"entries\": [\n  {\n   \"a\": 1\n  }\n ],\n \"pr\": null\n}"
        );
    }

    #[test]
    fn floats_match_repr() {
        let cases = [
            (1.0, "1.0"),
            (0.5, "0.5"),
            (1e16, "1e+16"),
            (1.5e-5, "1.5e-05"),
            (0.0001, "0.0001"),
            (123_456_789.125, "123456789.125"),
            (1_700_000_000.123_456_7, "1700000000.1234567"),
            (-2.5, "-2.5"),
            (1e22, "1e+22"),
            (9_999_999_999_999_998.0, "9999999999999998.0"),
            (0.1 + 0.2, "0.30000000000000004"),
            (-0.0, "-0.0"),
        ];
        for (x, want) in cases {
            assert_eq!(float_repr(x), want, "{x}");
        }
    }

    #[test]
    fn ensure_ascii_off_escapes_only_controls() {
        assert_eq!(string("é\u{1}\u{7f}", false), "\"é\\u0001\u{7f}\"");
    }

    #[test]
    fn maps_iterate_sorted() {
        // If serde_json's preserve_order ever got enabled, dumps would stop matching sort_keys=True.
        let v: Value = serde_json::from_str(r#"{"z": 1, "a": 2, "é": 3, "B": 4}"#).unwrap();
        assert_eq!(
            dumps_sorted(&v),
            concat!(r#"{"B": 4, "a": 2, "z": 1, ""#, r"\", r#"u00e9": 3}"#)
        );
    }

    #[test]
    fn py_str_and_repr() {
        assert_eq!(py_str(&json!(true)), "True");
        assert_eq!(py_str(&json!(null)), "None");
        assert_eq!(py_str(&json!("x")), "x");
        assert_eq!(py_repr(&json!(["a", 1, 2.0])), "['a', 1, 2.0]");
    }

    #[test]
    fn read_json_defaults() {
        let d = std::env::temp_dir().join(format!("swcore-json-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let p = d.join("x.json");
        assert_eq!(read_json(&p), None);
        std::fs::write(&p, "{\"a\": 1}").unwrap();
        assert_eq!(read_json(&p), Some(json!({"a": 1})));
        std::fs::write(&p, "{nope").unwrap();
        assert_eq!(read_json(&p), None);
        std::fs::write(&p, b"\"\xff\"").unwrap();
        assert_eq!(read_json(&p), None);
        std::fs::remove_dir_all(d).unwrap();
    }
}
