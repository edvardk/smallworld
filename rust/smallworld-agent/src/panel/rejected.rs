//! mc_panel.py `RejectedLogins` and `mask_address` (panel:885-980): the last REJECTED_KEEP whitelist rejections,
//! in the agent's state dir (rejected.json, 0600), fed from minecraft.service's journal (`feed`, called by the
//! daemon's `observe_logs` with the log round's entries) and trimmed by the panel (`drop_name`). Owner: data.
//!
//! The patterns are anchored to the server's console prefix (`[HH:MM:SS INFO]: `), so chat can't forge a line.
//! They have no `re.ASCII`: `\d` is any `Nd` digit (Rust's `\d`), `\s` is Python's (`data::PY_SPACE`), `(?i)` is
//! Unicode simple case folding in both.

use super::{NAME, REJECTED_KEEP};
use crate::types::{AResult, AgentError, Obj, Value};
use regex::Regex;
use serde_json::json;
use smallworld_core::pyjson::{Dumps, dumps, string};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{LazyLock, Mutex, PoisonError};

const PREFIX: &str = r"^\[\d{2}:\d{2}:\d{2} INFO\]: ";
static ANSI: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\x1b\[[0-9;]*[A-Za-z]").expect("constant"));
static REJECT_LINE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r"{PREFIX}Disconnecting (?P<name>[A-Za-z0-9_]{{1,16}}) \((?P<addr>[^()]{{0,80}})\): (?P<reason>.*)$"
    ))
    .expect("constant")
});
static LOST_LINE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r"{PREFIX}(?P<name>[A-Za-z0-9_]{{1,16}}) \((?P<addr>[^()]{{0,80}})\) lost connection: (?P<reason>.*)$"
    ))
    .expect("constant")
});
static UUID_LINE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r"{PREFIX}UUID of player (?P<name>[A-Za-z0-9_]{{1,16}}) is (?P<uuid>[0-9a-f-]{{36}})$"
    ))
    .expect("constant")
});
/// `WHITELIST_REASON` = `(?i)\bnot\s+white-?listed\b` without the `\b`s, which `whitelist_reason` checks with
/// Python's word characters (`str.isalnum()` or `_`; Rust's `\w` also counts combining marks).
static WHITELIST_REASON: LazyLock<Regex> = LazyLock::new(|| {
    let s = crate::data::PY_SPACE;
    Regex::new(&format!(r"(?i)not{s}+white-?listed")).expect("constant")
});

/// Python's `\w` for a str pattern: `isalnum()` (letters and numbers by category) or `_`.
fn py_word(c: char) -> bool {
    c == '_'
        || matches!(
            crate::unicode::category(c),
            "Lu" | "Ll" | "Lt" | "Lm" | "Lo" | "Nd" | "Nl" | "No"
        )
}

/// `WHITELIST_REASON.search(reason)`.
pub(crate) fn whitelist_reason(reason: &str) -> bool {
    let mut pos = 0;
    while let Some(m) = WHITELIST_REASON.find_at(reason, pos) {
        let before = reason[..m.start()].chars().next_back();
        let after = reason[m.end()..].chars().next();
        if !before.is_some_and(py_word) && !after.is_some_and(py_word) {
            return true;
        }
        pos = m.start() + reason[m.start()..].chars().next().map_or(1, char::len_utf8);
    }
    false
}
static BRACKETED: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^\[(.*)\](?::\d+)?$").expect("constant"));

/// The in-memory state (`uuids`: name -> uuid from "UUID of player" lines, insertion-ordered, at most 500;
/// `recent`: name -> ts of the last counted rejection).
#[derive(Default)]
struct Memory {
    uuids: Vec<(String, String)>,
    recent: HashMap<String, i64>,
}

pub struct RejectedLogins {
    path: PathBuf,
    lock: Mutex<()>,
    memory: Mutex<Memory>,
}

/// rejected.json's entry keys in the order Python writes them (others follow, sorted).
const KEY_ORDER: [&str; 6] = ["name", "uuid", "count", "first", "last", "ip"];

fn crash(msg: String) -> AgentError {
    AgentError::Crash(msg)
}

/// `e["name"].lower()`.
fn name_lower(e: &Obj) -> AResult<String> {
    match e.get("name") {
        Some(Value::String(s)) => Ok(s.to_lowercase()),
        Some(other) => Err(crash(format!(
            "AttributeError: '{}' object has no attribute 'lower'",
            crate::worlds::py_type(other)
        ))),
        None => Err(crash("KeyError: 'name'".into())),
    }
}

/// Python's `int(v)` for a value from `json.loads`.
fn py_int_of(v: &Value) -> AResult<i64> {
    match v {
        Value::Bool(b) => Ok(i64::from(*b)),
        Value::Number(n) => n.as_i64().map_or_else(
            || {
                #[allow(clippy::cast_possible_truncation)]
                let f = n.as_f64().unwrap_or(0.0).trunc() as i64;
                Ok(f)
            },
            Ok,
        ),
        Value::String(s) => crate::data::health::py_int_str(smallworld_core::pystr::strip_ws(s)),
        other => Err(crash(format!(
            "TypeError: int() argument must be a string, a bytes-like object or a real number, not '{}'",
            crate::worlds::py_type(other)
        ))),
    }
}

/// `json.dumps(keep)` with each entry's keys in Python's insertion order.
fn dump_entries(entries: &[Obj]) -> String {
    let opts = Dumps::python();
    let items: Vec<String> = entries
        .iter()
        .map(|e| {
            let mut keys: Vec<&String> = e.keys().collect();
            keys.sort_by_key(|k| {
                (
                    KEY_ORDER
                        .iter()
                        .position(|o| o == k)
                        .unwrap_or(KEY_ORDER.len()),
                    (*k).clone(),
                )
            });
            let fields: Vec<String> = keys
                .iter()
                .map(|k| format!("{}: {}", string(k, true), dumps(&e[k.as_str()], &opts)))
                .collect();
            format!("{{{}}}", fields.join(", "))
        })
        .collect();
    format!("[{}]", items.join(", "))
}

impl RejectedLogins {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self {
            path: path.into(),
            lock: Mutex::new(()),
            memory: Mutex::new(Memory::default()),
        }
    }

    pub fn path(&self) -> &std::path::Path {
        &self.path
    }

    /// `entries()`: the kept entries whose `str(name)` is a Java name.
    pub fn entries(&self) -> Vec<Obj> {
        let Some(Value::Array(items)) = smallworld_core::pyjson::read_json(&self.path) else {
            return Vec::new();
        };
        items
            .into_iter()
            .filter_map(|e| match e {
                Value::Object(o) => {
                    let name = o.get("name").map_or_else(String::new, crate::types::py_str);
                    NAME.is_match(&name).then_some(o)
                }
                _ => None,
            })
            .collect()
    }

    /// `entries()` (the interface's name).
    ///
    /// # Errors
    /// None (kept for the declared interface).
    #[allow(clippy::unnecessary_wraps)]
    pub fn load(&self) -> AResult<Vec<Obj>> {
        Ok(self.entries())
    }

    fn save(&self, entries: &[Obj]) -> AResult<()> {
        smallworld_core::fsx::atomic_write(&self.path, dump_entries(entries).as_bytes(), 0o600)?;
        Ok(())
    }

    /// `drop(name)`: removes a name (case-insensitive); true if it was there.
    ///
    /// # Errors
    /// `Crash` on a hand-broken entry (Python's KeyError/AttributeError), the write's `OSError`.
    pub fn drop_name(&self, name: &str) -> AResult<bool> {
        let _g = self.lock.lock().unwrap_or_else(PoisonError::into_inner);
        let entries = self.entries();
        let want = name.to_lowercase();
        let mut keep = Vec::new();
        for e in &entries {
            if name_lower(e)? != want {
                keep.push(e.clone());
            }
        }
        if keep.len() != entries.len() {
            self.save(&keep)?;
            return Ok(true);
        }
        Ok(false)
    }

    /// `feed(lines)`: (ts ms, message) lines of minecraft.service; true if the list changed.
    ///
    /// # Errors
    /// As `drop_name`.
    pub fn feed(&self, lines: &[(i64, String)]) -> AResult<bool> {
        Ok(!self.feed_hits(lines)?.is_empty())
    }

    fn feed_hits(&self, lines: &[(i64, String)]) -> AResult<Vec<String>> {
        let mut hits: Vec<(i64, String, String)> = Vec::new();
        let mut mem = self.memory.lock().unwrap_or_else(PoisonError::into_inner);
        for (ts, raw) in lines {
            let cleaned = ANSI.replace_all(raw, "");
            let msg = cleaned.trim_end_matches(['\r', '\n']);
            if let Some(u) = UUID_LINE.captures(msg) {
                let key = u["name"].to_lowercase();
                let uuid = u["uuid"].to_string();
                match mem.uuids.iter_mut().find(|(k, _)| *k == key) {
                    Some(e) => e.1 = uuid,
                    None => mem.uuids.push((key, uuid)),
                }
                if mem.uuids.len() > 500 {
                    mem.uuids.remove(0);
                }
                continue;
            }
            let Some(m) = REJECT_LINE
                .captures(msg)
                .or_else(|| LOST_LINE.captures(msg))
            else {
                continue;
            };
            if !whitelist_reason(&m["reason"]) {
                continue;
            }
            let name = m["name"].to_string();
            let lower = name.to_lowercase();
            let last = mem
                .recent
                .get(&lower)
                .copied()
                .unwrap_or(-1_000_000_000_000);
            if ts - last < 10_000 {
                continue; // its own "lost connection" line
            }
            mem.recent.insert(lower, *ts);
            hits.push((*ts, name, mask_address(&m["addr"])));
        }
        if hits.is_empty() {
            return Ok(Vec::new());
        }
        let _g = self.lock.lock().unwrap_or_else(PoisonError::into_inner);
        // `{e["name"].lower(): e for e in entries}`: a repeated name keeps its first place, its last entry
        let mut by_name: Vec<(String, Obj)> = Vec::new();
        for e in self.entries() {
            let k = name_lower(&e)?;
            match by_name.iter_mut().find(|(n, _)| *n == k) {
                Some(slot) => slot.1 = e,
                None => by_name.push((k, e)),
            }
        }
        for (ts, name, ip) in &hits {
            let k = name.to_lowercase();
            let idx = if let Some(i) = by_name.iter().position(|(n, _)| *n == k) {
                i
            } else {
                let mut e = Obj::new();
                e.insert("name".into(), json!(name));
                e.insert("uuid".into(), Value::Null);
                e.insert("count".into(), json!(0));
                e.insert("first".into(), json!(ts));
                by_name.push((k.clone(), e));
                by_name.len() - 1
            };
            let e = &mut by_name[idx].1;
            let count = py_int_of(e.get("count").unwrap_or(&json!(0)))? + 1;
            e.insert("count".into(), json!(count));
            e.insert("last".into(), json!(ts));
            e.insert("ip".into(), json!(ip));
            e.insert("name".into(), json!(name));
            let known = mem
                .uuids
                .iter()
                .find(|(n, _)| *n == k)
                .map(|(_, u)| u.clone());
            let uuid = match known.filter(|u| !u.is_empty()) {
                Some(u) => json!(u),
                None => e.get("uuid").cloned().unwrap_or(Value::Null),
            };
            e.insert("uuid".into(), uuid);
        }
        let mut keyed = Vec::with_capacity(by_name.len());
        for (_, e) in by_name {
            let last = py_int_of(e.get("last").unwrap_or(&json!(0)))?;
            keyed.push((-last, e));
        }
        keyed.sort_by_key(|(k, _)| *k); // stable, like sorted()
        let keep: Vec<Obj> = keyed
            .into_iter()
            .take(REJECTED_KEEP)
            .map(|(_, e)| e)
            .collect();
        self.save(&keep)?;
        Ok(hits.into_iter().map(|(_, n, _)| n).collect())
    }

    /// `feed` for lines without their own timestamps (all at `now`); returns the names counted. Prefer `feed`
    /// with the journal entries' `ts` (what `observe_logs` passes).
    ///
    /// # Errors
    /// As `feed`.
    pub fn observe(&self, lines: &[String], now: f64) -> AResult<Vec<String>> {
        #[allow(clippy::cast_possible_truncation)]
        let ts = (now * 1000.0).trunc() as i64;
        let lines: Vec<(i64, String)> = lines.iter().map(|l| (ts, l.clone())).collect();
        self.feed_hits(&lines)
    }
}

/// IPv4 as Python's `ipaddress` parses it: four ASCII decimal octets, no leading zeros, at most 255.
fn parse_v4(s: &str) -> Option<[u8; 4]> {
    let parts: Vec<&str> = s.split('.').collect();
    if parts.len() != 4 {
        return None;
    }
    let mut out = [0u8; 4];
    for (i, p) in parts.iter().enumerate() {
        if p.is_empty()
            || !p.bytes().all(|b| b.is_ascii_digit())
            || p.len() > 3
            || (*p != "0" && p.starts_with('0'))
        {
            return None;
        }
        out[i] = u8::try_from(p.parse::<u32>().ok()?).ok()?;
    }
    Some(out)
}

/// IPv6 as Python's `ipaddress._ip_int_from_string` parses it (one `::`, an embedded IPv4 tail).
fn parse_v6(s: &str) -> Option<[u16; 8]> {
    if s.is_empty() {
        return None;
    }
    let mut parts: Vec<String> = s.split(':').map(str::to_string).collect();
    if parts.len() < 3 {
        return None;
    }
    if parts.last().is_some_and(|p| p.contains('.')) {
        let v4 = parse_v4(&parts.pop()?)?;
        parts.push(format!("{:x}", u16::from_be_bytes([v4[0], v4[1]])));
        parts.push(format!("{:x}", u16::from_be_bytes([v4[2], v4[3]])));
    }
    if parts.len() > 9 {
        return None;
    }
    let mut skip = None;
    for (i, p) in parts.iter().enumerate().take(parts.len() - 1).skip(1) {
        if p.is_empty() {
            if skip.is_some() {
                return None;
            }
            skip = Some(i);
        }
    }
    let (hi, lo, skipped) = if let Some(si) = skip {
        let mut hi = si;
        let mut lo = parts.len() - si - 1;
        if parts[0].is_empty() {
            if hi != 1 {
                return None;
            }
            hi -= 1;
        }
        if parts[parts.len() - 1].is_empty() {
            if lo != 1 {
                return None;
            }
            lo -= 1;
        }
        let used = hi + lo;
        if used >= 8 {
            return None;
        }
        (hi, lo, 8 - used)
    } else {
        if parts.len() != 8 || parts[0].is_empty() || parts[7].is_empty() {
            return None;
        }
        (8, 0, 0)
    };
    let hextet = |p: &str| -> Option<u16> {
        if p.is_empty() || p.len() > 4 || !p.bytes().all(|b| b.is_ascii_hexdigit()) {
            return None;
        }
        u16::from_str_radix(p, 16).ok()
    };
    let mut out = [0u16; 8];
    for i in 0..hi {
        out[i] = hextet(&parts[i])?;
    }
    for i in 0..lo {
        out[hi + skipped + i] = hextet(&parts[parts.len() - lo + i])?;
    }
    Some(out)
}

/// Python's compressed IPv6 text (`_compress_hextets`: the longest run of two or more zero groups, the first on
/// a tie, becomes `::`).
fn v6_text(g: [u16; 8]) -> String {
    let (mut best, mut best_len) = (None, 0);
    let mut i = 0;
    while i < 8 {
        if g[i] == 0 {
            let start = i;
            while i < 8 && g[i] == 0 {
                i += 1;
            }
            if i - start > best_len {
                best = Some(start);
                best_len = i - start;
            }
        } else {
            i += 1;
        }
    }
    let hex: Vec<String> = g.iter().map(|x| format!("{x:x}")).collect();
    match best {
        Some(start) if best_len > 1 => {
            let head = hex[..start].join(":");
            let tail = hex[start + best_len..].join(":");
            format!("{head}::{tail}")
        }
        _ => hex.join(":"),
    }
}

/// mc_panel.py `mask_address(addr)`: "/203.0.113.7:51234" -> "203.0.113.x"; IPv6 -> its /48; else "".
pub fn mask_address(addr: &str) -> String {
    let a = smallworld_core::pystr::strip_ws(addr).trim_start_matches('/');
    let a = BRACKETED.replace(a, "$1").into_owned();
    let a = if a.matches(':').count() == 1 {
        a.split(':').next().unwrap_or("").to_string()
    } else {
        a
    };
    let a = a.split('%').next().unwrap_or("");
    if a.contains('/') {
        return String::new();
    }
    if let Some([a1, a2, a3, _]) = parse_v4(a) {
        return format!("{a1}.{a2}.{a3}.x");
    }
    if let Some(mut g) = parse_v6(a) {
        for x in g.iter_mut().skip(3) {
            *x = 0;
        }
        return format!("{}/48", v6_text(g));
    }
    String::new()
}

#[cfg(test)]
#[allow(clippy::items_after_statements)]
mod tests {
    use super::*;

    fn vectors() -> Value {
        serde_json::from_str::<Value>(include_str!(
            "../../../../tests/agent_parity/vectors/rejected.json"
        ))
        .unwrap()["vectors"]
            .clone()
    }

    #[test]
    fn mask_address_vectors() {
        for c in vectors()["mask_address"].as_array().unwrap() {
            assert_eq!(
                mask_address(c["input"].as_str().unwrap()),
                c["output"].as_str().unwrap(),
                "{c}"
            );
        }
    }

    #[test]
    fn mask_address_edges() {
        for (input, want) in [
            ("2001:db8:1:2:3:4:5::", "2001:db8:1::/48"),
            ("::", "::/48"),
            (":::", ""),
            ("2001:db8::2::3", ""),
            ("2001:db8:1:2:3:4:5:6:7", ""),
            ("2001:db8::192.0.2.1", "2001:db8::/48"),
            ("0192.0.2.1", ""),
            ("192.0.2.1/24", ""),
            ("[192.0.2.1]:80", "192.0.2.x"),
            ("12345::", ""),
            ("  /198.51.100.20:1  ", "198.51.100.x"),
        ] {
            assert_eq!(mask_address(input), want, "{input}");
        }
    }

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("sw-rej-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d.join("rejected.json")
    }

    #[test]
    fn feed_vector() {
        let v = vectors();
        let c = &v["feed"];
        let lines: Vec<(i64, String)> = c["lines"]
            .as_array()
            .unwrap()
            .iter()
            .map(|l| (l[0].as_i64().unwrap(), l[1].as_str().unwrap().to_string()))
            .collect();
        let path = tmp("feed");
        let r = RejectedLogins::new(&path);
        assert_eq!(r.feed(&lines).unwrap(), c["changed"].as_bool().unwrap());
        let got: Vec<Value> = r.entries().into_iter().map(Value::Object).collect();
        assert_eq!(Value::Array(got), c["entries"]);
        let text = std::fs::read_to_string(&path).unwrap();
        assert!(text.starts_with(r#"[{"name": "Arab", "uuid": null, "count": 1, "first": 20000, "last": 20000, "ip": "192.0.2.x"}, "#), "{text}");
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        for (k, want) in v.as_object().unwrap() {
            if k == "feed" || k == "mask_address" {
                continue;
            }
            eprintln!("rejected vector {k} not checked here: {want}");
        }
        assert!(!r.feed(&[(1, "nothing".into())]).unwrap());
        assert!(r.drop_name("MALLORY").unwrap());
        assert!(!r.drop_name("mallory").unwrap());
        assert_eq!(r.entries().len(), 3);
    }

    #[test]
    fn keeps_fifty_newest_and_broken_entries_crash_like_python() {
        let path = tmp("keep");
        let r = RejectedLogins::new(&path);
        let lines: Vec<(i64, String)> = (0..60)
            .map(|k| {
                (
                    i64::from(k) * 20_000,
                    format!("[01:02:03 INFO]: Disconnecting P{k} (/192.0.2.1:1): not whitelisted"),
                )
            })
            .collect();
        assert!(r.feed(&lines).unwrap());
        let e = r.entries();
        assert_eq!(e.len(), REJECTED_KEEP);
        assert_eq!(e[0]["name"], json!("P59"));
        std::fs::write(&path, r#"[{"name": null, "count": 1}]"#).unwrap();
        // str(None) == "None" passes the name check, then e["name"] raises
        assert_eq!(r.entries().len(), 1);
        assert_eq!(
            r.drop_name("x"),
            Err(AgentError::Crash(
                "AttributeError: 'NoneType' object has no attribute 'lower'".into()
            ))
        );
    }
}
