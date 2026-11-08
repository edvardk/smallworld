//! scripts/alerts.py `Alert` + `render` + `scrub` for the agent's Discord texts, and mc_panel.py's texts
//! (`selftest_alert`, `selftest_back_alert`, `expired_alert`, `rejected_alert`, `world_rejected_alert`; 639-831).
//! Byte-exact: operators and docs quote them. Owner: sync (misc's self-test uses the two selftest texts).

use super::super::panel::PENDING_TTL_S;
use regex::Regex;
use smallworld_core::pystr::{len_cp, prefix_cp};
use std::fmt::Write as _;
use std::sync::LazyLock;

/// alerts.py `MAX_CHARS`.
pub const MAX_CHARS: usize = 1900;
/// alerts.py `NO_ACTION`.
pub const NO_ACTION: &str = "No action needed - informational.";

// alerts.py IPV4/IPV6 (no re.ASCII: `\d` and `\b` are Unicode, as Rust's are)
static IPV4: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\b(?:\d{1,3}\.){3}\d{1,3}\b").unwrap());
static IPV6: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{0,4}\b").unwrap());

/// alerts.py `Alert` (severity: info | warning | action | recovered).
#[derive(Debug, Clone)]
pub struct Alert<'a> {
    pub severity: &'a str,
    pub headline: String,
    pub impact: String,
    pub cause: String,
    pub todo: String,
    pub details: String,
}

impl<'a> Alert<'a> {
    pub fn new(severity: &'a str, headline: impl Into<String>) -> Self {
        Alert {
            severity,
            headline: headline.into(),
            impact: String::new(),
            cause: String::new(),
            todo: NO_ACTION.into(),
            details: String::new(),
        }
    }
}

fn emoji(severity: &str) -> &'static str {
    match severity {
        "info" => "\u{2139}\u{fe0f}",
        "warning" => "\u{26a0}\u{fe0f}",
        "action" => "\u{1f534}",
        _ => "\u{2705}",
    }
}

/// alerts.py `scrub`: no IP addresses in an alert.
pub fn scrub(text: &str) -> String {
    let v4 = IPV4.replace_all(text, "<address>");
    IPV6.replace_all(&v4, "<address>").into_owned()
}

/// alerts.py `render`.
pub fn render(a: &Alert<'_>) -> String {
    let mut lines = vec![format!("{} **{}**", emoji(a.severity), a.headline)];
    for (label, value) in [
        ("Impact", &a.impact),
        ("Likely cause", &a.cause),
        ("What to do", &a.todo),
    ] {
        if !value.is_empty() {
            lines.push(format!("**{label}:** {value}"));
        }
    }
    if !a.details.is_empty() {
        lines.push(format!("**Details:** {}", a.details));
    }
    let text = scrub(&lines.join("\n"));
    if len_cp(&text) <= MAX_CHARS {
        text
    } else {
        format!("{}\u{2026}", prefix_cp(&text, MAX_CHARS - 1))
    }
}

/// `selftest_alert(error, failures)`.
pub fn selftest_alert(error: &str, failures: i64) -> String {
    render(&Alert {
        impact: "Whitelist and world changes made in the panel stay live but never reach Git; whitelist \
                 changes are dropped after 24 h, and the next deploy undoes them."
            .into(),
        cause: format!(
            "The daily Panel -> Git self-test failed {failures} times in a row, at {error}"
        ),
        todo: "Run `mcctl doctor`, and the self-test from the panel's Health tab after a fix. Token or \
               permissions: the panel's GitHub App (installed on the config repo, Contents + Pull requests); \
               a stale Worker: `mcctl render` + `mcctl apply`."
            .into(),
        details: format!("{failures} consecutive failures; last: {error}"),
        ..Alert::new(
            "warning",
            "The panel can't open pull requests on the config repo",
        )
    })
}

/// `selftest_back_alert(failures)`.
pub fn selftest_back_alert(failures: i64) -> String {
    render(&Alert {
        impact: "Panel whitelist and world changes reach the config repo again.".into(),
        cause: "The Panel -> Git self-test passed.".into(),
        details: format!("after {failures} failed run(s)"),
        ..Alert::new("recovered", "The panel can open pull requests again")
    })
}

/// `expired_alert(names)`.
pub fn expired_alert(names: &str) -> String {
    render(&Alert {
        impact: format!(
            "Live on the server for now ({names}), but the next deploy follows players.yml in Git, so they \
             will be undone then."
        ),
        cause: "They never reached the config repo within 24 h (no pull request could be opened or merged: \
                the panel's GitHub App, CONFIG_REPO, or GitHub itself)."
            .into(),
        todo: "Re-do them in the panel, or edit players.yml in the config repo and push; `mcctl doctor` checks \
               the panel's GitHub App."
            .into(),
        details: format!(
            "pending panel whitelist change(s) older than {} h: {names}",
            PENDING_TTL_S / 3600
        ),
        ..Alert::new("warning", "Whitelist changes made in the panel were dropped")
    })
}

/// `rejected_alert(number, reverted, failed)`.
pub fn rejected_alert(number: i64, reverted: &[String], failed: &[String]) -> String {
    let mut todo = "Nothing if that was intended.".to_string();
    if !failed.is_empty() {
        todo += " To undo it now: deploy (`mcctl deploy`, or push to main).";
    }
    todo += " To make the change after all: the panel again, or players.yml in the config repo.";
    let mut impact = String::new();
    if !reverted.is_empty() {
        let _ = write!(
            impact,
            "Undone on the live server with: whitelist {}. ",
            reverted.join(", ")
        );
    }
    if !failed.is_empty() {
        let _ = write!(
            impact,
            "Couldn't run whitelist {}: live until the next deploy. ",
            failed.join(", ")
        );
    }
    impact += "The whitelist follows players.yml in Git again.";
    let or_nothing = |l: &[String]| {
        if l.is_empty() {
            "nothing".to_string()
        } else {
            l.join(", ")
        }
    };
    render(&Alert {
        impact,
        cause: format!("Pull request #{number} was closed without merging."),
        todo,
        details: format!(
            "reverted: {}; failed: {}",
            or_nothing(reverted),
            or_nothing(failed)
        ),
        ..Alert::new(
            "info",
            format!("A panel whitelist change was turned down (pull request #{number} closed)"),
        )
    })
}

/// `world_rejected_alert(number, active)`.
pub fn world_rejected_alert(number: i64, active: &str) -> String {
    render(&Alert {
        impact: format!(
            "The server keeps running the world {active}; deploys honour it, but server.yml in Git still \
             names another world."
        ),
        cause: format!(
            "Pull request #{number} (server.yml world.active/library) was closed without merging."
        ),
        todo: "Merge an equivalent change to server.yml (world.active and world.library as the panel shows \
               them), or switch back in the panel's Worlds tab. The next world change opens a new pull \
               request."
            .into(),
        details: format!("world.active on the server: {active}"),
        ..Alert::new(
            "warning",
            format!("A panel world change wasn't merged (pull request #{number} closed)"),
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    #[test]
    fn texts_match_the_vectors() {
        let v: Value = serde_json::from_str(include_str!(
            "../../../../tests/agent_parity/vectors/texts.json"
        ))
        .unwrap();
        let a = &v["vectors"]["alerts"];
        let s = |k: &str| a[k].as_str().unwrap().to_string();
        assert_eq!(
            selftest_alert("token: no GitHub token from the Worker: HTTP 502", 2),
            s("selftest_alert")
        );
        assert_eq!(selftest_back_alert(3), s("selftest_back_alert"));
        assert_eq!(expired_alert("add Dave, remove Bob"), s("expired_alert"));
        assert_eq!(world_rejected_alert(2, "main"), s("world_rejected_alert"));
        let mut rendered = Alert::new("info", "from 203.0.113.7 and 2001:db8::1");
        rendered.details = "192.0.2.4".into();
        assert_eq!(render(&rendered), s("render_ip_scrub"));
        assert_eq!(
            render(&Alert::new("action", "h".repeat(1950))),
            s("render_long")
        );
        let cases = a["rejected_alert"].as_array().unwrap();
        assert!(!cases.is_empty());
        let sv = |x: &[&str]| x.iter().map(|v| (*v).to_string()).collect::<Vec<_>>();
        let want: [(&[&str], &[&str]); 3] = [
            (&["remove Bob"], &[]),
            (&[], &["add Eve"]),
            (&["remove A"], &["add B"]),
        ];
        assert_eq!(cases.len(), want.len());
        for (i, (r, f)) in want.iter().enumerate() {
            assert_eq!(
                rejected_alert(4, &sv(r), &sv(f)),
                cases[i].as_str().unwrap(),
                "case {i}"
            );
        }
    }
}
