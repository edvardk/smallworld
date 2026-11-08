//! `message_text`, `journal_entry`, `for_d1`, `for_archive`, `journal_argv`, the streamed batch read with its
//! caps (`LOG_BATCH_MAX_ENTRIES` 5000, `LOG_BATCH_MAX_BYTES` 8 MiB, `LOG_ENTRY_MAX_CHARS` 8000) and the CPU
//! `Slicer` (mc_agent.py 596-760). Owner: ship. Stub.

use crate::types::{Obj, Value};

pub const LOG_ROW_MAX_CHARS: usize = 2000;
pub const LOG_BATCH_MAX_ENTRIES: usize = 5000;
pub const LOG_BATCH_MAX_BYTES: usize = 8 << 20;
pub const LOG_ENTRY_MAX_CHARS: usize = 8000;
pub const LOG_CATCHUP_BATCHES: usize = 20;
pub const JOURNAL_FIELDS: &str = "_SYSTEMD_UNIT,SYSLOG_IDENTIFIER,PRIORITY,MESSAGE,MC_AUDIT,UNIT";
/// mc_agent.py `FIREWALL_PREFIXES`.
pub const FIREWALL_PREFIXES: [&str; 3] = ["mc-setfull:", "mc-ratelimit:", "mc-connlimit:"];

/// `message_text(value)`: journald's MESSAGE (a string, or a byte array for non-UTF-8 text). Stub.
pub fn message_text(_value: &Value) -> String {
    String::new()
}

/// `journal_entry(raw)`: the kept fields of one `journalctl -o json` line, or None. Stub.
pub fn journal_entry(_raw: &Obj) -> Option<Obj> {
    None
}

/// `for_d1(entry)`. Stub (ships nothing).
pub fn for_d1(_entry: &Obj) -> bool {
    false
}

/// `for_archive(entry)`. Stub (archives nothing).
pub fn for_archive(_entry: &Obj) -> bool {
    false
}
