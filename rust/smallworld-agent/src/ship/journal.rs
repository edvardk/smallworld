//! `message_text`, `journal_entry`, `for_d1`, `for_archive`, `journal_argv`, the streamed batch read with its
//! caps (`LOG_BATCH_MAX_ENTRIES` 5000, `LOG_BATCH_MAX_BYTES` 8 MiB, `LOG_ENTRY_MAX_CHARS` 8000) and the CPU
//! `Slicer` (mc_agent.py 596-760). Owner: ship.

use super::ShipError;
use crate::link::IngestError;
use crate::types::{Obj, Value, truthy};
use regex::Regex;
use smallworld_core::pyjson::{self, py_str};
use smallworld_core::pystr::{isspace, prefix_cp};
use std::io::{BufRead, BufReader};
use std::process::{Command, Stdio};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

pub const LOG_ROW_MAX_CHARS: usize = 2000;
pub const LOG_BATCH_MAX_ENTRIES: usize = 5000;
pub const LOG_BATCH_MAX_BYTES: usize = 8 << 20;
pub const LOG_ENTRY_MAX_CHARS: usize = 8000;
pub const LOG_CATCHUP_BATCHES: usize = 20;
pub const JOURNAL_FIELDS: &str = "_SYSTEMD_UNIT,SYSLOG_IDENTIFIER,PRIORITY,MESSAGE,MC_AUDIT,UNIT";
/// mc_agent.py `FIREWALL_PREFIXES` (the firewall role's log prefixes).
pub const FIREWALL_PREFIXES: [&str; 3] = ["mc-setfull:", "mc-ratelimit:", "mc-connlimit:"];
/// mc_agent.py `WORK_SLICE_S`, `WORK_PAUSE_S`.
pub const WORK_SLICE_S: f64 = 0.02;
pub const WORK_PAUSE_S: f64 = 0.005;
/// mc_agent.py `UNIT`.
pub const UNIT: &str = "minecraft.service";

/// `message_text(value)`: journald's MESSAGE: a string (its first 8000 code points), or a byte array for
/// non-UTF-8 text (its first 8000 bytes, decoded with replacement; an array that `bytes()` refuses is "").
pub fn message_text(value: &Value) -> String {
    match value {
        Value::Array(items) => {
            let mut raw = Vec::new();
            for item in items.iter().take(LOG_ENTRY_MAX_CHARS) {
                // bytes([...]): ints 0..=255 (bools are ints); anything else is a TypeError/ValueError -> "".
                let byte = match item {
                    Value::Bool(b) => Some(u8::from(*b)),
                    Value::Number(n) => n.as_u64().and_then(|n| u8::try_from(n).ok()),
                    _ => None,
                };
                match byte {
                    Some(b) => raw.push(b),
                    None => return String::new(),
                }
            }
            String::from_utf8_lossy(&raw).into_owned()
        }
        Value::String(s) => prefix_cp(s, LOG_ENTRY_MAX_CHARS).to_string(),
        _ => String::new(),
    }
}

/// The fields kept from one journal line (`journal_entry`'s dict, in its key order).
#[derive(Clone, Debug, PartialEq)]
pub struct Entry {
    pub ts: i64,
    pub unit: String,
    pub ident: String,
    /// `int(PRIORITY)` (beyond 64 bits a float; see PORTING.md "Divergences") or 6.
    pub prio: Value,
    pub msg: String,
    pub audit: i64,
    pub sd_unit: String,
}

impl Entry {
    /// The entry as the JSON object Python builds.
    pub fn to_obj(&self) -> Obj {
        let mut o = Obj::new();
        o.insert("ts".into(), self.ts.into());
        o.insert("unit".into(), self.unit.clone().into());
        o.insert("ident".into(), self.ident.clone().into());
        o.insert("prio".into(), self.prio.clone());
        o.insert("msg".into(), self.msg.clone().into());
        o.insert("audit".into(), self.audit.into());
        o.insert("sd_unit".into(), self.sd_unit.clone().into());
        o
    }

    /// `json.dumps(e, separators=(",", ":"))`: insertion order, `ensure_ascii`.
    pub fn compact_json(&self) -> String {
        let s = |v: &str| pyjson::string(v, true);
        format!(
            "{{\"ts\":{},\"unit\":{},\"ident\":{},\"prio\":{},\"msg\":{},\"audit\":{},\"sd_unit\":{}}}",
            self.ts,
            s(&self.unit),
            s(&self.ident),
            pyjson::dumps_compact(&self.prio),
            s(&self.msg),
            self.audit,
            s(&self.sd_unit)
        )
    }

    /// `for_d1(entry)`.
    pub fn for_d1(&self) -> bool {
        unit_or_ident_for_d1(&self.unit, &self.ident)
    }

    /// `for_archive(entry)`.
    pub fn for_archive(&self) -> bool {
        self.for_d1()
            || (self.ident == "kernel" && FIREWALL_PREFIXES.iter().any(|p| self.msg.starts_with(p)))
    }
}

/// `int(text)` for a str: surrounding whitespace, an optional sign, decimal digits (any `Nd`, as Python) with
/// single underscores between them, at most 4300 digits. `None` is Python's `ValueError`; a value beyond
/// `i128` too (none fits a journal timestamp).
fn py_int_str(text: &str) -> Option<i128> {
    let t = text.trim_matches(isspace);
    let (neg, body) = match t.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, t.strip_prefix('+').unwrap_or(t)),
    };
    let (mut n, mut digits, mut prev_us) = (0_i128, 0_usize, true);
    for ch in body.chars() {
        if ch == '_' {
            if prev_us {
                return None;
            }
            prev_us = true;
            continue;
        }
        let d = crate::unicode::decimal_value(ch)?;
        n = n.checked_mul(10)?.checked_add(i128::from(d))?;
        digits += 1;
        prev_us = false;
    }
    if digits == 0 || prev_us || digits > smallworld_core::pystr::INT_MAX_STR_DIGITS {
        return None;
    }
    Some(if neg { -n } else { n })
}

/// `int(value)` for what `json.loads` gives, as `journal_entry` and `LogState.load` call it: `None` for the
/// `TypeError`/`ValueError` they catch.
pub(super) fn py_int_value(value: &Value) -> Option<i128> {
    match value {
        Value::Bool(b) => Some(i128::from(*b)),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Some(i128::from(i))
            } else if let Some(u) = n.as_u64() {
                Some(i128::from(u))
            } else {
                let f = n.as_f64()?.trunc();
                // int(float): exact for any finite float; beyond i128 (|x| >= 2**127) only Python has it.
                #[allow(clippy::cast_possible_truncation)]
                (f.abs() < 1.7e38).then_some(f as i128)
            }
        }
        Value::String(s) => py_int_str(s),
        _ => None,
    }
}

/// `str(value)` for a journal field of any JSON type, as `journal_entry` prints it (`str(unit)[:100]`): a str
/// as is, `None`/`True`/`False`, numbers as Python prints them, lists as `repr`s. Unlike
/// `pyjson::py_str` (core has no Unicode tables), a str inside a list is escaped where Python's
/// `str.isprintable()` says so (categories C*, Z* except the space), from the agent's Unicode tables. JSON
/// objects print with sorted keys (serde_json's order; journalctl never emits one).
pub fn py_str_field(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        other => py_repr_field(other),
    }
}

fn py_repr_field(value: &Value) -> String {
    match value {
        Value::Null | Value::Bool(_) | Value::Number(_) => py_str(value),
        Value::String(s) => repr_str(s),
        Value::Array(items) => {
            let parts: Vec<String> = items.iter().map(py_repr_field).collect();
            format!("[{}]", parts.join(", "))
        }
        Value::Object(map) => {
            let parts: Vec<String> = map
                .iter()
                .map(|(k, v)| format!("{}: {}", repr_str(k), py_repr_field(v)))
                .collect();
            format!("{{{}}}", parts.join(", "))
        }
    }
}

/// `str.isprintable()` for one character.
fn isprintable(ch: char) -> bool {
    ch == ' '
        || !matches!(
            crate::unicode::category(ch),
            "Cc" | "Cf" | "Cs" | "Co" | "Cn" | "Zl" | "Zp" | "Zs"
        )
}

/// `repr(s)`: single quotes unless `s` has a `'` and no `"`; `\\t \\n \\r`, `\\xNN`, `\\uNNNN`, `\\UNNNNNNNN`
/// for what isn't printable.
fn repr_str(s: &str) -> String {
    use std::fmt::Write as _;
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::new();
    out.push(quote);
    for c in s.chars() {
        let cp = u32::from(c);
        match c {
            c if c == quote || c == '\\' => {
                out.push('\\');
                out.push(c);
            }
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if isprintable(c) => out.push(c),
            _ if cp <= 0xFF => {
                let _ = write!(out, "\\x{cp:02x}");
            }
            _ if cp <= 0xFFFF => {
                let _ = write!(out, "\\u{cp:04x}");
            }
            _ => {
                let _ = write!(out, "\\U{cp:08x}");
            }
        }
    }
    out.push(quote);
    out
}

/// `journal_priority(prio)`: `int(prio)` when `prio` is a string of digits `int()` takes, else 6. v1.4 security
/// fix (both implementations): digits `int()` refuses (a superscript, which `isdigit()` accepts; more than
/// 4300 digits) are the default too, instead of an uncaught `ValueError` that stalled log shipping at that entry.
fn priority(raw: Option<&Value>) -> Value {
    let Some(Value::String(p)) = raw else {
        return 6.into();
    };
    let decimal = |c: char| crate::unicode::decimal_value(c).is_some();
    if p.is_empty()
        || !p.chars().all(decimal)
        || p.chars().count() > smallworld_core::pystr::INT_MAX_STR_DIGITS
    {
        return 6.into();
    }
    let ascii: String = p
        .chars()
        .filter_map(|c| crate::unicode::decimal_value(c).and_then(|d| char::from_digit(d, 10)))
        .collect();
    match ascii.parse::<u64>() {
        Ok(n) => n.into(),
        Err(_) => ascii.parse::<f64>().map_or(Value::Null, Value::from),
    }
}

/// `journal_entry(raw)` as an `Entry`: None where Python returns None (no usable `__REALTIME_TIMESTAMP`). Since the
/// v1.4 PRIORITY fix nothing in it raises (it used to: a `ValueError` that stalled shipping).
pub fn try_journal_entry(raw: &Obj) -> Option<Entry> {
    let us = raw.get("__REALTIME_TIMESTAMP").and_then(py_int_value)?;
    // `// 1000` floors; beyond i64 milliseconds is a documented divergence (Python keeps the big int).
    let ts = i64::try_from(us.div_euclid(1000)).ok()?;
    let get = |k: &str| raw.get(k).filter(|v| truthy(v));
    let unit = get("_SYSTEMD_UNIT")
        .or_else(|| get("SYSLOG_IDENTIFIER"))
        .map_or(String::new(), py_str_field);
    let ident = get("SYSLOG_IDENTIFIER").map_or(String::new(), py_str_field);
    let prio = priority(raw.get("PRIORITY"));
    Some(Entry {
        ts,
        unit: prefix_cp(&unit, 100).to_string(),
        ident: prefix_cp(&ident, 100).to_string(),
        prio,
        msg: message_text(raw.get("MESSAGE").unwrap_or(&Value::Null)),
        audit: i64::from(raw.get("MC_AUDIT").and_then(Value::as_str) == Some("1")),
        sd_unit: prefix_cp(&get("UNIT").map_or(String::new(), py_str_field), 100).to_string(),
    })
}

/// `journal_entry(raw)` as the console tail calls it (`contextlib.suppress(ValueError)`): the kept fields of one
/// `journalctl -o json` line, or None.
pub fn journal_entry(raw: &Obj) -> Option<Obj> {
    try_journal_entry(raw).map(|e| e.to_obj())
}

fn d1_patterns() -> &'static (Regex, Regex) {
    static RE: OnceLock<(Regex, Regex)> = OnceLock::new();
    // Python's `$` (no MULTILINE) also matches before a final "\n": `\n?\z`.
    RE.get_or_init(|| {
        (
            Regex::new(r"^(?:minecraft\.service|mc-[a-z0-9@._-]+\.service)\n?\z")
                .expect("D1_UNITS"),
            Regex::new(r"^mc-[a-z0-9-]+\n?\z").expect("D1_IDENTIFIERS"),
        )
    })
}

fn unit_or_ident_for_d1(unit: &str, ident: &str) -> bool {
    let (units, idents) = d1_patterns();
    units.is_match(unit) || idents.is_match(ident)
}

fn obj_str<'a>(entry: &'a Obj, key: &str) -> &'a str {
    entry.get(key).and_then(Value::as_str).unwrap_or("")
}

/// `for_d1(entry)`: `D1_UNITS.match(unit) or D1_IDENTIFIERS.match(ident)`.
pub fn for_d1(entry: &Obj) -> bool {
    unit_or_ident_for_d1(obj_str(entry, "unit"), obj_str(entry, "ident"))
}

/// `for_archive(entry)`: the product's own units, mc-agent's audit entries and the firewall's drop lines.
pub fn for_archive(entry: &Obj) -> bool {
    for_d1(entry)
        || (obj_str(entry, "ident") == "kernel"
            && FIREWALL_PREFIXES
                .iter()
                .any(|p| obj_str(entry, "msg").starts_with(p)))
}

/// `journal_argv(cursor)`: after the cursor, or the last 5 minutes on the first run (a falsy cursor).
pub fn journal_argv(cursor: &Value) -> Vec<String> {
    let mut argv: Vec<String> = [
        "journalctl",
        "-o",
        "json",
        "--no-pager",
        &format!("--output-fields={JOURNAL_FIELDS}"),
    ]
    .iter()
    .map(|s| (*s).to_string())
    .collect();
    argv.push(if truthy(cursor) {
        format!("--after-cursor={}", py_str(cursor))
    } else {
        "--since=-5min".into()
    });
    argv
}

/// mc_agent.py `Slicer`: call it once per item; after `slice` of wall time it sleeps `pause`, giving the CPU
/// to the process's other threads (the metrics thread is the Worker's "host alive" signal).
pub struct Slicer {
    slice: Duration,
    pause: Duration,
    start: Instant,
    pub pauses: u64,
}

impl Default for Slicer {
    fn default() -> Self {
        Self::new(WORK_SLICE_S, WORK_PAUSE_S)
    }
}

impl Slicer {
    pub fn new(slice_s: f64, pause_s: f64) -> Self {
        Self {
            slice: Duration::from_secs_f64(slice_s),
            pause: Duration::from_secs_f64(pause_s),
            start: Instant::now(),
            pauses: 0,
        }
    }

    /// `slicer()`.
    pub fn cede(&mut self) {
        if self.start.elapsed() >= self.slice {
            std::thread::sleep(self.pause);
            self.pauses += 1;
            self.start = Instant::now();
        }
    }
}

/// What `read_journal` returns: the entries, the cursor of the last line read, and whether the batch hit a cap.
pub struct Batch {
    pub entries: Vec<Entry>,
    pub cursor: Value,
    pub full: bool,
}

/// One line of journalctl's output as `json.loads(bytes)` reads it: UTF-8 (a BOM dropped), a JSON object.
fn parse_line(line: &[u8]) -> Option<Obj> {
    let line = line.strip_prefix(b"\xef\xbb\xbf").unwrap_or(line);
    match pyjson::loads(std::str::from_utf8(line).ok()?)? {
        Value::Object(o) => Some(o),
        _ => None,
    }
}

/// `read_journal(cursor, limit, max_bytes, argv, cede)`: up to `limit` entries after `cursor` (or the last 5
/// minutes), streamed line by line from journalctl and cut at the caps, so a backlog never sits in memory.
///
/// # Errors
/// `IngestError("journalctl failed: ...")` when it can't run, or exits non-zero having given nothing.
pub fn read_journal(
    cursor: &Value,
    limit: usize,
    max_bytes: usize,
    argv: Option<&[String]>,
    cede: &mut dyn FnMut(),
) -> Result<Batch, ShipError> {
    let cmd = argv.map_or_else(|| journal_argv(cursor), <[String]>::to_vec);
    let mut child = Command::new(&cmd[0])
        .args(&cmd[1..])
        .stdin(Stdio::inherit())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| {
            ShipError::Ingest(IngestError {
                message: format!(
                    "journalctl failed: {}",
                    smallworld_core::errno::io_class(&e)
                ),
                status: None,
            })
        })?;
    let mut entries = Vec::new();
    let (mut last, mut size, mut full) = (cursor.clone(), 0_usize, false);
    if let Some(stdout) = child.stdout.take() {
        let mut reader = BufReader::new(stdout);
        let mut line = Vec::new();
        loop {
            line.clear();
            match reader.read_until(b'\n', &mut line) {
                Ok(0) | Err(_) => break,
                Ok(_) => {}
            }
            cede();
            let Some(raw) = parse_line(&line) else {
                continue;
            };
            if let Some(c) = raw.get("__CURSOR") {
                last = c.clone();
            }
            if let Some(entry) = try_journal_entry(&raw) {
                size += entry.msg.chars().count();
                entries.push(entry);
            }
            if entries.len() >= limit || size >= max_bytes {
                full = true;
                break;
            }
        }
    }
    // finally: kill it if it still runs, close the pipe (dropped above), reap it.
    if !matches!(child.try_wait(), Ok(Some(_))) {
        let _ = child.kill();
    }
    let status = child.wait();
    let code = status.ok().map(|s| {
        use std::os::unix::process::ExitStatusExt;
        s.code().unwrap_or_else(|| -s.signal().unwrap_or(0))
    });
    if let Some(rc) = code
        && !full
        && rc != 0
        && entries.is_empty()
    {
        return Err(ShipError::Ingest(IngestError {
            message: format!("journalctl failed: exit {rc}"),
            status: None,
        }));
    }
    Ok(Batch {
        entries,
        cursor: last,
        full,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fmt::Write as _;

    fn parsers() -> Value {
        serde_json::from_str(include_str!(
            "../../../../tests/agent_parity/vectors/parsers.json"
        ))
        .unwrap()
    }

    #[test]
    fn journal_entry_like_python() {
        let v = parsers();
        let cases = v["vectors"]["journal_entry"].as_array().unwrap();
        assert!(cases.len() >= 8);
        for case in cases {
            let raw = case["input"].as_object().unwrap();
            let got = journal_entry(raw).map_or(Value::Null, Value::Object);
            assert_eq!(got, case["output"], "input {raw:?}");
        }
    }

    #[test]
    fn for_d1_and_for_archive_like_python() {
        let v = parsers();
        for case in v["vectors"]["for_d1"].as_array().unwrap() {
            let e = case["entry"].as_object().unwrap();
            assert_eq!(Value::Bool(for_d1(e)), case["d1"], "{e:?}");
            assert_eq!(Value::Bool(for_archive(e)), case["archive"], "{e:?}");
        }
    }

    #[test]
    fn journal_argv_like_python() {
        let v = parsers();
        let want = &v["vectors"]["journal_argv"];
        assert_eq!(Value::from(journal_argv(&Value::Null)), want[0]);
        assert_eq!(Value::from(journal_argv(&"s=1;i=2".into())), want[1]);
    }

    #[test]
    fn python_edge_cases() {
        let entry = |raw: Value| try_journal_entry(raw.as_object().unwrap());
        let e = |raw: Value| entry(raw).unwrap();
        // int() of a str: whitespace, underscores, a sign, non-ASCII decimals; floors.
        assert_eq!(
            e(serde_json::json!({"__REALTIME_TIMESTAMP": " 1_234_567 "})).ts,
            1234
        );
        assert_eq!(e(serde_json::json!({"__REALTIME_TIMESTAMP": "-1"})).ts, -1);
        assert_eq!(
            e(serde_json::json!({"__REALTIME_TIMESTAMP": "\u{661}\u{662}\u{663}\u{664}"})).ts,
            1
        );
        assert_eq!(e(serde_json::json!({"__REALTIME_TIMESTAMP": 1999.9})).ts, 1);
        assert_eq!(e(serde_json::json!({"__REALTIME_TIMESTAMP": true})).ts, 0);
        for bad in ["1__0", "_1", "1_", "", "+", "0x10", "1.5"] {
            assert!(
                entry(serde_json::json!({"__REALTIME_TIMESTAMP": bad})).is_none(),
                "{bad}"
            );
        }
        assert!(entry(serde_json::json!({"__REALTIME_TIMESTAMP": null})).is_none());
        // PRIORITY: digits int() takes; anything else is 6, including digits int() refuses (a superscript, more
        // than 4300 digits), which used to be an uncaught ValueError that stalled shipping (v1.4 fix).
        let prio =
            |p: &str| e(serde_json::json!({"__REALTIME_TIMESTAMP": "0", "PRIORITY": p})).prio;
        assert_eq!(prio("\u{663}"), 3);
        assert_eq!(prio("4"), 4);
        for bad in [" 3", "\u{b2}", "\u{2460}", "", "x", &"1".repeat(5000)] {
            assert_eq!(prio(bad), 6, "{bad:.8}");
        }
        // message_text: byte arrays (bools are ints), a bad element empties it, 8000 elements at most.
        assert_eq!(
            message_text(&serde_json::json!([104, true, 255])),
            "h\u{1}\u{fffd}"
        );
        assert_eq!(message_text(&serde_json::json!([104, 256])), "");
        assert_eq!(message_text(&serde_json::json!([104, 1.0])), "");
        assert_eq!(message_text(&serde_json::json!(["a"])), "");
        let long: Vec<u8> = vec![b'a'; 9000];
        assert_eq!(message_text(&Value::from(long)).len(), 8000);
        // str(): non-string fields as Python prints them; falsy values skipped.
        let x = e(
            serde_json::json!({"__REALTIME_TIMESTAMP": "0", "_SYSTEMD_UNIT": "", "SYSLOG_IDENTIFIER": 5, "UNIT": [1, "a"]}),
        );
        assert_eq!(
            (x.unit.as_str(), x.ident.as_str(), x.sd_unit.as_str()),
            ("5", "5", "[1, 'a']")
        );
        // python3 -c 'print(str(["\x85​\U000e0001 é\U0001f600　\\", "x'"'"'y", 1.5, None]))'
        assert_eq!(
            py_str_field(&serde_json::json!([
                "\u{85}\u{200b}\u{e0001} \u{e9}\u{1f600}\u{3000}\\",
                "x'y",
                1.5,
                null
            ])),
            "['\\x85\\u200b\\U000e0001 \u{e9}\u{1f600}\\u3000\\\\', \"x'y\", 1.5, None]"
        );
        // Python's `$` matches before a final newline.
        let mut o = Obj::new();
        o.insert("unit".into(), "minecraft.service\n".into());
        assert!(for_d1(&o));
        o.insert("unit".into(), "minecraft.service\n\n".into());
        assert!(!for_d1(&o));
    }

    #[test]
    fn compact_json_is_pythons_bytes() {
        let e = Entry {
            ts: 5,
            unit: "u".into(),
            ident: "i".into(),
            prio: 6.into(),
            msg: "\u{e9}\"\n\u{1f600}".into(),
            audit: 0,
            sd_unit: String::new(),
        };
        // python3 -c 'import json; print(json.dumps({"ts":5,"unit":"u","ident":"i","prio":6,"msg":"é\"\n😀","audit":0,"sd_unit":""}, separators=(",", ":")))'
        assert_eq!(
            e.compact_json(),
            r#"{"ts":5,"unit":"u","ident":"i","prio":6,"msg":"\u00e9\"\n\ud83d\ude00","audit":0,"sd_unit":""}"#
        );
    }

    #[test]
    fn read_journal_streams_and_caps() {
        let dir = std::env::temp_dir().join(format!("ship-read-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("j.jsonl");
        let mut text = String::new();
        for i in 0..10 {
            let _ = writeln!(
                text,
                "{{\"__CURSOR\":\"c{i}\",\"__REALTIME_TIMESTAMP\":\"{i}000\",\"MESSAGE\":\"m{i}\"}}"
            );
        }
        text.push_str("not json\n[1]\n{\"__CURSOR\":\"last\",\"__REALTIME_TIMESTAMP\":\"x\"}");
        std::fs::write(&file, text).unwrap();
        let argv = vec!["cat".to_string(), file.display().to_string()];
        let mut n = 0;
        let b = read_journal(&Value::Null, 4, 1 << 20, Some(&argv), &mut || n += 1).unwrap();
        assert_eq!(
            (b.entries.len(), b.cursor.as_str(), b.full),
            (4, Some("c3"), true)
        );
        let b = read_journal(&Value::Null, 100, 1 << 20, Some(&argv), &mut || n += 1).unwrap();
        assert_eq!(
            (b.entries.len(), b.cursor.as_str(), b.full),
            (10, Some("last"), false)
        );
        let b = read_journal(&Value::Null, 100, 5, Some(&argv), &mut || ()).unwrap();
        assert_eq!((b.entries.len(), b.full), (3, true));
        assert!(n >= 17);
        let fail = vec!["false".to_string()];
        match read_journal(&Value::Null, 1, 1, Some(&fail), &mut || ()) {
            Err(ShipError::Ingest(e)) => assert_eq!(e.message, "journalctl failed: exit 1"),
            _ => panic!("expected an IngestError"),
        }
        let missing = vec!["/nonexistent/journalctl".to_string()];
        match read_journal(&Value::Null, 1, 1, Some(&missing), &mut || ()) {
            Err(ShipError::Ingest(e)) => {
                assert_eq!(e.message, "journalctl failed: FileNotFoundError");
            }
            _ => panic!("expected an IngestError"),
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn the_slicer_pauses_after_a_slice() {
        let mut s = Slicer::new(0.0, 0.0);
        s.cede();
        s.cede();
        assert_eq!(s.pauses, 2);
        let mut s = Slicer::new(60.0, 0.0);
        s.cede();
        assert_eq!(s.pauses, 0);
    }
}
