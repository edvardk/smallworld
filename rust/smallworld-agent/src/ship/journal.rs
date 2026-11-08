//! `message_text`, `journal_entry`, `for_d1`, `for_archive`, `journal_argv`, the streamed batch read with its
//! caps (`LOG_BATCH_MAX_ENTRIES` 5000, `LOG_BATCH_MAX_BYTES` 8 MiB, `LOG_ENTRY_MAX_CHARS` 8000) and the CPU
//! `Slicer` (mc_agent.py 596-760). Owner: ship.

use super::ShipError;
use crate::link::IngestError;
use crate::types::{AResult, AgentError, Obj, Value, truthy};
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

/// Characters for which `str.isdigit()` is true but `str.isdecimal()` isn't (superscripts, circled digits,
/// ...): `int()` refuses them. Python 3.11, 3.12 and 3.13 agree:
/// `[c for c in range(0x110000) if chr(c).isdigit() and not chr(c).isdecimal()]`.
const DIGIT_NOT_DECIMAL: [(u32, u32); 20] = [
    (0xB2, 0xB3),
    (0xB9, 0xB9),
    (0x1369, 0x1371),
    (0x19DA, 0x19DA),
    (0x2070, 0x2070),
    (0x2074, 0x2079),
    (0x2080, 0x2089),
    (0x2460, 0x2468),
    (0x2474, 0x247C),
    (0x2488, 0x2490),
    (0x24EA, 0x24EA),
    (0x24F5, 0x24FD),
    (0x24FF, 0x24FF),
    (0x2776, 0x277E),
    (0x2780, 0x2788),
    (0x278A, 0x2792),
    (0x10A40, 0x10A43),
    (0x10E60, 0x10E68),
    (0x11052, 0x1105A),
    (0x1F100, 0x1F10A),
];

fn digit_not_decimal(ch: char) -> bool {
    let c = u32::from(ch);
    DIGIT_NOT_DECIMAL.iter().any(|&(a, b)| (a..=b).contains(&c))
}

/// `int(prio) if isinstance(prio, str) and prio.isdigit() else 6`. A digit `int()` refuses raises an uncaught
/// `ValueError` in Python (the log round fails); returned as `AgentError::Value`.
fn priority(raw: Option<&Value>) -> AResult<Value> {
    let Some(Value::String(p)) = raw else {
        return Ok(6.into());
    };
    let decimal = |c: char| crate::unicode::decimal_value(c).is_some();
    if p.is_empty() || !p.chars().all(|c| decimal(c) || digit_not_decimal(c)) {
        return Ok(6.into());
    }
    if !p.chars().all(decimal) {
        return Err(AgentError::Value(
            "ValueError".into(),
            format!(
                "invalid literal for int() with base 10: {}",
                pyjson::repr_str(p)
            ),
        ));
    }
    if p.chars().count() > smallworld_core::pystr::INT_MAX_STR_DIGITS {
        return Err(AgentError::Value(
            "ValueError".into(),
            format!(
                "Exceeds the limit (4300 digits) for integer string conversion: value has {} digits; use \
                 sys.set_int_max_str_digits() to increase the limit",
                p.chars().count()
            ),
        ));
    }
    let ascii: String = p
        .chars()
        .filter_map(|c| crate::unicode::decimal_value(c).and_then(|d| char::from_digit(d, 10)))
        .collect();
    Ok(match ascii.parse::<u64>() {
        Ok(n) => n.into(),
        Err(_) => ascii.parse::<f64>().map_or(Value::Null, Value::from),
    })
}

/// `journal_entry(raw)` with Python's one uncaught exception: `Ok(None)` where Python returns None (no usable
/// `__REALTIME_TIMESTAMP`), `Err(Value)` where `int(PRIORITY)` raises.
///
/// # Errors
/// `AgentError::Value("ValueError", ...)` for a PRIORITY of digits `int()` refuses.
pub fn try_journal_entry(raw: &Obj) -> AResult<Option<Entry>> {
    let Some(us) = raw.get("__REALTIME_TIMESTAMP").and_then(py_int_value) else {
        return Ok(None);
    };
    // `// 1000` floors; beyond i64 milliseconds is a documented divergence (Python keeps the big int).
    let Ok(ts) = i64::try_from(us.div_euclid(1000)) else {
        return Ok(None);
    };
    let get = |k: &str| raw.get(k).filter(|v| truthy(v));
    let unit = get("_SYSTEMD_UNIT")
        .or_else(|| get("SYSLOG_IDENTIFIER"))
        .map_or(String::new(), py_str);
    let ident = get("SYSLOG_IDENTIFIER").map_or(String::new(), py_str);
    let prio = priority(raw.get("PRIORITY"))?;
    Ok(Some(Entry {
        ts,
        unit: prefix_cp(&unit, 100).to_string(),
        ident: prefix_cp(&ident, 100).to_string(),
        prio,
        msg: message_text(raw.get("MESSAGE").unwrap_or(&Value::Null)),
        audit: i64::from(raw.get("MC_AUDIT").and_then(Value::as_str) == Some("1")),
        sd_unit: prefix_cp(&get("UNIT").map_or(String::new(), py_str), 100).to_string(),
    }))
}

/// `journal_entry(raw)` as the console tail calls it (`contextlib.suppress(ValueError)`): the kept fields of one
/// `journalctl -o json` line, or None.
pub fn journal_entry(raw: &Obj) -> Option<Obj> {
    try_journal_entry(raw).ok().flatten().map(|e| e.to_obj())
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
/// `IngestError("journalctl failed: ...")` when it can't run, or exits non-zero having given nothing; the
/// `ValueError` of `journal_entry` (uncaught in Python).
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
    let mut failure = None;
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
            match try_journal_entry(&raw) {
                Ok(Some(entry)) => {
                    size += entry.msg.chars().count();
                    entries.push(entry);
                }
                Ok(None) => {}
                Err(e) => {
                    failure = Some(e);
                    break;
                }
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
    if let Some(e) = failure {
        return Err(ShipError::Agent(e));
    }
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
        let e = |raw: Value| entry(raw).unwrap().unwrap();
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
                entry(serde_json::json!({"__REALTIME_TIMESTAMP": bad}))
                    .unwrap()
                    .is_none(),
                "{bad}"
            );
        }
        assert!(
            entry(serde_json::json!({"__REALTIME_TIMESTAMP": null}))
                .unwrap()
                .is_none()
        );
        // PRIORITY: isdigit() then int(): a superscript digit is an uncaught ValueError.
        assert_eq!(
            e(serde_json::json!({"__REALTIME_TIMESTAMP": "0", "PRIORITY": "\u{663}"})).prio,
            3
        );
        assert_eq!(
            e(serde_json::json!({"__REALTIME_TIMESTAMP": "0", "PRIORITY": " 3"})).prio,
            6
        );
        let err = entry(serde_json::json!({"__REALTIME_TIMESTAMP": "0", "PRIORITY": "\u{b2}"}))
            .unwrap_err();
        assert_eq!(
            err.to_string(),
            "invalid literal for int() with base 10: '\u{b2}'"
        );
        assert!(
            journal_entry(
                serde_json::json!({"__REALTIME_TIMESTAMP": "0", "PRIORITY": "\u{b2}"})
                    .as_object()
                    .unwrap()
            )
            .is_none()
        );
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
