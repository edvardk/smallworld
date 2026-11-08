//! The secret scrubber (mc_agent.py 640-725): `SECRET_PATTERNS`, `SECRET_ENV`, `SECRET_SUFFIXES`, `Scrubber`
//! (known values first, then the shapes), `known_secrets` (incl. the RCON password), `scrubber_for`. Owner: ship.
//!
//! A security control: it decides what leaves the box, so it has to redact exactly what Python's does
//! (SECURITY.md's claims are about that code). Python's patterns use `re` features the `regex` crate lacks or
//! defines differently, so each pattern below is a rewrite, pinned by the vectors and by differential fuzzing
//! against the Python scrubber (`selftest-scrub`, tests/test_rust_scrub_fuzz.py):
//!
//! - **Look-around** (`(?<!...)`, `(?!...)`, `(?=@)`): the `regex` crate has none. Each pattern is the body
//!   without its look-arounds plus manual checks of the character before the match start and the one after its
//!   end (`Lookbehind`, `Lookahead`). A start whose checks fail is skipped and the search resumes one character
//!   later, which is what Python's search does. That is exact because every look-behind sits at the very start
//!   of its pattern, and a look-ahead sits at the end of a match that has only one possible end for a given
//!   start: a fixed length (`AKIA...`) or a greedy run that can't contain the looked-for `@` (URL userinfo).
//! - **`\s`** is Python's `str.isspace()` (U+001C..U+001F included; the `regex` crate's `\s` excludes them):
//!   the `PS` class spelled out, checked against `pystr::is_space` for every code point in the tests.
//! - **`(?i)`**: Python's `re` folds through its own tables: besides ASCII case pairs, `i` also matches U+0130
//!   and U+0131, `k` U+212A, `s` U+017F, and a class with letters (`[a-z]`, `[A-Za-z0-9]`, ...) all four. The
//!   `regex` crate's simple case folding misses U+0130 and U+0131. So no pattern here uses `(?i)`: letters are
//!   explicit classes (`ci`) and letter classes carry `CI_EXTRA`, measured from Python (3.11-3.13 agree).
//!
//! What the code does, not what Python's docstring says: `known_secrets` doesn't include the panel's GitHub
//! token (installation tokens are caught by the `gh[pousr]_` shape).

use crate::settings::{Env, Settings};
use regex::Regex;
use std::fmt::Write as _;
use std::sync::OnceLock;

/// mc_agent.py `SECRET_ENV`: environment variables whose values are removed wherever they appear.
pub const SECRET_ENV: [&str; 4] = [
    "AGENT_TOKEN",
    "COMMAND_HMAC_KEY",
    "HEARTBEAT_TOKEN",
    "DISCORD_WEBHOOK_URL",
];

/// mc_agent.py `SECRET_SUFFIXES`.
pub const SECRET_SUFFIXES: [&str; 5] = ["_TOKEN", "_KEY", "_SECRET", "_PASSWORD", "_WEBHOOK_URL"];

/// What a redaction leaves.
pub const REDACTED: &str = "<redacted>";

/// Python's `SECRET_PATTERNS[i].pattern`, verbatim: the spec each rewrite follows (the vectors check that these
/// are still Python's).
pub const PY_PATTERNS: [&str; 8] = [
    r"tskey-[A-Za-z0-9_-]+",
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
    r#"(?i)((?<![A-Za-z0-9])bearer\s+)[^\s\"',;]+"#,
    r"(?i)((?<![A-Za-z0-9])basic\s+)[A-Za-z0-9+/=._-]{6,}",
    r#"(?i)((?<![A-Za-z0-9])(?:(?:[A-Za-z0-9]+[_.-])*(?:password|passwd|pass|pwd|token|secret|api[_-]?key)|(?:[A-Za-z0-9]+[_.-])+key)["']?\s*[=:]\s*)("[^"]*"|'[^']*'|[^\s"',;&}\]]+)"#,
    r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])",
    r"(?i)https://(?:[a-z0-9-]+\.)?discord(?:app)?\.com/api/webhooks/\S+",
    r"(?i)((?<![a-z0-9+.-])[a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+(?=@)",
];

/// The members of Python's `\s` (`str.isspace()`), for use inside a `[...]` class.
const PS: &str = r"\t\n\x0B\x0C\r\x1C-\x20\x{85}\x{A0}\x{1680}\x{2000}-\x{200A}\x{2028}\x{2029}\x{202F}\x{205F}\x{3000}";

/// What Python's `(?i)` adds to a class that contains ASCII letters: U+0130 (İ), U+0131 (ı), U+017F (ſ) and
/// U+212A (Kelvin sign).
const CI_EXTRA: &str = r"\x{130}\x{131}\x{17F}\x{212A}";

/// One literal under Python's `(?i)`: every character Python's `re` matches for it.
fn ci(literal: &str) -> String {
    let mut out = String::new();
    for ch in literal.chars() {
        let extra = match ch {
            'i' => r"\x{130}\x{131}",
            'k' => r"\x{212A}",
            's' => r"\x{17F}",
            _ => "",
        };
        if ch.is_ascii_alphabetic() {
            let _ = write!(
                out,
                "[{}{}{extra}]",
                ch.to_ascii_lowercase(),
                ch.to_ascii_uppercase()
            );
        } else {
            out.push_str(&regex::escape(&ch.to_string()));
        }
    }
    out
}

/// `(?<![...])` before a match: the character before its start must not be in the class.
#[derive(Clone, Copy)]
enum Lookbehind {
    None,
    /// `(?i)(?<![A-Za-z0-9])` (`_NB` under `(?i)`).
    AlnumCi,
    /// `(?<![A-Z0-9])` (no `(?i)`).
    UpperDigit,
    /// `(?i)(?<![a-z0-9+.-])`.
    SchemeCi,
}

/// What must follow a match.
#[derive(Clone, Copy)]
enum Lookahead {
    None,
    /// `(?![A-Z0-9])`.
    NotUpperDigit,
    /// `(?=@)`.
    At,
}

fn is_ci_extra(ch: char) -> bool {
    matches!(ch, '\u{130}' | '\u{131}' | '\u{17F}' | '\u{212A}')
}

impl Lookbehind {
    fn allows(self, before: Option<char>) -> bool {
        let Some(c) = before else { return true };
        !match self {
            Lookbehind::None => false,
            Lookbehind::AlnumCi => c.is_ascii_alphanumeric() || is_ci_extra(c),
            Lookbehind::UpperDigit => c.is_ascii_uppercase() || c.is_ascii_digit(),
            Lookbehind::SchemeCi => {
                c.is_ascii_alphanumeric() || matches!(c, '+' | '.' | '-') || is_ci_extra(c)
            }
        }
    }
}

impl Lookahead {
    fn allows(self, after: Option<char>) -> bool {
        match self {
            Lookahead::None => true,
            Lookahead::NotUpperDigit => {
                !after.is_some_and(|c| c.is_ascii_uppercase() || c.is_ascii_digit())
            }
            Lookahead::At => after == Some('@'),
        }
    }
}

/// One `SECRET_PATTERNS` entry: its look-around-free body, the checks, and whether it has a group 1 (kept).
struct Pattern {
    re: Regex,
    behind: Lookbehind,
    ahead: Lookahead,
    keeps_group: bool,
}

impl Pattern {
    fn new(body: &str, behind: Lookbehind, ahead: Lookahead, keeps_group: bool) -> Pattern {
        // The patterns are constants; a failure here is a bug caught by every test.
        let re = Regex::new(body).expect("scrubber pattern");
        Pattern {
            re,
            behind,
            ahead,
            keeps_group,
        }
    }

    /// `pat.sub(lambda m: (m.group(1) if m.groups() else "") + "<redacted>", text)`.
    fn sub(&self, text: &str) -> String {
        let mut out = String::new();
        let (mut last, mut pos) = (0, 0);
        while pos <= text.len() {
            let Some(caps) = self.re.captures_at(text, pos) else {
                break;
            };
            let m = caps.get(0).expect("group 0");
            let ok = self.behind.allows(text[..m.start()].chars().next_back())
                && self.ahead.allows(text[m.end()..].chars().next());
            if !ok || m.is_empty() {
                // Python's search moves on to the next start position.
                pos = m.start() + text[m.start()..].chars().next().map_or(1, char::len_utf8);
                continue;
            }
            out.push_str(&text[last..m.start()]);
            if self.keeps_group {
                out.push_str(caps.get(1).map_or("", |g| g.as_str()));
            }
            out.push_str(REDACTED);
            last = m.end();
            pos = m.end();
        }
        if last == 0 {
            return text.to_string();
        }
        out.push_str(&text[last..]);
        out
    }
}

/// The eight patterns, compiled once.
fn patterns() -> &'static [Pattern] {
    static PATTERNS: OnceLock<Vec<Pattern>> = OnceLock::new();
    PATTERNS.get_or_init(|| {
        let alnum_ci = format!("[A-Za-z0-9{CI_EXTRA}]");
        let name = format!(
            "(?:(?:{alnum_ci}+[_.\\-])*(?:{}|{}|{}|{}|{}|{}|{}[_\\-]?{})|(?:{alnum_ci}+[_.\\-])+{})",
            ci("password"),
            ci("passwd"),
            ci("pass"),
            ci("pwd"),
            ci("token"),
            ci("secret"),
            ci("api"),
            ci("key"),
            ci("key"),
        );
        let scheme_ci = format!("[a-zA-Z0-9+.\\-{CI_EXTRA}]");
        vec![
            Pattern::new(
                r"tskey-[A-Za-z0-9_-]+",
                Lookbehind::None,
                Lookahead::None,
                false,
            ),
            Pattern::new(
                r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
                Lookbehind::None,
                Lookahead::None,
                false,
            ),
            Pattern::new(
                &format!("({}[{PS}]+)[^{PS}\"',;]+", ci("bearer")),
                Lookbehind::AlnumCi,
                Lookahead::None,
                true,
            ),
            Pattern::new(
                &format!("({}[{PS}]+)[A-Za-z0-9+/=._\\-{CI_EXTRA}]{{6,}}", ci("basic")),
                Lookbehind::AlnumCi,
                Lookahead::None,
                true,
            ),
            Pattern::new(
                &format!(
                    "({name}[\"']?[{PS}]*[=:][{PS}]*)(\"[^\"]*\"|'[^']*'|[^{PS}\"',;&}}\\]]+)"
                ),
                Lookbehind::AlnumCi,
                Lookahead::None,
                true,
            ),
            Pattern::new(
                r"(?:AKIA|ASIA)[0-9A-Z]{16}",
                Lookbehind::UpperDigit,
                Lookahead::NotUpperDigit,
                false,
            ),
            Pattern::new(
                &format!(
                    "{}(?:[a-zA-Z0-9\\-{CI_EXTRA}]+\\.)?{}(?:{})?{}[^{PS}]+",
                    ci("https://"),
                    ci("discord"),
                    ci("app"),
                    ci(".com/api/webhooks/"),
                ),
                Lookbehind::None,
                Lookahead::None,
                false,
            ),
            Pattern::new(
                &format!("([a-zA-Z{CI_EXTRA}]{scheme_ci}*://)[^{PS}/@:]+:[^{PS}/@]+"),
                Lookbehind::SchemeCi,
                Lookahead::At,
                true,
            ),
        ]
    })
}

/// mc_agent.py `Scrubber`: removes secrets from log text before it leaves the box: the exact values this
/// agent knows and well-known secret shapes.
pub struct Scrubber {
    known: Vec<String>,
}

impl Scrubber {
    /// `Scrubber(known)`: distinct values of at least 8 code points, longest first. (Python orders values of
    /// equal length by set iteration, which varies with the hash seed; here they are sorted, which is one of
    /// Python's possible orders.)
    pub fn new(known: Vec<String>) -> Self {
        let mut known: Vec<String> = known
            .into_iter()
            .filter(|k| k.chars().count() >= 8)
            .collect();
        known.sort_by(|a, b| b.chars().count().cmp(&a.chars().count()).then(a.cmp(b)));
        known.dedup();
        Self { known }
    }

    /// The values in the order they are replaced.
    pub fn known(&self) -> &[String] {
        &self.known
    }

    /// `scrubber(text)`.
    pub fn scrub(&self, text: &str) -> String {
        let mut text = text.to_string();
        for value in &self.known {
            if text.contains(value.as_str()) {
                text = text.replace(value.as_str(), REDACTED);
            }
        }
        for pat in patterns() {
            text = pat.sub(&text);
        }
        text
    }
}

/// `known_secrets(s, env)`: every secret value this agent can read: its settings, the listed variables and any
/// `*_TOKEN`/`_KEY`/... in its environment, and the RCON password (empty values dropped).
pub fn known_secrets(s: &Settings, env: &Env) -> Vec<String> {
    let mut known = vec![
        s.token.clone(),
        String::from_utf8_lossy(&s.hmac_key).into_owned(),
    ];
    let mut vars: Vec<(&String, &String)> = env
        .iter()
        .filter(|(k, _)| {
            SECRET_ENV.contains(&k.as_str()) || SECRET_SUFFIXES.iter().any(|suf| k.ends_with(suf))
        })
        .collect();
    vars.sort();
    known.extend(vars.into_iter().map(|(_, v)| v.clone()));
    known.extend(rcon_password());
    known.retain(|k| !k.is_empty());
    known
}

/// `mc_rcon.read_properties(SERVER_DIR / "server.properties")["rcon.password"]` under
/// `contextlib.suppress(OSError, KeyError, ValueError)`: `read_properties` only raises those (OS errors,
/// `UnicodeDecodeError`, its own `ValueError`s), so any error means "no password".
fn rcon_password() -> Option<String> {
    use smallworld_core::pystr::PyStr;
    let props =
        smallworld_core::properties::read_properties(&smallworld_core::mcrcon::properties_path())
            .ok()?;
    let value = props.get(&PyStr::from("rcon.password"))?;
    Some(value.to_string_checked().unwrap_or_else(|| value.display()))
}

/// `scrubber_for(s)`: the scrubber with every secret this process can read.
pub fn scrubber_for(s: &Settings) -> Scrubber {
    Scrubber::new(known_secrets(s, &crate::settings::process_env()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn vectors() -> Value {
        serde_json::from_str(include_str!(
            "../../../../tests/agent_parity/vectors/scrubber.json"
        ))
        .unwrap()
    }

    fn strings(v: &Value) -> Vec<String> {
        v.as_array()
            .unwrap()
            .iter()
            .map(|s| s.as_str().unwrap().to_string())
            .collect()
    }

    #[test]
    fn the_python_patterns_are_the_ones_rewritten() {
        let v = vectors();
        assert_eq!(strings(&v["vectors"]["patterns"]), PY_PATTERNS.to_vec());
    }

    #[test]
    fn the_known_values_like_python() {
        let v = vectors();
        let s = Scrubber::new(strings(&v["vectors"]["known"]));
        assert_eq!(s.known(), strings(&v["vectors"]["known_used"]).as_slice());
    }

    #[test]
    fn the_corpus_like_python() {
        let v = vectors();
        let s = Scrubber::new(strings(&v["vectors"]["known"]));
        let cases = v["vectors"]["cases"].as_array().unwrap();
        assert!(cases.len() > 40);
        for case in cases {
            let input = case["input"].as_str().unwrap();
            assert_eq!(
                s.scrub(input),
                case["output"].as_str().unwrap(),
                "input {input:?}"
            );
        }
    }

    #[test]
    fn the_space_class_is_pythons_isspace() {
        let re = Regex::new(&format!("^[{PS}]$")).unwrap();
        for cp in 0..=0x10_FFFF_u32 {
            if let Some(ch) = char::from_u32(cp) {
                let mut buf = [0; 4];
                assert_eq!(
                    re.is_match(ch.encode_utf8(&mut buf)),
                    smallworld_core::pystr::isspace(ch),
                    "U+{cp:04X}"
                );
            }
        }
    }

    #[test]
    fn case_folding_is_pythons() {
        // python3 -c 'import re; print([hex(c) for c in range(128, 0x110000) if re.fullmatch("(?i)[a-z]", chr(c))])'
        let re = Regex::new(&format!("^[a-zA-Z{CI_EXTRA}]$")).unwrap();
        let extra: Vec<u32> = (128..=0x10_FFFF_u32)
            .filter_map(char::from_u32)
            .filter(|c| re.is_match(&c.to_string()))
            .map(u32::from)
            .collect();
        assert_eq!(extra, vec![0x130, 0x131, 0x17F, 0x212A]);
        let s = Scrubber::new(vec![]);
        assert_eq!(s.scrub("BAS\u{130}C abcdefgh"), "BAS\u{130}C <redacted>");
        assert_eq!(s.scrub("ba\u{17F}ic abcdefgh"), "ba\u{17F}ic <redacted>");
        assert_eq!(s.scrub("\u{212A}ey=v"), "\u{212A}ey=v"); // a bare key= isn't a name
        assert_eq!(s.scrub("x_\u{212A}ey=v"), "x_\u{212A}ey=<redacted>");
        assert_eq!(s.scrub("\u{131}bearer x"), "\u{131}bearer x"); // ı is a letter before bearer
        assert_eq!(s.scrub("\u{e9}bearer x"), "\u{e9}bearer <redacted>"); // é isn't
    }

    #[test]
    fn look_arounds() {
        let s = Scrubber::new(vec![]);
        assert_eq!(s.scrub("AKIA0123456789ABCDEF"), "<redacted>");
        assert_eq!(s.scrub("XAKIA0123456789ABCDEF"), "XAKIA0123456789ABCDEF");
        assert_eq!(s.scrub("AKIA0123456789ABCDEFG"), "AKIA0123456789ABCDEFG");
        assert_eq!(s.scrub("aAKIA0123456789ABCDEF"), "a<redacted>");
        assert_eq!(s.scrub("u ftp://a:b@h"), "u ftp://<redacted>@h");
        assert_eq!(s.scrub("u ftp://a:b/h@x"), "u ftp://a:b/h@x");
        assert_eq!(s.scrub("bearer\u{1c}tok"), "bearer\u{1c}<redacted>");
        assert_eq!(s.scrub("RESTIC_PASSWORD=x"), "RESTIC_PASSWORD=<redacted>");
    }

    #[test]
    fn known_values_first_longest_first() {
        let s = Scrubber::new(vec![
            "short".into(),
            "abcdefgh".into(),
            "abcdefghij".into(),
            "abcdefgh".into(),
        ]);
        assert_eq!(s.known(), ["abcdefghij", "abcdefgh"]);
        assert_eq!(
            s.scrub("abcdefghij abcdefgh short"),
            "<redacted> <redacted> short"
        );
    }

    #[test]
    fn known_secrets_reads_the_settings_and_the_environment() {
        let mut env = Env::new();
        for (k, v) in [
            ("INGEST_URL", "https://worker.example"),
            ("AGENT_TOKEN", "t".repeat(32).as_str()),
            ("COMMAND_HMAC_KEY", "k".repeat(32).as_str()),
            ("RESTIC_PASSWORD", "restic-pass"),
            ("SOMETHING_WEBHOOK_URL", "https://hook.example/x"),
            ("EMPTY_TOKEN", ""),
            ("PATH", "/usr/bin"),
        ] {
            env.insert(k.into(), v.into());
        }
        let s = Settings::from_env(&env).unwrap();
        let mut known = known_secrets(&s, &env);
        known.retain(|k| k != "rcon"); // whatever this machine's server.properties says
        assert!(known.contains(&"restic-pass".to_string()));
        assert!(known.contains(&"https://hook.example/x".to_string()));
        assert!(known.contains(&"t".repeat(32)));
        assert!(!known.iter().any(|k| k.is_empty() || k == "/usr/bin"));
    }
}
