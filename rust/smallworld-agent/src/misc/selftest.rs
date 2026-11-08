//! The Panel -> Git self-test (v1.3): mc_panel.py `_no_token`, `selftest`, `SelfTestState`, `selftest_alert`,
//! `selftest_back_alert` (panel:502-675) and mc_agent.py `Agent.run_selftest`, `selftest_loop` (2350-2385).
//!
//! Proves the panel can still open a pull request, without opening one: the Worker mints a token, a fresh
//! `TokenCache` accepts it, the token's permissions are the ones the flow needs, and GitHub answers for the
//! config repo (the repository, main's head and players.yml as `open_pr` reads them, the pull request list).
//! Read-only: no branch, no commit, no pull request. Every error names the step; no token reaches a message.
//!
//! The probe is the GitHub client's read path (`GitHub(lambda: token, ...)`: GETs with a fixed token, so no
//! retry on 401) built here over crate::http; players.yml is parsed by sync's `parse_players`.

use super::token::{FetchFn, TokenCache};
use crate::http::{HttpError, Response};
use crate::jobs::{ClockFn, Event};
use crate::settings::{Env, url_hostname};
use crate::types::{AResult, AgentError, Obj, USER_AGENT, Value, py_str, truthy};
use crate::worlds::py_type;
use regex::Regex;
use serde_json::json;
use smallworld_core::pystr::{len_cp, prefix_cp};
use std::path::PathBuf;
use std::sync::{Arc, LazyLock, Mutex, PoisonError};
use std::time::Duration;

pub const SELFTEST_S: f64 = 86400.0;
pub const SELFTEST_FIRST_S: f64 = 300.0;
pub const SELFTEST_MIN_GAP_S: f64 = 60.0;
pub const SELFTEST_ALERT_AFTER: i64 = 2;
pub const SELFTEST_STEPS: [&str; 5] = ["token", "permissions", "repository", "main", "pulls"];
/// What the Worker narrows the token to (Python's dict order).
pub const SELFTEST_NEEDS: [(&str, &str); 2] = [("contents", "write"), ("pull_requests", "write")];

// --- the config repo (mc_panel.py github_from_env's checks) -----------------------------------------------

/// The config repo the panel's pull requests go to (mc_panel.py `GitHub`'s `repo`, `api`, `branch`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GhRepo {
    pub repo: String,
    pub api: String,
    pub branch: String,
}

static REPO_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$").expect("a constant pattern"));

impl GhRepo {
    /// `github_from_env(env, fetch_token)`'s checks (the daemon always has a token source): None without a
    /// valid CONFIG_REPO.
    ///
    /// # Errors
    /// `"GITHUB_API_URL must be https:// (http:// only for a loopback address)"` (Python's `ValueError`).
    pub fn from_env(e: &Env) -> Result<Option<GhRepo>, String> {
        let repo = e.get("CONFIG_REPO").map_or("", String::as_str);
        if !REPO_RE.is_match(repo) {
            return Ok(None);
        }
        let api = e
            .get("GITHUB_API_URL")
            .filter(|v| !v.is_empty())
            .map_or("https://api.github.com", String::as_str)
            .trim_end_matches('/')
            .to_string();
        let scheme = api.split_once("://").map(|(s, _)| s.to_lowercase());
        let host = url_hostname(&api);
        let loopback = scheme.as_deref() == Some("http")
            && matches!(host.as_deref(), Some("127.0.0.1" | "localhost" | "::1"));
        if scheme.as_deref() != Some("https") && !loopback {
            return Err(
                "GITHUB_API_URL must be https:// (http:// only for a loopback address)".into(),
            );
        }
        let branch = e
            .get("CONFIG_BRANCH")
            .filter(|v| !v.is_empty())
            .map_or("main", String::as_str)
            .to_string();
        Ok(Some(GhRepo {
            repo: repo.into(),
            api,
            branch,
        }))
    }
}

// --- the probe --------------------------------------------------------------------------------------------

/// One HTTP request (method, url, headers).
pub type HttpFn =
    Arc<dyn Fn(&str, &str, &[(&str, &str)]) -> Result<Response, HttpError> + Send + Sync>;

/// GitHub.call's transport: 20 s, the first 4 MiB of the reply.
pub fn http_default() -> HttpFn {
    Arc::new(|method, url, headers| {
        crate::http::request(method, url, headers, None, Duration::from_secs(20), 4 << 20)
    })
}

/// The exceptions the self-test's steps catch, with `str(e)`.
#[derive(Debug, Clone, PartialEq, Eq)]
enum StepError {
    GitHub(String),
    Key(String),
    Type(String),
    Value(String),
    /// anything the step doesn't catch (raised out of the self-test)
    Other(AgentError),
}

impl StepError {
    /// `str(e) or type(e).__name__`.
    fn text(&self) -> String {
        let (s, class) = match self {
            StepError::GitHub(s) => (s.clone(), "GitHubError"),
            StepError::Key(s) => (s.clone(), "KeyError"),
            StepError::Type(s) => (s.clone(), "TypeError"),
            StepError::Value(s) => (s.clone(), "ValueError"),
            StepError::Other(e) => (e.to_string(), "Exception"),
        };
        if s.is_empty() { class.into() } else { s }
    }

    fn into_agent(self) -> AgentError {
        match self {
            StepError::Other(e) => e,
            StepError::Value(m) => AgentError::Value("ValueError".into(), m),
            other => AgentError::Crash(other.text()),
        }
    }
}

/// `x[key]` on a value from `json.loads`, with Python's KeyError/TypeError.
fn subscript<'a>(v: &'a Value, key: &str) -> Result<&'a Value, StepError> {
    match v {
        Value::Object(o) => o
            .get(key)
            .ok_or_else(|| StepError::Key(smallworld_core::pyjson::repr_str(key))),
        Value::Array(_) => Err(StepError::Type(
            "list indices must be integers or slices, not str".into(),
        )),
        Value::String(_) => Err(StepError::Type(
            "string indices must be integers, not 'str'".into(),
        )),
        other => Err(StepError::Type(format!(
            "'{}' object is not subscriptable",
            py_type(other)
        ))),
    }
}

#[allow(clippy::cast_possible_truncation)] // bytes out of a 6-bit accumulator
/// `base64.b64decode(s)` (not validating: characters outside the alphabet are skipped).
fn b64decode(v: &Value) -> Result<Vec<u8>, StepError> {
    let Value::String(s) = v else {
        return Err(StepError::Type(format!(
            "argument should be a bytes-like object or ASCII string, not '{}'",
            py_type(v)
        )));
    };
    if !s.is_ascii() {
        return Err(StepError::Value(
            "string argument should contain only ASCII characters".into(),
        ));
    }
    let (mut out, mut quad, mut left, mut pads, mut chars) = (Vec::new(), 0u8, 0u32, 0u8, 0usize);
    for b in s.bytes() {
        if b == b'=' {
            if quad >= 2 {
                pads += 1;
                if quad + pads >= 4 {
                    return Ok(out);
                }
            }
            continue;
        }
        let val = match b {
            b'A'..=b'Z' => b - b'A',
            b'a'..=b'z' => b - b'a' + 26,
            b'0'..=b'9' => b - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => continue,
        };
        pads = 0;
        chars += 1;
        left = (left << 6) | u32::from(val);
        quad += 1;
        match quad {
            2 => out.push((left >> 4) as u8),
            3 => out.push((left >> 2) as u8),
            4 => {
                out.push(left as u8);
                quad = 0;
                left = 0;
            }
            _ => {}
        }
        left &= 0xff;
    }
    match quad {
        0 => Ok(out),
        1 => Err(StepError::Value(format!(
            "Invalid base64-encoded string: number of data characters ({chars}) cannot be 1 more than a \
             multiple of 4"
        ))),
        _ => Err(StepError::Value("Incorrect padding".into())),
    }
}

struct Probe<'a> {
    token: &'a str,
    gh: &'a GhRepo,
    http: &'a HttpFn,
}

impl Probe<'_> {
    /// `GitHub.call(method, path)` with a fixed token.
    fn call(&self, method: &str, path: &str) -> Result<Value, StepError> {
        let auth = format!("Bearer {}", self.token);
        let headers = [
            ("Authorization", auth.as_str()),
            ("Accept", "application/vnd.github+json"),
            ("X-GitHub-Api-Version", "2022-11-28"),
            ("User-Agent", USER_AGENT),
        ];
        let shown = path.split('?').next().unwrap_or(path);
        let raw = match (self.http)(method, &format!("{}{path}", self.gh.api), &headers) {
            Ok(r) => r.body,
            Err(HttpError::Status(code, _)) => {
                return Err(StepError::GitHub(format!("{method} {shown}: HTTP {code}")));
            }
            Err(HttpError::Transport(class)) => {
                return Err(StepError::GitHub(format!("{method} {shown}: {class}")));
            }
        };
        let raw = if raw.is_empty() { b"{}".to_vec() } else { raw };
        // json.loads(bytes): JSONDecodeError / UnicodeDecodeError, both ValueErrors (texts: PORTING.md)
        std::str::from_utf8(&raw)
            .ok()
            .and_then(smallworld_core::pyjson::loads)
            .ok_or_else(|| StepError::Value("Expecting value: line 1 column 1 (char 0)".into()))
    }

    /// `players_on_main()`, reduced to what the self-test reads: (whitelisted count, main's head).
    fn players_on_main(&self) -> Result<(usize, Value), StepError> {
        let (repo, branch) = (&self.gh.repo, &self.gh.branch);
        let r = self.call("GET", &format!("/repos/{repo}/git/ref/heads/{branch}"))?;
        let head = subscript(subscript(&r, "object")?, "sha")?.clone();
        let f = self.call(
            "GET",
            &format!("/repos/{repo}/contents/players.yml?ref={}", py_str(&head)),
        )?;
        let bytes = b64decode(subscript(&f, "content")?)?;
        let text = smallworld_core::pystr::decode_utf8_strict(&bytes)
            .map_err(|e| StepError::Value(AgentError::from(e).to_string()))?;
        let players = crate::sync::players::parse_players(&text).map_err(|e| match e {
            AgentError::Value(_, m) => StepError::Value(m),
            AgentError::Key(k) => StepError::Key(format!("'{k}'")),
            other => StepError::Other(other),
        })?;
        let _ = subscript(&f, "sha")?;
        let n = match players.get("whitelist") {
            Some(Value::Array(a)) => a.len(),
            _ => return Err(StepError::Key("'whitelist'".into())),
        };
        Ok((n, head))
    }
}

/// `_no_token(text, token)`.
fn no_token(text: &str, token: &str) -> String {
    if token.is_empty() {
        text.into()
    } else {
        text.replace(token, "<token>")
    }
}

struct Run {
    out: Obj,
    steps: Vec<Value>,
    error: String,
}

impl Run {
    fn step(&mut self, name: &str, ok: bool, detail: &str, token: &str) -> bool {
        let detail = prefix_cp(&no_token(detail, token), 200).to_string();
        if !ok && self.error.is_empty() {
            self.error = format!("{name}: {detail}");
        }
        self.steps
            .push(json!({"step": name, "ok": ok, "detail": detail}));
        ok
    }

    fn finish(mut self, ok: bool) -> Obj {
        self.out.insert("ok".into(), json!(ok));
        self.out.insert("steps".into(), Value::Array(self.steps));
        self.out.insert("error".into(), json!(self.error));
        self.out
    }
}

/// `selftest(fetch_token, gh, now, clock)`: `{ok, ts, configured, steps: [{step, ok, detail}], error}`.
///
/// # Errors
/// What Python doesn't catch inside (an `AttributeError` on a malformed repository reply, a JSON error in the
/// pulls step, sync's `NotPorted`).
#[allow(clippy::cast_possible_truncation)]
pub fn selftest(
    fetch_token: Option<&FetchFn>,
    gh: Option<&GhRepo>,
    now: f64,
    clock: ClockFn,
    http: &HttpFn,
) -> AResult<Obj> {
    let mut run = Run {
        out: Obj::new(),
        steps: Vec::new(),
        error: String::new(),
    };
    run.out
        .insert("ts".into(), json!((now * 1000.0).trunc() as i64));
    run.out.insert("configured".into(), json!(gh.is_some()));
    let (Some(gh), Some(fetch_token)) = (gh, fetch_token) else {
        run.error =
            "configuration: no config repo for the panel (CONFIG_REPO) or no token source".into();
        return Ok(run.finish(false));
    };
    let reply: Arc<Mutex<Value>> = Arc::new(Mutex::new(Value::Null));
    let fetch: FetchFn = {
        let (reply, f) = (reply.clone(), fetch_token.clone());
        Arc::new(move || {
            let r = f()?;
            *reply.lock().unwrap_or_else(PoisonError::into_inner) = r.clone();
            Ok(r)
        })
    };
    // a fresh cache: the Worker's answer is checked, not a remembered token
    let token = match TokenCache::new(fetch, clock).get() {
        Ok(t) => t,
        Err(e) => {
            run.step("token", false, &e.0, "");
            return Ok(run.finish(false));
        }
    };
    let t = token.as_str();
    run.step(
        "token",
        true,
        &format!(
            "the Worker minted a token ({} characters), TokenCache accepted it",
            len_cp(t)
        ),
        t,
    );
    let reply = reply.lock().unwrap_or_else(PoisonError::into_inner).clone();
    // the Worker reported GitHub's grant (checked below): permissions.push from GET /repos no longer decides
    let granted = matches!(reply.get("permissions"), Some(Value::Object(_)));
    if let Some(Value::Object(perms)) = reply.get("permissions") {
        let missing: Vec<String> = SELFTEST_NEEDS
            .iter()
            .filter(|(k, v)| perms.get(*k) != Some(&json!(v)))
            .map(|(k, v)| format!("{k}: {v}"))
            .collect();
        let why = if missing.is_empty() {
            "contents + pull_requests write".to_string()
        } else {
            format!("the token lacks {}", missing.join(", "))
        };
        if !run.step("permissions", missing.is_empty(), &why, t) {
            return Ok(run.finish(false));
        }
    } else {
        run.step(
            "permissions",
            true,
            "not reported by this Worker (checked through the repository below)",
            t,
        );
    }
    let probe = Probe { token: t, gh, http };
    let repo = &gh.repo;
    match probe.call("GET", &format!("/repos/{repo}")) {
        Ok(r) => {
            let push = match &r {
                Value::Object(o) => {
                    let p = o.get("permissions").filter(|v| truthy(v));
                    match p {
                        None => None,
                        Some(Value::Object(p)) => p.get("push").cloned(),
                        Some(other) => {
                            return Err(AgentError::Crash(format!(
                                "AttributeError: '{}' object has no attribute 'get'",
                                py_type(other)
                            )));
                        }
                    }
                }
                _ => None,
            };
            // v1.4.1: permissions.push describes a user's role, and is false for an App installation token even
            // with contents:write; it decides only when the Worker reported no grant (an older Worker)
            if push == Some(Value::Bool(false)) && granted {
                run.step(
                    "repository",
                    true,
                    &format!(
                        "{repo} readable; push comes from the token's contents:write grant \
                         (permissions.push describes a user role)"
                    ),
                    t,
                );
            } else if push == Some(Value::Bool(false)) {
                run.step(
                    "repository",
                    false,
                    &format!("{repo}: the token can't push (permissions.push is false)"),
                    t,
                );
                return Ok(run.finish(false));
            } else {
                let allowed = if push.as_ref().is_some_and(truthy) {
                    ", push allowed"
                } else {
                    ""
                };
                run.step("repository", true, &format!("{repo} readable{allowed}"), t);
            }
        }
        Err(StepError::Other(e)) => return Err(e),
        Err(e) => {
            run.step("repository", false, &e.text(), t);
            return Ok(run.finish(false));
        }
    }
    match probe.players_on_main() {
        Ok((n, head)) => {
            let detail = format!(
                "{} at {}, players.yml has {n} whitelisted",
                gh.branch,
                prefix_cp(&py_str(&head), 12)
            );
            run.step("main", true, &detail, t);
        }
        Err(StepError::Other(e)) => return Err(e),
        Err(e) => {
            run.step("main", false, &e.text(), t);
            return Ok(run.finish(false));
        }
    }
    match probe.call("GET", &format!("/repos/{repo}/pulls?state=open&per_page=1")) {
        Ok(Value::Array(_)) => {
            run.step(
                "pulls",
                true,
                "pull requests reachable (nothing was created)",
                t,
            );
        }
        Ok(_) => {
            run.step("pulls", false, "GET pulls: not a list", t);
            return Ok(run.finish(false));
        }
        Err(e @ StepError::GitHub(_)) => {
            run.step("pulls", false, &e.text(), t);
            return Ok(run.finish(false));
        }
        Err(e) => return Err(e.into_agent()),
    }
    Ok(run.finish(true))
}

// --- the Discord texts (alerts.py Alert + render) ---------------------------------------------------------

static IPV4: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\b(?:\d{1,3}\.){3}\d{1,3}\b").expect("a constant pattern"));
static IPV6: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{0,4}\b").expect("a constant pattern")
});

/// alerts.py `render(Alert(severity, headline, impact, cause, todo, details))` (`todo` "" = the default
/// "No action needed - informational."). Byte-exact; sync's alert texts use the same template.
pub fn render_alert(
    severity: &str,
    headline: &str,
    impact: &str,
    cause: &str,
    todo: &str,
    details: &str,
) -> String {
    let emoji = match severity {
        "info" => "\u{2139}\u{fe0f}",
        "warning" => "\u{26a0}\u{fe0f}",
        "action" => "\u{1f534}",
        _ => "\u{2705}",
    };
    let todo = if todo.is_empty() {
        "No action needed - informational."
    } else {
        todo
    };
    let mut lines = vec![format!("{emoji} **{headline}**")];
    for (label, value) in [
        ("Impact", impact),
        ("Likely cause", cause),
        ("What to do", todo),
    ] {
        if !value.is_empty() {
            lines.push(format!("**{label}:** {value}"));
        }
    }
    if !details.is_empty() {
        lines.push(format!("**Details:** {details}"));
    }
    let text = lines.join("\n");
    let text = IPV4.replace_all(&text, "<address>");
    let text = IPV6.replace_all(&text, "<address>").into_owned();
    if len_cp(&text) <= 1900 {
        text
    } else {
        format!("{}…", prefix_cp(&text, 1899))
    }
}

/// mc_panel.py `selftest_alert(error, failures)`: the daily self-test failed twice in a row.
pub fn selftest_alert(error: &str, failures: i64) -> String {
    render_alert(
        "warning",
        "The panel can't open pull requests on the config repo",
        "Whitelist and world changes made in the panel stay live but never reach Git; whitelist changes are \
         dropped after 24 h, and the next deploy undoes them.",
        &format!("The daily Panel -> Git self-test failed {failures} times in a row, at {error}"),
        "Run `mcctl doctor`, and the self-test from the panel's Health tab after a fix. Token or permissions: \
         the panel's GitHub App (installed on the config repo, Contents + Pull requests); a stale Worker: \
         `mcctl render` + `mcctl apply`.",
        &format!("{failures} consecutive failures; last: {error}"),
    )
}

/// mc_panel.py `selftest_back_alert(failures)`.
pub fn selftest_back_alert(failures: i64) -> String {
    render_alert(
        "recovered",
        "The panel can open pull requests again",
        "Panel whitelist and world changes reach the config repo again.",
        "The Panel -> Git self-test passed.",
        "",
        &format!("after {failures} failed run(s)"),
    )
}

// --- selftest.json (SelfTestState) ------------------------------------------------------------------------

/// `SelfTestState.load()`'s view of selftest.json.
#[derive(Debug, Clone, PartialEq)]
pub struct Loaded {
    pub last: Option<Obj>,
    pub last_panel: Option<Obj>,
    pub failures: i64,
    pub alerted: bool,
}

/// `int(x)` for `int(data.get("failures") or 0)`.
fn py_int(v: &Value) -> AResult<i64> {
    match v {
        Value::Null => Ok(0),
        Value::Bool(b) => Ok(i64::from(*b)),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                return Ok(i);
            }
            let f = n.as_f64().unwrap_or(0.0);
            if f.is_finite() && f.abs() < 9.2e18 {
                #[allow(clippy::cast_possible_truncation)]
                Ok(f.trunc() as i64)
            } else {
                Err(AgentError::Crash(
                    "OverflowError: cannot convert float infinity to integer".into(),
                ))
            }
        }
        Value::String(s) => {
            let t = smallworld_core::pystr::strip_ws(s);
            let (neg, body) = match t.strip_prefix('-') {
                Some(b) => (true, b),
                None => (false, t.strip_prefix('+').unwrap_or(t)),
            };
            crate::unicode::parse_decimal(body)
                .map(|n| if neg { -n } else { n })
                .ok_or_else(|| {
                    AgentError::Value(
                        "ValueError".into(),
                        format!(
                            "invalid literal for int() with base 10: {}",
                            smallworld_core::pyjson::repr_str(s)
                        ),
                    )
                })
        }
        other => Err(AgentError::Crash(format!(
            "TypeError: int() argument must be a string, a bytes-like object or a real number, not '{}'",
            py_type(other)
        ))),
    }
}

/// mc_panel.py `SelfTestState`: the last result and the failure streak in the agent's state dir (survives
/// restarts), so the daily run alerts once after SELFTEST_ALERT_AFTER consecutive failures, and once when it
/// passes again.
pub struct SelfTestState {
    pub path: PathBuf,
    lock: Mutex<()>,
}

impl SelfTestState {
    pub fn new(path: PathBuf) -> Self {
        Self {
            path,
            lock: Mutex::new(()),
        }
    }

    /// `load()`.
    ///
    /// # Errors
    /// `int()`'s errors for a `failures` that isn't a number.
    pub fn load(&self) -> AResult<Loaded> {
        let data = match smallworld_core::pyjson::read_json(&self.path) {
            Some(Value::Object(o)) => o,
            _ => Obj::new(),
        };
        let obj = |k: &str| data.get(k).and_then(Value::as_object).cloned();
        let failures = data
            .get("failures")
            .filter(|v| truthy(v))
            .map_or(Ok(0), py_int)?;
        Ok(Loaded {
            last: obj("last"),
            last_panel: obj("last_panel"),
            failures,
            alerted: data.get("alerted") == Some(&Value::Bool(true)),
        })
    }

    /// `record(result, alert)`: the new streak, written to selftest.json (0600, atomic).
    ///
    /// # Errors
    /// `load`'s, and the write's `OSError`.
    pub fn record(&self, result: &Obj, alert: Option<&dyn Fn(&str)>) -> AResult<Loaded> {
        let _g = self.lock.lock().unwrap_or_else(PoisonError::into_inner);
        let mut st = self.load()?;
        st.last = Some(result.clone());
        if result.get("trigger") == Some(&json!("panel")) {
            st.last_panel = Some(result.clone());
        }
        let ok = result.get("ok").is_some_and(truthy);
        let configured = result.get("configured").is_none_or(truthy);
        if ok || !configured {
            if st.alerted
                && ok
                && let Some(a) = alert
            {
                a(&selftest_back_alert(st.failures));
            }
            st.failures = 0;
            st.alerted = false;
        } else {
            st.failures += 1;
            if st.failures >= SELFTEST_ALERT_AFTER && !st.alerted {
                if let Some(a) = alert {
                    let error = result
                        .get("error")
                        .filter(|v| truthy(v))
                        .map_or_else(|| "unknown".to_string(), py_str);
                    a(&selftest_alert(&error, st.failures));
                }
                st.alerted = true;
            }
        }
        let opt = |o: &Option<Obj>| o.clone().map_or(Value::Null, Value::Object);
        let doc = json!({
            "last": opt(&st.last), "last_panel": opt(&st.last_panel),
            "failures": st.failures, "alerted": st.alerted,
        });
        smallworld_core::fsx::atomic_write(
            &self.path,
            smallworld_core::pyjson::dumps_sorted(&doc).as_bytes(),
            0o600,
        )?;
        Ok(st)
    }

    /// `public()`: the inventory's `selftest` (the last result's public fields + the streak), or None.
    ///
    /// # Errors
    /// `load`'s.
    pub fn public(&self) -> AResult<Option<Value>> {
        let st = self.load()?;
        let Some(last) = st.last.filter(|l| !l.is_empty()) else {
            return Ok(None);
        };
        let mut out = Obj::new();
        for k in ["ok", "ts", "configured", "error", "steps", "trigger"] {
            out.insert(k.into(), last.get(k).cloned().unwrap_or(Value::Null));
        }
        out.insert("failures".into(), json!(st.failures));
        Ok(Some(Value::Object(out)))
    }
}

// --- Agent.run_selftest + selftest_loop -------------------------------------------------------------------

/// `audit(message, **fields)`.
pub type AuditFn = Arc<dyn Fn(&str, &[(&str, &str)]) + Send + Sync>;

/// The agent's self-test: `Agent.run_selftest` and `Agent.selftest_loop` with what they use.
pub struct SelfTest {
    pub state: SelfTestState,
    /// `Agent.github` (None: no config repo, the daily run is skipped and a run reports "configuration").
    pub github: Option<GhRepo>,
    /// `Agent.github_token` (`POST /agent/github-token`).
    pub fetch_token: FetchFn,
    pub clock: ClockFn,
    pub http: HttpFn,
    /// `notify.discord`.
    pub alert: Arc<dyn Fn(&str) + Send + Sync>,
    /// `audit(message, **fields)`.
    pub audit: AuditFn,
    lock: Mutex<()>,
}

impl SelfTest {
    pub fn new(state_dir: &std::path::Path, github: Option<GhRepo>, fetch_token: FetchFn) -> Self {
        Self {
            state: SelfTestState::new(state_dir.join("selftest.json")),
            github,
            fetch_token,
            clock: Arc::new(crate::sys::now),
            http: http_default(),
            alert: Arc::new(crate::notify::discord),
            audit: Arc::new(smallworld_core::audit::audit),
            lock: Mutex::new(()),
        }
    }

    /// `run_selftest(trigger)`: recorded in selftest.json, audited; on demand ("panel") at most once per
    /// SELFTEST_MIN_GAP_S, counted from the last on-demand run (a daily run in between doesn't lift it).
    ///
    /// # Errors
    /// What `selftest` and `record` raise.
    pub fn run(&self, trigger: &str) -> AResult<Obj> {
        let _g = self.lock.lock().unwrap_or_else(PoisonError::into_inner);
        let prev = self.state.load()?.last_panel;
        if trigger == "panel"
            && let Some(prev) = prev.filter(|p| !p.is_empty())
            && let Some(ts) = prev.get("ts").and_then(crate::types::as_py_int_incl_bool)
        {
            #[allow(clippy::cast_precision_loss)]
            if (self.clock)() * 1000.0 - (ts as f64) < SELFTEST_MIN_GAP_S * 1000.0 {
                let mut out = prev;
                out.insert("cached".into(), json!(true));
                return Ok(out);
            }
        }
        let fetch = self.github.as_ref().map(|_| &self.fetch_token);
        let mut result = selftest(
            fetch,
            self.github.as_ref(),
            (self.clock)(),
            self.clock.clone(),
            &self.http,
        )?;
        result.insert("trigger".into(), json!(trigger));
        let alert = self.alert.clone();
        self.state.record(&result, Some(&move |m: &str| alert(m)))?;
        let ok = result.get("ok").is_some_and(truthy);
        let outcome = if ok {
            "ok".to_string()
        } else {
            prefix_cp(&result.get("error").map_or(String::new(), py_str), 300).to_string()
        };
        (self.audit)(
            &format!(
                "panel selftest ok={} trigger={trigger}",
                if ok { "True" } else { "False" }
            ),
            &[("action", "selftest"), ("result", &outcome)],
        );
        Ok(result)
    }

    /// `selftest_loop()`: daily, the first run SELFTEST_FIRST_S after start (a deploy restarts the agent: a
    /// broken release shows within minutes); only with a config repo. Runs until `stop`.
    pub fn run_loop(&self, stop: &Event, inventory_due: &Event) {
        let mut wait = SELFTEST_FIRST_S;
        while !stop.wait(wait) {
            wait = SELFTEST_S;
            if self.github.is_none() {
                continue;
            }
            let round =
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| self.run("daily")));
            match round {
                Ok(Ok(_)) => inventory_due.set(),
                Ok(Err(e)) => eprintln!("selftest failed: {}: {e}", e.class_name()),
                Err(_) => eprintln!("selftest failed: panic"),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::os::unix::fs::PermissionsExt;

    fn texts() -> Value {
        serde_json::from_str::<Value>(include_str!(
            "../../../../tests/agent_parity/vectors/texts.json"
        ))
        .unwrap()["vectors"]["alerts"]
            .clone()
    }

    #[test]
    fn alert_text_vectors() {
        let a = texts();
        assert_eq!(
            selftest_alert("token: no GitHub token from the Worker: HTTP 502", 2),
            a["selftest_alert"].as_str().unwrap()
        );
        assert_eq!(
            selftest_back_alert(3),
            a["selftest_back_alert"].as_str().unwrap()
        );
        // addresses are scrubbed; long texts are cut at 1900 code points
        // python3 -c 'import alerts; print(alerts.scrub("192.0.2.3 and fe80::1:2 and 2001:db8:0:0:0:0:0:1 x"))'
        let t = selftest_alert("192.0.2.3 and fe80::1:2 and 2001:db8:0:0:0:0:0:1 x", 2);
        assert!(t.contains("<address> and fe80::1:2 and <address> x"), "{t}");
        let long = selftest_alert(&"é".repeat(3000), 2);
        assert_eq!(len_cp(&long), 1900);
        assert!(long.ends_with('…'));
    }

    struct Gh {
        routes: HashMap<String, Result<Value, u16>>,
    }

    fn http(gh: Arc<Gh>, seen: Arc<Mutex<Vec<String>>>) -> HttpFn {
        Arc::new(move |method, url, headers| {
            assert_eq!(method, "GET");
            assert!(headers.contains(&(
                "Authorization",
                "Bearer ghs_TTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTT"
            )));
            let path = url.strip_prefix("http://127.0.0.1:1").unwrap();
            seen.lock().unwrap().push(path.to_string());
            match gh.routes.get(path) {
                Some(Ok(v)) => Ok(Response {
                    status: 200,
                    body: v.to_string().into_bytes(),
                }),
                Some(Err(code)) => Err(HttpError::Status(*code, Vec::new())),
                None => Err(HttpError::Status(404, Vec::new())),
            }
        })
    }

    fn good_token() -> Value {
        json!({"token": format!("ghs_{}", "T".repeat(36)), "expires_at": "2099-01-01T00:00:00Z",
               "permissions": {"contents": "write", "pull_requests": "write"}})
    }

    fn repo() -> GhRepo {
        GhRepo {
            repo: "o/c".into(),
            api: "http://127.0.0.1:1".into(),
            branch: "main".into(),
        }
    }

    fn routes(push: Value) -> HashMap<String, Result<Value, u16>> {
        HashMap::from([
            (
                "/repos/o/c".to_string(),
                Ok(json!({"permissions": {"push": push}})),
            ),
            (
                "/repos/o/c/git/ref/heads/main".to_string(),
                Ok(json!({"object": {"sha": "a1b2c3d4a1b2c3d4"}})),
            ),
            (
                "/repos/o/c/contents/players.yml?ref=a1b2c3d4a1b2c3d4".to_string(),
                Ok(json!({"sha": "blob", "content": "d2hp\ndGVsaXN0OiBbXQo=\n"})),
            ),
            (
                "/repos/o/c/pulls?state=open&per_page=1".to_string(),
                Ok(json!([])),
            ),
        ])
    }

    fn steps(r: &Obj) -> Vec<String> {
        r["steps"]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| {
                format!(
                    "{}={}: {}",
                    s["step"].as_str().unwrap(),
                    s["ok"],
                    s["detail"].as_str().unwrap()
                )
            })
            .collect()
    }

    #[test]
    fn steps_up_to_main() {
        // players.yml parsing is sync's (stubbed until it lands): everything before it, here
        let seen = Arc::new(Mutex::new(Vec::new()));
        let gh = Arc::new(Gh {
            routes: routes(json!(false)),
        });
        let fetch: FetchFn = Arc::new(|| Ok(good_token()));
        let r = selftest(
            Some(&fetch),
            Some(&repo()),
            1.5,
            Arc::new(|| 0.0),
            &http(gh, seen.clone()),
        )
        .unwrap();
        assert_eq!(r["ts"], json!(1500));
        // v1.4.1, the live case: the Worker reported contents + pull_requests write, GET /repos says
        // permissions.push false (a user's role, false for an App installation token): the grant decides
        assert_eq!(
            steps(&r)[..3],
            [
                "token=true: the Worker minted a token (40 characters), TokenCache accepted it",
                "permissions=true: contents + pull_requests write",
                "repository=true: o/c readable; push comes from the token's contents:write grant \
                 (permissions.push describes a user role)",
            ]
        );
        assert!(!r["error"].as_str().unwrap().starts_with("repository"));

        // an older Worker reports no grant: permissions.push false still fails the step
        let mut t = good_token();
        t.as_object_mut().unwrap().remove("permissions");
        let fetch: FetchFn = Arc::new(move || Ok(t.clone()));
        let gh = Arc::new(Gh {
            routes: routes(json!(false)),
        });
        let r = selftest(
            Some(&fetch),
            Some(&repo()),
            1.5,
            Arc::new(|| 0.0),
            &http(gh, seen.clone()),
        )
        .unwrap();
        assert_eq!(r["ok"], json!(false));
        assert_eq!(
            steps(&r),
            [
                "token=true: the Worker minted a token (40 characters), TokenCache accepted it",
                "permissions=true: not reported by this Worker (checked through the repository below)",
                "repository=false: o/c: the token can't push (permissions.push is false)",
            ]
        );
        assert_eq!(
            r["error"],
            json!("repository: o/c: the token can't push (permissions.push is false)")
        );

        let mut t = good_token();
        t["permissions"] = json!({"contents": "read"});
        let fetch: FetchFn = Arc::new(move || Ok(t.clone()));
        let gh = Arc::new(Gh {
            routes: routes(json!(true)),
        });
        let r = selftest(
            Some(&fetch),
            Some(&repo()),
            1.0,
            Arc::new(|| 0.0),
            &http(gh, seen.clone()),
        )
        .unwrap();
        assert_eq!(
            r["error"],
            json!("permissions: the token lacks contents: write, pull_requests: write")
        );

        let fetch: FetchFn = Arc::new(|| Err("POST /agent/github-token: HTTP 502".into()));
        let gh = Arc::new(Gh {
            routes: routes(json!(true)),
        });
        let r = selftest(
            Some(&fetch),
            Some(&repo()),
            1.0,
            Arc::new(|| 0.0),
            &http(gh, seen.clone()),
        )
        .unwrap();
        assert_eq!(
            r["error"],
            json!("token: no GitHub token from the Worker: POST /agent/github-token: HTTP 502")
        );
        let r = selftest(None, None, 1.0, Arc::new(|| 0.0), &http_default()).unwrap();
        assert_eq!(r["configured"], json!(false));
        assert_eq!(r["steps"], json!([]));

        // a GitHub error names the request, not the query or the token
        let mut rt = routes(json!(true));
        rt.insert("/repos/o/c".into(), Err(403));
        let fetch: FetchFn = Arc::new(|| Ok(good_token()));
        let r = selftest(
            Some(&fetch),
            Some(&repo()),
            1.0,
            Arc::new(|| 0.0),
            &http(Arc::new(Gh { routes: rt }), seen),
        )
        .unwrap();
        assert_eq!(r["error"], json!("repository: GET /repos/o/c: HTTP 403"));
    }

    #[test]
    fn python_subscripts_and_base64() {
        assert_eq!(
            subscript(&json!({}), "object"),
            Err(StepError::Key("'object'".into()))
        );
        assert_eq!(
            subscript(&json!(null), "sha").unwrap_err().text(),
            "'NoneType' object is not subscriptable"
        );
        assert_eq!(
            b64decode(&json!("d2hp\ndGVsaXN0OiBbXQo=\n")).unwrap(),
            b"whitelist: []\n"
        );
        assert_eq!(
            b64decode(&json!("YQ")).unwrap_err().text(),
            "Incorrect padding"
        );
        assert_eq!(b64decode(&json!("YQ==garbage")).unwrap(), b"a");
        assert_eq!(
            b64decode(&json!("YWJjZ")).unwrap_err().text(),
            "Invalid base64-encoded string: number of data characters (5) cannot be 1 more than a multiple of 4"
        );
        assert_eq!(
            b64decode(&json!(5)).unwrap_err().text(),
            "argument should be a bytes-like object or ASCII string, not 'int'"
        );
    }

    #[test]
    fn state_alerts_after_two_failures_and_on_recovery() {
        let d = std::env::temp_dir().join(format!("swagent-selftest-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let st = SelfTestState::new(d.join("selftest.json"));
        let sent = Arc::new(Mutex::new(Vec::<String>::new()));
        let s2 = sent.clone();
        let alert = move |m: &str| s2.lock().unwrap().push(m.to_string());
        let fail = json!({"ok": false, "configured": true, "error": "token: x", "trigger": "daily", "ts": 1});
        let fail = fail.as_object().unwrap();
        assert_eq!(st.record(fail, Some(&alert)).unwrap().failures, 1);
        assert!(sent.lock().unwrap().is_empty());
        let l = st.record(fail, Some(&alert)).unwrap();
        assert!(l.alerted && l.failures == 2 && l.last_panel.is_none());
        assert_eq!(
            sent.lock().unwrap().as_slice(),
            [selftest_alert("token: x", 2)]
        );
        st.record(fail, Some(&alert)).unwrap();
        assert_eq!(sent.lock().unwrap().len(), 1);
        let ok = json!({"ok": true, "configured": true, "error": "", "trigger": "panel", "ts": 2});
        let l = st.record(ok.as_object().unwrap(), Some(&alert)).unwrap();
        assert!(!l.alerted && l.failures == 0 && l.last_panel.is_some());
        assert_eq!(sent.lock().unwrap()[1], selftest_back_alert(3));
        let p = st.public().unwrap().unwrap();
        assert_eq!(p["failures"], json!(0));
        assert_eq!(p["trigger"], json!("panel"));
        let on_disk: Value =
            serde_json::from_str(&std::fs::read_to_string(&st.path).unwrap()).unwrap();
        assert_eq!(on_disk["last_panel"]["ts"], json!(2));
        assert_eq!(
            std::fs::metadata(&st.path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        std::fs::write(&st.path, r#"{"failures": "3", "alerted": 1}"#).unwrap();
        let l = st.load().unwrap();
        assert_eq!((l.failures, l.alerted), (3, false));
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn on_demand_runs_are_rate_limited_from_the_last_on_demand_run() {
        let d = std::env::temp_dir().join(format!("swagent-selftest-run-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let now = Arc::new(Mutex::new(1000.0));
        let n2 = now.clone();
        let audits = Arc::new(Mutex::new(Vec::<String>::new()));
        let a2 = audits.clone();
        let mut t = SelfTest::new(&d, None, Arc::new(|| Ok(good_token())));
        t.clock = Arc::new(move || *n2.lock().unwrap());
        t.alert = Arc::new(|_| {});
        t.audit = Arc::new(move |m, f| a2.lock().unwrap().push(format!("{m} {f:?}")));
        let r = t.run("panel").unwrap();
        assert!(r.get("cached").is_none());
        assert_eq!(r["trigger"], json!("panel"));
        *now.lock().unwrap() = 1030.0;
        assert_eq!(t.run("panel").unwrap()["cached"], json!(true));
        *now.lock().unwrap() = 1040.0;
        assert!(t.run("daily").unwrap().get("cached").is_none());
        // the daily run in between doesn't lift the limit
        *now.lock().unwrap() = 1050.0;
        assert_eq!(t.run("panel").unwrap()["cached"], json!(true));
        *now.lock().unwrap() = 1061.0;
        assert!(t.run("panel").unwrap().get("cached").is_none());
        assert_eq!(
            audits.lock().unwrap()[0],
            r#"panel selftest ok=False trigger=panel [("action", "selftest"), ("result", "configuration: no config repo for the panel (CONFIG_REPO) or no token source")]"#
        );
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn config_repo_from_env() {
        let env = |pairs: &[(&str, &str)]| -> Env {
            pairs
                .iter()
                .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
                .collect()
        };
        assert_eq!(GhRepo::from_env(&env(&[])), Ok(None));
        assert_eq!(GhRepo::from_env(&env(&[("CONFIG_REPO", "bad")])), Ok(None));
        assert_eq!(
            GhRepo::from_env(&env(&[("CONFIG_REPO", "o/c")])),
            Ok(Some(GhRepo {
                repo: "o/c".into(),
                api: "https://api.github.com".into(),
                branch: "main".into()
            }))
        );
        assert_eq!(
            GhRepo::from_env(&env(&[
                ("CONFIG_REPO", "o/c"),
                ("GITHUB_API_URL", "http://127.0.0.1:8788/"),
                ("CONFIG_BRANCH", "prod")
            ])),
            Ok(Some(GhRepo {
                repo: "o/c".into(),
                api: "http://127.0.0.1:8788".into(),
                branch: "prod".into()
            }))
        );
        assert!(
            GhRepo::from_env(&env(&[
                ("CONFIG_REPO", "o/c"),
                ("GITHUB_API_URL", "http://10.0.0.1")
            ]))
            .is_err()
        );
    }
}
