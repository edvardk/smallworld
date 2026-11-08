//! The agent half of scripts/mc_panel.py: plugins and snapshots (plugins.rs), pending whitelist and world
//! changes (pending.rs), rejected logins (rejected.rs, owner: data). The job runner is jobs.rs; the
//! whitelist -> Git sync, GitHub client and self-test are sync/ and misc/.

pub mod pending;
pub mod plugins;
pub mod rejected;

use regex::Regex;
use std::sync::LazyLock;

fn re(p: &str) -> Regex {
    Regex::new(&format!("^(?:{p})$")).expect("a constant pattern")
}

/// mc_panel.py `NAME` (a Java player name).
pub static NAME: LazyLock<Regex> = LazyLock::new(|| re("[A-Za-z0-9_]{3,16}"));
/// mc_panel.py `UUID`.
pub static UUID: LazyLock<Regex> =
    LazyLock::new(|| re("[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"));
/// mc_panel.py `SNAPSHOT_ID`.
pub static SNAPSHOT_ID: LazyLock<Regex> = LazyLock::new(|| re("[0-9a-f]{8,64}"));
/// mc_panel.py `PLUGIN_NAME` (= mcctl.schema PLUGIN_NAME).
pub static PLUGIN_NAME: LazyLock<Regex> = LazyLock::new(|| re("[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"));
/// mc_panel.py `JAR`.
pub static JAR: LazyLock<Regex> = LazyLock::new(|| re(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,199}\.jar"));

/// mc_panel.py `PENDING_TTL_S`.
pub const PENDING_TTL_S: i64 = 86400;
/// mc_panel.py `BATCH_S`.
pub const BATCH_S: i64 = 60;
/// mc_panel.py `PR_CHECK_S`.
pub const PR_CHECK_S: i64 = 120;
/// mc_panel.py `REJECTED_KEEP`.
pub const REJECTED_KEEP: usize = 50;
