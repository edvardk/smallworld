//! mc_agent.py `Warnings` (237-256): one stderr line when something starts failing and one when it works
//! again, not one per attempt (the journal is shipped too: an outage must not fill the day's D1 rows with the
//! agent's own retries). `WARN` is the module-level instance every loop shares, as in Python.

use std::collections::HashMap;
use std::io::Write;
use std::sync::{LazyLock, Mutex, PoisonError};

#[derive(Default)]
pub struct Warnings {
    failing: Mutex<HashMap<String, String>>,
}

/// mc_agent.py `WARN`.
pub static WARN: LazyLock<Warnings> = LazyLock::new(Warnings::default);

impl Warnings {
    /// The line `fail` would print (None: nothing), and records the text. Split out for the tests.
    fn fail_line(&self, key: &str, text: &str, relog: bool) -> Option<String> {
        let mut f = self.failing.lock().unwrap_or_else(PoisonError::into_inner);
        let line = match f.get(key) {
            Some(old) if !relog || old == text => None,
            _ => Some(format!("{key}: {text} (retrying quietly)")),
        };
        f.insert(key.into(), text.into());
        line
    }

    fn ok_line(&self, key: &str) -> Option<String> {
        let mut f = self.failing.lock().unwrap_or_else(PoisonError::into_inner);
        f.remove(key).map(|_| format!("{key}: working again"))
    }

    /// `fail(key, text, relog=False)`. relog: one more line when the text changes (use only with a fixed set of
    /// texts, never per-attempt details).
    pub fn fail(&self, key: &str, text: &str, relog: bool) {
        if let Some(line) = self.fail_line(key, text, relog) {
            let _ = writeln!(std::io::stderr(), "{line}");
        }
    }

    /// `ok(key)`.
    pub fn ok(&self, key: &str) {
        if let Some(line) = self.ok_line(key) {
            let _ = writeln!(std::io::stderr(), "{line}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn one_line_per_state_change() {
        let w = Warnings::default();
        assert_eq!(w.ok_line("job"), None);
        assert_eq!(
            w.fail_line("job", "POST /ingest/job: HTTP 502", false)
                .as_deref(),
            Some("job: POST /ingest/job: HTTP 502 (retrying quietly)")
        );
        assert_eq!(
            w.fail_line("job", "POST /ingest/job: HTTP 503", false),
            None
        );
        assert_eq!(w.fail_line("job", "POST /ingest/job: HTTP 503", true), None);
        assert_eq!(
            w.fail_line("job", "other", true).as_deref(),
            Some("job: other (retrying quietly)")
        );
        assert_eq!(w.ok_line("job").as_deref(), Some("job: working again"));
        assert_eq!(w.ok_line("job"), None);
    }
}
