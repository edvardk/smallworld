//! mc_panel.py `parse_players`, `dump_players`, `apply_entries` (117-189): players.yml in its canonical form
//! (= mcctl.schema.dump_players, byte for byte). Owner: sync.

use super::py::{py_eq, sub, type_error};
use crate::systemd::splitlines;
use crate::types::{AResult, AgentError, Obj, Value, py_iter, py_str};
use regex::Regex;
use smallworld_core::pyjson::{Dumps, dumps, repr_str};
use smallworld_core::pystr::{isspace, prefix_cp};
use std::sync::LazyLock;

/// `SECTIONS`, in order.
pub const SECTIONS: [(&str, &[&str]); 2] = [
    ("whitelist", &["name", "uuid"]),
    ("ops", &["name", "uuid", "level"]),
];

static HEAD: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(whitelist|ops):( \[\])?$").unwrap());
static ITEM: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^  - \{(.*)\}$").unwrap());
// Python's `\s` (str.isspace) also matches U+001C..U+001F, Rust's doesn't; `\d` is Unicode in both.
static PAIR: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"^[\s\x1C-\x1F]*([a-z]+): ("(?:[^"\\]|\\.)*"|-?\d+)[\s\x1C-\x1F]*(?:,|$)"#)
        .unwrap()
});

fn value_error(msg: String) -> AgentError {
    AgentError::Value("ValueError".into(), msg)
}

fn decode_error(msg: &str, pos: usize) -> AgentError {
    AgentError::Value(
        "JSONDecodeError".into(),
        format!("{msg}: line 1 column {} (char {pos})", pos + 1),
    )
}

fn hex4(cps: &[char], at: usize) -> Option<u32> {
    let s: String = cps.get(at..at + 4)?.iter().collect();
    if s.chars().all(|c| c.is_ascii_hexdigit()) {
        u32::from_str_radix(&s, 16).ok()
    } else {
        None
    }
}

/// `json.loads(value)` for what PAIR's value group can hold (a quoted string or `-?\d+`), with the messages of
/// Python's C scanner. A lone surrogate escape (Python keeps it; a Rust string can't) is a `ValueError`
/// (PORTING.md "Divergences").
///
/// # Errors
/// `JSONDecodeError` (a `ValueError`) as Python words it.
pub fn json_scalar(text: &str) -> AResult<Value> {
    let cps: Vec<char> = text.chars().collect();
    if cps.first() != Some(&'"') {
        let neg = usize::from(cps.first() == Some(&'-'));
        let digits = cps[neg..].iter().take_while(|c| c.is_ascii_digit()).count();
        if digits == 0 {
            return Err(decode_error("Expecting value", 0));
        }
        let end = neg + if cps[neg] == '0' { 1 } else { digits };
        if end < cps.len() {
            return Err(decode_error("Extra data", end));
        }
        return Ok(match text.parse::<i64>() {
            Ok(i) => Value::from(i),
            Err(_) => Value::from(text.parse::<f64>().unwrap_or(0.0)),
        });
    }
    let mut out = String::new();
    let mut pos = 1;
    while pos + 1 < cps.len() {
        let c = cps[pos];
        if (c as u32) < 0x20 {
            return Err(decode_error("Invalid control character at", pos));
        }
        if c != '\\' {
            out.push(c);
            pos += 1;
            continue;
        }
        let e = cps.get(pos + 1).copied().unwrap_or('"');
        let simple = match e {
            '"' => Some('"'),
            '\\' => Some('\\'),
            '/' => Some('/'),
            'b' => Some('\u{8}'),
            'f' => Some('\u{c}'),
            'n' => Some('\n'),
            'r' => Some('\r'),
            't' => Some('\t'),
            _ => None,
        };
        if let Some(ch) = simple {
            out.push(ch);
            pos += 2;
            continue;
        }
        if e != 'u' {
            return Err(decode_error("Invalid \\escape", pos));
        }
        let Some(u) = hex4(&cps, pos + 2) else {
            return Err(decode_error("Invalid \\uXXXX escape", pos + 1));
        };
        let mut cp = u;
        let mut len = 6;
        if (0xD800..0xDC00).contains(&u)
            && cps.get(pos + 6) == Some(&'\\')
            && cps.get(pos + 7) == Some(&'u')
        {
            let Some(low) = hex4(&cps, pos + 8) else {
                return Err(decode_error("Invalid \\uXXXX escape", pos + 7));
            };
            if (0xDC00..0xE000).contains(&low) {
                cp = 0x10000 + ((u - 0xD800) << 10) + (low - 0xDC00);
                len = 12;
            }
        }
        let Some(ch) = char::from_u32(cp) else {
            return Err(value_error(format!(
                "a lone surrogate escape at char {pos} (not supported by the Rust agent)"
            )));
        };
        out.push(ch);
        pos += len;
    }
    Ok(Value::String(out))
}

/// `parse_players(text)`: `{whitelist: [...], ops: [...]}` from the canonical form.
///
/// # Errors
/// `ValueError` with Python's text for a file not in canonical form.
pub fn parse_players(text: &str) -> AResult<Obj> {
    let mut out = Obj::new();
    let mut section: Option<&'static str> = None;
    for raw in splitlines(text) {
        let line = raw.trim_end_matches(isspace);
        if line.is_empty() || line.trim_start_matches(isspace).starts_with('#') {
            continue;
        }
        if let Some(h) = HEAD.captures(line) {
            let s = if &h[1] == "whitelist" {
                "whitelist"
            } else {
                "ops"
            };
            section = Some(s);
            out.insert(s.into(), Value::Array(Vec::new()));
            continue;
        }
        let (Some(m), Some(sec)) = (ITEM.captures(line), section) else {
            return Err(value_error(format!(
                "players.yml isn't in canonical form near {}",
                repr_str(prefix_cp(line, 60))
            )));
        };
        let body = m.get(1).map_or("", |b| b.as_str());
        let mut item = Obj::new();
        let mut pos = 0;
        while pos < body.len() {
            let Some(p) = PAIR.captures(&body[pos..]) else {
                return Err(value_error(format!(
                    "players.yml: can't read {}",
                    repr_str(prefix_cp(body, 60))
                )));
            };
            item.insert(p[1].to_string(), json_scalar(&p[2])?);
            pos += p.get(0).map_or(0, |x| x.end());
        }
        let keys = SECTIONS
            .iter()
            .find(|(s, _)| *s == sec)
            .map_or(&[][..], |(_, k)| *k);
        if item.len() != keys.len() || !keys.iter().all(|k| item.contains_key(*k)) {
            return Err(value_error(format!(
                "players.yml: {sec} entries have {}",
                keys.join(", ")
            )));
        }
        if let Some(Value::Array(a)) = out.get_mut(sec) {
            a.push(Value::Object(item));
        }
    }
    for (s, _) in SECTIONS {
        out.entry(s).or_insert_with(|| Value::Array(Vec::new()));
    }
    Ok(out)
}

/// `dump_players(players)`: the canonical text.
///
/// # Errors
/// The `KeyError`/`TypeError` Python raises for an item without `name`/`uuid` (callers pass parsed files).
pub fn dump_players(players: &Obj) -> AResult<String> {
    let mut lines: Vec<String> = Vec::new();
    for (section, keys) in SECTIONS {
        let v = crate::types::get(players, section);
        let items = if crate::types::truthy(v) {
            py_iter(v)?
        } else {
            Vec::new()
        };
        if items.is_empty() {
            lines.push(format!("{section}: []"));
            continue;
        }
        lines.push(format!("{section}:"));
        let mut keyed = Vec::new();
        for item in items {
            let k = (
                py_str(sub(&item, "name")?).to_lowercase(),
                py_str(sub(&item, "uuid")?),
            );
            keyed.push((k, item));
        }
        keyed.sort_by(|a, b| a.0.cmp(&b.0));
        for (_, item) in keyed {
            let mut pairs = Vec::new();
            for k in keys {
                pairs.push(format!("{k}: {}", dumps(sub(&item, k)?, &Dumps::python())));
            }
            lines.push(format!("  - {{{}}}", pairs.join(", ")));
        }
    }
    Ok(lines.join("\n") + "\n")
}

fn list_of(players: &Obj, key: &str) -> AResult<Vec<Value>> {
    match players.get(key) {
        None => Ok(Vec::new()),
        Some(v) => py_iter(v),
    }
}

/// `apply_entries(players, entries)`: players.yml with the pending whitelist changes applied, and the uuids
/// that changed it. Ops are never touched.
///
/// # Errors
/// Python's `KeyError`/`TypeError` for entries without `op`/`uuid`/`name`.
pub fn apply_entries(players: &Obj, entries: &[Value]) -> AResult<(Obj, Vec<Value>)> {
    // an insertion-ordered dict uuid -> item (deleting and re-adding moves a key to the end, as in Python)
    let mut wl: Vec<(Value, Value)> = Vec::new();
    for p in list_of(players, "whitelist")? {
        let uuid = sub(&p, "uuid")?.clone();
        let Value::Object(o) = &p else {
            return Err(type_error(
                "cannot convert dictionary update sequence element #0 to a sequence",
            ));
        };
        let item = Value::Object(o.clone());
        match wl.iter_mut().find(|(k, _)| py_eq(k, &uuid)) {
            Some(slot) => slot.1 = item,
            None => wl.push((uuid, item)),
        }
    }
    let ops_list = list_of(players, "ops")?;
    let mut ops = Vec::new();
    for o in &ops_list {
        ops.push(sub(o, "uuid")?.clone());
    }
    let mut changed = Vec::new();
    for e in entries {
        let op = sub(e, "op")?;
        let uuid = sub(e, "uuid")?.clone();
        let present = wl.iter().position(|(k, _)| py_eq(k, &uuid));
        if op == "add" && present.is_none() {
            let mut item = Obj::new();
            item.insert("name".into(), sub(e, "name")?.clone());
            item.insert("uuid".into(), uuid.clone());
            wl.push((uuid.clone(), Value::Object(item)));
            changed.push(uuid);
        } else if op == "remove"
            && let Some(i) = present
            && !ops.iter().any(|o| py_eq(o, &uuid))
        {
            wl.remove(i);
            changed.push(uuid);
        }
    }
    let mut out = Obj::new();
    out.insert(
        "whitelist".into(),
        Value::Array(wl.into_iter().map(|(_, v)| v).collect()),
    );
    out.insert("ops".into(), Value::Array(ops_list));
    Ok((out, changed))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn vectors() -> Value {
        serde_json::from_str(include_str!(
            "../../../../tests/agent_parity/vectors/players.json"
        ))
        .unwrap()
    }

    #[test]
    fn parse_matches_the_vectors() {
        let v = vectors();
        for case in v["vectors"]["parse"].as_array().unwrap() {
            let input = case["input"].as_str().unwrap();
            let got = parse_players(input);
            let want = &case["parse"];
            match (got, want.get("ok")) {
                (Ok(o), Some(ok)) => assert_eq!(Value::Object(o), *ok, "{input:?}"),
                (Err(e), None) => {
                    assert_eq!(e.to_string(), want["error"].as_str().unwrap(), "{input:?}");
                }
                (got, _) => panic!("{input:?}: got {got:?}, want {want}"),
            }
        }
    }

    #[test]
    fn dump_matches_the_vectors() {
        let v = vectors();
        for case in v["vectors"]["dump"].as_array().unwrap() {
            let got = dump_players(case["input"].as_object().unwrap()).unwrap();
            assert_eq!(got, case["dump"].as_str().unwrap());
        }
    }

    #[test]
    fn apply_entries_matches_the_vectors() {
        let v = vectors();
        for case in v["vectors"]["apply_entries"].as_array().unwrap() {
            let (new, changed) = apply_entries(
                case["players"].as_object().unwrap(),
                case["entries"].as_array().unwrap(),
            )
            .unwrap();
            assert_eq!(json!([new, changed]), case["result"]);
        }
    }

    #[test]
    fn json_scalar_errors_like_pythons_scanner() {
        // python3 -c 'import json; json.loads(...)' for each
        for (input, want) in [
            (r#""a\xb""#, "Invalid \\escape: line 1 column 3 (char 2)"),
            (
                "\"a\tb\"",
                "Invalid control character at: line 1 column 3 (char 2)",
            ),
            (
                r#""ab\u12g4""#,
                "Invalid \\uXXXX escape: line 1 column 5 (char 4)",
            ),
            (
                r#""\ud800\u12g4""#,
                "Invalid \\uXXXX escape: line 1 column 9 (char 8)",
            ),
            (r#""\U0041""#, "Invalid \\escape: line 1 column 2 (char 1)"),
            ("01", "Extra data: line 1 column 2 (char 1)"),
            ("-01", "Extra data: line 1 column 3 (char 2)"),
            ("\u{663}", "Expecting value: line 1 column 1 (char 0)"),
            ("1\u{663}", "Extra data: line 1 column 2 (char 1)"),
            ("-\u{663}", "Expecting value: line 1 column 1 (char 0)"),
        ] {
            assert_eq!(json_scalar(input).unwrap_err().to_string(), want, "{input}");
        }
        assert_eq!(json_scalar(r#""😀\/""#).unwrap(), json!("\u{1f600}/"));
        assert_eq!(json_scalar("-0").unwrap(), json!(0));
    }
}
