//! worlds.py `_q`, `library_line`, `set_world_section` (server.yml's world section in canonical form, for the
//! panel's world pull requests). Owner: sync.

use super::py::{get_attr, sub, type_error};
use crate::systemd::splitlines;
use crate::types::{AResult, AgentError, Value, py_iter, truthy};
use crate::worlds::DIMENSIONS;
use regex::Regex;
use smallworld_core::pyjson::{Dumps, dumps};
use smallworld_core::pystr::{isspace, len_cp, strip_ws};
use std::sync::LazyLock;

// Python's `\s` = str.isspace (U+001C..U+001F included).
static HEAD: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^world:([\s\x1C-\x1F]|$)").unwrap());
static HEAD_BLOCK: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^world:[\s\x1C-\x1F]*(#.*)?$").unwrap());
static KEY: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^  (active|library):").unwrap());

/// `_q(v)` = `json.dumps(v)`.
pub fn q(v: &Value) -> String {
    dumps(v, &Dumps::python())
}

fn get_or<'a>(entry: &'a Value, key: &str, default: &'a Value) -> AResult<&'a Value> {
    match entry {
        Value::Object(o) => Ok(o.get(key).unwrap_or(default)),
        _ => get_attr(entry, key),
    }
}

/// `library_line(entry)`: one canonical flow-style item of world.library.
///
/// # Errors
/// Python's `KeyError`/`TypeError`/`AttributeError` for an entry that isn't a checked one.
pub fn library_line(entry: &Value) -> AResult<String> {
    let empty = Value::String(String::new());
    let default = Value::String("default".into());
    let null = Value::Null;
    let mut pairs = vec![
        format!("name: {}", q(sub(entry, "name")?)),
        format!("seed: {}", q(get_or(entry, "seed", &empty)?)),
        format!("type: {}", q(get_or(entry, "type", &default)?)),
        format!("preset: {}", q(get_or(entry, "preset", &null)?)),
    ];
    let border = get_or(entry, "border_radius", &null)?;
    if !border.is_null() {
        let mut inner = Vec::new();
        for d in DIMENSIONS {
            let short = match d {
                "minecraft:overworld" => "overworld",
                "minecraft:the_nether" => "nether",
                _ => "end",
            };
            inner.push(format!("{short}: {}", q(get_attr(border, d)?)));
        }
        pairs.push(format!("border_radius: {{{}}}", inner.join(", ")));
    }
    let pregen = get_or(entry, "pregen", &null)?;
    if !pregen.is_null() {
        pairs.push(format!("pregen: {}", q(pregen)));
    }
    Ok(format!("    - {{{}}}", pairs.join(", ")))
}

fn value_error(msg: &str) -> AgentError {
    AgentError::Value("ValueError".into(), msg.into())
}

fn lstrip(s: &str) -> &str {
    s.trim_start_matches(isspace)
}

/// `set_world_section(text, active, library)`: server.yml with world.active and world.library replaced by
/// their canonical form; every other line stays.
///
/// # Errors
/// Python's `ValueError` texts for a `world:` the panel can't edit; `TypeError`/`KeyError` for a broken library.
pub fn set_world_section(text: &str, active: &Value, library: &Value) -> AResult<String> {
    let mut lines: Vec<String> = splitlines(text).into_iter().map(String::from).collect();
    let items = py_iter(library)?;
    let mut body = vec![
        format!("  active: {}", q(active)),
        format!("  library:{}", if truthy(library) { "" } else { " []" }),
    ];
    let mut keyed = Vec::new();
    for e in &items {
        let name = sub(e, "name")?;
        keyed.push((name.clone(), e));
    }
    if keyed.len() > 1 && keyed.iter().any(|(n, _)| !n.is_string()) {
        return Err(type_error(
            "'<' not supported between instances of these names",
        ));
    }
    keyed.sort_by(|a, b| a.0.as_str().cmp(&b.0.as_str()));
    for (_, e) in keyed {
        body.push(library_line(e)?);
    }
    let head = lines.iter().position(|ln| HEAD.is_match(ln));
    let Some(head) = head else {
        while lines.last().is_some_and(|l| strip_ws(l).is_empty()) {
            lines.pop();
        }
        lines.push(String::new());
        lines.push("world:".into());
        lines.extend(body);
        return Ok(lines.join("\n") + "\n");
    };
    if !HEAD_BLOCK.is_match(&lines[head]) {
        return Err(value_error(
            "server.yml: world: must be a block mapping (one key per line) for the panel to edit it",
        ));
    }
    let mut end = head + 1;
    let mut last = head;
    while end < lines.len() {
        let ln = &lines[end];
        let blank = strip_ws(ln).is_empty();
        if !(blank || ln.starts_with(' ') || ln.starts_with('\t') || ln.starts_with('#')) {
            break;
        }
        if !blank && !lstrip(ln).starts_with('#') {
            last = end;
        }
        end += 1;
    }
    let block = &lines[head + 1..=last];
    let indent = block
        .iter()
        .find(|ln| !strip_ws(ln).is_empty() && !lstrip(ln).starts_with('#'))
        .map_or(2, |ln| len_cp(ln) - len_cp(lstrip(ln)));
    if indent != 2 {
        return Err(value_error(
            "server.yml: the world: section must be indented by two spaces for the panel to edit it",
        ));
    }
    let mut kept: Vec<String> = Vec::new();
    let mut i = 0;
    while i < block.len() {
        let ln = &block[i];
        let Some(m) = KEY.captures(ln) else {
            kept.push(ln.clone());
            i += 1;
            continue;
        };
        let is_active = &m[1] == "active";
        i += 1;
        while i < block.len() {
            let nxt = &block[i];
            let blank = strip_ws(nxt).is_empty();
            let comment = lstrip(nxt).starts_with('#');
            if !(blank || nxt.starts_with("   ") || nxt.starts_with("  - ") || comment) {
                break;
            }
            if is_active && !blank && !comment {
                return Err(value_error("server.yml: world.active must be one line"));
            }
            i += 1;
        }
    }
    while kept.last().is_some_and(|l| strip_ws(l).is_empty()) {
        kept.pop();
    }
    let mut out: Vec<String> = lines[..=head].to_vec();
    out.extend(kept);
    out.extend(body);
    out.extend_from_slice(&lines[last + 1..]);
    Ok(out.join("\n") + "\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_the_worlds_vectors() {
        let v: Value = serde_json::from_str(include_str!(
            "../../../../tests/agent_parity/vectors/worlds.json"
        ))
        .unwrap();
        let v = &v["vectors"];
        let lib = &v["set_world_section"][0]["library"];
        for (e, want) in py_iter(lib)
            .unwrap()
            .iter()
            .zip(v["library_line"].as_array().unwrap())
        {
            assert_eq!(library_line(e).unwrap(), want.as_str().unwrap());
        }
        for case in v["set_world_section"].as_array().unwrap() {
            let text = case["text"].as_str().unwrap();
            let got = set_world_section(text, &case["active"], &case["library"]);
            match (got, case.get("ok")) {
                (Ok(s), Some(ok)) => assert_eq!(s, ok.as_str().unwrap(), "{text:?}"),
                (Err(e), None) => assert_eq!(e.to_string(), case["error"].as_str().unwrap()),
                (got, _) => panic!("{text:?}: {got:?} vs {case}"),
            }
        }
    }
}
