//! The logs state (`$STATE/logs.json`: `{cursor, day, rows}`), the gzip NDJSON archive (`flate2`, rust backend)
//! and D1 rows with the per-day cap (`select_rows`) (mc_agent.py 761-829). Owner: ship.

use super::journal::{Entry, LOG_ROW_MAX_CHARS, py_int_value};
use crate::types::{AResult, AgentError, Obj, Value};
use flate2::{Compression, GzBuilder};
use smallworld_core::pyjson::{self, Dumps, py_str};
use smallworld_core::pystr::prefix_cp;
use std::io::Write;
use std::path::Path;

/// mc_agent.py `LogState`: the journal cursor (any JSON value; null is None) and the D1 rows delivered on
/// `day` (UTC YYYYMMDD).
#[derive(Clone, Debug, PartialEq)]
pub struct LogState {
    pub cursor: Value,
    pub day: String,
    pub rows: i64,
}

impl Default for LogState {
    fn default() -> Self {
        Self {
            cursor: Value::Null,
            day: String::new(),
            rows: 0,
        }
    }
}

impl LogState {
    /// `LogState.load(path)`: the defaults on any `OSError`, `ValueError`, `TypeError`, `AttributeError`
    /// (unreadable, not JSON, not an object, `rows` not an int).
    pub fn load(path: &Path) -> LogState {
        let Some(Value::Object(data)) = smallworld_core::fsx::read_text(path)
            .as_deref()
            .and_then(pyjson::loads)
        else {
            return LogState::default();
        };
        let rows = match data.get("rows") {
            None => 0,
            Some(v) => match py_int_value(v) {
                // A Python int beyond 64 bits saturates here (it would never fit under any cap anyway).
                Some(n) => i64::try_from(n).unwrap_or(if n < 0 { i64::MIN } else { i64::MAX }),
                None => return LogState::default(),
            },
        };
        LogState {
            cursor: data.get("cursor").cloned().unwrap_or(Value::Null),
            day: data.get("day").map_or(String::new(), py_str),
            rows,
        }
    }

    /// `json.dumps({"cursor": ..., "day": ..., "rows": ...})`: Python's default separators, insertion order.
    pub fn to_json(&self) -> String {
        format!(
            "{{\"cursor\": {}, \"day\": {}, \"rows\": {}}}",
            pyjson::dumps(&self.cursor, &Dumps::python()),
            pyjson::string(&self.day, true),
            self.rows
        )
    }

    /// `LogState.save(path)`: `logs.json.tmp` then `os.replace` (mode from the umask: 0600).
    ///
    /// # Errors
    /// The `OSError` (uncaught in Python: the log round fails).
    pub fn save(&self, path: &Path) -> AResult<()> {
        smallworld_core::fsx::replace_write(path, self.to_json().as_bytes(), None)?;
        Ok(())
    }
}

/// `utc_day(ms)`: `time.strftime("%Y%m%d", time.gmtime(ms / 1000))` (the year unpadded, as glibc prints it).
pub fn utc_day(ms: i64) -> String {
    #[allow(clippy::cast_precision_loss)]
    let tm = crate::sys::gmtime(ms as f64 / 1000.0);
    format!("{}{:02}{:02}", tm.year, tm.month, tm.day)
}

/// One D1 row (`{k: e[k] for k in ("ts", "unit", "prio", "audit")}` plus `msg[:2000]`).
fn row(e: &Entry) -> Obj {
    let mut o = Obj::new();
    o.insert("ts".into(), e.ts.into());
    o.insert("unit".into(), e.unit.clone().into());
    o.insert("prio".into(), e.prio.clone());
    o.insert("audit".into(), e.audit.into());
    o.insert(
        "msg".into(),
        prefix_cp(&e.msg, LOG_ROW_MAX_CHARS).to_string().into(),
    );
    o
}

/// `select_rows(entries, st, cap)`: the D1 rows of this batch within today's cap (per UTC day; updates `st`).
pub fn select_rows(entries: &[Entry], st: &mut LogState, cap: i64) -> Vec<Obj> {
    let mut rows = Vec::new();
    for e in entries {
        if !e.for_d1() {
            continue;
        }
        let day = utc_day(e.ts);
        if day != st.day {
            st.day = day;
            st.rows = 0;
        }
        if st.rows >= cap {
            continue;
        }
        st.rows += 1;
        rows.push(row(e));
    }
    rows
}

/// The NDJSON text of the archive: one `json.dumps(e, separators=(",", ":"))` line per entry.
pub fn archive_text(entries: &[Entry]) -> String {
    let mut text = String::new();
    for e in entries {
        text.push_str(&e.compact_json());
        text.push('\n');
    }
    text
}

/// `archive_bytes(entries)`: gzip of the NDJSON lines, with the header Python's `gzip.GzipFile(fileobj=BytesIO,
/// mode="wb")` writes: no file name, MTIME = now, XFL 2 (level 9), OS 255. The deflate stream itself is
/// miniz_oxide's, not zlib's, so the bytes differ from Python's (which also differ run to run by MTIME); the
/// Worker and the parity harness only compare the decompressed text, which is byte-identical.
///
/// # Errors
/// `Crash` if compressing into memory fails (it can't).
pub fn archive_body(entries: &[Entry]) -> AResult<Vec<u8>> {
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    let mtime = crate::sys::now() as u32;
    let mut gz = GzBuilder::new()
        .mtime(mtime)
        .operating_system(255)
        .write(Vec::new(), Compression::best());
    gz.write_all(archive_text(entries).as_bytes())
        .and_then(|()| gz.finish())
        .map_err(|e| AgentError::Crash(format!("OSError: {e}")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ship::journal::try_journal_entry;
    use std::io::Read;

    fn parsers() -> Value {
        serde_json::from_str(include_str!(
            "../../../../tests/agent_parity/vectors/parsers.json"
        ))
        .unwrap()
    }

    fn entry(v: &Value) -> Entry {
        let o = v.as_object().unwrap();
        Entry {
            ts: o["ts"].as_i64().unwrap(),
            unit: o["unit"].as_str().unwrap().into(),
            ident: o["ident"].as_str().unwrap().into(),
            prio: o["prio"].clone(),
            msg: o["msg"].as_str().unwrap().into(),
            audit: o["audit"].as_i64().unwrap(),
            sd_unit: o["sd_unit"].as_str().unwrap().into(),
        }
    }

    #[test]
    fn select_rows_like_python() {
        let v = parsers();
        let sr = &v["vectors"]["select_rows"];
        let entries: Vec<Entry> = sr["entries"]
            .as_array()
            .unwrap()
            .iter()
            .map(entry)
            .collect();
        let mut st = LogState {
            cursor: Value::Null,
            day: sr["state_before"]["day"].as_str().unwrap().into(),
            rows: sr["state_before"]["rows"].as_i64().unwrap(),
        };
        let rows = select_rows(&entries, &mut st, sr["cap"].as_i64().unwrap());
        let rows: Vec<Value> = rows.into_iter().map(Value::Object).collect();
        assert_eq!(Value::from(rows), sr["rows"]);
        assert_eq!(st.day, sr["state_after"]["day"].as_str().unwrap());
        assert_eq!(Value::from(st.rows), sr["state_after"]["rows"]);
    }

    #[test]
    fn utc_day_like_python() {
        let v = parsers();
        for case in v["vectors"]["utc_day"].as_array().unwrap() {
            assert_eq!(
                utc_day(case["ms"].as_i64().unwrap()),
                case["day"].as_str().unwrap()
            );
        }
        // python3 -c 'import time; print(time.strftime("%Y%m%d", time.gmtime(-60000000000)))'
        assert_eq!(utc_day(-60_000_000_000_000), "680903");
    }

    #[test]
    fn the_archive_is_gzipped_ndjson_with_pythons_header() {
        let raw = serde_json::json!({"__REALTIME_TIMESTAMP": "5000", "_SYSTEMD_UNIT": "minecraft.service",
                                     "MESSAGE": "caf\u{e9}"});
        let e = try_journal_entry(raw.as_object().unwrap())
            .unwrap()
            .unwrap();
        let body = archive_body(&[e.clone(), e]).unwrap();
        assert_eq!(&body[..4], b"\x1f\x8b\x08\x00"); // magic, deflate, no FNAME
        assert_eq!((body[8], body[9]), (2, 255)); // XFL (best), OS unknown
        let mut text = String::new();
        flate2::read::GzDecoder::new(&body[..])
            .read_to_string(&mut text)
            .unwrap();
        let line = r#"{"ts":5,"unit":"minecraft.service","ident":"","prio":6,"msg":"caf\u00e9","audit":0,"sd_unit":""}"#;
        assert_eq!(text, format!("{line}\n{line}\n"));
    }

    #[test]
    fn the_state_round_trips_like_python() {
        let dir = std::env::temp_dir().join(format!("ship-state-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("logs.json");
        assert_eq!(LogState::load(&p), LogState::default());
        let st = LogState {
            cursor: "s=\u{e9};i=1".into(),
            day: "20261107".into(),
            rows: 3,
        };
        st.save(&p).unwrap();
        // python3 -c 'import json; print(json.dumps({"cursor": "s=\u00e9;i=1", "day": "20261107", "rows": 3}))'
        assert_eq!(
            std::fs::read_to_string(&p).unwrap(),
            r#"{"cursor": "s=\u00e9;i=1", "day": "20261107", "rows": 3}"#
        );
        assert_eq!(LogState::load(&p), st);
        for (text, want) in [
            (
                r#"{"cursor": 5, "day": 7, "rows": " 2 "}"#,
                Some((Value::from(5), "7", 2)),
            ),
            (r#"{"rows": true}"#, Some((Value::Null, "", 1))),
            (r#"{"cursor": "c", "rows": "x"}"#, None),
            (r#"{"cursor": "c", "rows": null}"#, None),
            ("[1]", None),
            ("{", None),
        ] {
            std::fs::write(&p, text).unwrap();
            let got = LogState::load(&p);
            match want {
                Some((c, d, r)) => assert_eq!(
                    (got.cursor, got.day.as_str(), got.rows),
                    (c, d, r),
                    "{text}"
                ),
                None => assert_eq!(got, LogState::default(), "{text}"),
            }
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
