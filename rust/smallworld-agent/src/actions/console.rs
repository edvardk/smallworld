//! mc_agent.py's console allowlist (1200-1314): `normalise_console`, `check_console`. A console line runs only
//! if its first word is on the list and its arguments fit that command's shape; NFKC first, the normalised text
//! is what reaches RCON.
//!
//! Parity notes (PORTING.md): Python compiles the shapes with `re.ASCII`, so `\d` is written `[0-9]` here (Rust's
//! `\d` would accept Arabic-Indic digits, which NFKC doesn't fold); `.` is "anything but a newline" in both;
//! lengths are code points; whitespace is Python's `isspace`.

use crate::types::{AResult, AgentError, Value};
use crate::unicode::{is_bad_category, nfkc};
use regex::Regex;
use smallworld_core::pystr::{isspace, len_cp, prefix_cp, split_ws, strip_ws};
use std::collections::HashMap;
use std::sync::LazyLock;

/// mc_agent.py `CONSOLE_MAX`.
pub const CONSOLE_MAX: usize = 256;
const NAME: &str = "[A-Za-z0-9_]{3,16}";
const IP: &str = "(?:[0-9]{1,3}(?:\\.[0-9]{1,3}){3}|[0-9A-Fa-f:]*:[0-9A-Fa-f:.]+)";
const DURATION: &str = "[0-9]{1,9}[dst]?";

/// mc_agent.py `REFUSED_HEADS`.
pub const REFUSED_HEADS: [(&str, &str); 10] = [
    ("execute", "`execute` runs other commands"),
    ("return", "`return` runs other commands"),
    ("function", "functions run other commands"),
    ("op", "operators are managed in Git (players.yml)"),
    ("deop", "operators are managed in Git (players.yml)"),
    (
        "stop",
        "use the panel's Stop (countdown, admin-stopped marker)",
    ),
    ("restart", "use the panel's Restart"),
    ("reload", "/reload breaks plugins; restart instead"),
    ("rl", "/reload breaks plugins; restart instead"),
    ("save-off", "turning saving off would break backups"),
];

/// mc_agent.py `FREE_TEXT`: heads whose arguments keep their case.
pub const FREE_TEXT: [&str; 8] = ["say", "me", "tell", "msg", "w", "kick", "ban", "ban-ip"];
/// mc_agent.py `MESSAGE_FROM`: where a chat message starts (selectors refused from there on).
pub const MESSAGE_FROM: [(&str, usize); 5] =
    [("say", 1), ("me", 1), ("tell", 2), ("msg", 2), ("w", 2)];

/// `_shape(*patterns)`: the alternatives, fully anchored.
fn shape(patterns: &[&str]) -> Regex {
    let alts: Vec<String> = patterns.iter().map(|p| format!("(?:{p})")).collect();
    Regex::new(&format!("^(?:{})$", alts.join("|"))).expect("a constant pattern")
}

/// mc_agent.py `CONSOLE_ALLOWLIST`.
pub static CONSOLE_ALLOWLIST: LazyLock<HashMap<&'static str, Regex>> = LazyLock::new(|| {
    let target_msg = format!("(?:{NAME}|@[apr]) .+");
    let opt_reason = format!("{NAME}(?: .+)?");
    let ban_ip = format!("(?:{IP}|{NAME})(?: .+)?");
    let time_set = format!("set (?:day|night|noon|midnight|{DURATION})");
    let time_add = format!("add {DURATION}");
    let weather = format!("(?:clear|rain|thunder)(?: {DURATION})?");
    let mut m = HashMap::new();
    m.insert("list", shape(&["", "uuids"]));
    m.insert("say", shape(&[".+"]));
    m.insert("me", shape(&[".+"]));
    for h in ["tell", "msg", "w"] {
        m.insert(h, shape(&[&target_msg]));
    }
    m.insert("kick", shape(&[&opt_reason]));
    m.insert("ban", shape(&[&opt_reason]));
    m.insert("ban-ip", shape(&[&ban_ip]));
    m.insert("pardon", shape(&[NAME]));
    m.insert("pardon-ip", shape(&[IP]));
    m.insert("banlist", shape(&["", "players", "ips"]));
    m.insert("whitelist", shape(&["list", "on", "reload"]));
    m.insert("save-all", shape(&["", "flush"]));
    for h in ["save-on", "seed", "tps", "mspt", "plugins", "pl"] {
        m.insert(h, shape(&[""]));
    }
    m.insert("version", shape(&["", "[A-Za-z0-9_.-]{1,64}"]));
    m.insert("ver", shape(&["", "[A-Za-z0-9_.-]{1,64}"]));
    m.insert("help", shape(&["", "[A-Za-z0-9_-]{1,32}"]));
    m.insert(
        "time",
        shape(&["query (?:daytime|gametime|day)", &time_set, &time_add]),
    );
    m.insert("weather", shape(&[&weather]));
    m.insert("difficulty", shape(&["", "peaceful|easy|normal|hard"]));
    m.insert("gamerule", shape(&["[A-Za-z0-9_.:]{1,64}"]));
    m.insert("worldborder", shape(&["get"]));
    m.insert(
        "spark",
        shape(&["tps", "gc", "ping", "health(?: --(?:memory|network))*"]),
    );
    m.insert(
        "chunky",
        shape(&["progress", "pause", "continue", "selection", "help"]),
    );
    m
});

/// mc_agent.py `SELECTOR_WORD`.
static SELECTOR_WORD: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?:@[aeprsn](?:\[.*)?)$").unwrap());

fn rejected(m: impl Into<String>) -> AgentError {
    AgentError::Rejected(m.into())
}

/// `normalise_console(command)`: the words of one console line after NFKC.
///
/// # Errors
/// `Rejected` with Python's text.
pub fn normalise_console(command: &Value) -> AResult<Vec<String>> {
    let Some(command) = command.as_str().filter(|c| !strip_ws(c).is_empty()) else {
        return Err(rejected("console: empty command"));
    };
    if len_cp(command) > CONSOLE_MAX {
        return Err(rejected(format!(
            "console: one line of at most {CONSOLE_MAX} characters"
        )));
    }
    let text = nfkc(command);
    if len_cp(&text) > CONSOLE_MAX {
        return Err(rejected(format!(
            "console: one line of at most {CONSOLE_MAX} characters (after normalisation)"
        )));
    }
    if text
        .chars()
        .any(|ch| is_bad_category(ch) || (isspace(ch) && ch != ' '))
    {
        return Err(rejected(
            "console: one line of printable text (no control characters)",
        ));
    }
    let mut words: Vec<String> = split_ws(text.trim_matches(' ').trim_start_matches('/'))
        .into_iter()
        .map(str::to_string)
        .collect();
    if words.is_empty() {
        return Err(rejected("console: empty command"));
    }
    words[0] = words[0].to_lowercase();
    Ok(words)
}

/// `check_console(command)`: the line to send over RCON if it is on the allowlist.
///
/// # Errors
/// `Rejected` with Python's text.
pub fn check_console(command: &Value) -> AResult<String> {
    let words = normalise_console(command)?;
    let head = words[0].as_str();
    let args = words[1..].join(" ");
    if head.contains(':') {
        return Err(rejected(format!(
            "console: namespaced commands (`{}`) are not allowed from the panel",
            prefix_cp(head, 40)
        )));
    }
    if let Some((_, why)) = REFUSED_HEADS.iter().find(|(h, _)| *h == head) {
        return Err(rejected(format!(
            "console: `{head}` is not allowed from the panel ({why})"
        )));
    }
    if head == "whitelist"
        && words
            .get(1)
            .is_some_and(|w| matches!(w.to_lowercase().as_str(), "add" | "remove"))
    {
        return Err(rejected(
            "console: use the panel's whitelist flow (it keeps players.yml in Git)",
        ));
    }
    let Some(shape) = CONSOLE_ALLOWLIST.get(head) else {
        return Err(rejected(format!(
            "console: `{}` is not allowed from the panel (not on the console allowlist)",
            prefix_cp(head, 40)
        )));
    };
    let subject = if FREE_TEXT.contains(&head) {
        args.clone()
    } else {
        args.to_lowercase()
    };
    if !shape.is_match(&subject) {
        return Err(rejected(format!(
            "console: these `{head}` arguments are not allowed from the panel"
        )));
    }
    if let Some((_, from)) = MESSAGE_FROM.iter().find(|(h, _)| *h == head)
        && words.iter().skip(*from).any(|w| SELECTOR_WORD.is_match(w))
    {
        return Err(rejected(format!(
            "console: selectors (@a, @e, ...) inside a `{head}` message are not allowed here"
        )));
    }
    Ok(words.join(" "))
}

/// The outcome of one call as the harness's vectors write it: `{"ok": result}` or `{"error": message, "type":
/// class}` (scripts/parity_vectors.py `outcome`).
fn outcome<T: Into<Value>>(r: AResult<T>) -> Value {
    match r {
        Ok(v) => serde_json::json!({"ok": v.into()}),
        Err(e) => serde_json::json!({"error": e.to_string(), "type": e.class_name()}),
    }
}

/// `smallworld-agent selftest-console` (hidden, test-only): reads a JSON array of console inputs on stdin and
/// prints, for each, `{"normalise": outcome, "check": outcome}`, the shape of the harness's console vectors.
/// Pure: it runs `normalise_console` and `check_console` and nothing else (no RCON, no file, no audit), so
/// tests/test_rust_agent_console.py can compare the allowlist with Python's on generated input (Hypothesis).
/// Exit 0, or 2 (with a message on stderr) when stdin isn't a JSON array.
pub fn selftest_main(input: &str, out: &mut dyn std::io::Write) -> u8 {
    let Some(Value::Array(items)) = smallworld_core::pyjson::loads(input) else {
        eprintln!("selftest-console: stdin must be a JSON array of console inputs");
        return 2;
    };
    let results: Vec<Value> = items
        .iter()
        .map(|item| {
            serde_json::json!({
                "normalise": outcome(normalise_console(item)),
                "check": outcome(check_console(item)),
            })
        })
        .collect();
    let text = smallworld_core::pyjson::dumps_compact(&Value::Array(results));
    if writeln!(out, "{text}").is_err() {
        return 1;
    }
    0
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const VECTORS: &str = include_str!("../../../../tests/agent_parity/vectors/console.json");

    /// Every console vector (the harness corpus: digits of every script, fullwidth, U+001C..U+001F,
    /// selectors, namespaced heads, the length limits) gives Python's line or refusal, byte for byte.
    #[test]
    fn console_vectors() {
        let data: Value = serde_json::from_str(VECTORS).unwrap();
        let vectors = data["vectors"].as_array().unwrap();
        assert!(vectors.len() > 200);
        for v in vectors {
            let input = &v["input"];
            assert_eq!(outcome(check_console(input)), v["check"], "check {input}");
            if let Some(want) = v.get("normalise") {
                assert_eq!(
                    outcome(normalise_console(input)),
                    *want,
                    "normalise {input}"
                );
            }
        }
    }

    #[test]
    fn selftest_reads_an_array_and_runs_nothing_else() {
        let mut out = Vec::new();
        assert_eq!(selftest_main(r#"["list", "op x", 5]"#, &mut out), 0);
        let got: Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(got[0]["check"], json!({"ok": "list"}));
        assert_eq!(got[0]["normalise"], json!({"ok": ["list"]}));
        assert_eq!(got[1]["check"]["type"], json!("Rejected"));
        assert_eq!(got[2]["check"]["error"], json!("console: empty command"));
        let mut out = Vec::new();
        assert_eq!(selftest_main("{}", &mut out), 2);
        assert!(out.is_empty());
    }

    fn ok(s: &str) -> String {
        check_console(&json!(s)).unwrap()
    }

    fn no(s: &str) -> String {
        match check_console(&json!(s)) {
            Err(AgentError::Rejected(m)) => m,
            other => panic!("{s:?}: {other:?}"),
        }
    }

    #[test]
    fn allowlist_like_python() {
        assert_eq!(ok("/LIST"), "list");
        assert_eq!(ok("say Hello @ there"), "say Hello @ there");
        assert_eq!(ok("\u{ff53}\u{ff41}\u{ff59} hi"), "say hi");
        assert_eq!(ok("time add 3d"), "time add 3d");
        assert_eq!(ok("kick Steve bye now"), "kick Steve bye now");
        assert_eq!(ok("ban-ip 203.0.113.7"), "ban-ip 203.0.113.7");
        assert_eq!(
            ok("spark health --memory --network"),
            "spark health --memory --network"
        );
        assert_eq!(
            no("time add \u{663}"),
            "console: these `time` arguments are not allowed from the panel"
        );
        assert_eq!(
            no("op Steve"),
            "console: `op` is not allowed from the panel (operators are managed in Git (players.yml))"
        );
        assert_eq!(
            no("minecraft:op Steve"),
            "console: namespaced commands (`minecraft:op`) are not allowed from the panel"
        );
        assert_eq!(
            no("whitelist ADD x"),
            "console: use the panel's whitelist flow (it keeps players.yml in Git)"
        );
        assert_eq!(
            no("summon zombie"),
            "console: `summon` is not allowed from the panel (not on the console allowlist)"
        );
        assert_eq!(
            no("say hi @e[type=x]"),
            "console: selectors (@a, @e, ...) inside a `say` message are not allowed here"
        );
        assert_eq!(ok("tell @a hi"), "tell @a hi");
        assert_eq!(
            no("tell @a hi @p"),
            "console: selectors (@a, @e, ...) inside a `tell` message are not allowed here"
        );
        assert_eq!(
            no("say a\u{1c}b"),
            "console: one line of printable text (no control characters)"
        );
        assert_eq!(no("   "), "console: empty command");
        assert_eq!(no("///"), "console: empty command");
        assert_eq!(
            no(&"a".repeat(257)),
            "console: one line of at most 256 characters"
        );
        assert_eq!(
            no(&format!("say {}", "\u{fdfa}".repeat(20))),
            "console: one line of at most 256 characters (after normalisation)"
        );
        assert!(matches!(
            check_console(&json!(5)),
            Err(AgentError::Rejected(_))
        ));
    }
}
