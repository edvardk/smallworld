//! JSON as telemetry.py sees it: Python dicts keep insertion order, and that order reaches bytes that matter (the
//! segments' NDJSON is `json.dumps(row, separators=(",", ":"))`, insertion order; verdicts.tsv lists the Worker's
//! players in its reply's order). serde_json's `Map` is sorted (`preserve_order` is never enabled, see
//! smallworld_core::pyjson), so telemetry builds and reads its documents as `J`, an insertion-ordered value, and
//! converts to `serde_json::Value` only where semantic equality is enough (the Worker's request bodies).
//!
//! `loads` is `json.loads` for the inputs telemetry reads (snapshots, stats files, segment lines, the Worker's
//! reply, its own state files): Python's grammar (`NaN`, `Infinity`, `-Infinity` accepted; strict strings: no raw
//! control characters), a duplicate key keeps its first position with the last value (`dict(pairs)`), a UTF-8 BOM
//! is skipped by the byte readers. Divergences (PORTING.md): an integer beyond 64 bits becomes a float (Python
//! keeps a big int); a lone surrogate escape (`"\ud800"`) makes the document invalid (Rust strings can't hold
//! one); UTF-16/32 input (which `json.loads(bytes)` auto-detects) is invalid.

use crate::types::Value;
use smallworld_core::pyjson::{float_repr, repr_str, string};
use std::fmt::Write as _;

/// A JSON value with Python's dict order.
#[derive(Clone, Debug, PartialEq)]
pub enum J {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    List(Vec<J>),
    Obj(OMap),
}

/// An insertion-ordered object: `insert` of an existing key replaces the value in place (Python's `d[k] = v`).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct OMap(pub Vec<(String, J)>);

impl OMap {
    pub fn new() -> Self {
        Self(Vec::new())
    }

    /// `d[k] = v`.
    pub fn insert(&mut self, key: impl Into<String>, value: impl Into<J>) {
        let key = key.into();
        let value = value.into();
        if let Some(slot) = self.0.iter_mut().find(|(k, _)| *k == key) {
            slot.1 = value;
        } else {
            self.0.push((key, value));
        }
    }

    /// Builder form of `insert`.
    #[must_use]
    pub fn with(mut self, key: &str, value: impl Into<J>) -> Self {
        self.insert(key, value);
        self
    }

    /// `d.get(k)`.
    pub fn get(&self, key: &str) -> Option<&J> {
        self.0.iter().find(|(k, _)| k == key).map(|(_, v)| v)
    }

    pub fn get_mut(&mut self, key: &str) -> Option<&mut J> {
        self.0.iter_mut().find(|(k, _)| k == key).map(|(_, v)| v)
    }

    /// `d.pop(k, None)`.
    pub fn remove(&mut self, key: &str) -> Option<J> {
        let i = self.0.iter().position(|(k, _)| k == key)?;
        Some(self.0.remove(i).1)
    }

    pub fn contains(&self, key: &str) -> bool {
        self.get(key).is_some()
    }

    pub fn len(&self) -> usize {
        self.0.len()
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    pub fn iter(&self) -> impl Iterator<Item = (&String, &J)> {
        self.0.iter().map(|(k, v)| (k, v))
    }

    pub fn keys(&self) -> impl Iterator<Item = &String> {
        self.0.iter().map(|(k, _)| k)
    }
}

impl From<bool> for J {
    fn from(b: bool) -> Self {
        J::Bool(b)
    }
}
impl From<i64> for J {
    fn from(n: i64) -> Self {
        J::Int(n)
    }
}
impl From<usize> for J {
    fn from(n: usize) -> Self {
        J::Int(i64::try_from(n).unwrap_or(i64::MAX))
    }
}
impl From<f64> for J {
    fn from(x: f64) -> Self {
        J::Float(x)
    }
}
impl From<&str> for J {
    fn from(s: &str) -> Self {
        J::Str(s.to_string())
    }
}
impl From<String> for J {
    fn from(s: String) -> Self {
        J::Str(s)
    }
}
impl From<OMap> for J {
    fn from(m: OMap) -> Self {
        J::Obj(m)
    }
}
impl From<Vec<J>> for J {
    fn from(v: Vec<J>) -> Self {
        J::List(v)
    }
}
impl<T: Into<J>> From<Option<T>> for J {
    fn from(v: Option<T>) -> Self {
        v.map_or(J::Null, Into::into)
    }
}

impl J {
    pub fn as_obj(&self) -> Option<&OMap> {
        match self {
            J::Obj(m) => Some(m),
            _ => None,
        }
    }

    pub fn as_list(&self) -> Option<&Vec<J>> {
        match self {
            J::List(v) => Some(v),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            J::Str(s) => Some(s),
            _ => None,
        }
    }

    /// `isinstance(v, int)` (bools are ints in Python) and the value.
    pub fn as_py_int(&self) -> Option<i64> {
        match self {
            J::Int(n) => Some(*n),
            J::Bool(b) => Some(i64::from(*b)),
            _ => None,
        }
    }

    /// Python's truth value.
    pub fn truthy(&self) -> bool {
        match self {
            J::Null => false,
            J::Bool(b) => *b,
            J::Int(n) => *n != 0,
            J::Float(x) => *x != 0.0,
            J::Str(s) => !s.is_empty(),
            J::List(v) => !v.is_empty(),
            J::Obj(m) => !m.is_empty(),
        }
    }

    /// Python's class name (`type(v).__name__`).
    pub fn py_type(&self) -> &'static str {
        match self {
            J::Null => "NoneType",
            J::Bool(_) => "bool",
            J::Int(_) => "int",
            J::Float(_) => "float",
            J::Str(_) => "str",
            J::List(_) => "list",
            J::Obj(_) => "dict",
        }
    }

    /// `str(v)`.
    pub fn py_str(&self) -> String {
        match self {
            J::Str(s) => s.clone(),
            other => other.py_repr(),
        }
    }

    /// `repr(v)`.
    pub fn py_repr(&self) -> String {
        match self {
            J::Null => "None".into(),
            J::Bool(true) => "True".into(),
            J::Bool(false) => "False".into(),
            J::Int(n) => n.to_string(),
            J::Float(x) => py_float_repr(*x),
            J::Str(s) => repr_str(s),
            J::List(v) => format!(
                "[{}]",
                v.iter().map(J::py_repr).collect::<Vec<_>>().join(", ")
            ),
            J::Obj(m) => format!(
                "{{{}}}",
                m.iter()
                    .map(|(k, v)| format!("{}: {}", repr_str(k), v.py_repr()))
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
        }
    }

    /// `json.dumps(v, separators=(",", ":"))`: insertion order, `ensure_ascii`.
    pub fn dumps_compact(&self) -> String {
        let mut out = String::new();
        self.write(&mut out, ",", ":");
        out
    }

    /// `json.dumps(v)` (default separators, insertion order).
    pub fn dumps(&self) -> String {
        let mut out = String::new();
        self.write(&mut out, ", ", ": ");
        out
    }

    fn write(&self, out: &mut String, item: &str, key: &str) {
        match self {
            J::Null => out.push_str("null"),
            J::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            J::Int(n) => {
                let _ = write!(out, "{n}");
            }
            J::Float(x) => out.push_str(&float_repr(*x)),
            J::Str(s) => out.push_str(&string(s, true)),
            J::List(v) => {
                out.push('[');
                for (i, x) in v.iter().enumerate() {
                    if i > 0 {
                        out.push_str(item);
                    }
                    x.write(out, item, key);
                }
                out.push(']');
            }
            J::Obj(m) => {
                out.push('{');
                for (i, (k, v)) in m.iter().enumerate() {
                    if i > 0 {
                        out.push_str(item);
                    }
                    out.push_str(&string(k, true));
                    out.push_str(key);
                    v.write(out, item, key);
                }
                out.push('}');
            }
        }
    }

    /// The same document as a `serde_json::Value` (keys sorted; for request bodies, where meaning is enough).
    /// A non-finite float (never in what telemetry sends) becomes null.
    pub fn to_value(&self) -> Value {
        match self {
            J::Null => Value::Null,
            J::Bool(b) => Value::Bool(*b),
            J::Int(n) => Value::from(*n),
            J::Float(x) => serde_json::Number::from_f64(*x).map_or(Value::Null, Value::Number),
            J::Str(s) => Value::String(s.clone()),
            J::List(v) => Value::Array(v.iter().map(J::to_value).collect()),
            J::Obj(m) => Value::Object(m.iter().map(|(k, v)| (k.clone(), v.to_value())).collect()),
        }
    }

    /// A `serde_json::Value` as `J` (keys in the map's order: sorted).
    pub fn from_value(v: &Value) -> J {
        match v {
            Value::Null => J::Null,
            Value::Bool(b) => J::Bool(*b),
            Value::Number(n) => n
                .as_i64()
                .map_or_else(|| J::Float(n.as_f64().unwrap_or(f64::NAN)), J::Int),
            Value::String(s) => J::Str(s.clone()),
            Value::Array(a) => J::List(a.iter().map(J::from_value).collect()),
            Value::Object(m) => J::Obj(OMap(
                m.iter()
                    .map(|(k, v)| (k.clone(), J::from_value(v)))
                    .collect(),
            )),
        }
    }
}

/// `repr(float)` (Python's `inf`/`nan` spellings, unlike JSON's).
pub fn py_float_repr(x: f64) -> String {
    if x.is_nan() {
        "nan".into()
    } else if x.is_infinite() {
        if x > 0.0 { "inf" } else { "-inf" }.into()
    } else {
        float_repr(x)
    }
}

/// `json.loads(text)`, or `None` where Python raises (or where this reader is stricter: see the module comment).
pub fn loads(text: &str) -> Option<J> {
    let mut p = Parser {
        s: text.as_bytes(),
        i: 0,
    };
    p.ws();
    let v = p.value(0)?;
    p.ws();
    (p.i == p.s.len()).then_some(v)
}

/// `json.loads(data)` for bytes: UTF-8, a leading BOM skipped.
pub fn loads_bytes(data: &[u8]) -> Option<J> {
    let data = data.strip_prefix(b"\xef\xbb\xbf").unwrap_or(data);
    loads(std::str::from_utf8(data).ok()?)
}

struct Parser<'a> {
    s: &'a [u8],
    i: usize,
}

/// Python's default recursion limit stops a deeply nested document long before this.
const MAX_DEPTH: usize = 900;

impl Parser<'_> {
    fn peek(&self) -> Option<u8> {
        self.s.get(self.i).copied()
    }

    fn ws(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.i += 1;
        }
    }

    fn eat(&mut self, lit: &str) -> bool {
        if self.s[self.i..].starts_with(lit.as_bytes()) {
            self.i += lit.len();
            true
        } else {
            false
        }
    }

    fn value(&mut self, depth: usize) -> Option<J> {
        if depth > MAX_DEPTH {
            return None;
        }
        match self.peek()? {
            b'{' => self.object(depth),
            b'[' => self.array(depth),
            b'"' => self.string().map(J::Str),
            b'n' if self.eat("null") => Some(J::Null),
            b't' if self.eat("true") => Some(J::Bool(true)),
            b'f' if self.eat("false") => Some(J::Bool(false)),
            b'N' if self.eat("NaN") => Some(J::Float(f64::NAN)),
            b'I' if self.eat("Infinity") => Some(J::Float(f64::INFINITY)),
            b'-' if self.eat("-Infinity") => Some(J::Float(f64::NEG_INFINITY)),
            b'-' | b'0'..=b'9' => self.number(),
            _ => None,
        }
    }

    fn object(&mut self, depth: usize) -> Option<J> {
        self.i += 1;
        let mut m = OMap::new();
        self.ws();
        if self.peek()? == b'}' {
            self.i += 1;
            return Some(J::Obj(m));
        }
        loop {
            self.ws();
            if self.peek()? != b'"' {
                return None;
            }
            let k = self.string()?;
            self.ws();
            if self.peek()? != b':' {
                return None;
            }
            self.i += 1;
            self.ws();
            let v = self.value(depth + 1)?;
            m.insert(k, v);
            self.ws();
            match self.peek()? {
                b',' => self.i += 1,
                b'}' => {
                    self.i += 1;
                    return Some(J::Obj(m));
                }
                _ => return None,
            }
        }
    }

    fn array(&mut self, depth: usize) -> Option<J> {
        self.i += 1;
        let mut v = Vec::new();
        self.ws();
        if self.peek()? == b']' {
            self.i += 1;
            return Some(J::List(v));
        }
        loop {
            self.ws();
            v.push(self.value(depth + 1)?);
            self.ws();
            match self.peek()? {
                b',' => self.i += 1,
                b']' => {
                    self.i += 1;
                    return Some(J::List(v));
                }
                _ => return None,
            }
        }
    }

    fn hex4(&mut self) -> Option<u32> {
        let h = self.s.get(self.i..self.i + 4)?;
        let h = std::str::from_utf8(h).ok()?;
        if !h.bytes().all(|b| b.is_ascii_hexdigit()) {
            return None;
        }
        self.i += 4;
        u32::from_str_radix(h, 16).ok()
    }

    fn string(&mut self) -> Option<String> {
        self.i += 1;
        let mut out = String::new();
        loop {
            let start = self.i;
            while let Some(b) = self.peek() {
                if b == b'"' || b == b'\\' || b < 0x20 {
                    break;
                }
                self.i += 1;
            }
            out.push_str(std::str::from_utf8(&self.s[start..self.i]).ok()?);
            match self.peek()? {
                b'"' => {
                    self.i += 1;
                    return Some(out);
                }
                b'\\' => {
                    self.i += 1;
                    let c = self.peek()?;
                    self.i += 1;
                    match c {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let hi = self.hex4()?;
                            let cp = if (0xd800..0xdc00).contains(&hi) && self.eat("\\u") {
                                let lo = self.hex4()?;
                                if !(0xdc00..0xe000).contains(&lo) {
                                    return None; // a lone surrogate (Python keeps it): invalid here
                                }
                                0x10000 + ((hi - 0xd800) << 10) + (lo - 0xdc00)
                            } else {
                                hi
                            };
                            out.push(char::from_u32(cp)?);
                        }
                        _ => return None,
                    }
                }
                _ => return None, // a raw control character (strict=True)
            }
        }
    }

    fn number(&mut self) -> Option<J> {
        let start = self.i;
        if self.peek() == Some(b'-') {
            self.i += 1;
        }
        match self.peek()? {
            b'0' => self.i += 1,
            b'1'..=b'9' => {
                while matches!(self.peek(), Some(b'0'..=b'9')) {
                    self.i += 1;
                }
            }
            _ => return None,
        }
        let mut float = false;
        if self.peek() == Some(b'.') && matches!(self.s.get(self.i + 1), Some(b'0'..=b'9')) {
            float = true;
            self.i += 1;
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.i += 1;
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            let save = self.i;
            self.i += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.i += 1;
            }
            if matches!(self.peek(), Some(b'0'..=b'9')) {
                float = true;
                while matches!(self.peek(), Some(b'0'..=b'9')) {
                    self.i += 1;
                }
            } else {
                self.i = save; // Python's scanner stops before a bare "e": the document then fails
            }
        }
        let text = std::str::from_utf8(&self.s[start..self.i]).ok()?;
        if !float && let Ok(n) = text.parse::<i64>() {
            return Some(J::Int(n));
        }
        text.parse::<f64>().ok().map(J::Float)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keeps_python_order_and_spelling() {
        let v = loads(r#"{"b": 1, "a": [1.0, 2.5e-5, 1e16, true, null], "b": "é\n", "c": NaN}"#)
            .unwrap();
        let m = v.as_obj().unwrap();
        assert_eq!(m.keys().collect::<Vec<_>>(), ["b", "a", "c"]);
        let mut m = m.clone();
        m.remove("c");
        // python3 -c 'import json; print(json.dumps({"b": "é\n", "a": [1.0, 2.5e-5, 1e16, True, None]}, separators=(",", ":")))'
        assert_eq!(
            J::Obj(m.clone()).dumps_compact(),
            r#"{"b":"\u00e9\n","a":[1.0,2.5e-05,1e+16,true,null]}"#
        );
        assert_eq!(
            J::Obj(OMap::new().with("src", "ab").with("seq", 3_i64)).dumps(),
            r#"{"src": "ab", "seq": 3}"#
        );
        assert_eq!(
            J::Obj(m).py_str(),
            "{'b': 'é\\n', 'a': [1.0, 2.5e-05, 1e+16, True, None]}"
        );
        assert!(loads("[1,]").is_none());
        assert!(loads("{\"a\":\"\x01\"}").is_none());
        assert!(loads("1.").is_none());
        assert!(loads("01").is_none());
        assert_eq!(loads("-0"), Some(J::Int(0)));
        assert_eq!(loads("-0.0"), Some(J::Float(-0.0)));
        assert_eq!(loads(" \"\\ud83d\\ude00\" "), Some(J::Str("😀".into())));
        assert!(loads("\"\\ud800\"").is_none());
        assert_eq!(
            loads("123456789012345678901234"),
            Some(J::Float(1.234_567_890_123_456_8e23))
        );
        assert_eq!(
            loads_bytes(b"\xef\xbb\xbf[1]"),
            Some(J::List(vec![J::Int(1)]))
        );
        assert_eq!(loads("-Infinity"), Some(J::Float(f64::NEG_INFINITY)));
    }
}
