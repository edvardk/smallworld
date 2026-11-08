//! mc_agent.py `Settings.from_env` and `check_url` (owner: link). The daemon's configuration comes from the
//! environment only (systemd `EnvironmentFile=/etc/minecraft/agent.env`; the cli runs under `mc-run`, which
//! loads /etc/minecraft/notify.env): there is no config file of the agent's own to parse.

use smallworld_core::paths;
use std::collections::HashMap;
use std::net::IpAddr;
use std::path::PathBuf;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Settings {
    pub ingest_url: String,
    pub token: String,
    pub hmac_key: Vec<u8>,
    pub metrics_interval_s: i64,
    pub log_batch_s: i64,
    pub log_max_rows_per_day: i64,
    pub poll_s: i64,
    pub state_dir: PathBuf,
    pub panel_sync_s: i64,
    pub panel_batch_s: i64,
    pub panel_pr_check_s: i64,
    pub experience: bool,
    pub client: bool,
    pub client_request_dir: PathBuf,
    pub client_code_file: PathBuf,
}

/// The environment as a map (tests pass their own).
pub type Env = HashMap<String, String>;

pub fn process_env() -> Env {
    std::env::vars_os()
        .filter_map(|(k, v)| Some((k.into_string().ok()?, v.into_string().ok()?)))
        .collect()
}

/// `int(e.get(name) or default)`: Python's `int()` accepts surrounding whitespace, `_` between digits and a
/// sign; anything else is a `ValueError` (the daemon then exits 2 with the message).
fn int_env(e: &Env, name: &str, default: i64) -> Result<i64, String> {
    let raw = e.get(name).map_or("", String::as_str);
    if raw.is_empty() {
        return Ok(default);
    }
    let t = smallworld_core::pystr::strip_ws(raw);
    let (neg, body) = match t.strip_prefix('-') {
        Some(b) => (true, b),
        None => (false, t.strip_prefix('+').unwrap_or(t)),
    };
    let ok = !body.is_empty()
        && !body.starts_with('_')
        && !body.ends_with('_')
        && !body.contains("__")
        && body
            .chars()
            .all(|c| c == '_' || crate::unicode::decimal_value(c).is_some());
    let digits: String = body.chars().filter(|&c| c != '_').collect();
    match (ok, crate::unicode::parse_decimal(&digits)) {
        (true, Some(n)) => Ok(if neg { -n } else { n }),
        _ => Err(format!(
            "invalid literal for int() with base 10: {}",
            smallworld_core::pyjson::repr_str(raw)
        )),
    }
}

impl Settings {
    /// `Settings.from_env(env)`. `Err` carries the `ValueError` text (`main` prints `mc-agent: {e}`, exits 2).
    pub fn from_env(e: &Env) -> Result<Settings, String> {
        let get = |k: &str| e.get(k).map_or("", String::as_str);
        let url = get("INGEST_URL").trim_end_matches('/').to_string();
        check_url(&url)?;
        let (token, key) = (get("AGENT_TOKEN"), get("COMMAND_HMAC_KEY"));
        if token.chars().count() < 32 || key.chars().count() < 32 {
            return Err("AGENT_TOKEN and COMMAND_HMAC_KEY must be set (>= 32 characters)".into());
        }
        let or = |k: &str, d: &str| -> PathBuf {
            let v = get(k);
            PathBuf::from(if v.is_empty() { d } else { v })
        };
        let state = get("STATE_DIRECTORY");
        let state = if state.is_empty() {
            paths::AGENT_STATE_DIR
        } else {
            state
        };
        Ok(Settings {
            ingest_url: url,
            token: token.into(),
            hmac_key: key.as_bytes().to_vec(),
            metrics_interval_s: int_env(e, "METRICS_INTERVAL_S", 60)?.clamp(30, 300),
            log_batch_s: int_env(e, "LOG_BATCH_S", 300)?.max(10),
            log_max_rows_per_day: int_env(e, "LOG_MAX_ROWS_PER_DAY", 30000)?.max(0),
            poll_s: int_env(e, "POLL_S", 15)?.max(5),
            // StateDirectory= entries arrive joined by ":" (mc-agent + mc-telemetry); ours is first.
            state_dir: PathBuf::from(state.split(':').next().unwrap_or(state)),
            panel_sync_s: int_env(e, "PANEL_SYNC_S", 30)?.max(1),
            panel_batch_s: int_env(e, "PANEL_BATCH_S", 60)?.max(1),
            panel_pr_check_s: int_env(e, "PANEL_PR_CHECK_S", 120)?.max(1),
            experience: e.get("EXPERIENCE").map_or("0", String::as_str) == "1",
            client: e.get("CLIENT").map_or("0", String::as_str) == "1",
            client_request_dir: or("CLIENT_REQUEST_DIR", paths::CLIENT_REQUEST_DIR),
            client_code_file: or("CLIENT_CODE_FILE", paths::CLIENT_CODE_FILE),
        })
    }
}

/// `urllib.parse.urlsplit(url).hostname`: lower-cased, without userinfo, port or IPv6 brackets.
pub fn url_hostname(url: &str) -> Option<String> {
    let (_, rest) = url.split_once("://")?;
    let netloc = rest.split(['/', '?', '#']).next().unwrap_or("");
    let host = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    let host = if let Some(inner) = host.strip_prefix('[') {
        inner.split(']').next().unwrap_or("")
    } else {
        host.split(':').next().unwrap_or("")
    };
    (!host.is_empty()).then(|| host.to_lowercase())
}

/// `check_url`: https only; http only to a loopback host (the e2e mock ingest).
pub fn check_url(url: &str) -> Result<(), String> {
    let scheme = url.split_once("://").map(|(s, _)| s.to_lowercase());
    if let Some(host) = url_hostname(url) {
        match scheme.as_deref() {
            Some("https") => return Ok(()),
            Some("http")
                if host == "localhost"
                    || host.parse::<IpAddr>().is_ok_and(|ip| ip.is_loopback()) =>
            {
                return Ok(());
            }
            _ => {}
        }
    }
    Err("INGEST_URL must be https:// (http:// only for a loopback address)".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env(pairs: &[(&str, &str)]) -> Env {
        pairs
            .iter()
            .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
            .collect()
    }

    #[test]
    fn from_env_clamps_like_python() {
        let t = "t".repeat(32);
        let s = Settings::from_env(&env(&[
            ("INGEST_URL", "https://hb.example.org/"),
            ("AGENT_TOKEN", &t),
            ("COMMAND_HMAC_KEY", &t),
            ("METRICS_INTERVAL_S", "5"),
            ("POLL_S", " 1 "),
            ("STATE_DIRECTORY", "/var/lib/mc-agent:/var/lib/mc-telemetry"),
        ]))
        .unwrap();
        assert_eq!(s.ingest_url, "https://hb.example.org");
        assert_eq!(s.metrics_interval_s, 30);
        assert_eq!(s.poll_s, 5);
        assert_eq!(s.state_dir, PathBuf::from("/var/lib/mc-agent"));
        assert!(!s.experience);
    }

    #[test]
    fn bad_env_is_a_value_error() {
        let t = "t".repeat(32);
        let e = env(&[
            ("INGEST_URL", "https://x"),
            ("AGENT_TOKEN", &t),
            ("COMMAND_HMAC_KEY", &t),
            ("POLL_S", "x"),
        ]);
        assert_eq!(
            Settings::from_env(&e).unwrap_err(),
            "invalid literal for int() with base 10: 'x'"
        );
        let short = env(&[("INGEST_URL", "https://x"), ("AGENT_TOKEN", "a")]);
        assert!(
            Settings::from_env(&short)
                .unwrap_err()
                .starts_with("AGENT_TOKEN")
        );
    }

    #[test]
    fn urls() {
        assert!(check_url("https://hb.example.org").is_ok());
        assert!(check_url("http://127.0.0.1:8787").is_ok());
        assert!(check_url("http://[::1]:8787").is_ok());
        assert!(check_url("http://localhost").is_ok());
        assert!(check_url("http://example.org").is_err());
        assert!(check_url("ftp://x").is_err());
        assert!(check_url("").is_err());
        assert_eq!(
            url_hostname("https://u:p@HB.Example.org:443/x"),
            Some("hb.example.org".into())
        );
    }
}
