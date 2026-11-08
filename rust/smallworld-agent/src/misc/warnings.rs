//! mc_agent.py `Warnings` (237-256): one stderr line when something starts failing and one when it works
//! again (the journal is shipped too). Owner: misc. Stub with the interface.

use std::collections::HashMap;
use std::sync::Mutex;

#[derive(Default)]
pub struct Warnings {
    failing: Mutex<HashMap<String, String>>,
}

impl Warnings {
    /// `fail(key, text, relog)`. Stub: records only.
    pub fn fail(&self, key: &str, text: &str, _relog: bool) {
        if let Ok(mut f) = self.failing.lock() {
            f.insert(key.into(), text.into());
        }
    }

    /// `ok(key)`. Stub: records only.
    pub fn ok(&self, key: &str) {
        if let Ok(mut f) = self.failing.lock() {
            f.remove(key);
        }
    }
}
