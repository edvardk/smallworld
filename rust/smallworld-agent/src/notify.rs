//! The parts of scripts/notify.py the agent uses in-process: the maintenance window (local marker + watchdog),
//! the markers (`admin_stopped`, `fenced`) and the Discord webhook. Like Python, nothing here ever fails its
//! caller: problems are one line on stderr, and no URL query or token is ever printed.

use crate::http::{self, HttpError};
use serde_json::json;
use smallworld_core::error::PyError;
use smallworld_core::paths;
use smallworld_core::pystr::{PyStr, collapse_ws, prefix_cp};
use std::io;
use std::path::Path;

/// notify.py `MAX_MAINTENANCE_MINUTES` (= the Worker's limit).
pub const MAX_MAINTENANCE_MINUTES: i64 = 1440;

fn env(name: &str) -> String {
    std::env::var(name).unwrap_or_default()
}

/// `_where(url)`: the URL without its query.
fn where_(url: &str) -> &str {
    url.split('?').next().unwrap_or(url)
}

/// `str(OSError)` for an error about one file.
fn os_error_text(e: &io::Error, path: &Path) -> String {
    PyError::from_io_with_path(e, Some(&PyStr::from(path.to_string_lossy().as_ref()))).to_string()
}

/// `notify.maintenance(minutes, reason)`: open (`minutes > 0`) or close (0) a maintenance window, locally
/// (`maintenance.json`, which health.py's self-heal respects) and on the watchdog (`POST /maintenance`).
pub fn maintenance(minutes: i64, reason: &str) {
    maintenance_at(
        minutes,
        reason,
        crate::sys::now(),
        Path::new(paths::MAINTENANCE_FILE),
    );
}

pub fn maintenance_at(minutes: i64, reason: &str, now: f64, marker: &Path) {
    let minutes = minutes.clamp(0, MAX_MAINTENANCE_MINUTES);
    let reason = prefix_cp(&collapse_ws(reason), 200).to_string();
    let result: Result<(), String> = if minutes > 0 {
        #[allow(clippy::cast_precision_loss)]
        let until = now + 60.0 * minutes as f64;
        // json.dumps({"until": until, "reason": reason}): key order and float repr as Python's.
        let text = format!(
            "{{\"until\": {}, \"reason\": {}}}",
            smallworld_core::pyjson::float_repr(until),
            smallworld_core::pyjson::string(&reason, true)
        );
        let tmp = smallworld_core::fsx::tmp_path(marker);
        match std::fs::write(&tmp, text) {
            Err(e) => Err(os_error_text(&e, &tmp)),
            Ok(()) => std::fs::rename(&tmp, marker).map_err(|e| {
                // os.replace's message names both files: "[Errno 2] ...: 'a' -> 'b'"
                let base = PyError::from_io(&e).to_string();
                format!(
                    "{base}: {} -> {}",
                    PyStr::from(tmp.to_string_lossy().as_ref()).repr(),
                    PyStr::from(marker.to_string_lossy().as_ref()).repr()
                )
            }),
        }
    } else {
        smallworld_core::fsx::unlink_missing_ok(marker).map_err(|e| os_error_text(&e, marker))
    };
    if let Err(text) = result {
        eprintln!("maintenance marker {}: {text}", marker.display());
    }
    let (base, token) = (env("WATCHDOG_URL"), env("HEARTBEAT_TOKEN"));
    if !base.is_empty() && !token.is_empty() {
        let url = format!("{}/maintenance", base.trim_end_matches('/'));
        let body = format!(
            "{{\"minutes\": {minutes}, \"reason\": {}}}",
            smallworld_core::pyjson::string(&reason, true)
        );
        if let Err(e) = http::notify_post(&url, body.as_bytes(), "application/json", Some(&token)) {
            eprintln!(
                "watchdog maintenance at {} failed: {}",
                where_(&url),
                e.class_name()
            );
        }
    }
}

/// `notify.admin_stopped()`.
pub fn admin_stopped() -> bool {
    smallworld_core::fsx::exists(Path::new(paths::ADMIN_STOPPED_FILE))
}

/// `notify.fenced()`.
pub fn fenced() -> bool {
    smallworld_core::fsx::exists(Path::new(paths::FENCED_FILE))
}

/// `notify.discord(message)`: `{"content": message[:1900]}` to `$DISCORD_WEBHOOK_URL` if set. Never fails.
pub fn discord(message: &str) {
    discord_to(message, &env("DISCORD_WEBHOOK_URL"));
}

pub fn discord_to(message: &str, url: &str) {
    if url.is_empty() {
        return;
    }
    let body = json!({"content": prefix_cp(message, 1900)}).to_string();
    if let Err(e) = http::notify_post(url, body.as_bytes(), "application/json", None) {
        let class = match e {
            HttpError::Status(..) => "HTTPError",
            HttpError::Transport(c) => c,
        };
        eprintln!("discord notify failed: {class}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maintenance_marker_like_python() {
        let d = std::env::temp_dir().join(format!("swagent-notify-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let m = d.join("maintenance.json");
        maintenance_at(15, "  panel\n restart ", 1_700_000_000.5, &m);
        assert_eq!(
            std::fs::read_to_string(&m).unwrap(),
            r#"{"until": 1700000900.5, "reason": "panel restart"}"#
        );
        maintenance_at(0, "x", 0.0, &m);
        assert!(!m.exists());
        maintenance_at(0, "x", 0.0, &m); // missing is fine
        std::fs::remove_dir_all(d).unwrap();
    }
}
